import asyncio
import datetime
import io
import os
import re
import time
from logging import ERROR, getLogger

from bson.json_util import dumps, loads
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    InputUserDeactivated,
    MessageTooLong,
    PeerIdInvalid,
    UserDeactivatedBan,
    UserIsBlocked,
)
from pyrogram.types import InlineKeyboardButton, Message

import info
from database.plugin_dbs import plugin_db as _plugin_db
from database.users_chats_db import db as _db

# 👑 IMPORTING THE GOD-TIER UNIVERSAL ADMIN ENGINE
from utils import admin_filter, get_admin_list, get_uid

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚙️ MONGODB & GLOBALS SETUP
# ============================================================
_DB_CLIENT = None
_BOT_DB = None
_broadcast_col = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _broadcast_col = _BOT_DB["broadcast_tasks"]
except Exception as e:
    logger.error(f"Failed to init DBs: {e}")

# Backup Globals
BACKUP_INTERVAL = 86400
last_backup_time = None
next_backup_time = None
scheduler_running = False

# Broadcast Globals
DELETE_DELAY = getattr(info, "BROADCAST_DELETE_TIME", 24 * 3600)
worker_started = False
_WORKER_TASK = None

if not hasattr(info, "REPAIR_MODE"):
    info.REPAIR_MODE = False


# ============================================================
# 🛠️ BOT SETTINGS & REPAIR MODE (/repairmode, /adminsettings)
# ============================================================
@Client.on_message(
    filters.command("repairmode") & admin_filter & (filters.private | filters.group)
)
async def toggle_repair_mode(bot: Client, message: Message):
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

    status = "🟢 **ENABLED**" if info.REPAIR_MODE else "🔴 **DISABLED**"
    text = f"🛠️ **Repair Mode is now {status}**\n\n"
    if info.REPAIR_MODE:
        text += "⚠️ *The bot is now in maintenance mode. It will NOT send any files to users until this is disabled.*"
    else:
        text += "✅ *The bot has returned to normal operation and will process file requests.*"

    await message.reply_text(text)


@Client.on_message(
    filters.command("adminsettings") & admin_filter & (filters.private | filters.group)
)
async def show_admin_settings(bot: Client, message: Message):
    repair_status = (
        "🟢 ENABLED" if getattr(info, "REPAIR_MODE", False) else "🔴 DISABLED"
    )
    fsub_status = (
        "🟢 ENABLED" if getattr(info, "IS_FSUB_ENABLED", True) else "🔴 DISABLED"
    )

    text = "⚙️ **Current Admin Settings Dashboard**\n━━━━━━━━━━━━━━━━━━━━━\n\n"
    text += f"🛠️ **Repair Mode:** {repair_status}\n🔐 **Force Subscribe:** {fsub_status}\n\n"
    text += (
        f"📢 **Main FSub Channel:** <code>{info.AUTH_CHANNEL}</code>\n"
        if getattr(info, "AUTH_CHANNEL", None)
        else "📢 **Main FSub Channel:** ❌ Not Set\n"
    )
    text += (
        f"📨 **Request FSub Channel:** <code>{info.REQ_CHANNEL}</code>\n"
        if getattr(info, "REQ_CHANNEL", None)
        else "📨 **Request FSub Channel:** ❌ Not Set\n"
    )

    admin_count = len(get_admin_list())
    text += f"\n👥 **Total Admins Configured:** `{admin_count}`\n\n━━━━━━━━━━━━━━━━━━━━━\n💡 *Use commands like `/repairmode` or `/disablefsub` to toggle these values.*"
    await message.reply_text(text)


# ============================================================
# 💾 DATABASE BACKUP ENGINE (/dbbackup, /dbrestore, /dbstats)
# ============================================================
async def auto_backup_task(bot: Client):
    global last_backup_time, next_backup_time, scheduler_running
    scheduler_running = True
    admins = get_admin_list()
    target_admin_id = admins[0] if admins else None

    while True:
        last_backup_time = time.time()
        next_backup_time = last_backup_time + BACKUP_INTERVAL
        await asyncio.sleep(BACKUP_INTERVAL)

        if not _BOT_DB or not target_admin_id:
            continue
        try:
            file_path = await generate_backup_file()
            caption = f"🔄 **Automated Daily Database Backup**\n\n📅 Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            await bot.send_document(
                chat_id=target_admin_id, document=file_path, caption=caption
            )
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Auto-backup failed: {e}")


async def generate_backup_file() -> str:
    collections = await _BOT_DB.list_collection_names()
    backup_data = {}
    for coll_name in collections:
        docs = await _BOT_DB[coll_name].find({}).to_list(length=None)
        backup_data[coll_name] = docs

    file_name = f"DB_Backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(dumps(backup_data, indent=4))
    return file_name


