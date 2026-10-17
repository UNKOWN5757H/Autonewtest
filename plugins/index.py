import asyncio
import html
import inspect
import math
import re
import time
from logging import ERROR, INFO, WARNING, getLogger
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChannelInvalid,
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    QueryIdInvalid,
    UserIsBlocked,
    UsernameInvalid,
    UsernameNotOccupied,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from utils import temp

logger = getLogger(__name__)
logger.setLevel(INFO)

# ============================================================
# 🔗 SAFE IMPORTS FOR DATABASE & MULTI-BOT WORKER POOL
# ============================================================
try:
    from database.ia_filterdb import save_file
except ImportError:
    save_file = None

try:
    from database.ia_filterdb import save_batch as _db_save_batch
except ImportError:
    _db_save_batch = None

try:
    from database.ia_filterdb import free_up_mongodb_storage as _free_up_mongodb_storage
except ImportError:
    _free_up_mongodb_storage = None

try:
    from database.ia_filterdb import Media as _Media
except ImportError:
    _Media = None

try:
    from plugins.multibot import get_indexing_clients
except ImportError:
    try:
        from multibot import get_indexing_clients
    except ImportError:

        async def get_indexing_clients(main_bot: Client) -> List[Client]:
            return [main_bot]


try:
    from plugins.uibuttons import is_waiting_for_cmd_input
except ImportError:

    def is_waiting_for_cmd_input(chat_id: int) -> bool:
        return False


# ============================================================
# 🛡️ PYROGRAM v2.2.26 COMPATIBILITY & BUTTON HELPERS
# ============================================================
BTN_PRIMARY = None
BTN_SUCCESS = None
BTN_DANGER = None
_SUPPORTS_BTN_STYLE = False

try:
    from pyrogram.enums import ButtonStyle

    if "style" in inspect.signature(InlineKeyboardButton.__init__).parameters:
        BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", None)
        BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", None)
        BTN_DANGER = getattr(ButtonStyle, "DANGER", None)
        _SUPPORTS_BTN_STYLE = True
except (ImportError, ValueError, TypeError):
    BTN_PRIMARY = BTN_SUCCESS = BTN_DANGER = None
    _SUPPORTS_BTN_STYLE = False


def create_btn(
    text: str,
    url: Optional[str] = None,
    callback_data: Optional[str] = None,
    style: Any = None,
) -> InlineKeyboardButton:
    kwargs: Dict[str, Any] = {"text": str(text)}
    if url:
        kwargs["url"] = str(url).strip()
    elif callback_data:
        kwargs["callback_data"] = str(callback_data)[:64]

    if _SUPPORTS_BTN_STYLE and style is not None:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


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


async def safe_reply_text(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    try:
        return await message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None
    except Exception as e:
        logger.error(f"safe_reply_text error in index.py: {e}")
        return None


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    try:
        return await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except FloodWait as e:
        if e.value <= 10:
            await asyncio.sleep(e.value + 1)
            try:
                return await message.edit_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                return None
        return None
    except (MessageNotModified, MessageIdInvalid, ChatWriteForbidden, Forbidden):
        return None
    except Exception as e:
        logger.error(f"safe_edit_text error in index.py: {e}")
        return None


def get_admin_list() -> List[int]:
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        raw_admins = str(raw_admins).replace(",", " ").split()
    elif isinstance(raw_admins, (list, tuple, set)):
        raw_admins = list(raw_admins)
    else:
        raw_admins = []
    return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]


async def is_global_admin(user_id: int) -> bool:
    if not user_id:
        return False
    return int(user_id) in get_admin_list()


# ============================================================
# 💾 RESUME CHECKPOINT & QUOTA-SAFE BULK WRITE ENGINE
# ============================================================
_idx_col = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _idx_col = _BOT_DB["index_checkpoints"]
except Exception as e:
    logger.error(f"Index checkpoint DB init error: {e}")

INDEX_LOCK = asyncio.Lock()
_RESUME_MEMORY_CACHE: Dict[int, Dict[str, Any]] = {}


