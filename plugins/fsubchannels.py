import asyncio
import io
import math
import time
from datetime import datetime, timezone
from logging import ERROR, getLogger

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.enums import ButtonStyle
from pyrogram.errors import (
    ChatAdminRequired,
    FloodWait,
    MessageNotModified,
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
from database.connections_mdb import (
    add_connection,
    all_connections,
    delete_connection,
    if_active,
)
from database.ia_filterdb import get_file_details
from database.join_reqs import join_reqs as _join_reqs_db
from database.plugin_dbs import plugin_db as _plugin_db
from database.users_chats_db import db as _db
from plugins.custom_settings import get_bot_settings

# 👑 IMPORTING THE GOD-TIER UNIVERSAL ADMIN ENGINE & UTILS
from utils import admin_filter, get_admin_list, get_size, get_uid

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚙️ MONGODB ENGINE SETUP
# ============================================================
_fsub_db = None
_vip_db = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _fsub_db = _BOT_DB["fsub_config"]
    _vip_db = _BOT_DB["fsub_vips"]
except Exception as e:
    logger.error(f"Failed to init FSub DB: {e}")

_WAITING_REQUESTS = {}


# 🚨 BUG FIX: Added filters.text to prevent media blocking and /start hijacking
@Client.on_message(filters.text & admin_filter, group=-12)
async def custom_listener(client: Client, message: Message):
    key = (message.chat.id, get_uid(message))
    if key in _WAITING_REQUESTS:
        future = _WAITING_REQUESTS.pop(key)
        if not future.done():
            future.set_result(message)
        message.stop_propagation()


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
# 🔌 CHAT CONNECTION ENGINE (/connect, /disconnect)
# ============================================================
@Client.on_message((filters.private | filters.group) & filters.command("connect"))
async def addconnection(client, message):
    userid = get_uid(message)
    if not userid:
        return await message.reply("You are anonymous admin. Use /connect in PM.")

    if message.chat.type == enums.ChatType.PRIVATE:
        try:
            cmd, group_id = message.text.split(" ", 1)
        except Exception:
            return await message.reply_text(
                "<b>Enter correct format!</b>\n\n<code>/connect groupid</code>",
                quote=True,
            )
    else:
        group_id = message.chat.id

    try:
        st = await client.get_chat_member(group_id, userid)
        if (
            st.status
            not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
            and userid not in get_admin_list()
        ):
            return await message.reply_text(
                "You should be an admin in the Given group!", quote=True
            )
    except Exception:
        return await message.reply_text(
            "Invalid Group ID!\n\nMake sure I'm present in your group!", quote=True
        )

    try:
        st = await client.get_chat_member(group_id, "me")
        if st.status == enums.ChatMemberStatus.ADMINISTRATOR:
            ttl = await client.get_chat(group_id)
            if await add_connection(str(group_id), str(userid)):
                await message.reply_text(
                    f"Successfully connected to **{ttl.title}**\nNow manage your group from my PM!",
                    quote=True,
                )
            else:
                await message.reply_text(
                    "You're already connected to this chat!", quote=True
                )
        else:
            await message.reply_text("Add me as an admin in group", quote=True)
    except Exception:
        await message.reply_text("Some error occurred! Try again later.", quote=True)


@Client.on_message((filters.private | filters.group) & filters.command("disconnect"))
async def deleteconnection(client, message):
    userid = get_uid(message)
    if not userid:
        return

    if message.chat.type == enums.ChatType.PRIVATE:
        return await message.reply_text(
            "Run /connections to view or disconnect from groups!", quote=True
        )
    group_id = message.chat.id

    st = await client.get_chat_member(group_id, userid)
    if (
        st.status
        not in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER]
        and userid not in get_admin_list()
    ):
        return

    if await delete_connection(str(userid), str(group_id)):
        await message.reply_text("Successfully disconnected from this chat", quote=True)
    else:
        await message.reply_text("This chat isn't connected to me!", quote=True)


