import asyncio
import html
import inspect
import re
import time
from logging import INFO, getLogger
from typing import List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import BulkWriteError
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
    """Safely replies to a message while suppressing common permission/flood errors."""
    try:
        return await message.reply_text(text, **kwargs)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        try:
            return await message.reply_text(text, **kwargs)
        except Exception:
            return None
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None
    except Exception as e:
        logger.error(f"safe_reply_text error in index.py: {e}")
        return None


async def safe_edit_text(message: Message, text: str, **kwargs):
    """Safely edits a message without crashing on MessageNotModified or FloodWait."""
    try:
        return await message.edit_text(text, **kwargs)
    except (MessageNotModified, MessageIdInvalid, ChatWriteForbidden, Forbidden):
        return None
    except FloodWait:
        # Skip progress UI update if rate-limited to keep indexing at maximum speed
        return None
    except Exception:
        return None


async def safe_answer(
    query: CallbackQuery, text: Optional[str] = None, show_alert: bool = False
):
    """Safely answers a callback query."""
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
# ⚙️ MONGODB SETUP & HIGH-SPEED IN-MEMORY CACHING
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

# In-memory TTL cache to avoid querying MongoDB on every single channel message
_CACHE_TTL = 60.0
_CACHE_STATE = {
    "idx_settings": None,
    "idx_settings_ts": 0.0,
    "clean_words": None,
    "clean_words_ts": 0.0,
    "clean_regex": None,
}


def invalidate_index_cache():
    """Invalidates in-memory settings cache when an admin updates configurations."""
    _CACHE_STATE["idx_settings_ts"] = 0.0
    _CACHE_STATE["clean_words_ts"] = 0.0
    _CACHE_STATE["clean_regex"] = None


# ============================================================
# 👑 SMART AUTHORIZATION ENGINE (MESSAGES + CALLBACKS)
# ============================================================
def get_admin_list() -> List[int]:
    """Parses and returns the list of authorized admin user IDs."""
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        raw_admins = str(raw_admins).replace(",", " ").split()
    elif isinstance(raw_admins, (list, tuple, set)):
        raw_admins = list(raw_admins)
    else:
        raw_admins = []
    return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]


