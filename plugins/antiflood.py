import asyncio
import html
import inspect
import time
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatAdminRequired,
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
# ⚙️ MONGODB STORAGE & IN-MEMORY CACHE FOR ANTI-FLOOD
# ============================================================
_flood_db = None
_active_db = None
_std_conn_db = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _flood_db = _BOT_DB["antiflood_settings"]
    _active_db = _BOT_DB["active_connections"]
    _std_conn_db = _BOT_DB["CONNECTION"]
except Exception as e:
    logger.error(f"Failed to init antiflood DB: {e}")

# 60-second config cache: {chat_id: (timestamp, config_dict)}
_FLOOD_CONFIG_CACHE: Dict[int, Tuple[float, Dict[str, Any]]] = {}

# Live message tracker: {(chat_id, user_id): [msg_timestamps]}
_USER_MSG_TRACKER: Dict[Tuple[int, int], List[float]] = {}
_WARN_COOLDOWN: Dict[Tuple[int, int], float] = {}


async def get_flood_config(
    chat_id: int, force_refresh: bool = False
) -> Dict[str, Any]:
    cid = int(chat_id)
    now = time.time()
    if not force_refresh and cid in _FLOOD_CONFIG_CACHE:
        ts, cached = _FLOOD_CONFIG_CACHE[cid]
        if now - ts < 60:
            return cached

    default_cfg = {
        "chat_id": cid,
        "enabled": True,
        "limit": int(getattr(info, "FLOOD_LIMIT", 5)),
        "window": int(getattr(info, "FLOOD_TIME", 5)),
        "action": str(getattr(info, "FLOOD_ACTION", "delete")),  # delete | mute | kick
        "mute_duration": 300,
    }
    if _flood_db is not None:
        try:
            doc = await _flood_db.find_one({"chat_id": cid})
            if doc:
                for k, v in default_cfg.items():
                    if k not in doc:
                        doc[k] = v
                _FLOOD_CONFIG_CACHE[cid] = (now, doc)
                return doc
        except Exception:
            pass

    _FLOOD_CONFIG_CACHE[cid] = (now, default_cfg)
    return default_cfg


async def update_flood_config(chat_id: int, updates: Dict[str, Any]):
    cid = int(chat_id)
    cur = await get_flood_config(cid, force_refresh=True)
    cur.update(updates)
    if _flood_db is not None:
        try:
            await _flood_db.update_one(
                {"chat_id": cid}, {"$set": updates}, upsert=True
            )
        except Exception as e:
            logger.error(f"update_flood_config error: {e}")
    _FLOOD_CONFIG_CACHE[cid] = (time.time(), cur)


# ============================================================
# 🛡️ SAFE CHAT PERMISSIONS BUILDER
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


async def _unmute_later(client: Client, chat_id: int, user_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await client.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=build_chat_permissions(can_send=True),
        )
    except Exception:
        pass


async def _delete_later(msg: Message, delay: int = 8):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


# ============================================================
# 👑 SMART AUTHORIZATION & CONNECTED GROUP ROUTING
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


async def is_cb_admin(
    client: Client, query: CallbackQuery, chat_id: int
) -> bool:
    if not query or not query.from_user:
        return False
    uid = query.from_user.id
    if uid in get_admin_list():
        return True
    try:
        member = await client.get_chat_member(int(chat_id), uid)
        return member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]
    except Exception:
        return False


