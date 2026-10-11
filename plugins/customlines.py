import ast
import asyncio
import datetime
import io
import math
import os
import platform
import re
import shutil
import time
import traceback
from datetime import timezone
from logging import ERROR, getLogger

from bson.json_util import dumps, loads
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
    InputUserDeactivated,
    MessageNotModified,
    MessageTooLong,
    PeerIdInvalid,
    UserDeactivatedBan,
    UserIsBlocked,
    UserNotParticipant,
)
from pyrogram.errors.exceptions.bad_request_400 import QueryIdInvalid
from pyrogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultCachedDocument,
    Message,
)

import info
from database.connections_mdb import (
    active_connection,
    add_connection,
    all_connections,
    delete_connection,
    if_active,
)
from database.filters_mdb import add_filter, delete_filter, find_filter, get_filters
from database.ia_filterdb import Media as _Media
from database.ia_filterdb import get_file_details, get_search_results
from database.join_reqs import join_reqs as _join_reqs_db
from database.plugin_dbs import plugin_db as _plugin_db
from database.users_chats_db import db as _db

# 👑 Removed 'is_subscribed' import to prevent fatal ImportError crashes on boot
from utils import admin_filter, get_admin_list, get_size, get_uid, temp

logger = getLogger(__name__)
logger.setLevel(ERROR)

try:
    import psutil
except ImportError:
    psutil = None

# 🚨 BUG FIX: Hardcoded integers to prevent MongoDB ButtonStyle encoding crash
BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER, BTN_SECONDARY = 1, 3, 4, 2

# ============================================================
# ⚙️ MONGODB & GLOBALS SETUP (MERGED)
# ============================================================
_DB_CLIENT = _BOT_DB = _broadcast_col = _fsub_db = _vip_db = _b_settings_db = _filter_col = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _broadcast_col = _BOT_DB["broadcast_tasks"]
    _fsub_db = _BOT_DB["fsub_config"]
    _vip_db = _BOT_DB["fsub_vips"]
    _b_settings_db = _BOT_DB["global_bot_settings"]
    _filter_col = _BOT_DB["filters"]
except Exception as e:
    logger.error(f"Failed to init DBs: {e}")

BACKUP_INTERVAL = 86400
last_backup_time = next_backup_time = None
scheduler_running = False

DELETE_DELAY = getattr(info, "BROADCAST_DELETE_TIME", 24 * 3600)
worker_started = False
_WORKER_TASK = None
_WAITING_REQUESTS = {}

if not hasattr(info, "REPAIR_MODE"):
    info.REPAIR_MODE = False


# ============================================================
# 🛑 UNIFIED CUSTOM LISTENER (Prevents Media Block & /start Hijack)
# ============================================================
@Client.on_message(filters.text & admin_filter, group=-12)
async def custom_listener(client: Client, message: Message):
    key = (message.chat.id, get_uid(message))
    if key in _WAITING_REQUESTS:
        future = _WAITING_REQUESTS.pop(key)
        if not future.done():
            future.set_result(message)
        # 🚨 BUG FIX: Pyrogram v2 compatibility - raises safely instead of crashing
        raise StopPropagation


async def native_listen(
    client: Client, chat_id: int, user_id: int, timeout: int = 120
) -> Message:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _WAITING_REQUESTS[(chat_id, user_id)] = future
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        _WAITING_REQUESTS.pop((chat_id, user_id), None)
        raise asyncio.TimeoutError


# ============================================================
# 🛠️ BOT SETTINGS & REPAIR MODE (/repairmode, /adminsettings)
# ============================================================
@Client.on_message(filters.command("repairmode") & admin_filter & (filters.private | filters.group))
async def toggle_repair_mode(bot: Client, message: Message):
    try:
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
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("adminsettings") & admin_filter & (filters.private | filters.group))
async def show_admin_settings(bot: Client, message: Message):
    try:
        repair_status = "🟢 ENABLED" if getattr(info, "REPAIR_MODE", False) else "🔴 DISABLED"
        fsub_status = "🟢 ENABLED" if getattr(info, "IS_FSUB_ENABLED", True) else "🔴 DISABLED"

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
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


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
            caption = f"🔄 **Automated Daily Database Backup**\n\n📅 Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
            await bot.send_document(chat_id=target_admin_id, document=file_path, caption=caption)
            os.remove(file_path)
        except Exception as e:
            logger.error(f"Auto-backup failed: {e}")


async def generate_backup_file() -> str:
    collections = await _BOT_DB.list_collection_names()
    backup_data = {}
    for coll_name in collections:
        docs = await _BOT_DB[coll_name].find({}).to_list(length=None)
        backup_data[coll_name] = docs

    file_name = f"DB_Backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(file_name, "w", encoding="utf-8") as f:
        f.write(dumps(backup_data, indent=4))
    return file_name


@Client.on_message(filters.command("dbbackup") & admin_filter)
async def db_backup_cmd(bot: Client, message: Message):
    status_msg = await message.reply_text("⏳ **Generating database backup...**")
    try:
        file_path = await generate_backup_file()
        caption = f"✅ **Database Backup Complete!**\n\n📅 Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n👤 **Requested By:** {message.from_user.mention if getattr(message, 'from_user', None) else 'Admin'}"
        await message.reply_document(document=file_path, caption=caption)
        os.remove(file_path)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"⚠️ **Backup Failed!**\n\nError: `{e}`")


@Client.on_message(filters.command("dbrestore") & admin_filter)
async def db_restore_cmd(bot: Client, message: Message):
    if not getattr(message, "reply_to_message", None) or not getattr(message.reply_to_message, "document", None):
        return await message.reply_text("⚙️ **Usage:**\nReply to a previously generated backup `.json` file with `/dbrestore`.")
    
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
        await status_msg.edit_text(f"✅ **Database Restored Successfully!**\n\n📁 **Collections:** `{restored_colls}`\n📄 **Documents:** `{restored_docs}`")
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
        return await message.reply_text("✅ **Auto-backup scheduler initiated!**\nThe first automated backup will run in 24 hours.")

    if next_backup_time:
        time_left = next_backup_time - time.time()
        hours, remainder = divmod(int(time_left), 3600)
        minutes, seconds = divmod(remainder, 60)
        await message.reply_text(f"⏰ **Scheduled Backup Status**\n\n🟢 **Status:** `Active`\n⏳ **Next Backup In:** `{hours}h {minutes}m {seconds}s`")
    else:
        await message.reply_text("⏰ Scheduler is active but time is calculating...")


