import asyncio
import inspect
import time
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    MessageEmpty,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    QueryIdInvalid,
    UserIsBlocked,
    UserNotParticipant,
)
from pyrogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from database.ia_filterdb import get_file_details
from utils import get_size, parse_text_and_markup

# Safe optional imports for multi-database health helpers
try:
    from database.ia_filterdb import (
        get_all_healthy_read_dbs,
        get_writable_System_db,
        mark_db_error,
    )
except ImportError:
    get_all_healthy_read_dbs = None
    get_writable_System_db = None
    mark_db_error = None

logger = getLogger(__name__)
logger.setLevel(ERROR)

# Strong reference set so sleeping auto-delete tasks are never garbage-collected
AUTO_DELETE_TASKS = set()

DEFAULT_FSUB_CAPTION = "<b>⚠️ Please join the channels below to get your file!</b>"

# Safe optional imports
try:
    from database.join_reqs import join_reqs as _join_reqs_db
except Exception:
    _join_reqs_db = None

try:
    from database.plugin_dbs import plugin_db as _plugin_db
except Exception:
    _plugin_db = None

try:
    from plugins.custom_settings import get_bot_settings
except Exception:

    async def get_bot_settings():
        return {}


try:
    from plugins.uibuttons import (
        get_default_file_keyboard as _get_default_file_keyboard,
    )
    from plugins.uibuttons import (
        get_deleted_file_notify_keyboard as _get_deleted_file_notify_keyboard,
    )
except Exception:
    _get_default_file_keyboard = None
    _get_deleted_file_notify_keyboard = None


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
_REPLY_PARAMS = set(inspect.signature(Message.reply_text).parameters.keys())
_EDIT_PARAMS = set(inspect.signature(Message.edit_text).parameters.keys())


