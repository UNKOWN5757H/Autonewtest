import asyncio
import html
import inspect
import os
import platform
import shutil
import sys
import time
from logging import ERROR, getLogger
from typing import List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    InputUserDeactivated,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    QueryIdInvalid,
    UserDeactivated,
    UserDeactivatedBan,
    UserIsBlocked,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from database.users_chats_db import db as _db
from utils import temp

# Safe optional imports so missing modules never break plugin loading
try:
    from database.connections_mdb import add_connection as _std_add_connection
    from database.connections_mdb import all_connections as _std_all_connections
    from database.connections_mdb import delete_connection as _std_delete_connection
    from database.connections_mdb import if_active as _std_if_active
    from database.connections_mdb import make_active as _std_make_active
    from database.connections_mdb import make_inactive as _std_make_inactive
except ImportError:
    _std_add_connection = None
    _std_all_connections = None
    _std_delete_connection = None
    _std_if_active = None
    _std_make_active = None
    _std_make_inactive = None

try:
    from database.ia_filterdb import Media as _Media
    from database.ia_filterdb import db as _db1_handle
    from database.ia_filterdb import (
        get_all_configured_dbs,
        get_all_dbs_stats,
        nuke_all_media_dbs,
    )
except ImportError:
    _Media = None
    _db1_handle = None
    get_all_configured_dbs = None
    get_all_dbs_stats = None
    nuke_all_media_dbs = None

try:
    from database.plugin_dbs import plugin_db as _plugin_db
except ImportError:
    _plugin_db = None

try:
    import psutil
except ImportError:
    psutil = None

logger = getLogger(__name__)
logger.setLevel(ERROR)

if not hasattr(info, "REPAIR_MODE"):
    info.REPAIR_MODE = False

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
):
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
    query: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
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
# ⚙️ MONGODB FALLBACK COLLECTIONS
# ============================================================
_DB_CLIENT = None
_BOT_DB = None
_active_conn_col = None
_std_conn_col = None
_ban_col = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _active_conn_col = _BOT_DB["active_connections"]
    _std_conn_col = _BOT_DB["CONNECTION"]
    _ban_col = _BOT_DB["banned_users"]
except Exception as e:
    logger.error(f"Failed to init admincore fallback DB: {e}")

# Flag to allow cancelling long-running /cleanusers task
_CLEAN_USERS_CANCEL = False


# ============================================================
# 👑 SMART AUTHORIZATION
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


async def quick_admin_check(client: Client, message: Message) -> bool:
    if not message:
        return False
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if await is_global_admin(uid):
        return True
    if message.chat and message.chat.type in [
        enums.ChatType.GROUP,
        enums.ChatType.SUPERGROUP,
    ]:
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