# ============================================================
# 📢 INLINE PROMOTIONS ENGINE (/addpromo, /delpromo)
# ============================================================
@Client.on_message(filters.command("addpromo") & admin_filter & (filters.private | filters.group))
async def add_promo_handler(bot: Client, message: Message):
    text = message.text.split(None, 1)[1] if len(message.command) > 1 else ""
    match = re.search(r'"([^"]+)"\s+(https?://\S+)', text)

    if not match:
        return await message.reply_text('⚙️ **Usage:**\n`/addpromo "Button Text" https://example.com`\n\n⚠️ Ensure the button text is inside double quotes `" "` followed by a valid URL.')

    btn_text, btn_url = match.group(1), match.group(2)
    try:
        await _plugin_db.add_promo(btn_text, btn_url)
        await message.reply_text(f"✅ **Promo Added Successfully!**\n\n**Text:** `{btn_text}`\n**URL:** {btn_url}", disable_web_page_preview=True)
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{e}`")


@Client.on_message(filters.command("delpromo") & admin_filter & (filters.private | filters.group))
async def del_promo_handler(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:**\n`/delpromo https://example.com`")
    btn_url = message.command[1]
    try:
        deleted = await _plugin_db.del_promo(btn_url)
        if deleted:
            await message.reply_text(f"🗑️ **Promo deleted successfully!**\n\n**URL:** {btn_url}", disable_web_page_preview=True)
        else:
            await message.reply_text(f"❌ **Promo not found!**\nCould not find any promo matching URL: {btn_url}", disable_web_page_preview=True)
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{e}`")


@Client.on_message(filters.command("listpromos") & admin_filter & (filters.private | filters.group))
async def list_promo_handler(bot: Client, message: Message):
    try:
        promos = await _plugin_db.get_all_promos()
        if not promos:
            return await message.reply_text("⚠️ **No active promotional links found in the database.**")

        text = "📢 **Current Promotional Links:**\n\n"
        for i, p in enumerate(promos, 1):
            text += f"**{i}. Text:** `{p['text']}`\n**🔗 URL:** {p['url']}\n\n"
        await message.reply_text(text, disable_web_page_preview=True)
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{e}`")


async def get_promo_buttons() -> list:
    try:
        promos = await _plugin_db.get_all_promos()
        return [InlineKeyboardButton(p["text"], url=p["url"]) for p in promos]
    except Exception:
        return []


# ============================================================
# 📤 BROADCAST ENGINE (/broadcast, /group_broadcast)
# ============================================================
def get_progress_bar(current, total):
    if total == 0: return "[░░░░░░░░░░] 0.0%"
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
                        await bot.delete_messages(chat_id=doc["chat_id"], message_ids=doc["message_id"])
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
                sent_msg = await message.forward(chat_id=chat_id, disable_notification=True)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                sent_msg = await message.forward(chat_id=chat_id, disable_notification=True)
            except Exception:
                if getattr(message, "photo", None):
                    sent_msg = await bot.send_photo(chat_id, message.photo.file_id, caption=message.caption, disable_notification=True)
                elif getattr(message, "video", None):
                    sent_msg = await bot.send_video(chat_id, message.video.file_id, caption=message.caption, disable_notification=True)
                elif getattr(message, "document", None):
                    sent_msg = await bot.send_document(chat_id, message.document.file_id, caption=message.caption, disable_notification=True)
                else:
                    sent_msg = await bot.send_message(chat_id, message.text, disable_notification=True)

        if sent_msg and _broadcast_col is not None:
            await _broadcast_col.insert_one({"chat_id": chat_id, "message_id": sent_msg.id, "delete_at": time.time() + DELETE_DELAY})
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
        return await message.reply_text("⚠️ **Reply to the message you want to broadcast.**")

    status_msg = await message.reply_text("⏳ **Fetching user database...**")
    try:
        users_cursor = await _db.get_all_users()
        users = await users_cursor.to_list(length=None) if hasattr(users_cursor, "to_list") else list(users_cursor)
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

        if status == 200: success += 1
        elif status == 400: blocked += 1; await _db.delete_user(user_id)
        else: failed += 1

        done += 1
        if done % 25 == 0 or done == total_users:
            try:
                await status_msg.edit_text(f"📢 **User Broadcast Progress**\n{get_progress_bar(done, total_users)}\n\n👥 **Total:** `{total_users}`\n✅ **Sent:** `{success}`\n🚫 **Dead/Blocked:** `{blocked}`\n⚠️ **Failed:** `{failed}`\n📦 **Processed:** `{done}/{total_users}`")
            except FloodWait as e: await asyncio.sleep(e.value + 1)
            except Exception: pass
        await asyncio.sleep(0.3)

    time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
    time_str = f"{DELETE_DELAY // 3600} hours" if DELETE_DELAY >= 3600 else f"{(DELETE_DELAY % 3600) // 60} minutes"
    await status_msg.edit_text(f"✅ **User Broadcast Completed!**\n\n🕒 **Duration:** `{time_taken}`\n👥 **Total:** `{total_users}`\n✅ **Successful:** `{success}`\n🚫 **Removed (Dead):** `{blocked}`\n⚠️ **Failed:** `{failed}`\n\n⏳ *These messages will vanish silently in {time_str}.*")


@Client.on_message(filters.command("group_broadcast") & admin_filter & filters.reply)
async def group_broadcast(bot: Client, message: Message):
    b_msg = message.reply_to_message
    if not b_msg:
        return await message.reply_text("⚠️ **Reply to the message you want to broadcast.**")

    status_msg = await message.reply_text("⏳ **Fetching chat database...**")
    try:
        chats_cursor = await _db.get_all_chats()
        chats = await chats_cursor.to_list(length=None) if hasattr(chats_cursor, "to_list") else list(chats_cursor)
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

        if status == 200: success += 1
        elif status == 400 and reason in ["Left", "Blocked/Deleted"]:
            left += 1
            try: await _db.disable_chat(chat_id, "Bot Removed")
            except Exception: pass
        else: failed += 1

        done += 1
        if done % 15 == 0 or done == total_chats:
            try:
                await status_msg.edit_text(f"🏘️ **Group Broadcast Progress**\n{get_progress_bar(done, total_chats)}\n\n💬 **Total:** `{total_chats}`\n✅ **Sent:** `{success}`\n🚷 **Kicked/Left:** `{left}`\n⚠️ **Failed:** `{failed}`\n📦 **Processed:** `{done}/{total_chats}`")
            except FloodWait as e: await asyncio.sleep(e.value + 1)
            except Exception: pass
        await asyncio.sleep(0.5)

    time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
    time_str = f"{DELETE_DELAY // 3600} hours" if DELETE_DELAY >= 3600 else f"{(DELETE_DELAY % 3600) // 60} minutes"
    await status_msg.edit_text(f"✅ **Group Broadcast Completed!**\n\n🕒 **Duration:** `{time_taken}`\n💬 **Total:** `{total_chats}`\n✅ **Successful:** `{success}`\n🚷 **Removed From:** `{left}`\n⚠️ **Failed:** `{failed}`\n\n⏳ *These messages will vanish silently in {time_str}.*")