@Client.on_message(filters.command("dbbackup") & admin_filter)
async def db_backup_cmd(bot: Client, message: Message):
    status_msg = await message.reply_text("⏳ **Generating database backup...**")
    try:
        file_path = await generate_backup_file()
        caption = f"✅ **Database Backup Complete!**\n\n📅 Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n👤 **Requested By:** {message.from_user.mention if getattr(message, 'from_user', None) else 'Admin'}"
        await message.reply_document(document=file_path, caption=caption)
        os.remove(file_path)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"⚠️ **Backup Failed!**\n\nError: `{e}`")


@Client.on_message(filters.command("dbrestore") & admin_filter)
async def db_restore_cmd(bot: Client, message: Message):
    if not getattr(message, "reply_to_message", None) or not getattr(message.reply_to_message, "document", None):
        return await message.reply_text(
            "⚙️ **Usage:**\nReply to a previously generated backup `.json` file with `/dbrestore`."
        )
    doc = message.reply_to_message.document
    if not doc.file_name.endswith(".json"):
        return await message.reply_text("❌ **Invalid File Format!**")

    status_msg = await message.reply_text("⏳ **Restoring database...**")
    try:
        file_path = await message.reply_to_message.download()
        with open(file_path, "r", encoding="utf-8") as f:
            backup_data = loads(f.read())
        restored_colls = restored_docs = 0

        for coll_name, docs in backup_data.items():
            if docs:
                await _BOT_DB[coll_name].delete_many({})
                await _BOT_DB[coll_name].insert_many(docs)
                restored_colls += 1
                restored_docs += len(docs)

        os.remove(file_path)
        await status_msg.edit_text(
            f"✅ **Database Restored Successfully!**\n\n📁 **Collections:** `{restored_colls}`\n📄 **Documents:** `{restored_docs}`"
        )
    except Exception as e:
        await status_msg.edit_text(f"⚠️ **Restore Failed!**\n\nError: `{e}`")
        if "file_path" in locals() and os.path.exists(file_path):
            os.remove(file_path)


@Client.on_message(filters.command("dbstats") & admin_filter)
async def db_stats_cmd(bot: Client, message: Message):
    try:
        stats = await _BOT_DB.command("dbstats")
        colls = await _BOT_DB.list_collection_names()
        total_size_mb = stats.get("dataSize", 0) / (1024 * 1024)

        text = f"📊 **Database Statistics**\n\n🗄️ **Database:** `{info.DATABASE_NAME}`\n📦 **Collections Count:** `{stats.get('collections', 0)}`\n📄 **Total Documents:** `{stats.get('objects', 0)}`\n💾 **Data Size:** `{total_size_mb:.2f} MB`\n\n📁 **Collection Breakdown:**\n"
        for coll in colls:
            count = await _BOT_DB[coll].count_documents({})
            text += f"  - `{coll}`: {count} docs\n"
        await message.reply_text(text)
    except Exception as e:
        await message.reply_text(f"⚠️ **Failed to fetch stats:**\n`{e}`")


@Client.on_message(filters.command("dbschedule") & admin_filter)
async def db_schedule_cmd(bot: Client, message: Message):
    if not scheduler_running:
        asyncio.create_task(auto_backup_task(bot))
        return await message.reply_text(
            "✅ **Auto-backup scheduler initiated!**\nThe first automated backup will run in 24 hours."
        )

    if next_backup_time:
        time_left = next_backup_time - time.time()
        hours, remainder = divmod(int(time_left), 3600)
        minutes, seconds = divmod(remainder, 60)
        await message.reply_text(
            f"⏰ **Scheduled Backup Status**\n\n🟢 **Status:** `Active`\n⏳ **Next Backup In:** `{hours}h {minutes}m {seconds}s`"
        )
    else:
        await message.reply_text("⏰ Scheduler is active but time is calculating...")


# ============================================================
# 📢 INLINE PROMOTIONS ENGINE (/addpromo, /delpromo)
# ============================================================
@Client.on_message(
    filters.command("addpromo") & admin_filter & (filters.private | filters.group)
)
async def add_promo_handler(bot: Client, message: Message):
    text = message.text.split(None, 1)[1] if len(message.command) > 1 else ""
    match = re.search(r'"([^"]+)"\s+(https?://\S+)', text)

    if not match:
        return await message.reply_text(
            '⚙️ **Usage:**\n`/addpromo "Button Text" https://example.com`\n\n⚠️ Ensure the button text is inside double quotes `" "` followed by a valid URL.'
        )

    btn_text, btn_url = match.group(1), match.group(2)
    await _plugin_db.add_promo(btn_text, btn_url)
    await message.reply_text(
        f"✅ **Promo Added Successfully!**\n\n**Text:** `{btn_text}`\n**URL:** {btn_url}",
        disable_web_page_preview=True,
    )