# ============================================================
# 🎛️ ANTI-FLOOD CONFIGURATION COMMANDS & DASHBOARD
# ============================================================
async def _build_flood_panel(chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = await get_flood_config(chat_id, force_refresh=True)
    is_en = bool(cfg.get("enabled", True))
    limit = int(cfg.get("limit", 5))
    window = int(cfg.get("window", 5))
    action = str(cfg.get("action", "delete")).upper()

    text = (
        f"🛡️ <b>Anti-Flood Protection Settings</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Group ID:</b> <code>{chat_id}</code>\n"
        f"📡 <b>Status:</b> <code>{'🟢 ENABLED' if is_en else '🔴 DISABLED'}</code>\n"
        f"⚡ <b>Trigger Limit:</b> <code>{limit} messages</code> in <code>{window}s</code>\n"
        f"🔨 <b>Penalty Action:</b> <code>{action}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Commands:</b>\n"
        f"• <code>/setflood &lt;messages&gt; [seconds]</code>\n"
        f"• <code>/floodmode &lt;delete|mute|kick&gt;</code>"
    )

    toggle_lbl = "🔴 Disable Anti-Flood" if is_en else "🟢 Enable Anti-Flood"
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    toggle_lbl, callback_data=f"af_ctl:toggle:{chat_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"{'✅ ' if action == 'DELETE' else ''}🗑️ Delete",
                    callback_data=f"af_ctl:mode:delete:{chat_id}",
                ),
                InlineKeyboardButton(
                    f"{'✅ ' if action == 'MUTE' else ''}🔇 Mute 5m",
                    callback_data=f"af_ctl:mode:mute:{chat_id}",
                ),
                InlineKeyboardButton(
                    f"{'✅ ' if action == 'KICK' else ''}👢 Kick",
                    callback_data=f"af_ctl:mode:kick:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton("🔐 Close", callback_data=f"af_ctl:close:{chat_id}")
            ],
        ]
    )
    return text, buttons


@Client.on_message(
    filters.command(["antiflood", "flood"]) & filters.incoming, group=-2
)
async def antiflood_panel_cmd(client: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> in PM or run this command inside your group.",
            parse_mode=enums.ParseMode.HTML,
        )

    if not await is_user_admin(client, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Group Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) > 1:
        arg = message.command[1].strip().lower()
        if arg in ["on", "enable", "true", "yes"]:
            await update_flood_config(grp_id, {"enabled": True})
        elif arg in ["off", "disable", "false", "no"]:
            await update_flood_config(grp_id, {"enabled": False})

    text, markup = await _build_flood_panel(grp_id)
    await safe_reply_text(
        message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("setflood") & filters.incoming, group=-2)
