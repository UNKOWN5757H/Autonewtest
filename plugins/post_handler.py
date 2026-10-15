import asyncio
import html
import inspect
import math
import random
import re
import urllib.parse
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    QueryIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from utils import temp

try:
    from plugins.Imdbposter import get_movie_detailsx
except ImportError:
    try:
        from Imdbposter import get_movie_detailsx
    except ImportError:

        async def get_movie_detailsx(name: str, **kwargs):
            return None


try:
    from database.users_chats_db import db as _db
except ImportError:
    _db = None

try:
    from database.connections_mdb import active_connection as _std_active_connection
except ImportError:
    _std_active_connection = None

logger = getLogger(__name__)
logger.setLevel(ERROR)

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

_SEND_MSG_PARAMS = set(inspect.signature(Client.send_message).parameters.keys())
_EDIT_MSG_PARAMS = set(inspect.signature(Client.edit_message_text).parameters.keys())
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


async def safe_send_message(
    client: Client,
    chat_id: int,
    text: str,
    disable_web_page_preview: bool = False,
    **kwargs,
) -> Optional[Message]:
    compat_kw = _prepare_compat_kwargs(
        kwargs, _SEND_MSG_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await client.send_message(chat_id, text=text, **compat_kw)
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        compat_kw.pop("reply_parameters", None)
        compat_kw.pop("reply_to_message_id", None)
        return await client.send_message(chat_id, text=text, **compat_kw)


async def safe_edit_message_text(
    client: Client,
    chat_id: int,
    message_id: int,
    text: str,
    disable_web_page_preview: bool = False,
    **kwargs,
) -> Optional[Message]:
    compat_kw = _prepare_compat_kwargs(
        kwargs, _EDIT_MSG_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await client.edit_message_text(
            chat_id, message_id, text=text, **compat_kw
        )
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        return await client.edit_message_text(
            chat_id, message_id, text=text, **compat_kw
        )


# ============================================================
# ⚙️ MONGODB DRAFTS & SETTINGS CONFIG
# ============================================================
drafts_db = global_post_db = _active_db = _std_conn_db = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    drafts_db = _BOT_DB["post_drafts"]
    global_post_db = _BOT_DB["global_post_settings"]
    _active_db = _BOT_DB["active_connections"]
    _std_conn_db = _BOT_DB["CONNECTION"]
except Exception as e:
    logger.error(f"Failed to init DBs: {e}")


async def get_global_post_settings() -> dict:
    if global_post_db is not None:
        doc = await global_post_db.find_one({"_id": "settings"})
        return doc or {}
    return {}


async def save_global_post_settings(key: str, value: Any):
    if global_post_db is not None:
        if value is None:
            await global_post_db.update_one(
                {"_id": "settings"}, {"$unset": {key: ""}}, upsert=True
            )
        else:
            await global_post_db.update_one(
                {"_id": "settings"}, {"$set": {key: value}}, upsert=True
            )


post_sessions: Dict[int, dict] = {}
TEMP_SEARCH: Dict[int, List[str]] = {}
USE_GETFILE_BUTTON_BY_DEFAULT = True

DEFAULT_WATERMARK = "<b>Jᴏɪɴ: @Sandalwood_Kannada_Moviesz</b>"
LANGUAGES_FORMAT = "<b>🔊 : {langs}</b>"
RESOLUTIONS_FORMAT = "<b>🖥️ : {resolutions}</b>"
GENRES_FORMAT = "<b>🎥 : {genres}</b>"
OTT_FORMAT = "<b>📺 : #{otts}</b>"

TEMPLATES = {
    "clean_grid": """✅ <b>{title} {year}</b>\n\n<blockquote>{LANGUAGES}\n{RESOLUTIONS}\n{GENRES}\n{OTT_PLATFORMS}\n<b>📟 : Available In Files.</b>\n\n<b>=========================</b></blockquote>""",
    "divider_list": """🎬 <b>{title} {year}</b>\n━━━━━━━━━━━━━━━━━━\n<blockquote>{LANGUAGES}\n{RESOLUTIONS}\n{OTT_PLATFORMS}</blockquote>""",
}

LANGUAGES = [
    "Kannada",
    "English",
    "Gujarati",
    "Hindi",
    "Bengali",
    "Malayalam",
    "Marathi",
    "Punjabi",
    "Tamil",
    "Telugu",
    "Urdu",
    "#NotAvailable",
]
RESOLUTIONS = [
    "480p",
    "720p",
    "1080p",
    "1440p",
    "2160p",
    "4k",
    "BluRay",
    "BDRip",
    "WEB-DL",
    "HDRip",
    "HEVC",
    "#NotAvailable",
]
GENRES = [
    "Action",
    "Adventure",
    "Animation",
    "Biography",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Horror",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "#NotAvailable",
]
OTT_PLATFORMS = [
    "Aha",
    "ALTBalaji",
    "JioHotstar",
    "JioCinema",
    "MXPlayer",
    "SonyLIV",
    "SunNXT",
    "Voot",
    "Zee5",
    "AmazonPrime",
    "Netflix",
    "NotAvailable",
]

# ============================================================
# 🎨 SAFE TELEGRAM BUTTON STYLE DETECTION
# ============================================================
BTN_PRIMARY = BTN_SECONDARY = BTN_SUCCESS = BTN_DANGER = None
_SUPPORTS_BTN_STYLE = False

try:
    from pyrogram.enums import ButtonStyle

    if "style" in inspect.signature(InlineKeyboardButton.__init__).parameters:
        BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", None)
        BTN_SECONDARY = getattr(ButtonStyle, "SECONDARY", None)
        BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", None)
        BTN_DANGER = getattr(ButtonStyle, "DANGER", None)
        _SUPPORTS_BTN_STYLE = True
except (ImportError, ValueError, TypeError):
    pass


def _normalize_style(style):
    if not _SUPPORTS_BTN_STYLE or style is None:
        return None
    if hasattr(style, "value"):
        return style
    if isinstance(style, int):
        return {
            1: BTN_PRIMARY,
            2: BTN_SECONDARY,
            3: BTN_SUCCESS,
            4: BTN_DANGER,
        }.get(style, None)
    if isinstance(style, str):
        return {
            "blue": BTN_PRIMARY,
            "primary": BTN_PRIMARY,
            "secondary": BTN_SECONDARY,
            "green": BTN_SUCCESS,
            "success": BTN_SUCCESS,
            "red": BTN_DANGER,
            "danger": BTN_DANGER,
        }.get(style.lower(), None)
    return None


def create_btn(text, url=None, callback_data=None, style=None):
    kwargs = {"text": str(text)}
    if url:
        kwargs["url"] = str(url).strip()
    elif callback_data:
        kwargs["callback_data"] = str(callback_data)[:64]
    norm_style = _normalize_style(style)
    if _SUPPORTS_BTN_STYLE and norm_style is not None:
        kwargs["style"] = norm_style
    try:
        return InlineKeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


def btn_to_dict(btn):
    if isinstance(btn, dict):
        st = btn.get("style")
        if hasattr(st, "value"):
            st = st.value
        return {
            "text": btn.get("text", ""),
            "url": btn.get("url"),
            "callback_data": btn.get("callback_data"),
            "style": st if isinstance(st, (int, str)) else None,
        }
    st = getattr(btn, "style", None)
    if hasattr(st, "value"):
        st = st.value
    return {
        "text": getattr(btn, "text", ""),
        "url": getattr(btn, "url", None),
        "callback_data": getattr(btn, "callback_data", None),
        "style": st if isinstance(st, (int, str)) else None,
    }


def dict_to_btn(data):
    if isinstance(data, InlineKeyboardButton):
        return data
    if isinstance(data, dict):
        return create_btn(
            text=data.get("text", "Button"),
            url=data.get("url"),
            callback_data=data.get("callback_data"),
            style=data.get("style"),
        )
    return create_btn(str(data), callback_data="noop")


def get_uid(msg: Message) -> int:
    if msg.from_user:
        return msg.from_user.id
    if msg.sender_chat:
        return msg.sender_chat.id
    return msg.chat.id if msg.chat else 0


def get_cb_uid(query: CallbackQuery) -> int:
    if query.from_user:
        return query.from_user.id
    if query.message:
        return get_uid(query.message)
    return 0


def get_admin_list() -> List[int]:
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        raw_admins = str(raw_admins).replace(",", " ").split()
    elif isinstance(raw_admins, (list, tuple, set)):
        raw_admins = list(raw_admins)
    else:
        raw_admins = []
    return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]


async def _get_connected_group_id(user_id: int) -> Optional[int]:
    uid_str = str(user_id)
    if callable(_std_active_connection):
        try:
            conn = await _std_active_connection(uid_str)
            if conn:
                return int(conn)
        except Exception:
            pass
    if _active_db is not None:
        try:
            doc = await _active_db.find_one({"user_id": uid_str})
            if doc and doc.get("group_id"):
                return int(doc["group_id"])
        except Exception:
            pass
    if _std_conn_db is not None:
        try:
            doc = await _std_conn_db.find_one({"_id": uid_str})
            if doc and doc.get("active_group"):
                return int(doc["active_group"])
        except Exception:
            pass
    return None


async def is_user_admin(client: Client, message: Message) -> bool:
    if not message:
        return False
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
    if (
        message.chat
        and message.chat.type == enums.ChatType.PRIVATE
        and message.from_user
    ):
        try:
            conn_gid = await _get_connected_group_id(message.from_user.id)
            if conn_gid:
                member = await client.get_chat_member(conn_gid, message.from_user.id)
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
        except Exception:
            pass
    return False


async def cb_user_admin(client: Client, query: CallbackQuery) -> bool:
    if not query:
        return False
    admin_list = get_admin_list()
    if query.from_user and query.from_user.id in admin_list:
        return True
    if (
        query.message
        and query.message.chat
        and query.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    ):
        if (
            query.message.sender_chat
            and query.message.sender_chat.id == query.message.chat.id
        ):
            return True
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
    if (
        query.message
        and query.message.chat
        and query.message.chat.type == enums.ChatType.PRIVATE
        and query.from_user
    ):
        try:
            conn_gid = await _get_connected_group_id(query.from_user.id)
            if conn_gid:
                member = await client.get_chat_member(conn_gid, query.from_user.id)
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
        except Exception:
            pass
    return False


async def _admin_filter_func(_, client: Client, message: Message) -> bool:
    return await is_user_admin(client, message)


admin_filter = filters.create(_admin_filter_func)


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
            return None, "Download failed"
        url = await asyncio.to_thread(_upload_sync, file_io.getvalue())
        return url, (None if url else "Upload failed")
    except Exception as e:
        return None, str(e)


_WAITING_REQUESTS = {}


async def _waiting_chat_check(_, __, message: Message) -> bool:
    if not message.chat or message.chat.id not in _WAITING_REQUESTS:
        return False
    if (
        message.text
        and message.text.startswith("/")
        and message.text.strip().lower() not in ["/reset", "/default", "/cancel"]
    ):
        return False
    return True


waiting_chat_filter = filters.create(_waiting_chat_check)


@Client.on_message((filters.group | filters.private) & waiting_chat_filter, group=-90)
async def custom_listener(client: Client, message: Message):
    chat_id = message.chat.id
    req_info = _WAITING_REQUESTS.get(chat_id)
    if not req_info:
        return
    sender_id = get_uid(message)
    expected_session_id = req_info["session_id"]
    if (
        sender_id == expected_session_id
        or sender_id == chat_id
        or await is_user_admin(client, message)
    ):
        future = req_info["future"]
        if not future.done():
            future.set_result(message)
        raise StopPropagation


async def native_listen(
    client: Client, chat_id: int, session_id: int, timeout: int = 180
) -> Message:
    if chat_id in _WAITING_REQUESTS:
        old_future = _WAITING_REQUESTS[chat_id].get("future")
        if old_future and not old_future.done():
            old_future.cancel()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _WAITING_REQUESTS[chat_id] = {"future": future, "session_id": session_id}
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        if _WAITING_REQUESTS.get(chat_id, {}).get("future") is future:
            _WAITING_REQUESTS.pop(chat_id, None)


async def save_session_to_db(user_id: int, session: dict):
    if drafts_db is not None:
        try:
            draft = session.copy()
            draft["buttons"] = [
                [btn_to_dict(b) for b in row] for row in session.get("buttons", [])
            ]
            await drafts_db.update_one({"_id": user_id}, {"$set": draft}, upsert=True)
        except Exception as e:
            logger.error(f"Draft Save Error: {e}")


async def get_or_load_session(user_id: int):
    if user_id in post_sessions:
        return post_sessions[user_id]
    if drafts_db is not None:
        try:
            doc = await drafts_db.find_one({"_id": user_id})
            if doc:
                doc.pop("_id", None)
                doc["buttons"] = [
                    [dict_to_btn(b) for b in row] for row in doc.get("buttons", [])
                ]
                post_sessions[user_id] = doc
                return doc
        except Exception as e:
            logger.error(f"Draft Load Error: {e}")
    return None


async def expire_post_session(client: Client, user_id: int, delay: int = 1800):
    await asyncio.sleep(delay)
    if session := post_sessions.pop(user_id, None):
        if session.get("last_preview_message_id") and session.get("original_chat_id"):
            try:
                await client.delete_messages(
                    session["original_chat_id"], session["last_preview_message_id"]
                )
            except Exception:
                pass


def smart_patch_imported_caption(session, old_val, new_val, is_title=False):
    if session.get("is_manual_caption") and session.get("caption"):
        if is_title:
            session["caption"] = re.sub(
                r"<b>(.*?)</b>", f"<b>{new_val}</b>", session["caption"], count=1
            )
        elif old_val and new_val:
            session["caption"] = session["caption"].replace(str(old_val), str(new_val))


@Client.on_message(
    filters.command(["edittitle", "edittittle"]) & admin_filter, group=-4
)
async def cmd_edit_title(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active post session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/edittitle New Title</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    new_title = message.text.split(None, 1)[1]
    old_title = session["movie_details"].get("title", "")
    session["movie_details"]["title"] = new_title
    smart_patch_imported_caption(session, old_title, new_title, is_title=True)
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message, "✅ <b>Title updated!</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command(["edityear"]) & admin_filter, group=-4)
async def cmd_edit_year(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/edityear 2024</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    new_year = message.text.split(None, 1)[1]
    old_year = session["movie_details"].get("year", "")
    session["movie_details"]["year"] = new_year
    smart_patch_imported_caption(session, old_year, new_year)
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message, "✅ <b>Year updated!</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command(["editlangs"]) & admin_filter, group=-4)
async def cmd_edit_langs(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editlangs Hindi, English</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    session["custom_languages"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",") if x.strip()
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message, "✅ <b>Languages updated!</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command(["editresolutions"]) & admin_filter, group=-4)
async def cmd_edit_res(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editresolutions 1080p, 720p</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    session["custom_resolutions"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",") if x.strip()
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message, "✅ <b>Resolutions updated!</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command(["editgenres"]) & admin_filter, group=-4)
async def cmd_edit_gen(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editgenres Action, Drama</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    session["custom_genres"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",") if x.strip()
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message, "✅ <b>Genres updated!</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command(["editotts"]) & admin_filter, group=-4)
async def cmd_edit_ott(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editotts Netflix, Prime</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    session["custom_otts"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",") if x.strip()
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message, "✅ <b>OTT Platforms updated!</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command(["editbutton"]) & admin_filter, group=-4)
async def cmd_edit_post_button(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2 or "|" not in message.text:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editbutton 1 Download Now | https://url.com</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        args = message.text.split(None, 1)[1]
        row_num_str, rest = args.split(" ", 1)
        row_idx = int(row_num_str) - 1
        btn_text, btn_url = [x.strip() for x in rest.split("|", 1)]
        if not btn_url.startswith(("http://", "https://", "tg://")):
            btn_url = "https://" + btn_url
        while len(session["buttons"]) <= row_idx:
            session["buttons"].append([])
        session["buttons"][row_idx] = [create_btn(btn_text, url=btn_url)]
        await save_session_to_db(uid, session)
        await update_post_preview(client, uid, message.chat.id, force_resend=False)
        await safe_reply_text(
            message,
            f"✅ <b>Button row {row_idx + 1} updated!</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command(["editbuttoncolour"]) & admin_filter, group=-4)
async def cmd_edit_btn_color(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 3:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editbuttoncolour 1 green</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        parts_in = message.text.strip().split()
        row_idx = int(parts_in[1]) - 1
        color_str = parts_in[2].lower()
        color_map = {"green": BTN_SUCCESS, "red": BTN_DANGER, "blue": BTN_PRIMARY}
        if color_str in color_map and len(session["buttons"]) > row_idx:
            session["buttons"][row_idx] = [
                create_btn(
                    dict_to_btn(b).text,
                    url=dict_to_btn(b).url,
                    callback_data=dict_to_btn(b).callback_data,
                    style=color_map[color_str],
                )
                for b in session["buttons"][row_idx]
            ]
            await save_session_to_db(uid, session)
            await update_post_preview(client, uid, message.chat.id, force_resend=False)
            await safe_reply_text(
                message,
                f"✅ <b>Button color changed to {color_str}!</b>",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command(["editdirect"]) & admin_filter, group=-4)
async def cmd_edit_post_direct(client: Client, message: Message):
    uid = get_uid(message)
    session = await get_or_load_session(uid)
    if not session:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active session.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editdirect Download Now 📥 | https://t.me/link</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    session["custom_direct_btn_text"] = message.text.split(None, 1)[1].strip()
    await handle_add_get_files(client, session, update_text_only=True)
    await save_session_to_db(uid, session)
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await safe_reply_text(
        message,
        "✅ <b>Direct Search button updated!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setpoststicker") & admin_filter, group=-4)
async def set_autopost_sticker(client: Client, message: Message):
    target_sticker = (
        message.reply_to_message.sticker
        if (message.reply_to_message and message.reply_to_message.sticker)
        else (message.sticker if message.sticker else None)
    )
    if not target_sticker:
        return await safe_reply_text(
            message,
            "⚠️ <b>Reply to a sticker</b> with <code>/setpoststicker</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
    await save_global_post_settings("sticker", target_sticker.file_id)
    await safe_reply_text(
        message,
        "✅ <b>Post Sticker Saved Permanently!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("rempoststicker") & admin_filter, group=-4)
async def rem_autopost_sticker(client: Client, message: Message):
    await save_global_post_settings("sticker", None)
    await safe_reply_text(
        message,
        "🗑️ <b>Auto-Post Sticker Permanently Removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


_TMDB_BEARER_FALLBACK = (
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI2ZGU3YTIyZGU1YjE5YTFjNmUyZGU5ZWEyMzE2ZmQxMCIs"
    "Im5iZiI6MTc0NTMyMjQ2Mi41MzMsInN1YiI6IjY4MDc4MWRlYzVjODAzNWZiMDhhNjExNCIsInNjb3"
    "BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.rMMJ2-PBIv8Y7ybxPIEpIlzTEXzuwrm9ruKxAUCAsbw"
)


def _search_tmdb_sync(movie_name: str, tmdb_key: str = ""):
    headers = {}
    if tmdb_key:
        url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&query={urllib.parse.quote(movie_name)}"
    else:
        url = f"https://api.themoviedb.org/3/search/multi?query={urllib.parse.quote(movie_name)}"
        headers = {
            "Authorization": f"Bearer {_TMDB_BEARER_FALLBACK}",
            "Content-Type": "application/json;charset=utf-8",
        }
    res = requests.get(url, headers=headers, timeout=8)
    if res.status_code == 200:
        return res.json().get("results", [])[:5]
    return []


@Client.on_message(filters.command("post") & filters.incoming, group=-4)
async def post_command(client: Client, message: Message):
    if not await is_user_admin(client, message):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )
    if len(message.command) == 1:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/post Vikram</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    movie_name = " ".join(message.command[1:]).strip()
    user_id = get_uid(message)
    status_msg = await safe_reply_text(
        message, "⏳ <b>Searching TMDB...</b>", parse_mode=enums.ParseMode.HTML
    )
    tmdb_key = getattr(info, "TMDB_API_KEY", "") or ""
    try:
        results = await asyncio.to_thread(_search_tmdb_sync, movie_name, tmdb_key)
        if results:
            buttons, TEMP_SEARCH[user_id] = [], []
            for r in results:
                title = r.get("title") or r.get("name")
                if not title:
                    continue
                date = r.get("release_date") or r.get("first_air_date") or ""
                year = date[:4] if date else ""
                display = f"{title} ({year})" if year else title
                TEMP_SEARCH[user_id].append(f"{title} {year}".strip())
                buttons.append(
                    [
                        create_btn(
                            display,
                            callback_data=f"p_init:{user_id}:{len(TEMP_SEARCH[user_id]) - 1}",
                        )
                    ]
                )
            if buttons and status_msg:
                buttons.append(
                    [create_btn("❌ Cancel", callback_data=f"p_cancel:{user_id}")]
                )
                return await status_msg.edit_text(
                    "🔍 <b>Select the exact movie:</b>",
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode=enums.ParseMode.HTML,
                )
    except Exception:
        pass
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass
    await start_post_session(client, message, user_id, movie_name)


@Client.on_callback_query(filters.regex(r"^p_init:|^p_cancel"), group=-4)
async def tmdb_selector_callback(client: Client, query: CallbackQuery):
    if not await cb_user_admin(client, query):
        return await query.answer("❌ Unauthorized.", show_alert=True)
    parts = query.data.split(":")
    if parts[0] == "p_cancel":
        TEMP_SEARCH.pop(
            (
                int(parts[1])
                if len(parts) > 1 and parts[1].lstrip("-").isdigit()
                else get_cb_uid(query)
            ),
            None,
        )
        await query.answer("Cancelled.")
        return await query.message.edit_text(
            "❌ <b>Cancelled.</b>", parse_mode=enums.ParseMode.HTML
        )
    session_uid, idx = (
        (int(parts[1]), int(parts[2]))
        if len(parts) == 3
        else (get_cb_uid(query), int(parts[1]))
    )
    if session_uid not in TEMP_SEARCH or len(TEMP_SEARCH[session_uid]) <= idx:
        return await query.answer(
            "⌛ Session expired. Run /post again.", show_alert=True
        )
    await query.answer("⏳ Fetching movie details...")
    selected_name = TEMP_SEARCH[session_uid][idx]
    TEMP_SEARCH.pop(session_uid, None)
    try:
        await query.message.delete()
    except Exception:
        pass
    await start_post_session(client, query.message, session_uid, selected_name)


async def start_post_session(
    client: Client, message: Message, user_id: int, movie_name: str
):
    try:
        status_msg = await safe_send_message(
            client,
            message.chat.id,
            "<i>⏳ Fetching movie details...</i>",
            parse_mode=enums.ParseMode.HTML,
        )
        movie_details = None
        try:
            movie_details = await get_movie_detailsx(movie_name)
        except Exception:
            pass
        if not movie_details or not isinstance(movie_details, dict):
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", movie_name)
            extracted_year = year_match.group(1) if year_match else ""
            clean_title = (
                movie_name.replace(extracted_year, "").strip()
                if extracted_year
                else movie_name
            )
            movie_details = {
                "title": clean_title or movie_name,
                "year": extracted_year,
                "rating": "N/A",
                "plot": "N/A",
                "poster_url": None,
                "backdrop_url": None,
            }

        post_sessions[user_id] = {
            "movie_name": movie_name,
            "caption": None,
            "is_manual_caption": False,
            "buttons": [],
            "photo_mode": False,
            "use_landscape": bool(movie_details.get("backdrop_url")),
            "custom_languages": [],
            "custom_resolutions": [],
            "custom_genres": [],
            "custom_otts": [],
            "schedule": 0,
            "auto_delete": 0,
            "last_preview_message_id": status_msg.id if status_msg else None,
            "original_message_id": (
                message.id
                if hasattr(message, "id")
                else (status_msg.id if status_msg else 0)
            ),
            "original_chat_id": message.chat.id,
            "custom_poster": None,
            "watermark": DEFAULT_WATERMARK,
            "lang_format": LANGUAGES_FORMAT,
            "ott_format": OTT_FORMAT,
            "gen_format": GENRES_FORMAT,
            "res_format": RESOLUTIONS_FORMAT,
            "active_template": "clean_grid",
            "movie_details": movie_details,
            "custom_direct_btn_text": "Direct Search 🔎",
        }
        asyncio.create_task(expire_post_session(client, user_id, 1800))
        if USE_GETFILE_BUTTON_BY_DEFAULT:
            await handle_add_get_files(client, post_sessions[user_id])
        await save_session_to_db(user_id, post_sessions[user_id])
        await update_post_preview(client, user_id, message.chat.id, force_resend=True)
    except Exception as e:
        await safe_send_message(
            client,
            message.chat.id,
            f"❌ <b>SESSION ERROR:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("editpost") & admin_filter, group=-4)
async def edit_post_cmd(client: Client, message: Message):
    try:
        if len(message.command) < 2:
            return await safe_reply_text(
                message,
                "❌ <b>Provide a post link.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        link = message.command[1].strip()
        parts = link.rstrip("/").split("/")
        chat_id = (
            int("-100" + parts[-2])
            if "t.me/c/" in link
            else ("@" + parts[-2] if not parts[-2].startswith("@") else parts[-2])
        )
        msg_id = int(parts[-1])
        status_msg = await safe_reply_text(
            message, "⏳ <b>Fetching post...</b>", parse_mode=enums.ParseMode.HTML
        )
        target_msg = await client.get_messages(chat_id, msg_id)
        poster_url, html_text, is_photo_mode = None, "", False
        if target_msg.photo:
            html_text = target_msg.caption.html if target_msg.caption else ""
            poster_url = target_msg.photo.file_id
            is_photo_mode = True
        else:
            html_text = target_msg.text.html if target_msg.text else ""
            match = re.search(
                r"<a href=['\"](https?://[^'\"]+)['\"]>&#8205;</a>", html_text
            )
            if match:
                poster_url = match.group(1)
                html_text = re.sub(
                    r"<a href=['\"](https?://[^'\"]+)['\"]>&#8205;</a>", "", html_text
                ).strip()
        title_match = re.search(r"<b>(.*?)</b>", html_text.split("\n")[0])
        search_name = title_match.group(1).strip() if title_match else "Unknown Movie"
        buttons = []
        if target_msg.reply_markup and target_msg.reply_markup.inline_keyboard:
            for row in target_msg.reply_markup.inline_keyboard:
                buttons.append(
                    [
                        create_btn(
                            text=b.text,
                            url=b.url,
                            callback_data=b.callback_data,
                            style=getattr(b, "style", None),
                        )
                        for b in row
                    ]
                )
        user_id = get_uid(message)
        post_sessions[user_id] = {
            "movie_name": search_name,
            "caption": html_text,
            "is_manual_caption": True,
            "buttons": buttons,
            "photo_mode": is_photo_mode,
            "use_landscape": False,
            "custom_languages": [],
            "custom_resolutions": [],
            "custom_genres": [],
            "custom_otts": [],
            "schedule": 0,
            "auto_delete": 0,
            "last_preview_message_id": status_msg.id if status_msg else None,
            "original_message_id": message.id,
            "original_chat_id": message.chat.id,
            "custom_poster": poster_url,
            "watermark": "",
            "lang_format": LANGUAGES_FORMAT,
            "ott_format": OTT_FORMAT,
            "gen_format": GENRES_FORMAT,
            "res_format": RESOLUTIONS_FORMAT,
            "active_template": "clean_grid",
            "movie_details": {"title": search_name, "year": ""},
            "edit_target": {"chat_id": chat_id, "message_id": msg_id},
            "custom_direct_btn_text": "Direct Search 🔎",
        }
        await save_session_to_db(user_id, post_sessions[user_id])
        asyncio.create_task(expire_post_session(client, user_id, 1800))
        await update_post_preview(client, user_id, message.chat.id, force_resend=True)
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


async def _build_final_post_content(session: dict, session_id: int):
    movie_details = session.get("movie_details", {})
    if session.get("is_manual_caption") and session.get("caption"):
        return (
            session["caption"],
            build_keyboard(session, session_id),
            (
                None
                if session.get("custom_poster") == "BLANK"
                else session.get("custom_poster")
            ),
        )
    template_str = TEMPLATES.get(
        session.get("active_template"), TEMPLATES["clean_grid"]
    )
    val_title, val_year, rating_str, plot_str = (
        html.escape(str(movie_details.get("title", ""))),
        html.escape(str(movie_details.get("year", ""))),
        html.escape(str(movie_details.get("rating", "N/A"))),
        html.escape(str(movie_details.get("plot", "N/A"))),
    )
    c_langs, c_res, c_gen, c_ott = (
        session.get("custom_languages", []),
        session.get("custom_resolutions", []),
        session.get("custom_genres", []),
        session.get("custom_otts", []),
    )
    langs_str, res_str, genres_str, otts_str = (
        ("" if c_langs == ["BLANK"] else (", ".join(c_langs) if c_langs else "N/A")),
        ("" if c_res == ["BLANK"] else (", ".join(c_res) if c_res else "N/A")),
        ("" if c_gen == ["BLANK"] else (", ".join(c_gen) if c_gen else "N/A")),
        ("" if c_ott == ["BLANK"] else (", ".join(c_ott) if c_ott else "N/A")),
    )
    val_langs = (
        session.get("lang_format", LANGUAGES_FORMAT).format_map(
            SafeDict(langs=langs_str, LANGUAGES=langs_str)
        )
        if langs_str != "N/A"
        else ""
    )
    val_res = (
        session.get("res_format", RESOLUTIONS_FORMAT).format_map(
            SafeDict(resolutions=res_str, RESOLUTIONS=res_str)
        )
        if res_str != "N/A"
        else ""
    )
    val_gens = (
        session.get("gen_format", GENRES_FORMAT).format_map(
            SafeDict(genres=genres_str, GENRES=genres_str)
        )
        if genres_str != "N/A"
        else ""
    )
    val_otts = (
        session.get("ott_format", OTT_FORMAT).format_map(
            SafeDict(otts=otts_str, OTT_PLATFORMS=otts_str)
        )
        if otts_str != "N/A"
        else ""
    )

    def render_text(plot_input):
        text = template_str.replace("{title}", val_title).replace("{year}", val_year)
        for tag, val in [
            ("{LANGUAGES}", val_langs),
            ("{RESOLUTIONS}", val_res),
            ("{GENRES}", val_gens),
            ("{OTT_PLATFORMS}", val_otts),
            ("{langs}", val_langs),
            ("{resolutions}", val_res),
            ("{genres}", val_gens),
            ("{otts}", val_otts),
        ]:
            text = (
                text.replace(tag, val)
                if val
                else re.sub(rf"[^\n]*{re.escape(tag)}[^\n]*\n?", "", text)
            )
        text = (
            text.replace("{size}", "Available In Files.")
            .replace("{rating}", rating_str)
            .replace("{plot}", plot_input)
        )
        if session.get("watermark"):
            text += f"\n\n{session['watermark']}"
        return text

    final_text = render_text(plot_str)
    if session.get("photo_mode") and len(final_text) > 1024:
        excess = len(final_text) - 1000
        final_text = (
            render_text(plot_str[: -(excess + 5)] + "...")
            if len(plot_str) > excess + 10
            else final_text[:1024]
        )
    poster_to_use = (
        None
        if session.get("custom_poster") == "BLANK"
        else (
            session.get("custom_poster")
            or (
                movie_details.get("backdrop_url")
                if session.get("use_landscape")
                else movie_details.get("poster_url")
            )
        )
    )
    return final_text, build_keyboard(session, session_id), poster_to_use


async def update_post_preview(
    client: Client, session_id: int, chat_id: int, force_resend: bool = False
):
    session = await get_or_load_session(session_id)
    if not session:
        return
    try:
        final_caption, keyboard, poster_to_use = await _build_final_post_content(
            session, session_id
        )
    except Exception as e:
        return logger.error(f"Build preview error: {e}")
    final_caption = final_caption + ("\u200b" * random.randint(1, 3))
    is_normal_photo = (
        session.get("photo_mode")
        and poster_to_use
        and str(poster_to_use).upper() != "BLANK"
    )
    old_msg_id = session.get("last_preview_message_id")
    try:
        if is_normal_photo:
            if force_resend or not old_msg_id:
                sent_msg = await client.send_photo(
                    chat_id,
                    photo=poster_to_use,
                    caption=final_caption,
                    reply_markup=keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
                session["last_preview_message_id"] = sent_msg.id
                if old_msg_id:
                    try:
                        await client.delete_messages(chat_id, old_msg_id)
                    except Exception:
                        pass
            else:
                try:
                    await client.edit_message_caption(
                        chat_id,
                        old_msg_id,
                        caption=final_caption,
                        reply_markup=keyboard,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except MessageNotModified:
                    pass
                except Exception:
                    sent_msg = await client.send_photo(
                        chat_id,
                        photo=poster_to_use,
                        caption=final_caption,
                        reply_markup=keyboard,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    session["last_preview_message_id"] = sent_msg.id
                    try:
                        await client.delete_messages(chat_id, old_msg_id)
                    except Exception:
                        pass
        else:
            text_content = (
                f"{final_caption}\n<a href='{poster_to_use}'>&#8205;</a>"
                if poster_to_use and str(poster_to_use).upper() != "BLANK"
                else final_caption
            )
            if force_resend or not old_msg_id:
                sent_msg = await safe_send_message(
                    client,
                    chat_id,
                    text=text_content,
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                    parse_mode=enums.ParseMode.HTML,
                )
                if sent_msg:
                    session["last_preview_message_id"] = sent_msg.id
                if old_msg_id:
                    try:
                        await client.delete_messages(chat_id, old_msg_id)
                    except Exception:
                        pass
            else:
                try:
                    await safe_edit_message_text(
                        client,
                        chat_id,
                        old_msg_id,
                        text=text_content,
                        reply_markup=keyboard,
                        disable_web_page_preview=False,
                        parse_mode=enums.ParseMode.HTML,
                    )
                except MessageNotModified:
                    pass
                except Exception:
                    sent_msg = await safe_send_message(
                        client,
                        chat_id,
                        text=text_content,
                        reply_markup=keyboard,
                        disable_web_page_preview=False,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    if sent_msg:
                        session["last_preview_message_id"] = sent_msg.id
                    try:
                        await client.delete_messages(chat_id, old_msg_id)
                    except Exception:
                        pass
        await save_session_to_db(session_id, session)
    except Exception as e:
        logger.error(f"Update preview failed: {e}")


def build_keyboard(session: dict, session_id: int):
    rows = [[dict_to_btn(b) for b in row] for row in session.get("buttons", [])]
    if session_id == -1:
        return InlineKeyboardMarkup(rows) if rows else None
    mode_btn = []
    if not session.get("edit_target"):
        mode_btn.append(
            create_btn(
                f"Mode: {'Normal Img' if session.get('photo_mode') else 'Preview Img'}",
                callback_data=f"post:toggle_mode:{session_id}",
            )
        )
    mode_btn.append(
        create_btn(
            f"Poster: {'Landscape' if session.get('use_landscape') else 'Portrait'}",
            callback_data=f"post:toggle_poster:{session_id}",
        )
    )
    sched_val, del_val = session.get("schedule", 0), session.get("auto_delete", 0)
    timing_row = [
        create_btn(
            f"⏰ Sched: {sched_val}s" if sched_val else "⏰ Sched: OFF",
            callback_data=f"post:set_schedule:{session_id}",
        ),
        create_btn(
            f"🗑️ Auto-Del: {del_val}s" if del_val else "🗑️ Auto-Del: OFF",
            callback_data=f"post:set_autodelete:{session_id}",
        ),
    ]
    rows.extend(
        [
            [
                create_btn(
                    "✏️ Buttons", callback_data=f"post:buttons_menu:{session_id}"
                ),
                create_btn(
                    "✏️ Caption", callback_data=f"post:edit_caption:{session_id}"
                ),
            ],
            [
                create_btn(
                    "🔗 editdirect", callback_data=f"post:edit_direct:{session_id}"
                ),
                create_btn(
                    "⚪ editbuttoncolour",
                    callback_data=f"post:edit_buttoncolour:{session_id}",
                ),
            ],
            [
                create_btn("🖼️ Poster", callback_data=f"post:set_poster:{session_id}"),
                create_btn(
                    "✨ Templates", callback_data=f"post:templates:{session_id}"
                ),
                create_btn(
                    "💧 Watermark", callback_data=f"post:set_watermark:{session_id}"
                ),
            ],
            [
                create_btn(
                    "🀄 setpoststicker", callback_data=f"post:set_sticker:{session_id}"
                ),
                create_btn(
                    "❌ rempoststicker", callback_data=f"post:rem_sticker:{session_id}"
                ),
            ],
            [
                create_btn("🔊", callback_data=f"post:languages:{session_id}"),
                create_btn("🖥️", callback_data=f"post:resolutions:{session_id}"),
                create_btn("🎥", callback_data=f"post:genres:{session_id}"),
                create_btn("📺", callback_data=f"post:otts:{session_id}"),
            ],
            timing_row,
            mode_btn,
            [
                create_btn(
                    "✅ Post",
                    callback_data=f"post:finalize:{session_id}",
                    style=BTN_SUCCESS,
                ),
                create_btn(
                    "❌ Cancel",
                    callback_data=f"post:cancel:{session_id}",
                    style=BTN_DANGER,
                ),
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


_TITLE_CACHE = {}


async def get_cached_chat_title(client: Client, chat_id):
    chat_id = int(chat_id)
    if chat_id in _TITLE_CACHE:
        return _TITLE_CACHE[chat_id]
    try:
        title = (await client.get_chat(chat_id)).title
        _TITLE_CACHE[chat_id] = title
        return title
    except Exception:
        return str(chat_id)


async def show_target_selection(query: CallbackQuery, session_id: int, page: int = 1):
    all_chats_dict, db_titles, settings = {}, {}, await get_global_post_settings()
    db_chats = []
    if _db is not None and hasattr(_db, "get_all_chats"):
        try:
            raw_chats = await _db.get_all_chats()
            db_chats = (
                [c async for c in raw_chats]
                if hasattr(raw_chats, "__aiter__")
                else list(raw_chats)
            )
        except Exception:
            db_chats = []
    for chat in db_chats:
        chat_id = chat.get("id") or chat.get("chat_id")
        if chat_id:
            db_titles[int(chat_id)] = (
                chat.get("title") or chat.get("name") or str(chat_id)
            )

    def _safe_parse(var):
        if isinstance(var, str):
            return [
                int(x)
                for x in var.replace(",", " ").split()
                if x.strip().lstrip("-").isdigit()
            ]
        if isinstance(var, (list, tuple, set)):
            return [int(x) for x in var if str(x).strip().lstrip("-").isdigit()]
        if isinstance(var, int):
            return [var]
        return []

    info_muc, db_muc = _safe_parse(
        getattr(info, "MOVIE_UPDATE_CHANNEL", [])
    ), settings.get("muc_list", [])
    for ch in set(info_muc + db_muc):
        if ch:
            all_chats_dict[int(ch)] = (
                f"🌟 {await get_cached_chat_title(query._client, ch)}"
            )
    info_apc, db_apc = _safe_parse(getattr(info, "AUTOPOSTCHANNEL", [])), settings.get(
        "apc_list", []
    )
    for ch in set(info_apc + db_apc):
        if ch and int(ch) not in all_chats_dict:
            all_chats_dict[int(ch)] = (
                f"🌟 {await get_cached_chat_title(query._client, ch)}"
            )
    for chat in db_chats:
        chat_id = chat.get("id") or chat.get("chat_id")
        if chat_id and int(chat_id) not in all_chats_dict:
            all_chats_dict[int(chat_id)] = (
                f"📢 {db_titles.get(int(chat_id), str(chat_id))[:20]}"
            )
    chat_list = list(all_chats_dict.items())
    page_size, total_pages = 10, max(1, math.ceil(len(chat_list) / 10))
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * page_size
    buttons = [
        [
            create_btn(
                "📢 Post to All Configured Channels",
                callback_data=f"post:send_all:{session_id}",
                style=BTN_SUCCESS,
            )
        ]
    ]
    for chat_id, title in chat_list[start_idx : start_idx + page_size]:
        buttons.append(
            [
                create_btn(
                    title,
                    callback_data=f"post:send_to:{session_id}:{chat_id}",
                    style=BTN_PRIMARY,
                )
            ]
        )
    nav_row = []
    if page > 1:
        nav_row.append(
            create_btn(
                "⬅️ Prev",
                callback_data=f"post:target_page:{session_id}:{page - 1}",
                style=BTN_SECONDARY,
            )
        )
    if page < total_pages:
        nav_row.append(
            create_btn(
                "Next ➡️",
                callback_data=f"post:target_page:{session_id}:{page + 1}",
                style=BTN_SECONDARY,
            )
        )
    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            create_btn(
                "🔙 Back to Editor",
                callback_data=f"post:back_editor:{session_id}",
                style=BTN_DANGER,
            )
        ]
    )
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^(post:|noop$)"), group=-4)
async def post_callbacks(client: Client, query: CallbackQuery):
    if query.data == "noop":
        return await query.answer()
    if not await cb_user_admin(client, query):
        return await query.answer("❌ Unauthorized: Admin only.", show_alert=True)
    try:
        parts = query.data.split(":")
        action, session_id = parts[1], int(parts[2])
        extra_data = parts[3:]
        session = await get_or_load_session(session_id)
        if not session:
            return await query.answer(
                "⌛ Session expired. Run /post again.", show_alert=True
            )
        force_resend = False
        if action == "back":
            await query.answer()
        elif action in [
            "languages",
            "resolutions",
            "templates",
            "buttons_menu",
            "remove_buttons_menu",
            "genres",
            "otts",
        ]:
            await query.answer()
            if action == "languages":
                await show_selection_menu(query, session_id, "languages")
            elif action == "resolutions":
                await show_selection_menu(query, session_id, "resolutions")
            elif action == "genres":
                await show_selection_menu(query, session_id, "genres")
            elif action == "otts":
                await show_selection_menu(query, session_id, "otts")
            elif action == "templates":
                await handle_templates_menu(query, session_id)
            elif action == "buttons_menu":
                await handle_buttons_menu(query, session_id)
            elif action == "remove_buttons_menu":
                await handle_remove_buttons_menu(query, session_id)
            return
        elif action in ["select_lang", "select_res", "select_gen", "select_ott"]:
            await query.answer()
            item = extra_data[0]
            arr_map = {
                "select_lang": "custom_languages",
                "select_res": "custom_resolutions",
                "select_gen": "custom_genres",
                "select_ott": "custom_otts",
            }
            k = arr_map[action]
            if "BLANK" in session[k]:
                session[k] = []
            if item not in session[k]:
                session[k].append(item)
            else:
                session[k].remove(item)
            await save_session_to_db(session_id, session)
            if action == "select_lang":
                await show_selection_menu(query, session_id, "languages")
            elif action == "select_res":
                await show_selection_menu(query, session_id, "resolutions")
            elif action == "select_gen":
                await show_selection_menu(query, session_id, "genres")
            elif action == "select_ott":
                await show_selection_menu(query, session_id, "otts")
            return
        elif action == "format_lang":
            return await format_updater(
                client, query, session_id, "lang_format", "{langs}", LANGUAGES_FORMAT
            )
        elif action == "format_res":
            return await format_updater(
                client,
                query,
                session_id,
                "res_format",
                "{resolutions}",
                RESOLUTIONS_FORMAT,
            )
        elif action == "format_gen":
            return await format_updater(
                client, query, session_id, "gen_format", "{genres}", GENRES_FORMAT
            )
        elif action == "format_ott":
            return await format_updater(
                client, query, session_id, "ott_format", "{otts}", OTT_FORMAT
            )
        elif action == "select_template":
            await query.answer("✅ Template selected!")
            session["active_template"] = extra_data[0]
            session["is_manual_caption"], session["caption"] = False, None
        elif action == "edit_caption":
            response = await get_user_input(
                client,
                query,
                session_id,
                "✏️ Send the new caption text (HTML supported):",
            )
            if response and response.text:
                session["caption"] = (
                    response.text.html
                    if hasattr(response.text, "html")
                    else response.text
                )
                session["is_manual_caption"] = True
        elif action == "set_poster":
            response = await get_user_input(
                client,
                query,
                session_id,
                "📸 Send a photo or an image URL.\n<i>(Or type <code>/reset</code> or <code>blank</code>)</i>",
            )
            if response:
                if response.photo:
                    status_msg = await safe_reply_text(
                        query.message,
                        "⏳ <b>Uploading image...</b>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                    url, _ = await upload_image_safely(client, response)
                    if url:
                        session["custom_poster"] = url
                        if not session.get("edit_target"):
                            session["photo_mode"] = False
                        if status_msg:
                            try:
                                await status_msg.edit_text(
                                    "✅ <b>Poster updated successfully!</b>",
                                    parse_mode=enums.ParseMode.HTML,
                                )
                            except Exception:
                                pass
                    else:
                        session["custom_poster"] = response.photo.file_id
                        session["photo_mode"] = True
                        if status_msg:
                            try:
                                await status_msg.edit_text(
                                    "⚠️ Cloud blocked. Switched to 'Normal Image' Mode!",
                                    parse_mode=enums.ParseMode.HTML,
                                )
                            except Exception:
                                pass
                elif response.text:
                    text_input = response.text.strip().lower()
                    if text_input == "/reset":
                        session["custom_poster"] = None
                        if not session.get("edit_target"):
                            session["photo_mode"] = False
                    elif text_input == "blank":
                        session["custom_poster"] = "BLANK"
                    elif response.text.startswith("http"):
                        session["custom_poster"] = response.text.strip()
                        if not session.get("edit_target"):
                            session["photo_mode"] = False
            force_resend = True
        elif action == "set_watermark":
            response = await get_user_input(
                client,
                query,
                session_id,
                "💧 Send watermark. Send <code>blank</code> or <code>/reset</code> to remove. Send <code>/default</code> for default.",
            )
            if response and response.text:
                text_input = response.text.strip().lower()
                if text_input in ["/reset", "blank"]:
                    session["watermark"] = ""
                elif text_input == "/default":
                    session["watermark"] = DEFAULT_WATERMARK
                else:
                    session["watermark"] = (
                        response.text.html
                        if hasattr(response.text, "html")
                        else response.text
                    )
        elif action == "edit_buttons":
            response = await get_user_input(
                client,
                query,
                session_id,
                "➕ Send the button layout. Format:\n<code>Button 1 - URL1 | Button 2 - URL2</code>",
            )
            if response and response.text:
                new_layout = []
                for row_str in response.text.strip().split("\n"):
                    row_btns = []
                    for btn_str in row_str.split("|"):
                        if " - " in btn_str:
                            text, url = btn_str.split(" - ", 1)
                            clean_url = (
                                url.strip()
                                if url.strip().startswith(
                                    ("http://", "https://", "tg://")
                                )
                                else "https://" + url.strip()
                            )
                            clean_text = (
                                text.replace("[", "")
                                .replace("]", "")
                                .replace("(", "")
                                .replace(")", "")
                                .strip()
                            )
                            row_btns.append(create_btn(clean_text, url=clean_url))
                    if row_btns:
                        new_layout.append(row_btns)
                session["buttons"] = new_layout
                await save_session_to_db(session_id, session)
        elif action == "remove_button":
            await query.answer("🗑️ Button removed!")
            try:
                row_i, col_i = int(extra_data[0]), int(extra_data[1])
                session["buttons"][row_i].pop(col_i)
                if not session["buttons"][row_i]:
                    session["buttons"].pop(row_i)
                await save_session_to_db(session_id, session)
            except (IndexError, ValueError):
                pass
            return await handle_remove_buttons_menu(query, session_id)
        elif action == "edit_direct":
            response = await get_user_input(
                client,
                query,
                session_id,
                "🔗 Send the new Direct Search button text and link separated by <code>|</code>\nExample: <code>Download Now 📥 | https://t.me/link</code>",
            )
            if response and response.text:
                session["custom_direct_btn_text"] = response.text.strip()
                await handle_add_get_files(client, session, update_text_only=True)
            force_resend = True
        elif action == "edit_buttoncolour":
            response = await get_user_input(
                client,
                query,
                session_id,
                "🎨 Send row number and color (e.g., <code>1 green</code>). Colors: green, red, blue.",
            )
            if response and response.text:
                try:
                    parts_in = response.text.strip().split()
                    row_idx, color_str = int(parts_in[0]) - 1, parts_in[1].lower()
                    color_map = {
                        "green": BTN_SUCCESS,
                        "red": BTN_DANGER,
                        "blue": BTN_PRIMARY,
                    }
                    if color_str in color_map and len(session["buttons"]) > row_idx:
                        session["buttons"][row_idx] = [
                            create_btn(
                                dict_to_btn(b).text,
                                url=dict_to_btn(b).url,
                                callback_data=dict_to_btn(b).callback_data,
                                style=color_map[color_str],
                            )
                            for b in session["buttons"][row_idx]
                        ]
                except Exception:
                    pass
            force_resend = True
        elif action == "set_sticker":
            response = await get_user_input(
                client,
                query,
                session_id,
                "🀄 Send a sticker to set as the auto-post sticker:",
            )
            if response and response.sticker:
                await save_global_post_settings("sticker", response.sticker.file_id)
                await safe_reply_text(
                    query.message,
                    "✅ <b>Auto-Post Sticker Saved Permanently!</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
        elif action == "rem_sticker":
            await save_global_post_settings("sticker", None)
            try:
                await query.answer("🗑️ Auto-Post Sticker Removed.", show_alert=True)
            except Exception:
                pass
        elif action == "add_get_files":
            added = await handle_add_get_files(client, session)
            await query.answer(
                (
                    "✅ 'Get Files' button added!"
                    if added
                    else "⚠️ Button already exists!"
                ),
                show_alert=not added,
            )
        elif action == "set_schedule":
            response = await get_user_input(
                client,
                query,
                session_id,
                "⏰ Send schedule delay in <b>seconds</b> (e.g., <code>3600</code>). Or type <code>off</code>.",
            )
            if response and response.text:
                text_val = response.text.strip().lower()
                session["schedule"] = (
                    0
                    if text_val == "off"
                    else (int(text_val) if text_val.isdigit() else session["schedule"])
                )
                await save_session_to_db(session_id, session)
        elif action == "set_autodelete":
            response = await get_user_input(
                client,
                query,
                session_id,
                "🗑️ Send auto-delete time in <b>seconds</b> (e.g., <code>86400</code>). Or type <code>off</code>.",
            )
            if response and response.text:
                text_val = response.text.strip().lower()
                session["auto_delete"] = (
                    0
                    if text_val == "off"
                    else (
                        int(text_val) if text_val.isdigit() else session["auto_delete"]
                    )
                )
                await save_session_to_db(session_id, session)
        elif action == "toggle_poster":
            await query.answer()
            session["use_landscape"] = not session.get("use_landscape", False)
            force_resend = True
        elif action == "toggle_mode":
            if not session.get("edit_target"):
                await query.answer()
                session["photo_mode"] = not session.get("photo_mode", False)
            else:
                return await query.answer(
                    "Cannot switch image modes on imported post!", show_alert=True
                )
            force_resend = True
        elif action == "cancel":
            await query.answer("❌ Cancelled.")
            if session := post_sessions.pop(session_id, None):
                if session.get("last_preview_message_id"):
                    try:
                        await client.delete_messages(
                            query.message.chat.id, session["last_preview_message_id"]
                        )
                    except Exception:
                        pass
            if drafts_db is not None:
                await drafts_db.delete_one({"_id": session_id})
            await safe_reply_text(
                query.message,
                "❌ <b>Post creation cancelled.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            return
        elif action == "finalize":
            await query.answer()
            if session.get("edit_target"):
                return await execute_post(
                    client, query, session_id, session["edit_target"]["chat_id"]
                )
            else:
                return await show_target_selection(query, session_id, 1)
        elif action == "target_page":
            await query.answer()
            return await show_target_selection(query, session_id, int(extra_data[0]))
        elif action == "send_to":
            await query.answer("📤 Posting...")
            return await execute_post(client, query, session_id, int(extra_data[0]))
        elif action == "send_all":
            await query.answer("📢 Broadcasting...")
            return await execute_post_all(client, query, session_id)
        elif action == "back_editor":
            await query.answer()
            return await update_post_preview(
                client, session_id, query.message.chat.id, force_resend=False
            )

        await save_session_to_db(session_id, session)
        await update_post_preview(
            client, session_id, query.message.chat.id, force_resend
        )
    except QueryIdInvalid:
        pass
    except Exception as e:
        await safe_reply_text(
            query.message,
            f"❌ <b>CALLBACK ERROR:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


async def show_selection_menu(query: CallbackQuery, session_id: int, menu_type: str):
    session = post_sessions[session_id]
    if menu_type == "languages":
        items, selected, act, fmt = (
            LANGUAGES,
            session["custom_languages"],
            "select_lang",
            "format_lang",
        )
    elif menu_type == "resolutions":
        items, selected, act, fmt = (
            RESOLUTIONS,
            session["custom_resolutions"],
            "select_res",
            "format_res",
        )
    elif menu_type == "genres":
        items, selected, act, fmt = (
            GENRES,
            session["custom_genres"],
            "select_gen",
            "format_gen",
        )
    elif menu_type == "otts":
        items, selected, act, fmt = (
            OTT_PLATFORMS,
            session["custom_otts"],
            "select_ott",
            "format_ott",
        )
    else:
        return
    buttons = [
        create_btn(
            f"✅ {i}" if i in selected else i,
            callback_data=f"post:{act}:{session_id}:{i}",
        )
        for i in items
    ]
    keyboard = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    keyboard.append(
        [create_btn("⚙ Change Format", callback_data=f"post:{fmt}:{session_id}")]
    )
    keyboard.append([create_btn("✅ Done", callback_data=f"post:back:{session_id}")])
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard))
    except MessageNotModified:
        pass


async def handle_templates_menu(query, session_id: int):
    session = post_sessions[session_id]
    buttons = [
        [
            create_btn(
                f"✅ {name}" if session.get("active_template") == name else name,
                callback_data=f"post:select_template:{session_id}:{name}",
            )
        ]
        for name in TEMPLATES
    ]
    buttons.append([create_btn("Back", callback_data=f"post:back:{session_id}")])
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


async def handle_buttons_menu(query, session_id):
    buttons = [
        [
            create_btn(
                "➕ Add/Edit Layout", callback_data=f"post:edit_buttons:{session_id}"
            )
        ],
        [
            create_btn(
                "📥 Add 'Get Files' Button",
                callback_data=f"post:add_get_files:{session_id}",
            )
        ],
        [
            create_btn(
                "🗑️ Remove a Button",
                callback_data=f"post:remove_buttons_menu:{session_id}",
            )
        ],
        [create_btn("Back", callback_data=f"post:back:{session_id}")],
    ]
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


async def handle_remove_buttons_menu(query, session_id: int):
    session = post_sessions[session_id]
    buttons = []
    for i, row in enumerate(session["buttons"]):
        for j, btn in enumerate(row):
            b_obj = dict_to_btn(btn)
            buttons.append(
                [
                    create_btn(
                        f"❌ {b_obj.text}",
                        callback_data=f"post:remove_button:{session_id}:{i}:{j}",
                    )
                ]
            )
    if not buttons:
        buttons.append([create_btn("No buttons to remove", callback_data="noop")])
    buttons.append([create_btn("Back", callback_data=f"post:back:{session_id}")])
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


async def get_user_input(
    client, query: CallbackQuery, session_id: int, prompt_text: str
):
    try:
        await query.answer()
    except Exception:
        pass
    session = post_sessions[session_id]
    prompt_full = f"{prompt_text}\n\n<i>(Send <code>/cancel</code> to abort)</i>"
    ask_msg = await safe_reply_text(
        query.message,
        prompt_full,
        reply_to_message_id=session.get("original_message_id"),
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        response = await native_listen(client, query.message.chat.id, session_id, 120)
        if ask_msg:
            try:
                await ask_msg.delete()
            except Exception:
                pass
        if response:
            try:
                await response.delete()
            except Exception:
                pass
            if response.text and response.text.strip().lower() == "/cancel":
                return None
            return response
    except asyncio.TimeoutError:
        if ask_msg:
            try:
                await ask_msg.delete()
            except Exception:
                pass
    return None


async def format_updater(
    client: Client,
    query: CallbackQuery,
    session_id: int,
    format_key: str,
    tag: str,
    default_format: str,
):
    response = await get_user_input(
        client,
        query,
        session_id,
        f"⚙️ Send format. Must include <code>{tag}</code>. Send <code>/reset</code> for default.",
    )
    session = post_sessions[session_id]
    if response and response.text:
        if response.text.strip().lower() == "/reset":
            session[format_key] = default_format
        elif tag not in response.text:
            await safe_reply_text(
                query.message,
                f"⚠️ Must contain <code>{tag}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            session[format_key] = response.text
    await save_session_to_db(session_id, session)
    return await update_post_preview(
        client, session_id, query.message.chat.id, force_resend=False
    )


async def handle_add_get_files(
    client: Client, session: dict, update_text_only=False
) -> bool:
    movie_details = session["movie_details"]
    title = (
        str(movie_details.get("title", "movie"))
        .replace("(", "")
        .replace(")", "")
        .replace("[", "")
        .replace("]", "")
    )
    year = (
        str(movie_details.get("year", ""))
        .replace("(", "")
        .replace(")", "")
        .replace("[", "")
        .replace("]", "")
    )
    btn_data = session.get("custom_direct_btn_text", "Direct Search 🔎")
    if "|" in btn_data:
        btn_text, btn_url = [x.strip() for x in btn_data.split("|", 1)]
        if not btn_url.startswith(("http://", "https://", "tg://")):
            btn_url = "https://" + btn_url
    else:
        btn_text = btn_data
        bot_username = temp.U_NAME or (await client.get_me()).username
        safe_query = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{title} {year}".strip()).strip(
            "_"
        )[:50]
        btn_url = f"https://t.me/{bot_username}?start=search_{safe_query}"
    session["buttons"] = [
        [dict_to_btn(b) for b in row] for row in session.get("buttons", [])
    ]
    if update_text_only:
        updated = False
        for r_idx, row in enumerate(session["buttons"]):
            for c_idx, btn in enumerate(row):
                if btn.url and (
                    "?start=search_" in btn.url
                    or "Direct Search" in btn.text
                    or r_idx == len(session["buttons"]) - 1
                ):
                    session["buttons"][r_idx][c_idx] = create_btn(
                        text=btn_text, url=btn_url, style=BTN_SUCCESS
                    )
                    updated = True
        if not updated:
            session["buttons"].append(
                [create_btn(text=btn_text, url=btn_url, style=BTN_SUCCESS)]
            )
        return True
    for row in session["buttons"]:
        for btn in row:
            if btn.url == btn_url:
                return False
    session["buttons"].append(
        [
            create_btn(
                text="Group 1 🎬",
                url="https://t.me/Sandalwood_Kannada_Group",
                style=BTN_PRIMARY,
            ),
            create_btn(
                text="Group 2 🎬",
                url="https://t.me/+GLsPkRgLGGszMzY1",
                style=BTN_PRIMARY,
            ),
        ]
    )
    session["buttons"].append(
        [create_btn(text=btn_text, url=btn_url, style=BTN_SUCCESS)]
    )
    return True


async def delete_after_delay(client: Client, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def delayed_post_worker(
    client: Client, session: dict, target_chats: list, edit_target: dict = None
):
    delay = session.get("schedule", 0)
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        final_caption, keyboard, poster_to_use = await _build_final_post_content(
            session, -1
        )
    except Exception as e:
        return logger.error(f"Post Worker Error: {e}")
    is_normal_photo = (
        session.get("photo_mode")
        and poster_to_use
        and str(poster_to_use).upper() != "BLANK"
    )
    g_settings = await get_global_post_settings()
    sticker_id = g_settings.get("sticker")
    for chat_id in target_chats:
        try:
            if edit_target:
                if is_normal_photo:
                    await client.edit_message_caption(
                        edit_target["chat_id"],
                        edit_target["message_id"],
                        caption=final_caption,
                        reply_markup=keyboard,
                        parse_mode=enums.ParseMode.HTML,
                    )
                else:
                    text_content = (
                        f"{final_caption}\n<a href='{poster_to_use}'>&#8205;</a>"
                        if poster_to_use and str(poster_to_use).upper() != "BLANK"
                        else final_caption
                    )
                    await safe_edit_message_text(
                        client,
                        edit_target["chat_id"],
                        edit_target["message_id"],
                        text=text_content,
                        reply_markup=keyboard,
                        disable_web_page_preview=False,
                        parse_mode=enums.ParseMode.HTML,
                    )
                continue
            if is_normal_photo:
                sent_msg = await client.send_photo(
                    chat_id,
                    photo=poster_to_use,
                    caption=final_caption,
                    reply_markup=keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                text_content = (
                    f"{final_caption}\n<a href='{poster_to_use}'>&#8205;</a>"
                    if poster_to_use and str(poster_to_use).upper() != "BLANK"
                    else final_caption
                )
                sent_msg = await safe_send_message(
                    client,
                    chat_id,
                    text=text_content,
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                    parse_mode=enums.ParseMode.HTML,
                )
            if sticker_id:
                try:
                    await client.send_sticker(chat_id, sticker=sticker_id)
                except Exception:
                    pass
            auto_del = session.get("auto_delete", 0)
            if auto_del > 0 and sent_msg:
                asyncio.create_task(
                    delete_after_delay(client, chat_id, sent_msg.id, auto_del)
                )
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as e:
            logger.error(f"Failed to post in {chat_id}: {e}")


async def execute_post(
    client: Client, query: CallbackQuery, session_id: int, target_chat_id: int
):
    session = post_sessions.pop(session_id, None) or await get_or_load_session(
        session_id
    )
    post_sessions.pop(session_id, None)
    if drafts_db is not None:
        await drafts_db.delete_one({"_id": session_id})
    if not session:
        return
    try:
        await client.delete_messages(
            query.message.chat.id, session["last_preview_message_id"]
        )
    except Exception:
        pass
    delay = session.get("schedule", 0)
    if delay > 0:
        await safe_reply_text(
            query.message,
            f"✅ <b>Post Scheduled!</b> It will execute after <code>{delay}</code> seconds.",
            parse_mode=enums.ParseMode.HTML,
        )
        asyncio.create_task(
            delayed_post_worker(
                client, session, [target_chat_id], session.get("edit_target")
            )
        )
    else:
        status_msg = (
            await safe_reply_text(
                query.message,
                "<i>Finalizing and posting...</i>",
                parse_mode=enums.ParseMode.HTML,
            )
            if query.message
            else None
        )
        await delayed_post_worker(
            client, session, [target_chat_id], session.get("edit_target")
        )
        if status_msg:
            try:
                await status_msg.edit_text(
                    "✅ <b>Post completed successfully!</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass


async def execute_post_all(client: Client, query: CallbackQuery, session_id: int):
    session = post_sessions.pop(session_id, None) or await get_or_load_session(
        session_id
    )
    post_sessions.pop(session_id, None)
    if drafts_db is not None:
        await drafts_db.delete_one({"_id": session_id})
    if not session:
        return
    try:
        await client.delete_messages(
            query.message.chat.id, session["last_preview_message_id"]
        )
    except Exception:
        pass
    settings = await get_global_post_settings()

    def _safe_parse(var):
        if isinstance(var, str):
            return [
                int(x)
                for x in var.replace(",", " ").split()
                if x.strip().lstrip("-").isdigit()
            ]
        if isinstance(var, (list, tuple, set)):
            return [int(x) for x in var if str(x).strip().lstrip("-").isdigit()]
        if isinstance(var, int):
            return [var]
        return []

    info_muc, db_muc = _safe_parse(
        getattr(info, "MOVIE_UPDATE_CHANNEL", [])
    ), settings.get("muc_list", [])
    info_apc, db_apc = _safe_parse(getattr(info, "AUTOPOSTCHANNEL", [])), settings.get(
        "apc_list", []
    )
    all_targets = list(set(info_muc + db_muc + info_apc + db_apc))
    if not all_targets:
        return await safe_reply_text(
            query.message,
            "❌ <b>No channels configured.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    delay = session.get("schedule", 0)
    if delay > 0:
        await safe_reply_text(
            query.message,
            f"✅ <b>Broadcast Scheduled!</b> Will post to <code>{len(all_targets)}</code> channels after <code>{delay}</code> seconds.",
            parse_mode=enums.ParseMode.HTML,
        )
        asyncio.create_task(delayed_post_worker(client, session, all_targets))
    else:
        status_msg = (
            await safe_reply_text(
                query.message,
                f"<i>📢 Broadcasting to {len(all_targets)} channels...</i>",
                parse_mode=enums.ParseMode.HTML,
            )
            if query.message
            else None
        )
        await delayed_post_worker(client, session, all_targets)
        if status_msg:
            try:
                await status_msg.edit_text(
                    "✅ <b>Broadcast Complete!</b>", parse_mode=enums.ParseMode.HTML
                )
            except Exception:
                pass