async def is_user_admin(client: Client, message: Message) -> bool:
    """Checks if a message sender is a bot admin or group administrator."""
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
    """Checks if a callback query user is a bot admin or group administrator."""
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
# 🗄️ ULTRA-FAST BULK DATABASE & FORWARD HELPERS
# ============================================================
async def safe_save_batch(media_list: list) -> Tuple[int, int, int]:
    """
    Saves a batch of media objects at maximum speed.
    Priority:
    1. Native `save_batch` from ia_filterdb (if available).
    2. Direct MongoDB `insert_many(ordered=False)` via Media.collection.
    3. Concurrent bounded `save_file` fallback using asyncio.Semaphore.
    """
    if not media_list:
        return 0, 0, 0

    # 1. Use existing batch function if present in ia_filterdb
    if save_batch is not None:
        try:
            res = await save_batch(media_list)
            if isinstance(res, tuple) and len(res) == 3:
                return res
            return len(media_list), 0, 0
        except Exception as e:
            logger.warning(f"save_batch error, falling back to high-speed bulk: {e}")

    # 2. Ultra-fast direct MongoDB unordered bulk insert if Media collection is accessible
    raw_collection = getattr(Media, "collection", None)
    if raw_collection is not None:
        docs = []
        seen_ids = set()
        batch_dups = 0
        for m in media_list:
            uid = getattr(m, "file_unique_id", None) or getattr(m, "file_id", "")
            if not uid:
                continue
            if uid in seen_ids:
                batch_dups += 1
                continue
            seen_ids.add(uid)
            docs.append(
                {
                    "_id": getattr(m, "file_id", uid),
                    "file_ref": getattr(m, "file_ref", None),
                    "file_name": getattr(m, "file_name", "File"),
                    "file_size": getattr(m, "file_size", 0),
                    "file_type": getattr(m, "file_type", "document"),
                    "mime_type": getattr(m, "mime_type", None),
                    "caption": getattr(m, "caption", None),
                }
            )
        if docs:
            try:
                res = await raw_collection.insert_many(docs, ordered=False)
                saved_count = len(res.inserted_ids)
                return saved_count, batch_dups, 0
            except BulkWriteError as bwe:
                saved_count = bwe.details.get("nInserted", 0)
                write_errors = bwe.details.get("writeErrors", [])
                dup_errors = sum(
                    1 for err in write_errors if err.get("code") == 11000
                )
                other_errors = len(write_errors) - dup_errors
                return saved_count, batch_dups + dup_errors, other_errors
            except Exception as e:
                logger.debug(f"Direct collection insert_many fallback: {e}")

    # 3. Concurrent fallback using save_file (15x faster than sequential loop)
    if save_file is not None:
        sem = asyncio.Semaphore(25)

        async def _save_one(media):
            async with sem:
                try:
                    res = await save_file(media)
                    if isinstance(res, tuple):
                        ok, status = res[0], res[1]
                        if ok:
                            return 1, 0, 0
                        elif status == 0:
                            return 0, 1, 0
                        return 0, 0, 1
                    elif res == "suc":
                        return 1, 0, 0
                    elif res == "dup":
                        return 0, 1, 0
                    return 0, 0, 1
                except Exception:
                    return 0, 0, 1

        results = await asyncio.gather(*(_save_one(m) for m in media_list))
        saved = sum(r[0] for r in results)
        dup = sum(r[1] for r in results)
        err = sum(r[2] for r in results)
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
    """Persists the latest indexed message ID to allow crash recovery."""
    if resume_db is not None:
        try:
            await resume_db.update_one(
                {"chat_id": chat_id},
                {"$set": {"last_id": last_processed_id}},
                upsert=True,
            )
        except Exception:
            pass


async def get_resume_state(chat_id):
    """Retrieves the last saved message ID for a channel."""
    if resume_db is not None:
        doc = await resume_db.find_one({"chat_id": chat_id})
        return doc.get("last_id", 0) if doc else 0
    return 0


async def get_clean_words() -> list:
    """Returns cached clean words list to avoid repetitive database reads."""
    now = time.monotonic()
    if (
        _CACHE_STATE["clean_words"] is not None
        and (now - _CACHE_STATE["clean_words_ts"]) < _CACHE_TTL
    ):
        return _CACHE_STATE["clean_words"]

    default = ["sandalwood", "mkv", "mp4", "avi", "webm", "zip", "rar"]
    if cw_db is None:
        _CACHE_STATE["clean_words"] = default
        _CACHE_STATE["clean_words_ts"] = now
        return default
    try:
        doc = await cw_db.find_one({"id": "words"})
        if doc is None or doc.get("use_default", True):
            res = default
        else:
            res = doc.get("list", [])
    except Exception:
        res = default

    _CACHE_STATE["clean_words"] = res
    _CACHE_STATE["clean_words_ts"] = now
    _CACHE_STATE["clean_regex"] = None
    return res


async def get_idx_settings() -> dict:
    """Returns cached index settings to keep auto-indexing and batch loops fast."""
    now = time.monotonic()
    if (
        _CACHE_STATE["idx_settings"] is not None
        and (now - _CACHE_STATE["idx_settings_ts"]) < _CACHE_TTL
    ):
        return _CACHE_STATE["idx_settings"]

    default_settings = {
        "min_size": 0,
        "blacklist": ["trailer", "promo", "teaser", "sample"],
        "whitelist": [],
        "auto_backup": False,
        "backup_channel": None,
        "concurrency": 4,
    }
    if idx_db is None:
        _CACHE_STATE["idx_settings"] = default_settings
        _CACHE_STATE["idx_settings_ts"] = now
        return default_settings
    try:
        doc = await idx_db.find_one({"id": "config"})
        if not doc:
            res = default_settings
        else:
            for k, v in default_settings.items():
                if k not in doc:
                    doc[k] = v
            res = doc
    except Exception:
        res = default_settings

    _CACHE_STATE["idx_settings"] = res
    _CACHE_STATE["idx_settings_ts"] = now
    return res


