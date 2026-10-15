import asyncio
import html
import inspect
import re
import time
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Tuple

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import Message

import info
from utils import save_group_settings

# Optional standard connection database fallback
try:
    from database.connections_mdb import active_connection as _std_active_connection
except ImportError:
    _std_active_connection = None

logger = getLogger(__name__)
logger.setLevel(ERROR)

# Backup original default File Not Found image & text from info.py
_DEFAULT_FNF_IMG = getattr(info, "NOT_FOUND_IMG", None)
_DEFAULT_FNF_MSG = getattr(info, "NOT_FOUND_MSG", "<b>🚫 File not found.</b>")

# ============================================================
# 🛡️ PYROGRAM v2.2.26 (LAYER 230) COMPATIBILITY ENGINE
# ============================================================
try:
    from pyrogram.types import LinkPreviewOptions
except ImportError:
    LinkPreviewOptions = None

try:
    from pyrogram.types import ReplyParameters
except ImportError:
    ReplyParameters = None

_REPLY_PARAMS = set(inspect.signature(Message.reply_text).parameters.keys())
_EDIT_PARAMS = set(inspect.signature(Message.edit_text).parameters.keys())


def _prepare_compat_kwargs(
    kwargs: dict, supported_params: set, disable_preview: Optional[bool] = None
) -> dict:
    clean_kw = dict(kwargs)
    explicit_disable = clean_kw.pop("disable_web_page_preview", disable_preview)

    if explicit_disable is not None:
        if "disable_web_page_preview" in supported_params:
            clean_kw["disable_web_page_preview"] = bool(explicit_disable)
        elif (
            "link_preview_options" in supported_params
            and LinkPreviewOptions is not None
        ):
            try:
                clean_kw["link_preview_options"] = LinkPreviewOptions(
                    is_disabled=bool(explicit_disable)
                )
            except Exception:
                pass

    if "reply_to_message_id" in clean_kw:
        rep_id = clean_kw.pop("reply_to_message_id")
        if rep_id:
            if "reply_to_message_id" in supported_params:
                clean_kw["reply_to_message_id"] = rep_id
            elif "reply_parameters" in supported_params and ReplyParameters is not None:
                try:
                    clean_kw["reply_parameters"] = ReplyParameters(message_id=rep_id)
                except Exception:
                    pass

    return clean_kw