@Client.on_message(filters.private & filters.command(["connections"]))
async def connections(client, message):
    userid = get_uid(message)
    groupids = await all_connections(str(userid))

    if not groupids:
        return await message.reply_text(
            "There are no active connections!! Connect to some groups first.",
            quote=True,
        )

    buttons = []
    for groupid in groupids:
        try:
            ttl = await client.get_chat(int(groupid))
            act = " - ACTIVE" if await if_active(str(userid), str(groupid)) else ""
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"{ttl.title}{act}",
                        callback_data=f"groupcb:{groupid}:{act}",
                    )
                ]
            )
        except Exception:
            pass

    if buttons:
        await message.reply_text(
            "Your connected group details:\n\n",
            reply_markup=InlineKeyboardMarkup(buttons),
            quote=True,
        )
    else:
        await message.reply_text("There are no active connections!", quote=True)


# ============================================================
# 📱 INTERACTIVE CHANNEL MANAGER DASHBOARD (/channels)
# ============================================================
async def build_channels_dashboard(client: Client, page: int = 1):
    chats_data = await _db.get_all_chats()
    chats = (
        await chats_data.to_list(length=None)
        if hasattr(chats_data, "to_list")
        else list(chats_data)
    )
    channels = [chat for chat in chats if str(chat.get("id", "")).startswith("-100")]

    total_channels = len(channels)
    if total_channels == 0:
        return "⚠️ **No channels found in database.**", None

    page_size = 5
    total_pages = max(1, math.ceil(total_channels / page_size))
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * page_size
    current_channels = channels[start_idx : start_idx + page_size]

    text = f"📊 **Interactive Channel Dashboard** (Page {page}/{total_pages})\n📢 **Connected Channels:** `{total_channels}`\n\n"
    buttons = []

    for ch in current_channels:
        cid = int(ch["id"])
        title = ch.get("title", "Unknown")[:18]
        status_tag, member_count_str = "❌ Left", "?"
        try:
            chat_obj = await client.get_chat(cid)
            member_count_str = str(chat_obj.members_count or "?")
            member_self = await client.get_chat_member(cid, "me")
            if member_self.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]:
                status_tag = "👑 Admin"
            else:
                status_tag = "👤 Member"
        except Exception:
            pass

        text += f"• **{title}** (`{cid}`)\n  └ {status_tag} | 👥 `{member_count_str}` members\n\n"
        buttons.append(
            [
                InlineKeyboardButton(f"📢 {title}", callback_data=f"chan_info:{cid}"),
                InlineKeyboardButton(
                    "🚪 Leave", callback_data=f"chan_leave:{cid}:{page}"
                ),
            ]
        )

    nav_row = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"chan_page:{page-1}")
        )
    if page < total_pages:
        nav_row.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"chan_page:{page+1}")
        )
    if nav_row:
        buttons.append(nav_row)

    buttons.append(
        [
            InlineKeyboardButton(
                "🔄 Refresh Dashboard", callback_data=f"chan_page:{page}"
            )
        ]
    )
    return text, InlineKeyboardMarkup(buttons)