# ============================================================
# 🔌 CHAT CONNECTION ENGINE (/connect, /disconnect)
# ============================================================
@Client.on_message((filters.private | filters.group) & filters.command("connect"))
async def addconnection(client, message):
    try:
        userid = get_uid(message)
        if not userid:
            return await message.reply("You are anonymous admin. Use /connect in PM.")

        if message.chat.type == enums.ChatType.PRIVATE:
            try:
                cmd, group_id_str = message.text.split(" ", 1)
                group_id = int(group_id_str) if group_id_str.lstrip("-").isdigit() else group_id_str
            except Exception:
                return await message.reply_text("<b>Enter correct format!</b>\n\n<code>/connect groupid</code>", quote=True)
        else:
            group_id = message.chat.id

        st = await client.get_chat_member(group_id, userid)
        if st.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER] and userid not in get_admin_list():
            return await message.reply_text("You should be an admin in the Given group!", quote=True)

        st = await client.get_chat_member(group_id, "me")
        if st.status == enums.ChatMemberStatus.ADMINISTRATOR:
            ttl = await client.get_chat(group_id)
            if await add_connection(str(group_id), str(userid)):
                await message.reply_text(f"Successfully connected to **{ttl.title}**\nNow manage your group from my PM!", quote=True)
            else:
                await message.reply_text("You're already connected to this chat!", quote=True)
        else:
            await message.reply_text("Add me as an admin in group", quote=True)
    except Exception as e:
        await message.reply_text(f"❌ Some error occurred! Try again later.\n`{str(e)}`", quote=True)


@Client.on_message((filters.private | filters.group) & filters.command("disconnect"))
async def deleteconnection(client, message):
    try:
        userid = get_uid(message)
        if not userid: return

        if message.chat.type == enums.ChatType.PRIVATE:
            return await message.reply_text("Run /connections to view or disconnect from groups!", quote=True)
        group_id = message.chat.id

        st = await client.get_chat_member(group_id, userid)
        if st.status not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER] and userid not in get_admin_list():
            return

        if await delete_connection(str(userid), str(group_id)):
            await message.reply_text("Successfully disconnected from this chat", quote=True)
        else:
            await message.reply_text("This chat isn't connected to me!", quote=True)
    except Exception as e:
        await message.reply_text(f"❌ Error: `{str(e)}`")


@Client.on_message(filters.private & filters.command(["connections"]))
async def connections(client, message):
    try:
        userid = get_uid(message)
        groupids = await all_connections(str(userid))

        if not groupids:
            return await message.reply_text("There are no active connections!! Connect to some groups first.", quote=True)

        buttons = []
        for groupid in groupids:
            try:
                ttl = await client.get_chat(int(groupid))
                act = " - ACTIVE" if await if_active(str(userid), str(groupid)) else ""
                buttons.append([InlineKeyboardButton(text=f"{ttl.title}{act}", callback_data=f"groupcb:{groupid}:{act}")])
            except Exception: pass

        if buttons:
            await message.reply_text("Your connected group details:\n\n", reply_markup=InlineKeyboardMarkup(buttons), quote=True)
        else:
            await message.reply_text("There are no active connections!", quote=True)
    except Exception as e:
        await message.reply_text(f"❌ Error: `{str(e)}`")


# ============================================================
# 📄 EXPORT SCRIPTS
# ============================================================
@Client.on_message(filters.command("exportusers") & admin_filter)
async def export_users_cmd(bot: Client, message: Message):
    status = await message.reply_text("⏳ **Exporting users from database...**")
    try:
        users_data = await _db.get_all_users()
        users = await users_data.to_list(length=None) if hasattr(users_data, "to_list") else list(users_data)
        total = len(users)

        if total == 0: return await status.edit_text("⚠️ No users found in the database.")

        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        text = f"🧍 **User List Export**\n\n📅 Generated on: `{time_str} UTC`\n👥 Total Users: `{total}`\n\n"
        data = "\n".join([f"{user.get('id', 'Unknown')} - {user.get('name', 'Unknown')}" for user in users])

        file = io.BytesIO(data.encode("utf-8"))
        file.name = "users_list.txt"

        await message.reply_document(document=file, caption=text)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("exportgroups") & admin_filter)
async def export_groups_cmd(bot: Client, message: Message):
    status = await message.reply_text("⏳ **Exporting groups from database...**")
    try:
        chats_data = await _db.get_all_chats()
        chats = await chats_data.to_list(length=None) if hasattr(chats_data, "to_list") else list(chats_data)
        total = len(chats)

        if total == 0: return await status.edit_text("⚠️ No groups found in the database.")

        groups = [chat for chat in chats if not str(chat.get("id", "")).startswith("-100")]
        supergroups = [chat for chat in chats if str(chat.get("id", "")).startswith("-100")]

        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        text = f"💬 **Group Chats Export**\n\n📅 Generated on: `{time_str} UTC`\n🏘️ Total Chats: `{total}`\n👥 Groups: `{len(groups)}` | 📢 Supergroups: `{len(supergroups)}`\n\n"
        data = "\n".join([f"{chat.get('id', 'Unknown')} - {chat.get('title', 'Unknown')}" for chat in chats])

        file = io.BytesIO(data.encode("utf-8"))
        file.name = "chats_list.txt"

        await message.reply_document(document=file, caption=text)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("exportchannels") & admin_filter)
async def export_channels_cmd(bot: Client, message: Message):
    status = await message.reply_text("⏳ **Exporting channels from database...**")
    try:
        chats_data = await _db.get_all_chats()
        chats = await chats_data.to_list(length=None) if hasattr(chats_data, "to_list") else list(chats_data)
        channels = [chat for chat in chats if str(chat.get("id", "")).startswith("-100")]
        total = len(channels)

        if total == 0: return await status.edit_text("⚠️ No channels found in the database.")

        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        text = f"📢 **Channel List Export**\n\n📅 Generated on: `{time_str} UTC`\n📺 Total Channels: `{total}`\n\n"
        data = "\n".join([f"{chat.get('id', 'Unknown')} - {chat.get('title', 'Unknown')}" for chat in channels])

        file = io.BytesIO(data.encode("utf-8"))
        file.name = "channels_list.txt"

        await message.reply_document(document=file, caption=text)
        await status.delete()
    except Exception as e:
        await status.edit_text(f"❌ **Error:**\n`{str(e)}`")


# ============================================================
# 🚪 JOIN REQUEST CATCHER & MANAGER
# ============================================================
@Client.on_chat_join_request()
async def join_reqs_handler(bot: Client, join_req: ChatJoinRequest):
    if not _join_reqs_db.isActive(): return
    user = join_req.from_user
    try:
        await _join_reqs_db.add_user(user_id=user.id, first_name=user.first_name or "Unknown", username=user.username or "None", date=join_req.date)
    except Exception as e: logger.error(f"Failed to log join request for {user.id}: {e}")

@Client.on_message(filters.command("totalrequests") & admin_filter)
async def total_requests(bot: Client, message: Message):
    if not _join_reqs_db.isActive(): return await message.reply_text("⚠️ Join request tracking is not active (DB inactive).")
    try:
        total = await _join_reqs_db.total_requests()
        await message.reply_text(f"📨 **Total Join Requests:** <code>{total}</code>")
    except Exception as e: await message.reply_text(f"⚠️ Error: <code>{e}</code>")

@Client.on_message(filters.command("purgerequests") & admin_filter)
async def purge_requests(bot: Client, message: Message):
    if not _join_reqs_db.isActive(): return await message.reply_text("⚠️ Join request tracking is not active (DB inactive).")
    uid = get_uid(message)
    ask_msg = await message.reply_text("⚠️ **Are you sure you want to delete all join requests? (y/n)**")
    try:
        resp = await native_listen(bot, message.chat.id, uid, timeout=60)
        try: await ask_msg.delete()
        except Exception: pass
        if resp:
            try: await resp.delete()
            except Exception: pass

        if not resp or not resp.text: return
        if resp.text.lower() == "y":
            count = await _join_reqs_db.clear_all()
            await message.reply_text(f"✅ All join requests purged successfully. (<code>{count}</code> deleted)")
        else: await message.reply_text("❌ Operation cancelled.")
    except asyncio.TimeoutError: await message.reply_text("⌛ Timeout: No response, operation cancelled.")
    except Exception as e: await message.reply_text(f"⚠️ Error clearing requests: <code>{e}</code>")