async def safe_reply_text(
    message: Message, text: str, disable_web_page_preview: bool = True, **kwargs
) -> Optional[Message]:
    compat_kw = _prepare_compat_kwargs(
        kwargs, _REPLY_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await message.reply_text(text, **compat_kw)
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        compat_kw.pop("reply_parameters", None)
        compat_kw.pop("reply_to_message_id", None)
        return await message.reply_text(text, **compat_kw)
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None


async def safe_edit_text(
    message: Message, text: str, disable_web_page_preview: bool = True, **kwargs
) -> Optional[Message]:
    compat_kw = _prepare_compat_kwargs(
        kwargs, _EDIT_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await message.edit_text(text, **compat_kw)
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        return await message.edit_text(text, **compat_kw)
    except (MessageNotModified, MessageIdInvalid, ChatWriteForbidden, Forbidden):
        return None


# ============================================================
# ⚙️ DIRECT MONGODB ISOLATION & IN-MEMORY CACHE
# ============================================================
b_settings_db = g_welcome_db = _active_db = _std_conn_db = _custom_cap_db = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    b_settings_db = _BOT_DB["global_bot_settings"]
    g_welcome_db = _BOT_DB["group_welcome_settings"]
    _active_db = _BOT_DB["active_connections"]
    _std_conn_db = _BOT_DB["CONNECTION"]
    _custom_cap_db = _BOT_DB["custom_captions"]
except Exception as e:
    logger.error(f"Failed to init custom settings DB: {e}")

# Fast in-memory cache so pm_filter.py and fsub.py never wait on MongoDB round-trips
_BOT_SETTINGS_CACHE: Optional[Dict[str, Any]] = None
_BOT_SETTINGS_CACHE_TS: float = 0.0
_WELCOME_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}

# Map custom_settings keys to info.py runtime attributes so all plugins stay in sync
_INFO_ATTR_MAP = {
    "fsub_img": "FSUB_IMG",
    "fsub_msg": "FSUB_MSG",
    "auto_img": "AUTO_IMG",
    "not_found_img": "NOT_FOUND_IMG",
    "not_found_text": "NOT_FOUND_MSG",
    "info_msg": "INFO_MSG",
    "info_img": "INFO_IMG",
    "del_msg": "DEL_MSG",
    "del_img": "DEL_IMG",
}


async def get_bot_settings(force_refresh: bool = False) -> Dict[str, Any]:
    global _BOT_SETTINGS_CACHE, _BOT_SETTINGS_CACHE_TS
    now = time.time()
    if (
        not force_refresh
        and _BOT_SETTINGS_CACHE is not None
        and (now - _BOT_SETTINGS_CACHE_TS) < 60
    ):
        return _BOT_SETTINGS_CACHE

    if b_settings_db is not None:
        try:
            doc = await b_settings_db.find_one({"_id": "bot_config"}) or {}
            _BOT_SETTINGS_CACHE = doc
            _BOT_SETTINGS_CACHE_TS = now
            return doc
        except Exception as e:
            logger.error(f"get_bot_settings error: {e}")

    return _BOT_SETTINGS_CACHE or {}


async def update_bot_settings(key: str, value: Any):
    global _BOT_SETTINGS_CACHE, _BOT_SETTINGS_CACHE_TS
    if b_settings_db is not None:
        try:
            await b_settings_db.update_one(
                {"_id": "bot_config"}, {"$set": {key: value}}, upsert=True
            )
        except Exception as e:
            logger.error(f"update_bot_settings error: {e}")

    if _BOT_SETTINGS_CACHE is None:
        _BOT_SETTINGS_CACHE = {}
    _BOT_SETTINGS_CACHE[key] = value
    _BOT_SETTINGS_CACHE_TS = time.time()

    # Sync to runtime `info` module and `custom_captions` collection
    info_attr = _INFO_ATTR_MAP.get(key)
    if info_attr:
        setattr(info, info_attr, value)
        if _custom_cap_db is not None:
            try:
                await _custom_cap_db.update_one(
                    {"_id": "settings"}, {"$set": {info_attr: value}}, upsert=True
                )
            except Exception:
                pass


async def get_group_welcome(chat_id: int) -> Dict[str, Any]:
    cid = int(chat_id)
    now = time.time()
    cached = _WELCOME_CACHE.get(cid)
    if cached and (now - cached[0]) < 60:
        return cached[1]

    if g_welcome_db is not None:
        try:
            doc = await g_welcome_db.find_one({"_id": cid}) or {}
            _WELCOME_CACHE[cid] = (now, doc)
            return doc
        except Exception:
            pass
    return {}


async def update_group_welcome(chat_id: int, key: str, value: Any):
    cid = int(chat_id)
    if g_welcome_db is not None:
        try:
            await g_welcome_db.update_one(
                {"_id": cid}, {"$set": {key: value}}, upsert=True
            )
        except Exception:
            pass

    cached = _WELCOME_CACHE.get(cid)
    cur_doc = dict(cached[1]) if cached else {}
    cur_doc[key] = value
    _WELCOME_CACHE[cid] = (time.time(), cur_doc)

    try:
        if key == "welcome":
            await save_group_settings(cid, "welcome", bool(value))
        elif key == "text":
            await save_group_settings(cid, "welcome_text", value)
        elif key == "img":
            await save_group_settings(cid, "welcome_img", value)
    except Exception:
        pass


# ============================================================
# 👑 SMART AUTHORIZATION & CONNECTION ENGINE
# ============================================================
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
    return int(user_id) in get_admin_list() if user_id else False


async def is_group_admin(client: Client, user_id: int, chat_id: int) -> bool:
    if await is_global_admin(user_id):
        return True
    if not user_id or not chat_id:
        return False
    try:
        member = await client.get_chat_member(int(chat_id), int(user_id))
        return member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]
    except Exception:
        return False