def get_size_str(bytes_size) -> str:
    if not bytes_size or bytes_size < 0:
        return "0.00 B"
    size = float(bytes_size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


async def get_total_files_count() -> int:
    if callable(get_all_dbs_stats):
        try:
            st = await get_all_dbs_stats()
            return int(st.get("total_files", 0))
        except Exception:
            pass
    if _Media is None:
        return 0
    try:
        col = getattr(_Media, "collection", _Media)
        return await col.count_documents({})
    except Exception:
        try:
            return await _Media.count_documents({})
        except Exception:
            return 0


# ============================================================
# 🔗 UNIFIED CONNECTION ENGINE (SYNCS ALL SCHEMAS)
# ============================================================
async def safe_add_connection(group_id: str, user_id: str) -> bool:
    uid, gid = str(user_id), str(group_id)
    added = False
    if callable(_std_add_connection):
        try:
            added = bool(await _std_add_connection(gid, uid))
        except Exception as e:
            logger.error(f"std add_connection error: {e}")

    if _std_conn_col is not None:
        try:
            await _std_conn_col.update_one(
                {"_id": uid},
                {
                    "$addToSet": {
                        "group_details": {"group_id": gid},
                        "group_ids": gid,
                    },
                    "$set": {"active_group": gid},
                },
                upsert=True,
            )
            added = True
        except Exception:
            pass

    await safe_make_active(uid, gid)
    return added


async def safe_make_active(user_id: str, group_id: str):
    uid, gid = str(user_id), str(group_id)
    if callable(_std_make_active):
        try:
            await _std_make_active(uid, gid)
        except Exception:
            pass
    if _std_conn_col is not None:
        try:
            await _std_conn_col.update_one(
                {"_id": uid}, {"$set": {"active_group": gid}}, upsert=True
            )
        except Exception:
            pass
    if _active_conn_col is not None:
        try:
            await _active_conn_col.update_one(
                {"user_id": uid},
                {"$set": {"group_id": gid, "active_group": gid}},
                upsert=True,
            )
        except Exception:
            pass


async def safe_make_inactive(user_id: str):
    uid = str(user_id)
    if callable(_std_make_inactive):
        try:
            await _std_make_inactive(uid)
        except Exception:
            pass
    if _std_conn_col is not None:
        try:
            await _std_conn_col.update_one(
                {"_id": uid}, {"$set": {"active_group": None}}
            )
        except Exception:
            pass
    if _active_conn_col is not None:
        try:
            await _active_conn_col.delete_one({"user_id": uid})
        except Exception:
            pass


async def safe_delete_connection(user_id: str, group_id: str) -> bool:
    uid, gid = str(user_id), str(group_id)
    deleted = False
    if callable(_std_delete_connection):
        try:
            deleted = bool(await _std_delete_connection(uid, gid))
        except Exception:
            pass

    if _std_conn_col is not None:
        try:
            doc = await _std_conn_col.find_one({"_id": uid})
            if doc:
                await _std_conn_col.update_one(
                    {"_id": uid},
                    {
                        "$pull": {
                            "group_details": {"group_id": gid},
                            "group_ids": gid,
                        }
                    },
                )
                if str(doc.get("active_group")) == gid:
                    await _std_conn_col.update_one(
                        {"_id": uid}, {"$set": {"active_group": None}}
                    )
                deleted = True
        except Exception:
            pass

    if _active_conn_col is not None:
        try:
            res = await _active_conn_col.delete_many({"user_id": uid, "group_id": gid})
            if res.deleted_count:
                deleted = True
        except Exception:
            pass

    return deleted


async def safe_all_connections(user_id: str) -> list:
    uid = str(user_id)
    groups = []
    if callable(_std_all_connections):
        try:
            res = await _std_all_connections(uid)
            if res:
                groups.extend([str(g) for g in res if g])
        except Exception:
            pass

    if _std_conn_col is not None:
        try:
            doc = await _std_conn_col.find_one({"_id": uid})
            if doc:
                for item in doc.get("group_details", []):
                    if isinstance(item, dict) and item.get("group_id"):
                        groups.append(str(item["group_id"]))
                for gid in doc.get("group_ids", []):
                    if gid:
                        groups.append(str(gid))
                if doc.get("active_group"):
                    groups.append(str(doc["active_group"]))
        except Exception:
            pass

    if _active_conn_col is not None:
        try:
            doc = await _active_conn_col.find_one({"user_id": uid})
            if doc and doc.get("group_id"):
                groups.append(str(doc["group_id"]))
        except Exception:
            pass

    return list(dict.fromkeys(groups))


async def safe_if_active(user_id: str, group_id: str) -> bool:
    uid, gid = str(user_id), str(group_id)
    if callable(_std_if_active):
        try:
            if await _std_if_active(uid, gid):
                return True
        except Exception:
            pass
    if _std_conn_col is not None:
        try:
            doc = await _std_conn_col.find_one({"_id": uid})
            if doc and str(doc.get("active_group")) == gid:
                return True
        except Exception:
            pass
    if _active_conn_col is not None:
        try:
            doc = await _active_conn_col.find_one({"user_id": uid})
            if doc and str(doc.get("group_id")) == gid:
                return True
        except Exception:
            pass
    return False


# ============================================================
# 🔗 CONNECTION COMMANDS & CALLBACKS
# ============================================================
@Client.on_message(filters.command("connect") & filters.incoming, group=-2)
async def addconnection_cmd(client: Client, message: Message):
    uid = str(
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await quick_admin_check(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if message.chat.type == enums.ChatType.PRIVATE:
        if len(message.command) < 2:
            return await safe_reply_text(
                message,
                "⚠️ <b>Usage in PM:</b> <code>/connect &lt;group_id&gt;</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        group_id = message.command[1].strip()
        try:
            chat = await client.get_chat(int(group_id))
            title = html.escape(chat.title or "Group")
            if not await is_global_admin(int(uid)):
                member = await client.get_chat_member(int(group_id), int(uid))
                if member.status not in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]:
                    return await safe_reply_text(
                        message,
                        f"❌ You are not an Admin in <code>{title}</code>",
                        parse_mode=enums.ParseMode.HTML,
                    )
        except Exception as e:
            return await safe_reply_text(
                message,
                f"❌ Invalid Group ID or I'm not in that group!\n<b>Error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        group_id = str(message.chat.id)
        title = html.escape(message.chat.title or "Group")

    await safe_add_connection(group_id, uid)
    await safe_reply_text(
        message,
        f"✅ <b>Successfully connected & activated:</b>\n"
        f"📢 <code>{title}</code> (<code>{group_id}</code>)\n\n"
        f"You can now manage this group directly from my PM!",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("disconnect") & filters.incoming, group=-2)
async def deleteconnection_cmd(client: Client, message: Message):
    uid = str(
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await quick_admin_check(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if message.chat.type == enums.ChatType.PRIVATE:
        if len(message.command) < 2:
            return await safe_reply_text(
                message,
                "⚠️ <b>Usage in PM:</b> <code>/disconnect &lt;group_id&gt;</code>\n"
                "Or use <code>/connections</code> to manage them interactively.",
                parse_mode=enums.ParseMode.HTML,
            )
        group_id = message.command[1].strip()
    else:
        group_id = str(message.chat.id)

    if await safe_delete_connection(uid, group_id):
        await safe_reply_text(
            message,
            f"✅ <b>Successfully disconnected from:</b> <code>{html.escape(group_id)}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await safe_reply_text(
            message,
            "⚠️ This chat isn't in your connections list!",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("connections") & filters.incoming, group=-2)
async def connections_cmd(client: Client, message: Message):
    uid = str(
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await quick_admin_check(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    groups = await safe_all_connections(uid)
    if not groups:
        return await safe_reply_text(
            message,
            "⚠️ <b>No active connections!</b>\nUse <code>/connect &lt;group_id&gt;</code> to add one.",
            parse_mode=enums.ParseMode.HTML,
        )

    await generate_connections_menu(client, message, uid, is_edit=False)


@Client.on_callback_query(
    filters.regex(r"^(groupcb|connectcb|inactcb|disconnectcb|backcb|conn_close)"),
    group=-2,
)
async def connection_callbacks(client: Client, query: CallbackQuery):
    data = query.data
    uid = str(query.from_user.id)

    try:
        if data == "conn_close":
            await safe_answer(query, "Closed!")
            return await query.message.delete()

        if data.startswith("groupcb:"):
            group_id = data.split(":", 1)[1]
            await safe_answer(query)
            try:
                chat_obj = await client.get_chat(int(group_id))
                title = html.escape(chat_obj.title or "Unknown Group")
            except Exception:
                title = "Unknown Group"

            is_act = await safe_if_active(uid, group_id)
            status_str = "🟢 ACTIVE" if is_act else "⚪️ INACTIVE"

            toggle_btn = (
                InlineKeyboardButton(
                    "📴 Make Inactive", callback_data=f"inactcb:{group_id}"
                )
                if is_act
                else InlineKeyboardButton(
                    "✅ Make Active", callback_data=f"connectcb:{group_id}"
                )
            )

            buttons = [
                [toggle_btn],
                [
                    InlineKeyboardButton(
                        "🗑️ Disconnect", callback_data=f"disconnectcb:{group_id}"
                    )
                ],
                [
                    InlineKeyboardButton("🔙 Back to List", callback_data="backcb"),
                    InlineKeyboardButton("🔐 Close", callback_data="conn_close"),
                ],
            ]
            await safe_edit_text(
                query.message,
                f"⚙️ <b>Manage Connection:</b>\n\n"
                f"📢 <b>Group:</b> <code>{title}</code>\n"
                f"🆔 <b>ID:</b> <code>{group_id}</code>\n"
                f"📡 <b>Status:</b> <code>{status_str}</code>",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=enums.ParseMode.HTML,
            )

        elif data.startswith("connectcb:"):
            group_id = data.split(":", 1)[1]
            await safe_make_active(uid, group_id)
            await safe_answer(query, "✅ Connection Activated!", show_alert=True)
            await generate_connections_menu(client, query.message, uid, is_edit=True)

        elif data.startswith("inactcb:"):
            await safe_make_inactive(uid)
            await safe_answer(query, "📴 Connection Deactivated!", show_alert=True)
            await generate_connections_menu(client, query.message, uid, is_edit=True)

        elif data.startswith("disconnectcb:"):
            group_id = data.split(":", 1)[1]
            await safe_delete_connection(uid, group_id)
            await safe_answer(query, "🗑️ Connection Deleted!", show_alert=True)
            await generate_connections_menu(client, query.message, uid, is_edit=True)

        elif data == "backcb":
            await safe_answer(query)
            await generate_connections_menu(client, query.message, uid, is_edit=True)
    except Exception as e:
        logger.error(f"Connection callback error: {e}")


async def generate_connections_menu(
    client: Client, message: Message, uid: str, is_edit: bool = True
):
    groupids = await safe_all_connections(uid)
    if not groupids:
        text = (
            "⚠️ <b>No active connections!</b>\n"
            "Use <code>/connect &lt;group_id&gt;</code> to add one."
        )
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔐 Close", callback_data="conn_close")]]
        )
        if is_edit:
            return await safe_edit_text(
                message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
            )
        return await safe_reply_text(
            message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )

    buttons = []
    for gid in groupids:
        try:
            chat_obj = await client.get_chat(int(gid))
            title = chat_obj.title or f"Group {gid}"
            is_act = await safe_if_active(uid, str(gid))
            prefix = "🟢" if is_act else "⚪️"
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{prefix} {title[:32]}", callback_data=f"groupcb:{gid}"
                    )
                ]
            )
        except Exception:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"⚠️ Unknown ({gid})", callback_data=f"groupcb:{gid}"
                    )
                ]
            )

    buttons.append([InlineKeyboardButton("🔐 Close", callback_data="conn_close")])
    text = (
        "🔗 <b>Your Connected Groups:</b>\n\n"
        "<i>(Click a group below to Activate, Deactivate, or Disconnect it)</i>"
    )
    if is_edit:
        await safe_edit_text(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await safe_reply_text(
            message,
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# ⚡ OPTIMIZE DATABASE INDEXES COMMAND (/optimize_db)
# ============================================================
@Client.on_message(filters.command("optimize_db") & filters.incoming, group=-2)
async def optimize_db_cmd(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    sts = await safe_reply_text(
        message,
        "⚡ <b>Optimizing MongoDB Indexes across all configured databases...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    created_indexes = []
    col_name = getattr(info, "COLLECTION_NAME", "VersionQ")
    try:
        try:
            from plugins.datadb import load_datadb_config

            await load_datadb_config()
        except Exception:
            pass

        if callable(get_all_configured_dbs):
            all_dbs = get_all_configured_dbs(include_when_disabled=True)
        else:
            all_dbs = (
                [(1, _db1_handle, "Primary DB")] if _db1_handle is not None else []
            )

        for idx, db_h, lbl in all_dbs:
            if db_h is not None:
                try:
                    idx_res = await db_h[col_name].create_index([("file_name", 1)])
                    created_indexes.append(f"DB #{idx} ({lbl}) Media: {idx_res}")
                except Exception:
                    pass

        if _BOT_DB is not None:
            idx2 = await _BOT_DB["active_connections"].create_index([("user_id", 1)])
            created_indexes.append(f"Connections: {idx2}")
            idx3 = await _BOT_DB["fsub_join_requests"].create_index(
                [("user_id", 1), ("chat_id", 1)]
            )
            created_indexes.append(f"FSub Requests: {idx3}")

        summary = "\n".join(
            f"• <code>{html.escape(str(x))}</code>" for x in created_indexes
        )
        if sts:
            await safe_edit_text(
                sts,
                f"✅ <b>Database Optimization Complete!</b>\n\n{summary or 'All indexes verified.'}",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        if sts:
            await safe_edit_text(
                sts,
                f"⚠️ <b>Optimization Notice:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


# ============================================================
# ℹ️ USER INFO COMMAND
# ============================================================
@Client.on_message(filters.command(["info", "id"]) & filters.incoming)
async def who_is(client: Client, message: Message):
    is_admin = await quick_admin_check(client, message)
    status = await safe_reply_text(
        message, "🔍 <b>Fetching user info...</b>", parse_mode=enums.ParseMode.HTML
    )

    try:
        user = None
        if message.reply_to_message and message.reply_to_message.from_user:
            user = message.reply_to_message.from_user
        elif len(message.command) > 1 and is_admin:
            target = message.text.split(None, 1)[1].strip()
            try:
                user = await client.get_users(
                    int(target) if target.lstrip("-").isdigit() else target
                )
            except Exception:
                pass

        if not user:
            user = message.from_user
        if not user:
            if status:
                return await safe_edit_text(
                    status,
                    f"🆔 <b>Chat ID:</b> <code>{message.chat.id}</code>\n"
                    "⚠️ <i>Reply to a user or provide a User ID to inspect user details.</i>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        first_name = html.escape(user.first_name or "None")
        last_name = html.escape(user.last_name or "None")
        username = f"@{html.escape(user.username)}" if user.username else "None"

        text = (
            f"<b>👤 First Name:</b> {first_name}\n"
            f"<b>🧾 Last Name:</b> {last_name}\n"
            f"<b>🆔 Telegram ID:</b> <code>{user.id}</code>\n"
            f"<b>💬 Chat ID:</b> <code>{message.chat.id}</code>\n"
            f"<b>🌐 Data Centre:</b> <code>{user.dc_id or 'N/A'}</code>\n"
            f"<b>🔗 Username:</b> {username}\n"
            f"<b>✨ Mention:</b> <a href='tg://user?id={user.id}'>Click Here</a>\n"
        )

        if message.chat.type in (
            enums.ChatType.SUPERGROUP,
            enums.ChatType.CHANNEL,
            enums.ChatType.GROUP,
        ):
            try:
                member = await client.get_chat_member(message.chat.id, user.id)
                if member.joined_date:
                    text += f"<b>📅 Joined Chat On:</b> <code>{member.joined_date.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            except Exception:
                pass

        btn_rows = []
        uid_caller = message.from_user.id if message.from_user else 0
        if await is_global_admin(uid_caller) and not await is_global_admin(user.id):
            banned = await check_user_banned(user.id)
            if banned:
                btn_rows.append(
                    [
                        InlineKeyboardButton(
                            "✅ Unban User", callback_data=f"adm_unban:{user.id}"
                        )
                    ]
                )
            else:
                btn_rows.append(
                    [
                        InlineKeyboardButton(
                            "🚫 Ban User", callback_data=f"adm_ban:{user.id}"
                        )
                    ]
                )

        btn_rows.append([InlineKeyboardButton("🔐 Close", callback_data="close_data")])
        markup = InlineKeyboardMarkup(btn_rows)

        if user.photo:
            try:
                await message.reply_photo(
                    photo=user.photo.big_file_id,
                    caption=text,
                    reply_markup=markup,
                    parse_mode=enums.ParseMode.HTML,
                )
                if status:
                    await status.delete()
                return
            except Exception:
                pass

        if status:
            await safe_edit_text(
                status, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"Info Command Error: {e}")
        if status:
            await safe_edit_text(
                status,
                "❌ <b>An error occurred while fetching user data.</b>",
                parse_mode=enums.ParseMode.HTML,
            )


# ============================================================
# ⚙️ INTERACTIVE ADMIN SETTINGS & REPAIR MODE
# ============================================================
async def _build_admin_settings_panel() -> Tuple[str, InlineKeyboardMarkup]:
    repair_on = bool(getattr(info, "REPAIR_MODE", False))
    react_on = bool(getattr(info, "ENABLE_REACTION", True))

    fsub_on = bool(getattr(info, "IS_FSUB_ENABLED", True))
    fsub_ch_count = 0
    try:
        from plugins.fsub import get_fsub_config

        f_cfg = await get_fsub_config()
        fsub_on = bool(f_cfg.get("is_enabled", True))
        fsub_ch_count = len(f_cfg.get("channels", {}))
    except Exception:
        pass

    repair_status = "🟢 ENABLED" if repair_on else "🔴 DISABLED"
    fsub_status = "🟢 ENABLED" if fsub_on else "🔴 DISABLED"
    react_status = "🟢 ENABLED" if react_on else "🔴 DISABLED"
    auth_ch = html.escape(str(getattr(info, "AUTH_CHANNEL", None) or "❌ Not Set"))
    req_ch = html.escape(str(getattr(info, "REQ_CHANNEL", None) or "❌ Not Set"))
    admin_count = len(get_admin_list())

    text = (
        "⚙️ <b>Current Admin Settings Dashboard</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛠️ <b>Repair Mode:</b> <code>{repair_status}</code>\n"
        f"🔐 <b>Force Subscribe:</b> <code>{fsub_status}</code> (<code>{fsub_ch_count}</code> Dynamic Channels)\n"
        f"❤️ <b>Auto Reactions:</b> <code>{react_status}</code>\n\n"
        f"📢 <b>Main FSub Channel:</b> <code>{auth_ch}</code>\n"
        f"📨 <b>Request FSub Channel:</b> <code>{req_ch}</code>\n\n"
        f"👥 <b>Total Admins Configured:</b> <code>{admin_count}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Use the buttons below to toggle settings live.</i>"
    )

    repair_btn_lbl = "🔴 Disable Repair Mode" if repair_on else "🟢 Enable Repair Mode"
    fsub_btn_lbl = "🔴 Disable ForceSub" if fsub_on else "🟢 Enable ForceSub"
    react_btn_lbl = "🔴 Disable Reactions" if react_on else "🟢 Enable Reactions"

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    repair_btn_lbl, callback_data="admset:toggle_repair"
                )
            ],
            [InlineKeyboardButton(fsub_btn_lbl, callback_data="admset:toggle_fsub")],
            [InlineKeyboardButton(react_btn_lbl, callback_data="admset:toggle_react")],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="admset:refresh"),
                InlineKeyboardButton("🔐 Close", callback_data="admset:close"),
            ],
        ]
    )
    return text, markup


@Client.on_message(filters.command("repairmode") & filters.incoming, group=-2)
async def toggle_repair_mode(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> You must be a Global Bot Admin.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) > 1:
        arg = message.command[1].lower()
        if arg in ["on", "true", "enable"]:
            info.REPAIR_MODE = True
        elif arg in ["off", "false", "disable"]:
            info.REPAIR_MODE = False
        else:
            info.REPAIR_MODE = not getattr(info, "REPAIR_MODE", False)
    else:
        info.REPAIR_MODE = not getattr(info, "REPAIR_MODE", False)

    status = "🟢 <b>ENABLED</b>" if info.REPAIR_MODE else "🔴 <b>DISABLED</b>"
    text = f"🛠️ <b>Repair Mode is now {status}</b>\n\n"
    if info.REPAIR_MODE:
        text += "⚠️ <i>The bot is now in maintenance mode. It will NOT send any files to users until this is disabled.</i>"
    else:
        text += "✅ <i>The bot has returned to normal operation and will process file requests.</i>"
    await safe_reply_text(message, text, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("adminsettings") & filters.incoming, group=-2)
async def show_admin_settings(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> You must be a Global Bot Admin.",
            parse_mode=enums.ParseMode.HTML,
        )

    text, markup = await _build_admin_settings_panel()
    await safe_reply_text(
        message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


@Client.on_callback_query(
    filters.regex(r"^admset:(toggle_repair|toggle_fsub|toggle_react|refresh|close)$"),
    group=-2,
)
async def admin_settings_cb(client: Client, query: CallbackQuery):
    if not await is_global_admin(query.from_user.id):
        return await safe_answer(
            query, "❌ Unauthorized: Global Admin only!", show_alert=True
        )

    action = query.matches[0].group(1)
    try:
        if action == "close":
            await safe_answer(query, "Closed!")
            return await query.message.delete()
        elif action == "toggle_repair":
            info.REPAIR_MODE = not getattr(info, "REPAIR_MODE", False)
            await safe_answer(
                query,
                f"🛠️ Repair Mode {'ENABLED' if info.REPAIR_MODE else 'DISABLED'}!",
                show_alert=True,
            )
        elif action == "toggle_fsub":
            try:
                from plugins.fsub import get_fsub_config, update_fsub_config

                cfg = await get_fsub_config(force_refresh=True)
                new_fsub = not bool(cfg.get("is_enabled", True))
                await update_fsub_config("is_enabled", new_fsub)
                info.IS_FSUB_ENABLED = new_fsub
            except Exception:
                info.IS_FSUB_ENABLED = not getattr(info, "IS_FSUB_ENABLED", True)
            await safe_answer(
                query,
                f"🔐 Force Subscribe {'ENABLED' if info.IS_FSUB_ENABLED else 'DISABLED'}!",
                show_alert=True,
            )
        elif action == "toggle_react":
            info.ENABLE_REACTION = not getattr(info, "ENABLE_REACTION", True)
            await safe_answer(
                query,
                f"❤️ Auto Reactions {'ENABLED' if info.ENABLE_REACTION else 'DISABLED'}!",
                show_alert=True,
            )
        else:
            await safe_answer(query, "Refreshed!")

        text, markup = await _build_admin_settings_panel()
        await safe_edit_text(
            query.message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Admin settings callback error: {e}")


# ============================================================
# 🚫 UNBREAKABLE BAN SYSTEM (MULTI-DB SYNC)
# ============================================================
async def check_user_banned(user_id: int) -> bool:
    uid = int(user_id)
    if hasattr(temp, "BANNED_USERS") and uid in temp.BANNED_USERS:
        return True
    if _plugin_db is not None and hasattr(_plugin_db, "is_banned"):
        try:
            if await _plugin_db.is_banned(uid):
                return True
        except Exception:
            pass
    if _ban_col is not None:
        try:
            doc = await _ban_col.find_one({"_id": uid})
            if doc:
                return True
        except Exception:
            pass
    return False


async def perform_ban_user(user_id: int):
    uid = int(user_id)
    if hasattr(temp, "BANNED_USERS") and isinstance(temp.BANNED_USERS, list):
        if uid not in temp.BANNED_USERS:
            temp.BANNED_USERS.append(uid)
    if _plugin_db is not None and hasattr(_plugin_db, "ban_user"):
        try:
            await _plugin_db.ban_user(uid)
        except Exception:
            pass
    if _ban_col is not None:
        try:
            await _ban_col.update_one(
                {"_id": uid}, {"$set": {"banned_at": int(time.time())}}, upsert=True
            )
        except Exception:
            pass


async def perform_unban_user(user_id: int):
    uid = int(user_id)
    if hasattr(temp, "BANNED_USERS") and uid in temp.BANNED_USERS:
        try:
            temp.BANNED_USERS.remove(uid)
        except ValueError:
            pass
    if _plugin_db is not None and hasattr(_plugin_db, "unban_user"):
        try:
            await _plugin_db.unban_user(uid)
        except Exception:
            pass
    if _ban_col is not None:
        try:
            await _ban_col.delete_one({"_id": uid})
        except Exception:
            pass


async def get_total_banned_count() -> int:
    counts = []
    if _plugin_db is not None and hasattr(_plugin_db, "get_ban_count"):
        try:
            counts.append(int(await _plugin_db.get_ban_count()))
        except Exception:
            pass
    if _ban_col is not None:
        try:
            counts.append(int(await _ban_col.count_documents({})))
        except Exception:
            pass
    if hasattr(temp, "BANNED_USERS"):
        counts.append(len(temp.BANNED_USERS))
    return max(counts) if counts else 0


@Client.on_message(filters.command("ban") & filters.incoming, group=-2)
async def ban_user_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized: Global Admin Only.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1:
        target = message.command[1].strip()
        if target.lstrip("-").isdigit():
            user_id = int(target)
        else:
            try:
                u_obj = await bot.get_users(target)
                user_id = u_obj.id
            except Exception:
                pass

    if not user_id:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/ban [user_id or @username]</code> or reply to a user.",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        if await is_global_admin(user_id):
            return await safe_reply_text(
                message,
                "❌ <b>You cannot ban a bot administrator!</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        bot_id = bot.me.id if getattr(bot, "me", None) else (await bot.get_me()).id
        if user_id == bot_id:
            return await safe_reply_text(
                message,
                "❌ <b>I cannot ban myself!</b>",
                parse_mode=enums.ParseMode.HTML,
            )

        await perform_ban_user(user_id)
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Unban User", callback_data=f"adm_unban:{user_id}"
                    )
                ]
            ]
        )
        await safe_reply_text(
            message,
            f"🚫 <b>User <code>{user_id}</code> has been successfully BANNED.</b>\n"
            f"They can no longer use this bot.",
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("unban") & filters.incoming, group=-2)
async def unban_user_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized: Global Admin Only.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1:
        target = message.command[1].strip()
        if target.lstrip("-").isdigit():
            user_id = int(target)
        else:
            try:
                u_obj = await bot.get_users(target)
                user_id = u_obj.id
            except Exception:
                pass

    if not user_id:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/unban [user_id or @username]</code> or reply to a user.",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        if not await check_user_banned(user_id):
            return await safe_reply_text(
                message,
                f"⚠️ <b>User <code>{user_id}</code> is not currently banned.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        await perform_unban_user(user_id)
        await safe_reply_text(
            message,
            f"✅ <b>User <code>{user_id}</code> has been successfully UNBANNED.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_callback_query(filters.regex(r"^adm_(ban|unban):(\d+)$"), group=-2)
async def inline_ban_unban_cb(client: Client, query: CallbackQuery):
    if not await is_global_admin(query.from_user.id):
        return await safe_answer(
            query, "❌ Unauthorized: Global Admin only!", show_alert=True
        )

    action, raw_uid = query.matches[0].group(1), int(query.matches[0].group(2))
    if action == "ban":
        if await is_global_admin(raw_uid):
            return await safe_answer(query, "❌ Cannot ban an admin!", show_alert=True)
        await perform_ban_user(raw_uid)
        await safe_answer(query, f"🚫 User {raw_uid} banned!", show_alert=True)
        try:
            await query.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Unban User", callback_data=f"adm_unban:{raw_uid}"
                            )
                        ],
                        [InlineKeyboardButton("🔐 Close", callback_data="close_data")],
                    ]
                )
            )
        except Exception:
            pass
    else:
        await perform_unban_user(raw_uid)
        await safe_answer(query, f"✅ User {raw_uid} unbanned!", show_alert=True)
        try:
            await query.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🚫 Ban User", callback_data=f"adm_ban:{raw_uid}"
                            )
                        ],
                        [InlineKeyboardButton("🔐 Close", callback_data="close_data")],
                    ]
                )
            )
        except Exception:
            pass