@Client.on_message(filters.command("channels") & admin_filter)
async def channels_dashboard_cmd(client: Client, message: Message):
    text, markup = await build_channels_dashboard(client, page=1)
    await message.reply_text(text, reply_markup=markup, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^chan_page:(\d+)") & admin_filter)
async def channels_page_cb(client: Client, query: CallbackQuery):
    page = int(query.matches[0].group(1))
    text, markup = await build_channels_dashboard(client, page=page)
    try:
        await query.message.edit_text(
            text, reply_markup=markup, disable_web_page_preview=True
        )
    except MessageNotModified:
        pass
    await query.answer()


@Client.on_callback_query(filters.regex(r"^chan_leave:(-?\d+):(\d+)") & admin_filter)
async def channels_leave_cb(client: Client, query: CallbackQuery):
    cid = int(query.matches[0].group(1))
    page = int(query.matches[0].group(2))
    try:
        await client.leave_chat(cid)
    except Exception:
        pass
    try:
        await _db.disable_chat(cid, "Admin Dashboard Left")
    except Exception:
        pass

    await query.answer(f"Left channel {cid}", show_alert=True)
    text, markup = await build_channels_dashboard(client, page=page)
    try:
        await query.message.edit_text(
            text, reply_markup=markup, disable_web_page_preview=True
        )
    except Exception:
        pass


@Client.on_callback_query(filters.regex(r"^chan_info:(-?\d+)") & admin_filter)
async def channels_info_cb(client: Client, query: CallbackQuery):
    cid = int(query.matches[0].group(1))
    try:
        chat = await client.get_chat(cid)
        await query.answer(
            f"Title: {chat.title}\nID: {chat.id}\nMembers: {chat.members_count}",
            show_alert=True,
        )
    except Exception as e:
        await query.answer(f"Error fetching channel: {e}", show_alert=True)


# ============================================================
# 📄 EXPORT SCRIPTS
# ============================================================
@Client.on_message(filters.command("exportusers") & admin_filter)
async def export_users_cmd(bot: Client, message: Message):
    status = await message.reply_text("⏳ **Exporting users from database...**")
    users_data = await _db.get_all_users()
    users = (
        await users_data.to_list(length=None)
        if hasattr(users_data, "to_list")
        else list(users_data)
    )
    total = len(users)

    if total == 0:
        return await status.edit_text("⚠️ No users found in the database.")

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    text = f"🧍 **User List Export**\n\n📅 Generated on: `{time_str} UTC`\n👥 Total Users: `{total}`\n\n"
    data = "\n".join(
        [
            f"{user.get('id', 'Unknown')} - {user.get('name', 'Unknown')}"
            for user in users
        ]
    )

    file = io.BytesIO(data.encode("utf-8"))
    file.name = "users_list.txt"

    await message.reply_document(document=file, caption=text)
    await status.delete()


@Client.on_message(filters.command("exportgroups") & admin_filter)
async def export_groups_cmd(bot: Client, message: Message):
    status = await message.reply_text("⏳ **Exporting groups from database...**")
    chats_data = await _db.get_all_chats()
    chats = (
        await chats_data.to_list(length=None)
        if hasattr(chats_data, "to_list")
        else list(chats_data)
    )
    total = len(chats)

    if total == 0:
        return await status.edit_text("⚠️ No groups found in the database.")

    groups = [chat for chat in chats if not str(chat.get("id", "")).startswith("-100")]
    supergroups = [chat for chat in chats if str(chat.get("id", "")).startswith("-100")]

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    text = f"💬 **Group Chats Export**\n\n📅 Generated on: `{time_str} UTC`\n🏘️ Total Chats: `{total}`\n👥 Groups: `{len(groups)}` | 📢 Supergroups: `{len(supergroups)}`\n\n"
    data = "\n".join(
        [
            f"{chat.get('id', 'Unknown')} - {chat.get('title', 'Unknown')}"
            for chat in chats
        ]
    )

    file = io.BytesIO(data.encode("utf-8"))
    file.name = "chats_list.txt"

    await message.reply_document(document=file, caption=text)
    await status.delete()


@Client.on_message(filters.command("exportchannels") & admin_filter)
async def export_channels_cmd(bot: Client, message: Message):
    status = await message.reply_text("⏳ **Exporting channels from database...**")
    chats_data = await _db.get_all_chats()
    chats = (
        await chats_data.to_list(length=None)
        if hasattr(chats_data, "to_list")
        else list(chats_data)
    )
    channels = [chat for chat in chats if str(chat.get("id", "")).startswith("-100")]
    total = len(channels)

    if total == 0:
        return await status.edit_text("⚠️ No channels found in the database.")

    time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    text = f"📢 **Channel List Export**\n\n📅 Generated on: `{time_str} UTC`\n📺 Total Channels: `{total}`\n\n"
    data = "\n".join(
        [
            f"{chat.get('id', 'Unknown')} - {chat.get('title', 'Unknown')}"
            for chat in channels
        ]
    )

    file = io.BytesIO(data.encode("utf-8"))
    file.name = "channels_list.txt"

    await message.reply_document(document=file, caption=text)
    await status.delete()


# ============================================================
# 🚪 JOIN REQUEST CATCHER & MANAGER
# ============================================================
@Client.on_chat_join_request()
async def join_reqs_handler(bot: Client, join_req: ChatJoinRequest):
    if not _join_reqs_db.isActive():
        return
    user = join_req.from_user
    try:
        await _join_reqs_db.add_user(
            user_id=user.id,
            first_name=user.first_name or "Unknown",
            username=user.username or "None",
            date=join_req.date,
        )
    except Exception as e:
        logger.error(f"Failed to log join request for {user.id}: {e}")


@Client.on_message(filters.command("totalrequests") & admin_filter)
async def total_requests(bot: Client, message: Message):
    if not _join_reqs_db.isActive():
        return await message.reply_text(
            "⚠️ Join request tracking is not active (DB inactive)."
        )
    try:
        total = await _join_reqs_db.total_requests()
        await message.reply_text(f"📨 **Total Join Requests:** <code>{total}</code>")
    except Exception as e:
        logger.error(f"Error fetching join requests: {e}")
        await message.reply_text(f"⚠️ Error: <code>{e}</code>")


@Client.on_message(filters.command("purgerequests") & admin_filter)
async def purge_requests(bot: Client, message: Message):
    if not _join_reqs_db.isActive():
        return await message.reply_text(
            "⚠️ Join request tracking is not active (DB inactive)."
        )

    uid = get_uid(message)
    ask_msg = await message.reply_text(
        "⚠️ **Are you sure you want to delete all join requests? (y/n)**"
    )
    try:
        resp = await native_listen(bot, message.chat.id, uid, timeout=60)
        try:
            await ask_msg.delete()
        except Exception:
            pass
        if resp:
            try:
                await resp.delete()
            except Exception:
                pass

        if not resp or not resp.text:
            return
        if resp.text.lower() == "y":
            count = await _join_reqs_db.clear_all()
            await message.reply_text(
                f"✅ All join requests purged successfully. (<code>{count}</code> deleted)"
            )
        else:
            await message.reply_text("❌ Operation cancelled.")
    except asyncio.TimeoutError:
        await message.reply_text("⌛ Timeout: No response, operation cancelled.")
    except Exception as e:
        await message.reply_text(f"⚠️ Error clearing requests: <code>{e}</code>")


# ============================================================
# ⚙️ MULTI-CHANNEL FORCESUB MANAGER
# ============================================================
async def get_fsub_config():
    if _fsub_db is not None:
        doc = await _fsub_db.find_one({"_id": "config"})
        if doc:
            return doc
    return {
        "is_enabled": True,
        "max_count": 0,
        "rotation_enabled": False,
        "channels": {},
    }


async def update_fsub_config(key, value):
    if _fsub_db is not None:
        await _fsub_db.update_one(
            {"_id": "config"}, {"$set": {key: value}}, upsert=True
        )


async def update_fsub_channels(channels_dict):
    if _fsub_db is not None:
        await _fsub_db.update_one(
            {"_id": "config"}, {"$set": {"channels": channels_dict}}, upsert=True
        )


async def add_vip(user_id: int):
    if _vip_db is not None:
        await _vip_db.update_one(
            {"user_id": user_id}, {"$set": {"is_vip": True}}, upsert=True
        )


async def rem_vip(user_id: int):
    if _vip_db is not None:
        await _vip_db.delete_one({"user_id": user_id})


async def is_vip(user_id: int):
    if _vip_db is not None:
        return bool(await _vip_db.find_one({"user_id": user_id}))
    return False


async def get_all_vips():
    if _vip_db is not None:
        return await _vip_db.find().to_list(length=None)
    return []


async def get_invite_link(bot: Client, chat_id_str: str, config: dict) -> str:
    channels = config.get("channels", {})
    channel_data = channels.get(chat_id_str, {})

    if channel_data.get("target"):
        return channel_data["target"]
    if channel_data.get("link"):
        return channel_data["link"]

    try:
        chat_id_int = int(chat_id_str.strip())
        chat = await bot.get_chat(chat_id_int)
        is_req = channel_data.get("type") == "req"
        invite = await bot.create_chat_invite_link(
            chat_id_int, creates_join_request=is_req
        )

        channels[chat_id_str]["link"] = invite.invite_link
        channels[chat_id_str]["title"] = chat.title
        await update_fsub_channels(channels)
        return invite.invite_link
    except ChatAdminRequired:
        return ""
    except Exception:
        return ""


async def check_user_in_channel(
    bot: Client, channel_id: int, user_id: int, is_req: bool = False
) -> bool:
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        if member.status in [
            enums.ChatMemberStatus.MEMBER,
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]:
            return True
    except UserNotParticipant:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await check_user_in_channel(bot, channel_id, user_id, is_req)
    except Exception:
        pass

    if is_req:
        try:
            if _join_reqs_db.isActive() and await _join_reqs_db.get_user(user_id):
                return True
        except Exception:
            pass
    return False


async def ForceSub(
    bot: Client, message: Message, file_id: str = None, mode: str = None
) -> bool:
    user = message.from_user
    if not user or user.id in get_admin_list():
        return True
    if await is_vip(user.id):
        return True

    config = await get_fsub_config()
    if not config.get("is_enabled", True):
        return True
    channels_dict = config.get("channels", {})
    if not channels_dict:
        return True

    rotation_enabled, max_count = config.get("rotation_enabled", False), config.get(
        "max_count", 0
    )

    if rotation_enabled:
        eligible_channels = list(channels_dict.keys())
        if eligible_channels:
            shift = int(time.time() / 3600) % len(eligible_channels)
            eligible_channels = eligible_channels[shift:] + eligible_channels[:shift]
    else:
        eligible_channels = [
            k for k, v in channels_dict.items() if v.get("status") == "active"
        ]

    not_joined_buttons = []
    channels_to_force = 0

    try:
        for chat_id_str in eligible_channels:
            if max_count > 0 and channels_to_force >= max_count:
                break

            data = channels_dict[chat_id_str]
            chat_id = int(chat_id_str)
            is_req = data.get("type") == "req"
            is_participant = await check_user_in_channel(bot, chat_id, user.id, is_req)

            if not is_participant:
                link = await get_invite_link(bot, chat_id_str, config)
                if link:
                    btn_text = "⚓ Request to Join" if is_req else "📢 Join Channel"
                    not_joined_buttons.append(
                        [
                            InlineKeyboardButton(
                                btn_text, url=link, style=ButtonStyle.PRIMARY
                            )
                        ]
                    )
                    channels_to_force += 1

        if not not_joined_buttons:
            await _plugin_db.add_fsub_user(user.id)
            return True

        cb_data = f"refresh_fsub_{file_id}" if file_id else "refresh_fsub_0"
        not_joined_buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ I've Joined",
                    callback_data=cb_data,
                    style=ButtonStyle.SUCCESS,
                )
            ]
        )

        b_settings = await get_bot_settings()
        custom_fsub_img = b_settings.get("fsub_img", getattr(info, "FSUB_IMG", None))

        try:
            if custom_fsub_img and str(custom_fsub_img).strip():
                try:
                    await message.reply_photo(
                        photo=custom_fsub_img,
                        caption="**⚠️ Please join the channels below to get your file!**",
                        reply_markup=InlineKeyboardMarkup(not_joined_buttons),
                    )
                except Exception:
                    await message.reply_text(
                        "**⚠️ Please join the channels below to get your file!**",
                        reply_markup=InlineKeyboardMarkup(not_joined_buttons),
                        disable_web_page_preview=True,
                    )
            else:
                await message.reply_text(
                    "**⚠️ Please join the channels below to get your file!**",
                    reply_markup=InlineKeyboardMarkup(not_joined_buttons),
                    disable_web_page_preview=True,
                )
        except UserIsBlocked:
            return False
        return False
    except Exception:
        return True


