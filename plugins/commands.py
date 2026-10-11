import asyncio
import base64
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
    MediaEmpty,
    MessageIdInvalid,
    MessageNotModified,
    MessageTooLong,
    PeerIdInvalid,
    PhotoInvalidDimensions,
    QueryIdInvalid,
    UserIsBlocked,
    WebpageMediaEmpty,
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
from database.plugin_dbs import plugin_db as _plugin_db
from database.users_chats_db import db as _db
from info import CUSTOM_FILE_CAPTION, LOG_CHANNEL, PICS
from plugins.custom_settings import get_bot_settings, get_group_welcome
from plugins.fsubchannels import ForceSub
from Script import script
from utils import (
    admin_filter,
    get_admin_list,
    get_file_id,
    get_poster,
    get_settings,
    get_size,
    get_uid,
    parse_text_and_markup,
    temp,
)

logger = getLogger(__name__)
logger.setLevel(ERROR)

pm_db = cw_db = _del_db = _countdown_col = search_stats_col = users_col = chats_col = (
    media_col
) = _ui_settings = None
AUTO_DELETE_TASKS = set()
_COUNTDOWN_WORKER_STARTED = False

MESSAGE_EMOJI_PLANE = "✈️"
MESSAGE_EMOJI_LINK = "🔗"
BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER, BTN_SECONDARY = 1, 3, 4, 2

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    pm_db, cw_db, _del_db, _countdown_col = (
        _BOT_DB["pm_settings"],
        _BOT_DB["clean_words"],
        _BOT_DB["deletion_settings"],
        _BOT_DB["countdown_tasks"],
    )
    search_stats_col, users_col, chats_col, media_col = (
        _BOT_DB["search_analytics"],
        _BOT_DB["users"],
        _BOT_DB["chats"],
        _BOT_DB[getattr(info, "COLLECTION_NAME", "Anime_Files")],
    )
    _ui_settings = _BOT_DB["ui_config"]
except Exception as e:
    logger.error(f"Failed to init DBs: {e}")


def create_btn(text, url=None, callback_data=None, style=None):
    kwargs = {"text": text}
    if url:
        kwargs["url"] = url
    if callback_data:
        kwargs["callback_data"] = callback_data
    if style is not None:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


def format_btn_list(btns):
    if not btns:
        return "No custom buttons."
    return "".join(
        [
            f"**{i}.** `{b['text']}` | Layout: `{b['layout']}` | Color: `{b['color']}`\n"
            for i, b in enumerate(btns, 1)
        ]
    )


def parse_button_cmd(text):
    try:
        if "|" in text:
            parts = text.split(None, 1)[1].split("|", 1)
            return parts[0].strip(), parts[1].strip()
        match = re.search(r'["“]([^"”]+)["”]\s+(https?://\S+)', text)
        if match:
            return match.group(1).strip(), match.group(2).strip()
    except Exception:
        pass
    return None, None


async def get_ui():
    return (
        await _ui_settings.find_one({"_id": "bot_ui"})
        if _ui_settings is not None
        else {}
    )


async def update_ui(key, value):
    if _ui_settings is not None:
        await _ui_settings.update_one(
            {"_id": "bot_ui"}, {"$set": {key: value}}, upsert=True
        )


def parse_timer(var_name: str, default_time: int = 1800) -> int:
    try:
        global_switch = getattr(info, "AUTO_DELETE", True)
        if str(global_switch).strip().lower() in ["false", "off", "0"]:
            return 0
        val = getattr(info, "AUTO_DELETE_TIME", getattr(info, var_name, default_time))
        return 1800 if isinstance(val, bool) and val else int(val)
    except Exception:
        return 1800


async def live_text_auto_delete(
    client: Client, media_msg: Message, warning_msg: Message, delay: int
):
    expire_at = time.time() + delay
    while time.time() < expire_at:
        remaining = int(expire_at - time.time())
        if remaining <= 0:
            break
        m, s = divmod(remaining, 60)
        time_str = f"{m} Minutes {s} Seconds" if s > 0 else f"{m} Minutes"
        text = f"<b>📢 Please Note\n\n✅ The above file will be autodeleted in {time_str} to avoid copyright issues.\n\n✅ Please forward this file to your saved messages and start downloading from there.\n\nTᴇᴀᴍ: @KR_Picture</b>"
        try:
            await warning_msg.edit_text(text, parse_mode=enums.ParseMode.HTML)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception:
            pass
        await asyncio.sleep(min(60 if remaining > 60 else 10, remaining))
    try:
        await media_msg.delete()
    except Exception:
        pass
    try:
        await warning_msg.delete()
    except Exception:
        pass


def build_dynamic_keyboard(custom_buttons, static_buttons):
    keyboard, row = [], []
    for btn in custom_buttons:
        ibtn = create_btn(
            btn["text"],
            url=btn["url"],
            style={"blue": 1, "green": 3, "red": 4, "normal": None}.get(
                btn.get("color", "normal")
            ),
        )
        if btn.get("layout") == "sidebyside":
            row.append(ibtn)
            if len(row) == 2:
                keyboard.append(row)
                row = []
        else:
            if row:
                keyboard.append(row)
                row = []
            keyboard.append([ibtn])
    if row:
        keyboard.append(row)
    keyboard.extend(static_buttons)
    return InlineKeyboardMarkup(keyboard)