@Client.on_message(
    filters.command("delpromo") & admin_filter & (filters.private | filters.group)
)
async def del_promo_handler(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "⚙️ **Usage:**\n`/delpromo https://example.com`"
        )
    btn_url = message.command[1]
    deleted = await _plugin_db.del_promo(btn_url)
    if deleted:
        await message.reply_text(
            f"🗑️ **Promo deleted successfully!**\n\n**URL:** {btn_url}",
            disable_web_page_preview=True,
        )
    else:
        await message.reply_text(
            f"❌ **Promo not found!**\nCould not find any promo matching URL: {btn_url}",
            disable_web_page_preview=True,
        )


@Client.on_message(
    filters.command("listpromos") & admin_filter & (filters.private | filters.group)
)
async def list_promo_handler(bot: Client, message: Message):
    promos = await _plugin_db.get_all_promos()
    if not promos:
        return await message.reply_text(
            "⚠️ **No active promotional links found in the database.**"
        )

    text = "📢 **Current Promotional Links:**\n\n"
    for i, p in enumerate(promos, 1):
        text += f"**{i}. Text:** `{p['text']}`\n**🔗 URL:** {p['url']}\n\n"
    await message.reply_text(text, disable_web_page_preview=True)


async def get_promo_buttons() -> list:
    promos = await _plugin_db.get_all_promos()
    return [InlineKeyboardButton(p["text"], url=p["url"]) for p in promos]


# ============================================================
# 📤 BROADCAST ENGINE (/broadcast, /group_broadcast)
# ============================================================
def get_progress_bar(current, total):
    if total == 0:
        return "[░░░░░░░░░░] 0.0%"
    percent = (current / total) * 100
    filled = int(percent / 10)
    return f"[{'█' * filled}{'░' * (10 - filled)}] {percent:.1f}%"


async def bcast_cleaner_worker(bot: Client):
    while True:
        try:
            now = time.time()
            if _broadcast_col is not None:
                cursor = _broadcast_col.find({"delete_at": {"$lte": now}})
                docs = await cursor.to_list(length=100)
                for doc in docs:
                    try:
                        await bot.delete_messages(
                            chat_id=doc["chat_id"], message_ids=doc["message_id"]
                        )
                    except Exception:
                        pass
                    await _broadcast_col.delete_one({"_id": doc["_id"]})
        except Exception:
            pass
        await asyncio.sleep(60)


@Client.on_message(filters.group | filters.private, group=-100)
async def init_worker(bot: Client, message):
    global worker_started, _WORKER_TASK
    if not worker_started:
        worker_started = True
        _WORKER_TASK = asyncio.create_task(bcast_cleaner_worker(bot))


async def send_and_schedule_delete(bot: Client, chat_id: int, message: Message):
    try:
        sent_msg = None
        try:
            sent_msg = await message.copy(chat_id=chat_id, disable_notification=True)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            sent_msg = await message.copy(chat_id=chat_id, disable_notification=True)
        except Exception:
            try:
                sent_msg = await message.forward(
                    chat_id=chat_id, disable_notification=True
                )
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                sent_msg = await message.forward(
                    chat_id=chat_id, disable_notification=True
                )
            except Exception:
                if getattr(message, "photo", None):
                    sent_msg = await bot.send_photo(
                        chat_id,
                        message.photo.file_id,
                        caption=message.caption,
                        disable_notification=True,
                    )
                elif getattr(message, "video", None):
                    sent_msg = await bot.send_video(
                        chat_id,
                        message.video.file_id,
                        caption=message.caption,
                        disable_notification=True,
                    )
                elif getattr(message, "document", None):
                    sent_msg = await bot.send_document(
                        chat_id,
                        message.document.file_id,
                        caption=message.caption,
                        disable_notification=True,
                    )
                else:
                    sent_msg = await bot.send_message(
                        chat_id, message.text, disable_notification=True
                    )

        if sent_msg and _broadcast_col is not None:
            await _broadcast_col.insert_one(
                {
                    "chat_id": chat_id,
                    "message_id": sent_msg.id,
                    "delete_at": time.time() + DELETE_DELAY,
                }
            )
            return 200, None
        return 500, "Unknown Error"

    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await send_and_schedule_delete(bot, chat_id, message)
    except (UserIsBlocked, InputUserDeactivated, UserDeactivatedBan):
        return 400, "Blocked/Deleted"
    except PeerIdInvalid:
        return 400, "Invalid"
    except ChatWriteForbidden:
        return 400, "Left"
    except MessageTooLong:
        return 400, "MEDIA_CAPTION_TOO_LONG"
    except Exception as e:
        return 500, str(e)


