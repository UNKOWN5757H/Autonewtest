import asyncio
import base64
import io
import math
import random
import re
import time
from datetime import datetime
from io import BytesIO
from logging import ERROR, getLogger
from typing import Optional

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatAdminRequired,
    FloodWait,
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
from database.ia_filterdb import get_file_details, unpack_new_file_id
from database.users_chats_db import db as _db
from database.plugin_dbs import plugin_db as _plugin_db
from info import CUSTOM_FILE_CAPTION, LOG_CHANNEL, PICS
from plugins.custom_settings import get_group_welcome, get_ui
from plugins.customlines import ForceSub
from Script import script
from utils import (
    admin_filter,
    get_admin_list,
    get_poster,
    get_settings,
    get_size,
    get_uid,
    parse_text_and_markup,
    temp,
    is_subscribed
)

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚙️ MONGODB & GLOBALS SETUP
# ============================================================
_del_db = _countdown_col = search_stats_col = users_col = chats_col = media_col = None
AUTO_DELETE_TASKS = set()
_COUNTDOWN_WORKER_STARTED = False

MESSAGE_EMOJI_PLANE = "✈️"
MESSAGE_EMOJI_LINK = "🔗"

BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER, BTN_SECONDARY = 1, 3, 4, 2

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _del_db = _BOT_DB["deletion_settings"]
    _countdown_col = _BOT_DB["countdown_tasks"]
    search_stats_col = _BOT_DB["search_analytics"]
    users_col = _BOT_DB["users"]
    chats_col = _BOT_DB["chats"]
    media_col = _BOT_DB[getattr(info, "COLLECTION_NAME", "Anime_Files")]
except Exception as e:
    logger.error(f"Failed to init DBs: {e}")


# ============================================================
# 🛠️ HELPER FUNCTIONS
# ============================================================
def create_btn(text, url=None, callback_data=None, style=None):
    kwargs = {"text": text}
    if url: kwargs["url"] = url
    if callback_data: kwargs["callback_data"] = callback_data
    if style is not None: kwargs["style"] = style
    try: return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)

async def get_del_setting(key: str, default: int) -> int:
    doc = await _del_db.find_one({"_id": "timers"}) if _del_db is not None else None
    return int(doc[key]) if doc and key in doc else int(getattr(info, key, default))

async def save_del_setting(key: str, value: int):
    setattr(info, key, value)
    if _del_db is not None:
        await _del_db.update_one({"_id": "timers"}, {"$set": {key: value}}, upsert=True)

def parse_timer(var_name: str, default_time: int = 1800) -> int:
    try:
        global_switch = getattr(info, "AUTO_DELETE", True)
        if str(global_switch).strip().lower() in ["false", "off", "0"]: return 0
        val = getattr(info, "AUTO_DELETE_TIME", getattr(info, var_name, default_time))
        return 1800 if isinstance(val, bool) and val else int(val)
    except Exception:
        return 1800

async def live_text_auto_delete(client, msg1, msg2, delay):
    await asyncio.sleep(delay)
    try: await client.delete_messages(msg1.chat.id, [msg1.id, msg2.id])
    except Exception: pass


# ============================================================
# 🛡️ GROUP GATEKEEPER & WELCOME ENGINE
# ============================================================
TELEGRAM_LINK_PATTERN = re.compile(r"(https?://)?(www\.)?(t(elegram)?\.(me|dog)|telegram\.org)/(joinchat/|[a-zA-Z0-9_]+)", re.IGNORECASE)

async def is_group_admin(bot: Client, chat_id: int, user_id: int) -> bool:
    if user_id in get_admin_list(): return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
    except Exception:
        return False

@Client.on_message(filters.group & ~filters.service, group=-2)
async def group_gatekeeper_and_link_protector(bot: Client, message: Message):
    if not getattr(message, "from_user", None): return
    if await is_group_admin(bot, message.chat.id, message.from_user.id): return

    text = message.text or message.caption or ""
    if TELEGRAM_LINK_PATTERN.search(text):
        try:
            await message.delete()
            warning = await message.reply_text(f"⚠️ **Link Deleted!** {message.from_user.mention}, unauthorized invite links are not permitted in this group.")
            await asyncio.sleep(8)
            await warning.delete()
        except Exception: pass
        return

    if getattr(message, "forward_from_chat", None) and message.forward_from_chat.type == enums.ChatType.CHANNEL:
        try:
            await message.delete()
            warning = await message.reply_text(f"⚠️ **Channel Forward Blocked!** {message.from_user.mention}, forwarding messages from external channels is prohibited.")
            await asyncio.sleep(8)
            await warning.delete()
        except Exception: pass
        return

@Client.on_message(filters.new_chat_members & filters.group)
async def save_group(bot: Client, message: Message):
    new_members = [u.id for u in message.new_chat_members]
    if temp.ME in new_members:
        if not await _db.get_chat(message.chat.id):
            try: total = await bot.get_chat_members_count(message.chat.id)
            except Exception: total = 0
            added_by = message.from_user.mention if getattr(message, "from_user", None) else "Anonymous"
            if LOG_CHANNEL:
                try: await bot.send_message(LOG_CHANNEL, script.LOG_TEXT_G.format(message.chat.title, message.chat.id, total, added_by))
                except Exception: pass
            await _db.add_chat(message.chat.id, message.chat.title)

        if message.chat.id in temp.BANNED_CHATS:
            support = getattr(info, "SUPPORT_CHAT", "")
            btn = [[InlineKeyboardButton("🤖 ADMINS", url=f"https://t.me/{support}")]] if support else []
            await message.reply_text("<b>🚫 This chat is restricted!\nMy admins have disabled me here.</b>", reply_markup=InlineKeyboardMarkup(btn) if btn else None)
            await bot.leave_chat(message.chat.id)
            return

        bot_user = temp.U_NAME or (await bot.get_me()).username
        btn = [[InlineKeyboardButton("ℹ️ Help", url=f"https://t.me/{bot_user}?start=help"), InlineKeyboardButton("📖 About", url=f"https://t.me/{bot_user}?start=about")]]
        await message.reply_text(f"<b>Thanks for adding me to {message.chat.title} ❣️</b>", reply_markup=InlineKeyboardMarkup(btn))
    else:
        settings = await get_settings(message.chat.id)
        if settings.get("welcome"):
            w_settings = await get_group_welcome(message.chat.id)
            w_txt = w_settings.get("text") or "<b>👋 Hey {mention}, welcome to {title}!</b>"
            w_img = w_settings.get("img")
            try: total_members = await bot.get_chat_members_count(message.chat.id)
            except Exception: total_members = ""

            for user in message.new_chat_members:
                formatted_text = w_txt.format(mention=user.mention, title=message.chat.title, count=total_members)
                old_msg = temp.MELCOW.get(message.chat.id)
                if old_msg:
                    try: await old_msg.delete()
                    except Exception: pass
                if w_img:
                    temp.MELCOW[message.chat.id] = await message.reply_photo(photo=w_img, caption=formatted_text, parse_mode=enums.ParseMode.DEFAULT)
                else:
                    temp.MELCOW[message.chat.id] = await message.reply_text(text=formatted_text, parse_mode=enums.ParseMode.DEFAULT)


