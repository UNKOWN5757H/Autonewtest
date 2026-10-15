import asyncio
import html
import inspect
import re
import time
from logging import INFO, getLogger
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    QueryIdInvalid,
    UserIsBlocked,
    UsernameInvalid,
    UsernameNotModified,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from database.ia_filterdb import Media
from utils import get_size, temp

try:
    from database.ia_filterdb import save_batch
except ImportError:
    save_batch = None

try:
    from database.ia_filterdb import save_file
except ImportError:
    save_file = None

LOG_CHANNEL = getattr(info, "INDEX_REQ_CHANNEL", getattr(info, "LOG_CHANNEL", 0))

logger = getLogger(__name__)
logger.setLevel(INFO)

# ============================================================
# 🛡️ PYROGRAM v2.2.26 (LAYER 230) SAFE SEND HELPERS
# ============================================================
_REPLY_PARAMS = set(inspect.signature(Message.reply_text).parameters.keys())
_EDIT_PARAMS = set(inspect.signature(Message.edit_text).parameters.keys())


async def safe_reply_text(message: Message, text: str, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None
    except Exception as e:
        logger.error(f"safe_reply_text error in index.py: {e}")
        return None


async def safe_edit_text(message: Message, text: str, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except (MessageNotModified, MessageIdInvalid, ChatWriteForbidden, Forbidden):
        return None
    except Exception:
        return None


async def safe_answer(
    query: CallbackQuery, text: Optional[str] = None, show_alert: bool = False
):
    if not query:
        return
    try:
        if text is not None:
            await query.answer(str(text)[:200], show_alert=show_alert)
        else:
            await query.answer(show_alert=show_alert)
    except (QueryIdInvalid, Exception):
        pass


# ============================================================
# ⚙️ MONGODB SETUP FOR RESUME, CLEAN WORDS & INDEX SETTINGS
# ============================================================
resume_db = cw_db = idx_db = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    resume_db = _BOT_DB["index_resume"]
    cw_db = _BOT_DB["clean_words"]
    idx_db = _BOT_DB["index_settings"]
except Exception as e:
    logger.error(f"Failed to init advanced DBs: {e}")


# ============================================================
# 👑 SMART AUTHORIZATION ENGINE (MESSAGES + CALLBACKS)
# ============================================================
def get_admin_list():
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        raw_admins = str(raw_admins).replace(",", " ").split()
    elif isinstance(raw_admins, (list, tuple, set)):
        raw_admins = list(raw_admins)
    else:
        raw_admins = []
    return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]


async def is_user_admin(client: Client, message: Message) -> bool:
    admin_list = get_admin_list()

    if message.from_user and message.from_user.id in admin_list:
        return True
    if message.sender_chat and message.sender_chat.id in admin_list:
        return True
    if message.chat and message.chat.type in [
        enums.ChatType.GROUP,
        enums.ChatType.SUPERGROUP,
    ]:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True
        if message.from_user:
            try:
                member = await client.get_chat_member(
                    message.chat.id, message.from_user.id
                )
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
            except Exception:
                pass
    return False


async def is_cb_admin(client: Client, query: CallbackQuery) -> bool:
    admin_list = get_admin_list()

    if query.from_user and query.from_user.id in admin_list:
        return True
    if (
        query.message
        and query.message.chat
        and query.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    ):
        if query.from_user:
            try:
                member = await client.get_chat_member(
                    query.message.chat.id, query.from_user.id
                )
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
            except Exception:
                pass
    return False


# ============================================================
# 🗄️ DATABASE & FORWARD HELPERS
# ============================================================
async def safe_save_batch(media_list: list):
    if save_batch is not None:
        return await save_batch(media_list)
    if save_file is not None:
        saved, dup, err = 0, 0, 0
        for media in media_list:
            try:
                res = await save_file(media)
                if isinstance(res, tuple):
                    ok, status = res[0], res[1]
                    if ok:
                        saved += 1
                    elif status == 0:
                        dup += 1
                    else:
                        err += 1
                elif res == "suc":
                    saved += 1
                elif res == "dup":
                    dup += 1
                else:
                    err += 1
            except Exception:
                err += 1
        return saved, dup, err
    return 0, 0, len(media_list)


def extract_forward_info(message: Message):
    """Supports both standard Pyrogram and Pyrofork/Kurigram Layer 230+ forward origins."""
    if (
        getattr(message, "forward_from_chat", None)
        and message.forward_from_chat.type == enums.ChatType.CHANNEL
    ):
        return message.forward_from_chat.id, message.forward_from_message_id

    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "chat", None):
        if origin.chat.type == enums.ChatType.CHANNEL:
            return origin.chat.id, getattr(origin, "message_id", 0)

    return None, None