# ============================================================
# ⚙️ MULTI-CHANNEL FORCESUB MANAGER
# ============================================================
async def get_fsub_config():
    if _fsub_db is not None:
        doc = await _fsub_db.find_one({"_id": "config"})
        if doc: return doc
    return {"is_enabled": True, "max_count": 0, "rotation_enabled": False, "channels": {}}

async def update_fsub_config(key, value):
    if _fsub_db is not None: await _fsub_db.update_one({"_id": "config"}, {"$set": {key: value}}, upsert=True)

async def update_fsub_channels(channels_dict):
    if _fsub_db is not None: await _fsub_db.update_one({"_id": "config"}, {"$set": {"channels": channels_dict}}, upsert=True)

async def add_vip(user_id: int):
    if _vip_db is not None: await _vip_db.update_one({"user_id": user_id}, {"$set": {"is_vip": True}}, upsert=True)

async def rem_vip(user_id: int):
    if _vip_db is not None: await _vip_db.delete_one({"user_id": user_id})

async def is_vip(user_id: int):
    if _vip_db is not None: return bool(await _vip_db.find_one({"user_id": user_id}))
    return False

async def get_all_vips():
    if _vip_db is not None: return await _vip_db.find().to_list(length=None)
    return []

async def get_invite_link(bot: Client, chat_id_str: str, config: dict) -> str:
    channels = config.get("channels") or {}
    channel_data = channels.get(chat_id_str, {})
    if channel_data.get("target"): return channel_data["target"]
    if channel_data.get("link"): return channel_data["link"]

    try:
        chat_id_int = int(chat_id_str.strip())
        chat = await bot.get_chat(chat_id_int)
        is_req = channel_data.get("type") == "req"
        invite = await bot.create_chat_invite_link(chat_id_int, creates_join_request=is_req)

        channels[chat_id_str]["link"] = invite.invite_link
        channels[chat_id_str]["title"] = chat.title
        await update_fsub_channels(channels)
        return invite.invite_link
    except Exception: return ""

async def check_user_in_channel(bot: Client, channel_id: int, user_id: int, is_req: bool = False) -> bool:
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        if member.status in [enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]: return True
    except UserNotParticipant: pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await check_user_in_channel(bot, channel_id, user_id, is_req)
    except Exception: pass

    if is_req:
        try:
            if _join_reqs_db.isActive() and await _join_reqs_db.get_user(user_id): return True
        except Exception: pass
    return False

# LOCAL Subscription checker to prevent import crash
async def local_is_subscribed(bot: Client, user_id: int) -> bool:
    auth_ch = getattr(info, "AUTH_CHANNEL", None)
    req_ch = getattr(info, "REQ_CHANNEL", None)
    if not auth_ch and not req_ch: return True
    
    if auth_ch:
        try:
            ch_id = int(auth_ch) if str(auth_ch).lstrip('-').isdigit() else auth_ch
            if await check_user_in_channel(bot, ch_id, user_id, False): pass
            else: return False
        except Exception: pass
            
    if req_ch:
        try:
            ch_id = int(req_ch) if str(req_ch).lstrip('-').isdigit() else req_ch
            if await check_user_in_channel(bot, ch_id, user_id, True): pass
            else: return False
        except Exception: pass
    return True


async def ForceSub(bot: Client, message: Message, file_id: str = None, mode: str = None) -> bool:
    user = getattr(message, "from_user", None)
    if not user: return True
    user_id = user.id

    if user_id in get_admin_list() or await is_vip(user_id): return True

    config = await get_fsub_config()
    if not config.get("is_enabled", True): return True
    channels_dict = config.get("channels") or {}
    if not channels_dict: return True

    rotation_enabled, max_count = config.get("rotation_enabled", False), config.get("max_count", 0)

    if rotation_enabled:
        eligible_channels = list(channels_dict.keys())
        if eligible_channels:
            shift = int(time.time() / 3600) % len(eligible_channels)
            eligible_channels = eligible_channels[shift:] + eligible_channels[:shift]
    else:
        eligible_channels = [k for k, v in channels_dict.items() if v.get("status") == "active"]

    not_joined_buttons, channels_to_force = [], 0

    try:
        for chat_id_str in eligible_channels:
            if max_count > 0 and channels_to_force >= max_count: break
            data = channels_dict[chat_id_str]
            chat_id = int(chat_id_str)
            is_req = data.get("type") == "req"
            is_participant = await check_user_in_channel(bot, chat_id, user.id, is_req)

            if not is_participant:
                link = await get_invite_link(bot, chat_id_str, config)
                if link:
                    btn_text = "⚓ Request to Join" if is_req else "📢 Join Channel"
                    not_joined_buttons.append([InlineKeyboardButton(btn_text, url=link)])
                    channels_to_force += 1

        if not not_joined_buttons:
            await _plugin_db.add_fsub_user(user.id)
            return True

        cb_data = f"refresh_fsub_{file_id}" if file_id else "refresh_fsub_0"
        not_joined_buttons.append([InlineKeyboardButton(text="✅ I've Joined", callback_data=cb_data)])

        from plugins.custom_settings import get_bot_settings
        b_settings = await get_bot_settings()
        custom_fsub_img = b_settings.get("fsub_img", getattr(info, "FSUB_IMG", None))

        try:
            if custom_fsub_img and str(custom_fsub_img).strip():
                try: await message.reply_photo(photo=custom_fsub_img, caption="**⚠️ Please join the channels below to get your file!**", reply_markup=InlineKeyboardMarkup(not_joined_buttons))
                except Exception: await message.reply_text("**⚠️ Please join the channels below to get your file!**", reply_markup=InlineKeyboardMarkup(not_joined_buttons), disable_web_page_preview=True)
            else: await message.reply_text("**⚠️ Please join the channels below to get your file!**", reply_markup=InlineKeyboardMarkup(not_joined_buttons), disable_web_page_preview=True)
        except UserIsBlocked: return False
        return False
    except Exception: return True