# ============================================================
# 💬 BASIC GROUP COMMANDS
# ============================================================
@Client.on_message(filters.command("leave") & admin_filter)
async def leave_chat(bot: Client, message: Message):
    if len(message.command) == 1: return await message.reply_text("⚠️ Usage: `/leave <chat_id>`")
    try: chat_id = int(message.command[1])
    except ValueError: return await message.reply_text("❌ Invalid chat ID!")
    try:
        await bot.leave_chat(chat_id)
        await message.reply_text(f"✅ Left the chat `{chat_id}` successfully.")
    except Exception as e: await message.reply_text(f"⚠️ Error: `{e}`")

@Client.on_message(filters.command("disable") & admin_filter)
async def disable_chat(bot: Client, message: Message):
    if len(message.command) == 1: return await message.reply_text("⚠️ Usage: `/disable <chat_id> [reason]`")
    parts = message.text.split(None, 2)
    try: chat_id = int(parts[1])
    except ValueError: return await message.reply_text("❌ Invalid chat ID!")
    reason = parts[2] if len(parts) > 2 else "No reason provided."

    chat_info = await _db.get_chat(chat_id)
    if not chat_info: return await message.reply_text("⚠️ Chat not found in DB.")
    if chat_info.get("is_disabled"): return await message.reply_text(f"🚷 This chat is already disabled.\nReason: `{chat_info.get('reason', 'Unknown')}`")

    await _db.disable_chat(chat_id, reason)
    if chat_id not in temp.BANNED_CHATS: temp.BANNED_CHATS.append(chat_id)
    await message.reply_text(f"✅ Chat `{chat_id}` successfully disabled.")
    try: await bot.leave_chat(chat_id)
    except Exception: pass

@Client.on_message(filters.command("enable") & admin_filter)
async def enable_chat(bot: Client, message: Message):
    if len(message.command) == 1: return await message.reply_text("⚙️ Usage: `/enable <chat_id>`")
    try: chat_id = int(message.command[1])
    except ValueError: return await message.reply_text("❌ Invalid chat ID!")

    chat_info = await _db.get_chat(chat_id)
    if not chat_info: return await message.reply_text("⚠️ Chat not found in DB.")
    if not chat_info.get("is_disabled"): return await message.reply_text("✅ This chat is already enabled.")

    await _db.re_enable_chat(chat_id)
    if chat_id in temp.BANNED_CHATS: temp.BANNED_CHATS.remove(chat_id)
    await message.reply_text(f"✅ Chat `{chat_id}` successfully re-enabled.")