def _ensure_non_empty_text(text: Any, fallback: str = DEFAULT_FSUB_CAPTION) -> str:
    """Ensures text is never None, empty, or whitespace-only (Fixes [400 MESSAGE_EMPTY])."""
    if text is None:
        return fallback
    s = str(text).strip()
    if not s or s.lower() in ["none", "null", "false"]:
        return fallback
    return s


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
):
    safe_txt = _ensure_non_empty_text(text)
    compat_kw = _prepare_compat_kwargs(
        kwargs, _REPLY_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await message.reply_text(safe_txt, **compat_kw)
    except MessageEmpty:
        return await message.reply_text(DEFAULT_FSUB_CAPTION, **compat_kw)
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        compat_kw.pop("reply_parameters", None)
        compat_kw.pop("reply_to_message_id", None)
        return await message.reply_text(safe_txt, **compat_kw)
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None


async def safe_edit_text(
    message: Message, text: str, disable_web_page_preview: bool = True, **kwargs
):
    safe_txt = _ensure_non_empty_text(text)
    compat_kw = _prepare_compat_kwargs(
        kwargs, _EDIT_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await message.edit_text(safe_txt, **compat_kw)
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        return await message.edit_text(safe_txt, **compat_kw)
    except (MessageNotModified, MessageIdInvalid, ChatWriteForbidden, Forbidden):
        return None


async def safe_send_message(
    client: Client,
    chat_id: int,
    text: str,
    disable_web_page_preview: Optional[bool] = True,
    **kwargs,
):
    safe_txt = _ensure_non_empty_text(text)
    compat_kw = _prepare_compat_kwargs(
        kwargs, _SEND_MSG_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await client.send_message(chat_id=chat_id, text=safe_txt, **compat_kw)
    except MessageEmpty:
        return await client.send_message(
            chat_id=chat_id, text=DEFAULT_FSUB_CAPTION, **compat_kw
        )
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        compat_kw.pop("reply_parameters", None)
        compat_kw.pop("reply_to_message_id", None)
        return await client.send_message(chat_id=chat_id, text=safe_txt, **compat_kw)
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None


async def safe_answer(
    query: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
    url: Optional[str] = None,
):
    if not query:
        return
    try:
        kwargs: Dict[str, Any] = {"show_alert": show_alert}
        if text is not None:
            kwargs["text"] = str(text)[:200]
        if url is not None:
            kwargs["url"] = url
        await query.answer(**kwargs)
    except (QueryIdInvalid, Exception):
        pass


# ============================================================
# 🎨 SAFE TELEGRAM BUTTON STYLE DETECTION
# ============================================================
BTN_PRIMARY = None
BTN_SECONDARY = None
BTN_SUCCESS = None
BTN_DANGER = None
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
    BTN_PRIMARY = BTN_SECONDARY = BTN_SUCCESS = BTN_DANGER = None
    _SUPPORTS_BTN_STYLE = False


def create_btn(text, url=None, callback_data=None, style=None):
    kwargs = {"text": str(text)}
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


# ============================================================
# ⏳ SELF-CONTAINED AUTO-DELETE ENGINE (ZERO IMPORT ERRORS)
# ============================================================
def get_auto_delete_timer(
    var_name: str = "FILE_AUTO_DELETE", default_time: int = 1800
) -> int:
    try:
        global_switch = getattr(info, "AUTO_DELETE", True)
        if str(global_switch).strip().lower() in ["false", "off", "0", "no"]:
            return 0
        raw_val = getattr(
            info,
            var_name,
            getattr(
                info,
                "FILE_AUTO_DELETE",
                getattr(info, "AUTO_DELETE_TIME", default_time),
            ),
        )
        if isinstance(raw_val, bool):
            return default_time if raw_val else 0
        val = int(str(raw_val).strip())
        return 1800 if 0 < val < 10 else max(0, val)
    except Exception:
        return default_time


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0 Seconds"
    m, s = divmod(int(seconds), 60)
    if m > 0 and s > 0:
        return f"{m} Minute{'s' if m != 1 else ''} {s} Second{'s' if s != 1 else ''}"
    if m > 0:
        return f"{m} Minute{'s' if m != 1 else ''}"
    return f"{s} Second{'s' if s != 1 else ''}"


async def _fsub_auto_delete_worker(
    client: Client, chat_id: int, is_pm: bool, delay: int, *messages
):
    await asyncio.sleep(delay)

    msg_ids = [m.id for m in messages if m is not None and hasattr(m, "id") and m.id]
    if msg_ids:
        try:
            await client.delete_messages(chat_id, msg_ids)
        except Exception:
            for m in messages:
                if m is not None:
                    try:
                        await m.delete()
                    except Exception:
                        pass

    if is_pm:
        notify_text = (
            getattr(info, "DEL_MSG", None)
            or "<b>Successfully deleted your file, if you want request again in group.</b>"
        )
        markup = (
            _get_deleted_file_notify_keyboard()
            if callable(_get_deleted_file_notify_keyboard)
            else InlineKeyboardMarkup(
                [
                    [
                        create_btn(
                            "Movie Request 📝",
                            url="https://t.me/Sandalwood_Kannada_Group",
                            style=BTN_SUCCESS,
                        )
                    ]
                ]
            )
        )
        try:
            await safe_send_message(
                client,
                chat_id,
                text=notify_text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass


def schedule_auto_delete(
    client: Client, chat_id: int, is_pm: bool, delay: int, *messages
):
    if delay <= 0:
        return
    task = asyncio.create_task(
        _fsub_auto_delete_worker(client, chat_id, is_pm, delay, *messages)
    )
    AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(AUTO_DELETE_TASKS.discard)


# ============================================================
# ⚙️ QUOTA-SAFE MONGODB FSUB & FAST IN-MEMORY CACHE ENGINE
# ============================================================
_fsub_db = None
_active_db = None
_fsub_reqs_db = None
_fsub_users_db = None
_BOT_DB = None

# Circuit breaker so a full 512MB DB never logs errors on join requests
_FSUB_DB_QUOTA_FULL_UNTIL: float = 0.0

# Ultra-fast in-memory caches so ForceSub checks don't wait on DB or Telegram API round-trips
_FSUB_CONFIG_CACHE: Optional[dict] = None
_FSUB_CACHE_TS: float = 0.0
_INVITE_LINK_CACHE: Dict[str, str] = {}

# Cache verified (channel_id, user_id) -> expiry_timestamp for 10 minutes (0ms file delivery!)
_VERIFIED_MEMBER_CACHE: Dict[Tuple[int, int], float] = {}
# RAM set of users who sent a join request: {(channel_id, user_id)}
_JOIN_REQ_RAM_CACHE: set = set()

try:
    _DB_CLIENT = AsyncIOMotorClient(
        info.DATABASE_URI,
        serverSelectionTimeoutMS=4000,
        connectTimeoutMS=4000,
        socketTimeoutMS=8000,
    )
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _fsub_db = _BOT_DB["fsub_config"]
    _active_db = _BOT_DB["active_connections"]
    _fsub_reqs_db = _BOT_DB["fsub_join_requests"]
    _fsub_users_db = _BOT_DB["fsub_verified_users"]
except Exception as e:
    logger.error(f"Failed to init FSub DB: {e}")


def _get_writable_col(col_name: str, fallback_col: Any):
    if callable(get_writable_System_db):
        try:
            target_db = get_writable_System_db()
            if target_db is not None:
                return target_db[col_name]
        except Exception:
            pass
    return fallback_col


def _is_quota_or_auth_err(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        "space quota" in s
        or "over your space" in s
        or "8000" in s
        or "bad auth" in s
        or "authentication failed" in s
        or "paused" in s
    )


async def get_fsub_config(force_refresh: bool = False) -> dict:
    global _FSUB_CONFIG_CACHE, _FSUB_CACHE_TS
    now = time.time()
    if (
        not force_refresh
        and _FSUB_CONFIG_CACHE is not None
        and (now - _FSUB_CACHE_TS) < 120
    ):
        return _FSUB_CONFIG_CACHE

    default_cfg = {
        "is_enabled": True,
        "max_count": 0,
        "rotation_enabled": False,
        "channels": {},
    }

    if callable(get_all_healthy_read_dbs):
        for idx, target_db in get_all_healthy_read_dbs():
            try:
                doc = await target_db["fsub_config"].find_one({"_id": "config"})
                if doc:
                    for k, v in default_cfg.items():
                        if k not in doc:
                            doc[k] = v
                    _FSUB_CONFIG_CACHE = doc
                    _FSUB_CACHE_TS = now
                    return doc
            except Exception as e:
                if callable(mark_db_error):
                    mark_db_error(idx, e)
                continue
    elif _fsub_db is not None:
        try:
            doc = await _fsub_db.find_one({"_id": "config"})
            if doc:
                for k, v in default_cfg.items():
                    if k not in doc:
                        doc[k] = v
                _FSUB_CONFIG_CACHE = doc
                _FSUB_CACHE_TS = now
                return doc
        except Exception:
            pass

    if _FSUB_CONFIG_CACHE is not None:
        _FSUB_CACHE_TS = now
        return _FSUB_CONFIG_CACHE

    _FSUB_CONFIG_CACHE = default_cfg
    _FSUB_CACHE_TS = now
    return default_cfg


async def update_fsub_config(key: str, value: Any):
    global _FSUB_CONFIG_CACHE, _FSUB_CACHE_TS
    if _FSUB_CONFIG_CACHE is None:
        await get_fsub_config()
    if _FSUB_CONFIG_CACHE is not None:
        _FSUB_CONFIG_CACHE[key] = value
        _FSUB_CACHE_TS = time.time()

    target_col = _get_writable_col("fsub_config", _fsub_db)
    if target_col is not None:
        try:
            await target_col.update_one(
                {"_id": "config"}, {"$set": {key: value}}, upsert=True
            )
        except Exception:
            # RAM cache is already updated if MongoDB is 512MB full
            pass


async def update_fsub_channels(channels_dict: dict):
    global _FSUB_CONFIG_CACHE, _FSUB_CACHE_TS
    _VERIFIED_MEMBER_CACHE.clear()
    if _FSUB_CONFIG_CACHE is None:
        await get_fsub_config()
    if _FSUB_CONFIG_CACHE is not None:
        _FSUB_CONFIG_CACHE["channels"] = channels_dict
        _FSUB_CACHE_TS = time.time()

    target_col = _get_writable_col("fsub_config", _fsub_db)
    if target_col is not None:
        try:
            await target_col.update_one(
                {"_id": "config"}, {"$set": {"channels": channels_dict}}, upsert=True
            )
        except Exception:
            pass


async def _bg_save_fsub_user(user_id: int):
    global _FSUB_DB_QUOTA_FULL_UNTIL
    if time.time() < _FSUB_DB_QUOTA_FULL_UNTIL:
        return

    target_col = _get_writable_col("fsub_verified_users", _fsub_users_db)
    if target_col is not None:
        try:
            await target_col.update_one(
                {"_id": int(user_id)},
                {"$set": {"timestamp": time.time()}},
                upsert=True,
            )
        except Exception as e:
            if _is_quota_or_auth_err(e):
                _FSUB_DB_QUOTA_FULL_UNTIL = time.time() + 1800
            return

    if _plugin_db is not None and hasattr(_plugin_db, "add_fsub_user"):
        try:
            await _plugin_db.add_fsub_user(user_id)
        except Exception:
            pass


async def safe_add_fsub_user(user_id: int):
    """Non-blocking background save so file delivery never waits on MongoDB."""
    asyncio.create_task(_bg_save_fsub_user(user_id))


async def get_total_fsub_users() -> int:
    total = len(_VERIFIED_MEMBER_CACHE)
    if _fsub_users_db is not None:
        try:
            db_cnt = await _fsub_users_db.count_documents({})
            total = max(total, int(db_cnt or 0))
        except Exception:
            pass
    if _plugin_db is not None and hasattr(_plugin_db, "get_fsub_users_count"):
        try:
            p_cnt = await _plugin_db.get_fsub_users_count()
            total = max(total, int(p_cnt or 0))
        except Exception:
            pass
    return total


async def clear_all_fsub_users() -> int:
    _VERIFIED_MEMBER_CACHE.clear()
    deleted = 0
    if _fsub_users_db is not None:
        try:
            res = await _fsub_users_db.delete_many({})
            deleted = getattr(res, "deleted_count", 0)
        except Exception:
            pass
    if _plugin_db is not None and hasattr(_plugin_db, "clear_fsub_users"):
        try:
            await _plugin_db.clear_fsub_users()
        except Exception:
            pass
    return deleted


async def record_join_request(
    user_id: int,
    chat_id: int,
    first_name: str = "User",
    username: str = "None",
    date=None,
):
    """
    Saves join requests in RAM cache (0ms) and attempts a quota-safe DB write.
    Never logs errors when the 512MB MongoDB cluster is full!
    """
    global _FSUB_DB_QUOTA_FULL_UNTIL
    uid, cid = int(user_id), int(chat_id)
    if len(_JOIN_REQ_RAM_CACHE) > 50000:
        _JOIN_REQ_RAM_CACHE.clear()
    _JOIN_REQ_RAM_CACHE.add((cid, uid))
    _VERIFIED_MEMBER_CACHE[(cid, uid)] = time.time() + 3600

    now = time.time()
    if now < _FSUB_DB_QUOTA_FULL_UNTIL:
        return

    target_col = _get_writable_col("fsub_join_requests", _fsub_reqs_db)
    if target_col is not None:
        try:
            await target_col.update_one(
                {"user_id": uid, "chat_id": cid},
                {
                    "$set": {
                        "user_id": uid,
                        "chat_id": cid,
                        "timestamp": now,
                    }
                },
                upsert=True,
            )
        except Exception as e:
            if _is_quota_or_auth_err(e):
                # Mute DB writes for 30 minutes when 512MB quota is full; RAM cache handles verification
                _FSUB_DB_QUOTA_FULL_UNTIL = now + 1800
                return

    try:
        if _join_reqs_db and _join_reqs_db.isActive():
            await _join_reqs_db.add_user(
                user_id=uid,
                first_name=first_name,
                username=username,
                date=date,
            )
    except Exception as e:
        if _is_quota_or_auth_err(e):
            _FSUB_DB_QUOTA_FULL_UNTIL = now + 1800


async def has_join_request(user_id: int, chat_id: Optional[int] = None) -> bool:
    """Checks RAM cache first (0ms), then healthy MongoDB if needed."""
    uid = int(user_id)
    if chat_id is not None and (int(chat_id), uid) in _JOIN_REQ_RAM_CACHE:
        return True
    if chat_id is None and any(u == uid for (_, u) in _JOIN_REQ_RAM_CACHE):
        return True

    target_col = _get_writable_col("fsub_join_requests", _fsub_reqs_db)
    if target_col is not None:
        try:
            if chat_id is not None:
                doc = await target_col.find_one(
                    {"user_id": uid, "chat_id": int(chat_id)}
                )
                if doc:
                    _JOIN_REQ_RAM_CACHE.add((int(chat_id), uid))
                    return True
            doc_any = await target_col.find_one({"user_id": uid})
            if doc_any:
                if chat_id is not None:
                    _JOIN_REQ_RAM_CACHE.add((int(chat_id), uid))
                return True
        except Exception:
            pass

    try:
        if _join_reqs_db and _join_reqs_db.isActive():
            if await _join_reqs_db.get_user(uid):
                if chat_id is not None:
                    _JOIN_REQ_RAM_CACHE.add((int(chat_id), uid))
                return True
    except Exception:
        pass
    return False


async def get_total_join_requests() -> int:
    total = len(_JOIN_REQ_RAM_CACHE)
    if _fsub_reqs_db is not None:
        try:
            db_total = await _fsub_reqs_db.count_documents({})
            total = max(total, int(db_total or 0))
        except Exception:
            pass
    try:
        if _join_reqs_db and _join_reqs_db.isActive():
            legacy_total = await _join_reqs_db.total_requests()
            total = max(total, int(legacy_total or 0))
        elif _join_reqs_db and hasattr(_join_reqs_db, "get_all_users_count"):
            legacy_total = await _join_reqs_db.get_all_users_count()
            total = max(total, int(legacy_total or 0))
    except Exception:
        pass
    return total


async def clear_all_join_requests() -> int:
    global _FSUB_DB_QUOTA_FULL_UNTIL
    ram_cnt = len(_JOIN_REQ_RAM_CACHE)
    _JOIN_REQ_RAM_CACHE.clear()
    _VERIFIED_MEMBER_CACHE.clear()
    _FSUB_DB_QUOTA_FULL_UNTIL = 0.0
    deleted = 0
    if _fsub_reqs_db is not None:
        try:
            res = await _fsub_reqs_db.delete_many({})
            deleted = getattr(res, "deleted_count", 0)
        except Exception:
            pass
    try:
        if _join_reqs_db and _join_reqs_db.isActive():
            if hasattr(_join_reqs_db, "clear_all"):
                await _join_reqs_db.clear_all()
            elif hasattr(_join_reqs_db, "delete_all_users"):
                await _join_reqs_db.delete_all_users()
    except Exception:
        pass
    return max(deleted, ram_cnt)


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
    if not user_id:
        return False
    return int(user_id) in get_admin_list()


async def get_active_connection(user_id: str) -> Optional[int]:
    uid_str = str(user_id)
    if _active_db is not None:
        try:
            doc = await _active_db.find_one({"user_id": uid_str})
            if doc and doc.get("group_id"):
                return int(doc["group_id"])
        except Exception:
            pass

    try:
        from database.connections_mdb import active_connection

        conn = await active_connection(uid_str)
        if conn:
            return int(conn)
    except Exception:
        pass

    try:
        from database.connections_mdb import all_connections, if_active

        groupids = await all_connections(uid_str)
        if groupids:
            for gid in groupids:
                if await if_active(uid_str, str(gid)):
                    return int(gid)
            return int(groupids[-1])
    except Exception:
        pass

    return None


async def get_target_chat(client: Client, message: Message) -> Optional[int]:
    if message.chat.type == enums.ChatType.PRIVATE:
        if not message.from_user:
            return None
        return await get_active_connection(str(message.from_user.id))
    return message.chat.id


async def verify_admin(
    client: Client, message: Message, target_chat_id: Optional[int] = None
) -> bool:
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if await is_global_admin(uid):
        return True

    if message.chat.type == enums.ChatType.PRIVATE:
        if not target_chat_id:
            return False
        try:
            member = await client.get_chat_member(int(target_chat_id), uid)
            return member.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]
        except Exception:
            return False

    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True
        if message.from_user:
            try:
                member = await client.get_chat_member(message.chat.id, uid)
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
            except Exception:
                return False
    return False


async def verify_cb_admin(client: Client, query: CallbackQuery) -> bool:
    uid = query.from_user.id if query.from_user else 0
    if await is_global_admin(uid):
        return True
    if query.message and query.message.chat:
        if query.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            try:
                member = await client.get_chat_member(query.message.chat.id, uid)
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
            except Exception:
                return False
        elif query.message.chat.type == enums.ChatType.PRIVATE:
            conn_id = await get_active_connection(str(uid))
            if conn_id:
                try:
                    member = await client.get_chat_member(int(conn_id), uid)
                    return member.status in [
                        enums.ChatMemberStatus.ADMINISTRATOR,
                        enums.ChatMemberStatus.OWNER,
                    ]
                except Exception:
                    return False
    return False


# ============================================================
# 🔄 ASYNC NATIVE LISTENER ENGINE
# ============================================================
_WAITING_REQUESTS = {}


@Client.on_message(filters.group | filters.private, group=-12)
async def custom_listener(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in _WAITING_REQUESTS:
        return
    if (
        message.text
        and message.text.startswith("/")
        and message.text.strip().lower() not in ["/cancel"]
    ):
        return

    sender_id = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    expected_session_id = _WAITING_REQUESTS[chat_id]["session_id"]

    if sender_id == expected_session_id or sender_id == chat_id:
        future = _WAITING_REQUESTS[chat_id]["future"]
        if not future.done():
            future.set_result(message)
        message.stop_propagation()


async def native_listen(
    client: Client, chat_id: int, user_id: int, timeout: int = 120
) -> Message:
    if chat_id in _WAITING_REQUESTS:
        old_fut = _WAITING_REQUESTS[chat_id].get("future")
        if old_fut and not old_fut.done():
            old_fut.cancel()

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _WAITING_REQUESTS[chat_id] = {"future": future, "session_id": user_id}
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        if _WAITING_REQUESTS.get(chat_id, {}).get("future") is future:
            _WAITING_REQUESTS.pop(chat_id, None)


# ============================================================
# 🔗 INVITE LINK & FAST RAM-CACHED PARALLEL USER CHECKING
# ============================================================
async def get_invite_link(bot: Client, chat_id_str: str, config: dict) -> str:
    channels = config.get("channels", {})
    channel_data = channels.get(chat_id_str, {})
    if channel_data.get("target"):
        return channel_data["target"]
    if channel_data.get("link"):
        return channel_data["link"]
    if chat_id_str in _INVITE_LINK_CACHE:
        return _INVITE_LINK_CACHE[chat_id_str]

    try:
        chat_id_int = int(str(chat_id_str).strip())
        chat = await bot.get_chat(chat_id_int)
        is_req = channel_data.get("type") == "req"
        try:
            invite = await bot.create_chat_invite_link(
                chat_id_int, creates_join_request=is_req
            )
            inv_link = invite.invite_link
        except Exception:
            inv_link = chat.invite_link or (
                f"https://t.me/{chat.username}" if chat.username else ""
            )

        if inv_link:
            _INVITE_LINK_CACHE[chat_id_str] = inv_link
            if chat_id_str not in channels:
                channels[chat_id_str] = {
                    "title": chat.title or str(chat_id_int),
                    "link": inv_link,
                    "target": None,
                    "type": "req" if is_req else "regular",
                    "status": "active",
                }
            else:
                channels[chat_id_str]["link"] = inv_link
                channels[chat_id_str]["title"] = chat.title or str(chat_id_int)
            await update_fsub_channels(channels)
        return inv_link
    except (ChatAdminRequired, Exception) as e:
        logger.error(f"get_invite_link error for {chat_id_str}: {e}")
        return ""


async def check_user_in_channel(
    bot: Client, channel_id: int, user_id: int, is_req: bool = False
) -> bool:
    cid, uid = int(channel_id), int(user_id)
    cache_key = (cid, uid)
    now = time.time()

    # ⚡ 0ms RAM Cache Check: If verified within the last 10 minutes, skip Telegram API call!
    exp = _VERIFIED_MEMBER_CACHE.get(cache_key)
    if exp and now < exp:
        return True

    if is_req and cache_key in _JOIN_REQ_RAM_CACHE:
        _VERIFIED_MEMBER_CACHE[cache_key] = now + 3600
        return True

    try:
        member = await bot.get_chat_member(cid, uid)
        if member.status in [
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]:
            if len(_VERIFIED_MEMBER_CACHE) > 20000:
                _VERIFIED_MEMBER_CACHE.clear()
            _VERIFIED_MEMBER_CACHE[cache_key] = now + 600
            return True
    except UserNotParticipant:
        pass
    except FloodWait as e:
        if e.value <= 5:
            await asyncio.sleep(e.value + 1)
            return await check_user_in_channel(bot, cid, uid, is_req)
        return True
    except Exception:
        pass

    if is_req:
        if await has_join_request(uid, cid):
            _VERIFIED_MEMBER_CACHE[cache_key] = now + 3600
            return True
    return False


async def _get_missing_channel_buttons(
    bot: Client, user_id: int, config: dict
) -> List[List[InlineKeyboardButton]]:
    """Checks all eligible FSub channels concurrently for maximum speed."""
    channels_dict = config.get("channels", {})
    if not channels_dict:
        return []

    rotation_enabled = config.get("rotation_enabled", False)
    max_count = int(config.get("max_count", 0) or 0)

    if rotation_enabled:
        eligible_channels = list(channels_dict.keys())
        if eligible_channels:
            shift = int(time.time() / 3600) % len(eligible_channels)
            eligible_channels = eligible_channels[shift:] + eligible_channels[:shift]
    else:
        eligible_channels = [
            k for k, v in channels_dict.items() if v.get("status", "active") == "active"
        ]

    if not eligible_channels:
        return []

    async def _check_single(cid_str: str):
        ch_data = channels_dict.get(cid_str, {})
        is_req = ch_data.get("type") == "req"
        joined = await check_user_in_channel(bot, int(cid_str), user_id, is_req)
        return cid_str, is_req, joined

    results = await asyncio.gather(
        *[_check_single(cid) for cid in eligible_channels], return_exceptions=True
    )

    not_joined_buttons = []
    channels_to_force = 0

    for res in results:
        if isinstance(res, Exception):
            continue
        cid_str, is_req, joined = res
        if joined:
            continue
        if max_count > 0 and channels_to_force >= max_count:
            break
        link = await get_invite_link(bot, cid_str, config)
        if link:
            btn_text = "⚓ Request to Join" if is_req else "📢 Join Channel"
            not_joined_buttons.append(
                [create_btn(btn_text, url=link, style=BTN_PRIMARY)]
            )
            channels_to_force += 1

    return not_joined_buttons


# ============================================================
# 🛑 MAIN FORCESUB INTERCEPTOR
# ============================================================
async def ForceSub(
    bot: Client, message: Message, file_id: str = None, mode: str = None
) -> bool:
    user = message.from_user
    if not user:
        return True
    if await is_global_admin(user.id):
        return True

    config = await get_fsub_config()
    if not config.get("is_enabled", True) or not config.get("channels"):
        return True

    try:
        not_joined_buttons = await _get_missing_channel_buttons(bot, user.id, config)

        if not not_joined_buttons:
            await safe_add_fsub_user(user.id)
            return True

        safe_mode = mode or "checksub"
        safe_fid = file_id or "0"
        cb_data = f"refresh_fsub#{safe_mode}#{safe_fid}"
        not_joined_buttons.append(
            [
                create_btn(
                    text="✅ I've Joined", callback_data=cb_data, style=BTN_SUCCESS
                )
            ]
        )

        b_settings = await get_bot_settings() or {}
        custom_fsub_img = b_settings.get("fsub_img") or getattr(info, "FSUB_IMG", None)
        raw_fsub_msg = b_settings.get("fsub_msg") or getattr(info, "FSUB_MSG", None)
        fsub_caption = _ensure_non_empty_text(raw_fsub_msg, DEFAULT_FSUB_CAPTION)

        default_markup = InlineKeyboardMarkup(not_joined_buttons)
        try:
            parsed_cap, parsed_markup = parse_text_and_markup(
                fsub_caption, default_markup
            )
            fsub_caption = _ensure_non_empty_text(parsed_cap, DEFAULT_FSUB_CAPTION)
            final_markup = parsed_markup or default_markup
        except Exception:
            final_markup = default_markup

        try:
            if custom_fsub_img and str(custom_fsub_img).strip().lower() not in [
                "none",
                "null",
                "false",
                "",
            ]:
                try:
                    await message.reply_photo(
                        photo=custom_fsub_img,
                        caption=fsub_caption,
                        reply_markup=final_markup,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    return False
                except Exception:
                    pass
            await safe_reply_text(
                message,
                fsub_caption,
                reply_markup=final_markup,
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )
        except (UserIsBlocked, PeerIdInvalid, ChatWriteForbidden, Forbidden):
            return False
        except Exception as e:
            logger.error(f"ForceSub reply error: {e}")
        return False
    except Exception as e:
        logger.error(f"ForceSub error: {e}")
        return True


async def _deliver_verified_file(
    bot: Client, user_id: int, chat_id: int, file_id: str, mode: str = "checksub"
):
    """Fetches full file details (or batch/genlink payloads) and sends with auto-delete."""
    if not file_id or file_id == "0":
        return

    # 1. Check if this is a BATCH-, DSTORE-, or genlink payload
    if file_id.startswith(("BATCH-", "DSTORE-")):
        try:
            from plugins.genlink import deliver_genlink_payload

            if await deliver_genlink_payload(bot, user_id, chat_id, file_id):
                return
        except Exception as e:
            logger.error(f"Genlink FSub delivery error: {e}")

    protect = mode in ["filep", "checksubp"]
    files_ = await get_file_details(file_id)

    default_file_kb = (
        _get_default_file_keyboard()
        if callable(_get_default_file_keyboard)
        else InlineKeyboardMarkup(
            [
                [
                    create_btn(
                        text="🎥 ಕನ್ನಡ ಹೊಸ ಮೂವೀಗಳು 🎥",
                        url="https://t.me/sandalwood_kannada_moviesz",
                        style=BTN_SUCCESS,
                    )
                ]
            ]
        )
    )

    if files_:
        files = files_[0]
        title = str(
            files.get("file_name", "Unknown")
            if isinstance(files, dict)
            else getattr(files, "file_name", "Unknown")
        )
        size = get_size(
            int(
                files.get("file_size", 0)
                if isinstance(files, dict)
                else getattr(files, "file_size", 0)
            )
        )
        f_caption = str(
            files.get("caption", "")
            if isinstance(files, dict)
            else getattr(files, "caption", "")
        )
        db_file_id = (
            files.get("full_file_id", files.get("file_id", file_id))
            if isinstance(files, dict)
            else getattr(files, "full_file_id", getattr(files, "file_id", file_id))
        )

        custom_cap = getattr(info, "CUSTOM_FILE_CAPTION", None)
        if custom_cap:
            try:
                f_caption = custom_cap.format(
                    file_name="" if title == "Unknown" else title,
                    file_size="" if size == "0B" else size,
                    file_caption="" if not f_caption else f_caption,
                )
            except Exception:
                pass

        if not f_caption or not str(f_caption).strip():
            f_caption = f"<b>{title}</b>"
        elif not f_caption.strip().startswith("<b>"):
            f_caption = f"<b>{f_caption}</b>"

        if getattr(info, "CAPTION_PLUS", None):
            f_caption += f"\n\n{info.CAPTION_PLUS}"

        final_caption, final_keyboard = parse_text_and_markup(
            f_caption, default_file_kb
        )
        final_caption = _ensure_non_empty_text(final_caption, f"<b>{title}</b>")
    else:
        # Check if stored in genlink_files DB
        try:
            from plugins.genlink import deliver_genlink_payload

            prefix = "filep" if protect else "file"
            if await deliver_genlink_payload(
                bot, user_id, chat_id, f"{prefix}_{file_id}"
            ):
                return
        except Exception:
            pass

        db_file_id = file_id
        final_caption = "<b>🎉 Thank you for joining! Here is your file:</b>"
        final_keyboard = default_file_kb

    # Send optional Info Message / Info Image before delivering the file if configured
    info_msg_txt = getattr(info, "INFO_MSG", None)
    info_img_val = getattr(info, "INFO_IMG", None)
    pre_info_msg = None
    if info_msg_txt or info_img_val:
        try:
            if info_img_val and str(info_img_val).strip().lower() not in [
                "none",
                "null",
                "",
            ]:
                pre_info_msg = await bot.send_photo(
                    chat_id=user_id,
                    photo=info_img_val,
                    caption=str(info_msg_txt or ""),
                    parse_mode=enums.ParseMode.HTML,
                )
            elif info_msg_txt and str(info_msg_txt).strip():
                pre_info_msg = await safe_send_message(
                    bot,
                    chat_id=user_id,
                    text=str(info_msg_txt),
                    parse_mode=enums.ParseMode.HTML,
                )
        except Exception:
            pre_info_msg = None

    sent_msg = None
    try:
        sent_msg = await bot.send_cached_media(
            chat_id=user_id,
            file_id=db_file_id,
            caption=final_caption,
            reply_markup=final_keyboard,
            protect_content=protect,
            parse_mode=enums.ParseMode.HTML,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        try:
            sent_msg = await bot.send_cached_media(
                chat_id=user_id,
                file_id=db_file_id,
                caption=final_caption,
                reply_markup=final_keyboard,
                protect_content=protect,
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass
    except Exception:
        try:
            sent_msg = await bot.send_document(
                chat_id=user_id,
                document=db_file_id,
                caption=final_caption,
                reply_markup=final_keyboard,
                protect_content=protect,
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            await safe_send_message(
                bot,
                chat_id=user_id,
                text="✅ <b>Verification complete!</b>\n\n⚠️ <i>Please click the file link again to receive your movie.</i>",
                parse_mode=enums.ParseMode.HTML,
            )
            return

    if sent_msg:
        try:
            delete_timer = get_auto_delete_timer("FILE_AUTO_DELETE", 1800)
            if delete_timer > 0:
                time_str = format_duration(delete_timer)
                warn_text = (
                    f"<b>📢 Please Note\n\n"
                    f"✅ The above file will be autodeleted in {time_str} to avoid copyright issues.\n\n"
                    f"✅ Please forward this file to your saved messages and start downloading from there.\n\n"
                    f"Tᴇᴀᴍ: @KR_Picture</b>"
                )
                warn_msg = await safe_send_message(
                    bot,
                    chat_id=user_id,
                    text=warn_text,
                    reply_to_message_id=sent_msg.id,
                    parse_mode=enums.ParseMode.HTML,
                )
                schedule_auto_delete(
                    bot, chat_id, True, delete_timer, pre_info_msg, sent_msg, warn_msg
                )
        except Exception as e:
            logger.error(f"FSub auto-delete schedule error: {e}")


@Client.on_callback_query(filters.regex(r"^refresh_fsub"), group=-3)
async def refresh_fsub_callback(bot: Client, query: CallbackQuery):
    user_id = query.from_user.id if query.from_user else 0
    data = query.data

    mode = "checksub"
    file_id = "0"
    if data.startswith("refresh_fsub#"):
        parts = data.split("#", 2)
        if len(parts) == 3:
            _, mode, file_id = parts
    elif data.startswith("refresh_fsub_"):
        file_id = data.replace("refresh_fsub_", "", 1)

    config = await get_fsub_config()
    channels_dict = config.get("channels", {})

    if not await is_global_admin(user_id) and channels_dict:
        not_joined_buttons = await _get_missing_channel_buttons(bot, user_id, config)

        if not_joined_buttons:
            await safe_answer(
                query,
                "❌ You haven't joined all required channels yet!",
                show_alert=True,
            )
            cb_data = f"refresh_fsub#{mode}#{file_id}"
            not_joined_buttons.append(
                [
                    create_btn(
                        text="✅ I've Joined",
                        callback_data=cb_data,
                        style=BTN_SUCCESS,
                    )
                ]
            )
            try:
                await query.message.edit_reply_markup(
                    InlineKeyboardMarkup(not_joined_buttons)
                )
            except (MessageNotModified, MessageIdInvalid):
                pass
            return

    await safe_add_fsub_user(user_id)
    await safe_answer(query, "✅ Subscriptions verified!", show_alert=False)

    try:
        await query.message.delete()
    except Exception:
        pass

    await _deliver_verified_file(bot, user_id, query.message.chat.id, file_id, mode)


# ============================================================
# ⚙️ CONFIGURATION COMMANDS & INTERACTIVE BUTTONS
# ============================================================
@Client.on_message(filters.command("rotatefsub") & filters.incoming, group=-3)
async def toggle_rotate(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    config = await get_fsub_config(force_refresh=True)
    current = config.get("rotation_enabled", False)
    await update_fsub_config("rotation_enabled", not current)
    status = (
        "🟢 ENABLED (Smart 1-Hour User Rotation)"
        if not current
        else "🔴 DISABLED (Static Priority)"
    )
    await safe_reply_text(
        message,
        f"🔄 <b>Auto-Rotation Engine:</b> {status}\n\n"
        f"<i>When enabled, the bot automatically cycles through all channels and forces users into the ones they haven't joined yet!</i>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("enablefsub") & filters.incoming, group=-3)
async def enable_fsub(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    await update_fsub_config("is_enabled", True)
    await safe_reply_text(
        message,
        "✅ <b>Force Subscribe has been ENABLED.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("disablefsub") & filters.incoming, group=-3)
async def disable_fsub(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    await update_fsub_config("is_enabled", False)
    await safe_reply_text(
        message,
        "❌ <b>Force Subscribe has been DISABLED.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setfsubcount") & filters.incoming, group=-3)
async def set_fsub_count_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2 or not message.command[1].strip().isdigit():
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/setfsubcount &lt;number&gt;</code>\n<i>(Use 0 for unlimited active channels)</i>",
            parse_mode=enums.ParseMode.HTML,
        )

    count_val = int(message.command[1].strip())
    await update_fsub_config("max_count", count_val)
    await safe_reply_text(
        message,
        f"✅ <b>Maximum ForceSub Queue Count set to:</b> <code>{count_val if count_val > 0 else 'Unlimited'}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


async def _finalize_setfsub(
    bot: Client, chat_id: int, channel_id: str, is_req: bool
) -> Tuple[bool, str]:
    try:
        chat = await bot.get_chat(int(channel_id))
        fsub_type = "req" if is_req else "regular"

        config = await get_fsub_config(force_refresh=True)
        channels = config.get("channels", {})
        max_count = int(config.get("max_count", 0) or 0)
        active_count = len(
            [c for c in channels.values() if c.get("status") == "active"]
        )
        status = "pending" if max_count > 0 and active_count >= max_count else "active"

        invite = await bot.create_chat_invite_link(chat.id, creates_join_request=is_req)
        _INVITE_LINK_CACHE[str(chat.id)] = invite.invite_link

        channels[str(chat.id)] = {
            "title": chat.title or str(chat.id),
            "link": invite.invite_link,
            "target": None,
            "type": fsub_type,
            "status": status,
        }
        await update_fsub_channels(channels)

        msg = (
            f"✅ <b>Successfully Configured FSub!</b>\n\n"
            f"📢 <b>Channel:</b> <code>{chat.title}</code>\n"
            f"🆔 <b>ID:</b> <code>{chat.id}</code>\n"
            f"🔗 <b>Link:</b> {invite.invite_link}\n"
            f"⚙️ <b>Type:</b> <code>{'Join Request' if is_req else 'Normal'}</code>\n"
            f"🟢 <b>Status:</b> <code>{status.upper()}</code>"
        )
        return True, msg
    except ChatAdminRequired:
        return (
            False,
            "❌ <b>The bot is not an admin in that channel or lacks 'Invite Users' rights.</b>",
        )
    except Exception as e:
        return False, f"❌ <b>Error:</b> <code>{e}</code>"


@Client.on_message(filters.command("setfsub") & filters.incoming, group=-3)
async def add_dynamic_fsub(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/setfsub &lt;channel_id&gt;</code>\nExample: <code>/setfsub -1001234567890</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    channel_id = message.command[1].strip()
    try:
        chat = await bot.get_chat(
            int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
        )
        channel_id = str(chat.id)
    except Exception as e:
        return await safe_reply_text(
            message,
            f"❌ <b>Failed to fetch channel.</b> Make sure the bot is an admin in <code>{channel_id}</code>.\n<code>Error: {e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    # If user passed y/n or req/normal directly: /setfsub -10012345 y
    if len(message.command) >= 3:
        arg2 = message.command[2].strip().lower()
        is_req = arg2 in ["y", "yes", "req", "request", "true"]
        _, result_text = await _finalize_setfsub(
            bot, message.chat.id, channel_id, is_req
        )
        return await safe_reply_text(
            message,
            result_text,
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.HTML,
        )

    buttons = [
        [
            create_btn(
                "⚓ Join Request (Yes)",
                callback_data=f"fsub_setup#req#{channel_id}",
                style=BTN_SUCCESS,
            ),
            create_btn(
                "📢 Normal Invite (No)",
                callback_data=f"fsub_setup#reg#{channel_id}",
                style=BTN_PRIMARY,
            ),
        ],
        [
            create_btn(
                "❌ Cancel",
                callback_data="fsub_setup#cancel#0",
                style=BTN_DANGER,
            )
        ],
    ]

    ask_msg = await safe_reply_text(
        message,
        f"🎯 <b>Target:</b> <code>{chat.title}</code> (<code>{channel_id}</code>)\n\n"
        f"Do you want this to be a <b>Join Request</b> channel?\n\n"
        f"👉 <b>Click a button below</b> <i>(or reply with <code>y</code> for Yes / <code>n</code> for No)</i>:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        user_id = (
            message.from_user.id
            if message.from_user
            else getattr(message.sender_chat, "id", 0)
        )
        resp = await native_listen(bot, message.chat.id, user_id, timeout=60)
        if not resp or not resp.text:
            return
        txt = resp.text.strip().lower()
        if txt == "/cancel":
            if ask_msg:
                await safe_edit_text(
                    ask_msg,
                    "❌ <b>FSub setup cancelled.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return
        if txt in ["y", "yes", "n", "no"]:
            is_req = txt in ["y", "yes"]
            try:
                await resp.delete()
            except Exception:
                pass
            _, result_text = await _finalize_setfsub(
                bot, message.chat.id, channel_id, is_req
            )
            if ask_msg:
                await safe_edit_text(
                    ask_msg,
                    result_text,
                    disable_web_page_preview=True,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                await safe_reply_text(
                    message,
                    result_text,
                    disable_web_page_preview=True,
                    parse_mode=enums.ParseMode.HTML,
                )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@Client.on_callback_query(
    filters.regex(r"^(fsub_setup#|fsub_purge#|fsub_del#)"), group=-3
)
async def fsub_admin_callbacks(bot: Client, query: CallbackQuery):
    if not await verify_cb_admin(bot, query):
        return await safe_answer(query, "❌ Unauthorized: Admin only.", show_alert=True)

    data = query.data
    if query.message.chat.id in _WAITING_REQUESTS:
        fut = _WAITING_REQUESTS[query.message.chat.id].get("future")
        if fut and not fut.done():
            fut.cancel()
        _WAITING_REQUESTS.pop(query.message.chat.id, None)

    if data.startswith("fsub_setup#"):
        _, mode, channel_id = data.split("#", 2)
        if mode == "cancel":
            await safe_answer(query, "Cancelled.")
            return await safe_edit_text(
                query.message,
                "❌ <b>FSub setup cancelled.</b>",
                parse_mode=enums.ParseMode.HTML,
            )

        await safe_answer(query, "⏳ Configuring FSub channel...")
        is_req = mode == "req"
        _, result_text = await _finalize_setfsub(
            bot, query.message.chat.id, channel_id, is_req
        )
        return await safe_edit_text(
            query.message,
            result_text,
            disable_web_page_preview=True,
            parse_mode=enums.ParseMode.HTML,
        )

    if data.startswith("fsub_del#"):
        _, channel_id = data.split("#", 1)
        config = await get_fsub_config(force_refresh=True)
        channels = config.get("channels", {})
        if channel_id in channels:
            del channels[channel_id]
            _INVITE_LINK_CACHE.pop(channel_id, None)
            await update_fsub_channels(channels)
            await safe_answer(query, "🗑️ Channel removed!", show_alert=True)
            text, markup = await build_fsub_list_ui("All")
            return await safe_edit_text(
                query.message,
                text,
                reply_markup=markup,
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )
        return await safe_answer(query, "Channel already removed.", show_alert=True)

    if data.startswith("fsub_purge#"):
        _, action = data.split("#", 1)
        if action == "cancel":
            await safe_answer(query, "Cancelled.")
            return await safe_edit_text(
                query.message,
                "❌ <b>Operation cancelled.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        if action == "confirm":
            count = await clear_all_join_requests()
            await safe_answer(query, "✅ Join requests purged!", show_alert=True)
            return await safe_edit_text(
                query.message,
                f"✅ <b>All join requests purged successfully!</b> (<code>{count}</code> deleted)",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(filters.command("rmfsub") & filters.incoming, group=-3)
async def rem_fsub(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/rmfsub &lt;channel_id&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    channel_id = message.command[1].strip()

    config = await get_fsub_config(force_refresh=True)
    channels = config.get("channels", {})

    if channel_id in channels:
        del channels[channel_id]
        _INVITE_LINK_CACHE.pop(channel_id, None)
        await update_fsub_channels(channels)
        await safe_reply_text(
            message,
            f"🗑️ <b>ForceSub channel <code>{channel_id}</code> removed permanently.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await safe_reply_text(
            message,
            "❌ <b>Channel not found in FSub database.</b>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("rmallfsub") & filters.incoming, group=-3)
async def rem_all_fsub(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    _INVITE_LINK_CACHE.clear()
    await update_fsub_channels({})
    await safe_reply_text(
        message,
        "🗑️ <b>ALL ForceSub channels have been removed and database is empty.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


async def build_fsub_list_ui(
    title_prefix: str = "All", filter_status: str = None
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    config = await get_fsub_config(force_refresh=True)
    channels_dict = config.get("channels", {})
    max_count = config.get("max_count", 0)
    rot_status = "ON" if config.get("rotation_enabled", False) else "OFF"
    fsub_enabled = "🟢 ENABLED" if config.get("is_enabled", True) else "🔴 DISABLED"

    channels = list(channels_dict.items())
    if filter_status:
        channels = [(k, v) for k, v in channels if v.get("status") == filter_status]

    header = (
        f"📡 <b>Force Subscribe Status:</b> {fsub_enabled}\n"
        f"⚙️ <b>Queue Limit:</b> <code>{max_count if max_count > 0 else 'Unlimited'}</code>\n"
        f"🔄 <b>Auto-Rotation:</b> <code>{rot_status}</code>\n\n"
    )

    if not channels:
        return (
            header
            + f"❌ <i>No {title_prefix.lower()} ForceSub channels configured.</i>",
            None,
        )

    text = header + f"📋 <b>{title_prefix} ForceSub Channels ({len(channels)}):</b>\n\n"
    buttons = []
    for idx, (cid, data) in enumerate(channels, 1):
        status_emoji = "🟢" if data.get("status", "active") == "active" else "🟡"
        req_type = "Join Request" if data.get("type") == "req" else "Normal"
        title_str = data.get("title", "Unknown")
        active_link = data.get("target") or data.get("link") or "No Link Generated"
        text += (
            f"{idx}. {status_emoji} <b>{title_str}</b>\n"
            f"├ ID: <code>{cid}</code>\n"
            f"├ Type: <code>{req_type}</code>\n"
            f"└ Link: {active_link}\n\n"
        )
        buttons.append(
            [
                create_btn(
                    f"🗑️ Remove {title_str[:20]}",
                    callback_data=f"fsub_del#{cid}",
                    style=BTN_DANGER,
                )
            ]
        )

    return text, (InlineKeyboardMarkup(buttons) if buttons else None)


@Client.on_message(filters.command(["getallfsub", "fsub"]) & filters.incoming, group=-3)
async def get_all_fsub(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    text, markup = await build_fsub_list_ui("All")
    await safe_reply_text(
        message,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("getactivefsub") & filters.incoming, group=-3)
async def get_active_fsub_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    text, markup = await build_fsub_list_ui("Active", filter_status="active")
    await safe_reply_text(
        message,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("getpendingfsub") & filters.incoming, group=-3)
async def get_pending_fsub_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    text, markup = await build_fsub_list_ui("Pending", filter_status="pending")
    await safe_reply_text(
        message,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(
    filters.command(["activatefsub", "deactivatefsub"]) & filters.incoming, group=-3
)
async def toggle_fsub_channel_status_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    cmd = message.command[0].lower()
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/{cmd} &lt;channel_id&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    cid = message.command[1].strip()
    config = await get_fsub_config(force_refresh=True)
    channels = config.get("channels", {})
    if cid not in channels:
        return await safe_reply_text(
            message,
            f"❌ <b>Channel <code>{cid}</code> not found in FSub database.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    new_status = "active" if cmd == "activatefsub" else "pending"
    channels[cid]["status"] = new_status
    await update_fsub_channels(channels)
    await safe_reply_text(
        message,
        f"✅ <b>Channel <code>{cid}</code> status updated to:</b> <code>{new_status.upper()}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("updatefsubtarget") & filters.incoming, group=-3)
async def update_fsub_target_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 3:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/updatefsubtarget &lt;channel_id&gt; &lt;custom_invite_url_or_off&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    cid = message.command[1].strip()
    target_link = message.command[2].strip()
    config = await get_fsub_config(force_refresh=True)
    channels = config.get("channels", {})
    if cid not in channels:
        return await safe_reply_text(
            message,
            f"❌ <b>Channel <code>{cid}</code> not found in FSub database.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    channels[cid]["target"] = (
        None if target_link.lower() in ["off", "none", "reset"] else target_link
    )
    _INVITE_LINK_CACHE.pop(cid, None)
    await update_fsub_channels(channels)
    await safe_reply_text(
        message,
        f"✅ <b>Updated custom target link for <code>{cid}</code>!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("checkfsubusers") & filters.incoming, group=-3)
async def check_fsub_users_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    count = await get_total_fsub_users()
    await safe_reply_text(
        message,
        f"👥 <b>Total Verified ForceSub Users:</b> <code>{count}</code>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("clearfsubusers") & filters.incoming, group=-3)
async def clear_fsub_users_cmd(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    deleted = await clear_all_fsub_users()
    await safe_reply_text(
        message,
        f"🗑️ <b>Cleared all ForceSub user records!</b> (<code>{deleted}</code> removed)",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 🚪 JOIN REQUEST CATCHER (WORKS FOR ALL DYNAMIC FSUB CHANNELS)
# ============================================================
@Client.on_chat_join_request(group=-3)
async def join_reqs_handler(bot: Client, join_req: ChatJoinRequest):
    user = join_req.from_user
    if not user or not join_req.chat:
        return
    try:
        await record_join_request(
            user_id=user.id,
            chat_id=join_req.chat.id,
            first_name=user.first_name or "Unknown",
            username=user.username or "None",
            date=join_req.date,
        )
    except Exception:
        pass


# ============================================================
# 📊 JOIN REQUEST COMMANDS
# ============================================================
@Client.on_message(filters.command("totalrequests") & filters.incoming, group=-3)
async def total_requests(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        total = await get_total_join_requests()
        await safe_reply_text(
            message,
            f"📨 <b>Total Join Requests:</b> <code>{total}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"⚠️ Error: <code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("purgerequests") & filters.incoming, group=-3)
async def purge_requests(bot: Client, message: Message):
    target_chat_id = await get_target_chat(bot, message)
    if not await verify_admin(bot, message, target_chat_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    buttons = [
        [
            create_btn(
                "🔥 Yes, Purge All Requests",
                callback_data="fsub_purge#confirm",
                style=BTN_DANGER,
            )
        ],
        [
            create_btn(
                "❌ Cancel",
                callback_data="fsub_purge#cancel",
                style=BTN_PRIMARY,
            )
        ],
    ]

    ask_msg = await safe_reply_text(
        message,
        "⚠️ <b>Are you sure you want to delete all join requests?</b>\n\n"
        "👉 <i>Click a button below or reply with <code>y</code> / <code>n</code>:</i>",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        user_id = (
            message.from_user.id
            if message.from_user
            else getattr(message.sender_chat, "id", 0)
        )
        resp = await native_listen(bot, message.chat.id, user_id, timeout=60)
        if not resp or not resp.text:
            return
        try:
            await resp.delete()
        except Exception:
            pass

        if resp.text.strip().lower() in ["y", "yes"]:
            count = await clear_all_join_requests()
            if ask_msg:
                await safe_edit_text(
                    ask_msg,
                    f"✅ <b>All join requests purged successfully!</b> (<code>{count}</code> deleted)",
                    parse_mode=enums.ParseMode.HTML,
                )
        else:
            if ask_msg:
                await safe_edit_text(
                    ask_msg,
                    "❌ <b>Operation cancelled.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    except Exception as e:
        await safe_reply_text(
            message,
            f"⚠️ Error clearing requests: <code>{e}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