@Client.on_message(filters.command("bannedusers") & filters.incoming, group=-2)
async def check_banned_users(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    try:
        count = await get_total_banned_count()
        await safe_reply_text(
            message,
            f"📊 <b>Total Banned Users:</b> <code>{count}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# 📜 LOGS & SERVER COMMANDS
# ============================================================
@Client.on_message(filters.command("logs") & filters.incoming, group=-2)
async def get_logs_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return
    log_file = "TelegramBot.log"
    if not os.path.exists(log_file):
        return await safe_reply_text(
            message,
            "⚠️ <b>Log file not found!</b> No errors recorded yet.",
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        await message.reply_document(
            document=log_file,
            caption="📜 <b>Here are the latest bot logs.</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Failed to send logs:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


async def _build_server_stats_text() -> str:
    text = "🖥 <b>Server Statistics</b>\n━━━━━━━━━━━━━━\n"
    if psutil:
        cpu_pct = await asyncio.to_thread(psutil.cpu_percent, 0.2)
        ram = psutil.virtual_memory()
        text += (
            f"🧠 <b>CPU Usage:</b> <code>{cpu_pct}%</code>\n"
            f"📉 <b>RAM Usage:</b> <code>{ram.percent}%</code>\n"
            f"💾 <b>RAM Total:</b> <code>{get_size_str(ram.total)}</code>\n"
            f"💿 <b>RAM Free:</b> <code>{get_size_str(ram.available)}</code>\n\n"
        )
    else:
        text += (
            f"🧠 <b>CPU Cores:</b> <code>{os.cpu_count()}</code>\n"
            f"💻 <b>OS System:</b> <code>{platform.system()} {platform.release()}</code>\n\n"
        )

    total, used, free = shutil.disk_usage("/")
    text += (
        f"💽 <b>Disk Total:</b> <code>{get_size_str(total)}</code>\n"
        f"📀 <b>Disk Used:</b> <code>{get_size_str(used)}</code> (<code>{(used / total) * 100:.1f}%</code>)\n"
        f"💿 <b>Disk Free:</b> <code>{get_size_str(free)}</code>\n"
        "━━━━━━━━━━━━━━"
    )
    return text


@Client.on_message(filters.command("server") & filters.incoming, group=-2)
async def server_stats_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    msg = await safe_reply_text(
        message,
        "⏳ <b>Fetching server statistics...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    text = await _build_server_stats_text()
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="srv_refresh"),
                InlineKeyboardButton("🔐 Close", callback_data="close_data"),
            ]
        ]
    )
    if msg:
        await safe_edit_text(
            msg, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )


@Client.on_callback_query(filters.regex(r"^srv_refresh$"), group=-2)
async def server_refresh_cb(client: Client, query: CallbackQuery):
    if not await is_global_admin(query.from_user.id):
        return await safe_answer(query, "❌ Unauthorized.", show_alert=True)
    await safe_answer(query, "Refreshing server stats...")
    text = await _build_server_stats_text()
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="srv_refresh"),
                InlineKeyboardButton("🔐 Close", callback_data="close_data"),
            ]
        ]
    )
    await safe_edit_text(
        query.message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("restart") & filters.incoming, group=-2)