async def save_index_checkpoint(
    chat_id: int,
    current_msg_id: int,
    last_msg_id: int,
    media_filter: str = "all",
):
    data = {
        "chat_id": int(chat_id),
        "current_msg_id": int(current_msg_id),
        "last_msg_id": int(last_msg_id),
        "media_filter": str(media_filter),
        "updated_at": time.time(),
    }
    _RESUME_MEMORY_CACHE[int(chat_id)] = data
    if _idx_col is not None:
        try:
            await _idx_col.update_one(
                {"_id": int(chat_id)}, {"$set": data}, upsert=True
            )
        except Exception:
            # Ignore 512MB quota errors on checkpoints; RAM cache still holds it
            pass


async def get_index_checkpoint(chat_id: int) -> Optional[Dict[str, Any]]:
    if int(chat_id) in _RESUME_MEMORY_CACHE:
        return _RESUME_MEMORY_CACHE[int(chat_id)]
    if _idx_col is not None:
        try:
            doc = await _idx_col.find_one({"_id": int(chat_id)})
            if doc:
                _RESUME_MEMORY_CACHE[int(chat_id)] = doc
                return doc
        except Exception:
            pass
    return None


async def clear_index_checkpoint(chat_id: int):
    _RESUME_MEMORY_CACHE.pop(int(chat_id), None)
    if _idx_col is not None:
        try:
            await _idx_col.delete_one({"_id": int(chat_id)})
        except Exception:
            pass


def _is_quota_exceeded_error(err: Exception) -> bool:
    err_str = str(err).lower()
    return (
        "space quota" in err_str
        or "8000" in err_str
        or "atlaserror" in err_str
        or "over your space" in err_str
    )


async def fast_save_media_batch(media_list: list) -> Tuple[int, int, int, bool]:
    """
    Saves a batch of media objects at maximum speed.
    Returns: (saved_count, duplicate_count, error_count, all_dbs_quota_full)
    """
    if not media_list:
        return 0, 0, 0, False

    # 1. Use native bulk save_batch from database/ia_filterdb.py if available
    if callable(_db_save_batch):
        try:
            res = await _db_save_batch(media_list)
            if isinstance(res, tuple):
                if len(res) == 4:
                    return res
                if len(res) == 3:
                    return res[0], res[1], res[2], False
                if len(res) == 2:
                    return res[0], res[1], 0, False
        except Exception as e:
            if _is_quota_exceeded_error(e):
                # Trigger auto-storage cleanup once if available
                if callable(_free_up_mongodb_storage):
                    try:
                        await _free_up_mongodb_storage()
                        res = await _db_save_batch(media_list)
                        if isinstance(res, tuple) and len(res) >= 3:
                            return res[0], res[1], res[2], False
                    except Exception:
                        pass
                return 0, 0, len(media_list), True
            logger.warning(f"save_batch fallback triggered: {e}")

    # 2. Fallback: High-speed concurrent save_file calls (in sub-batches of 50)
    if not callable(save_file):
        return 0, 0, len(media_list), False

    saved = 0
    dups = 0
    errs = 0
    quota_full = False

    for i in range(0, len(media_list), 50):
        sub_batch = media_list[i : i + 50]
        results = await asyncio.gather(
            *[save_file(m) for m in sub_batch], return_exceptions=True
        )
        for r in results:
            if isinstance(r, Exception):
                if _is_quota_exceeded_error(r):
                    quota_full = True
                errs += 1
            elif isinstance(r, tuple):
                ok, code = r[0], r[1]
                if ok:
                    saved += 1
                elif code == 0:
                    dups += 1
                elif code == 8000:
                    quota_full = True
                    errs += 1
                else:
                    errs += 1
            elif r is True or r == 1:
                saved += 1
            elif r == 0:
                dups += 1
            else:
                errs += 1

        if quota_full:
            break

    return saved, dups, errs, quota_full