async def save_resume_state(chat_id, last_processed_id):
    if resume_db is not None:
        await resume_db.update_one(
            {"chat_id": chat_id}, {"$set": {"last_id": last_processed_id}}, upsert=True
        )


async def get_resume_state(chat_id):
    if resume_db is not None:
        doc = await resume_db.find_one({"chat_id": chat_id})
        return doc.get("last_id", 0) if doc else 0
    return 0


async def get_clean_words():
    default = ["sandalwood", "mkv", "mp4", "avi", "webm", "zip", "rar"]
    if cw_db is None:
        return default
    try:
        doc = await cw_db.find_one({"id": "words"})
        if doc is None or doc.get("use_default", True):
            return default
        return doc.get("list", [])
    except Exception:
        return default


async def get_idx_settings():
    default_settings = {
        "min_size": 0,
        "blacklist": ["trailer", "promo", "teaser", "sample"],
        "whitelist": [],
        "auto_backup": False,
        "backup_channel": None,
    }
    if idx_db is None:
        return default_settings
    try:
        doc = await idx_db.find_one({"id": "config"})
        if not doc:
            return default_settings
        for k, v in default_settings.items():
            if k not in doc:
                doc[k] = v
        return doc
    except Exception:
        return default_settings


async def save_idx_settings(key, value):
    if idx_db is not None:
        await idx_db.update_one({"id": "config"}, {"$set": {key: value}}, upsert=True)


def clean_filename(name: str, clean_words: list) -> str:
    if not name:
        return "File"
    if any(w.lower() in ["mkv", "sandalwood"] for w in clean_words):
        name = re.sub(r"(?i)\[?@?sandalwood[^\]\s]*\]?", "", name)
        name = re.sub(r"(?i)\b(sandalwood|mkv|mp4|avi|webm|zip|rar)\b", "", name)
    name = re.sub(r"[_.-]", " ", name)
    for word in clean_words:
        if word.lower() in ["mkv", "sandalwood", "mp4", "avi", "webm", "zip", "rar"]:
            continue
        name = re.sub(rf"(?i){re.escape(word)}", "", name)
    return re.sub(r"\s+", " ", name).strip()


def parse_channels(chan_var):
    if isinstance(chan_var, list):
        return [int(x) for x in chan_var if str(x).lstrip("-").isdigit()]
    if isinstance(chan_var, str):
        return [
            int(x)
            for x in chan_var.replace(",", " ").split()
            if x.strip().lstrip("-").isdigit()
        ]
    if isinstance(chan_var, int):
        return [chan_var]
    return []


AUTO_INDEX_CHANNELS = []
try:
    AUTO_INDEX_CHANNELS.extend(parse_channels(getattr(info, "CHANNELS", [])))
except Exception:
    pass
try:
    AUTO_INDEX_CHANNELS.extend(parse_channels(getattr(info, "INDEX_CHANNELS", [])))
except Exception:
    pass
AUTO_INDEX_CHANNELS = list(set(AUTO_INDEX_CHANNELS))
lock = asyncio.Lock()


class SafeMedia:
    def __init__(self, media_obj, file_type, caption, override_name=None):
        self.file_id = getattr(media_obj, "file_id", "")
        self.file_unique_id = getattr(media_obj, "file_unique_id", "")
        self.file_name = (
            override_name if override_name else getattr(media_obj, "file_name", "")
        )
        self.file_size = getattr(media_obj, "file_size", 0)
        self.mime_type = getattr(media_obj, "mime_type", "")
        self.file_type = file_type
        self.caption = caption