# 🚨 BUG FIX: Properly formatting the verified file delivery using get_file_details
@Client.on_callback_query(filters.regex(r"^refresh_fsub_(.*)"))
async def refresh_fsub_callback(bot: Client, query: CallbackQuery):
    user_id, file_id = query.from_user.id, query.matches[0].group(1)

    if await is_vip(user_id):
        return await query.answer("👑 VIP Verified!", show_alert=False)

    config = await get_fsub_config()
    channels_dict = config.get("channels", {})
    rotation_enabled, max_count = config.get("rotation_enabled", False), config.get(
        "max_count", 0
    )

    if not channels_dict:
        return await query.answer(
            "Force subscribe is no longer required.", show_alert=True
        )

    if rotation_enabled:
        eligible_channels = list(channels_dict.keys())
        if eligible_channels:
            shift = int(time.time() / 3600) % len(eligible_channels)
            eligible_channels = eligible_channels[shift:] + eligible_channels[:shift]
    else:
        eligible_channels = [
            k for k, v in channels_dict.items() if v.get("status") == "active"
        ]

    not_joined_buttons = []
    channels_to_force = 0

    for chat_id_str in eligible_channels:
        if max_count > 0 and channels_to_force >= max_count:
            break

        data = channels_dict[chat_id_str]
        is_req = data.get("type") == "req"
        is_participant = await check_user_in_channel(
            bot, int(chat_id_str), user_id, is_req
        )

        if not is_participant:
            link = await get_invite_link(bot, chat_id_str, config)
            if link:
                btn_text = "⚓ Request to Join" if is_req else "📢 Join Channel"
                not_joined_buttons.append(
                    [
                        InlineKeyboardButton(
                            btn_text, url=link, style=ButtonStyle.PRIMARY
                        )
                    ]
                )
                channels_to_force += 1

    if channels_to_force > 0:
        await query.answer(
            "❌ You haven't joined all required channels yet!", show_alert=True
        )
        cb_data = f"refresh_fsub_{file_id}" if file_id else "refresh_fsub_0"
        not_joined_buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ I've Joined",
                    callback_data=cb_data,
                    style=ButtonStyle.SUCCESS,
                )
            ]
        )
        try:
            await query.message.edit_reply_markup(
                InlineKeyboardMarkup(not_joined_buttons)
            )
        except MessageNotModified:
            pass
    else:
        await _plugin_db.add_fsub_user(user_id)
        try:
            await query.message.delete()
        except Exception:
            pass

        await query.answer("✅ Subscriptions verified!", show_alert=False)

        if file_id and str(file_id) != "0":
            if file_id.startswith("search_") or file_id.startswith("hdra_"):
                text = "✅ **Verification complete!**\n\n🔍 Your search is unlocked. Please click the button in the main group again to get your files!"
                try:
                    await bot.send_message(chat_id=user_id, text=text)
                except Exception:
                    pass
            else:
                files_ = await get_file_details(file_id)
                if files_:
                    file = files_[0]
                    title = file.get("file_name", "Unknown")
                    size = get_size(file.get("file_size", 0))
                    f_caption = file.get("caption", "")
                    db_file_id = file.get("full_file_id", file.get("file_id", file_id))

                    if getattr(info, "CUSTOM_FILE_CAPTION", None):
                        try:
                            f_caption = info.CUSTOM_FILE_CAPTION.format(
                                file_name=title,
                                file_size=size,
                                file_caption=f_caption,
                            )
                        except Exception:
                            pass
                    if not f_caption:
                        f_caption = f"<b>{title}</b>"

                    try:
                        btn = [
                            [
                                InlineKeyboardButton(
                                    "🎥 ಕನ್ನಡ ಹೊಸ ಮೂವೀಗಳು 🎥",
                                    url="https://t.me/sandalwood_kannada_moviesz",
                                )
                            ]
                        ]
                        await bot.send_cached_media(
                            chat_id=user_id,
                            file_id=db_file_id,
                            caption=f_caption,
                            reply_markup=InlineKeyboardMarkup(btn),
                        )
                    except Exception:
                        await bot.send_message(
                            chat_id=user_id,
                            text="✅ **Verification complete!**\n\n⚠️ *Please go back to the main group and click the file link again to get your movie!*",
                        )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text="✅ **Verification complete!**\n\n⚠️ *Please go back to the main group and click the link again.*",
                    )