@Client.on_callback_query(filters.regex(r"^refresh_fsub_(.*)"))
async def refresh_fsub_callback(bot: Client, query: CallbackQuery):
    try:
        user_id, file_id = query.from_user.id, query.matches[0].group(1)
        if await is_vip(user_id): return await query.answer("👑 VIP Verified!", show_alert=False)

        config = await get_fsub_config()
        channels_dict = config.get("channels") or {}
        rotation_enabled, max_count = config.get("rotation_enabled", False), config.get("max_count", 0)

        if not channels_dict: return await query.answer("Force subscribe is no longer required.", show_alert=True)

        if rotation_enabled:
            eligible_channels = list(channels_dict.keys())
            if eligible_channels:
                shift = int(time.time() / 3600) % len(eligible_channels)
                eligible_channels = eligible_channels[shift:] + eligible_channels[:shift]
        else:
            eligible_channels = [k for k, v in channels_dict.items() if v.get("status") == "active"]

        not_joined_buttons, channels_to_force = [], 0

        for chat_id_str in eligible_channels:
            if max_count > 0 and channels_to_force >= max_count: break
            data = channels_dict[chat_id_str]
            is_req = data.get("type") == "req"
            is_participant = await check_user_in_channel(bot, int(chat_id_str), user_id, is_req)

            if not is_participant:
                link = await get_invite_link(bot, chat_id_str, config)
                if link:
                    btn_text = "⚓ Request to Join" if is_req else "📢 Join Channel"
                    not_joined_buttons.append([InlineKeyboardButton(btn_text, url=link)])
                    channels_to_force += 1

        if channels_to_force > 0:
            await query.answer("❌ You haven't joined all required channels yet!", show_alert=True)
            cb_data = f"refresh_fsub_{file_id}" if file_id else "refresh_fsub_0"
            not_joined_buttons.append([InlineKeyboardButton(text="✅ I've Joined", callback_data=cb_data)])
            try: await query.message.edit_reply_markup(InlineKeyboardMarkup(not_joined_buttons))
            except MessageNotModified: pass
        else:
            await _plugin_db.add_fsub_user(user_id)
            try: await query.message.delete()
            except Exception: pass

            await query.answer("✅ Subscriptions verified!", show_alert=False)

            if file_id and str(file_id) != "0":
                if file_id.startswith("search_") or file_id.startswith("hdra_"):
                    try: await bot.send_message(chat_id=user_id, text="✅ **Verification complete!**\n\n🔍 Your search is unlocked. Please click the button in the main group again to get your files!")
                    except Exception: pass
                else:
                    files_ = await get_file_details(file_id)
                    if files_:
                        file = files_[0]
                        title = file.get("file_name", "Unknown")
                        size = get_size(file.get("file_size", 0))
                        f_caption = file.get("caption", "")
                        db_file_id = file.get("full_file_id", file.get("file_id", file_id))

                        if getattr(info, "CUSTOM_FILE_CAPTION", None):
                            try: f_caption = info.CUSTOM_FILE_CAPTION.format(file_name=title, file_size=size, file_caption=f_caption)
                            except Exception: pass
                        if not f_caption: f_caption = f"<b>{title}</b>"

                        try:
                            btn = [[InlineKeyboardButton("🎥 ಕನ್ನಡ ಹೊಸ ಮೂವೀಗಳು 🎥", url="https://t.me/sandalwood_kannada_moviesz")]]
                            await bot.send_cached_media(chat_id=user_id, file_id=db_file_id, caption=f_caption, reply_markup=InlineKeyboardMarkup(btn))
                        except Exception:
                            await bot.send_message(chat_id=user_id, text="✅ **Verification complete!**\n\n⚠️ *Please go back to the main group and click the file link again to get your movie!*")
                    else:
                        await bot.send_message(chat_id=user_id, text="✅ **Verification complete!**\n\n⚠️ *Please go back to the main group and click the link again.*")
    except Exception as e:
        await query.answer(f"❌ Callback Error: {str(e)[:40]}", show_alert=True)


# ============================================================
# ⚙️ CONFIGURATION COMMANDS
# ============================================================
@Client.on_message(filters.command("rotatefsub") & admin_filter)
async def toggle_rotate(bot: Client, message: Message):
    try:
        config = await get_fsub_config()
        current = config.get("rotation_enabled", False)
        await update_fsub_config("rotation_enabled", not current)
        status = "🟢 ENABLED (Smart 1-Hour User Rotation)" if not current else "🔴 DISABLED (Static Priority)"
        await message.reply_text(f"🔄 **Auto-Rotation Engine:** {status}\n\n*When enabled, the bot automatically cycles through all channels and forces users into the ones they haven't joined yet!*")
    except Exception as e: await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("enablefsub") & admin_filter)
async def enable_fsub(bot: Client, message: Message):
    await update_fsub_config("is_enabled", True)
    await message.reply_text("✅ **Force Subscribe has been ENABLED.**")


@Client.on_message(filters.command("disablefsub") & admin_filter)
async def disable_fsub(bot: Client, message: Message):
    await update_fsub_config("is_enabled", False)
    await message.reply_text("❌ **Force Subscribe has been DISABLED.**")