async def get_active_group(user_id: int) -> Optional[int]:
    if callable(_std_active_connection):
        try:
            conn = await _std_active_connection(str(user_id))
            if conn:
                return int(conn)
        except Exception:
            pass

    try:
        if _active_db is not None:
            doc = await _active_db.find_one(
                {
                    "$or": [
                        {"user_id": str(user_id)},
                        {"user_id": int(user_id)},
                        {"_id": str(user_id)},
                        {"_id": int(user_id)},
                    ]
                }
            )
            if doc:
                gid = (
                    doc.get("group_id") or doc.get("active_group") or doc.get("chat_id")
                )
                if gid:
                    return int(gid)
    except Exception as e:
        logger.error(f"Error fetching active connection: {e}")

    try:
        if _std_conn_db is not None:
            doc = await _std_conn_db.find_one(
                {"$or": [{"_id": str(user_id)}, {"_id": int(user_id)}]}
            )
            if doc and doc.get("active_group"):
                return int(doc["active_group"])
    except Exception:
        pass

    return None


async def get_target_chat(client: Client, message: Message) -> Optional[int]:
    if message.command and len(message.command) > 1:
        first_arg = message.command[1].strip()
        if first_arg.startswith("-100") and first_arg.lstrip("-").isdigit():
            return int(first_arg)

    if message.chat.type == enums.ChatType.PRIVATE:
        if not message.from_user:
            return None
        connected_chat = await get_active_group(message.from_user.id)
        if connected_chat:
            return int(connected_chat)
        return None
    return message.chat.id


def extract_text_payload(message: Message) -> Optional[str]:
    if (
        message.reply_to_message
        and len(getattr(message, "command", [])) == 1
        and (message.reply_to_message.text or message.reply_to_message.caption)
    ):
        replied = message.reply_to_message
        raw = replied.text or replied.caption
        return getattr(raw, "html", str(raw)).strip()

    if message.text and len(getattr(message, "command", [])) > 1:
        raw_html = getattr(message.text, "html", str(message.text))
        cleaned = re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s+", "", raw_html, count=1)
        cleaned = re.sub(r"^-100\d+\s*", "", cleaned, count=1).strip()
        return cleaned or None
    return None


def _invalidate_pm_filter_search_cache():
    try:
        import plugins.pm_filter as _pmf

        _pmf.GLOBAL_SEARCH_CACHE._data.clear()
    except Exception:
        pass


# ============================================================
# 🛑 SMART STOPWORDS ENGINE (GLOBAL SETTING)
# ============================================================
DEFAULT_STOPWORDS = [
    "send",
    "snd",
    "give",
    "gib",
    "pls",
    "plz",
    "please",
    "need",
    "want",
    "upload",
    "uplod",
    "drop",
    "share",
    "find",
    "search",
    "provide",
    "post",
    "movie",
    "movies",
    "film",
    "films",
    "cinema",
    "cinemas",
    "full",
    "fullmovie",
    "download",
    "downlod",
    "link",
    "links",
    "file",
    "files",
    "print",
    "audio",
    "ott",
    "hd",
    "hq",
    "bluray",
    "rip",
    "watch",
    "online",
    "admin",
    "beku",
    "bekithu",
    "bekittu",
    "bekagide",
    "kodi",
    "kodro",
    "kalsi",
    "kalsro",
    "kalisi",
    "haki",
    "haku",
    "hakro",
    "ideya",
    "irboda",
    "bidi",
    "madu",
    "yaradru",
    "kannadadalli",
    "sandalwood",
    "kr_picture",
    "kannada_filmy_group",
    "telegram",
    "dubbed",
]

ACTIVE_STOPWORDS = DEFAULT_STOPWORDS.copy()
_stopwords_loaded = False


def get_stopwords() -> List[str]:
    return ACTIVE_STOPWORDS