# ============================================================
# 👑 VIP SYSTEM COMMANDS
# ============================================================
@Client.on_message(filters.command("addvip") & admin_filter)
async def cmd_addvip(bot: Client, message: Message):
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1:
        try:
            user_id = int(message.command[1])
        except ValueError:
            return await message.reply_text("❌ Invalid User ID.")
    else:
        return await message.reply_text(
            "⚠️ **Usage:** `/addvip <user_id>` or reply to a user."
        )

    await add_vip(user_id)
    await message.reply_text(
        f"👑 **User `{user_id}` added to VIP List!**\nThey will now bypass all Force Subscribe restrictions."
    )


@Client.on_message(filters.command("remvip") & admin_filter)
async def cmd_remvip(bot: Client, message: Message):
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    elif len(message.command) > 1:
        try:
            user_id = int(message.command[1])
        except ValueError:
            return await message.reply_text("❌ Invalid User ID.")
    else:
        return await message.reply_text(
            "⚠️ **Usage:** `/remvip <user_id>` or reply to a user."
        )

    await rem_vip(user_id)
    await message.reply_text(f"🗑️ **User `{user_id}` removed from VIP List!**")


@Client.on_message(filters.command("viplist") & admin_filter)
async def cmd_viplist(bot: Client, message: Message):
    vips = await get_all_vips()
    if not vips:
        return await message.reply_text("ℹ️ **VIP List is empty.**")

    text = "👑 **ForceSub VIP List:**\n\n"
    for i, vip in enumerate(vips, 1):
        text += f"{i}. `{vip['user_id']}`\n"
    await message.reply_text(text)