# ============================================================
# 🛠️ MEDIA FILTER EXTRACTOR & TIME FORMATTER
# ============================================================
def extract_media_from_message(message: Message, media_filter: str = "all"):
    """
    Extracts the media object from a message according to media_filter:
    - 'all': document, video, audio
    - 'doc': document only
    - 'video': video only
    - 'audio': audio only
    """
    if not message or getattr(message, "empty", False) or not message.media:
        return None, "empty"

    allowed_types = {
        "all": (
            enums.MessageMediaType.DOCUMENT,
            enums.MessageMediaType.VIDEO,
            enums.MessageMediaType.AUDIO,
        ),
        "doc": (enums.MessageMediaType.DOCUMENT,),
        "video": (enums.MessageMediaType.VIDEO,),
        "audio": (enums.MessageMediaType.AUDIO,),
    }.get(
        media_filter,
        (
            enums.MessageMediaType.DOCUMENT,
            enums.MessageMediaType.VIDEO,
            enums.MessageMediaType.AUDIO,
        ),
    )

    if message.media not in allowed_types:
        return None, "unsupported"

    media = getattr(message, message.media.value, None)
    if not media:
        return None, "unsupported"

    media.file_type = message.media.value
    media.caption = message.caption or ""
    return media, "ok"


def format_eta(seconds: float) -> str:
    if seconds <= 0 or math.isinf(seconds) or math.isnan(seconds):
        return "0s"
    sec = int(seconds)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def parse_channel_link_or_forward(message: Message) -> Tuple[Optional[Any], Optional[int]]:
    """Extracts (chat_id_or_username, last_msg_id) from a link or forwarded message."""
    target = message.reply_to_message if message.reply_to_message else message

    if target.text:
        regex = re.compile(
            r"(?:https?://)?(?:t\.me/|telegram\.me/|telegram\.dog/)(?:c/)?(\d+|[a-zA-Z0-9_]+)/(\d+)"
        )
        match = regex.search(target.text)
        if match:
            chat_id_raw = match.group(1)
            last_msg_id = int(match.group(2))
            if chat_id_raw.isdigit():
                chat_id = int("-100" + chat_id_raw)
            else:
                chat_id = chat_id_raw
            return chat_id, last_msg_id

        # Also support "/index -1001234567890 500000"
        parts = target.text.strip().split()
        if len(parts) >= 3 and parts[1].lstrip("-").isdigit() and parts[2].isdigit():
            return int(parts[1]), int(parts[2])

    if getattr(target, "forward_from_chat", None) and target.forward_from_chat.type == enums.ChatType.CHANNEL:
        return target.forward_from_chat.id, target.forward_from_message_id

    if getattr(target, "forward_origin", None):
        origin = target.forward_origin
        chat_obj = getattr(origin, "chat", None)
        msg_id = getattr(origin, "message_id", None)
        if chat_obj and msg_id:
            return chat_obj.id, int(msg_id)

    return None, None


# ============================================================
# 🧹 /freedb COMMAND (FREES UP WASTED MONGODB SPACE INSTANTLY)
# ============================================================
@Client.on_message(filters.command(["freedb", "compactdb", "cleanquota"]) & filters.incoming, group=-2)
async def freedb_command_handler(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized:</b> Global Bot Admin only."
        )

    status_msg = await safe_reply_text(
        message,
        "🧹 <b>Optimizing & Freeing Up MongoDB Storage Across All Clusters...</b>\n"
        "<i>Dropping heavy text indexes & stripping redundant fields...</i>",
    )

    if callable(_free_up_mongodb_storage):
        try:
            report = await _free_up_mongodb_storage()
            return await safe_edit_text(
                status_msg,
                f"✅ <b>MongoDB Storage Optimization Complete!</b>\n\n{report}",
            )
        except Exception as e:
            return await safe_edit_text(
                status_msg, f"⚠️ <b>Optimization Error:</b> <code>{html.escape(str(e))}</code>"
            )

    # Fallback direct cleanup if helper not exported
    try:
        col = getattr(_Media, "collection", _Media)
        if col is not None:
            try:
                await col.drop_indexes()
            except Exception:
                pass
            await col.update_many(
                {}, {"$unset": {"file_id_raw": "", "mime_type": ""}}
            )
        await safe_edit_text(
            status_msg,
            "✅ <b>Dropped redundant indexes & compacted media documents!</b>",
        )
    except Exception as e:
        await safe_edit_text(
            status_msg,
            f"⚠️ <b>Could not compact DB:</b> <code>{html.escape(str(e))}</code>",
        )