# ============================================================
# 📊 ANALYTICS ENGINE
# ============================================================
@Client.on_message(filters.text & filters.incoming & ~filters.bot & ~filters.command(list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")), group=10)
async def silent_search_tracker(client: Client, message: Message):
    if search_stats_col is None: return
    query = message.text.strip().lower()
    if len(query) < 2 or len(query) > 60: return
    if re.match(r"https?://\S+|t\.me/\S+|@\w+", query): return

    clean_query = re.sub(r"(?i)\b(1080p|720p|480p|2160p|4k|mkv|mp4|avi|hdrip|web-?dl|webrip|bluray|brrip|dvdrip|x264|x265|hevc|dual audio|hindi|kannada|telugu|tamil|malayalam|english|subtitles|subs|episodes|season\s*\d+|s\d+e\d+|complete)\b", "", query)
    clean_query = re.sub(r"[\-\|:;_\[\]\(\)\{\}]", " ", clean_query)
    clean_query = re.sub(r"\s+", " ", clean_query).strip()
    if len(clean_query) < 2: return

    asyncio.create_task(search_stats_col.update_one({"date": datetime.now().strftime("%Y-%m-%d"), "query": clean_query}, {"$inc": {"count": 1}}, upsert=True))

@Client.on_message(filters.command("analize") & admin_filter)
async def live_dashboard_command(client: Client, message: Message):
    msg = await message.reply_text("⏳ **Fetching Live Analytics...**")
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        top_searches = await search_stats_col.find({"date": today}).sort("count", -1).limit(15).to_list(length=15) if search_stats_col is not None else []
        total_searches_agg = await search_stats_col.aggregate([{"$match": {"date": today}}, {"$group": {"_id": None, "total": {"$sum": "$count"}}}]).to_list(length=1) if search_stats_col is not None else []
        total_searches_today = total_searches_agg[0]["total"] if total_searches_agg else 0

        total_users = await users_col.count_documents({}) if users_col is not None else 0
        total_chats = await chats_col.count_documents({}) if chats_col is not None else 0
        total_files = await media_col.count_documents({}) if media_col is not None else 0

        dash_text = f"📊 **ENTERPRISE ANALYTICS DASHBOARD** 📊\n━━━━━━━━━━━━━━━━━━━━\n\n📈 **NETWORK STATS:**\n👥 **Total Users:** `{total_users:,}`\n📢 **Total Groups:** `{total_chats:,}`\n📂 **Total Files Indexed:** `{total_files:,}`\n\n🔥 **LIVE AUDIENCE DEMAND (Today):**\n🔍 **Total Queries Processed:** `{total_searches_today:,}`\n\n🏆 **TOP 15 TRENDING SEARCHES:**\n"
        if top_searches:
            for idx, s in enumerate(top_searches, 1): dash_text += f"  **{idx}.** `{s['query'].title()}` ➔ _{s['count']} requests_\n"
        else: dash_text += "  _No searches tracked yet today._\n"
        dash_text += "\n💡 _Tip: Use /exportstats to download the complete search history._\n━━━━━━━━━━━━━━━━━━━━\n"
        await msg.edit_text(dash_text)
    except Exception as e: await msg.edit_text(f"❌ **Dashboard Generation Failed:**\n`{str(e)}`")

@Client.on_message(filters.command("exportstats") & admin_filter)
async def export_stats_command(client: Client, message: Message):
    msg = await message.reply_text("⏳ **Generating Complete Search History Report...**")
    try:
        if search_stats_col is None: return await msg.edit_text("❌ Database not connected.")
        all_searches = await search_stats_col.find({}).sort([("date", -1), ("count", -1)]).to_list(length=None)
        if not all_searches: return await msg.edit_text("⚠️ No search data available to export.")

        report_lines = ["📊 COMPLETE SEARCH ANALYTICS EXPORT 📊", "=" * 40, ""]
        for s in all_searches: report_lines.append(f"Date: {s.get('date')} | Count: {s.get('count')} | Query: {s.get('query')}")

        file_io = BytesIO("\n".join(report_lines).encode("utf-8"))
        file_io.name = f"Search_Analytics_{datetime.now().strftime('%Y%m%d')}.txt"
        await message.reply_document(document=file_io, caption="✅ **Complete Search Statistics Exported Successfully!**")
        await msg.delete()
    except Exception as e: await msg.edit_text(f"❌ **Export Failed:**\n`{str(e)}`")

@Client.on_message(filters.command("optimize_db") & admin_filter)
async def optimize_mongodb(client: Client, message: Message):
    msg = await message.reply_text("🚀 **Initiating Database Core Optimization...**\n_Please wait, this might take a few seconds._")
    try:
        if media_col is None: return await msg.edit_text("❌ **Error:** Media collection not found.")
        try: await media_col.drop_index("media_text_index")
        except Exception: pass
        try: await media_col.drop_index("file_name_text")
        except Exception: pass

        await media_col.create_index([("file_name", "text")])
        await media_col.create_index([("file_id", 1)])
        await media_col.create_index([("file_size", -1)])
        if search_stats_col is not None:
            try: await search_stats_col.create_index([("date", 1), ("query", 1)], unique=True)
            except Exception: pass
        await msg.edit_text("✅ **MongoDB Core Optimization Complete!**\n\n**Upgrades Applied:**\n➤ `$text` search index injected.\n➤ Fast-Sort index added for `file_size`.\n➤ Analytics background indexes synced.\n\n🔥 _Your bot is now operating at Peak Enterprise Level!_")
    except Exception as e: await msg.edit_text(f"❌ **Optimization Failed:**\n`{str(e)}`")


# ============================================================
# 🗑️ AUTOMATIC DELETION & PURGER ENGINE
# ============================================================
def format_countdown_string(seconds_left: int) -> str:
    if seconds_left <= 0: return "⏳ Deleting now..."
    m, s = divmod(seconds_left, 60)
    h, m = divmod(m, 60)
    if h > 0: return f"⏳ Deleting in: {h}h {m}m"
    return f"⏳ Deleting in: {m}m {s}s"

async def countdown_live_worker(client: Client):
    while True:
        try:
            if _countdown_col is not None:
                now = time.time()
                tasks = await _countdown_col.find().to_list(length=100)
                for task in tasks:
                    chat_id, msg_id, expiry = task["chat_id"], task["message_id"], task["delete_at"]
                    remaining = int(expiry - now)
                    if remaining <= 0:
                        try: await client.delete_messages(chat_id, msg_id)
                        except Exception: pass
                        await _countdown_col.delete_one({"_id": task["_id"]})
                        continue

                    last_update = task.get("last_updated", 0)
                    if now - last_update >= 30:
                        timer_label = format_countdown_string(remaining)
                        new_keyboard = [[InlineKeyboardButton(b["text"], url=b.get("url"), callback_data=b.get("callback_data")) for b in row] for row in task.get("buttons", [])]
                        new_keyboard.append([InlineKeyboardButton(timer_label, callback_data="noop_countdown")])
                        try:
                            await client.edit_message_reply_markup(chat_id, msg_id, reply_markup=InlineKeyboardMarkup(new_keyboard))
                            await _countdown_col.update_one({"_id": task["_id"]}, {"$set": {"last_updated": now}})
                        except MessageNotModified: pass
                        except FloodWait as e: await asyncio.sleep(e.value + 1)
                        except Exception: await _countdown_col.delete_one({"_id": task["_id"]})
        except Exception as e: logger.error(f"Countdown worker error: {e}")
        await asyncio.sleep(10)

@Client.on_message(group=-99)
async def start_countdown_worker(client: Client, message: Message):
    global _COUNTDOWN_WORKER_STARTED
    if not _COUNTDOWN_WORKER_STARTED:
        _COUNTDOWN_WORKER_STARTED = True
        asyncio.create_task(countdown_live_worker(client))

async def schedule_file_with_countdown(client: Client, sent_message: Message, delay_seconds: int, base_buttons: list = None):
    if not sent_message or delay_seconds <= 0 or _countdown_col is None: return
    now = time.time()
    serialized_btns = [[{"text": btn.text, "url": getattr(btn, "url", None), "callback_data": getattr(btn, "callback_data", None)} for btn in row] for row in base_buttons] if base_buttons else []
    await _countdown_col.insert_one({"chat_id": sent_message.chat.id, "message_id": sent_message.id, "delete_at": now + delay_seconds, "last_updated": now, "buttons": serialized_btns})

@Client.on_callback_query(filters.regex("^noop_countdown$"))
async def noop_countdown_cb(client: Client, query):
    await query.answer("⚠️ This file will be automatically deleted when the timer ends. Forward or save it now!", show_alert=True)

@Client.on_message(filters.command("purgeduplicates") & admin_filter)
async def purge_duplicates_cmd(client: Client, message: Message):
    status = await message.reply_text("🔍 **Scanning database for duplicate files...**\nThis might take some time on large databases.")
    def get_resolution_score(filename: str) -> int:
        name = filename.lower()
        if any(q in name for q in ["2160p", "4k", "uhd"]): return 4
        if any(q in name for q in ["1440p", "2k"]): return 3
        if any(q in name for q in ["1080p", "fhd"]): return 2
        if any(q in name for q in ["720p", "hd"]): return 1
        return 0
    try:
        duplicate_groups = await _Media.collection.aggregate([{"$group": {"_id": "$file_name", "count": {"$sum": 1}, "docs": {"$push": {"id": "$_id", "size": "$file_size", "name": "$file_name"}}}}, {"$match": {"count": {"$gt": 1}}}]).to_list(length=None)
        if not duplicate_groups: return await status.edit_text("✅ **Database is squeaky clean!** No duplicate files found.")

        total_duplicates = 0
        deleted_ids = []
        for group in duplicate_groups:
            docs = group["docs"]
            docs.sort(key=lambda d: (get_resolution_score(d.get("name", "")), d.get("size", 0)), reverse=True)
            for d in docs[1:]:
                deleted_ids.append(d["id"])
                total_duplicates += 1

        for i in range(0, len(deleted_ids), 500):
            await _Media.collection.delete_many({"_id": {"$in": deleted_ids[i : i + 500]}})
        await status.edit_text(f"🧹 **Duplicate File Purge Completed!**\n\n📁 **Duplicate Sets Found:** `{len(duplicate_groups)}`\n🗑️ **Redundant Copies Removed:** `{total_duplicates}`\n✨ **Preserved:** Best quality copies.")
    except Exception as e: await status.edit_text(f"❌ **Error while purging duplicates:**\n`{e}`")

@Client.on_message(filters.command("delete") & admin_filter)
async def delete_single_file(bot: Client, message: Message):
    try:
        if len(message.command) == 2:
            file_id = message.command[1].strip()
            status_msg = await message.reply_text("🧹 **Deleting file from DB...**")
            result = await _Media.collection.delete_one({"_id": file_id})
            return await status_msg.edit_text(f"✅ **File `{file_id}` deleted successfully.**" if result.deleted_count else "⚠️ **File not found in database.**")

        reply = message.reply_to_message
        if not (reply and reply.media): return await message.reply_text("⚙️ **Usage:**\n`/delete <file_id>`\nOr reply to a file with `/delete`")

        status_msg = await message.reply_text("⏳ **Processing...**")
        media = getattr(reply, reply.media.value, None) if reply.media else None
        if not media: return await status_msg.edit_text("❌ **This is not a supported file format.**")

        file_id, _ = unpack_new_file_id(media.file_id)
        if (await _Media.collection.delete_one({"_id": file_id})).deleted_count:
            return await status_msg.edit_text("✅ **File successfully deleted from database.**")

        file_name = re.sub(r"(_|\-|\.|\+)", " ", str(getattr(media, "file_name", "")))
        res = await _Media.collection.delete_many({"file_name": file_name, "file_size": getattr(media, "file_size", 0), "mime_type": getattr(media, "mime_type", "")})
        await status_msg.edit_text(f"✅ **Deleted {res.deleted_count} duplicate files from database.**" if res.deleted_count else "⚠️ **File not found in database.**")
    except Exception as e: await message.reply_text(f"❌ **Error while deleting:**\n`{e}`")

@Client.on_message(filters.command("delmulti") & admin_filter)
async def delete_multiple_files(bot: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("⚙️ **Usage:** `/delmulti <keyword/name>`")
    keyword = message.text.split(None, 1)[1]
    status_msg = await message.reply_text(f"⏳ **Searching and deleting files matching:** `{keyword}`...")
    try:
        result = await _Media.collection.delete_many({"file_name": {"$regex": keyword, "$options": "i"}})
        await status_msg.edit_text(f"✅ **Successfully deleted `{result.deleted_count}` files matching:** `{keyword}`" if result.deleted_count > 0 else f"⚠️ **No files found matching:** `{keyword}`")
    except Exception as e: await status_msg.edit_text(f"❌ **Error while deleting files:**\n`{e}`")

@Client.on_message(filters.command("autodelete") & admin_filter)
async def set_autodelete_timer(bot: Client, message: Message):
    current = await get_del_setting("FILE_AUTO_DELETE", 1800)
    if len(message.command) < 2: return await message.reply_text(f"⚙️ **Usage:** `/autodelete [seconds]`\n\n⏱ **Current File Auto-Delete:** `{current}` seconds.")
    try:
        seconds = int(message.command[1])
        if seconds < 0: raise ValueError
        await save_del_setting("FILE_AUTO_DELETE", seconds)
        await message.reply_text("✅ **File Auto-Delete has been DISABLED permanently.**" if seconds == 0 else f"✅ **File Auto-Delete permanently set to:** `{seconds}` seconds.")
    except ValueError: await message.reply_text("❌ **Invalid duration!** Please provide a valid positive integer.")

@Client.on_message(filters.command("buttondel") & admin_filter)
async def set_buttondel_timer(bot: Client, message: Message):
    current = await get_del_setting("BUTTON_AUTO_DELETE", 1800)
    if len(message.command) < 2: return await message.reply_text(f"⚙️ **Usage:** `/buttondel [seconds]`\n\n⏱ **Current Button Auto-Delete:** `{current}` seconds.")
    try:
        seconds = int(message.command[1])
        if seconds < 0: raise ValueError
        await save_del_setting("BUTTON_AUTO_DELETE", seconds)
        await message.reply_text("✅ **Group Button Auto-Delete has been DISABLED permanently.**" if seconds == 0 else f"✅ **Group Button Auto-Delete permanently set to:** `{seconds}` seconds.")
    except ValueError: await message.reply_text("❌ **Invalid duration!** Please provide a valid positive integer.")

# ============================================================
# 💬 ID / INFO / IMDB COMMANDS
# ============================================================
@Client.on_message(filters.command("id"))
async def show_id(client: Client, message: Message):
    chat_type = message.chat.type
    if chat_type == enums.ChatType.PRIVATE:
        user = message.from_user
        await message.reply_text(f"<b>👤 First Name:</b> {getattr(user, 'first_name', 'Unknown')}\n<b>🆔 Telegram ID:</b> <code>{user.id}</code>")
    elif chat_type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        text = f"<b>🆔 Chat ID:</b> <code>{message.chat.id}</code>\n"
        if message.reply_to_message:
            text += f"<b>👤 Replied User ID:</b> <code>{getattr(message.reply_to_message, 'from_user', None).id if getattr(message.reply_to_message, 'from_user', None) else 'Anonymous'}</code>\n"
        await message.reply_text(text)

@Client.on_message(filters.command("info"))
async def who_is(client: Client, message: Message):
    status = await message.reply_text("🔍 Fetching user info...")
    try:
        user = None
        if getattr(message, "reply_to_message", None) and getattr(message.reply_to_message, "from_user", None):
            user = message.reply_to_message.from_user
        elif len(message.command) > 1:
            try: user = await client.get_users(message.text.split(None, 1)[1])
            except Exception: pass
        if not user: user = getattr(message, "from_user", None)
        if not user: return await status.edit_text("⚠️ No valid user found.")
        text = f"<b>👤 First Name:</b> {getattr(user, 'first_name', 'Unknown')}\n<b>🧾 Last Name:</b> {getattr(user, 'last_name', 'None')}\n<b>🆔 Telegram ID:</b> <code>{user.id}</code>\n<b>💬 Username:</b> @{getattr(user, 'username', 'None')}\n<b>🔗 User Link:</b> <a href='tg://user?id={user.id}'>Click Here</a>\n"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Close", callback_data="close_data")]])
        if user.photo:
            try:
                await message.reply_photo(photo=user.photo.big_file_id, caption=text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
                await status.delete()
                return
            except Exception: pass
        await message.reply_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        await status.delete()
    except Exception: await status.edit_text("❌ An error occurred while fetching user data.")

class SafeDict(dict):
    def __missing__(self, key): return "{" + key + "}"

@Client.on_message(filters.command(["imdb", "search"]))
async def imdb_search(client: Client, message: Message):
    if len(message.command) < 2: return await message.reply_text("🎬 **Usage:** `/imdb Movie or Series Name`")
    query = message.text.split(None, 1)[1]
    wait_msg = await message.reply_text("🔎 Searching IMDb...")
    try:
        movies = await get_poster(query, bulk=True)
        if not movies: return await wait_msg.edit_text("❌ No results found on IMDb.")
        buttons = []
        for movie in movies[:10]:
            m_id = getattr(movie, "movieID", None) or (movie.get("movieID") if isinstance(movie, dict) else None) or movie.get("id", "")
            title = getattr(movie, "title", None) or (movie.get("title") if isinstance(movie, dict) else "Unknown")
            year = getattr(movie, "year", None) or (movie.get("year") if isinstance(movie, dict) else "")
            if m_id: buttons.append([InlineKeyboardButton(f"{title} ({year})" if year else title, callback_data=f"imdb#{m_id}")])
        if not buttons: return await wait_msg.edit_text("❌ Could not parse IMDb results.")
        await wait_msg.edit_text("🎥 **Here's what I found:**", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e: await wait_msg.edit_text(f"⚠️ IMDb search error: `{e}`")

@Client.on_callback_query(filters.regex(r"^imdb#"))
async def imdb_callback(client: Client, query: CallbackQuery):
    try:
        _, movie_id = query.data.split("#")
        await query.answer("Fetching Details...", show_alert=False)
        imdb = await get_poster(query=movie_id, id=True)
        if not imdb: return await query.message.edit_text("❌ No IMDb results found.")
        url = imdb.get("url", f"https://www.imdb.com/title/tt{movie_id}/")
        buttons = [[InlineKeyboardButton(f"{imdb.get('title', 'IMDb Link')}", url=url)]]
        format_args = SafeDict(
            query=imdb.get("title", "N/A"), title=imdb.get("title", "N/A"), votes=imdb.get("votes", "N/A"), aka=imdb.get("aka", "N/A"),
            seasons=imdb.get("seasons", "N/A"), box_office=imdb.get("box_office", "N/A"), localized_title=imdb.get("localized_title", "N/A"),
            kind=imdb.get("kind", "N/A"), imdb_id=imdb.get("imdb_id", movie_id), cast=imdb.get("cast", "N/A"), runtime=imdb.get("runtime", "N/A"),
            countries=imdb.get("countries", "N/A"), certificates=imdb.get("certificates", "N/A"), languages=imdb.get("languages", "N/A"),
            director=imdb.get("director", "N/A"), writer=imdb.get("writer", "N/A"), producer=imdb.get("producer", "N/A"), composer=imdb.get("composer", "N/A"),
            cinematographer=imdb.get("cinematographer", "N/A"), music_team=imdb.get("music_team", "N/A"), distributors=imdb.get("distributors", "N/A"),
            release_date=imdb.get("release_date", "N/A"), year=imdb.get("year", "N/A"), genres=imdb.get("genres", "N/A"), poster=imdb.get("poster", "N/A"),
            plot=imdb.get("plot", "N/A"), rating=imdb.get("rating", "N/A"), url=url,
        )
        caption = getattr(info, "IMDB_TEMPLATE", """🎬 **Title:** <a href="{url}">{title}</a> ({year})\n⭐️ **Rating:** {rating} / 10\n🎭 **Genres:** {genres}\n🗣 **Languages:** {languages}\n⏰ **Runtime:** {runtime}\n📜 **Plot:** {plot}""").format_map(format_args)
        if imdb.get("poster"):
            try: await query.message.reply_photo(photo=imdb["poster"], caption=caption, reply_markup=InlineKeyboardMarkup(buttons))
            except Exception: await query.message.reply_photo(photo=imdb["poster"].replace(".jpg", "._V1_UX360.jpg"), caption=caption, reply_markup=InlineKeyboardMarkup(buttons))
            await query.message.delete()
        else:
            await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), disable_web_page_preview=False)
    except Exception: pass

# ============================================================
# 🌐 UI ROUTING AND START COMMAND
# ============================================================
def build_dynamic_keyboard(custom_buttons, static_buttons):
    keyboard, row = [], []
    for btn in custom_buttons:
        ibtn = create_btn(btn["text"], url=btn["url"], style={"blue": BTN_PRIMARY, "green": BTN_SUCCESS, "red": BTN_DANGER, "normal": None}.get(btn.get("color", "normal")))
        if btn.get("layout") == "sidebyside":
            row.append(ibtn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        else:
            if row: keyboard.append(row)
            keyboard.append([ibtn])
            row = []
    if row: keyboard.append(row)
    keyboard.extend(static_buttons)
    return InlineKeyboardMarkup(keyboard)

async def get_start_keyboard(user_id):
    ui = await get_ui()
    static_buttons = [
        [create_btn("✈️ Gʀᴏᴜᴘ 1", url="https://t.me/Sandalwood_Kannada_Group", style=BTN_PRIMARY), create_btn("✈️ Gʀᴏᴜᴘ 2", url="http://t.me/Kannada_Filmy_Group", style=BTN_PRIMARY), create_btn("✈️ Gʀᴏᴜᴘ 3", url="https://t.me/+GLsPkRgLGGszMzY1", style=BTN_PRIMARY)]
    ]
    if user_id in get_admin_list(): static_buttons.append([create_btn("ℹ️ 𝙷𝚎𝚕𝚙", callback_data="help"), create_btn("😊 𝙰𝚋𝚘𝚞𝚝", callback_data="about")])
    static_buttons.append([create_btn("🔗 Nᴇᴡ Rᴇʟᴇᴀꜱᴇꜱ & Oᴛᴛ Uᴘᴅᴀᴛᴇꜱ", url="https://t.me/sandalwood_kannada_moviesz", style=BTN_SUCCESS)])
    return build_dynamic_keyboard(ui.get("start_buttons", []), static_buttons)

async def get_help_keyboard():
    return InlineKeyboardMarkup([
        [create_btn("🖥️ UI Start", callback_data="helps_uistart"), create_btn("🖥️ UI Help", callback_data="helps_uihelp"), create_btn("🖥️ UI About", callback_data="helps_uiabout")],
        [create_btn("👋 Welcome", callback_data="helps_welcome"), create_btn("🖼️ Images", callback_data="helps_images")],
        [create_btn("🔍 Spell Check", callback_data="helps_spell"), create_btn("📝 Filters", callback_data="helps_filters")],
        [create_btn("📱 Force Sub", callback_data="helps_forcesub"), create_btn("👥 Force Add", callback_data="helps_forceadd")],
        [create_btn("🚫 Bans", callback_data="helps_bans"), create_btn("🗑️ Delete", callback_data="helps_delete")],
        [create_btn("📢 Promotions", callback_data="helps_promotions"), create_btn("📚 Index", callback_data="helps_index")],
        [create_btn("⚙ Settings", callback_data="helps_settings"), create_btn("🌐 Connections", callback_data="helps_connections")],
        [create_btn("📊 Utilities", callback_data="helps_utilities"), create_btn("💬 Custom Messages", callback_data="helps_custommessages")],
        [create_btn("📝 Post Handle", callback_data="helps_posthand"), create_btn("📝 Custom Captions", callback_data="helps_customcaption")],
        [create_btn("🧬 Auto post", callback_data="helps_autopost"), create_btn("💬 Pm Auto Reply", callback_data="helps_pmautoreply")],
        [create_btn("💾 Backup", callback_data="helps_backup", style=BTN_SUCCESS)],
        [create_btn("🔙 Back", callback_data="start", style=BTN_PRIMARY), create_btn("🔐 Cʟᴏsᴇ", callback_data="close_data", style=BTN_DANGER)],
    ])

async def get_channels_page(client: Client, page: int = 1):
    raw_chats = await _db.get_all_chats()
    all_chats = await raw_chats.to_list(length=None) if hasattr(raw_chats, "to_list") else list(raw_chats)
    total_chats, page_size = len(all_chats), 15
    total_pages = max(1, math.ceil(total_chats / page_size))
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * page_size
    current_chats = all_chats[start_idx : start_idx + page_size]
    text = f"📑 <b>All Connected Channels & Groups</b> (Page {page}/{total_pages})\n\n"

    for i, chat in enumerate(current_chats, start=start_idx + 1):
        chat_id = chat.get("id") or chat.get("chat_id")
        title = chat.get("title") or chat.get("name") or "Unknown"
        link = f"https://t.me/{chat.get('username')}" if chat.get("username") else "Private / No Link"
        if not chat.get("username"):
            try:
                chat_obj = await client.get_chat(chat_id)
                link = f"https://t.me/{chat_obj.username}" if chat_obj.username else (chat_obj.invite_link or await client.export_chat_invite_link(chat_id))
            except Exception: link = f"https://t.me/c/{str(chat_id)[4:] if str(chat_id).startswith('-100') else str(chat_id)}/1"
        text += f"<b>{i}. {title}</b>\n🔗 Link: {link}\n🆔 ID: <code>{chat_id}</code>\n\n"

    buttons, nav_row = [], []
    if page > 1: nav_row.append(create_btn("⬅️ Previous", callback_data=f"channels_page#{page - 1}", style=BTN_PRIMARY))
    if page < total_pages: nav_row.append(create_btn("Next ➡️", callback_data=f"channels_page#{page + 1}", style=BTN_PRIMARY))
    if nav_row: buttons.append(nav_row)
    buttons.append([create_btn("🔐 Close", callback_data="close_data", style=BTN_DANGER)])
    return text, InlineKeyboardMarkup(buttons)

async def transition_ui_message(client, msg_obj, text, keyboard, img=None):
    try:
        if img:
            if getattr(msg_obj, "photo", None) or getattr(msg_obj, "video", None) or getattr(msg_obj, "document", None):
                await msg_obj.edit_caption(caption=text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
            else:
                await msg_obj.delete()
                await client.send_photo(chat_id=msg_obj.chat.id, photo=img, caption=text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
        else:
            if getattr(msg_obj, "photo", None) or getattr(msg_obj, "video", None) or getattr(msg_obj, "document", None):
                await msg_obj.delete()
                await client.send_message(chat_id=msg_obj.chat.id, text=text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
            else:
                await msg_obj.edit_text(text=text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML)
    except MessageNotModified: pass
    except Exception as e: logger.error(f"UI Transition Error: {e}")


@Client.on_message(filters.command(["start", "help"]) & filters.incoming)
async def start(client: Client, message: Message):
    uid = get_uid(message)
    if getattr(info, "REPAIR_MODE", False) and uid not in get_admin_list():
        return await message.reply_text("🛠️ <b>Bot is currently under maintenance!</b>", parse_mode=enums.ParseMode.HTML)
    if getattr(message, "from_user", None) and await _plugin_db.is_banned(uid): return

    bot_uname, b_name, ui = temp.U_NAME or "my_bot", temp.B_NAME or "MovieBot", await get_ui()
    is_help_command = (message.command[0] == "help") or (len(message.command) == 2 and message.command[1] == "help")

    if is_help_command:
        text = (ui.get("help_text") or script.HELP_TXT).format(mention=getattr(message.from_user, "mention", "User"), uname=bot_uname, bname=b_name)
        final_text, final_keyboard = parse_text_and_markup(text, await get_help_keyboard())
        img = ui.get("help_img")
        if img: return await message.reply_photo(photo=img, caption=final_text, reply_markup=final_keyboard, parse_mode=enums.ParseMode.HTML)
        else: return await message.reply_text(text=final_text, reply_markup=final_keyboard, parse_mode=enums.ParseMode.HTML)

    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        raw_text = (ui.get("start_text") or script.START_TXT).format(mention=(getattr(message.from_user, "mention", None) or message.chat.title), uname=bot_uname, bname=b_name, plane_emoji=MESSAGE_EMOJI_PLANE, link_emoji=MESSAGE_EMOJI_LINK)
        final_text, final_keyboard = parse_text_and_markup(raw_text, await get_start_keyboard(uid if getattr(message, "from_user", None) else 0))
        await message.reply(final_text, reply_markup=final_keyboard, parse_mode=enums.ParseMode.HTML)
        await asyncio.sleep(2)
        if not await _db.get_chat(message.chat.id):
            try: total = await client.get_chat_members_count(message.chat.id)
            except Exception: total = 0
            try: await client.send_message(LOG_CHANNEL, script.LOG_TEXT_G.format(message.chat.title, message.chat.id, total, "Unknown"))
            except Exception: pass
            await _db.add_chat(message.chat.id, message.chat.title)
        return

    if not await _db.is_user_exist(uid):
        await _db.add_user(uid, getattr(message.from_user, "first_name", "Unknown"))
        try: await client.send_message(LOG_CHANNEL, script.LOG_TEXT_P.format(uid, getattr(message.from_user, "mention", "Unknown")))
        except Exception: pass

    if len(message.command) != 2 or message.command[1] in ["error", "okay", "hehe"]:
        raw_text = (ui.get("start_text") or script.START_TXT).format(mention=getattr(message.from_user, "mention", "Unknown"), uname=bot_uname, bname=b_name, plane_emoji=MESSAGE_EMOJI_PLANE, link_emoji=MESSAGE_EMOJI_LINK)
        final_text, final_keyboard = parse_text_and_markup(raw_text, await get_start_keyboard(uid))
        db_img = ui.get("start_img")
        photo_to_send = db_img if db_img else (random.choice(PICS) if PICS else None)
        try:
            if photo_to_send: await message.reply_photo(photo=photo_to_send, caption=final_text, reply_markup=final_keyboard, parse_mode=enums.ParseMode.HTML)
            else: await message.reply_text(text=final_text, reply_markup=final_keyboard, parse_mode=enums.ParseMode.HTML)
        except (UserIsBlocked, PeerIdInvalid): pass
        return

    cmd_data = message.command[1]
    if cmd_data == "subscribe":
        return await ForceSub(client, message)

    if cmd_data.startswith("search_") or cmd_data.startswith("hdra_"):
        query = ""
        try:
            if cmd_data.startswith("search_"): query = cmd_data.replace("search_", "").replace("_", " ")
            else:
                token = cmd_data.replace("hdra_", "")
                padding = 4 - (len(token) % 4)
                if padding != 4: token += "=" * padding
                query = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
            message.text = query
            from plugins.pm_filter import auto_filter
            return await auto_filter(client, message)
        except Exception:
            return await message.reply_text(f"🔍 **Search Query:** `{query}`\n\nTap the text above to copy it, then send it to me to search!")

    kk, file_id = cmd_data.split("_", 1) if "_" in cmd_data else (False, False)
    pre = ("checksubp" if kk == "filep" else "checksub") if kk else False

    status = await ForceSub(client, message, file_id=file_id or cmd_data, mode=pre)
    if not status: return

    files_ = await get_file_details(file_id or cmd_data)
    if not files_: return await message.reply_text("⚠️ No such file exists.", parse_mode=enums.ParseMode.HTML)

    files = files_[0]
    title = str(files.get("file_name", "Unknown") if isinstance(files, dict) else getattr(files, "file_name", "Unknown"))
    size = get_size(int(files.get("file_size", 0) if isinstance(files, dict) else getattr(files, "file_size", 0)))
    f_caption = str(files.get("caption", "") if isinstance(files, dict) else getattr(files, "caption", ""))
    db_file_id = files.get("full_file_id", files.get("file_id", file_id or cmd_data)) if isinstance(files, dict) else getattr(files, "full_file_id", getattr(files, "file_id", file_id or cmd_data))

    if CUSTOM_FILE_CAPTION:
        try: f_caption = CUSTOM_FILE_CAPTION.format(file_name="" if title == "Unknown" else title, file_size="" if size == "0B" else size, file_caption="" if not f_caption else f_caption)
        except Exception: pass

    if not f_caption: f_caption = f"<b>{title}</b>"
    if getattr(info, "CAPTION_PLUS", None): f_caption += f"\n\n{info.CAPTION_PLUS}"

    final_caption, final_keyboard = parse_text_and_markup(f_caption, InlineKeyboardMarkup([[create_btn(text="🎥 ಕನ್ನಡ ಹೊಸ ಮೂವೀಗಳು 🎥", url="https://t.me/Sandalwood_kannada_moviesz", style=BTN_SUCCESS)]]))

    msg = None
    try:
        msg = await client.send_cached_media(chat_id=uid, file_id=db_file_id, caption=final_caption, reply_markup=final_keyboard, protect_content=True if kk in ["filep", "checksubp"] else False, parse_mode=enums.ParseMode.HTML)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        msg = await client.send_cached_media(chat_id=uid, file_id=db_file_id, caption=final_caption, protect_content=True if kk in ["filep", "checksubp"] else False, parse_mode=enums.ParseMode.HTML)
    except Exception:
        try: msg = await client.send_document(chat_id=uid, document=db_file_id, caption=final_caption, reply_markup=final_keyboard, protect_content=(True if ident in ["filep", "checksubp"] else False), parse_mode=enums.ParseMode.HTML)
        except Exception as err: return await message.reply_text(f"⚠️ <b>Error:</b>\n<code>{err}</code>", parse_mode=enums.ParseMode.HTML)

    if msg:
        try:
            delete_timer = parse_timer("FILE_AUTO_DELETE", 1800)
            if delete_timer > 0:
                min_, sec_ = divmod(delete_timer, 60)
                time_str = f"{min_} Minutes {sec_} Seconds" if sec_ > 0 else f"{min_} Minutes"
                k = await client.send_message(chat_id=uid, text=f"<b>📢 Please Note\n\n✅ The above file will be autodeleted in {time_str} to avoid copyright issues.\n\n✅ Please forward this file to your saved messages and start downloading from there.\n\nTᴇᴀᴍ: @KR_Picture</b>", reply_to_message_id=msg.id, parse_mode=enums.ParseMode.HTML)
                task = asyncio.create_task(live_text_auto_delete(client, msg, k, delete_timer))
                AUTO_DELETE_TASKS.add(task)
                task.add_done_callback(AUTO_DELETE_TASKS.discard)
        except Exception: pass

@Client.on_callback_query(filters.regex(r"^(close_data|channels_page#.*|start|help|about|helps_.*|stats|file.*|checksub.*)$"))
async def cb_handler(client: Client, query: CallbackQuery):
    if getattr(info, "REPAIR_MODE", False):
        if get_uid(query.message) not in get_admin_list() and query.data != "close_data":
            return await query.answer("🛠️ Bot is under maintenance!", show_alert=True)
    try:
        if query.data == "close_data":
            await query.message.delete()
        elif query.data.startswith("channels_page#"):
            page_num = int(query.data.split("#")[1])
            try:
                text, reply_markup = await get_channels_page(client, page=page_num)
                await query.message.edit_text(text=text, reply_markup=reply_markup, disable_web_page_preview=True, parse_mode=enums.ParseMode.HTML)
            except Exception: pass
            await query.answer()
        elif query.data == "start":
            await query.answer()
            ui = await get_ui()
            raw_text = (ui.get("start_text") or script.START_TXT).format(mention=query.from_user.mention, uname=temp.U_NAME or "my_bot", bname=temp.B_NAME or "MovieBot", plane_emoji=MESSAGE_EMOJI_PLANE, link_emoji=MESSAGE_EMOJI_LINK)
            final_text, final_keyboard = parse_text_and_markup(raw_text, await get_start_keyboard(query.from_user.id))
            await transition_ui_message(client, query.message, final_text, final_keyboard, ui.get("start_img"))
        elif query.data == "help":
            await query.answer()
            ui = await get_ui()
            raw_text = (ui.get("help_text") or script.HELP_TXT).format(mention=query.from_user.mention, uname=temp.U_NAME or "my_bot", bname=temp.B_NAME or "MovieBot")
            final_text, final_keyboard = parse_text_and_markup(raw_text, await get_help_keyboard())
            await transition_ui_message(client, query.message, final_text, final_keyboard, ui.get("help_img"))
        elif query.data == "about":
            await query.answer()
            ui = await get_ui()
            raw_text = (ui.get("about_text") or script.ABOUT_TXT).format(mention=query.from_user.mention, bname=temp.B_NAME or "MovieBot")
            final_text, final_keyboard = parse_text_and_markup(raw_text, InlineKeyboardMarkup([[create_btn("Sᴛᴀᴛᴜs ​", callback_data="stats")], [create_btn("🏘 Hᴏᴍᴇ", callback_data="start", style=BTN_SUCCESS), create_btn("🔐 Cʟᴏsᴇ", callback_data="close_data", style=BTN_DANGER)]]))
            await transition_ui_message(client, query.message, final_text, final_keyboard, ui.get("about_img"))
        elif query.data.startswith("helps_"):
            await query.answer()
            buttons = [[create_btn("🔙 Back", callback_data="help")]]
            help_dict = {
                "helps_uistart": ("UISTART_TXT", "🎨 UI Start Menu"), "helps_uihelp": ("UIHELP_TXT", "🎨 UI Help Menu"), "helps_uiabout": ("UIABOUT_TXT", "🎨 UI About Menu"),
                "helps_welcome": ("WELCOME_TXT", "👋 Welcome Help"), "helps_images": ("IMAGES_TXT", "🖼️ Images Help"), "helps_spell": ("SPELLCHECK_TXT", "🔍 Spell Check Help"),
                "helps_filters": ("FILTERS_TXT", "📝 Filters Help"), "helps_forcesub": ("FORCESUB_TXT", "📱 Force Sub Help"), "helps_forceadd": ("FORCEADD_TXT", "👥 Force Add Help"),
                "helps_bans": ("BANS_TXT", "🚫 Bans Help"), "helps_delete": ("DELETE_TXT", "🗑 Delete Help"), "helps_promotions": ("PROMOTIONS_TXT", "📢 Promotions Help"),
                "helps_index": ("INDEX_TXT", "📚 Index Help"), "helps_settings": ("SETTINGS_TXT", "⚙️ Settings Help"), "helps_connections": ("CONNECTIONS_TXT", "🌐 Connections Help"),
                "helps_utilities": ("UTILITIES_TXT", "📊 Utilities Help"), "helps_custommessages": ("CUSTOMMESSAGES_TXT", "💬 Custom Messages"),
                "helps_posthand": ("POSTHAND_TXT", "📝 Post Handle Help"), "helps_customcaption": ("CUSTOMCAPTION_TXT", "📝 Custom Captions Help"),
                "helps_backup": ("BACKUP_TXT", "💾 Backup Help"), "helps_autopost": ("AUTOPOST_TXT", "🧬 Auto Post Help"), "helps_pmautoreply": ("PMAUTOREPLY_TXT", "💬 PM Auto Reply Help"),
            }
            target_var, default_text = help_dict.get(query.data, ("HELP_TXT", "Help unavailable."))
            text = getattr(script, target_var, default_text)
            try:
                if getattr(query.message, "photo", None) or getattr(query.message, "video", None) or getattr(query.message, "animation", None) or getattr(query.message, "document", None):
                    await query.message.edit_caption(caption=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
                else: await query.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
            except MessageNotModified: pass
            except Exception:
                clean_text = re.sub(r"</?(b|i|u|s|code|pre|a|blockquote)[^>]*>", "", text)
                try:
                    if getattr(query.message, "photo", None) or getattr(query.message, "video", None) or getattr(query.message, "animation", None) or getattr(query.message, "document", None):
                        await query.message.edit_caption(caption=clean_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.DISABLED)
                    else: await query.message.edit_text(text=clean_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.DISABLED)
                except Exception: pass
        elif query.data.startswith("file") or query.data.startswith("checksub"):
            if query.data.startswith("checksub"):
                if (getattr(info, "AUTH_CHANNEL", None) or getattr(info, "REQ_CHANNEL", None)) and not await is_subscribed(client, query):
                    return await query.answer("Search Your Self In The Group. Team: @KR_PICTURE", show_alert=True)
            try: ident, file_id = query.data.split("#")
            except ValueError: return await query.answer("Invalid button data!", show_alert=True)

            files_ = await get_file_details(file_id)
            if not files_: return await query.answer("No such file exist.", show_alert=True)

            files = files_[0]
            title = str(files.get("file_name", "Unknown") if isinstance(files, dict) else getattr(files, "file_name", "Unknown"))
            size = get_size(int(files.get("file_size", 0) if isinstance(files, dict) else getattr(files, "file_size", 0)))
            f_caption = str(files.get("caption", "") if isinstance(files, dict) else getattr(files, "caption", ""))

            if getattr(info, "CUSTOM_FILE_CAPTION", None):
                try: f_caption = info.CUSTOM_FILE_CAPTION.format(file_name="" if title == "Unknown" else title, file_size="" if size == "0B" else size, file_caption="" if not f_caption else f_caption)
                except Exception: pass
            if not f_caption: f_caption = f"<b>{title}</b>"
            final_caption, final_keyboard = parse_text_and_markup(f_caption, None)

            await query.answer()
            m = None
            try: m = await client.send_cached_media(chat_id=query.from_user.id, file_id=file_id, caption=final_caption, reply_markup=final_keyboard, protect_content=True if ident in ["filep", "checksubp"] else False, parse_mode=enums.ParseMode.HTML)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                m = await client.send_cached_media(chat_id=query.from_user.id, file_id=file_id, caption=final_caption, reply_markup=final_keyboard, protect_content=True if ident in ["filep", "checksubp"] else False, parse_mode=enums.ParseMode.HTML)
            except Exception:
                try: m = await client.send_document(chat_id=query.from_user.id, document=file_id, caption=final_caption, reply_markup=final_keyboard, protect_content=(True if ident in ["filep", "checksubp"] else False), parse_mode=enums.ParseMode.HTML)
                except Exception: pass

            if m:
                try:
                    delete_timer = parse_timer("FILE_AUTO_DELETE", 1800)
                    if delete_timer > 0:
                        min_, sec_ = divmod(delete_timer, 60)
                        time_str = f"{min_} Minutes {sec_} Seconds" if sec_ > 0 else f"{min_} Minutes"
                        k = await client.send_message(chat_id=query.from_user.id, text=f"<b>📢 Please Note\n\n✅ The above file will be autodeleted in {time_str} to avoid copyright issues.\n\n✅ Please forward this file to your saved messages and start downloading from there.\n\nTᴇᴀᴍ: @KR_Picture</b>", reply_to_message_id=m.id, parse_mode=enums.ParseMode.HTML)
                        task = asyncio.create_task(live_text_auto_delete(client, m, k, delete_timer))
                        AUTO_DELETE_TASKS.add(task)
                        task.add_done_callback(AUTO_DELETE_TASKS.discard)
                except Exception: pass
    except QueryIdInvalid: pass