@Client.on_message(filters.command("broadcast") & admin_filter & filters.reply)
async def user_broadcast(bot: Client, message: Message):
    b_msg = message.reply_to_message
    if not b_msg:
        return await message.reply_text(
            "⚠️ **Reply to the message you want to broadcast.**"
        )

    status_msg = await message.reply_text("⏳ **Fetching user database...**")
    try:
        users_cursor = await _db.get_all_users()
        users = (
            await users_cursor.to_list(length=None)
            if hasattr(users_cursor, "to_list")
            else list(users_cursor)
        )
    except Exception as e:
        return await status_msg.edit_text(f"❌ **Database Error:** `{e}`")

    total_users = len(users)
    if total_users == 0:
        return await status_msg.edit_text("⚠️ **No users found in the database.**")

    await status_msg.edit_text(f"🚀 **Broadcasting to {total_users} users...**")
    start_time = time.time()
    done = success = blocked = failed = 0

    for user in users:
        user_id = int(user.get("id") or user.get("user_id"))
        status, _ = await send_and_schedule_delete(bot, user_id, b_msg)

        if status == 200:
            success += 1
        elif status == 400:
            blocked += 1
            await _db.delete_user(user_id)
        else:
            failed += 1

        done += 1
        if done % 25 == 0 or done == total_users:
            try:
                await status_msg.edit_text(
                    f"📢 **User Broadcast Progress**\n{get_progress_bar(done, total_users)}\n\n👥 **Total:** `{total_users}`\n✅ **Sent:** `{success}`\n🚫 **Dead/Blocked:** `{blocked}`\n⚠️ **Failed:** `{failed}`\n📦 **Processed:** `{done}/{total_users}`"
                )
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception:
                pass
        await asyncio.sleep(0.3)

    time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
    time_str = (
        f"{DELETE_DELAY // 3600} hours"
        if DELETE_DELAY >= 3600
        else f"{(DELETE_DELAY % 3600) // 60} minutes"
    )
    await status_msg.edit_text(
        f"✅ **User Broadcast Completed!**\n\n🕒 **Duration:** `{time_taken}`\n👥 **Total:** `{total_users}`\n✅ **Successful:** `{success}`\n🚫 **Removed (Dead):** `{blocked}`\n⚠️ **Failed:** `{failed}`\n\n⏳ *These messages will vanish silently in {time_str}.*"
    )


@Client.on_message(filters.command("group_broadcast") & admin_filter & filters.reply)
async def group_broadcast(bot: Client, message: Message):
    b_msg = message.reply_to_message
    if not b_msg:
        return await message.reply_text(
            "⚠️ **Reply to the message you want to broadcast.**"
        )

    status_msg = await message.reply_text("⏳ **Fetching chat database...**")
    try:
        chats_cursor = await _db.get_all_chats()
        chats = (
            await chats_cursor.to_list(length=None)
            if hasattr(chats_cursor, "to_list")
            else list(chats_cursor)
        )
    except Exception as e:
        return await status_msg.edit_text(f"❌ **Database Error:** `{e}`")

    total_chats = len(chats)
    if total_chats == 0:
        return await status_msg.edit_text("⚠️ **No groups found in the database.**")

    await status_msg.edit_text(f"🚀 **Broadcasting to {total_chats} groups...**")
    start_time = time.time()
    done = success = left = failed = 0

    for chat in chats:
        chat_id = int(chat.get("id") or chat.get("chat_id"))
        status, reason = await send_and_schedule_delete(bot, chat_id, b_msg)

        if status == 200:
            success += 1
        elif status == 400 and reason in ["Left", "Blocked/Deleted"]:
            left += 1
            try:
                await _db.disable_chat(chat_id, "Bot Removed")
            except Exception:
                pass
        else:
            failed += 1

        done += 1
        if done % 15 == 0 or done == total_chats:
            try:
                await status_msg.edit_text(
                    f"🏘️ **Group Broadcast Progress**\n{get_progress_bar(done, total_chats)}\n\n💬 **Total:** `{total_chats}`\n✅ **Sent:** `{success}`\n🚷 **Kicked/Left:** `{left}`\n⚠️ **Failed:** `{failed}`\n📦 **Processed:** `{done}/{total_chats}`"
                )
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception:
                pass
        await asyncio.sleep(0.5)

    time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
    time_str = (
        f"{DELETE_DELAY // 3600} hours"
        if DELETE_DELAY >= 3600
        else f"{(DELETE_DELAY % 3600) // 60} minutes"
    )
    await status_msg.edit_text(
        f"✅ **Group Broadcast Completed!**\n\n🕒 **Duration:** `{time_taken}`\n💬 **Total:** `{total_chats}`\n✅ **Successful:** `{success}`\n🚷 **Removed From:** `{left}`\n⚠️ **Failed:** `{failed}`\n\n⏳ *These messages will vanish silently in {time_str}.*"
    )