# ============================================================
# ⏩ /setskip, /deleteskip, /currentskip COMMANDS
# ============================================================
@Client.on_message(filters.command("setskip") & filters.incoming, group=-2)
async def set_skip_number(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized:</b> Global Bot Admin only."
        )

    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/setskip &lt;message_id_number&gt;</code>\n"
            "Example: <code>/setskip 5000</code>",
        )

    skip_val = max(0, int(message.command[1]))
    temp.CURRENT = skip_val
    await safe_reply_text(
        message,
        f"✅ <b>Starting Skip Message ID set to:</b> <code>{skip_val}</code>",
    )


@Client.on_message(filters.command("deleteskip") & filters.incoming, group=-2)
async def delete_skip_number(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized:</b> Global Bot Admin only."
        )

    temp.CURRENT = 0
    await safe_reply_text(
        message, "✅ <b>Skip number reset to:</b> <code>0</code>"
    )


@Client.on_message(filters.command("currentskip") & filters.incoming, group=-2)
async def current_skip_number(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized:</b> Global Bot Admin only."
        )

    cur = int(getattr(temp, "CURRENT", 0) or 0)
    await safe_reply_text(
        message, f"⏩ <b>Current Skip Message ID:</b> <code>{cur}</code>"
    )


# ============================================================
# 🎯 /index, /indexdoc, /indexvideo, /indexaudio, /resumeindex
# ============================================================
async def _prompt_index_confirmation(
    client: Client,
    message: Message,
    chat_id: Any,
    last_msg_id: int,
    media_filter: str = "all",
    skip_override: Optional[int] = None,
):
    try:
        chat_obj = await client.get_chat(chat_id)
        real_chat_id = chat_obj.id
        chat_title = html.escape(chat_obj.title or str(real_chat_id))
    except (ChannelInvalid, PeerIdInvalid, UsernameInvalid, UsernameNotOccupied) as e:
        return await safe_reply_text(
            message,
            f"❌ <b>Cannot access channel <code>{chat_id}</code>!</b>\n"
            f"Make sure the Main Bot (and your Worker Bots in <code>/bots</code>) are <b>Admins</b> in that channel.\n"
            f"<code>{html.escape(str(e))}</code>",
        )
    except Exception as e:
        return await safe_reply_text(
            message, f"⚠️ <b>Error fetching channel:</b> <code>{html.escape(str(e))}</code>"
        )

    # Check how many bots are available in the Multi-Bot pool
    active_bots = await get_indexing_clients(client)
    bot_count = len(active_bots)
    start_skip = (
        skip_override
        if skip_override is not None
        else int(getattr(temp, "CURRENT", 0) or 0)
    )
    total_to_scan = max(0, int(last_msg_id) - start_skip)

    filter_label = {
        "all": "All Media (Video + Doc + Audio)",
        "doc": "Documents Only",
        "video": "Videos Only",
        "audio": "Audio Only",
    }.get(media_filter, "All Media")

    mode_badge = (
        f"⚡ <b>Multi-Bot Parallel ({bot_count} Bots Splitting Load)</b>"
        if bot_count > 1
        else "🤖 <b>Single Bot Mode</b> (Add more bots via <code>/bots</code> for 10x speed!)"
    )

    buttons = [
        [
            create_btn(
                f"🚀 Start Indexing ({bot_count} Bot{'s' if bot_count > 1 else ''})",
                callback_data=f"index#yes#{real_chat_id}#{last_msg_id}#{media_filter}#{start_skip}",
                style=BTN_SUCCESS,
            )
        ],
        [
            create_btn(
                "❌ Cancel",
                callback_data="index#close",
                style=BTN_DANGER,
            )
        ],
    ]

    await safe_reply_text(
        message,
        f"📚 <b>Channel Indexing Confirmation</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📢 <b>Channel:</b> {chat_title} (<code>{real_chat_id}</code>)\n"
        f"🔢 <b>Start Message ID:</b> <code>{start_skip + 1}</code>\n"
        f"🏁 <b>Last Message ID:</b> <code>{last_msg_id:,}</code>\n"
        f"📊 <b>Messages to Scan:</b> <code>{total_to_scan:,}</code>\n"
        f"🎞️ <b>Filter Mode:</b> <code>{filter_label}</code>\n"
        f"⚙️ <b>Engine:</b> {mode_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>If using Multi-Bot, ensure all Worker Bots in <code>/bots</code> are Admins in {chat_title}!</i>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_message(
    filters.command(["index", "indexdoc", "indexvideo", "indexaudio"])
    & filters.incoming,
    group=-2,
)
async def index_command(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized:</b> Global Bot Admin only."
        )

    cmd = message.command[0].lower()
    media_filter = {
        "index": "all",
        "indexdoc": "doc",
        "indexvideo": "video",
        "indexaudio": "audio",
    }.get(cmd, "all")

    chat_id, last_msg_id = parse_channel_link_or_forward(message)
    if not chat_id or not last_msg_id:
        return await safe_reply_text(
            message,
            f"📚 <b>How to use <code>/{cmd}</code> (Supports 10 Lakh+ Files):</b>\n\n"
            f"1️⃣ <b>Forward the last message</b> from your channel to the bot, OR\n"
            f"2️⃣ <b>Reply to a Channel Post Link</b> with <code>/{cmd}</code>, OR\n"
            f"3️⃣ Send: <code>/{cmd} https://t.me/c/123456789/1000000</code>\n\n"
            f"💡 <i>Tip: Use <code>/bots</code> to add up to 10 Worker Bots for 10x faster parallel indexing!</i>",
        )

    await _prompt_index_confirmation(
        client, message, chat_id, last_msg_id, media_filter=media_filter
    )