async def get_start_keyboard(user_id):
    ui = await get_ui()
    static_buttons = [
        [
            create_btn(
                "✈️ Gʀᴏᴜᴘ 1", url="https://t.me/Sandalwood_Kannada_Group", style=1
            ),
            create_btn("✈️ Gʀᴏᴜᴘ 2", url="http://t.me/Kannada_Filmy_Group", style=1),
            create_btn("✈️ Gʀᴏᴜᴘ 3", url="https://t.me/+GLsPkRgLGGszMzY1", style=1),
        ]
    ]
    if user_id in get_admin_list():
        static_buttons.append(
            [
                create_btn("ℹ️ 𝙷𝚎𝚕𝚙", callback_data="help"),
                create_btn("😊 𝙰𝚋𝚘𝚞𝚝", callback_data="about"),
            ]
        )
    static_buttons.append(
        [
            create_btn(
                "🔗 Nᴇᴡ Rᴇʟᴇᴀꜱᴇꜱ & Oᴛᴛ Uᴘᴅᴀᴛᴇꜱ",
                url="https://t.me/sandalwood_kannada_moviesz",
                style=3,
            )
        ]
    )
    return build_dynamic_keyboard(ui.get("start_buttons", []), static_buttons)


async def get_help_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                create_btn("🖥️ UI Start", callback_data="helps_uistart"),
                create_btn("🖥️ UI Help", callback_data="helps_uihelp"),
                create_btn("🖥️ UI About", callback_data="helps_uiabout"),
            ],
            [
                create_btn("👋 Welcome", callback_data="helps_welcome"),
                create_btn("🖼️ Images", callback_data="helps_images"),
            ],
            [
                create_btn("🔍 Spell Check", callback_data="helps_spell"),
                create_btn("📝 Filters", callback_data="helps_filters"),
            ],
            [
                create_btn("📱 Force Sub", callback_data="helps_forcesub"),
                create_btn("👥 Force Add", callback_data="helps_forceadd"),
            ],
            [
                create_btn("🚫 Bans", callback_data="helps_bans"),
                create_btn("🗑️ Delete", callback_data="helps_delete"),
            ],
            [
                create_btn("📢 Promotions", callback_data="helps_promotions"),
                create_btn("📚 Index", callback_data="helps_index"),
            ],
            [
                create_btn("⚙ Settings", callback_data="helps_settings"),
                create_btn("🌐 Connections", callback_data="helps_connections"),
            ],
            [
                create_btn("📊 Utilities", callback_data="helps_utilities"),
                create_btn("💬 Custom Messages", callback_data="helps_custommessages"),
            ],
            [
                create_btn("📝 Post Handle", callback_data="helps_posthand"),
                create_btn("📝 Custom Captions", callback_data="helps_customcaption"),
            ],
            [
                create_btn("🧬 Auto post", callback_data="helps_autopost"),
                create_btn("💬 Pm Auto Reply", callback_data="helps_pmautoreply"),
            ],
            [create_btn("💾 Backup", callback_data="helps_backup", style=3)],
            [
                create_btn("🔙 Back", callback_data="start", style=1),
                create_btn("🔐 Cʟᴏsᴇ", callback_data="close_data", style=4),
            ],
        ]
    )


