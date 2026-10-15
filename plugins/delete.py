import asyncio
import html
import inspect
import re
import time
from logging import ERROR, getLogger
from typing import List, Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
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
from database.ia_filterdb import Media as _Media
from database.ia_filterdb import unpack_new_file_id
from utils import save_group_settings, temp

# Safe optional imports
try:
    from database.plugin_dbs import plugin_db as _plugin_db
except ImportError:
    _plugin_db = None

try:
    from plugins.custom_settings import update_bot_settings
except ImportError:
    update_bot_settings = None

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
# ⚙️ MONGODB STORAGE & PERSISTENT COUNTDOWN QUEUE
# ============================================================
_del_db = None
_countdown_col = None
_active_db = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _del_db = _BOT_DB["deletion_settings"]
    _countdown_col = _BOT_DB["countdown_tasks"]
    _active_db = _BOT_DB["active_connections"]
except Exception as e:
    logger.error(f"Failed to init Deletion Settings DB: {e}")


def get_media_collection():
    """Safely returns the underlying Motor collection whether Media is a umongo Document or raw collection."""
    return getattr(_Media, "collection", _Media)


def _invalidate_pm_filter_search_cache():
    try:
        import plugins.pm_filter as _pmf

        _pmf.GLOBAL_SEARCH_CACHE._data.clear()
        _pmf.FUZZY_TITLES_CACHE._data.clear()
    except Exception:
        pass


async def get_del_setting(key: str, default: int) -> int:
    if _del_db is not None:
        try:
            doc = await _del_db.find_one({"_id": "timers"})
            if doc and key in doc and doc[key] is not None:
                return int(doc[key])
        except Exception:
            pass
    return int(getattr(info, key, default))


async def save_del_setting(key: str, value: int):
    setattr(info, key, value)
    if key == "FILE_AUTO_DELETE":
        setattr(info, "AUTO_DELETE_TIME", value)
        setattr(info, "AUTO_DELETE", value > 0)
        setattr(temp, "FILE_AUTO_DELETE", value)
    elif key == "BUTTON_AUTO_DELETE":
        setattr(info, "BUTTON_DEL", value)
        setattr(info, "BUTTON_DELETE_TIME", value)
        setattr(temp, "BUTTON_AUTO_DELETE", value)
        setattr(temp, "BUTTON_DEL", value)
        if callable(update_bot_settings):
            try:
                await update_bot_settings("button_del", value)
            except Exception:
                pass
        if _plugin_db is not None and hasattr(_plugin_db, "set_buttondel"):
            try:
                await _plugin_db.set_buttondel(value)
            except Exception:
                pass

    if _del_db is not None:
        try:
            await _del_db.update_one(
                {"_id": "timers"}, {"$set": {key: value}}, upsert=True
            )
        except Exception:
            pass


# ============================================================
# 👑 FULL POWER ADMIN CHECKER (GLOBAL + CHAT + ANONYMOUS)
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

    return False


async def get_connected_group_id(message: Message) -> Optional[int]:
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        return message.chat.id
    if not message.from_user:
        return None
    uid_str = str(message.from_user.id)
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
    return None