@Client.on_message(filters.command("resumeindex") & filters.incoming, group=-2)
async def resume_index_command(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized:</b> Global Bot Admin only."
        )

    if len(message.command) < 2:
        if _RESUME_MEMORY_CACHE:
            last_cid = list(_RESUME_MEMORY_CACHE.keys())[-1]
            cp = _RESUME_MEMORY_CACHE[last_cid]
            return await _prompt_index_confirmation(
                client,
                message,
                cp["chat_id"],
                cp["last_msg_id"],
                media_filter=cp.get("media_filter", "all"),
                skip_override=cp["current_msg_id"],
            )
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/resumeindex &lt;channel_id&gt;</code>\n"
            "Example: <code>/resumeindex -1001234567890</code>",
        )

    raw_cid = message.command[1].strip()
    if not raw_cid.lstrip("-").isdigit():
        return await safe_reply_text(message, "❌ Invalid Channel ID!")

    cp = await get_index_checkpoint(int(raw_cid))
    if not cp:
        return await safe_reply_text(
            message,
            f"ℹ️ <b>No saved checkpoint found for <code>{raw_cid}</code>.</b>\n"
            f"Use <code>/setskip &lt;number&gt;</code> and <code>/index</code> instead.",
        )

    await _prompt_index_confirmation(
        client,
        message,
        cp["chat_id"],
        cp["last_msg_id"],
        media_filter=cp.get("media_filter", "all"),
        skip_override=cp["current_msg_id"],
    )


# Automatic Private Forwarded Channel Message / Link Catcher for Admins
@Client.on_message(
    filters.private
    & filters.incoming
    & (
        filters.forwarded
        | filters.regex(
            r"(?:https?://)?(?:t\.me/|telegram\.me/|telegram\.dog/)(?:c/)?(\d+|[a-zA-Z0-9_]+)/(\d+)"
        )
    ),
    group=0,
)
async def auto_channel_forward_index_prompt(client: Client, message: Message):
    if is_waiting_for_cmd_input(message.chat.id):
        return
    if message.text and message.text.startswith("/"):
        return

    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return

    chat_id, last_msg_id = parse_channel_link_or_forward(message)
    if not chat_id or not last_msg_id:
        return

    await _prompt_index_confirmation(
        client, message, chat_id, last_msg_id, media_filter="all"
    )