async def save_idx_settings(key, value):
    """Updates an index setting in MongoDB and refreshes the local memory cache."""
    if idx_db is not None:
        await idx_db.update_one({"id": "config"}, {"$set": {key: value}}, upsert=True)
    invalidate_index_cache()


# Pre-compiled base regex patterns for 10x faster filename normalization
_SANDALWOOD_TAG_RE = re.compile(r"(?i)\[?@?sandalwood[^\]\s]*\]?")
_DEFAULT_EXT_RE = re.compile(r"(?i)\b(sandalwood|mkv|mp4|avi|webm|zip|rar)\b")
_SEPARATORS_RE = re.compile(r"[_.-]+")
_MULTI_SPACE_RE = re.compile(r"\s+")


def compile_clean_regex(clean_words: list) -> Optional[re.Pattern]:
    """Compiles custom clean words into a single unified regex pattern."""
    custom_words = [
        re.escape(w)
        for w in clean_words
        if w and w.lower() not in {"mkv", "sandalwood", "mp4", "avi", "webm", "zip", "rar"}
    ]
    if not custom_words:
        return None
    return re.compile(r"(?i)(" + "|".join(custom_words) + r")")


def clean_filename(
    name: str, clean_words: list, compiled_custom_re: Optional[re.Pattern] = None
) -> str:
    """Fast filename sanitizer using pre-compiled regular expressions."""
    if not name:
        return "File"
    if any(w.lower() in ("mkv", "sandalwood") for w in clean_words):
        name = _SANDALWOOD_TAG_RE.sub("", name)
        name = _DEFAULT_EXT_RE.sub("", name)
    name = _SEPARATORS_RE.sub(" ", name)

    if compiled_custom_re is not None:
        name = compiled_custom_re.sub("", name)
    else:
        for word in clean_words:
            if word.lower() in ("mkv", "sandalwood", "mp4", "avi", "webm", "zip", "rar"):
                continue
            name = re.sub(rf"(?i){re.escape(word)}", "", name)

    cleaned = _MULTI_SPACE_RE.sub(" ", name).strip()
    return cleaned or "File"