# ============================================================
# ⚙️ CONFIGURATION COMMANDS
# ============================================================
@Client.on_message(filters.command("rotatefsub") & admin_filter)
async def toggle_rotate(bot: Client, message: Message):
    config = await get_fsub_config()
    current = config.get("rotation_enabled", False)
    await update_fsub_config("rotation_enabled", not current)
    status = (
        "🟢 ENABLED (Smart 1-Hour User Rotation)"
        if not current
        else "🔴 DISABLED (Static Priority)"
    )
    await message.reply_text(
        f"🔄 **Auto-Rotation Engine:** {status}\n\n*When enabled, the bot automatically cycles through all channels and forces users into the ones they haven't joined yet!*"
    )


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
        return await message.reply_text(
            "⚙️ **Usage:** `/setfsubcount [number]`\n*Set to 0 for unlimited active channels.*"
        )
    try:
        count = int(message.command[1])
        if count < 0:
            raise ValueError
        await update_fsub_config("max_count", count)
        limit_text = (
            f"maximum `{count}` active channels at once"
            if count > 0
            else "unlimited channels simultaneously"
        )
        await message.reply_text(
            f"✅ **FSub Queue Limit Updated!**\nThe system will now force a {limit_text}."
        )
    except ValueError:
        await message.reply_text(
            "❌ **Invalid number.** Please provide a valid positive integer."
        )