async def restart_bot_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    await safe_reply_text(
        message,
        "⚠️ <b>Are you sure you want to restart the bot?</b>",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Confirm Restart", callback_data="util_restart"
                    ),
                    InlineKeyboardButton("❌ Cancel", callback_data="close_data"),
                ]
            ]
        ),
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^util_restart$"), group=-2)
async def confirm_restart_cb(bot: Client, query: CallbackQuery):
    uid = query.from_user.id
    if not await is_global_admin(uid):
        return await safe_answer(query, "❌ Unauthorized.", show_alert=True)

    await safe_answer(query, "♻️ Restarting...", show_alert=True)
    msg = await safe_edit_text(
        query.message,
        "♻️ <b>Bot is restarting... Please wait.</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        target_msg = msg or query.message
        with open("restart.txt", "w") as f:
            f.write(f"{target_msg.chat.id}\n{target_msg.id}")
    except Exception:
        pass
    os.execl(sys.executable, sys.executable, *sys.argv)


# ============================================================
# 📊 DYNAMIC MULTI-DATABASE LIVE STATS & INSTANT CLEANUP
# ============================================================
def _format_single_db_block(label: str, db_info: dict) -> str:
    safe_lbl = html.escape(str(label))
    if not db_info.get("configured"):
        return f"🗄️ <b>{safe_lbl}:</b> <code>⚪ Not Configured</code>\n"
    status = db_info.get("status", "🟢 Connected")
    files = db_info.get("files", 0)
    used_str = get_size_str(db_info.get("used_bytes", 0))
    free_str = get_size_str(db_info.get("free_bytes", 0))
    return (
        f"🗄️ <b>{safe_lbl}:</b> <code>{status}</code>\n"
        f"├ 📁 <b>Files:</b> <code>{files:,}</code>\n"
        f"├ 💾 <b>Used:</b> <code>{used_str} / 512.00 MB</code>\n"
        f"└ 💿 <b>Free:</b> <code>{free_str}</code>\n"
    )


async def _build_db_stats_text() -> str:
    # Ensure all saved /datadb URLs are loaded so every database is included
    try:
        from plugins.datadb import load_datadb_config

        await load_datadb_config()
    except Exception:
        pass

    total_users = 0
    total_chats = 0
    try:
        total_users, total_chats = await asyncio.gather(
            _db.total_users_count(), _db.total_chat_count()
        )
    except Exception:
        pass

    if callable(get_all_dbs_stats):
        st = await get_all_dbs_stats()
        all_dbs_list = st.get("all_dbs") or []
        if not all_dbs_list:
            for idx_k in ("db1", "db2", "db3"):
                if st.get(idx_k) and st[idx_k].get("configured"):
                    all_dbs_list.append(st[idx_k])

        total_files = st.get("total_files", 0)
        total_used = get_size_str(st.get("total_used_bytes", 0))
        total_free = get_size_str(st.get("total_free_bytes", 0))
        total_cap = get_size_str(st.get("total_capacity_bytes", 536870912))
        active_idx = st.get("active_db_index", 1)
        multi_on = st.get("multi_db_enabled", True)
        db_count = len(all_dbs_list)

        db_blocks = []
        for d_item in all_dbs_list:
            d_idx = d_item.get("index", 1)
            raw_lbl = d_item.get("label") or f"Database #{d_idx}"
            display_lbl = (
                f"DB #{d_idx} ({raw_lbl})"
                if not str(raw_lbl).lower().startswith("db")
                else raw_lbl
            )
            db_blocks.append(_format_single_db_block(display_lbl, d_item))

        mode_str = "🟢 ON" if multi_on else "🔴 OFF (Primary Only)"
        return (
            f"📊 <b>Multi-Database Cluster Statistics ({db_count} DB{'s' if db_count != 1 else ''})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>Total Users:</b> <code>{total_users:,}</code>\n"
            f"🏘 <b>Total Groups:</b> <code>{total_chats:,}</code>\n"
            f"📁 <b>Combined Total Files:</b> <code>{total_files:,}</code>\n"
            f"⚙️ <b>Multi-DB Mode:</b> <code>{mode_str}</code>\n"
            f"⚡ <b>Active Write DB:</b> <code>Database #{active_idx}</code>\n"
            f"💾 <b>Total Used:</b> <code>{total_used} / {total_cap}</code>\n"
            f"💿 <b>Total Free:</b> <code>{total_free}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(db_blocks)
            + "━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <i>Manage unlimited DB URLs anytime with <code>/datadb</code></i>"
        )

    total_files = await get_total_files_count()
    db_size = await _db.get_db_size()
    return (
        f"📊 <b>Bot Database Statistics</b>\n━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Users:</b> <code>{total_users:,}</code>\n"
        f"🏘 <b>Total Groups:</b> <code>{total_chats:,}</code>\n"
        f"📁 <b>Total Files:</b> <code>{total_files:,}</code>\n"
        f"💾 <b>DB Size:</b> <code>{get_size_str(db_size)}</code>\n━━━━━━━━━━━━━━"
    )


@Client.on_message(filters.command(["stats", "total"]) & filters.incoming, group=-2)
async def bot_stats_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    status_msg = await safe_reply_text(
        message,
        "⏳ <b>Fetching Multi-Database Live Stats...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        stats_text = await _build_db_stats_text()
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh", callback_data="adm_stats_refresh"
                    ),
                    InlineKeyboardButton("🔐 Close", callback_data="close_data"),
                ]
            ]
        )
        if status_msg:
            await safe_edit_text(
                status_msg,
                stats_text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        if status_msg:
            await safe_edit_text(
                status_msg,
                f"❌ <b>Error fetching stats:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_callback_query(filters.regex(r"^adm_stats_refresh$"), group=-2)
async def adm_stats_refresh_cb(client: Client, query: CallbackQuery):
    if not await is_global_admin(query.from_user.id):
        return await safe_answer(query, "❌ Unauthorized.", show_alert=True)
    await safe_answer(query, "Refreshing Multi-DB stats...")
    try:
        stats_text = await _build_db_stats_text()
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh", callback_data="adm_stats_refresh"
                    ),
                    InlineKeyboardButton("🔐 Close", callback_data="close_data"),
                ]
            ]
        )
        await safe_edit_text(
            query.message,
            stats_text,
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception:
        pass


@Client.on_message(filters.command("cleanusers") & filters.incoming, group=-2)
async def clean_users_cmd(bot: Client, message: Message):
    global _CLEAN_USERS_CANCEL
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    _CLEAN_USERS_CANCEL = False
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛑 Stop Cleaning", callback_data="cancel_cleanusers")]]
    )
    status_msg = await safe_reply_text(
        message,
        "⏳ <b>Starting Deep Clean...</b> (Scanning dead/blocked accounts)",
        reply_markup=cancel_kb,
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        users_cursor = await _db.get_all_users()
        users = (
            await users_cursor.to_list(length=None)
            if hasattr(users_cursor, "to_list")
            else list(users_cursor)
        )
    except Exception as e:
        if status_msg:
            return await safe_edit_text(
                status_msg,
                f"❌ <b>Failed to fetch users:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        return

    active, blocked, total_users = 0, 0, len(users)

    async def check_user(u):
        user_id = u.get("id") or u.get("user_id")
        if not user_id:
            return "blocked"
        try:
            await bot.send_chat_action(int(user_id), enums.ChatAction.TYPING)
            return "active"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return "active"
        except (
            UserIsBlocked,
            InputUserDeactivated,
            UserDeactivated,
            PeerIdInvalid,
            UserDeactivatedBan,
        ):
            try:
                await _db.delete_user(int(user_id))
            except Exception:
                pass
            return "blocked"
        except Exception:
            return "active"

    for i in range(0, total_users, 30):
        if _CLEAN_USERS_CANCEL:
            if status_msg:
                return await safe_edit_text(
                    status_msg,
                    f"🛑 <b>Deep Clean Stopped by Admin!</b>\n\n"
                    f"🟢 <b>Active Scanned:</b> <code>{active}</code>\n"
                    f"🔴 <b>Dead Removed So Far:</b> <code>{blocked}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        chunk = users[i : i + 30]
        results = await asyncio.gather(*[check_user(u) for u in chunk])

        active += results.count("active")
        blocked += results.count("blocked")

        if (i + len(chunk)) % 150 == 0 or (i + len(chunk)) == total_users:
            if status_msg:
                await safe_edit_text(
                    status_msg,
                    f"⏳ <b>Cleaning Database...</b>\n"
                    f"📊 <b>Scanned:</b> <code>{i + len(chunk)} / {total_users}</code>\n"
                    f"🟢 <b>Active:</b> <code>{active}</code> | 🔴 <b>Removed:</b> <code>{blocked}</code>",
                    reply_markup=cancel_kb,
                    parse_mode=enums.ParseMode.HTML,
                )
        await asyncio.sleep(1.5)

    if status_msg:
        await safe_edit_text(
            status_msg,
            f"✅ <b>Deep Clean Completed!</b>\n\n"
            f"🟢 <b>Active Users Kept:</b> <code>{active}</code>\n"
            f"🔴 <b>Dead Accounts Removed:</b> <code>{blocked}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_callback_query(filters.regex(r"^cancel_cleanusers$"), group=-2)
async def cancel_cleanusers_cb(client: Client, query: CallbackQuery):
    global _CLEAN_USERS_CANCEL
    if not await is_global_admin(query.from_user.id):
        return await safe_answer(query, "❌ Unauthorized.", show_alert=True)
    _CLEAN_USERS_CANCEL = True
    await safe_answer(query, "🛑 Stopping user cleanup...", show_alert=True)


# ============================================================
# 🔥 INSTANT MULTI-DATABASE WIPE COMMANDS (/clearfiles, /clearusers)
# ============================================================
@Client.on_message(
    filters.command(["clearfiles", "clearusers"]) & filters.incoming,
    group=-2,
)
async def wipe_prompt_cmd(bot: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message, "❌ <b>Unauthorized.</b>", parse_mode=enums.ParseMode.HTML
        )

    cmd = message.command[0].lower()
    action = "files" if cmd == "clearfiles" else "users"
    target_desc = (
        "ALL MOVIE FILES ACROSS ALL CONFIGURED DATABASES (PRIMARY DB + ALL /datadb URLs)"
        if action == "files"
        else "ALL USERS"
    )

    await safe_reply_text(
        message,
        f"⚠️ <b>WARNING!</b> ⚠️\nAre you sure you want to permanently delete <b>{target_desc}</b>?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🔥 YES, DELETE ALL {action.upper()}",
                        callback_data=f"nuke_{action}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="close_data")],
            ]
        ),
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_callback_query(filters.regex(r"^nuke_(files|users|fsub)$"), group=-2)
async def nuke_callbacks(bot: Client, query: CallbackQuery):
    uid = query.from_user.id
    if not await is_global_admin(uid):
        return await safe_answer(query, "❌ Unauthorized.", show_alert=True)

    await safe_answer(query, "Wiping database...", show_alert=False)
    action = query.data.split("_")[1]
    await safe_edit_text(
        query.message,
        "⏳ <b>Wiping database... Please wait a second.</b>",
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        if action == "files":
            try:
                from plugins.datadb import load_datadb_config

                await load_datadb_config()
            except Exception:
                pass

            if callable(nuke_all_media_dbs):
                summary = await nuke_all_media_dbs()
                total_del = summary.get("total_deleted", 0)
                cleared_list = (
                    ", ".join(summary.get("dbs_cleared", [])) or "All Databases"
                )
                return await safe_edit_text(
                    query.message,
                    f"✅ <b>All Media Databases Wiped Instantly!</b>\n\n"
                    f"🗑️ <b>Total Deleted Files:</b> <code>{total_del:,}</code>\n"
                    f"🗄️ <b>Databases Cleared:</b> <code>{html.escape(cleared_list)}</code>\n"
                    f"💿 <b>Storage Status:</b> <code>100% Freed</code>",
                    parse_mode=enums.ParseMode.HTML,
                )

            if _Media is None:
                return await safe_edit_text(
                    query.message,
                    "❌ <b>Media collection unavailable.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            collection = getattr(_Media, "collection", _Media)
            await collection.drop()
            await safe_edit_text(
                query.message,
                "✅ <b>Database Wiped Instantly!</b>\n🗑 <b>Deleted Files:</b> <code>All</code>",
                parse_mode=enums.ParseMode.HTML,
            )

        elif action == "users":
            collection = getattr(_db, "col", _db)
            count = 0
            try:
                count = await collection.estimated_document_count()
            except Exception:
                pass
            try:
                await collection.drop()
            except Exception:
                res = await collection.delete_many({})
                count = getattr(res, "deleted_count", count)
            await safe_edit_text(
                query.message,
                f"✅ <b>Users Database Wiped!</b>\n🗑 <b>Deleted Users:</b> <code>{count:,}</code>",
                parse_mode=enums.ParseMode.HTML,
            )

        elif action == "fsub":
            if _plugin_db is not None and hasattr(_plugin_db, "clear_fsub_users"):
                await _plugin_db.clear_fsub_users()
            if _BOT_DB is not None:
                for col_name in await _BOT_DB.list_collection_names():
                    if "fsub" in col_name.lower() or "join_req" in col_name.lower():
                        try:
                            await _BOT_DB[col_name].drop()
                        except Exception:
                            await _BOT_DB[col_name].delete_many({})
            await safe_edit_text(
                query.message,
                "✅ <b>Force Subscribe Database has been completely cleared.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        logger.exception(f"Error during nuke_{action}")
        await safe_edit_text(
            query.message,
            f"❌ <b>Error occurred:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