def format_countdown_string(seconds_left: int) -> str:
    if seconds_left <= 0:
        return "⏳ Deleting now..."
    m, s = divmod(seconds_left, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"⏳ Deleting in: {h}h {m}m"
    return f"⏳ Deleting in: {m}m {s}s"


# ============================================================
# ⏱️ ACTIVE LIVE COUNTDOWN BACKGROUND WORKER
# ============================================================
_COUNTDOWN_WORKER_STARTED = False


async def countdown_live_worker(client: Client):
    while True:
        try:
            if _countdown_col is not None:
                now = time.time()
                tasks = await _countdown_col.find().to_list(length=100)
                for task in tasks:
                    chat_id = task["chat_id"]
                    msg_id = task["message_id"]
                    remaining = int(task["delete_at"] - now)

                    if remaining <= 0:
                        try:
                            await client.delete_messages(chat_id, msg_id)
                        except Exception:
                            pass
                        await _countdown_col.delete_one({"_id": task["_id"]})
                        continue

                    last_update = task.get("last_updated", 0)
                    if now - last_update >= 30:
                        timer_label = format_countdown_string(remaining)
                        original_buttons = task.get("buttons", [])

                        new_keyboard = []
                        for row in original_buttons:
                            btn_row = []
                            for b in row:
                                if b.get("url"):
                                    btn_row.append(
                                        InlineKeyboardButton(b["text"], url=b["url"])
                                    )
                                elif b.get("callback_data"):
                                    btn_row.append(
                                        InlineKeyboardButton(
                                            b["text"], callback_data=b["callback_data"]
                                        )
                                    )
                            if btn_row:
                                new_keyboard.append(btn_row)
                        new_keyboard.append(
                            [
                                InlineKeyboardButton(
                                    timer_label, callback_data="noop_countdown"
                                )
                            ]
                        )

                        try:
                            await client.edit_message_reply_markup(
                                chat_id,
                                msg_id,
                                reply_markup=InlineKeyboardMarkup(new_keyboard),
                            )
                            await _countdown_col.update_one(
                                {"_id": task["_id"]}, {"$set": {"last_updated": now}}
                            )
                        except MessageNotModified:
                            await _countdown_col.update_one(
                                {"_id": task["_id"]}, {"$set": {"last_updated": now}}
                            )
                        except FloodWait as e:
                            await asyncio.sleep(e.value + 1)
                        except Exception:
                            await _countdown_col.delete_one({"_id": task["_id"]})
        except Exception as e:
            logger.error(f"Countdown worker error: {e}")
        await asyncio.sleep(10)


@Client.on_message(filters.all, group=-99)
async def start_countdown_worker(client: Client, message: Message):
    global _COUNTDOWN_WORKER_STARTED
    if not _COUNTDOWN_WORKER_STARTED:
        _COUNTDOWN_WORKER_STARTED = True
        try:
            saved_file_timer = await get_del_setting("FILE_AUTO_DELETE", 1800)
            setattr(info, "FILE_AUTO_DELETE", saved_file_timer)
            setattr(info, "AUTO_DELETE_TIME", saved_file_timer)
            saved_btn_timer = await get_del_setting("BUTTON_AUTO_DELETE", 300)
            setattr(info, "BUTTON_AUTO_DELETE", saved_btn_timer)
            setattr(info, "BUTTON_DEL", saved_btn_timer)
            setattr(temp, "BUTTON_AUTO_DELETE", saved_btn_timer)
        except Exception:
            pass
        asyncio.create_task(countdown_live_worker(client))


async def schedule_file_with_countdown(
    client: Client, sent_message: Message, delay_seconds: int, base_buttons: list = None
):
    if not sent_message or delay_seconds <= 0 or _countdown_col is None:
        return

    now = time.time()
    serialized_btns = []
    if base_buttons:
        for row in base_buttons:
            row_data = [
                {
                    "text": btn.text,
                    "url": getattr(btn, "url", None),
                    "callback_data": getattr(btn, "callback_data", None),
                }
                for btn in row
            ]
            serialized_btns.append(row_data)

    await _countdown_col.insert_one(
        {
            "chat_id": sent_message.chat.id,
            "message_id": sent_message.id,
            "delete_at": now + delay_seconds,
            "last_updated": 0,
            "buttons": serialized_btns,
        }
    )


@Client.on_callback_query(filters.regex(r"^noop_countdown$"), group=-2)
async def noop_countdown_cb(client: Client, query: CallbackQuery):
    await safe_answer(
        query,
        "⚠️ This file will be automatically deleted when the timer ends. Forward or save it now!",
        show_alert=True,
    )


# ============================================================
# 🧹 DUPLICATE FILE PURGER & DEAD LINK CLEANER
# ============================================================
def get_resolution_score(filename: str) -> int:
    name = (filename or "").lower()
    if any(q in name for q in ["2160p", "4k", "uhd"]):
        return 4
    if any(q in name for q in ["1440p", "2k"]):
        return 3
    if any(q in name for q in ["1080p", "fhd"]):
        return 2
    if any(q in name for q in ["720p", "hd"]):
        return 1
    return 0


@Client.on_message(filters.command("purgeduplicates") & filters.incoming, group=-2)
async def purge_duplicates_cmd(client: Client, message: Message):
    if not await is_user_admin(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    media_col = get_media_collection()
    if media_col is None:
        return await safe_reply_text(
            message,
            "❌ <b>Media database collection is unavailable.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message,
        "🔍 <b>Scanning database for duplicate files...</b>\n"
        "<i>This might take some time on large databases.</i>",
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        pipeline = [
            {
                "$group": {
                    "_id": "$file_name",
                    "count": {"$sum": 1},
                    "docs": {
                        "$push": {
                            "id": "$_id",
                            "size": "$file_size",
                            "name": "$file_name",
                        }
                    },
                }
            },
            {"$match": {"count": {"$gt": 1}}},
        ]

        cursor = media_col.aggregate(pipeline, allowDiskUse=True)
        duplicate_groups = await cursor.to_list(length=None)

        if not duplicate_groups:
            if status:
                return await safe_edit_text(
                    status,
                    "✅ <b>Database is squeaky clean!</b> No duplicate files found.",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        total_duplicates = 0
        deleted_ids = []

        for group in duplicate_groups:
            docs = group.get("docs", [])
            docs.sort(
                key=lambda d: (
                    get_resolution_score(d.get("name", "")),
                    d.get("size", 0) or 0,
                ),
                reverse=True,
            )
            to_remove = docs[1:]
            for d in to_remove:
                deleted_ids.append(d["id"])
                total_duplicates += 1

        for i in range(0, len(deleted_ids), 500):
            batch = deleted_ids[i : i + 500]
            await media_col.delete_many({"_id": {"$in": batch}})

        _invalidate_pm_filter_search_cache()
        if status:
            await safe_edit_text(
                status,
                f"🧹 <b>Duplicate File Purge Completed!</b>\n\n"
                f"📁 <b>Duplicate Sets Found:</b> <code>{len(duplicate_groups)}</code>\n"
                f"🗑️ <b>Redundant Copies Removed:</b> <code>{total_duplicates}</code>\n"
                f"✨ <b>Preserved:</b> Best quality copies (Highest resolution & bitrates).",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        logger.exception("purgeduplicates failed")
        if status:
            await safe_edit_text(
                status,
                f"❌ <b>Error while purging duplicates:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(filters.command("cleandeadlinks") & filters.incoming, group=-2)
async def clean_dead_links_cmd(client: Client, message: Message):
    if not await is_user_admin(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    media_col = get_media_collection()
    if media_col is None:
        return await safe_reply_text(
            message,
            "❌ <b>Media database collection is unavailable.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message,
        "🧹 <b>Scanning for broken or empty file entries...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    try:
        res = await media_col.delete_many(
            {
                "$or": [
                    {"file_name": {"$in": [None, ""]}},
                    {"file_size": {"$lte": 0}},
                    {"_id": {"$in": [None, ""]}},
                ]
            }
        )
        _invalidate_pm_filter_search_cache()
        if status:
            await safe_edit_text(
                status,
                f"✅ <b>Broken Entry Cleanup Complete!</b>\n"
                f"🗑️ <b>Removed Corrupt Entries:</b> <code>{res.deleted_count}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        if status:
            await safe_edit_text(
                status,
                f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


# ============================================================
# 🗑️ STANDARD DELETION & AUTO-DELETE TIMER COMMANDS
# ============================================================
@Client.on_message(filters.command("delete") & filters.incoming, group=-2)
async def delete_single_file(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    media_col = get_media_collection()
    if media_col is None:
        return await safe_reply_text(
            message,
            "❌ <b>Media database collection is unavailable.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        target_msg = (
            message.reply_to_message
            if (message.reply_to_message and message.reply_to_message.media)
            else (message if message.media else None)
        )

        if target_msg is None and len(message.command) >= 2:
            arg_str = message.text.split(None, 1)[1].strip()
            status_msg = await safe_reply_text(
                message,
                "🧹 <b>Deleting file from DB...</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            result = await media_col.delete_many(
                {
                    "$or": [
                        {"_id": arg_str},
                        {"file_id": arg_str},
                        {"file_name": arg_str},
                        {
                            "file_name": {
                                "$regex": f"^{re.escape(arg_str)}$",
                                "$options": "i",
                            }
                        },
                    ]
                }
            )
            if result.deleted_count:
                _invalidate_pm_filter_search_cache()
                if status_msg:
                    return await safe_edit_text(
                        status_msg,
                        f"✅ <b>Deleted <code>{result.deleted_count}</code> file(s) matching:</b> <code>{html.escape(arg_str)}</code>",
                        parse_mode=enums.ParseMode.HTML,
                    )
                return
            if status_msg:
                return await safe_edit_text(
                    status_msg,
                    "⚠️ <b>File not found in database.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        if not target_msg or not target_msg.media:
            return await safe_reply_text(
                message,
                "⚙️ <b>Usage:</b>\n"
                "• <code>/delete &lt;file_id or exact_file_name&gt;</code>\n"
                "• Or reply to a media file with <code>/delete</code>",
                parse_mode=enums.ParseMode.HTML,
            )

        status_msg = await safe_reply_text(
            message, "⏳ <b>Processing...</b>", parse_mode=enums.ParseMode.HTML
        )
        media = (
            getattr(target_msg, target_msg.media.value, None)
            if target_msg.media
            else None
        )
        if not media or not hasattr(media, "file_id"):
            if status_msg:
                return await safe_edit_text(
                    status_msg,
                    "❌ <b>This is not a supported file format.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        file_id, _ = unpack_new_file_id(media.file_id)
        res = await media_col.delete_many(
            {"$or": [{"_id": file_id}, {"file_id": file_id}, {"_id": media.file_id}]}
        )
        if res.deleted_count:
            _invalidate_pm_filter_search_cache()
            if status_msg:
                return await safe_edit_text(
                    status_msg,
                    "✅ <b>File successfully deleted from database.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        raw_fname = str(getattr(media, "file_name", "") or "")
        clean_fname = re.sub(r"(_|\-|\.|\+)", " ", raw_fname)
        name_candidates = list({raw_fname, clean_fname} - {""})

        query = {"file_size": getattr(media, "file_size", 0)}
        if name_candidates:
            query["file_name"] = {"$in": name_candidates}

        res = await media_col.delete_many(query)
        if res.deleted_count:
            _invalidate_pm_filter_search_cache()
            if status_msg:
                await safe_edit_text(
                    status_msg,
                    f"✅ <b>Deleted {res.deleted_count} matching file(s) from database.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
        else:
            if status_msg:
                await safe_edit_text(
                    status_msg,
                    "⚠️ <b>File not found in database.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
    except Exception as e:
        logger.exception("delete_file failed")
        await safe_reply_text(
            message,
            f"❌ <b>Error while deleting:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("delmulti") & filters.incoming, group=-2)
async def delete_multiple_files(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    media_col = get_media_collection()
    if media_col is None:
        return await safe_reply_text(
            message,
            "❌ <b>Media database collection is unavailable.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> <code>/delmulti &lt;keyword/name&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    keyword = message.text.split(None, 1)[1].strip()
    escaped_kw = html.escape(keyword)
    status_msg = await safe_reply_text(
        message,
        f"⏳ <b>Searching and deleting files matching:</b> <code>{escaped_kw}</code>...",
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        tokens = [re.escape(t) for t in keyword.split() if t]
        safe_pattern = r"[\s\.\_\-\+]+".join(tokens) if tokens else re.escape(keyword)
        query = {"file_name": {"$regex": safe_pattern, "$options": "i"}}
        result = await media_col.delete_many(query)

        if result.deleted_count > 0:
            _invalidate_pm_filter_search_cache()
            if status_msg:
                await safe_edit_text(
                    status_msg,
                    f"✅ <b>Successfully deleted <code>{result.deleted_count}</code> files matching:</b> <code>{escaped_kw}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
        else:
            if status_msg:
                await safe_edit_text(
                    status_msg,
                    f"⚠️ <b>No files found matching:</b> <code>{escaped_kw}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
    except Exception as e:
        logger.exception("delete_multiple_files failed")
        if status_msg:
            await safe_edit_text(
                status_msg,
                f"❌ <b>Error while deleting files:</b>\n<code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(filters.command("autodelete") & filters.incoming, group=-2)
async def set_autodelete_timer(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    current = await get_del_setting("FILE_AUTO_DELETE", 1800)
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/autodelete [seconds]</code>\n\n"
            f"⏱ <b>Current PM File Auto-Delete:</b> <code>{current}</code> seconds.",
            parse_mode=enums.ParseMode.HTML,
        )

    arg = message.command[1].strip().lower()
    try:
        seconds = 0 if arg in ["off", "disable", "false", "0"] else int(arg)
        if seconds < 0:
            raise ValueError
        await save_del_setting("FILE_AUTO_DELETE", seconds)
        if seconds == 0:
            await safe_reply_text(
                message,
                "✅ <b>PM File Auto-Delete has been DISABLED permanently.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await safe_reply_text(
                message,
                f"✅ <b>PM File Auto-Delete permanently set to:</b> <code>{seconds}</code> seconds.",
                parse_mode=enums.ParseMode.HTML,
            )
    except ValueError:
        await safe_reply_text(
            message,
            "❌ <b>Invalid duration!</b> Please provide a valid positive integer (seconds) or <code>off</code>.",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("buttondel") & filters.incoming, group=-2)
async def set_button_autodelete_timer(bot: Client, message: Message):
    if not await is_user_admin(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    current = await get_del_setting("BUTTON_AUTO_DELETE", 300)
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b> <code>/buttondel [seconds]</code>\n\n"
            f"⏱ <b>Current Group Button Auto-Delete:</b> <code>{current}</code> seconds.",
            parse_mode=enums.ParseMode.HTML,
        )

    arg = message.command[1].strip().lower()
    try:
        seconds = 0 if arg in ["off", "disable", "false", "0"] else int(arg)
        if seconds < 0:
            raise ValueError
        await save_del_setting("BUTTON_AUTO_DELETE", seconds)

        conn_gid = await get_connected_group_id(message)
        if conn_gid:
            try:
                await save_group_settings(conn_gid, "button_del", seconds)
                await save_group_settings(conn_gid, "auto_delete", seconds > 0)
            except Exception:
                pass

        if seconds == 0:
            await safe_reply_text(
                message,
                "✅ <b>Group Search Results (Button) Auto-Delete has been DISABLED.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await safe_reply_text(
                message,
                f"✅ <b>Group Search Results (Button) Auto-Delete set to:</b> <code>{seconds}</code> seconds.",
                parse_mode=enums.ParseMode.HTML,
            )
    except ValueError:
        await safe_reply_text(
            message,
            "❌ <b>Invalid duration!</b> Please provide a valid positive integer (seconds) or <code>off</code>.",
            parse_mode=enums.ParseMode.HTML,
        )