# ============================================================
# 🚀 DISTRIBUTED MULTI-BOT PARALLEL INDEXING ENGINE (10 LAKH+ FILES)
# ============================================================
@Client.on_callback_query(filters.regex(r"^index#"), group=-2)
async def index_callback_handler(bot: Client, query: CallbackQuery):
    uid = query.from_user.id if query.from_user else 0
    if not await is_global_admin(uid):
        return await safe_answer(query, "❌ Admin only!", show_alert=True)

    parts = query.data.split("#")
    ident = parts[1] if len(parts) > 1 else "close"

    if ident == "close":
        await safe_answer(query, "Closed.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if ident == "cancel":
        temp.CANCEL = True
        return await safe_answer(
            query,
            "🛑 Stopping all Worker Bots after their current 200-msg chunk...",
            show_alert=True,
        )

    if ident == "yes":
        if INDEX_LOCK.locked():
            return await safe_answer(
                query,
                "⏳ An indexing process is already running! Wait or cancel it first.",
                show_alert=True,
            )

        try:
            chat_id = int(parts[2]) if parts[2].lstrip("-").isdigit() else parts[2]
            last_msg_id = int(parts[3])
            media_filter = parts[4] if len(parts) > 4 else "all"
            start_skip = int(parts[5]) if len(parts) > 5 else int(getattr(temp, "CURRENT", 0) or 0)
        except Exception:
            return await safe_answer(query, "Invalid callback parameters!", show_alert=True)

        await safe_answer(query, "🚀 Launching Distributed Multi-Bot Indexer...")
        asyncio.create_task(
            run_distributed_indexing(
                main_bot=bot,
                status_msg=query.message,
                chat_id=chat_id,
                start_skip=start_skip,
                last_msg_id=last_msg_id,
                media_filter=media_filter,
            )
        )


async def _verify_worker_channel_access(
    clients: List[Client], chat_id: int
) -> Tuple[List[Client], List[str]]:
    """
    Verifies which bots in the pool can read messages in the target channel
    and warms up their peer cache so `get_messages` never raises PeerIdInvalid.
    """
    usable_clients: List[Client] = []
    skipped_bots: List[str] = []

    for c in clients:
        b_name = getattr(c.me, "first_name", "Bot") if getattr(c, "me", None) else "Bot"
        b_uname = getattr(c.me, "username", "") if getattr(c, "me", None) else ""
        label = f"{b_name} (@{b_uname})" if b_uname else b_name
        try:
            await c.get_chat(chat_id)
            usable_clients.append(c)
        except Exception:
            skipped_bots.append(label)

    return usable_clients, skipped_bots


async def run_distributed_indexing(
    main_bot: Client,
    status_msg: Message,
    chat_id: int,
    start_skip: int,
    last_msg_id: int,
    media_filter: str = "all",
):
    async with INDEX_LOCK:
        temp.CANCEL = False
        start_time = time.time()

        # 1. Discover all active bots (Main Bot + up to 10 Worker Bots from /bots)
        candidate_clients = await get_indexing_clients(main_bot)
        usable_clients, skipped_bots = await _verify_worker_channel_access(
            candidate_clients, chat_id
        )

        if not usable_clients:
            await safe_edit_text(
                status_msg,
                f"❌ <b>No bots have access to channel <code>{chat_id}</code>!</b>\n"
                f"Please make sure the bot is an Admin in the channel.",
            )
            return

        total_range = max(0, last_msg_id - start_skip)
        if total_range <= 0:
            await safe_edit_text(
                status_msg,
                f"⚠️ <b>Nothing to index!</b> Start skip (<code>{start_skip}</code>) is >= Last Message ID (<code>{last_msg_id}</code>).",
            )
            return

        num_bots = len(usable_clients)
        # Partition [start_skip + 1 ... last_msg_id] evenly across all usable bots
        slice_size = math.ceil(total_range / num_bots)
        bot_ranges: List[Tuple[Client, int, int, str]] = []

        for idx, client_obj in enumerate(usable_clients):
            w_start = start_skip + (idx * slice_size) + 1
            w_end = min(last_msg_id, start_skip + ((idx + 1) * slice_size))
            if w_start <= w_end:
                b_uname = (
                    f"@{client_obj.me.username}"
                    if getattr(client_obj, "me", None) and client_obj.me.username
                    else f"Bot #{idx + 1}"
                )
                bot_ranges.append((client_obj, w_start, w_end, b_uname))

        # Shared real-time statistics across all parallel workers
        stats: Dict[str, Any] = {
            "processed": 0,
            "saved": 0,
            "dups": 0,
            "deleted": 0,
            "non_media": 0,
            "errors": 0,
            "quota_full": False,
            "worker_progress": {
                b_label: f"{w_s:,} ➔ {w_e:,} (0%)"
                for _, w_s, w_e, b_label in bot_ranges
            },
        }

        cancel_markup = InlineKeyboardMarkup(
            [[create_btn("🛑 Cancel Indexing", callback_data="index#cancel", style=BTN_DANGER)]]
        )

        await safe_edit_text(
            status_msg,
            f"⚡ <b>Starting Distributed Multi-Bot Indexing...</b>\n"
            f"🤖 <b>Active Parallel Bots:</b> <code>{len(bot_ranges)}</code>\n"
            f"📊 <b>Total Messages to Scan:</b> <code>{total_range:,}</code>",
            reply_markup=cancel_markup,
        )

        # Worker coroutine: each bot independently fetches chunks of 200 in its assigned ID range
        async def _worker_task(
            worker_client: Client, w_start: int, w_end: int, b_label: str
        ):
            total_worker_msgs = max(1, w_end - w_start + 1)
            done_by_worker = 0

            for chunk_start in range(w_start, w_end + 1, 200):
                if temp.CANCEL or stats["quota_full"]:
                    break

                chunk_end = min(w_end, chunk_start + 199)
                msg_ids = list(range(chunk_start, chunk_end + 1))

                messages = None
                for attempt in range(3):
                    try:
                        messages = await worker_client.get_messages(chat_id, msg_ids)
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 2)
                    except Exception as e:
                        logger.warning(f"Worker {b_label} get_messages error: {e}")
                        await asyncio.sleep(2)

                if messages is None:
                    stats["errors"] += len(msg_ids)
                    stats["processed"] += len(msg_ids)
                    done_by_worker += len(msg_ids)
                    continue

                if not isinstance(messages, list):
                    messages = [messages]

                media_batch = []
                local_deleted = 0
                local_non_media = 0

                for msg in messages:
                    if temp.CANCEL or stats["quota_full"]:
                        break
                    if not msg or getattr(msg, "empty", False):
                        local_deleted += 1
                        continue
                    media_obj, status_code = extract_media_from_message(
                        msg, media_filter
                    )
                    if status_code == "ok" and media_obj is not None:
                        media_batch.append(media_obj)
                    else:
                        local_non_media += 1

                stats["deleted"] += local_deleted
                stats["non_media"] += local_non_media

                if media_batch:
                    s_cnt, d_cnt, e_cnt, q_full = await fast_save_media_batch(
                        media_batch
                    )
                    stats["saved"] += s_cnt
                    stats["dups"] += d_cnt
                    stats["errors"] += e_cnt
                    if q_full:
                        stats["quota_full"] = True
                        temp.CANCEL = True

                chunk_len = len(msg_ids)
                stats["processed"] += chunk_len
                done_by_worker += chunk_len
                pct = min(100.0, (done_by_worker / total_worker_msgs) * 100)
                stats["worker_progress"][b_label] = (
                    f"{chunk_end:,} / {w_end:,} ({pct:.1f}%)"
                )

                # Save checkpoint periodically for single-bot or first worker
                if chunk_end % 2000 == 0:
                    temp.CURRENT = chunk_end
                    await save_index_checkpoint(
                        chat_id, chunk_end, last_msg_id, media_filter
                    )

        # Live UI progress updater (updates Telegram message every 5 seconds to avoid FloodWait)
        workers_done = False

        async def _ui_progress_loop():
            while not workers_done and not temp.CANCEL and not stats["quota_full"]:
                await asyncio.sleep(5)
                if workers_done:
                    break
                elapsed = max(0.1, time.time() - start_time)
                proc = stats["processed"]
                speed = proc / elapsed
                remaining = max(0, total_range - proc)
                eta_str = format_eta(remaining / speed if speed > 0 else 0)
                pct_total = min(100.0, (proc / total_range) * 100)

                worker_lines = "\n".join(
                    f"├ 🤖 <code>{lbl}</code>: <code>{prog}</code>"
                    for lbl, prog in list(stats["worker_progress"].items())[:11]
                )

                progress_text = (
                    f"⚡ <b>Distributed Multi-Bot Indexing Live</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🤖 <b>Active Bots:</b> <code>{len(bot_ranges)} Parallel Workers</code>\n"
                    f"📈 <b>Overall Progress:</b> <code>{pct_total:.2f}%</code> (<code>{proc:,} / {total_range:,}</code>)\n"
                    f"🚀 <b>Speed:</b> <code>{speed:,.0f} msgs/sec</code> | ⏳ <b>ETA:</b> <code>{eta_str}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ <b>Saved Files:</b> <code>{stats['saved']:,}</code>\n"
                    f"♻️ <b>Duplicates Skipped:</b> <code>{stats['dups']:,}</code>\n"
                    f"🗑️ <b>Deleted Msgs:</b> <code>{stats['deleted']:,}</code>\n"
                    f"📄 <b>Non-Media Skipped:</b> <code>{stats['non_media']:,}</code>\n"
                    f"⚠️ <b>Errors:</b> <code>{stats['errors']:,}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<b>Worker Partitions:</b>\n{worker_lines}"
                )
                if skipped_bots:
                    progress_text += (
                        f"\n\n⚠️ <i>Bots not Admin in channel: {', '.join(skipped_bots[:3])}</i>"
                    )

                await safe_edit_text(
                    status_msg, progress_text, reply_markup=cancel_markup
                )

        ui_task = asyncio.create_task(_ui_progress_loop())
        await asyncio.gather(
            *[
                _worker_task(c, ws, we, lbl)
                for c, ws, we, lbl in bot_ranges
            ],
            return_exceptions=True,
        )
        workers_done = True
        ui_task.cancel()

        elapsed_total = max(0.1, time.time() - start_time)
        avg_speed = stats["processed"] / elapsed_total

        if stats["quota_full"]:
            await safe_edit_text(
                status_msg,
                f"⚠️ <b>Indexing Paused: MongoDB Storage Quota Reached (512 MB)!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ <b>Saved Before Full:</b> <code>{stats['saved']:,}</code>\n"
                f"📊 <b>Processed:</b> <code>{stats['processed']:,} / {total_range:,}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <b>How to Fix Immediately:</b>\n"
                f"1️⃣ Send <code>/freedb</code> to drop heavy indexes & shrink documents by 65%, OR\n"
                f"2️⃣ Add <code>SECOND_DATABASE_URI</code> / <code>THIRD_DATABASE_URI</code> in your environment variables for 1.5 GB storage!",
            )
            return

        if temp.CANCEL:
            await safe_edit_text(
                status_msg,
                f"🛑 <b>Indexing Cancelled by Admin!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤖 <b>Workers Used:</b> <code>{len(bot_ranges)} Bots</code>\n"
                f"📊 <b>Processed:</b> <code>{stats['processed']:,} / {total_range:,}</code>\n"
                f"✅ <b>Saved Files:</b> <code>{stats['saved']:,}</code>\n"
                f"♻️ <b>Duplicates:</b> <code>{stats['dups']:,}</code>\n"
                f"⏱️ <b>Elapsed:</b> <code>{format_eta(elapsed_total)}</code> (<code>{avg_speed:,.0f} msgs/s</code>)",
            )
            return

        await clear_index_checkpoint(chat_id)
        temp.CURRENT = 0

        await safe_edit_text(
            status_msg,
            f"🎉 <b>Distributed Multi-Bot Indexing Completed!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Parallel Bots Used:</b> <code>{len(bot_ranges)} Bots</code>\n"
            f"📊 <b>Total Messages Scanned:</b> <code>{stats['processed']:,}</code>\n"
            f"✅ <b>Total Files Saved:</b> <code>{stats['saved']:,}</code>\n"
            f"♻️ <b>Duplicates Skipped:</b> <code>{stats['dups']:,}</code>\n"
            f"🗑️ <b>Deleted Messages:</b> <code>{stats['deleted']:,}</code>\n"
            f"📄 <b>Non-Media Messages:</b> <code>{stats['non_media']:,}</code>\n"
            f"⏱️ <b>Time Taken:</b> <code>{format_eta(elapsed_total)}</code> (Avg: <code>{avg_speed:,.0f} msgs/sec</code>)\n"
            f"━━━━━━━━━━━━━━━━━━━━━",
        )
