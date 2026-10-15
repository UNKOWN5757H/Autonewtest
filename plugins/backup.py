import asyncio
import gzip
import html
import inspect
import os
import time
from datetime import datetime
from logging import ERROR, getLogger
from typing import List, Optional, Tuple

from bson.json_util import dumps, loads
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
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
_REPLY_PARAMS = set(inspect.signature(Message.reply_text).parameters.keys())
_EDIT_PARAMS = set(inspect.signature(Message.edit_text).parameters.keys())


async def safe_reply_text(message: Message, text: str, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None
    except Exception as e:
        logger.error(f"safe_reply_text error in backup.py: {e}")
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
# ⚙️ MONGODB CONNECTION & SCHEDULER STATE
# ============================================================
_DB_CLIENT = None
_BOT_DB = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
except Exception as e:
    logger.error(f"Failed to init Backup DB: {e}")

BACKUP_INTERVAL = 86400  # 24 hours
last_backup_time = None
next_backup_time = None
scheduler_running = False
_scheduler_task: Optional[asyncio.Task] = None

# Telegram Bot API upload limit safety threshold (48 MB)
MAX_RAW_JSON_BYTES = 48 * 1024 * 1024


# ============================================================
# 👑 SMART AUTHORIZATION ENGINE
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


async def is_callback_admin(client: Client, query: CallbackQuery) -> bool:
    if not query or not query.from_user:
        return False
    if await is_global_admin(query.from_user.id):
        return True
    if (
        query.message
        and query.message.chat
        and query.message.chat.type
        in [
            enums.ChatType.GROUP,
            enums.ChatType.SUPERGROUP,
        ]
    ):
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
# ⚡ NON-BLOCKING BACKUP & RESTORE ENGINE
# ============================================================
def _write_backup_file_sync(backup_data: dict, base_filename: str) -> str:
    """Serializes BSON data off the event loop and auto-compresses if over 48MB."""
    json_str = dumps(backup_data, indent=2)
    encoded = json_str.encode("utf-8")

    if len(encoded) > MAX_RAW_JSON_BYTES:
        gz_name = f"{base_filename}.json.gz"
        with gzip.open(gz_name, "wb", compresslevel=6) as f:
            f.write(encoded)
        return gz_name

    json_name = f"{base_filename}.json"
    with open(json_name, "wb") as f:
        f.write(encoded)
    return json_name


def _read_backup_file_sync(file_path: str) -> dict:
    """Reads either .json or .json.gz backup files off the main event loop."""
    if file_path.endswith(".gz"):
        with gzip.open(file_path, "rt", encoding="utf-8") as f:
            return loads(f.read())
    with open(file_path, "r", encoding="utf-8") as f:
        return loads(f.read())


async def generate_backup_file(exclude_media: bool = False) -> Tuple[str, int, int]:
    """Generates a backup file and returns (file_path, total_collections, total_docs)."""
    if _BOT_DB is None:
        raise RuntimeError("Database connection is not initialized.")

    collections = await _BOT_DB.list_collection_names()
    backup_data = {}
    total_colls = 0
    total_docs = 0

    media_col_name = getattr(info, "COLLECTION_NAME", "Telegram_files").lower()

    for coll_name in collections:
        if coll_name.startswith("system."):
            continue
        if exclude_media and coll_name.lower() in (
            media_col_name,
            "telegram_files",
            "media",
        ):
            continue
        docs = await _BOT_DB[coll_name].find({}).to_list(length=None)
        backup_data[coll_name] = docs
        total_colls += 1
        total_docs += len(docs)

    prefix = "DB_Config_Backup" if exclude_media else "DB_Full_Backup"
    base_name = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    file_path = await asyncio.to_thread(_write_backup_file_sync, backup_data, base_name)
    return file_path, total_colls, total_docs


# ============================================================
# 🔄 AUTOMATED 24H SCHEDULER
# ============================================================
async def auto_backup_task(bot: Client):
    global last_backup_time, next_backup_time, scheduler_running
    scheduler_running = True

    try:
        while scheduler_running:
            last_backup_time = time.time()
            next_backup_time = last_backup_time + BACKUP_INTERVAL
            await asyncio.sleep(BACKUP_INTERVAL)

            if not scheduler_running:
                break

            admins = get_admin_list()
            target_admin_id = admins[0] if admins else None
            if _BOT_DB is None or not target_admin_id:
                continue

            file_path = None
            try:
                file_path, colls_cnt, docs_cnt = await generate_backup_file(
                    exclude_media=False
                )
                caption = (
                    f"🔄 <b>Automated Daily Database Backup</b>\n\n"
                    f"📅 <b>Date:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                    f"📁 <b>Collections:</b> <code>{colls_cnt}</code>\n"
                    f"📄 <b>Total Documents:</b> <code>{docs_cnt}</code>"
                )
                await bot.send_document(
                    chat_id=target_admin_id,
                    document=file_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e:
                logger.error(f"Auto-backup failed: {e}")
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
    except asyncio.CancelledError:
        pass
    finally:
        scheduler_running = False
        next_backup_time = None


# ============================================================
# 🎛️ INTERACTIVE BACKUP DASHBOARD BUILDER
# ============================================================
async def _build_backup_dashboard() -> Tuple[str, InlineKeyboardMarkup]:
    db_name = html.escape(str(getattr(info, "DATABASE_NAME", "Unknown")))
    colls_count = 0
    total_docs = 0
    size_mb = 0.0

    if _BOT_DB is not None:
        try:
            stats = await _BOT_DB.command("dbstats")
            colls_count = stats.get("collections", 0)
            total_docs = stats.get("objects", 0)
            size_mb = stats.get("dataSize", 0) / (1024 * 1024)
        except Exception:
            pass

    if scheduler_running and next_backup_time:
        rem = max(0, int(next_backup_time - time.time()))
        h, r = divmod(rem, 3600)
        m, s = divmod(r, 60)
        sched_str = f"🟢 Active (Next in {h}h {m}m {s}s)"
        sched_btn_text = "🛑 Stop Auto-Backup"
    else:
        sched_str = "🔴 Disabled"
        sched_btn_text = "⏰ Enable 24h Auto-Backup"

    text = (
        f"💾 <b>Database Backup & Restore Center</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🗄️ <b>Database:</b> <code>{db_name}</code>\n"
        f"📦 <b>Collections:</b> <code>{colls_count}</code>\n"
        f"📄 <b>Total Documents:</b> <code>{total_docs}</code>\n"
        f"💾 <b>Data Size:</b> <code>{size_mb:.2f} MB</code>\n"
        f"⏰ <b>Auto-Backup:</b> <code>{sched_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>To restore a backup, reply to any <code>.json</code> or <code>.json.gz</code> file with <code>/dbrestore</code>.</i>"
    )

    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 Full Backup", callback_data="db_bk:full"),
                InlineKeyboardButton("⚡ Settings Only", callback_data="db_bk:light"),
            ],
            [
                InlineKeyboardButton(
                    "📊 Collection Stats", callback_data="db_bk:stats"
                ),
                InlineKeyboardButton(sched_btn_text, callback_data="db_bk:sched"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="db_bk:refresh"),
                InlineKeyboardButton("🔐 Close", callback_data="db_bk:close"),
            ],
        ]
    )
    return text, buttons