# ============================================================
# 🤖 AUTO INDEX LISTENER
# ============================================================
@Client.on_message(
    filters.channel
    & (filters.document | filters.video | filters.audio)
    & ~filters.forwarded,
    group=-5,
)
async def auto_index_new_files(bot: Client, message: Message):
    if AUTO_INDEX_CHANNELS and message.chat.id not in AUTO_INDEX_CHANNELS:
        return

    media_obj = getattr(message, message.media.value, None)
    if not media_obj:
        return

    idx_settings = await get_idx_settings()
    if getattr(media_obj, "file_size", 0) < idx_settings.get("min_size", 0):
        return

    raw_name, raw_caption = (
        getattr(media_obj, "file_name", "Unknown") or "Unknown",
        message.caption or "",
    )
    blacklist, whitelist = idx_settings.get("blacklist", []), idx_settings.get(
        "whitelist", []
    )

    if any(
        b.lower() in raw_name.lower() or b.lower() in raw_caption.lower()
        for b in blacklist
    ):
        return
    if whitelist and not any(
        w.lower() in raw_name.lower() or w.lower() in raw_caption.lower()
        for w in whitelist
    ):
        return

    target_media, target_caption = media_obj, message.caption
    if idx_settings.get("auto_backup") and idx_settings.get("backup_channel"):
        try:
            copied_msg = await message.copy(chat_id=idx_settings.get("backup_channel"))
            target_media = getattr(copied_msg, copied_msg.media.value, media_obj)
            target_caption = copied_msg.caption
        except Exception as e:
            logger.error(f"Auto-backup failed for {message.chat.id}: {e}")

    clean_words = await get_clean_words()
    cleaned_name = clean_filename(
        getattr(target_media, "file_name", "Unknown") or "Unknown", clean_words
    )
    safe_media = SafeMedia(
        target_media, message.media.value, target_caption, cleaned_name
    )

    try:
        await safe_save_batch([safe_media])
    except Exception as e:
        logger.error(f"Auto-index failed for {message.chat.title}: {e}")