@Client.on_message(filters.command("setfsubcount") & admin_filter)
async def set_fsub_count(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/setfsubcount [number]`\n*Set to 0 for unlimited active channels.*")
    try:
        count = int(message.command[1])
        if count < 0: raise ValueError
        await update_fsub_config("max_count", count)
        limit_text = f"maximum `{count}` active channels at once" if count > 0 else "unlimited channels simultaneously"
        await message.reply_text(f"✅ **FSub Queue Limit Updated!**\nThe system will now force a {limit_text}.")
    except ValueError:
        return await message.reply_text("❌ **Invalid number.** Please provide a valid positive integer.")


@Client.on_message(filters.command("setfsub") & admin_filter)
async def add_dynamic_fsub(bot: Client, message: Message):
    try:
        if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/setfsub [channel_id]`")
        channel_id, uid = message.command[1], get_uid(message)

        try:
            cid = int(channel_id) if channel_id.lstrip("-").isdigit() else channel_id
            chat = await bot.get_chat(cid)
        except Exception as e:
            return await message.reply_text(f"❌ **Failed to fetch channel.** Make sure the bot is an admin in `{channel_id}`.\n`Error: {e}`")

        ask_msg = await message.reply_text(f"🎯 **Target:** `{chat.title}`\n\nDo you want this to be a **Join Request** channel?\n\nReply with `y` for Yes, or `n` for No (Normal invite).")
        try:
            resp = await native_listen(bot, message.chat.id, uid, timeout=120)
            try: await ask_msg.delete()
            except Exception: pass
            if resp:
                try: await resp.delete()
                except Exception: pass

            if not resp or not resp.text: return await message.reply_text("❌ **Invalid response.** Please send text (y/n).")

            is_req = resp.text.lower() == "y"
            fsub_type = "req" if is_req else "regular"

            config = await get_fsub_config()
            channels = config.get("channels") or {}
            max_count = config.get("max_count", 0)

            active_count = len([c for c in channels.values() if c.get("status") == "active"])
            status = "pending" if max_count > 0 and active_count >= max_count else "active"

            invite = await bot.create_chat_invite_link(chat.id, creates_join_request=is_req)

            channels[channel_id] = {"title": chat.title, "link": invite.invite_link, "target": None, "type": fsub_type, "status": status}
            await update_fsub_channels(channels)

            await message.reply_text(f"✅ **Successfully Configured FSub!**\n\n📢 **Channel:** `{chat.title}`\n🔗 **Link:** {invite.invite_link}\n⚙️ **Type:** `{'Join Request' if is_req else 'Normal'}`\n🟢 **Status:** `{status.upper()}`", disable_web_page_preview=True)
        except ChatAdminRequired: await message.reply_text("❌ **The bot is not an admin in that channel or lacks 'Invite Users' rights.**")
        except asyncio.TimeoutError: await message.reply_text("⌛ **Timeout.** Setup cancelled.")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{traceback.format_exc()}`")


@Client.on_message(filters.command("activatefsub") & admin_filter)
async def activate_fsub(bot: Client, message: Message):
    try:
        if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/activatefsub [channel_id]`")
        channel_id = message.command[1]

        config = await get_fsub_config()
        channels = config.get("channels") or {}
        max_count = config.get("max_count", 0)

        if channel_id not in channels: return await message.reply_text("❌ Channel not found in FSub database.")

        active_count = len([c for c in channels.values() if c.get("status") == "active"])
        if max_count > 0 and active_count >= max_count:
            return await message.reply_text(f"⚠️ **Queue Full!** You already have `{active_count}` active channels (Limit: {max_count}).\nUse `/deactivatefsub` on another channel first.")

        channels[channel_id]["status"] = "active"
        await update_fsub_channels(channels)
        await message.reply_text(f"✅ FSub for `{channel_id}` is now **ACTIVE**.")
    except Exception as e: await message.reply_text(f"❌ **Error:** `{e}`")


@Client.on_message(filters.command("deactivatefsub") & admin_filter)
async def deactivate_fsub(bot: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/deactivatefsub [channel_id]`")
    channel_id = message.command[1]
    config = await get_fsub_config()
    channels = config.get("channels") or {}

    if channel_id in channels:
        channels[channel_id]["status"] = "pending"
        await update_fsub_channels(channels)
        await message.reply_text(f"⏸ FSub for `{channel_id}` is now **PENDING (Deactivated)**.")
    else: await message.reply_text("❌ Channel not found in FSub database.")


@Client.on_message(filters.command("updatefsubtarget") & admin_filter)
async def update_fsub_target(bot: Client, message: Message):
    if len(message.command) < 3: return await message.reply_text("⚙️ **Usage:** `/updatefsubtarget [channel_id] [target_link]`\n*Use 'none' to remove custom target.*")
    channel_id, target = str(message.command[1]), message.command[2]

    config = await get_fsub_config()
    channels = config.get("channels") or {}

    if channel_id not in channels: return await message.reply_text(f"❌ Channel `{channel_id}` is not configured in FSub.")

    if target.lower() == "none":
        channels[channel_id]["target"] = None
        await message.reply_text("✅ Custom target removed. Bot will use standard invite link.")
    else:
        channels[channel_id]["target"] = target
        await message.reply_text(f"✅ Target for `{channel_id}` updated to:\n{target}")

    await update_fsub_channels(channels)


@Client.on_message(filters.command("rmfsub") & admin_filter)
async def rem_fsub(bot: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/rmfsub [channel_id]`")
    channel_id = message.command[1]
    config = await get_fsub_config()
    channels = config.get("channels") or {}

    if channel_id in channels:
        del channels[channel_id]
        await update_fsub_channels(channels)
        await message.reply_text(f"🗑️ **ForceSub channel `{channel_id}` removed permanently.**")
    else: await message.reply_text("❌ Channel not found in FSub database.")


@Client.on_message(filters.command("rmallfsub") & admin_filter)
async def rem_all_fsub(bot: Client, message: Message):
    await update_fsub_channels({})
    await message.reply_text("🗑️ **ALL ForceSub channels have been removed and database is empty.**")


async def build_fsub_list_text(title_prefix: str, filter_status: str = None) -> str:
    config = await get_fsub_config()
    channels_dict = config.get("channels") or {}
    channels = channels_dict.items()
    if filter_status: channels = [(k, v) for k, v in channels if v.get("status") == filter_status]

    if not channels: return f"❌ No {title_prefix.lower()} ForceSub channels found."
    text = f"📋 **{title_prefix} ForceSub Channels:**\n\n"
    for idx, (cid, data) in enumerate(channels, 1):
        status_emoji = "🟢" if data.get("status") == "active" else "🟡"
        req_type = "Req" if data.get("type") == "req" else "Normal"
        active_link = data.get("target") or data.get("link") or "No Link Generated"
        text += f"{idx}. {status_emoji} **{data.get('title', 'Unknown')}**\n├ ID: `{cid}`\n├ Type: `{req_type}`\n└ Link: {active_link}\n\n"
    return text


@Client.on_message(filters.command("getallfsub") & admin_filter)
async def get_all_fsub(bot: Client, message: Message):
    try:
        config = await get_fsub_config()
        max_count = config.get("max_count", 0)
        rot_status = "ON" if config.get("rotation_enabled", False) else "OFF"
        text = f"⚙️ **Queue Limit:** `{max_count if max_count > 0 else 'Unlimited'}`\n🔄 **Auto-Rotation:** `{rot_status}`\n\n" + await build_fsub_list_text("All")
        await message.reply_text(text, disable_web_page_preview=True)
    except Exception as e: await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("getactivefsub") & admin_filter)
async def get_active_fsub(bot: Client, message: Message):
    await message.reply_text(await build_fsub_list_text("Active", "active"), disable_web_page_preview=True)


@Client.on_message(filters.command("getpendingfsub") & admin_filter)
async def get_pending_fsub(bot: Client, message: Message):
    await message.reply_text(await build_fsub_list_text("Pending", "pending"), disable_web_page_preview=True)


# ============================================================
# 🎨 FILTER COMMANDS, SEARCH INTERCEPTOR AND AUTO-FILTER HOOK
# ============================================================
async def db_add_filter(grp_id: int, keyword: str, text: str, btn: str, alert: str, fileid: str):
    try: return await add_filter(grp_id, keyword, text, btn, alert, fileid)
    except Exception as e: logger.warning(f"Layer 1 add_filter failed, attempting direct DB: {e}")
    if _filter_col is not None:
        await _filter_col.update_one({"chat_id": int(grp_id), "keyword": keyword}, {"$set": {"text": text, "btn": btn, "alert": alert, "fileid": fileid}}, upsert=True)

async def db_find_filter(grp_id: int, keyword: str):
    try:
        reply_text, btn, alert, fileid = await find_filter(grp_id, keyword)
        if reply_text or (fileid and fileid != "None"): return reply_text, btn, alert, fileid
    except Exception as e: logger.warning(f"Layer 1 find_filter failed, attempting direct DB: {e}")
    if _filter_col is not None:
        doc = await _filter_col.find_one({"chat_id": int(grp_id), "keyword": keyword})
        if doc: return (doc.get("text", ""), doc.get("btn", "[]"), doc.get("alert", "[]"), doc.get("fileid", "None"))
    return None, "[]", "[]", "None"

async def db_delete_filter(message: Message, keyword: str, grp_id: int):
    try:
        await delete_filter(message, keyword, grp_id)
        return True
    except Exception as e: logger.warning(f"Layer 1 delete_filter failed, attempting direct DB: {e}")
    if _filter_col is not None:
        await _filter_col.delete_one({"chat_id": int(grp_id), "keyword": keyword})
        return True
    return False

async def db_get_filters(grp_id: int):
    try:
        res = await get_filters(grp_id)
        if res is not None: return res
    except Exception as e: logger.warning(f"Layer 1 get_filters failed, attempting direct DB: {e}")
    if _filter_col is not None:
        return [d["keyword"] for d in await _filter_col.find({"chat_id": int(grp_id)}).to_list(length=None) if "keyword" in d]
    return []

def build_keyboard(btn_str: str):
    if not btn_str or btn_str in ["[]", "None", "False", ""]: return None
    try:
        parsed_btn = ast.literal_eval(btn_str)
        button_layout = []
        for row in parsed_btn:
            btn_row = []
            for b in row:
                if isinstance(b, dict):
                    b_copy = b.copy()
                    style_val = b_copy.pop("style", None)
                    final_style = {1: BTN_PRIMARY, 3: BTN_SUCCESS, 4: BTN_DANGER}.get(style_val, style_val)
                    btn_row.append(InlineKeyboardButton(text=b_copy.get("text", "Button"), url=b_copy.get("url"), callback_data=b_copy.get("callback_data"), **({"style": final_style} if final_style else {})))
                else: btn_row.append(b)
            button_layout.append(btn_row)
        return button_layout
    except Exception as e:
        logger.error(f"Button parsing error: {e}")
        return None

def parse_markdown_buttons(text: str):
    if not text: return "", "[]"
    buttons, clean_lines = [], []
    for line in text.split("\n"):
        row_btns = []
        matches = list(re.finditer(r"\[([^\[\]]+)\]\(([^()]+)\)", line)) + list(re.finditer(r"\[([^\[\]]+)\|([^()]+)\]", line))
        matches.sort(key=lambda m: m.start())
        for match in matches: row_btns.append({"text": match.group(1).strip(), "url": match.group(2).strip()})
        if row_btns: buttons.append(row_btns)
        clean_line = re.sub(r"\[([^\[\]]+)\|([^()]+)\]", "", re.sub(r"\[([^\[\]]+)\]\(([^()]+)\)", "", line)).strip()
        if clean_line: clean_lines.append(clean_line)
        elif not matches: clean_lines.append("")
    return "\n".join(clean_lines).strip(), str(buttons) if buttons else "[]"


async def get_target_group(client: Client, message: Message):
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        grp_id = message.chat.id
    else:
        grp_id = await active_connection(str(get_uid(message)))
        if not grp_id:
            await message.reply_text("⚠️ **You are not connected to any active group!**\n\nUse `/connect <group_id>` to connect to a group first.")
            return None, False
    if not await is_group_admin(client, grp_id, get_uid(message)):
        await message.reply_text("⚠️ **You must be an admin of the connected group to use this command.**")
        return None, False
    return grp_id, True


@Client.on_message(filters.command("filter") & (filters.group | filters.private))
async def add_filter_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok: return
    if not getattr(message, "reply_to_message", None): return await message.reply_text("⚠️ **Reply to a message to set it as a filter.**")
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/filter <keyword>`")

    keyword = message.text.split(None, 1)[1].lower().strip()
    replied = message.reply_to_message
    text, btn = parse_markdown_buttons(replied.text or replied.caption or "")

    fileid = "None"
    for media_attr in ["photo", "video", "document", "audio", "animation", "sticker"]:
        media_obj = getattr(replied, media_attr, None)
        if media_obj:
            fileid = media_obj.file_id
            break

    await db_add_filter(grp_id, keyword, text, btn, "[]", fileid)
    await message.reply_text(f"✅ **Filter successfully added!**\n\n**Keyword:** `{keyword}`")


@Client.on_message(filters.command("addfilter") & (filters.group | filters.private))
async def add_premade_filter_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok: return
    if not getattr(message, "reply_to_message", None): return await message.reply_text("⚠️ **Reply to a message containing inline buttons.**")
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/addfilter <keyword>`")

    keyword = message.text.split(None, 1)[1].lower().strip()
    replied = message.reply_to_message
    buttons = []
    if replied.reply_markup and getattr(replied.reply_markup, "inline_keyboard", None):
        for row in replied.reply_markup.inline_keyboard:
            row_btns = []
            for btn in row:
                btn_dict = {"text": btn.text}
                if btn.url: btn_dict["url"] = btn.url
                elif btn.callback_data: btn_dict["callback_data"] = btn.callback_data
                if hasattr(btn, "style") and btn.style: btn_dict["style"] = int(btn.style)
                row_btns.append(btn_dict)
            if row_btns: buttons.append(row_btns)

    fileid = "None"
    for media_attr in ["photo", "video", "document", "audio", "animation", "sticker"]:
        media_obj = getattr(replied, media_attr, None)
        if media_obj:
            fileid = media_obj.file_id
            break

    await db_add_filter(grp_id, keyword, replied.text or replied.caption or "", str(buttons) if buttons else "[]", "[]", fileid)
    await message.reply_text(f"✅ **Filter with buttons added!**\n\n**Keyword:** `{keyword}`")


@Client.on_message(filters.command("filterimage") & (filters.group | filters.private))
async def edit_filter_image_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok: return
    if not getattr(message, "reply_to_message", None) or not getattr(message.reply_to_message, "media", None):
        return await message.reply_text("⚠️ **Reply to a photo, video, or document to set the new image.**")
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/filterimage <keyword>`")

    keyword = message.text.split(None, 1)[1].lower().strip()
    reply_text, btn, alert, old_fileid = await db_find_filter(grp_id, keyword)
    if not reply_text and (not old_fileid or old_fileid == "None"): return await message.reply_text(f"❌ Filter `{keyword}` not found.")

    fileid = "None"
    for media_attr in ["photo", "video", "document", "audio", "animation", "sticker"]:
        media_obj = getattr(message.reply_to_message, media_attr, None)
        if media_obj:
            fileid = media_obj.file_id
            break

    if fileid == "None": return await message.reply_text("❌ Could not extract valid media.")
    await db_add_filter(grp_id, keyword, reply_text, btn, alert, fileid)
    await message.reply_text(f"✅ **Filter image updated!**\n\n**Keyword:** `{keyword}`")


@Client.on_message(filters.command(["editfiltercolur", "editfiltercolour"]) & (filters.group | filters.private))
async def edit_filter_colour_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok: return
    args = message.command
    if len(args) < 4: return await message.reply_text("❌ **Usage:** `/editfiltercolur <keyword> <button_number> <colour>`\n\n**Colours:** `green`, `red`, `blue`")

    try: btn_num, color_str, keyword = int(args[-2]), args[-1].lower(), " ".join(args[1:-2]).lower().strip()
    except ValueError: return await message.reply_text("❌ Button number must be an integer.")

    color_map = {"green": 3, "red": 4, "blue": 1}
    if color_str not in color_map: return await message.reply_text("❌ Invalid colour. Choose from: `green`, `red`, `blue`.")

    reply_text, btn, alert, fileid = await db_find_filter(grp_id, keyword)
    if not reply_text and (not fileid or fileid == "None"): return await message.reply_text(f"❌ Filter `{keyword}` not found.")
    if not btn or btn in ["[]", "None", "False", ""]: return await message.reply_text(f"❌ Filter `{keyword}` has no buttons.")

    try: button_data = ast.literal_eval(btn)
    except Exception: return await message.reply_text("❌ Corrupted button format.")

    count, found = 0, False
    for r_idx, row in enumerate(button_data):
        for c_idx, b in enumerate(row):
            count += 1
            if count == btn_num:
                button_data[r_idx][c_idx]["style"] = color_map[color_str]
                found = True
                break
        if found: break

    if not found: return await message.reply_text(f"❌ Button {btn_num} not found. Filter has {count} buttons.")
    await db_add_filter(grp_id, keyword, reply_text, str(button_data), alert, fileid)
    await message.reply_text(f"✅ Filter `{keyword}` Button {btn_num} colour changed to {color_str.title()}!")


@Client.on_message(filters.command("delfilter") & (filters.group | filters.private))
async def del_filter_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok: return
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/delfilter <keyword>`")
    keyword = message.text.split(None, 1)[1].lower().strip()
    await db_delete_filter(message, keyword, grp_id)
    await message.reply_text(f"🗑️ **Filter `{keyword}` deleted.**")


@Client.on_message(filters.command("listfilters") & (filters.group | filters.private))
async def list_filters_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok: return
    keywords = await db_get_filters(grp_id)
    if not keywords: return await message.reply_text("⚠️ **No active filters found.**")
    await message.reply_text("📋 **Current Filters:**\n\n" + "\n".join([f"• `{kw}`" for kw in keywords]))


async def send_filter_media(client: Client, chat_id: int, fileid: str, reply_text: str, reply_markup, reply_id: int):
    try: return await client.send_cached_media(chat_id, fileid, caption=reply_text, reply_markup=reply_markup, reply_to_message_id=reply_id)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await send_filter_media(client, chat_id, fileid, reply_text, reply_markup, reply_id)
    except (UserIsBlocked, PeerIdInvalid): return None
    except Exception: pass

    try: return await client.send_cached_media(chat_id, fileid, caption=reply_text, reply_markup=reply_markup)
    except Exception: pass

    for send_fn, arg_name in [(client.send_photo, "photo"), (client.send_video, "video"), (client.send_document, "document"), (client.send_animation, "animation")]:
        try: return await send_fn(**{"chat_id": chat_id, arg_name: fileid, "caption": reply_text, "reply_markup": reply_markup})
        except Exception: continue

    try: return await client.send_message(chat_id, text=reply_text or "Here is your file:", reply_markup=reply_markup)
    except Exception as err:
        logger.error(f"Media filter failed all 4 layers: {err}")
        return None


async def safe_send_text_filter(client: Client, chat_id: int, text: str, reply_markup, reply_id: int):
    try: return await client.send_message(chat_id, text, disable_web_page_preview=True, reply_markup=reply_markup, reply_to_message_id=reply_id)
    except Exception:
        try: return await client.send_message(chat_id, text, disable_web_page_preview=True, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Text filter failed: {e}")
            return None


@Client.on_message(filters.group & filters.text & ~filters.bot, group=5)
async def filter_listener_hook(client: Client, message: Message):
    if message.text.startswith("/"): return
    if getattr(info, "REPAIR_MODE", False):
        if get_uid(message) not in get_admin_list(): return

    group_id = message.chat.id
    if message.chat.type == enums.ChatType.PRIVATE:
        active_grp = await active_connection(str(get_uid(message)))
        if active_grp: group_id = active_grp

    name = message.text or message.caption or ""
    reply_id = message.reply_to_message.id if getattr(message, "reply_to_message", None) else message.id
    keywords = await db_get_filters(group_id)
    if not keywords: return

    for keyword in reversed(sorted(keywords, key=len)):
        if re.search(r"( |^|[^\w])" + re.escape(keyword) + r"( |$|[^\w])", name, flags=re.IGNORECASE):
            reply_text, btn, alert, fileid = await db_find_filter(group_id, keyword)
            if reply_text: reply_text = reply_text.replace("\\n", "\n").replace("\\t", "\t")
            button_layout = build_keyboard(btn)
            reply_markup = InlineKeyboardMarkup(button_layout) if button_layout else None
            fileid_str = str(fileid).strip()

            sent_msg = await safe_send_text_filter(client, message.chat.id, reply_text or "", reply_markup, reply_id) if not fileid or fileid_str in ["None", "[]", "", "False"] else await send_filter_media(client, message.chat.id, fileid, reply_text or "", reply_markup, reply_id)

            if sent_msg:
                delete_timer = getattr(info, "BUTTON_AUTO_DELETE", 1800)
                if delete_timer > 0:
                    asyncio.create_task(client.delete_messages(sent_msg.chat.id, sent_msg.id) if delete_timer == 0 else asyncio.sleep(0))
            return


@Client.on_inline_query()
async def inline_answer(bot, query: InlineQuery):
    try:
        cache_time = 0 if getattr(info, "AUTH_USERS", []) or getattr(info, "AUTH_CHANNEL", None) else getattr(info, "CACHE_TIME", 300)
        auth_users = getattr(info, "AUTH_USERS", [])
        if auth_users and query.from_user and query.from_user.id not in auth_users:
            return await query.answer(results=[], cache_time=0, switch_pm_text="okDa", switch_pm_parameter="hehe")
        
        # 👑 Local subscription check replaces external import
        if getattr(info, "AUTH_CHANNEL", None) or getattr(info, "REQ_CHANNEL", None):
            is_subbed = await local_is_subscribed(bot, query.from_user.id)
            if not is_subbed:
                return await query.answer(results=[], cache_time=0, switch_pm_text="You have to subscribe to use this bot", switch_pm_parameter="subscribe")

        string, file_type = ((query.query.split("|", 1)[0].strip(), query.query.split("|", 1)[1].strip().lower()) if "|" in query.query else (query.query.strip(), None))
        files, next_offset, total = await get_search_results(string, file_type=file_type, max_results=10, offset=int(query.offset or 0))

        results = []
        for file in files:
            f_caption = file.caption
            if getattr(info, "CUSTOM_FILE_CAPTION", None):
                try: f_caption = info.CUSTOM_FILE_CAPTION.format(file_name="" if file.file_name is None else file.file_name, file_size="" if get_size(file.file_size) is None else get_size(file.file_size), file_caption="" if f_caption is None else f_caption)
                except Exception: pass
            results.append(InlineQueryResultCachedDocument(title=file.file_name, document_file_id=file.file_id, caption=f_caption or f"{file.file_name}", description=f"Size: {get_size(file.file_size)}\nType: {file.file_type}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("♻️ 𝗦𝗲𝗮𝗿𝗰𝗵 𝗔𝗴𝗮𝗶𝗻 ♻️", switch_inline_query_current_chat=string)]])))

        if results:
            try: await query.answer(results=results, is_personal=True, cache_time=cache_time, switch_pm_text=(f"📁 Results - {total} for {string}" if string else f"📁 Results - {total}"), switch_pm_parameter="start", next_offset=str(next_offset))
            except QueryIdInvalid: pass
            except Exception as e: logger.exception(str(e))
        else:
            await query.answer(results=[], is_personal=True, cache_time=cache_time, switch_pm_text=(f'❌ No results for "{string}"' if string else f"❌ No results"), switch_pm_parameter="okay")
    except Exception as e:
        logger.error(f"Inline search failed: {e}")