# ============================================================
# 🛠️ DATABASE MANAGEMENT COMMANDS & CALLBACKS
# ============================================================
@Client.on_message(
    filters.command(["backup", "dbpanel"]) & filters.incoming,
    group=-2,
)
async def backup_dashboard_cmd(bot: Client, message: Message):
    if not await quick_admin_check(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    text, markup = await _build_backup_dashboard()
    await safe_reply_text(
        message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("dbbackup") & filters.incoming, group=-2)
async def db_backup_cmd(bot: Client, message: Message):
    if not await quick_admin_check(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    exclude_media = len(message.command) > 1 and message.command[1].strip().lower() in [
        "light",
        "settings",
        "config",
    ]
    status_msg = await safe_reply_text(
        message,
        "⏳ <b>Generating database backup...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    file_path = None
    try:
        file_path, colls_cnt, docs_cnt = await generate_backup_file(
            exclude_media=exclude_media
        )
        mention = (
            message.from_user.mention
            if message.from_user
            else (
                html.escape(message.sender_chat.title)
                if message.sender_chat
                else "Admin"
            )
        )
        caption = (
            f"✅ <b>Database Backup Complete!</b>\n\n"
            f"📅 <b>Date:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
            f"📁 <b>Collections:</b> <code>{colls_cnt}</code>\n"
            f"📄 <b>Documents:</b> <code>{docs_cnt}</code>\n"
            f"👤 <b>Requested By:</b> {mention}"
        )
        await message.reply_document(
            document=file_path, caption=caption, parse_mode=enums.ParseMode.HTML
        )
        if status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass
    except Exception as e:
        if status_msg:
            await safe_edit_text(
                status_msg,
                f"⚠️ <b>Backup Failed!</b>\n\n<b>Error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


@Client.on_message(filters.command("dbrestore") & filters.incoming, group=-2)
async def db_restore_cmd(bot: Client, message: Message):
    if not await quick_admin_check(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    target_msg = (
        message.reply_to_message
        if (message.reply_to_message and message.reply_to_message.document)
        else (message if message.document else None)
    )

    if not target_msg or not target_msg.document:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b>\nReply to a previously generated backup <code>.json</code> or <code>.json.gz</code> file with <code>/dbrestore</code>.",
            parse_mode=enums.ParseMode.HTML,
        )

    doc = target_msg.document
    fname = (doc.file_name or "").lower()
    if not (fname.endswith(".json") or fname.endswith(".json.gz")):
        return await safe_reply_text(
            message,
            "❌ <b>Invalid File Format!</b> Please reply to a <code>.json</code> or <code>.json.gz</code> backup file.",
            parse_mode=enums.ParseMode.HTML,
        )

    status_msg = await safe_reply_text(
        message,
        "⏳ <b>Downloading and restoring database...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    file_path = None
    try:
        file_path = await target_msg.download()
        backup_data = await asyncio.to_thread(_read_backup_file_sync, file_path)

        if not isinstance(backup_data, dict):
            raise ValueError("Backup file root must be a JSON object of collections.")

        restored_colls = 0
        restored_docs = 0

        for coll_name, docs in backup_data.items():
            if coll_name.startswith("system.") or not isinstance(docs, list):
                continue
            await _BOT_DB[coll_name].delete_many({})
            if docs:
                for i in range(0, len(docs), 1000):
                    batch = docs[i : i + 1000]
                    await _BOT_DB[coll_name].insert_many(batch, ordered=False)
                restored_docs += len(docs)
            restored_colls += 1

        if status_msg:
            await safe_edit_text(
                status_msg,
                f"✅ <b>Database Restored Successfully!</b>\n\n"
                f"📁 <b>Collections Restored:</b> <code>{restored_colls}</code>\n"
                f"📄 <b>Documents Restored:</b> <code>{restored_docs}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        if status_msg:
            await safe_edit_text(
                status_msg,
                f"⚠️ <b>Restore Failed!</b>\n\n<b>Error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


async def _build_detailed_dbstats_text() -> str:
    stats = await _BOT_DB.command("dbstats")
    colls = await _BOT_DB.list_collection_names()
    total_size_mb = stats.get("dataSize", 0) / (1024 * 1024)
    db_name = html.escape(str(getattr(info, "DATABASE_NAME", "Unknown")))

    text = (
        f"📊 <b>Database Statistics</b>\n\n"
        f"🗄️ <b>Database:</b> <code>{db_name}</code>\n"
        f"📦 <b>Collections Count:</b> <code>{stats.get('collections', 0)}</code>\n"
        f"📄 <b>Total Documents:</b> <code>{stats.get('objects', 0)}</code>\n"
        f"💾 <b>Data Size:</b> <code>{total_size_mb:.2f} MB</code>\n\n"
        f"📁 <b>Collection Breakdown:</b>\n"
    )
    for coll in sorted(colls):
        if coll.startswith("system."):
            continue
        count = await _BOT_DB[coll].count_documents({})
        text += f"  • <code>{html.escape(coll)}</code>: <code>{count}</code> docs\n"
    return text


@Client.on_message(filters.command("dbstats") & filters.incoming, group=-2)
async def db_stats_cmd(bot: Client, message: Message):
    if not await quick_admin_check(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    try:
        text = await _build_detailed_dbstats_text()
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💾 Backup Panel", callback_data="db_bk:refresh"
                    ),
                    InlineKeyboardButton("🔐 Close", callback_data="db_bk:close"),
                ]
            ]
        )
        await safe_reply_text(
            message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await safe_reply_text(
            message,
            f"⚠️ <b>Failed to fetch stats:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("dbschedule") & filters.incoming, group=-2)
async def db_schedule_cmd(bot: Client, message: Message):
    global _scheduler_task, scheduler_running
    if not await quick_admin_check(bot, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) > 1 and message.command[1].lower() in [
        "off",
        "stop",
        "disable",
    ]:
        scheduler_running = False
        if _scheduler_task and not _scheduler_task.done():
            _scheduler_task.cancel()
        return await safe_reply_text(
            message,
            "🛑 <b>Auto-backup scheduler has been stopped.</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    if not scheduler_running:
        _scheduler_task = asyncio.create_task(auto_backup_task(bot))
        return await safe_reply_text(
            message,
            "✅ <b>Auto-backup scheduler initiated!</b>\nThe automated backup will run every 24 hours.",
            parse_mode=enums.ParseMode.HTML,
        )

    if next_backup_time:
        time_left = max(0, int(next_backup_time - time.time()))
        hours, remainder = divmod(time_left, 3600)
        minutes, seconds = divmod(remainder, 60)
        await safe_reply_text(
            message,
            f"⏰ <b>Scheduled Backup Status</b>\n\n"
            f"🟢 <b>Status:</b> <code>Active</code>\n"
            f"⏳ <b>Next Backup In:</b> <code>{hours}h {minutes}m {seconds}s</code>",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🛑 Stop Auto-Backup", callback_data="db_bk:sched"
                        )
                    ]
                ]
            ),
            parse_mode=enums.ParseMode.HTML,
        )
    else:
        await safe_reply_text(
            message,
            "⏰ <b>Scheduler is active and calculating next run time...</b>",
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_callback_query(
    filters.regex(r"^db_bk:(full|light|stats|sched|refresh|close)$"),
    group=-2,
)
async def backup_panel_callbacks(bot: Client, query: CallbackQuery):
    global _scheduler_task, scheduler_running
    if not await is_callback_admin(bot, query):
        return await safe_answer(query, "❌ Unauthorized: Admin only!", show_alert=True)

    action = query.matches[0].group(1)
    try:
        if action == "close":
            await safe_answer(query, "Closed!")
            return await query.message.delete()

        if action in ("full", "light"):
            exclude_media = action == "light"
            await safe_answer(
                query, "⏳ Generating backup file, please wait...", show_alert=False
            )
            file_path = None
            try:
                file_path, colls_cnt, docs_cnt = await generate_backup_file(
                    exclude_media=exclude_media
                )
                label = "Settings & Users" if exclude_media else "Full Database"
                caption = (
                    f"✅ <b>{label} Backup Complete!</b>\n\n"
                    f"📅 <b>Date:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                    f"📁 <b>Collections:</b> <code>{colls_cnt}</code>\n"
                    f"📄 <b>Documents:</b> <code>{docs_cnt}</code>\n"
                    f"👤 <b>Requested By:</b> {query.from_user.mention}"
                )
                await query.message.reply_document(
                    document=file_path,
                    caption=caption,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception as e:
                await safe_reply_text(
                    query.message,
                    f"⚠️ <b>Backup Failed:</b> <code>{html.escape(str(e))}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
            return

        if action == "stats":
            await safe_answer(query, "Loading collection breakdown...")
            stats_text = await _build_detailed_dbstats_text()
            buttons = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔙 Back to Backup Panel", callback_data="db_bk:refresh"
                        ),
                        InlineKeyboardButton("🔐 Close", callback_data="db_bk:close"),
                    ]
                ]
            )
            return await safe_edit_text(
                query.message,
                stats_text,
                reply_markup=buttons,
                parse_mode=enums.ParseMode.HTML,
            )

        if action == "sched":
            if scheduler_running:
                scheduler_running = False
                if _scheduler_task and not _scheduler_task.done():
                    _scheduler_task.cancel()
                await safe_answer(
                    query, "🛑 24h Auto-Backup Disabled!", show_alert=True
                )
            else:
                _scheduler_task = asyncio.create_task(auto_backup_task(bot))
                await safe_answer(query, "✅ 24h Auto-Backup Enabled!", show_alert=True)

        else:
            await safe_answer(query, "Refreshed!")

        text, markup = await _build_backup_dashboard()
        await safe_edit_text(
            query.message, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Backup callback error: {e}")
