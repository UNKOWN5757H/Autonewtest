import asyncio
import html
import inspect
import time
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    QueryIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info

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
# ⚙️ ISOLATED MONGODB ENGINE & IN-MEMORY CACHE
# ============================================================
fa_config_db = None
fa_settings_sync_db = None
fa_users_db = None
_active_db = None
_std_conn_db = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    fa_config_db = _BOT_DB["forceadd_config"]
    fa_settings_sync_db = _BOT_DB["force_add_settings"]
    fa_users_db = _BOT_DB["forceadd_users"]
    _active_db = _BOT_DB["active_connections"]
    _std_conn_db = _BOT_DB["CONNECTION"]
except Exception as e:
    logger.error(f"Failed to init Force Add DB: {e}")

# Fast 60s in-memory cache so every group message does not hit MongoDB
_FA_CONFIG_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}


def invalidate_fa_cache(chat_id: int):
    _FA_CONFIG_CACHE.pop(int(chat_id), None)


async def set_fa_config(
    chat_id: int,
    limit: Optional[int] = None,
    mode: Optional[str] = None,
    enabled: Optional[bool] = None,
):
    cid = int(chat_id)
    cur = await get_fa_config(cid, force_refresh=True)

    new_limit = cur["limit"] if limit is None else int(limit)
    new_mode = cur["mode"] if mode is None else str(mode)
    if enabled is None:
        new_enabled = new_limit > 0
    else:
        new_enabled = bool(enabled)
        if new_enabled and new_limit <= 0:
            new_limit = int(cur.get("saved_limit") or 5)

    saved_limit = new_limit if new_limit > 0 else int(cur.get("saved_limit") or 5)

    payload = {
        "chat_id": cid,
        "limit": new_limit if new_enabled else 0,
        "saved_limit": saved_limit,
        "mode": new_mode,
        "enabled": new_enabled,
        "is_enabled": new_enabled,
    }

    if fa_config_db is not None:
        try:
            await fa_config_db.update_one(
                {"chat_id": cid}, {"$set": payload}, upsert=True
            )
        except Exception as e:
            logger.error(f"set_fa_config error: {e}")

    # Sync with force_add_settings collection read by plugins/uibuttons.py
    if fa_settings_sync_db is not None:
        try:
            await fa_settings_sync_db.update_one(
                {"chat_id": cid}, {"$set": payload}, upsert=True
            )
        except Exception:
            pass

    _FA_CONFIG_CACHE[cid] = (time.time(), payload)


async def get_fa_config(chat_id: int, force_refresh: bool = False) -> Dict[str, Any]:
    cid = int(chat_id)
    now = time.time()
    if not force_refresh and cid in _FA_CONFIG_CACHE:
        ts, cached_doc = _FA_CONFIG_CACHE[cid]
        if now - ts < 60:
            return cached_doc

    doc = None
    if fa_config_db is not None:
        try:
            doc = await fa_config_db.find_one({"chat_id": cid})
        except Exception:
            doc = None

    sync_doc = None
    if fa_settings_sync_db is not None:
        try:
            sync_doc = await fa_settings_sync_db.find_one({"chat_id": cid})
        except Exception:
            sync_doc = None

    if doc or sync_doc:
        base = doc or sync_doc or {}
        limit_val = int(base.get("limit", 0))
        saved_limit = int(
            base.get("saved_limit") or (limit_val if limit_val > 0 else 5)
        )
        mode_val = str(base.get("mode", "all"))

        # Check if toggled on/off via uibuttons.py
        if sync_doc and "enabled" in sync_doc:
            is_en = bool(sync_doc["enabled"])
        elif "enabled" in base:
            is_en = bool(base["enabled"])
        else:
            is_en = limit_val > 0

        effective_limit = (limit_val if limit_val > 0 else saved_limit) if is_en else 0
        res = {
            "limit": effective_limit,
            "saved_limit": saved_limit,
            "mode": mode_val,
            "enabled": is_en and effective_limit > 0,
        }
        _FA_CONFIG_CACHE[cid] = (now, res)
        return res

    default_res = {"limit": 0, "saved_limit": 5, "mode": "all", "enabled": False}
    _FA_CONFIG_CACHE[cid] = (now, default_res)
    return default_res