async def set_flood_limit_cmd(client: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )

    if not await is_user_admin(client, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/setflood &lt;messages&gt; [window_seconds]</code>\n"
            "<b>Example:</b> <code>/setflood 5 5</code> (or <code>/setflood off</code>)",
            parse_mode=enums.ParseMode.HTML,
        )

    arg1 = message.command[1].strip().lower()
    if arg1 in ["off", "0", "disable", "false"]:
        await update_flood_config(grp_id, {"enabled": False})
        return await safe_reply_text(
            message,
            f"🔴 <b>Anti-Flood disabled for group <code>{grp_id}</code>.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        limit = max(2, int(arg1))
        window = (
            max(2, int(message.command[2])) if len(message.command) > 2 else 5
        )
    except ValueError:
        return await safe_reply_text(
            message,
            "❌ <b>Please provide valid integers.</b> Example: <code>/setflood 5 5</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    await update_flood_config(
        grp_id, {"enabled": True, "limit": limit, "window": window}
    )
    text, markup = await _build_flood_panel(grp_id)
    await safe_reply_text(
        message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("floodmode") & filters.incoming, group=-2)
async def set_flood_mode_cmd(client: Client, message: Message):
    grp_id = await resolve_target_group(message)
    if not grp_id:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active group connected!</b>\nUse <code>/connect &lt;group_id&gt;</code> first.",
            parse_mode=enums.ParseMode.HTML,
        )

    if not await is_user_admin(client, message, target_chat_id=grp_id):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2 or message.command[1].lower() not in [
        "delete",
        "mute",
        "kick",
    ]:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/floodmode &lt;delete|mute|kick&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    mode = message.command[1].lower()
    await update_flood_config(grp_id, {"action": mode})
    text, markup = await _build_flood_panel(grp_id)
    await safe_reply_text(
        message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


@Client.on_callback_query(filters.regex(r"^af_ctl:"), group=-2)
async def antiflood_callbacks(client: Client, query: CallbackQuery):
    parts = query.data.split(":")
    action = parts[1]
    chat_id = int(parts[-1])

    if not await is_cb_admin(client, query, chat_id):
        return await safe_answer(
            query, "❌ Unauthorized: Group Admin only!", show_alert=True
        )

    if action == "close":
        await safe_answer(query, "Closed!")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "toggle":
        cfg = await get_flood_config(chat_id, force_refresh=True)
        new_state = not bool(cfg.get("enabled", True))
        await update_flood_config(chat_id, {"enabled": new_state})
        await safe_answer(
            query,
            f"🛡️ Anti-Flood {'ENABLED' if new_state else 'DISABLED'}!",
            show_alert=False,
        )
    elif action == "mode":
        new_mode = parts[2].lower()
        await update_flood_config(chat_id, {"action": new_mode})
        await safe_answer(
            query, f"🔨 Penalty set to: {new_mode.upper()}", show_alert=False
        )

    text, markup = await _build_flood_panel(chat_id)
    await safe_edit_text(
        query.message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


# ============================================================
# ⚡ REAL-TIME GROUP ANTI-FLOOD ENFORCER
# ============================================================
@Client.on_message(
    filters.group & filters.incoming & ~filters.service & ~filters.bot,
    group=-6,
)
async def group_antiflood_enforcer(client: Client, message: Message):
    user = message.from_user
    if not user or not message.chat:
        return

    chat_id = message.chat.id
    user_id = user.id

    cfg = await get_flood_config(chat_id)
    if not cfg.get("enabled", True):
        return

    limit = int(cfg.get("limit", 5))
    window = int(cfg.get("window", 5))
    if limit <= 0:
        return

    now = time.time()
    key = (chat_id, user_id)

    history = _USER_MSG_TRACKER.get(key, [])
    history = [ts for ts in history if now - ts <= window]
    history.append(now)
    _USER_MSG_TRACKER[key] = history

    if len(_USER_MSG_TRACKER) > 5000:
        _USER_MSG_TRACKER.clear()

    if len(history) <= limit:
        return

    # Bypass admins only when threshold is exceeded (saves API calls on normal messages)
    if await is_user_admin(client, message, target_chat_id=chat_id):
        _USER_MSG_TRACKER.pop(key, None)
        return

    # Delete flooding message
    try:
        await message.delete()
    except Exception:
        pass

    action = str(cfg.get("action", "delete")).lower()
    mute_dur = int(cfg.get("mute_duration", 300))

    if action == "mute":
        try:
            await client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=build_chat_permissions(can_send=False),
            )
            asyncio.create_task(_unmute_later(client, chat_id, user_id, mute_dur))
        except (ChatAdminRequired, FloodWait, Exception):
            pass
    elif action == "kick":
        try:
            await client.ban_chat_member(chat_id, user_id)
            await asyncio.sleep(1)
            await client.unban_chat_member(chat_id, user_id)
        except (ChatAdminRequired, FloodWait, Exception):
            pass

    last_warn = _WARN_COOLDOWN.get(key, 0.0)
    if now - last_warn >= 15.0:
        _WARN_COOLDOWN[key] = now
        penalty_note = (
            "🔇 You have been muted for 5 minutes."
            if action == "mute"
            else (
                "👢 You have been kicked from the group."
                if action == "kick"
                else "🗑️ Please slow down and avoid spamming!"
            )
        )
        warn_msg = await safe_reply_text(
            message,
            f"⚠️ <b>Flooding Detected, {user.mention}!</b>\n{penalty_note}",
            parse_mode=enums.ParseMode.HTML,
        )
        if warn_msg:
            asyncio.create_task(_delete_later(warn_msg, 8))

    raise StopPropagation