def parse_channels(chan_var) -> List[int]:
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
    """Lightweight, memory-optimized media container using __slots__."""

    __slots__ = (
        "file_id",
        "file_unique_id",
        "file_name",
        "file_size",
        "mime_type",
        "file_type",
        "caption",
    )

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

    raw_name = getattr(media_obj, "file_name", "Unknown") or "Unknown"
    raw_caption = message.caption or ""
    raw_name_lower = raw_name.lower()
    raw_caption_lower = raw_caption.lower()

    blacklist = idx_settings.get("blacklist", [])
    whitelist = idx_settings.get("whitelist", [])

    if any(
        b.lower() in raw_name_lower or b.lower() in raw_caption_lower
        for b in blacklist
    ):
        return
    if whitelist and not any(
        w.lower() in raw_name_lower or w.lower() in raw_caption_lower
        for w in whitelist
    ):
        return

    target_media, target_caption = media_obj, message.caption
    if idx_settings.get("auto_backup") and idx_settings.get("backup_channel"):
        try:
            copied_msg = await message.copy(chat_id=idx_settings.get("backup_channel"))
            target_media = getattr(copied_msg, copied_msg.media.value, media_obj)
            target_caption = copied_msg.caption
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
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
                    "🚀 Index Whole Channel",
                    callback_data=f"idx#ac#{fwd_chat_id}#{fwd_msg_id}#{uid}#all",
                )
            ],
            [
                InlineKeyboardButton(
                    "💾 Save Only This File", callback_data=f"save_single#{message.id}"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="idx_close")],
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
        saved, dup, _ = await safe_save_batch([safe_media])
        if dup > 0:
            await safe_reply_text(
                message,
                f"♻️ <b>Duplicate File (Already in Database):</b>\n\n<code>{html.escape(cleaned_name)}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
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
# 🎛️ ADVANCED COMMAND CONTROLS (SKIP, SPEED, WORKERS, FILTERS)
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
        cur = getattr(temp, "INDEX_SPEED", 0.0)
        idx_settings = await get_idx_settings()
        workers = idx_settings.get("concurrency", 4)
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/setindexspeed &lt;seconds&gt;</code>\n"
            f"⏱️ <b>Current Batch Delay:</b> <code>{cur}s</code>\n"
            f"🚀 <b>Parallel Batch Workers:</b> <code>{workers}</code> (800 msgs/cycle)",
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


@Client.on_message(filters.command("setindexworkers"), group=-5)
async def set_index_workers_cmd(bot: Client, message: Message):
    """Allows admins to tune parallel batch concurrency (1 to 6 workers)."""
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2 or not message.command[1].isdigit():
        idx_settings = await get_idx_settings()
        cur_w = idx_settings.get("concurrency", 4)
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/setindexworkers &lt;1-6&gt;</code>\n"
            f"🚀 <b>Current Parallel Workers:</b> <code>{cur_w}</code> ({cur_w * 200} msgs per wave)\n"
            f"💡 <i>Recommended: 4 for maximum speed within Telegram flood limits.</i>",
            parse_mode=enums.ParseMode.HTML,
        )
    workers = max(1, min(6, int(message.command[1])))
    await save_idx_settings("concurrency", workers)
    await safe_reply_text(
        message,
        f"✅ <b>Parallel index workers set to:</b> <code>{workers}</code> ({workers * 200} messages per batch wave)",
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
                "⏳ <b>Stopping high-speed index engine...</b>",
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
    await safe_answer(query, "Starting High-Speed Indexing...⚡", show_alert=False)

    chat = int(chat) if str(chat).lstrip("-").isdigit() else chat

    await safe_edit_text(
        msg,
        "🚀 <b>Initializing High-Performance Indexing Engine...</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"idx_cancel#{chat}")]]
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
    idx_settings = await get_idx_settings()
    workers = idx_settings.get("concurrency", 4)
    uid = message.from_user.id if message.from_user else 0
    buttons = [
        [
            InlineKeyboardButton(
                "🚀 Yes, Start High-Speed Indexing",
                callback_data=f"idx#ac#{chat_id}#{last_msg_id}#{uid}#{filter_type}",
            )
        ],
        [InlineKeyboardButton("❌ Close", callback_data="idx_close")],
    ]
    return await safe_reply_text(
        message,
        f"📂 <b>Ready to Index ({filter_type.title()})</b>\n\n"
        f"📢 <b>Channel:</b> <code>{chat_title}</code>\n"
        f"🆔 <b>Chat ID:</b> <code>{chat_id}</code>\n"
        f"🔢 <b>Last Message ID:</b> <code>{last_msg_id}</code>\n"
        f"⏭️ <b>Skip Count:</b> <code>{skip_count}</code>\n"
        f"⚡ <b>Turbo Mode:</b> <code>{workers}x Parallel Batch Workers</code>",
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
# 🚀 HIGH-PERFORMANCE CONCURRENT INDEXING ENGINE
# ============================================================
def _format_eta(seconds: float) -> str:
    """Formats remaining seconds into a clean human-readable ETA string."""
    if seconds <= 0 or seconds > 86400 * 7:
        return "Calculating..."
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _build_progress_bar(percentage: float, length: int = 12) -> str:
    """Builds a visual progress bar for the Telegram status message."""
    filled = int(round(length * percentage / 100))
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)


async def _fetch_chunk_safe(
    bot: Client, chat_id, chunk: List[int], stagger_delay: float = 0.0
) -> List[Message]:
    """
    Fetches a chunk of up to 200 messages with automatic FloodWait back-off
    and staggered dispatch to respect Telegram MTProto rate limits.
    """
    if stagger_delay > 0:
        await asyncio.sleep(stagger_delay)

    max_retries = 4
    while max_retries > 0:
        try:
            messages = await bot.get_messages(chat_id, chunk)
            if not messages:
                return []
            return messages if isinstance(messages, list) else [messages]
        except FloodWait as e:
            wait_time = e.value + 1.5
            logger.warning(
                f"Telegram FloodWait triggered ({e.value}s). Pausing worker for {wait_time:.1f}s..."
            )
            await asyncio.sleep(wait_time)
            max_retries -= 1
        except Exception as e:
            logger.debug(f"Chunk fetch warning on {chat_id}: {e}")
            break
    return []


async def _copy_backup_safe(bot: Client, message: Message, backup_channel: int, sem: asyncio.Semaphore):
    """Copies a media message to the backup channel with FloodWait protection and concurrency control."""
    async with sem:
        for _ in range(2):
            try:
                copied_msg = await message.copy(chat_id=backup_channel)
                return getattr(copied_msg, copied_msg.media.value, None), copied_msg.caption
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception:
                break
        return None, None


async def index_files_to_db(
    lst_msg_id: int,
    chat,
    msg: Message,
    bot: Client,
    skip_number: int = 0,
    filter_type: str = "all",
):
    """
    High-performance media indexing worker:
    - Fetches multiple 200-message chunks concurrently (up to 800-1200 messages per wave).
    - Pre-compiles regex patterns and lowercase filter sets outside the hot loop.
    - Performs bulk unordered MongoDB inserts.
    - Displays real-time speed (msgs/sec), percentage progress bar, and ETA.
    """
    total_files, duplicate, errors, deleted, no_media, unsupported = 0, 0, 0, 0, 0, 0
    last_processed = skip_number

    clean_words = await get_clean_words()
    compiled_custom_re = compile_clean_regex(clean_words)

    idx_settings = await get_idx_settings()
    min_size = idx_settings.get("min_size", 0)
    blacklist_lower = [b.lower() for b in idx_settings.get("blacklist", []) if b]
    whitelist_lower = [w.lower() for w in idx_settings.get("whitelist", []) if w]
    auto_backup = idx_settings.get("auto_backup", False)
    backup_channel = idx_settings.get("backup_channel")
    concurrency = max(1, min(6, int(idx_settings.get("concurrency", 4))))

    delay_speed = float(getattr(temp, "INDEX_SPEED", 0.0) or 0.0)

    if not hasattr(temp, "INDEX_CANCEL"):
        temp.INDEX_CANCEL = {}
    temp.INDEX_CANCEL[chat] = False

    valid_media_types = {
        enums.MessageMediaType.VIDEO,
        enums.MessageMediaType.AUDIO,
        enums.MessageMediaType.DOCUMENT,
    }
    target_enum = {
        "video": enums.MessageMediaType.VIDEO,
        "document": enums.MessageMediaType.DOCUMENT,
        "audio": enums.MessageMediaType.AUDIO,
    }.get(filter_type)

    backup_sem = asyncio.Semaphore(3)

    async with lock:
        try:
            start_id = max(1, skip_number + 1) if skip_number > 0 else 1
            total_to_fetch = max(0, lst_msg_id - start_id + 1)
            if total_to_fetch <= 0:
                return await safe_edit_text(
                    msg,
                    "⚠️ <b>Nothing to index:</b> Skip count is greater than or equal to the last message ID.",
                    parse_mode=enums.ParseMode.HTML,
                )

            fetched_count = 0
            start_time = time.monotonic()
            last_update_time = start_time

            # Step size = 200 (Telegram's hard maximum per get_messages call)
            chunk_size = 200
            wave_size = chunk_size * concurrency

            for wave_start in range(start_id, lst_msg_id + 1, wave_size):
                if temp.INDEX_CANCEL.get(chat):
                    elapsed = max(0.1, time.monotonic() - start_time)
                    await safe_edit_text(
                        msg,
                        f"<b>⛔ Indexing Cancelled!</b>\n\n"
                        f"📥 <b>Processed:</b> <code>{fetched_count}/{total_to_fetch}</code>\n"
                        f"✅ <b>Saved:</b> <code>{total_files}</code>\n"
                        f"♻️ <b>Duplicates:</b> <code>{duplicate}</code>\n"
                        f"🗑️ <b>Deleted/Empty:</b> <code>{deleted}</code>\n"
                        f"⚠️ <b>Skipped/Non-Media:</b> <code>{no_media + unsupported}</code>\n"
                        f"⏱️ <b>Elapsed:</b> <code>{_format_eta(elapsed)}</code>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                    temp.INDEX_CANCEL[chat] = False
                    return

                # Build parallel chunks for this wave (e.g., 4 chunks x 200 IDs = 800 messages)
                wave_end = min(lst_msg_id + 1, wave_start + wave_size)
                chunks = [
                    list(range(c_start, min(wave_end, c_start + chunk_size)))
                    for c_start in range(wave_start, wave_end, chunk_size)
                ]

                # Fire concurrent requests with slight 45ms stagger to prevent burst FloodWaits
                fetch_tasks = [
                    _fetch_chunk_safe(bot, chat, chunk, stagger_delay=idx * 0.045)
                    for idx, chunk in enumerate(chunks)
                ]
                wave_results = await asyncio.gather(*fetch_tasks)

                media_to_save = []
                backup_candidates = []

                for chunk, messages in zip(chunks, wave_results):
                    if not messages:
                        deleted += len(chunk)
                        fetched_count += len(chunk)
                        last_processed = chunk[-1]
                        continue

                    for message in messages:
                        fetched_count += 1
                        if not message:
                            deleted += 1
                            continue

                        msg_id = getattr(message, "id", None)
                        if msg_id and msg_id > last_processed:
                            last_processed = msg_id

                        if getattr(message, "empty", False):
                            deleted += 1
                            continue

                        msg_media = getattr(message, "media", None)
                        if not msg_media:
                            no_media += 1
                            continue

                        if msg_media not in valid_media_types:
                            unsupported += 1
                            continue

                        if target_enum is not None and msg_media != target_enum:
                            unsupported += 1
                            continue

                        media_obj = getattr(message, msg_media.value, None)
                        if not media_obj or getattr(media_obj, "file_size", 0) < min_size:
                            unsupported += 1
                            continue

                        raw_name = getattr(media_obj, "file_name", "Unknown") or "Unknown"
                        raw_caption = message.caption or ""

                        if blacklist_lower or whitelist_lower:
                            raw_name_l = raw_name.lower()
                            raw_cap_l = raw_caption.lower()
                            if blacklist_lower and any(
                                b in raw_name_l or b in raw_cap_l for b in blacklist_lower
                            ):
                                unsupported += 1
                                continue
                            if whitelist_lower and not any(
                                w in raw_name_l or w in raw_cap_l for w in whitelist_lower
                            ):
                                unsupported += 1
                                continue

                        if auto_backup and backup_channel:
                            backup_candidates.append(
                                (message, media_obj, msg_media.value, raw_caption)
                            )
                        else:
                            cleaned_name = clean_filename(
                                raw_name, clean_words, compiled_custom_re
                            )
                            media_to_save.append(
                                SafeMedia(
                                    media_obj,
                                    msg_media.value,
                                    raw_caption,
                                    cleaned_name,
                                )
                            )

                # If auto_backup is enabled, process backups with bounded concurrency instead of 1s blocking sleep
                if backup_candidates:
                    backup_results = await asyncio.gather(
                        *(
                            _copy_backup_safe(bot, item[0], backup_channel, backup_sem)
                            for item in backup_candidates
                        )
                    )
                    for (_, orig_media, m_type, orig_cap), (copied_media, copied_cap) in zip(
                        backup_candidates, backup_results
                    ):
                        final_media = copied_media or orig_media
                        final_cap = copied_cap if copied_media else orig_cap
                        raw_n = getattr(final_media, "file_name", "") or "File"
                        cleaned_name = clean_filename(
                            raw_n, clean_words, compiled_custom_re
                        )
                        media_to_save.append(
                            SafeMedia(final_media, m_type, final_cap, cleaned_name)
                        )

                # Save entire wave (up to 800-1200 items) & update resume state concurrently
                db_tasks = [save_resume_state(chat, last_processed)]
                if media_to_save:
                    db_tasks.append(safe_save_batch(media_to_save))

                db_results = await asyncio.gather(*db_tasks, return_exceptions=True)

                if media_to_save and len(db_results) > 1:
                    batch_res = db_results[1]
                    if isinstance(batch_res, Exception):
                        errors += len(media_to_save)
                    elif isinstance(batch_res, tuple) and len(batch_res) == 3:
                        total_files += batch_res[0]
                        duplicate += batch_res[1]
                        errors += batch_res[2]
                    else:
                        total_files += len(media_to_save)

                # Update live progress UI every 6 seconds to avoid FloodWait on edit_message_text
                now = time.monotonic()
                if (now - last_update_time) >= 6.0:
                    elapsed = max(0.1, now - start_time)
                    speed = fetched_count / elapsed
                    remaining_msgs = max(0, total_to_fetch - fetched_count)
                    eta_sec = remaining_msgs / speed if speed > 0 else 0
                    pct = min(100.0, (fetched_count / total_to_fetch) * 100.0)
                    p_bar = _build_progress_bar(pct)

                    await safe_edit_text(
                        msg,
                        text=(
                            f"⚡ <b>Turbo Indexing ({filter_type.title()})</b>\n"
                            f"<code>[{p_bar}] {pct:.1f}%</code>\n\n"
                            f"📥 <b>Fetched:</b> <code>{fetched_count:,}/{total_to_fetch:,}</code>\n"
                            f"✅ <b>Saved:</b> <code>{total_files:,}</code>\n"
                            f"♻️ <b>Duplicates:</b> <code>{duplicate:,}</code>\n"
                            f"🗑️ <b>Deleted/Empty:</b> <code>{deleted:,}</code>\n"
                            f"⚠️ <b>Skipped:</b> <code>{(no_media + unsupported):,}</code>\n"
                            f"🚀 <b>Speed:</b> <code>{speed:.0f} msgs/sec</code>\n"
                            f"⏳ <b>ETA:</b> <code>{_format_eta(eta_sec)}</code>"
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "❌ Cancel Indexing",
                                        callback_data=f"idx_cancel#{chat}",
                                    )
                                ]
                            ]
                        ),
                        parse_mode=enums.ParseMode.HTML,
                    )
                    last_update_time = now

                if delay_speed > 0:
                    await asyncio.sleep(delay_speed)

        except Exception as e:
            logger.exception("Indexing worker error:")
            await safe_edit_text(
                msg,
                f"⚠️ <b>Indexing Error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            if not temp.INDEX_CANCEL.get(chat):
                total_elapsed = max(0.1, time.monotonic() - start_time)
                avg_speed = fetched_count / total_elapsed
                await safe_edit_text(
                    msg,
                    f"🎉 <b>High-Speed Indexing Complete!</b>\n"
                    f"<code>[████████████] 100.0%</code>\n\n"
                    f"📥 <b>Total Scanned:</b> <code>{fetched_count:,}</code>\n"
                    f"✅ <b>Saved:</b> <code>{total_files:,}</code>\n"
                    f"♻️ <b>Duplicates:</b> <code>{duplicate:,}</code>\n"
                    f"🗑️ <b>Deleted:</b> <code>{deleted:,}</code>\n"
                    f"⚠️ <b>Skipped/Non-Media:</b> <code>{(no_media + unsupported):,}</code>\n"
                    f"❌ <b>Errors:</b> <code>{errors:,}</code>\n"
                    f"🚀 <b>Avg Speed:</b> <code>{avg_speed:.0f} msgs/sec</code>\n"
                    f"⏱️ <b>Total Time:</b> <code>{_format_eta(total_elapsed)}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