async def add_user_score(chat_id: int, user_id: int, count: int):
    if fa_users_db is not None and count > 0:
        timestamps = [int(time.time())] * count
        await fa_users_db.update_one(
            {"chat_id": int(chat_id), "user_id": int(user_id)},
            {"$push": {"adds": {"$each": timestamps}}},
            upsert=True,
        )


async def get_user_score(chat_id: int, user_id: int) -> int:
    if fa_users_db is not None:
        doc = await fa_users_db.find_one(
            {"chat_id": int(chat_id), "user_id": int(user_id)}
        )
        if doc and "adds" in doc:
            return len(doc["adds"])
    return 0


async def mark_new_user(chat_id: int, user_id: int):
    if fa_users_db is not None:
        await fa_users_db.update_one(
            {"chat_id": int(chat_id), "user_id": int(user_id)},
            {"$set": {"is_new": True}},
            upsert=True,
        )


async def is_new_user(chat_id: int, user_id: int) -> bool:
    if fa_users_db is not None:
        doc = await fa_users_db.find_one(
            {"chat_id": int(chat_id), "user_id": int(user_id)}
        )
        return bool(doc.get("is_new", False)) if doc else False
    return False


# ============================================================
# 🛡️ SAFE CHAT PERMISSIONS BUILDER (CROSS-VERSION COMPATIBLE)
# ============================================================
_VALID_PERM_PARAMS = set(
    inspect.signature(ChatPermissions.__init__).parameters.keys()
) - {"self"}


def build_chat_permissions(can_send: bool) -> ChatPermissions:
    desired = {
        "can_send_messages": can_send,
        "can_send_media_messages": can_send,
        "can_send_other_messages": can_send,
        "can_add_web_page_previews": can_send,
        "can_send_audios": can_send,
        "can_send_documents": can_send,
        "can_send_photos": can_send,
        "can_send_videos": can_send,
        "can_send_video_notes": can_send,
        "can_send_voice_notes": can_send,
        "can_send_polls": can_send,
        "can_invite_users": True,
    }
    valid_kwargs = {k: v for k, v in desired.items() if k in _VALID_PERM_PARAMS}
    return ChatPermissions(**valid_kwargs)


async def _delete_later(msg: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


# ============================================================
# 🛡️ ANTI-SPAM WARNING CACHE
# ============================================================
class TTLCache:
    def __init__(self, maxsize: int = 5000, ttl: int = 60):
        self._data: Dict[str, float] = {}
        self._maxsize = maxsize
        self._ttl = ttl

    def is_rate_limited(self, key: str) -> bool:
        now = time.time()
        if key in self._data and now < self._data[key]:
            return True
        self._cleanup(now)
        if len(self._data) >= self._maxsize:
            self._data.clear()
        self._data[key] = now + self._ttl
        return False

    def _cleanup(self, now: float):
        expired = [k for k, exp in self._data.items() if now >= exp]
        for k in expired:
            del self._data[k]


WARN_CACHE = TTLCache(ttl=60)


# ============================================================
# 👑 SMART AUTHORIZATION & ACTIVE CONNECTION ROUTING
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


async def get_active_connected_group(user_id: int) -> Optional[int]:
    uid_str = str(user_id)
    try:
        from plugins.pm_filter import get_active_connection

        conn = await get_active_connection(uid_str)
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


async def resolve_target_group(message: Message) -> Optional[int]:
    """Resolves target group ID whether command is run in Group or Connected PM."""
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return int(message.chat.id)
    if message.from_user:
        return await get_active_connected_group(message.from_user.id)
    return None


async def is_user_admin(
    client: Client, message: Message, target_chat_id: Optional[int] = None
) -> bool:
    if not message:
        return False
    admin_list = get_admin_list()

    if message.from_user and message.from_user.id in admin_list:
        return True

    if message.sender_chat and message.sender_chat.id in admin_list:
        return True

    check_cid = target_chat_id or (
        message.chat.id
        if message.chat
        and message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
        else None
    )

    if check_cid:
        if message.sender_chat and message.sender_chat.id == check_cid:
            return True
        if message.from_user:
            try:
                member = await client.get_chat_member(
                    int(check_cid), message.from_user.id
                )
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
            except Exception:
                pass

    return False


async def is_callback_user_admin(
    client: Client, query: CallbackQuery, chat_id: int = None
) -> bool:
    if not query or not query.from_user:
        return False
    user_id = query.from_user.id
    if user_id in get_admin_list():
        return True

    target_chat_id = chat_id or (query.message.chat.id if query.message else None)
    if target_chat_id:
        try:
            member = await client.get_chat_member(int(target_chat_id), user_id)
            return member.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]
        except Exception:
            pass
    return False