@Client.on_message(filters.command("setfsub") & admin_filter)
async def add_dynamic_fsub(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/setfsub [channel_id]`")
    channel_id, uid = message.command[1], get_uid(message)

    try:
        chat = await bot.get_chat(int(channel_id))
    except Exception as e:
        return await message.reply_text(
            f"❌ **Failed to fetch channel.** Make sure the bot is an admin in `{channel_id}`.\n`Error: {e}`"
        )

    try:
        ask_msg = await message.reply_text(
            f"🎯 **Target:** `{chat.title}`\n\nDo you want this to be a **Join Request** channel?\n\nReply with `y` for Yes, or `n` for No (Normal invite)."
        )
        resp = await native_listen(bot, message.chat.id, uid, timeout=120)

        try:
            await ask_msg.delete()
        except Exception:
            pass
        if resp:
            try:
                await resp.delete()
            except Exception:
                pass

        if not resp or not resp.text:
            return await message.reply_text(
                "❌ **Invalid response.** Please send text (y/n)."
            )

        is_req = resp.text.lower() == "y"
        fsub_type = "req" if is_req else "regular"

        config = await get_fsub_config()
        channels, max_count = config.get("channels", {}), config.get("max_count", 0)

        active_count = len(
            [c for c in channels.values() if c.get("status") == "active"]
        )
        status = "pending" if max_count > 0 and active_count >= max_count else "active"

        invite = await bot.create_chat_invite_link(chat.id, creates_join_request=is_req)

        channels[channel_id] = {
            "title": chat.title,
            "link": invite.invite_link,
            "target": None,
            "type": fsub_type,
            "status": status,
        }
        await update_fsub_channels(channels)

        await message.reply_text(
            f"✅ **Successfully Configured FSub!**\n\n📢 **Channel:** `{chat.title}`\n🔗 **Link:** {invite.invite_link}\n⚙️ **Type:** `{'Join Request' if is_req else 'Normal'}`\n🟢 **Status:** `{status.upper()}`",
            disable_web_page_preview=True,
        )

    except ChatAdminRequired:
        await message.reply_text(
            "❌ **The bot is not an admin in that channel or lacks 'Invite Users' rights.**"
        )
    except asyncio.TimeoutError:
        await message.reply_text("⌛ **Timeout.** Setup cancelled.")
    except Exception as e:
        await message.reply_text(f"❌ **Error:** {e}")


@Client.on_message(filters.command("activatefsub") & admin_filter)
async def activate_fsub(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/activatefsub [channel_id]`")
    channel_id = message.command[1]

    config = await get_fsub_config()
    channels, max_count = config.get("channels", {}), config.get("max_count", 0)

    if channel_id not in channels:
        return await message.reply_text("❌ Channel not found in FSub database.")

    active_count = len([c for c in channels.values() if c.get("status") == "active"])
    if max_count > 0 and active_count >= max_count:
        return await message.reply_text(
            f"⚠️ **Queue Full!** You already have `{active_count}` active channels (Limit: {max_count}).\nUse `/deactivatefsub` on another channel first."
        )

    channels[channel_id]["status"] = "active"
    await update_fsub_channels(channels)
    await message.reply_text(f"✅ FSub for `{channel_id}` is now **ACTIVE**.")


@Client.on_message(filters.command("deactivatefsub") & admin_filter)
async def deactivate_fsub(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/deactivatefsub [channel_id]`")
    channel_id = message.command[1]

    config = await get_fsub_config()
    channels = config.get("channels", {})

    if channel_id in channels:
        channels[channel_id]["status"] = "pending"
        await update_fsub_channels(channels)
        await message.reply_text(
            f"⏸ FSub for `{channel_id}` is now **PENDING (Deactivated)**."
        )
    else:
        await message.reply_text("❌ Channel not found in FSub database.")


@Client.on_message(filters.command("updatefsubtarget") & admin_filter)
async def update_fsub_target(bot: Client, message: Message):
    if len(message.command) < 3:
        return await message.reply_text(
            "⚙️ **Usage:** `/updatefsubtarget [channel_id] [target_link]`\n*Use 'none' to remove custom target.*"
        )
    channel_id, target = str(message.command[1]), message.command[2]

    config = await get_fsub_config()
    channels = config.get("channels", {})

    if channel_id not in channels:
        return await message.reply_text(
            f"❌ Channel `{channel_id}` is not configured in FSub."
        )

    if target.lower() == "none":
        channels[channel_id]["target"] = None
        await message.reply_text(
            "✅ Custom target removed. Bot will use standard invite link."
        )
    else:
        channels[channel_id]["target"] = target
        await message.reply_text(f"✅ Target for `{channel_id}` updated to:\n{target}")

    await update_fsub_channels(channels)


@Client.on_message(filters.command("rmfsub") & admin_filter)
async def rem_fsub(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/rmfsub [channel_id]`")
    channel_id = message.command[1]

    config = await get_fsub_config()
    channels = config.get("channels", {})

    if channel_id in channels:
        del channels[channel_id]
        await update_fsub_channels(channels)
        await message.reply_text(
            f"🗑️ **ForceSub channel `{channel_id}` removed permanently.**"
        )
    else:
        await message.reply_text("❌ Channel not found in FSub database.")


@Client.on_message(filters.command("rmallfsub") & admin_filter)
async def rem_all_fsub(bot: Client, message: Message):
    await update_fsub_channels({})
    await message.reply_text(
        "🗑️ **ALL ForceSub channels have been removed and database is empty.**"
    )


async def build_fsub_list_text(title_prefix: str, filter_status: str = None) -> str:
    config = await get_fsub_config()
    channels = config.get("channels", {}).items()
    if filter_status:
        channels = [(k, v) for k, v in channels if v.get("status") == filter_status]

    if not channels:
        return f"❌ No {title_prefix.lower()} ForceSub channels found."
    text = f"📋 **{title_prefix} ForceSub Channels:**\n\n"
    for idx, (cid, data) in enumerate(channels, 1):
        status_emoji = "🟢" if data.get("status") == "active" else "🟡"
        req_type = "Req" if data.get("type") == "req" else "Normal"
        active_link = data.get("target") or data.get("link") or "No Link Generated"
        text += f"{idx}. {status_emoji} **{data.get('title', 'Unknown')}**\n├ ID: `{cid}`\n├ Type: `{req_type}`\n└ Link: {active_link}\n\n"
    return text


@Client.on_message(filters.command("getallfsub") & admin_filter)
async def get_all_fsub(bot: Client, message: Message):
    config = await get_fsub_config()
    max_count = config.get("max_count", 0)
    rot_status = "ON" if config.get("rotation_enabled", False) else "OFF"
    text = (
        f"⚙️ **Queue Limit:** `{max_count if max_count > 0 else 'Unlimited'}`\n🔄 **Auto-Rotation:** `{rot_status}`\n\n"
        + await build_fsub_list_text("All")
    )
    await message.reply_text(text, disable_web_page_preview=True)


@Client.on_message(filters.command("getactivefsub") & admin_filter)
async def get_active_fsub(bot: Client, message: Message):
    await message.reply_text(
        await build_fsub_list_text("Active", "active"), disable_web_page_preview=True
    )


@Client.on_message(filters.command("getpendingfsub") & admin_filter)
async def get_pending_fsub(bot: Client, message: Message):
    await message.reply_text(
        await build_fsub_list_text("Pending", "pending"), disable_web_page_preview=True
    )