@Client.on_message(filters.all, group=-999)
async def load_stopwords_on_boot(client: Client, message: Message):
    global _stopwords_loaded, ACTIVE_STOPWORDS
    if _stopwords_loaded:
        return
    _stopwords_loaded = True
    try:
        settings = await get_bot_settings(force_refresh=True)
        custom_stops = settings.get("custom_stopwords")
        if custom_stops is not None:
            ACTIVE_STOPWORDS = list(custom_stops)
        for db_key, info_attr in _INFO_ATTR_MAP.items():
            if db_key in settings and settings[db_key] is not None:
                setattr(info, info_attr, settings[db_key])
    except Exception as e:
        logger.error(f"Boot settings load error: {e}")


@Client.on_message(filters.command("addstopwords") & filters.incoming, group=-2)
async def add_stopwords(client: Client, message: Message):
    global ACTIVE_STOPWORDS
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> You must be a Global Bot Admin to use this command.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/addstopwords word1, word2</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    raw_input = message.text.split(None, 1)[1]
    new_words = [w.strip().lower() for w in re.split(r"[,\n]+", raw_input) if w.strip()]
    ACTIVE_STOPWORDS = sorted(list(set(ACTIVE_STOPWORDS + new_words)))
    await update_bot_settings("custom_stopwords", ACTIVE_STOPWORDS)
    _invalidate_pm_filter_search_cache()
    await safe_reply_text(
        message,
        f"✅ <b>Added {len(new_words)} stopwords globally.</b>\n"
        f"📊 <b>Total Active:</b> <code>{len(ACTIVE_STOPWORDS)}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("stopwords") & filters.incoming, group=-2)
async def show_stopwords(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> You must be a Global Bot Admin.",
            parse_mode=enums.ParseMode.HTML,
        )

    if not ACTIVE_STOPWORDS:
        return await safe_reply_text(
            message,
            "ℹ️ <b>No stopwords are currently active.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    escaped_words = html.escape(", ".join(ACTIVE_STOPWORDS))
    await safe_reply_text(
        message,
        f"🛑 <b>Current Active Stopwords:</b>\n\n"
        f"<code>{escaped_words}</code>\n\n"
        f"<b>Total:</b> <code>{len(ACTIVE_STOPWORDS)}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remstopwords") & filters.incoming, group=-2)
async def rem_stopwords(client: Client, message: Message):
    global ACTIVE_STOPWORDS
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> You must be a Global Bot Admin.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/remstopwords word1, word2</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    raw_input = message.text.split(None, 1)[1]
    words_to_remove = {
        w.strip().lower() for w in re.split(r"[,\n]+", raw_input) if w.strip()
    }
    if not ACTIVE_STOPWORDS:
        return await safe_reply_text(
            message,
            "ℹ️ <b>There are no stopwords to remove.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    original_count = len(ACTIVE_STOPWORDS)
    ACTIVE_STOPWORDS = [w for w in ACTIVE_STOPWORDS if w not in words_to_remove]
    await update_bot_settings("custom_stopwords", ACTIVE_STOPWORDS)
    _invalidate_pm_filter_search_cache()
    await safe_reply_text(
        message,
        f"✅ <b>Removed {original_count - len(ACTIVE_STOPWORDS)} stopwords.</b>\n"
        f"📊 <b>Total Left:</b> <code>{len(ACTIVE_STOPWORDS)}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remallstopwords") & filters.incoming, group=-2)
async def rem_all_stopwords(client: Client, message: Message):
    global ACTIVE_STOPWORDS
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    ACTIVE_STOPWORDS = []
    await update_bot_settings("custom_stopwords", [])
    _invalidate_pm_filter_search_cache()
    await safe_reply_text(
        message,
        "🗑️ <b>All stopwords have been completely removed globally.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("defaultstopwords") & filters.incoming, group=-2)
async def default_stopwords(client: Client, message: Message):
    global ACTIVE_STOPWORDS
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    ACTIVE_STOPWORDS = DEFAULT_STOPWORDS.copy()
    await update_bot_settings("custom_stopwords", ACTIVE_STOPWORDS)
    _invalidate_pm_filter_search_cache()
    await safe_reply_text(
        message,
        "✅ <b>Stopwords have been successfully reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# ⚡ 10-LAYER TITANIUM UPLOAD ENGINE (FOR PERMANENT IMAGES)
# ============================================================
def _upload_sync(file_bytes):
    headers = {"User-Agent": "Mozilla/5.0"}
    for url, files_dict, data_dict in [
        (
            "https://catbox.moe/user/api.php",
            {"fileToUpload": ("img.jpg", file_bytes, "image/jpeg")},
            {"reqtype": "fileupload"},
        ),
        (
            "https://graph.org/upload",
            {"file": ("img.jpg", file_bytes, "image/jpeg")},
            None,
        ),
        ("https://envs.sh", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        ("https://x0.at", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        ("https://ttm.sh", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        ("https://0x0.st", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        (
            "https://telegra.ph/upload",
            {"file": ("img.jpg", file_bytes, "image/jpeg")},
            None,
        ),
    ]:
        try:
            res = requests.post(
                url, data=data_dict, files=files_dict, headers=headers, timeout=8
            )
            if res.status_code in [200, 201]:
                if url in ["https://graph.org/upload", "https://telegra.ph/upload"]:
                    return url.rsplit("/", 1)[0] + res.json()[0]["src"]
                elif res.text.strip().startswith("http"):
                    return res.text.strip()
        except Exception:
            continue
    return None


async def upload_image_safely(client: Client, message: Message):
    try:
        file_io = await client.download_media(message, in_memory=True)
        if not file_io:
            return None, "❌ Failed to download the image."
        url = await asyncio.to_thread(_upload_sync, file_io.getvalue())
        if not url:
            return None, "❌ All cloud uploaders failed."
        return url, None
    except Exception as e:
        return None, f"❌ Internal Error: {e}"


# ============================================================
# 🖼️ GLOBAL IMAGE SETTINGS (GLOBAL ADMINS ONLY)
# ============================================================
async def process_global_image_setting(
    client: Client, message: Message, setting_key: str, success_msg: str
):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> You must be a Global Bot Admin to use this.",
            parse_mode=enums.ParseMode.HTML,
        )

    target_msg = (
        message.reply_to_message
        if (message.reply_to_message and message.reply_to_message.photo)
        else (message if message.photo else None)
    )

    if target_msg is None and len(message.command) > 1:
        arg = message.text.split(None, 1)[1].strip()
        if arg.lower() in ["off", "none", "reset", "clear"]:
            await update_bot_settings(setting_key, None)
            return await safe_reply_text(
                message,
                f"🗑️ <b>{success_msg.replace('updated!', 'removed!')}</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        if arg.startswith(("http://", "https://")) or len(arg) > 20:
            await update_bot_settings(setting_key, arg)
            return await safe_reply_text(
                message,
                f"✅ <b>{success_msg}</b>",
                parse_mode=enums.ParseMode.HTML,
            )

    if not target_msg or not target_msg.photo:
        return await safe_reply_text(
            message,
            f"⚠️ <b>Usage:</b> Reply to a photo with <code>/{message.command[0]}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message, "⏳ <b>Uploading to Cloud...</b>", parse_mode=enums.ParseMode.HTML
    )
    url, _ = await upload_image_safely(client, target_msg)

    if url:
        await update_bot_settings(setting_key, url)
        if status:
            await safe_edit_text(
                status,
                f"✅ <b>{success_msg}</b>\n<i>(Saved permanently to cloud)</i>",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        file_id = target_msg.photo.file_id
        await update_bot_settings(setting_key, file_id)
        if status:
            await safe_edit_text(
                status,
                f"⚠️ Cloud blocked. <b>{success_msg}</b>\n<i>(Saved as Telegram File_ID)</i>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(filters.command("setfsubimg") & filters.incoming, group=-2)
async def set_fsub_img(client: Client, message: Message):
    await process_global_image_setting(
        client, message, "fsub_img", "Force Subscribe Image updated!"
    )


@Client.on_message(filters.command("setautoimg") & filters.incoming, group=-2)
async def set_auto_img(client: Client, message: Message):
    await process_global_image_setting(
        client, message, "auto_img", "Default Auto-Filter Image updated!"
    )


@Client.on_message(filters.command("setfilenotfoundimg") & filters.incoming, group=-2)
async def set_fnf_img(client: Client, message: Message):
    await process_global_image_setting(
        client, message, "not_found_img", "File Not Found Image updated!"
    )


@Client.on_message(filters.command("remautoimg") & filters.incoming, group=-2)
async def rem_auto_img(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )
    await update_bot_settings("auto_img", None)
    await safe_reply_text(
        message,
        "🗑️ <b>Default Auto-Filter Image removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remfilenotfoundimg") & filters.incoming, group=-2)
async def rem_fnf_img(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )
    await update_bot_settings("not_found_img", None)
    await safe_reply_text(
        message,
        "🗑️ <b>File Not Found Image removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(
    filters.command("defaultfilenotfoundimg") & filters.incoming, group=-2
)
async def default_fnf_img(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )
    await update_bot_settings("not_found_img", _DEFAULT_FNF_IMG)
    await safe_reply_text(
        message,
        "✅ <b>File Not Found Image reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 📝 GLOBAL TEXT SETTINGS (GLOBAL ADMINS ONLY)
# ============================================================
@Client.on_message(filters.command("setnotfoundtext") & filters.incoming, group=-2)
async def set_fnf_text(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    text_val = extract_text_payload(message)
    if not text_val:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/setnotfoundtext &lt;your text&gt;</code> or reply to a message.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_bot_settings("not_found_text", text_val)
    await safe_reply_text(
        message,
        "✅ <b>File Not Found Text updated!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remnotfoundtext") & filters.incoming, group=-2)
async def rem_fnf_text(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )
    await update_bot_settings("not_found_text", None)
    await safe_reply_text(
        message,
        "🗑️ <b>File Not Found Text removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("defaultnotfoundtext") & filters.incoming, group=-2)
async def def_fnf_text(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(user_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )
    await update_bot_settings("not_found_text", _DEFAULT_FNF_MSG)
    await safe_reply_text(
        message,
        "✅ <b>File Not Found Text reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 🪄 SPELL CHECK (SMART PM & GROUP ROUTING)
# ============================================================
@Client.on_message(filters.command("enablespellcheck") & filters.incoming, group=-2)
async def enable_spell_check(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b>\nPlease run <code>/connect &lt;group_id&gt;</code> first or use this command inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message,
            f"❌ <b>Unauthorized:</b> You must be an Admin of the target group (<code>{target_chat_id}</code>).",
            parse_mode=enums.ParseMode.HTML,
        )

    await save_group_settings(target_chat_id, "spell_check", True)
    await safe_reply_text(
        message,
        f"✅ <b>Spell Check Enabled for chat:</b> <code>{target_chat_id}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("disablespellcheck") & filters.incoming, group=-2)
async def disable_spell_check(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b>\nPlease run <code>/connect &lt;group_id&gt;</code> first or use this command inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message,
            f"❌ <b>Unauthorized:</b> You must be an Admin of the target group (<code>{target_chat_id}</code>).",
            parse_mode=enums.ParseMode.HTML,
        )

    await save_group_settings(target_chat_id, "spell_check", False)
    await safe_reply_text(
        message,
        f"🚫 <b>Spell Check Disabled for chat:</b> <code>{target_chat_id}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 👋 WELCOME SETTINGS & AUTO-GREETER (SMART PM & GROUP ROUTING)
# ============================================================
@Client.on_message(filters.command("enablewelcome") & filters.incoming, group=-2)
async def enable_welc(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b> Please run <code>/connect &lt;group_id&gt;</code> first or use this inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    await update_group_welcome(target_chat_id, "welcome", True)
    await safe_reply_text(
        message,
        f"✅ <b>Welcome Messages Enabled for <code>{target_chat_id}</code>.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("disablewelcome") & filters.incoming, group=-2)
async def disable_welc(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b> Please run <code>/connect &lt;group_id&gt;</code> first or use this inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    await update_group_welcome(target_chat_id, "welcome", False)
    await safe_reply_text(
        message,
        f"🚫 <b>Welcome Messages Disabled for <code>{target_chat_id}</code>.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setwelcometxt") & filters.incoming, group=-2)
async def set_welc_txt(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b> Please run <code>/connect &lt;group_id&gt;</code> first or use this inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    text_val = extract_text_payload(message)
    if not text_val:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/setwelcometxt &lt;Your text&gt;</code> or reply to a text message.\n\n"
            "💡 <b>Variables:</b> <code>{mention}</code>, <code>{title}</code>, <code>{count}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    await update_group_welcome(target_chat_id, "text", text_val)
    await update_group_welcome(target_chat_id, "welcome", True)
    await safe_reply_text(
        message,
        f"✅ <b>Welcome Text updated for <code>{target_chat_id}</code>!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setwelcomeimg") & filters.incoming, group=-2)
async def set_welc_img(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b> Please run <code>/connect &lt;group_id&gt;</code> first or use this inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    target_msg = (
        message.reply_to_message
        if (message.reply_to_message and message.reply_to_message.photo)
        else (message if message.photo else None)
    )

    if not target_msg or not target_msg.photo:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> Reply to a photo with <code>/setwelcomeimg</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message, "⏳ <b>Uploading to Cloud...</b>", parse_mode=enums.ParseMode.HTML
    )
    url, _ = await upload_image_safely(client, target_msg)
    if url:
        await update_group_welcome(target_chat_id, "img", url)
        if status:
            await safe_edit_text(
                status,
                f"✅ <b>Welcome Image updated for <code>{target_chat_id}</code> (Cloud Saved)!</b>",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        await update_group_welcome(target_chat_id, "img", target_msg.photo.file_id)
        if status:
            await safe_edit_text(
                status,
                f"⚠️ Cloud blocked. <b>Welcome Image updated for <code>{target_chat_id}</code> (File ID Saved)!</b>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(filters.command("setwelcome") & filters.incoming, group=-2)
async def set_welcome_both(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b> Please run <code>/connect &lt;group_id&gt;</code> first or use this inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    target_msg = (
        message.reply_to_message
        if (message.reply_to_message and message.reply_to_message.photo)
        else (message if message.photo else None)
    )

    if not target_msg or not target_msg.photo:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> Reply to a photo containing a caption with <code>/setwelcome</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    raw_cap = target_msg.caption
    txt = getattr(raw_cap, "html", str(raw_cap)) if raw_cap else None
    if txt and txt.startswith("/setwelcome"):
        txt = re.sub(r"^/setwelcome(?:@[a-zA-Z0-9_]+)?\s*", "", txt, count=1).strip()

    status = await safe_reply_text(
        message, "⏳ <b>Processing...</b>", parse_mode=enums.ParseMode.HTML
    )

    url, _ = await upload_image_safely(client, target_msg)
    img_data = url if url else target_msg.photo.file_id

    await update_group_welcome(target_chat_id, "img", img_data)
    if txt:
        await update_group_welcome(target_chat_id, "text", txt)
    await update_group_welcome(target_chat_id, "welcome", True)

    if status:
        await safe_edit_text(
            status,
            f"✅ <b>Welcome Image & Text set for <code>{target_chat_id}</code>!</b>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("delwelcome") & filters.incoming, group=-2)
async def del_welcome(client: Client, message: Message):
    user_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    target_chat_id = await get_target_chat(client, message)

    if not target_chat_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connection!</b> Please run <code>/connect &lt;group_id&gt;</code> first or use this inside a group.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_group_admin(client, user_id, target_chat_id):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    await update_group_welcome(target_chat_id, "img", None)
    await update_group_welcome(target_chat_id, "text", None)
    await update_group_welcome(target_chat_id, "welcome", False)
    await safe_reply_text(
        message,
        f"🗑️ <b>Welcome settings deleted and disabled for <code>{target_chat_id}</code>.</b>",
        parse_mode=enums.ParseMode.HTML,
    )