# ============================================================
# ⚙️ CONFIGURATION COMMANDS (WORK IN BOTH GROUP & CONNECTED PM)
# ============================================================
@Client.on_message(filters.command("setforceadd") & filters.incoming, group=-2)
async def set_force_add(bot: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> in PM or run this command inside your group.",
            parse_mode=enums.ParseMode.HTML,
        )

    if not await is_user_admin(bot, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Group Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/setforceadd &lt;number&gt;</code>\n"
            "<b>Example:</b> <code>/setforceadd 5</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        limit = int(message.command[1])
        if limit < 0:
            raise ValueError
    except ValueError:
        return await safe_reply_text(
            message,
            "❌ <b>Please provide a valid positive number.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    if limit == 0:
        await set_fa_config(grp_id, limit=0, mode="all", enabled=False)
        return await safe_reply_text(
            message,
            f"🗑️ <b>Force Add disabled for group <code>{grp_id}</code>.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 Force Add for ALL Members",
                    callback_data=f"fa_set_{limit}_all_{grp_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🆕 Force Add for ONLY NEW Members",
                    callback_data=f"fa_set_{limit}_new_{grp_id}",
                )
            ],
        ]
    )

    await safe_reply_text(
        message,
        f"🎯 <b>Target Group:</b> <code>{grp_id}</code>\n"
        f"🔢 <b>Required Invites:</b> <code>{limit}</code>\n\n"
        "<b>Who should this requirement apply to?</b>",
        reply_markup=kb,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^fa_set_(\d+)_([a-z]+)_(-?\d+)$"), group=-2)
async def set_forceadd_callback(bot: Client, query: CallbackQuery):
    limit = int(query.matches[0].group(1))
    mode = query.matches[0].group(2)
    grp_id = int(query.matches[0].group(3))

    if not await is_callback_user_admin(bot, query, chat_id=grp_id):
        return await safe_answer(
            query,
            "❌ Unauthorized: Only group admins can configure Force Add!",
            show_alert=True,
        )

    await set_fa_config(grp_id, limit=limit, mode=mode, enabled=True)
    mode_text = (
        "ALL MEMBERS" if mode == "all" else "ONLY NEW MEMBERS (who join from now on)"
    )

    await safe_answer(query, "✅ Force Add Configured!")
    await safe_edit_text(
        query.message,
        f"✅ <b>Force Add Configured Successfully!</b>\n\n"
        f"🏘️ <b>Group ID:</b> <code>{grp_id}</code>\n"
        f"🔢 <b>Limit:</b> <code>{limit} members</code>\n"
        f"🎯 <b>Target:</b> <code>{mode_text}</code>\n"
        f"<i>(Engine is now 100% Active)</i>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remforceadd") & filters.incoming, group=-2)