# ============================================================
# ⚡ PM MEDIA FORWARD HANDLER
# ============================================================
@Client.on_message(
    filters.private & (filters.document | filters.video | filters.audio),
    group=-5,
)
async def pm_media_handler(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return

    # Do not intercept if admin is answering an interactive Help button prompt
    try:
        from plugins.uibuttons import is_waiting_for_cmd_input

        if is_waiting_for_cmd_input(message.chat.id):
            return
    except Exception:
        pass

    fwd_chat_id, fwd_msg_id = extract_forward_info(message)
    if fwd_chat_id and fwd_msg_id:
        uid = message.from_user.id if message.from_user else 0
        buttons = [
            [
                InlineKeyboardButton(
                    "Index Whole Channel",
                    callback_data=f"idx#ac#{fwd_chat_id}#{fwd_msg_id}#{uid}#all",
                )
            ],
            [
                InlineKeyboardButton(
                    "Save Only This File", callback_data=f"save_single#{message.id}"
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data="idx_close")],
        ]
        await safe_reply_text(
            message,
            "📂 <b>File received from a channel.</b>\n\nWhat would you like to do?",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await save_single_file(bot, message)


@Client.on_callback_query(filters.regex(r"^save_single#"), group=-5)
async def save_single_callback(bot: Client, query: CallbackQuery):
    if not await is_cb_admin(bot, query):
        return await safe_answer(query, "❌ Unauthorized: Admin only.", show_alert=True)

    msg_id = int(query.data.split("#")[1])
    try:
        original_message = await bot.get_messages(query.message.chat.id, msg_id)
        await query.message.delete()
        if original_message:
            await save_single_file(bot, original_message)
    except Exception:
        await safe_answer(query, "Failed to process file.", show_alert=True)


async def save_single_file(bot: Client, message: Message):
    media_obj = getattr(message, message.media.value, None)
    if not media_obj:
        return

    idx_settings = await get_idx_settings()
    if getattr(media_obj, "file_size", 0) < idx_settings.get("min_size", 0):
        return await safe_reply_text(
            message,
            "⚠️ <b>Ignored:</b> File size is smaller than minimum limit.",
            parse_mode=enums.ParseMode.HTML,
        )

    raw_name, raw_caption = (
        getattr(media_obj, "file_name", "Unknown") or "Unknown",
        message.caption or "",
    )
    blacklist, whitelist = idx_settings.get("blacklist", []), idx_settings.get(
        "whitelist", []
    )

    if any(
        b.lower() in raw_name.lower() or b.lower() in raw_caption.lower()
        for b in blacklist
    ):
        return await safe_reply_text(
            message,
            "⚠️ <b>Ignored:</b> File contains blacklisted words.",
            parse_mode=enums.ParseMode.HTML,
        )
    if whitelist and not any(
        w.lower() in raw_name.lower() or w.lower() in raw_caption.lower()
        for w in whitelist
    ):
        return await safe_reply_text(
            message,
            "⚠️ <b>Ignored:</b> File does not contain whitelisted words.",
            parse_mode=enums.ParseMode.HTML,
        )

    target_media, target_caption = media_obj, message.caption
    if idx_settings.get("auto_backup") and idx_settings.get("backup_channel"):
        try:
            copied_msg = await message.copy(chat_id=idx_settings.get("backup_channel"))
            target_media = getattr(copied_msg, copied_msg.media.value, media_obj)
            target_caption = copied_msg.caption
        except Exception:
            pass

    clean_words = await get_clean_words()
    cleaned_name = clean_filename(
        getattr(target_media, "file_name", "Unknown") or "Unknown", clean_words
    )
    safe_media = SafeMedia(
        target_media, message.media.value, target_caption, cleaned_name
    )

    try:
        await safe_save_batch([safe_media])
        await safe_reply_text(
            message,
            f"✅ <b>Successfully Saved to Database!</b>\n\n<code>{html.escape(cleaned_name)}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"PM Auto-index failed: {e}")
        await safe_reply_text(
            message,
            f"❌ <b>Failed to index:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# 🎛️ ADVANCED COMMAND CONTROLS (SKIP, SPEED, FILTERS, BACKUP)
# ============================================================
@Client.on_message(filters.command("setskip"), group=-5)
async def set_skip_number(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) > 1:
        try:
            skip = int(message.command[1])
            if skip < 0:
                return await safe_reply_text(
                    message,
                    "⚠️ Skip number must be 0 or greater.",
                    parse_mode=enums.ParseMode.HTML,
                )
            temp.CURRENT = skip
            await safe_reply_text(
                message,
                f"✅ Successfully set default SKIP number to <code>{skip}</code>.",
                parse_mode=enums.ParseMode.HTML,
            )
        except ValueError:
            await safe_reply_text(
                message,
                "⚠️ Skip number must be an integer.",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/setskip 100</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("deleteskip"), group=-5)
async def delete_skip_number(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    temp.CURRENT = 0
    await safe_reply_text(
        message,
        "✅ Successfully reset SKIP number to <code>0</code>.",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("currentskip"), group=-5)
async def current_skip_number(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    current = getattr(temp, "CURRENT", 0)
    await safe_reply_text(
        message,
        f"ℹ️ The current default SKIP number is: <code>{current}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setindexspeed"), group=-5)
async def set_index_speed_cmd(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        cur = getattr(temp, "INDEX_SPEED", 0)
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/setindexspeed &lt;seconds&gt;</code>\n⏱️ <b>Current Delay:</b> <code>{cur}s</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        delay = max(0.0, float(message.command[1]))
        temp.INDEX_SPEED = delay
        await safe_reply_text(
            message,
            f"✅ <b>Indexing batch delay set to:</b> <code>{delay}s</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except ValueError:
        await safe_reply_text(
            message,
            "⚠️ Delay must be a valid number.",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("setminsize"), group=-5)
async def set_min_size_cmd(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2 or not message.command[1].isdigit():
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/setminsize &lt;size_in_MB&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    mb_val = int(message.command[1])
    await save_idx_settings("min_size", mb_val * 1024 * 1024)
    await safe_reply_text(
        message,
        f"✅ <b>Minimum file size filter set to:</b> <code>{mb_val} MB</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(
    filters.command(
        [
            "setblacklist",
            "remblacklist",
            "allblacklist",
            "setwhitelist",
            "remwhitelist",
            "allwhitelist",
        ]
    ),
    group=-5,
)
async def manage_index_word_lists(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    cmd = message.command[0].lower()
    is_black = "black" in cmd
    key = "blacklist" if is_black else "whitelist"
    label = "Blacklist" if is_black else "Whitelist"

    idx_settings = await get_idx_settings()
    cur_list = list(idx_settings.get(key, []))

    if cmd.startswith("all"):
        words_str = (
            ", ".join(f"<code>{html.escape(w)}</code>" for w in cur_list)
            if cur_list
            else "<i>None</i>"
        )
        return await safe_reply_text(
            message,
            f"📋 <b>Active Index {label} Words ({len(cur_list)}):</b>\n\n{words_str}",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/{cmd} word1, word2</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    input_words = [
        w.strip() for w in message.text.split(None, 1)[1].split(",") if w.strip()
    ]
    if cmd.startswith("set"):
        merged = list(dict.fromkeys(cur_list + input_words))
        await save_idx_settings(key, merged)
        return await safe_reply_text(
            message,
            f"✅ <b>Updated Index {label} ({len(merged)} words)!</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        lower_rm = {w.lower() for w in input_words}
        remaining = [w for w in cur_list if w.lower() not in lower_rm]
        await save_idx_settings(key, remaining)
        return await safe_reply_text(
            message,
            f"🗑️ <b>Removed from Index {label}. Remaining:</b> <code>{len(remaining)}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command(["setbackupchannel", "autobackup"]), group=-5)
async def manage_index_backup_cmd(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    cmd = message.command[0].lower()
    idx_settings = await get_idx_settings()

    if cmd == "setbackupchannel":
        if len(message.command) < 2:
            return await safe_reply_text(
                message,
                "⚙️ <b>Usage:</b> <code>/setbackupchannel -1001234567890</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        try:
            cid = int(message.command[1].strip())
            await save_idx_settings("backup_channel", cid)
            return await safe_reply_text(
                message,
                f"✅ <b>Backup Channel ID set to:</b> <code>{cid}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        except ValueError:
            return await safe_reply_text(
                message,
                "❌ Invalid Channel ID.",
                parse_mode=enums.ParseMode.HTML,
            )

    new_state = not bool(idx_settings.get("auto_backup", False))
    await save_idx_settings("auto_backup", new_state)
    await safe_reply_text(
        message,
        f"🛡️ <b>Auto-Backup Shield:</b> <code>{'ENABLED ✅' if new_state else 'DISABLED ❌'}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 🧭 CALLBACK HANDLERS & PROCESSORS
# ============================================================
@Client.on_callback_query(filters.regex(r"^(idx#|idx_cancel#|idx_close)"), group=-5)
async def index_files(bot: Client, query: CallbackQuery):
    if not await is_cb_admin(bot, query):
        return await safe_answer(query, "❌ Unauthorized: Admin only.", show_alert=True)

    if query.data == "idx_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return await safe_answer(query)

    if query.data.startswith("idx_cancel#"):
        try:
            _, raw_chat_id = query.data.split("#", 1)
            chat_id = (
                int(raw_chat_id) if raw_chat_id.lstrip("-").isdigit() else raw_chat_id
            )
            if not hasattr(temp, "INDEX_CANCEL"):
                temp.INDEX_CANCEL = {}
            temp.INDEX_CANCEL[chat_id] = True
            await safe_answer(query, "Cancelling Indexing...", show_alert=True)
            await safe_edit_text(
                query.message,
                "⏳ <b>Stopping index process...</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        except Exception:
            return await safe_answer(query, "Cancel signal sent.", show_alert=True)

    data_parts = query.data.split("#")
    if len(data_parts) == 6:
        _, action, chat, lst_msg_id, from_user, filter_type = data_parts
    else:
        return await safe_answer(
            query, "Invalid callback data structure.", show_alert=True
        )

    if lock.locked():
        return await safe_answer(
            query,
            "⚠️ Another index process is currently running. Please wait.",
            show_alert=True,
        )

    msg = query.message
    await safe_answer(query, "Starting Indexing...⏳", show_alert=False)

    chat = int(chat) if str(chat).lstrip("-").isdigit() else chat

    await safe_edit_text(
        msg,
        "⚡ <b>Starting Indexing Engine...</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data=f"idx_cancel#{chat}")]]
        ),
        parse_mode=enums.ParseMode.HTML,
    )

    skip_val = getattr(temp, "CURRENT", 0)
    asyncio.create_task(
        index_files_to_db(int(lst_msg_id), chat, msg, bot, skip_val, filter_type)
    )


async def process_index_request(
    bot: Client, message: Message, chat_id, last_msg_id, filter_type="all"
):
    try:
        chat_obj = await bot.get_chat(chat_id)
        chat_id = chat_obj.id
        chat_title = html.escape(chat_obj.title or str(chat_id))
    except (ChannelInvalid, ChannelPrivate, UsernameInvalid, UsernameNotModified):
        return await safe_reply_text(
            message,
            "⚠️ Invalid Link/Channel or Bot is not an Admin in that channel.",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        return await safe_reply_text(
            message,
            f"⚠️ Error accessing channel: <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    skip_count = getattr(temp, "CURRENT", 0)
    uid = message.from_user.id if message.from_user else 0
    buttons = [
        [
            InlineKeyboardButton(
                "✅ Yes, Start Indexing",
                callback_data=f"idx#ac#{chat_id}#{last_msg_id}#{uid}#{filter_type}",
            )
        ],
        [InlineKeyboardButton("❌ Close", callback_data="idx_close")],
    ]
    return await safe_reply_text(
        message,
        f"📂 <b>Ready to Index ({filter_type.title()})</b>\n\n"
        f"<b>Channel:</b> <code>{chat_title}</code>\n"
        f"<b>Chat ID:</b> <code>{chat_id}</code>\n"
        f"<b>Last Message ID:</b> <code>{last_msg_id}</code>\n"
        f"<b>Skip Count:</b> <code>{skip_count}</code>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(
    filters.command(["index", "indexvideo", "indexdoc", "indexaudio"]),
    group=-5,
)
async def index_command(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    cmd = message.command[0].lower()
    filter_type = (
        "video"
        if cmd == "indexvideo"
        else (
            "document"
            if cmd == "indexdoc"
            else "audio" if cmd == "indexaudio" else "all"
        )
    )

    if message.reply_to_message:
        fwd_chat_id, fwd_msg_id = extract_forward_info(message.reply_to_message)
        if fwd_chat_id and fwd_msg_id:
            return await process_index_request(
                bot,
                message,
                fwd_chat_id,
                fwd_msg_id,
                filter_type,
            )

    if len(message.command) == 2 and re.match(
        r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9_]+)/(\d+)$",
        message.command[1],
    ):
        match = re.match(
            r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9_]+)/(\d+)$",
            message.command[1],
        )
        chat_id = (
            int(f"-100{match.group(4)}")
            if match.group(4).isnumeric()
            else match.group(4)
        )
        return await process_index_request(
            bot, message, chat_id, int(match.group(5)), filter_type
        )

    if len(message.command) != 3:
        return await safe_reply_text(
            message,
            f"<b>Usage:</b>\n<code>/{cmd} &lt;chat_id&gt; &lt;last_message_id&gt;</code>\n"
            f"Or <code>/{cmd} &lt;channel_message_link&gt;</code>\n"
            f"Or reply to a forwarded channel message with <code>/{cmd}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    chat_id = (
        int(message.command[1])
        if message.command[1].lstrip("-").isdigit()
        else message.command[1]
    )
    try:
        last_msg_id = int(message.command[2])
    except ValueError:
        return await safe_reply_text(
            message,
            "⚠️ Last message ID must be an integer.",
            parse_mode=enums.ParseMode.HTML,
        )

    await process_index_request(bot, message, chat_id, last_msg_id, filter_type)


@Client.on_message(filters.command("resumeindex"), group=-5)
async def resume_index_command(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) != 2:
        return await safe_reply_text(
            message,
            "<b>Usage:</b> <code>/resumeindex &lt;chat_id&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    chat_id = (
        int(message.command[1])
        if message.command[1].lstrip("-").isnumeric()
        else message.command[1]
    )
    last_processed = await get_resume_state(chat_id)
    if last_processed <= 0:
        return await safe_reply_text(
            message,
            "⚠️ No crash-resume state found for that chat.",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message,
        "⏳ Fetching highest message ID...",
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        last_msg_id = last_processed + 1000
        async for m in bot.get_chat_history(chat_id, limit=1):
            last_msg_id = m.id
            break
        if status:
            await status.delete()
        temp.CURRENT = last_processed
        await process_index_request(bot, message, chat_id, last_msg_id, "all")
    except Exception as e:
        if status:
            await safe_edit_text(
                status,
                f"❌ Failed to resume: <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(
    filters.text
    & filters.regex(
        r"^(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9_]+)/(\d+)$"
    ),
    group=-5,
)
async def send_for_index(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return

    try:
        from plugins.uibuttons import is_waiting_for_cmd_input

        if is_waiting_for_cmd_input(message.chat.id):
            return
    except Exception:
        pass

    match = re.match(
        r"^(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9_]+)/(\d+)$",
        message.text.strip(),
    )
    if match:
        chat_id = (
            int(f"-100{match.group(4)}")
            if match.group(4).isnumeric()
            else match.group(4)
        )
        await process_index_request(bot, message, chat_id, int(match.group(5)), "all")


@Client.on_message(filters.private & filters.forwarded & filters.text, group=-5)
async def pm_forward_text_indexer(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return

    try:
        from plugins.uibuttons import is_waiting_for_cmd_input

        if is_waiting_for_cmd_input(message.chat.id):
            return
    except Exception:
        pass

    fwd_chat_id, fwd_msg_id = extract_forward_info(message)
    if fwd_chat_id and fwd_msg_id:
        await process_index_request(bot, message, fwd_chat_id, fwd_msg_id, "all")


# ============================================================
# 🚀 CORE INDEXING WORKER
# ============================================================
async def index_files_to_db(
    lst_msg_id,
    chat,
    msg: Message,
    bot: Client,
    skip_number: int = 0,
    filter_type: str = "all",
):
    total_files, duplicate, errors, deleted, no_media, unsupported = 0, 0, 0, 0, 0, 0
    last_processed = skip_number

    clean_words = await get_clean_words()
    idx_settings = await get_idx_settings()
    min_size, blacklist, whitelist, auto_backup, backup_channel = (
        idx_settings.get("min_size", 0),
        idx_settings.get("blacklist", []),
        idx_settings.get("whitelist", []),
        idx_settings.get("auto_backup", False),
        idx_settings.get("backup_channel"),
    )
    delay_speed = getattr(temp, "INDEX_SPEED", 0)

    if not hasattr(temp, "INDEX_CANCEL"):
        temp.INDEX_CANCEL = {}
    temp.INDEX_CANCEL[chat] = False

    async with lock:
        try:
            start_id, fetched_count, last_update_time = (
                max(1, skip_number),
                0,
                time.time(),
            )
            message_ids = list(range(start_id, lst_msg_id + 1))

            for i in range(0, len(message_ids), 200):
                if temp.INDEX_CANCEL.get(chat):
                    await safe_edit_text(
                        msg,
                        f"<b>⛔ Indexing Cancelled!</b>\n\n"
                        f"✅ Saved: <code>{total_files}</code>\n"
                        f"♻️ Duplicates: <code>{duplicate}</code>\n"
                        f"🗑️ Deleted/Empty: <code>{deleted}</code>\n"
                        f"⚠️ Skipped/Unsupported: <code>{no_media + unsupported}</code>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                    temp.INDEX_CANCEL[chat] = False
                    return

                chunk, messages, max_retries = message_ids[i : i + 200], [], 3
                while max_retries > 0:
                    try:
                        messages = await bot.get_messages(chat, chunk)
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                        max_retries -= 1
                    except Exception:
                        break

                if not messages:
                    continue

                if not isinstance(messages, list):
                    messages = [messages]

                media_to_save = []
                for message in messages:
                    fetched_count += 1
                    if not message:
                        deleted += 1
                        continue

                    last_processed = getattr(message, "id", last_processed)

                    if time.time() - last_update_time > 8:
                        await safe_edit_text(
                            msg,
                            text=(
                                f"⚡ <b>Indexing ({filter_type.title()})...</b>\n\n"
                                f"📥 Fetched: <code>{fetched_count}/{len(message_ids)}</code>\n"
                                f"✅ Saved: <code>{total_files}</code>\n"
                                f"♻️ Duplicates: <code>{duplicate}</code>\n"
                                f"🗑️ Deleted: <code>{deleted}</code>"
                            ),
                            reply_markup=InlineKeyboardMarkup(
                                [
                                    [
                                        InlineKeyboardButton(
                                            "❌ Cancel",
                                            callback_data=f"idx_cancel#{chat}",
                                        )
                                    ]
                                ]
                            ),
                            parse_mode=enums.ParseMode.HTML,
                        )
                        last_update_time = time.time()

                    if getattr(message, "empty", False):
                        deleted += 1
                        continue
                    if not getattr(message, "media", None):
                        no_media += 1
                        continue
                    if message.media not in [
                        enums.MessageMediaType.VIDEO,
                        enums.MessageMediaType.AUDIO,
                        enums.MessageMediaType.DOCUMENT,
                    ]:
                        unsupported += 1
                        continue
                    if (
                        filter_type == "video"
                        and message.media != enums.MessageMediaType.VIDEO
                    ):
                        unsupported += 1
                        continue
                    if (
                        filter_type == "document"
                        and message.media != enums.MessageMediaType.DOCUMENT
                    ):
                        unsupported += 1
                        continue
                    if (
                        filter_type == "audio"
                        and message.media != enums.MessageMediaType.AUDIO
                    ):
                        unsupported += 1
                        continue

                    media_obj = getattr(message, message.media.value, None)
                    if not media_obj or getattr(media_obj, "file_size", 0) < min_size:
                        unsupported += 1
                        continue

                    raw_name, raw_caption = (
                        getattr(media_obj, "file_name", "Unknown") or "Unknown",
                        message.caption or "",
                    )
                    if any(
                        b.lower() in raw_name.lower()
                        or b.lower() in raw_caption.lower()
                        for b in blacklist
                    ):
                        unsupported += 1
                        continue
                    if whitelist and not any(
                        w.lower() in raw_name.lower()
                        or w.lower() in raw_caption.lower()
                        for w in whitelist
                    ):
                        unsupported += 1
                        continue

                    target_media, target_caption = media_obj, raw_caption
                    if auto_backup and backup_channel:
                        try:
                            copied_msg = await message.copy(chat_id=backup_channel)
                            target_media = getattr(
                                copied_msg, copied_msg.media.value, media_obj
                            )
                            target_caption = copied_msg.caption
                            await asyncio.sleep(1)
                        except Exception:
                            pass

                    cleaned_name = clean_filename(
                        getattr(target_media, "file_name", "") or "File", clean_words
                    )
                    media_to_save.append(
                        SafeMedia(
                            target_media,
                            message.media.value,
                            target_caption,
                            cleaned_name,
                        )
                    )

                if media_to_save:
                    try:
                        result = await safe_save_batch(media_to_save)
                        if isinstance(result, tuple) and len(result) == 3:
                            total_files += result[0]
                            duplicate += result[1]
                            errors += result[2]
                        else:
                            total_files += len(media_to_save)
                    except Exception:
                        errors += len(media_to_save)

                await save_resume_state(chat, last_processed)
                if delay_speed > 0:
                    await asyncio.sleep(delay_speed)

        except Exception as e:
            await safe_edit_text(
                msg,
                f"⚠️ <b>Indexing Error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            if not temp.INDEX_CANCEL.get(chat):
                await safe_edit_text(
                    msg,
                    f"🎉 <b>Indexing Complete!</b>\n\n"
                    f"✅ <b>Saved:</b> <code>{total_files}</code>\n"
                    f"♻️ <b>Duplicates:</b> <code>{duplicate}</code>\n"
                    f"🗑️ <b>Deleted:</b> <code>{deleted}</code>\n"
                    f"⚠️ <b>Skipped/Non-Media:</b> <code>{no_media + unsupported}</code>\n"
                    f"❌ <b>Errors:</b> <code>{errors}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