async def transition_ui_message(client, msg_obj, text, keyboard, img=None):
    try:
        if img:
            if (
                getattr(msg_obj, "photo", None)
                or getattr(msg_obj, "video", None)
                or getattr(msg_obj, "document", None)
            ):
                await msg_obj.edit_caption(
                    caption=text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML
                )
            else:
                await msg_obj.delete()
                await client.send_photo(
                    chat_id=msg_obj.chat.id,
                    photo=img,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
        else:
            if (
                getattr(msg_obj, "photo", None)
                or getattr(msg_obj, "video", None)
                or getattr(msg_obj, "document", None)
            ):
                await msg_obj.delete()
                await client.send_message(
                    chat_id=msg_obj.chat.id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                await msg_obj.edit_text(
                    text=text, reply_markup=keyboard, parse_mode=enums.ParseMode.HTML
                )
    except MessageNotModified:
        pass
    except Exception as e:
        logger.error(f"UI Transition Error: {e}")


@Client.on_message(filters.command(["start", "help"]) & filters.incoming)
async def start(client: Client, message: Message):
    uid = get_uid(message)
    if getattr(info, "REPAIR_MODE", False) and uid not in get_admin_list():
        return await message.reply_text(
            "🛠️ <b>Bot is currently under maintenance!</b>",
            parse_mode=enums.ParseMode.HTML,
        )
    if getattr(message, "from_user", None) and await _plugin_db.is_banned(uid):
        return

    bot_uname, b_name, ui = (
        temp.U_NAME or "my_bot",
        temp.B_NAME or "MovieBot",
        await get_ui(),
    )
    is_help_command = (message.command[0] == "help") or (
        len(message.command) == 2 and message.command[1] == "help"
    )

    if is_help_command:
        text = (ui.get("help_text") or script.HELP_TXT).format(
            mention=(
                message.from_user.mention
                if getattr(message, "from_user", None)
                else "User"
            ),
            uname=bot_uname,
            bname=b_name,
        )
        final_text, final_keyboard = parse_text_and_markup(
            text, await get_help_keyboard()
        )
        img = ui.get("help_img")
        if img:
            return await message.reply_photo(
                photo=img,
                caption=final_text,
                reply_markup=final_keyboard,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            return await message.reply_text(
                text=final_text,
                reply_markup=final_keyboard,
                parse_mode=enums.ParseMode.HTML,
            )

    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        raw_text = (ui.get("start_text") or script.START_TXT).format(
            mention=(
                message.from_user.mention
                if getattr(message, "from_user", None)
                else message.chat.title
            ),
            uname=bot_uname,
            bname=b_name,
            plane_emoji=MESSAGE_EMOJI_PLANE,
            link_emoji=MESSAGE_EMOJI_LINK,
        )
        final_text, final_keyboard = parse_text_and_markup(
            raw_text,
            await get_start_keyboard(uid if getattr(message, "from_user", None) else 0),
        )
        await message.reply(
            final_text, reply_markup=final_keyboard, parse_mode=enums.ParseMode.HTML
        )
        await asyncio.sleep(2)
        if not await _db.get_chat(message.chat.id):
            total = await client.get_chat_members_count(message.chat.id)
            try:
                await client.send_message(
                    LOG_CHANNEL,
                    script.LOG_TEXT_G.format(
                        message.chat.title, message.chat.id, total, "Unknown"
                    ),
                )
            except Exception:
                pass
            await _db.add_chat(message.chat.id, message.chat.title)
        return

    if not await _db.is_user_exist(uid):
        await _db.add_user(uid, getattr(message.from_user, "first_name", "Unknown"))

    if len(message.command) != 2 or message.command[1] in ["error", "okay", "hehe"]:
        raw_text = (ui.get("start_text") or script.START_TXT).format(
            mention=message.from_user.mention,
            uname=bot_uname,
            bname=b_name,
            plane_emoji=MESSAGE_EMOJI_PLANE,
            link_emoji=MESSAGE_EMOJI_LINK,
        )
        final_text, final_keyboard = parse_text_and_markup(
            raw_text, await get_start_keyboard(uid)
        )
        db_img = ui.get("start_img")
        photo_to_send = db_img if db_img else (random.choice(PICS) if PICS else None)
        try:
            if photo_to_send:
                await message.reply_photo(
                    photo=photo_to_send,
                    caption=final_text,
                    reply_markup=final_keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                await message.reply_text(
                    text=final_text,
                    reply_markup=final_keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
        except (UserIsBlocked, PeerIdInvalid):
            pass
        return

    cmd_data = message.command[1]
    if cmd_data == "subscribe":
        return await ForceSub(client, message)

    if cmd_data.startswith("search_") or cmd_data.startswith("hdra_"):
        query = ""
        try:
            if cmd_data.startswith("search_"):
                query = cmd_data.replace("search_", "").replace("_", " ")
            else:
                token = cmd_data.replace("hdra_", "")
                padding = 4 - (len(token) % 4)
                if padding != 4:
                    token += "=" * padding
                query = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
            message.text = query
            from plugins.pm_filter import auto_filter

            return await auto_filter(client, message)
        except Exception as e:
            return await message.reply_text(
                f"🔍 **Search Query:** `{query}`\n\nTap the text above to copy it, then send it to me to search!"
            )

    kk, file_id = cmd_data.split("_", 1) if "_" in cmd_data else (False, False)
    pre = ("checksubp" if kk == "filep" else "checksub") if kk else False

    status = await ForceSub(client, message, file_id=file_id or cmd_data, mode=pre)
    if not status:
        return

    files_ = await get_file_details(file_id or cmd_data)
    if not files_:
        return await message.reply_text(
            "⚠️ No such file exists.", parse_mode=enums.ParseMode.HTML
        )

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
        files.get("full_file_id", files.get("file_id", file_id or cmd_data))
        if isinstance(files, dict)
        else getattr(
            files, "full_file_id", getattr(files, "file_id", file_id or cmd_data)
        )
    )

    if CUSTOM_FILE_CAPTION:
        try:
            f_caption = CUSTOM_FILE_CAPTION.format(
                file_name="" if title == "Unknown" else title,
                file_size="" if size == "0B" else size,
                file_caption="" if not f_caption else f_caption,
            )
        except Exception:
            pass
    if not f_caption:
        f_caption = f"<b>{title}</b>"

    final_caption, final_keyboard = parse_text_and_markup(
        f_caption,
        InlineKeyboardMarkup(
            [
                [
                    create_btn(
                        text="🎥 ಕನ್ನಡ ಹೊಸ ಮೂವೀಗಳು 🎥",
                        url="https://t.me/Sandalwood_kannada_moviesz",
                        style=BTN_SUCCESS,
                    )
                ]
            ]
        ),
    )

    msg = None
    try:
        msg = await client.send_cached_media(
            chat_id=uid,
            file_id=db_file_id,
            caption=final_caption,
            reply_markup=final_keyboard,
            protect_content=True if kk in ["filep", "checksubp"] else False,
            parse_mode=enums.ParseMode.HTML,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        msg = await client.send_cached_media(
            chat_id=uid,
            file_id=db_file_id,
            caption=final_caption,
            protect_content=True if kk in ["filep", "checksubp"] else False,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e1:
        try:
            msg = await client.send_document(
                chat_id=uid,
                document=db_file_id,
                caption=final_caption,
                reply_markup=final_keyboard,
                protect_content=True if kk in ["filep", "checksubp"] else False,
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception as err:
            return await message.reply_text(
                f"⚠️ <b>Error:</b>\n<code>{err}</code>", parse_mode=enums.ParseMode.HTML
            )

    if msg:
        try:
            delete_timer = parse_timer("FILE_AUTO_DELETE", 1800)
            if delete_timer > 0:
                m, s = divmod(delete_timer, 60)
                time_str = f"{m} Minutes {s} Seconds" if s > 0 else f"{m} Minutes"
                k = await client.send_message(
                    chat_id=uid,
                    text=f"<b>📢 Please Note\n\n✅ The above file will be autodeleted in {time_str} to avoid copyright issues.\n\n✅ Please forward this file to your saved messages and start downloading from there.\n\nTᴇᴀᴍ: @KR_Picture</b>",
                    reply_to_message_id=msg.id,
                    parse_mode=enums.ParseMode.HTML,
                )
                asyncio.create_task(live_text_auto_delete(client, msg, k, delete_timer))
        except Exception as e:
            logger.error(f"Auto-delete warning failed to send: {e}")


@Client.on_callback_query(
    filters.regex(r"^(close_data|channels_page#.*|start|help|about|helps_.*|stats)$")
)
async def cb_handler(client: Client, query: CallbackQuery):
    if getattr(info, "REPAIR_MODE", False):
        if (
            get_uid(query.message) not in get_admin_list()
            and query.data != "close_data"
        ):
            return await query.answer("🛠️ Bot is under maintenance!", show_alert=True)
    try:
        if query.data == "close_data":
            await query.message.delete()
        elif query.data == "start":
            await query.answer()
            ui = await get_ui()
            raw_text = (ui.get("start_text") or script.START_TXT).format(
                mention=query.from_user.mention,
                uname=temp.U_NAME or "my_bot",
                bname=temp.B_NAME or "MovieBot",
                plane_emoji=MESSAGE_EMOJI_PLANE,
                link_emoji=MESSAGE_EMOJI_LINK,
            )
            final_text, final_keyboard = parse_text_and_markup(
                raw_text, await get_start_keyboard(query.from_user.id)
            )
            await transition_ui_message(
                client, query.message, final_text, final_keyboard, ui.get("start_img")
            )
        elif query.data == "help":
            await query.answer()
            ui = await get_ui()
            raw_text = (ui.get("help_text") or script.HELP_TXT).format(
                mention=query.from_user.mention,
                uname=temp.U_NAME or "my_bot",
                bname=temp.B_NAME or "MovieBot",
            )
            final_text, final_keyboard = parse_text_and_markup(
                raw_text, await get_help_keyboard()
            )
            await transition_ui_message(
                client, query.message, final_text, final_keyboard, ui.get("help_img")
            )
        elif query.data == "about":
            await query.answer()
            ui = await get_ui()
            raw_text = (ui.get("about_text") or script.ABOUT_TXT).format(
                mention=query.from_user.mention, bname=temp.B_NAME or "MovieBot"
            )
            final_text, final_keyboard = parse_text_and_markup(
                raw_text,
                InlineKeyboardMarkup(
                    [
                        [create_btn("Sᴛᴀᴛᴜs ​", callback_data="stats")],
                        [
                            create_btn("🏘 Hᴏᴍᴇ", callback_data="start", style=3),
                            create_btn("🔐 Cʟᴏsᴇ", callback_data="close_data", style=4),
                        ],
                    ]
                ),
            )
            await transition_ui_message(
                client, query.message, final_text, final_keyboard, ui.get("about_img")
            )
        elif query.data.startswith("helps_"):
            await query.answer()
            buttons = [[create_btn("🔙 Back", callback_data="help")]]
            help_dict = {
                "helps_uistart": ("UISTART_TXT", "🎨 UI Start Menu"),
                "helps_uihelp": ("UIHELP_TXT", "🎨 UI Help Menu"),
                "helps_uiabout": ("UIABOUT_TXT", "🎨 UI About Menu"),
                "helps_welcome": ("WELCOME_TXT", "👋 Welcome Help"),
                "helps_images": ("IMAGES_TXT", "🖼️ Images Help"),
                "helps_spell": ("SPELLCHECK_TXT", "🔍 Spell Check Help"),
                "helps_filters": ("FILTERS_TXT", "📝 Filters Help"),
                "helps_forcesub": ("FORCESUB_TXT", "📱 Force Sub Help"),
                "helps_forceadd": ("FORCEADD_TXT", "👥 Force Add Help"),
                "helps_bans": ("BANS_TXT", "🚫 Bans Help"),
                "helps_delete": ("DELETE_TXT", "🗑 Delete Help"),
                "helps_promotions": ("PROMOTIONS_TXT", "📢 Promotions Help"),
                "helps_index": ("INDEX_TXT", "📚 Index Help"),
                "helps_settings": ("SETTINGS_TXT", "⚙️ Settings Help"),
                "helps_connections": ("CONNECTIONS_TXT", "🌐 Connections Help"),
                "helps_utilities": ("UTILITIES_TXT", "📊 Utilities Help"),
                "helps_custommessages": ("CUSTOMMESSAGES_TXT", "💬 Custom Messages"),
                "helps_posthand": ("POSTHAND_TXT", "📝 Post Handle Help"),
                "helps_customcaption": ("CUSTOMCAPTION_TXT", "📝 Custom Captions Help"),
                "helps_backup": ("BACKUP_TXT", "💾 Backup Help"),
                "helps_autopost": ("AUTOPOST_TXT", "🧬 Auto Post Help"),
                "helps_pmautoreply": ("PMAUTOREPLY_TXT", "💬 PM Auto Reply Help"),
            }
            target_var, default_text = help_dict.get(
                query.data, ("HELP_TXT", "Help unavailable.")
            )
            text = getattr(script, target_var, default_text)
            try:
                if (
                    getattr(query.message, "photo", None)
                    or getattr(query.message, "video", None)
                    or getattr(query.message, "animation", None)
                    or getattr(query.message, "document", None)
                ):
                    await query.message.edit_caption(
                        caption=text,
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode=enums.ParseMode.HTML,
                    )
                else:
                    await query.message.edit_text(
                        text=text,
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode=enums.ParseMode.HTML,
                    )
            except MessageNotModified:
                pass
            except Exception:
                pass
    except QueryIdInvalid:
        pass


# ============================================================
# 🗑️ AUTOMATIC DELETION & PURGER ENGINE
# ============================================================
def format_countdown_string(seconds_left: int) -> str:
    if seconds_left <= 0:
        return "⏳ Deleting now..."
    m, s = divmod(seconds_left, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"⏳ Deleting in: {h}h {m}m"
    return f"⏳ Deleting in: {m}m {s}s"


async def countdown_live_worker(client: Client):
    while True:
        try:
            if _countdown_col is not None:
                now = time.time()
                tasks = await _countdown_col.find().to_list(length=100)
                for task in tasks:
                    chat_id, msg_id, expiry = (
                        task["chat_id"],
                        task["message_id"],
                        task["delete_at"],
                    )
                    remaining = int(expiry - now)
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
                        new_keyboard = [
                            [
                                InlineKeyboardButton(
                                    b["text"],
                                    url=b.get("url"),
                                    callback_data=b.get("callback_data"),
                                )
                                for b in row
                            ]
                            for row in task.get("buttons", [])
                        ]
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
                            pass
                        except FloodWait as e:
                            await asyncio.sleep(e.value + 1)
                        except Exception:
                            await _countdown_col.delete_one({"_id": task["_id"]})
        except Exception as e:
            logger.error(f"Countdown worker error: {e}")
        await asyncio.sleep(10)


@Client.on_message(group=-99)
async def start_countdown_worker(client: Client, message: Message):
    global _COUNTDOWN_WORKER_STARTED
    if not _COUNTDOWN_WORKER_STARTED:
        _COUNTDOWN_WORKER_STARTED = True
        asyncio.create_task(countdown_live_worker(client))


async def schedule_file_with_countdown(
    client: Client, sent_message: Message, delay_seconds: int, base_buttons: list = None
):
    if not sent_message or delay_seconds <= 0 or _countdown_col is None:
        return
    now = time.time()
    serialized_btns = (
        [
            [
                {
                    "text": btn.text,
                    "url": getattr(btn, "url", None),
                    "callback_data": getattr(btn, "callback_data", None),
                }
                for btn in row
            ]
            for row in base_buttons
        ]
        if base_buttons
        else []
    )
    await _countdown_col.insert_one(
        {
            "chat_id": sent_message.chat.id,
            "message_id": sent_message.id,
            "delete_at": now + delay_seconds,
            "last_updated": now,
            "buttons": serialized_btns,
        }
    )


@Client.on_callback_query(filters.regex("^noop_countdown$"))
async def noop_countdown_cb(client: Client, query):
    await query.answer(
        "⚠️ This file will be automatically deleted when the timer ends. Forward or save it now!",
        show_alert=True,
    )


@Client.on_message(filters.command("purgeduplicates") & admin_filter)
async def purge_duplicates_cmd(client: Client, message: Message):
    status = await message.reply_text(
        "🔍 **Scanning database for duplicate files...**\nThis might take some time on large databases."
    )

    def get_resolution_score(filename: str) -> int:
        name = filename.lower()
        if any(q in name for q in ["2160p", "4k", "uhd"]):
            return 4
        if any(q in name for q in ["1440p", "2k"]):
            return 3
        if any(q in name for q in ["1080p", "fhd"]):
            return 2
        if any(q in name for q in ["720p", "hd"]):
            return 1
        return 0

    try:
        duplicate_groups = await _Media.collection.aggregate(
            [
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
        ).to_list(length=None)
        if not duplicate_groups:
            return await status.edit_text(
                "✅ **Database is squeaky clean!** No duplicate files found."
            )

        total_duplicates = 0
        deleted_ids = []
        for group in duplicate_groups:
            docs = group["docs"]
            docs.sort(
                key=lambda d: (
                    get_resolution_score(d.get("name", "")),
                    d.get("size", 0),
                ),
                reverse=True,
            )
            for d in docs[1:]:
                deleted_ids.append(d["id"])
                total_duplicates += 1

        for i in range(0, len(deleted_ids), 500):
            await _Media.collection.delete_many(
                {"_id": {"$in": deleted_ids[i : i + 500]}}
            )

        await status.edit_text(
            f"🧹 **Duplicate File Purge Completed!**\n\n📁 **Duplicate Sets Found:** `{len(duplicate_groups)}`\n🗑️ **Redundant Copies Removed:** `{total_duplicates}`\n✨ **Preserved:** Best quality copies."
        )
    except Exception as e:
        await status.edit_text(f"❌ **Error while purging duplicates:**\n`{e}`")


@Client.on_message(filters.command("delete") & admin_filter)
async def delete_single_file(bot: Client, message: Message):
    try:
        if len(message.command) == 2:
            file_id = message.command[1].strip()
            status_msg = await message.reply_text("🧹 **Deleting file from DB...**")
            result = await _Media.collection.delete_one({"_id": file_id})
            return await status_msg.edit_text(
                f"✅ **File `{file_id}` deleted successfully.**"
                if result.deleted_count
                else "⚠️ **File not found in database.**"
            )

        reply = message.reply_to_message
        if not (reply and reply.media):
            return await message.reply_text(
                "⚙️ **Usage:**\n`/delete <file_id>`\nOr reply to a file with `/delete`"
            )

        status_msg = await message.reply_text("⏳ **Processing...**")
        media = getattr(reply, reply.media.value, None) if reply.media else None
        if not media:
            return await status_msg.edit_text(
                "❌ **This is not a supported file format.**"
            )

        file_id, _ = unpack_new_file_id(media.file_id)
        if (await _Media.collection.delete_one({"_id": file_id})).deleted_count:
            return await status_msg.edit_text(
                "✅ **File successfully deleted from database.**"
            )

        file_name = re.sub(r"(_|\-|\.|\+)", " ", str(getattr(media, "file_name", "")))
        res = await _Media.collection.delete_many(
            {
                "file_name": file_name,
                "file_size": getattr(media, "file_size", 0),
                "mime_type": getattr(media, "mime_type", ""),
            }
        )
        await status_msg.edit_text(
            f"✅ **Deleted {res.deleted_count} duplicate files from database.**"
            if res.deleted_count
            else "⚠️ **File not found in database.**"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error while deleting:**\n`{e}`")


@Client.on_message(filters.command("delmulti") & admin_filter)
async def delete_multiple_files(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/delmulti <keyword/name>`")
    keyword = message.text.split(None, 1)[1]
    status_msg = await message.reply_text(
        f"⏳ **Searching and deleting files matching:** `{keyword}`..."
    )
    try:
        result = await _Media.collection.delete_many(
            {"file_name": {"$regex": keyword, "$options": "i"}}
        )
        await status_msg.edit_text(
            f"✅ **Successfully deleted `{result.deleted_count}` files matching:** `{keyword}`"
            if result.deleted_count > 0
            else f"⚠️ **No files found matching:** `{keyword}`"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ **Error while deleting files:**\n`{e}`")


@Client.on_message(filters.command("autodelete") & admin_filter)
async def set_autodelete_timer(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"⚙️ **Usage:** `/autodelete [seconds]`\n\n⏱ **Current File Auto-Delete:** `{getattr(info, 'AUTO_DELETE_TIME', 1800)}` seconds."
        )
    try:
        seconds = int(message.command[1])
        if seconds < 0:
            raise ValueError
        info.AUTO_DELETE_TIME = seconds
        await message.reply_text(
            "✅ **File Auto-Delete has been DISABLED permanently.**"
            if seconds == 0
            else f"✅ **File Auto-Delete permanently set to:** `{seconds}` seconds."
        )
    except ValueError:
        await message.reply_text(
            "❌ **Invalid duration!** Please provide a valid positive integer."
        )


@Client.on_message(filters.command("buttondel") & admin_filter)
async def set_buttondel_timer(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"⚙️ **Usage:** `/buttondel [seconds]`\n\n⏱ **Current Button Auto-Delete:** `{getattr(info, 'BUTTON_AUTO_DELETE', 1800)}` seconds."
        )
    try:
        seconds = int(message.command[1])
        if seconds < 0:
            raise ValueError
        info.BUTTON_AUTO_DELETE = seconds
        await message.reply_text(
            "✅ **Group Button Auto-Delete has been DISABLED permanently.**"
            if seconds == 0
            else f"✅ **Group Button Auto-Delete permanently set to:** `{seconds}` seconds."
        )
    except ValueError:
        await message.reply_text(
            "❌ **Invalid duration!** Please provide a valid positive integer."
        )


# ============================================================
# 📝 UI SETTINGS EDITORS
# ============================================================
def _upload_sync(file_bytes):
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": ("img.jpg", file_bytes, "image/jpeg")},
            timeout=6,
        )
    except:
        res = None
    if res and res.status_code == 200 and res.text.startswith("http"):
        return res.text.strip()
    try:
        res = requests.post(
            "https://graph.org/upload",
            files={"file": ("img.jpg", file_bytes, "image/jpeg")},
            timeout=6,
        )
    except:
        res = None
    if res and res.status_code == 200:
        return "https://graph.org" + res.json()[0]["src"]
    try:
        res = requests.post(
            "https://envs.sh",
            files={"file": ("img.jpg", file_bytes, "image/jpeg")},
            headers=headers,
            timeout=6,
        )
    except:
        res = None
    if res and res.status_code == 200 and res.text.startswith("http"):
        return res.text.strip()
    return None


async def upload_image_safely(client: Client, message: Message):
    try:
        file_io = await client.download_media(message, in_memory=True)
        if not file_io:
            return None
        return await asyncio.to_thread(_upload_sync, file_io.getvalue())
    except Exception:
        return None


async def process_image_upload(
    client: Client, message: Message, db_key: str, menu_name: str
):
    if not getattr(message, "reply_to_message", None) or not getattr(
        message.reply_to_message, "photo", None
    ):
        return await message.reply_text(
            f"⚠️ **Usage:** Reply to a photo with the command."
        )
    status = await message.reply_text("⏳ **Uploading to Cloud...**")
    url = await upload_image_safely(client, message.reply_to_message)
    if url:
        await update_ui(db_key, url)
        await status.edit_text(
            f"✅ **{menu_name} Image updated!**\n*(Saved permanently to cloud)*"
        )
    else:
        file_id = message.reply_to_message.photo.file_id
        await update_ui(db_key, file_id)
        await status.edit_text(
            f"⚠️ Cloud blocked. **{menu_name} Image updated!**\n*(Saved as Telegram File_ID)*"
        )


@Client.on_message(filters.command("setstarttext") & admin_filter)
async def set_start_text(client, message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **Usage:** `/setstarttext <text>`")
    await update_ui("start_text", message.text.split(None, 1)[1])
    await message.reply_text("✅ **Start Menu Text updated!**")


@Client.on_message(filters.command("defaultstarttext") & admin_filter)
async def def_start_text(client, message):
    await update_ui("start_text", None)
    await message.reply_text("✅ **Start Menu Text reset to default.**")


@Client.on_message(filters.command("setstartimage") & admin_filter)
async def set_start_img(client, message):
    await process_image_upload(client, message, "start_img", "Start Menu")


@Client.on_message(filters.command("remstartimage") & admin_filter)
async def rem_start_img(client, message):
    await update_ui("start_img", None)
    await message.reply_text("🗑️ **Start Menu Image removed.**")


@Client.on_message(filters.command("addstartbutton") & admin_filter)
async def add_start_btn(client, message):
    btn_text, url = parse_button_cmd(message.text)
    if not btn_text:
        return await message.reply_text(
            "⚠️ **Usage:** `/addstartbutton Text | https://link.com`"
        )
    ui = await get_ui()
    btns = ui.get("start_buttons", [])
    btns.append({"text": btn_text, "url": url, "layout": "belowside", "color": "blue"})
    await update_ui("start_buttons", btns)
    await message.reply_text(
        f"✅ **Start Button Added!**\n\nCurrent Buttons:\n{format_btn_list(btns)}"
    )


@Client.on_message(filters.command("editstartbuttons") & admin_filter)
async def edit_start_btn(client, message):
    try:
        parts = message.text.split()[1:]
        if len(parts) < 3:
            raise ValueError
        idx, layout, color = int(parts[0]) - 1, parts[1].lower(), parts[2].lower()
        if layout not in ["sidebyside", "belowside"]:
            return await message.reply_text(
                "⚠️ Layout must be `sidebyside` or `belowside`"
            )
        if color not in ["green", "red", "blue", "gray"]:
            return await message.reply_text(
                "⚠️ Color must be `green`, `red`, `blue`, or `gray`"
            )
        ui = await get_ui()
        btns = ui.get("start_buttons", [])
        btns[idx]["layout"], btns[idx]["color"] = layout, color
        await update_ui("start_buttons", btns)
        await message.reply_text(
            f"✅ **Start Button #{idx+1} Edited!**\n\n{format_btn_list(btns)}"
        )
    except (IndexError, ValueError):
        await message.reply_text(
            "⚠️ **Usage:** `/editstartbuttons <number> <sidebyside/belowside> <color>`"
        )


@Client.on_message(filters.command("remstartbutton") & admin_filter)
async def rem_start_btn(client, message):
    await update_ui("start_buttons", [])
    await message.reply_text("🗑️ **All custom Start Menu buttons cleared.**")


@Client.on_message(filters.command("sethelptext") & admin_filter)
async def set_help_text(client, message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **Usage:** `/sethelptext <text>`")
    await update_ui("help_text", message.text.split(None, 1)[1])
    await message.reply_text("✅ **Help Menu Text updated!**")


@Client.on_message(filters.command("defaulthelptext") & admin_filter)
async def def_help_text(client, message):
    await update_ui("help_text", None)
    await message.reply_text("✅ **Help Menu Text reset to default.**")


@Client.on_message(filters.command("sethelpimage") & admin_filter)
async def set_help_img(client, message):
    await process_image_upload(client, message, "help_img", "Help Menu")


@Client.on_message(filters.command("remhelpimage") & admin_filter)
async def rem_help_img(client, message):
    await update_ui("help_img", None)
    await message.reply_text("🗑️ **Help Menu Image removed.**")


@Client.on_message(filters.command("addhelpbutton") & admin_filter)
async def add_help_btn(client, message):
    btn_text, url = parse_button_cmd(message.text)
    if not btn_text:
        return await message.reply_text(
            "⚠️ **Usage:** `/addhelpbutton Text | https://link.com`"
        )
    ui = await get_ui()
    btns = ui.get("help_buttons", [])
    btns.append({"text": btn_text, "url": url, "layout": "belowside", "color": "blue"})
    await update_ui("help_buttons", btns)
    await message.reply_text(
        f"✅ **Help Button Added!**\n\nCurrent Buttons:\n{format_btn_list(btns)}"
    )


@Client.on_message(filters.command("edithelpbuttons") & admin_filter)
async def edit_help_btn(client, message):
    try:
        parts = message.text.split()[1:]
        idx, layout, color = int(parts[0]) - 1, parts[1].lower(), parts[2].lower()
        if layout not in ["sidebyside", "belowside"]:
            return await message.reply_text(
                "⚠️ Layout must be `sidebyside` or `belowside`"
            )
        if color not in ["green", "red", "blue", "gray"]:
            return await message.reply_text(
                "⚠️ Color must be `green`, `red`, `blue`, or `gray`"
            )
        ui = await get_ui()
        btns = ui.get("help_buttons", [])
        btns[idx]["layout"], btns[idx]["color"] = layout, color
        await update_ui("help_buttons", btns)
        await message.reply_text(
            f"✅ **Help Button #{idx+1} Edited!**\n\n{format_btn_list(btns)}"
        )
    except (IndexError, ValueError):
        await message.reply_text(
            "⚠️ **Usage:** `/edithelpbuttons <number> <sidebyside/belowside> <color>`"
        )


@Client.on_message(filters.command("remhelpbutton") & admin_filter)
async def rem_help_btn(client, message):
    await update_ui("help_buttons", [])
    await message.reply_text("🗑️ **All custom Help Menu buttons cleared.**")


@Client.on_message(filters.command("setabouttext") & admin_filter)
async def set_about_text(client, message):
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **Usage:** `/setabouttext <text>`")
    await update_ui("about_text", message.text.split(None, 1)[1])
    await message.reply_text("✅ **About Menu Text updated!**")


@Client.on_message(filters.command("defaultabouttext") & admin_filter)
async def def_about_text(client, message):
    await update_ui("about_text", None)
    await message.reply_text("✅ **About Menu Text reset to default.**")


@Client.on_message(filters.command("setaboutimage") & admin_filter)
async def set_about_img(client, message):
    await process_image_upload(client, message, "about_img", "About Menu")


@Client.on_message(filters.command("remaboutimage") & admin_filter)
async def rem_about_img(client, message):
    await update_ui("about_img", None)
    await message.reply_text("🗑️ **About Menu Image removed.**")


@Client.on_message(filters.command("addaboutbutton") & admin_filter)
async def add_about_btn(client, message):
    btn_text, url = parse_button_cmd(message.text)
    if not btn_text:
        return await message.reply_text(
            "⚠️ **Usage:** `/addaboutbutton Text | https://link.com`"
        )
    ui = await get_ui()
    btns = ui.get("about_buttons", [])
    btns.append({"text": btn_text, "url": url, "layout": "belowside", "color": "blue"})
    await update_ui("about_buttons", btns)
    await message.reply_text(
        f"✅ **About Button Added!**\n\nCurrent Buttons:\n{format_btn_list(btns)}"
    )


@Client.on_message(filters.command("editaboutbuttons") & admin_filter)
async def edit_about_btn(client, message):
    try:
        parts = message.text.split()[1:]
        idx, layout, color = int(parts[0]) - 1, parts[1].lower(), parts[2].lower()
        if layout not in ["sidebyside", "belowside"]:
            return await message.reply_text(
                "⚠️ Layout must be `sidebyside` or `belowside`"
            )
        if color not in ["green", "red", "blue", "gray"]:
            return await message.reply_text(
                "⚠️ Color must be `green`, `red`, `blue`, or `gray`"
            )
        ui = await get_ui()
        btns = ui.get("about_buttons", [])
        btns[idx]["layout"], btns[idx]["color"] = layout, color
        await update_ui("about_buttons", btns)
        await message.reply_text(
            f"✅ **About Button #{idx+1} Edited!**\n\n{format_btn_list(btns)}"
        )
    except (IndexError, ValueError):
        await message.reply_text(
            "⚠️ **Usage:** `/editaboutbuttons <number> <sidebyside/belowside> <color>`"
        )


@Client.on_message(filters.command("remaboutbutton") & admin_filter)
async def rem_about_btn(client, message):
    await update_ui("about_buttons", [])
    await message.reply_text("🗑️ **All custom About Menu buttons cleared.**")


@Client.on_message(filters.command(["preview_ui", "preview"]) & admin_filter)
async def preview_ui_cmd(client: Client, message: Message):
    if len(message.command) < 2 or message.command[1].lower() not in [
        "start",
        "help",
        "about",
    ]:
        return await message.reply_text(
            "⚠️ **Usage:**\n`/preview_ui start`\n`/preview_ui help`\n`/preview_ui about`"
        )
    menu = message.command[1].lower()
    ui = await get_ui()
    text = ui.get(
        f"{menu}_text",
        f"*(Default {menu.title()} Text Not Set)*\n\nPlease set it using `/set{menu}text`.",
    ).replace(
        "{mention}",
        message.from_user.mention if getattr(message, "from_user", None) else "Admin",
    )
    img, keyboard = ui.get(f"{menu}_img"), []
    for b in ui.get(f"{menu}_buttons", []):
        btn_obj = InlineKeyboardButton(b["text"], url=b["url"])
        if b.get("layout") == "sidebyside" and keyboard and len(keyboard[-1]) < 2:
            keyboard[-1].append(btn_obj)
        else:
            keyboard.append([btn_obj])
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    try:
        if img:
            await message.reply_photo(
                photo=img, caption=text, reply_markup=reply_markup
            )
        else:
            await message.reply_text(
                text=text, reply_markup=reply_markup, disable_web_page_preview=True
            )
    except Exception as e:
        await message.reply_text(f"❌ **Preview Render Error:**\n`{e}`")


@Client.on_message(filters.command("setcleanfilename") & admin_filter)
async def set_clean_words(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            "⚠️ **Usage:** `/setcleanfilename word1, word2, @ChannelName`"
        )
    words_list = [
        w.strip() for w in message.text.split(None, 1)[1].split(",") if w.strip()
    ]
    if cw_db is not None:
        await cw_db.update_one(
            {"id": "words"},
            {"$set": {"list": words_list, "use_default": False}},
            upsert=True,
        )
        await message.reply_text(
            f"✅ **Clean Words Set Successfully!**\n\nThe following words will be removed from buttons:\n`{', '.join(words_list)}`"
        )


@Client.on_message(filters.command("defaultcleanfilename") & admin_filter)
async def default_clean_words(client: Client, message: Message):
    if cw_db is not None:
        await cw_db.update_one(
            {"id": "words"}, {"$set": {"use_default": True}}, upsert=True
        )
        await message.reply_text(
            "✅ **Reverted to Default Clean Words!**\n(Standard extensions and default tags will be removed)."
        )


@Client.on_message(filters.command("remcleanfilename") & admin_filter)
async def rem_clean_words(client: Client, message: Message):
    if cw_db is not None:
        await cw_db.update_one(
            {"id": "words"}, {"$set": {"list": [], "use_default": False}}, upsert=True
        )
        await message.reply_text(
            "🗑️ **All Clean Words Removed.**\nFilenames will now display exactly as they are uploaded."
        )