async def remove_force_add(bot: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_user_admin(bot, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    await set_fa_config(grp_id, limit=0, mode="all", enabled=False)
    await safe_reply_text(
        message,
        f"🗑️ <b>Force Add requirement has been completely removed for <code>{grp_id}</code>.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("getforceadd") & filters.incoming, group=-2)
async def get_force_add(bot: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )

    settings = await get_fa_config(grp_id, force_refresh=True)
    if settings["limit"] == 0 or not settings.get("enabled", False):
        await safe_reply_text(
            message,
            f"ℹ️ <b>Force Add is currently DISABLED in group <code>{grp_id}</code>.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        target = "Everyone" if settings["mode"] == "all" else "Only New Members"
        await safe_reply_text(
            message,
            f"ℹ️ <b>Force Add Configuration (<code>{grp_id}</code>):</b>\n\n"
            f"🔢 <b>Required Adds:</b> <code>{settings['limit']}</code> members\n"
            f"🎯 <b>Applies to:</b> <code>{target}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# 📊 LEADERBOARD & STATS LOGIC
# ============================================================
async def generate_leaderboard(
    bot: Client, message: Message, title: str, time_limit_seconds: int = None
):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )

    if fa_users_db is None:
        return await safe_reply_text(
            message, "❌ <b>Database Error.</b>", parse_mode=enums.ParseMode.HTML
        )

    pipeline = [{"$match": {"chat_id": int(grp_id)}}]

    if time_limit_seconds:
        cutoff = int(time.time()) - time_limit_seconds
        pipeline.append(
            {
                "$project": {
                    "user_id": 1,
                    "valid_adds": {
                        "$filter": {
                            "input": {"$ifNull": ["$adds", []]},
                            "as": "timestamp",
                            "cond": {"$gte": ["$$timestamp", cutoff]},
                        }
                    },
                }
            }
        )
        pipeline.append(
            {
                "$project": {
                    "user_id": 1,
                    "score": {"$size": {"$ifNull": ["$valid_adds", []]}},
                }
            }
        )
    else:
        pipeline.append(
            {"$project": {"user_id": 1, "score": {"$size": {"$ifNull": ["$adds", []]}}}}
        )

    pipeline.extend(
        [{"$match": {"score": {"$gt": 0}}}, {"$sort": {"score": -1}}, {"$limit": 10}]
    )
    cursor = fa_users_db.aggregate(pipeline)
    top_users = await cursor.to_list(length=10)

    if not top_users:
        return await safe_reply_text(
            message,
            f"📊 <b>{title}</b> (<code>{grp_id}</code>)\n\nNo members have added anyone yet!",
            parse_mode=enums.ParseMode.HTML,
        )

    text = f"📊 <b>{title} (Top 10)</b>\n🆔 <b>Group:</b> <code>{grp_id}</code>\n\n"
    for i, data in enumerate(top_users, 1):
        uid, score = data["user_id"], data["score"]
        try:
            user = await bot.get_users(uid)
            user_name = user.mention if user else f"User {uid}"
        except Exception:
            user_name = f"User <code>{uid}</code>"
        text += f"<b>{i}.</b> {user_name} ➔ <code>{score}</code> added\n"

    await safe_reply_text(
        message, text, disable_web_page_preview=True, parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("topaddall") & filters.incoming, group=-2)
async def top_add_all(bot: Client, message: Message):
    await generate_leaderboard(bot, message, "All-Time Top Adders", None)


@Client.on_message(filters.command("topadd24") & filters.incoming, group=-2)
async def top_add_24(bot: Client, message: Message):
    await generate_leaderboard(bot, message, "Top Adders (Past 24 Hours)", 86400)


@Client.on_message(filters.command("topadd7") & filters.incoming, group=-2)
async def top_add_7(bot: Client, message: Message):
    await generate_leaderboard(bot, message, "Top Adders (Past 7 Days)", 604800)


@Client.on_message(filters.command("resetadddaily") & filters.incoming, group=-2)
async def reset_add_daily(bot: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_user_admin(bot, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if fa_users_db is not None:
        cutoff = int(time.time()) - 86400
        await fa_users_db.update_many(
            {"chat_id": int(grp_id)}, {"$pull": {"adds": {"$lt": cutoff}}}
        )
        await safe_reply_text(
            message,
            f"♻️ <b>Daily limits reset for <code>{grp_id}</code>!</b> Leaderboards for past 24h have been wiped.",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("resetadd") & filters.incoming, group=-2)
async def reset_all_adds(bot: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not await is_user_admin(bot, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if fa_users_db is not None:
        await fa_users_db.update_many({"chat_id": int(grp_id)}, {"$set": {"adds": []}})
        await safe_reply_text(
            message,
            f"💥 <b>TOTAL RESET for <code>{grp_id}</code>!</b> All members' scores are now 0. Everyone must add members again.",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("myadds") & filters.incoming, group=-2)
async def my_adds(bot: Client, message: Message):
    if not message.from_user:
        return
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>Use this command inside a group or connect a group first with <code>/connect</code>.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    settings = await get_fa_config(grp_id)
    current_adds = await get_user_score(grp_id, message.from_user.id)

    if settings["limit"] == 0:
        return await safe_reply_text(
            message,
            f"ℹ️ <b>Force Add is not active in <code>{grp_id}</code>.</b>\n"
            f"👥 <b>Your Total Invites:</b> <code>{current_adds}</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    if current_adds >= settings["limit"]:
        await safe_reply_text(
            message,
            f"✅ You have added <b>{current_adds}</b> members. You are cleared to chat freely!",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await safe_reply_text(
            message,
            f"⚠️ You have added <b>{current_adds}/{settings['limit']}</b> members.",
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# 🎯 THE ENGINE: TRACKER & ENFORCER
# ============================================================
@Client.on_message(filters.new_chat_members & filters.group, group=10)
async def track_added_members(bot: Client, message: Message):
    for u in message.new_chat_members:
        if not u.is_bot:
            await mark_new_user(message.chat.id, u.id)

    if not message.from_user:
        return

    settings = await get_fa_config(message.chat.id)
    if settings["limit"] == 0:
        return

    adder_id = message.from_user.id
    added_others = [
        u for u in message.new_chat_members if u.id != adder_id and not u.is_bot
    ]

    if not added_others:
        return

    await add_user_score(message.chat.id, adder_id, len(added_others))
    current_adds = await get_user_score(message.chat.id, adder_id)

    if current_adds >= settings["limit"]:
        try:
            await bot.restrict_chat_member(
                chat_id=message.chat.id,
                user_id=adder_id,
                permissions=build_chat_permissions(can_send=True),
            )
            msg = await bot.send_message(
                chat_id=message.chat.id,
                text=(
                    f"🎉 <b>Congratulations {message.from_user.mention}!</b>\n\n"
                    f"You've added <b>{current_adds}</b> members and met the group requirement. "
                    f"<b>You can now chat freely!</b>"
                ),
                parse_mode=enums.ParseMode.HTML,
            )
            asyncio.create_task(_delete_later(msg, 15))
        except Exception:
            pass


@Client.on_message(
    filters.group
    & ~filters.service
    & ~filters.new_chat_members
    & ~filters.left_chat_member,
    group=-1,
)
async def enforce_force_add(bot: Client, message: Message):
    if await is_user_admin(bot, message, target_chat_id=message.chat.id):
        return

    user = message.from_user
    if not user or user.is_bot:
        return
    chat_id, user_id = message.chat.id, user.id

    settings = await get_fa_config(chat_id)
    limit = settings["limit"]
    if limit == 0:
        return

    if settings["mode"] == "new" and not await is_new_user(chat_id, user_id):
        return

    text = message.text or message.caption
    if text and text.startswith("/"):
        return

    current_adds = await get_user_score(chat_id, user_id)

    if current_adds < limit:
        try:
            await message.delete()
        except Exception:
            pass

        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=build_chat_permissions(can_send=False),
            )
        except Exception:
            pass

        warn_key = f"warn_{chat_id}_{user_id}"
        if not WARN_CACHE.is_rate_limited(warn_key):
            try:
                warn_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🛑 <b>Hold on, {user.mention}!</b>\n\n"
                        f"You must add <b>{limit - current_adds} more member(s)</b> to this group before you can send messages.\n\n"
                        f"🔇 <b>You have been restricted from messaging.</b>"
                    ),
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "📊 How many users have I added?",
                                    callback_data="forceadd_check",
                                )
                            ]
                        ]
                    ),
                    parse_mode=enums.ParseMode.HTML,
                )
                asyncio.create_task(_delete_later(warn_msg, 60))
            except Exception as e:
                logger.error(f"ForceAdd warning error: {e}")

        raise StopPropagation


@Client.on_callback_query(filters.regex(r"^forceadd_check$"), group=-2)
async def check_adds_button(bot: Client, query: CallbackQuery):
    if not query.message or not query.from_user:
        return
    chat_id, user_id = query.message.chat.id, query.from_user.id
    settings = await get_fa_config(chat_id)

    if settings["limit"] == 0:
        return await safe_answer(
            query, "Force Add is not active in this group.", show_alert=True
        )

    current_adds = await get_user_score(chat_id, user_id)
    if current_adds >= settings["limit"]:
        await safe_answer(
            query,
            f"✅ You have added {current_adds} members.\nYou are cleared to chat freely!",
            show_alert=True,
        )
    else:
        await safe_answer(
            query,
            f"⚠️ You have added {current_adds}/{settings['limit']} members.\n\nYou need {settings['limit'] - current_adds} more to chat.",
            show_alert=True,
        )
