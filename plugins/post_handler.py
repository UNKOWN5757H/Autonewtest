import asyncio
import base64
import html
import math
import random
import re
import time
import urllib.parse
from logging import ERROR, getLogger

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import ButtonUrlInvalid, FloodWait, MessageNotModified
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

import info
from plugins.pm_filter import get_movie_detailsx
from utils import get_size, temp

logger = getLogger(__name__)
logger.setLevel(ERROR)

drafts_db = ap_db = hash_db = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    drafts_db, ap_db, hash_db = (
        _BOT_DB["post_drafts"],
        _BOT_DB["autopost_settings"],
        _BOT_DB["autopost_hashes"],
    )
except Exception as e:
    logger.error(f"Failed to init DBs: {e}")


def get_admin_list():
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, str):
        return [
            int(x)
            for x in raw_admins.replace(",", " ").split()
            if x.strip().lstrip("-").isdigit()
        ]
    elif isinstance(raw_admins, int):
        return [raw_admins]
    elif isinstance(raw_admins, list):
        return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]
    return []


def get_uid(message: Message) -> int:
    if getattr(message, "from_user", None):
        return message.from_user.id
    if getattr(message, "sender_chat", None):
        return message.sender_chat.id
    return 0


admin_filter = filters.create(lambda _, __, msg: bool(get_uid(msg) in get_admin_list()))
_WAITING_REQUESTS = {}


@Client.on_message(admin_filter, group=-90)
async def custom_listener(client: Client, message: Message):
    key = (message.chat.id, get_uid(message))
    if key in _WAITING_REQUESTS:
        future = _WAITING_REQUESTS.pop(key)
        if not future.done():
            future.set_result(message)
        # 🚨 BUG FIX: Pyrogram v2 requires raising StopPropagation instead of calling it
        raise StopPropagation


async def native_listen(
    client: Client, chat_id: int, user_id: int, timeout: int = 300
) -> Message:
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _WAITING_REQUESTS[(chat_id, user_id)] = future
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        _WAITING_REQUESTS.pop((chat_id, user_id), None)
        raise asyncio.TimeoutError


DEFAULT_WATERMARK = "<b>Jᴏɪɴ: @Sandalwood_Kannada_Moviesz</b>"
LANGUAGES_FORMAT = "<b>🔊 : {langs}</b>"
RESOLUTIONS_FORMAT = "<b>🖥️ : {resolutions}</b>"
GENRES_FORMAT = "<b>🎥 : {genres}</b>"
OTT_FORMAT = "<b>📺 : #{otts}</b>"
TEMPLATES = {
    "clean_grid": "✅ <b>{title} {year}</b>\n\n<blockquote>{LANGUAGES}\n{RESOLUTIONS}\n{GENRES}\n{OTT_PLATFORMS}\n<b>📟 : Available In Files.</b>\n\n<b>=========================</b></blockquote>",
    "divider_list": "🎬 <b>{title} {year}</b>\n━━━━━━━━━━━━━━━━━━\n<blockquote>{LANGUAGES}\n{RESOLUTIONS}\n{OTT_PLATFORMS}</blockquote>",
}

LANGUAGES = ["Kannada", "English", "Gujarati", "Hindi", "Bengali", "Malayalam", "Marathi", "Punjabi", "Tamil", "Telugu", "Urdu"]
RESOLUTIONS = ["480p", "720p", "1080p", "1440p", "2160p", "4k", "BluRay", "BDRip", "WEB-DL", "HDRip", "WEBRip", "HDTC", "HEVC"]
GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Drama", "Fantasy", "Horror", "Mystery", "Romance", "Sci-Fi", "Thriller"]
OTT_PLATFORMS = ["Aha", "JioCinema", "SonyLIV", "Voot", "Zee5", "AmazonPrime", "Netflix", "Hulu", "Disney+"]

try:
    from pyrogram.enums import ButtonStyle

    BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER, BTN_SECONDARY = (
        getattr(ButtonStyle, "PRIMARY", 1),
        getattr(ButtonStyle, "SUCCESS", 3),
        getattr(ButtonStyle, "DANGER", 4),
        getattr(ButtonStyle, "SECONDARY", 2),
    )
except ImportError:
    BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER, BTN_SECONDARY = 1, 3, 4, 2


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


async def save_session_to_db(user_id: int, session: dict):
    if drafts_db is not None:
        try:
            draft = session.copy()
            clean_buttons = []
            for row in draft.get("buttons", []):
                clean_row = []
                for b in row:
                    b_dict = {
                        "text": (
                            getattr(b, "text", "")
                            if not isinstance(b, dict)
                            else b.get("text", "")
                        )
                    }
                    b_url = (
                        getattr(b, "url", None)
                        if not isinstance(b, dict)
                        else b.get("url")
                    )
                    if b_url:
                        b_dict["url"] = b_url
                    b_cb = (
                        getattr(b, "callback_data", None)
                        if not isinstance(b, dict)
                        else b.get("callback_data")
                    )
                    if b_cb:
                        b_dict["callback_data"] = b_cb
                    style = (
                        getattr(b, "style", None)
                        if not isinstance(b, dict)
                        else b.get("style")
                    )
                    if style is not None:
                        b_dict["style"] = (
                            style.value if hasattr(style, "value") else int(style)
                        )
                    clean_row.append(b_dict)
                clean_buttons.append(clean_row)
            draft["buttons"] = clean_buttons
            await drafts_db.update_one({"_id": user_id}, {"$set": draft}, upsert=True)
        except Exception as e:
            logger.error(f"Draft Save Error: {e}")


async def get_hydra_status():
    if drafts_db is not None:
        doc = await drafts_db.find_one({"_id": "hydra_config"})
        return doc.get("enabled", False) if doc else False
    return False


async def set_hydra_status(status: bool):
    if drafts_db is not None:
        await drafts_db.update_one(
            {"_id": "hydra_config"}, {"$set": {"enabled": status}}, upsert=True
        )


async def get_ap_settings():
    if ap_db is None:
        return {}
    settings = await ap_db.find_one({"id": "ap_config"})
    return (
        settings
        if settings
        else {
            "enabled": False,
            "template": TEMPLATES["clean_grid"],
            "image_mode": "preview",
            "muc_list": [],
            "apc_list": [],
        }
    )


async def save_ap_settings(key, value):
    if ap_db is not None:
        await ap_db.update_one({"id": "ap_config"}, {"$set": {key: value}}, upsert=True)


def safe_ints(lst):
    return [int(str(x).strip()) for x in lst if str(x).strip().lstrip("-").isdigit()]


def get_html_text(message: Message):
    if message.reply_to_message and message.reply_to_message.text:
        return message.reply_to_message.text.html
    elif len(message.command) > 1:
        return re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s+", "", message.text.html, count=1)
    return None


@Client.on_message(filters.command("admin") & admin_filter, group=-4)
async def master_admin_dashboard(client: Client, message: Message):
    text = (
        "👑 **Enterprise Admin Control Panel**\n\nManage your automated systems below:"
    )
    buttons = [
        [
            create_btn("🐉 Hydra Anti-Ban", callback_data="admin_db:hydra"),
            create_btn("🧬 Auto-Post", callback_data="admin_db:autopost"),
        ],
        [
            create_btn("📊 Live Analytics", callback_data="admin_db:analytics"),
            create_btn("🚀 Optimize DB", callback_data="admin_db:optimize"),
        ],
        [
            create_btn("📱 Connected Channels", callback_data="admin_db:channels"),
            create_btn(
                "❌ Close Panel", callback_data="admin_db:close", style=BTN_DANGER
            ),
        ],
    ]
    await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^admin_db:") & admin_filter)
async def admin_db_cbs(client: Client, query: CallbackQuery):
    action = query.data.split(":")[1]
    if action == "hydra":
        status = not await get_hydra_status()
        await set_hydra_status(status)
        await query.answer(
            f"Hydra Engine is now {'ON' if status else 'OFF'}", show_alert=True
        )
    elif action == "autopost":
        await query.answer(
            "Use /autopost on or /autopost off to toggle.", show_alert=True
        )
    elif action == "analytics":
        try:
            await query.message.delete()
        except:
            pass
        await client.send_message(query.message.chat.id, "/analize")
    elif action == "optimize":
        try:
            await query.message.delete()
        except:
            pass
        await client.send_message(query.message.chat.id, "/optimize_db")
    elif action == "channels":
        try:
            await query.message.delete()
        except:
            pass
        await client.send_message(query.message.chat.id, "/channels")
    elif action == "close":
        try:
            await query.message.delete()
        except:
            pass


PENDING_AP = {}
POST_QUEUE = asyncio.Queue()
_WORKER_STARTED = False


@Client.on_message(filters.command("autopost") & admin_filter, group=-4)
async def toggle_autopost(client: Client, message: Message):
    if len(message.command) < 2:
        settings = await get_ap_settings()
        status = "🟢 ON" if settings.get("enabled") else "🔴 OFF"
        return await message.reply_text(
            f"**Auto-Post Status:** {status}\n\nUse `/autopost on` or `/autopost off` to toggle."
        )
    cmd = message.command[1].lower()
    if cmd == "on":
        await save_ap_settings("enabled", True)
        await message.reply_text("✅ **Auto-Post Engine ON!**")
    elif cmd == "off":
        await save_ap_settings("enabled", False)
        await message.reply_text("🔴 **Auto-Post Engine OFF!**")


@Client.on_message(
    filters.chat(info.FILE_STORE_CHANNEL)
    & (filters.document | filters.video | filters.audio)
)
async def auto_post_trigger(client: Client, message: Message):
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        asyncio.create_task(auto_post_worker(client))
        _WORKER_STARTED = True
    await POST_QUEUE.put(message)


async def auto_post_worker(client: Client):
    while True:
        try:
            message = await POST_QUEUE.get()
            await process_single_auto_post(client, message)
            POST_QUEUE.task_done()
        except Exception as e:
            logger.error(f"Queue Worker Error: {e}")
        await asyncio.sleep(0.5)


async def process_single_auto_post(client: Client, message: Message):
    try:
        settings = await get_ap_settings()
        if not settings.get("enabled", False):
            return
        media = message.document or message.video or message.audio
        if not media:
            return
        file_unique_id = getattr(media, "file_unique_id", None)
        if hash_db is not None and file_unique_id:
            if await hash_db.find_one({"file_unique_id": file_unique_id}):
                return
            await hash_db.update_one(
                {"file_unique_id": file_unique_id},
                {"$set": {"time": time.time()}},
                upsert=True,
            )

        info_apc = safe_ints(
            info.AUTOPOSTCHANNEL
            if isinstance(info.AUTOPOSTCHANNEL, list)
            else [info.AUTOPOSTCHANNEL]
        )
        db_apc = safe_ints(settings.get("apc_list", []))
        update_channels = list(set(info_apc + db_apc))
        if not update_channels:
            info_muc = safe_ints(
                info.MOVIE_UPDATE_CHANNEL
                if isinstance(info.MOVIE_UPDATE_CHANNEL, list)
                else [info.MOVIE_UPDATE_CHANNEL]
            )
            db_muc = safe_ints(settings.get("muc_list", []))
            update_channels = list(set(info_muc + db_muc))
        if not update_channels:
            return

        file_name = getattr(media, "file_name", "Unknown") or "Unknown"
        size_str = get_size(getattr(media, "file_size", 0))
        clean_name = re.sub(r"(?i)\[?@?sandalwood[^\]\s]*\]?", "", file_name)
        clean_name = re.sub(r"[_.-]", " ", clean_name)
        langs = list(
            set(
                [
                    l.title()
                    for l in re.findall(
                        r"(?i)\b(Kannada|English|Gujarati|Hindi|Bengali|Malayalam|Marathi|Punjabi|Tamil|Telugu|Urdu|Dual Audio|Multi Audio)\b",
                        clean_name,
                    )
                ]
            )
        )
        res = list(
            set(
                [
                    r.upper() if "p" not in r.lower() else r.lower()
                    for r in re.findall(
                        r"(?i)\b(WEB-DL|HDRip|HDTC|1080p|720p|480p|1440p|2160p|4k|BluRay|BDRip|WEBRip|HDTVRip|DVDRip|CAMRip|HEVC)\b",
                        clean_name,
                    )
                ]
            )
        )
        otts = list(
            set(
                [
                    o.title()
                    .replace("Appletv", "Apple TV")
                    .replace("Disney+", "Disney")
                    for o in re.findall(
                        r"(?i)\b(Netflix|Amazon Prime|Prime Video|Aha|Zee5|Hotstar|Disney\+?|JioCinema|SonyLIV|SunNXT|Voot|Hulu|HBO|Apple TV|AppleTV|Crunchyroll)\b",
                        clean_name,
                    )
                ]
            )
        )

        search_name = re.sub(
            r"(?i)\b(1080p|720p|480p|2160p|4k|WEB-DL|HDRip|HDTC|BDRip|BluRay|DVDRip|WEBRip|CAMRip|HEVC|mkv|mp4|avi|hindi|kannada|telugu|tamil|malayalam|english|dual audio|multi audio|dual|multi|subs|episodes|season\s*\d+|s\d+e\d+|Netflix|Prime|Aha|Zee5|Hotstar|JioCinema|SonyLIV|Voot)\b",
            "",
            clean_name,
        )
        search_name = re.sub(r"\b(19\d{2}|20\d{2})\b", "", search_name)
        search_name = re.sub(r"\s+", " ", search_name).strip()

        movie_details = await get_movie_detailsx(search_name)
        if not movie_details or not movie_details.get("title"):
            fallback_year = re.search(r"\b(19\d{2}|20\d{2})\b", file_name)
            movie_details = {
                "title": search_name.title() or "Unknown Movie",
                "year": fallback_year.group(1) if fallback_year else "N/A",
                "rating": "N/A",
                "genres": ["Drama", "Action"],
                "plot": "N/A",
                "poster_url": None,
            }

        title, year, rating, plot = (
            movie_details.get("title", search_name),
            movie_details.get("year", "N/A"),
            movie_details.get("rating", "N/A"),
            movie_details.get("plot", "N/A"),
        )
        poster = settings.get("image") or movie_details.get("poster_url")
        image_mode = settings.get("image_mode", "preview")
        val_title = settings.get("format_title", "<b>{title}</b>").replace(
            "{title}", html.escape(title)
        )
        val_year = (
            settings.get("format_year", "<b>{year}</b>").replace(
                "{year}", html.escape(str(year))
            )
            if str(year) != "N/A"
            else ""
        )
        val_langs = (
            settings.get("format_languages", "<b>{langs}</b>")
            .replace("{langs}", ", ".join(langs))
            .replace("{LANGUAGES}", ", ".join(langs))
            if langs
            else ""
        )
        val_res = (
            settings.get("format_resolutions", "<b>{resolutions}</b>")
            .replace("{resolutions}", ", ".join(res))
            .replace("{RESOLUTIONS}", ", ".join(res))
            if res
            else ""
        )
        val_gens = (
            settings.get("format_genres", "<b>{genres}</b>")
            .replace("{genres}", ", ".join(movie_details.get("genres", [])))
            .replace("{GENRES}", ", ".join(movie_details.get("genres", [])))
            if movie_details.get("genres")
            else ""
        )
        val_otts = (
            settings.get("format_otts", "<b>{otts}</b>")
            .replace("{otts}", ", ".join(otts))
            .replace("{OTT_PLATFORMS}", ", ".join(otts))
            if otts
            else ""
        )

        text = (
            settings.get("template", TEMPLATES["clean_grid"])
            .replace("{title}", val_title)
            .replace("{year}", val_year)
        )
        for tag, val in [
            ("{LANGUAGES}", val_langs),
            ("{RESOLUTIONS}", val_res),
            ("{GENRES}", val_gens),
            ("{OTT_PLATFORMS}", val_otts),
        ]:
            text = (
                text.replace(tag, val)
                if val
                else re.sub(rf"[^\n]*{re.escape(tag)}[^\n]*\n?", "", text)
            )
        if image_mode == "photo" and len(plot) > 250:
            plot = plot[:250] + "..."
        text = (
            text.replace("{size}", size_str)
            .replace("{rating}", html.escape(str(rating)))
            .replace("{plot}", html.escape(str(plot)))
            .replace("{file_name}", html.escape(file_name))
        )
        text += f"\n\n<b>Jᴏɪɴ: @Sandalwood_Kannada_Moviesz</b>"
        if poster and image_mode == "preview":
            text = f"{text}\n<a href='{poster}'>&#8205;</a>"

        bot_username = (await client.get_me()).username
        safe_query = re.sub(
            r"[^a-zA-Z0-9_-]", "_", f"{title} {year}" if str(year) != "N/A" else title
        ).strip("_")[:50]
        deep_link = (
            f"https://t.me/{bot_username}?start=hdra_{base64.urlsafe_b64encode(safe_query.encode('utf-8')).decode('utf-8').rstrip('=')}"
            if await get_hydra_status()
            else f"https://t.me/{bot_username}?start=search_{safe_query}"
        )

        btn_layout = []
        btn1, btn2, btn3 = (
            settings.get(
                "btn1",
                {"text": "Group 1 🎬", "url": "https://t.me/Sandalwood_Kannada_Group"},
            ),
            settings.get(
                "btn2", {"text": "Group 2 🎬", "url": "https://t.me/+GLsPkRgLGGszMzY1"}
            ),
            settings.get(
                "btn3",
                {
                    "text": settings.get("direct_button_text", "Direct Search 🔎"),
                    "url": "{deep_link}",
                },
            ),
        )
        if btn1 or btn2:
            row = []
            if btn1:
                row.append(
                    create_btn(
                        btn1["text"],
                        url=btn1["url"].replace("{deep_link}", deep_link),
                        style=BTN_PRIMARY,
                    )
                )
            if btn2:
                row.append(
                    create_btn(
                        btn2["text"],
                        url=btn2["url"].replace("{deep_link}", deep_link),
                        style=BTN_PRIMARY,
                    )
                )
            btn_layout.append(row)
        if btn3:
            btn_layout.append(
                [
                    create_btn(
                        btn3["text"],
                        url=btn3["url"].replace("{deep_link}", deep_link),
                        style=BTN_SUCCESS,
                    )
                ]
            )

        target_admin = (
            message.from_user.id
            if getattr(message, "from_user", None)
            and message.from_user.id in get_admin_list()
            else get_admin_list()[0]
        )
        pm_markup_layout = list(btn_layout)
        pm_markup_layout.extend(
            [
                [
                    create_btn(
                        "✅ Post to Channel",
                        callback_data="ap_post_1",
                        style=BTN_SUCCESS,
                    )
                ],
                [
                    create_btn(
                        "✏️ Edit Text", callback_data="ap_edit_1", style=BTN_PRIMARY
                    ),
                    create_btn(
                        "📸 Edit Image", callback_data="ap_editimg_1", style=BTN_PRIMARY
                    ),
                ],
                [
                    create_btn(
                        "❌ Cancel", callback_data="ap_cancel_1", style=BTN_DANGER
                    )
                ],
            ]
        )

        if poster and image_mode == "photo":
            ask_msg = await client.send_photo(
                chat_id=target_admin,
                photo=poster,
                caption=f"**🚨 AUTO-POST TRIGGERED 🚨**\n\n{text}",
                reply_markup=InlineKeyboardMarkup(pm_markup_layout),
            )
        else:
            ask_msg = await client.send_message(
                chat_id=target_admin,
                text=f"**🚨 AUTO-POST TRIGGERED 🚨**\n\n{text}",
                reply_markup=InlineKeyboardMarkup(pm_markup_layout),
            )
        msg_id_str = str(ask_msg.id)
        PENDING_AP[msg_id_str] = {
            "text": text,
            "buttons": btn_layout,
            "channels": update_channels,
            "sticker": settings.get("sticker"),
            "poster": poster,
            "image_mode": image_mode,
            "chat_id": target_admin,
            "message_id": ask_msg.id,
        }
        asyncio.create_task(expire_pending_post(client, msg_id_str, 86400))
    except Exception as e:
        logger.error(f"Process Auto-Post Failed: {e}")


async def expire_pending_post(client: Client, msg_id_str: str, delay: int = 86400):
    await asyncio.sleep(delay)
    payload = PENDING_AP.pop(msg_id_str, None)
    if payload and "chat_id" in payload and "message_id" in payload:
        try:
            await client.delete_messages(
                chat_id=payload["chat_id"], message_id=payload["message_id"]
            )
        except Exception:
            pass


async def extract_and_edit_live_post(client: Client, message: Message, edit_type: str):
    try:
        link, new_val = "", ""
        if (
            edit_type in ["image", "normalimage"]
            and getattr(message, "reply_to_message", None)
            and getattr(message.reply_to_message, "photo", None)
        ):
            if len(message.command) < 2:
                return await message.reply(
                    f"⚠️ Usage: Reply to photo with `/{message.command[0]} <link>`"
                )
            link, new_val = (
                message.command[1].strip(),
                message.reply_to_message.photo.file_id,
            )
        else:
            if len(message.command) < 2 or "|" not in message.text:
                return await message.reply(
                    f"⚠️ Usage: `/{message.command[0]} <link> | <new_value>`"
                )
            link, new_val = [
                x.strip() for x in message.text.split(None, 1)[1].split("|", 1)
            ]

        parts = link.split("/")
        chat_id = (
            int("-100" + parts[-2])
            if "t.me/c/" in link
            else ("@" + parts[-2] if not parts[-2].startswith("@") else parts[-2])
        )
        msg_id = int(parts[-1])
        target_msg = await client.get_messages(chat_id, msg_id)
        if not target_msg or target_msg.empty:
            return await message.reply("❌ Post not found. Check the link.")

        html_text = (
            target_msg.caption.html
            if target_msg.photo
            else (target_msg.text.html if target_msg.text else "")
        )
        reply_markup, new_photo = target_msg.reply_markup, None

        if edit_type == "title":
            html_text = re.sub(r"<b>(.*?)</b>", f"<b>{new_val}</b>", html_text, count=1)
        elif edit_type == "year":
            year_match = re.search(r"<b>.*? (\d{4})</b>", html_text)
            if year_match:
                html_text = html_text.replace(year_match.group(1), new_val)
        elif edit_type == "direct":
            if reply_markup and getattr(reply_markup, "inline_keyboard", None):
                new_kbd = []
                for row in reply_markup.inline_keyboard:
                    new_kbd.append(
                        [
                            (
                                create_btn(
                                    new_val, url=b.url, style=getattr(b, "style", None)
                                )
                                if b.url
                                and (
                                    "?start=search_" in b.url or "?start=hdra_" in b.url
                                )
                                else b
                            )
                            for b in row
                        ]
                    )
                reply_markup = InlineKeyboardMarkup(new_kbd)
        elif edit_type == "button":
            row_str, rest = new_val.split(" ", 1)
            row_idx = int(row_str) - 1
            b_text, b_url = [x.strip() for x in rest.split("|", 1)]
            if not b_url.startswith(("http://", "https://", "tg://")):
                b_url = "https://" + b_url
            if (
                reply_markup
                and getattr(reply_markup, "inline_keyboard", None)
                and len(reply_markup.inline_keyboard) > row_idx
            ):
                kbd = list(reply_markup.inline_keyboard)
                kbd[row_idx] = [create_btn(b_text, url=b_url)]
                reply_markup = InlineKeyboardMarkup(kbd)
        elif edit_type == "buttoncolour":
            row_str, color_str = new_val.split(" ", 1)
            row_idx = int(row_str) - 1
            color_map = {"green": BTN_SUCCESS, "red": BTN_DANGER, "blue": BTN_PRIMARY}
            if (
                reply_markup
                and getattr(reply_markup, "inline_keyboard", None)
                and len(reply_markup.inline_keyboard) > row_idx
            ):
                kbd = list(reply_markup.inline_keyboard)
                kbd[row_idx] = [
                    create_btn(
                        b.text,
                        url=b.url,
                        callback_data=b.callback_data,
                        style=color_map.get(color_str.lower(), None),
                    )
                    for b in kbd[row_idx]
                ]
                reply_markup = InlineKeyboardMarkup(kbd)
        elif edit_type == "image":
            html_text = re.sub(
                r"<a href=['\"](https?://[^'\"]+)['\"]>&#8205;</a>",
                f"<a href='{new_val}'>&#8205;</a>",
                html_text,
            )
        elif edit_type == "normalimage":
            new_photo = new_val

        html_text = html_text + ("\u200b" * random.randint(1, 3))
        if new_photo:
            await client.edit_message_media(
                chat_id,
                msg_id,
                media=InputMediaPhoto(media=new_photo, caption=html_text),
                reply_markup=reply_markup,
            )
        else:
            if target_msg.photo:
                await client.edit_message_caption(
                    chat_id, msg_id, caption=html_text, reply_markup=reply_markup
                )
            else:
                await client.edit_message_text(
                    chat_id,
                    msg_id,
                    text=html_text,
                    reply_markup=reply_markup,
                    disable_web_page_preview=False,
                )
        await message.reply("✅ Live channel post updated successfully!")
    except Exception as e:
        await message.reply(f"❌ Error during live edit: {e}")
@Client.on_message(filters.command(["editposttitle"]) & admin_filter, group=-4)
async def live_edit_title(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "title")

@Client.on_message(filters.command(["editpostyear"]) & admin_filter, group=-4)
async def live_edit_year(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "year")

@Client.on_message(filters.command(["editpostdirect"]) & admin_filter, group=-4)
async def live_edit_direct(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "direct")

@Client.on_message(filters.command(["editpostbutton"]) & admin_filter, group=-4)
async def live_edit_button(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "button")

@Client.on_message(filters.command(["editpostbuttoncolour"]) & admin_filter, group=-4)
async def live_edit_btn_color(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "buttoncolour")

@Client.on_message(filters.command(["editpostimage"]) & admin_filter, group=-4)
async def live_edit_image(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "image")

@Client.on_message(filters.command(["editpostnormalimage"]) & admin_filter, group=-4)
async def live_edit_nimg(client: Client, message: Message):
    await extract_and_edit_live_post(client, message, "normalimage")


post_sessions, TEMP_SEARCH = {}, {}


def smart_patch_imported_caption(session, old_val, new_val, is_title=False):
    if session.get("is_manual_caption") and session.get("caption"):
        if is_title:
            session["caption"] = re.sub(
                r"<b>(.*?)</b>", f"<b>{new_val}</b>", session["caption"], count=1
            )
        elif old_val and new_val:
            session["caption"] = session["caption"].replace(str(old_val), str(new_val))


@Client.on_message(
    filters.command(["edittitle", "edittittle"]) & admin_filter, group=-4
)
async def cmd_edit_title(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply(
            "⚠️ No active post session. Use `/post` or `/editpost` first."
        )
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/edittitle New Movie Title`")
    new_title = message.text.split(None, 1)[1]
    old_title = post_sessions[uid]["movie_details"].get("title", "")
    post_sessions[uid]["movie_details"]["title"] = new_title
    smart_patch_imported_caption(
        post_sessions[uid], old_title, new_title, is_title=True
    )
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply(
        "✅ Updated in draft! Click '✅ Post' to apply to channel."
        if post_sessions[uid].get("edit_target")
        else "✅ Title updated!"
    )


@Client.on_message(filters.command(["edityear"]) & admin_filter, group=-4)
async def cmd_edit_year(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/edityear 2024`")
    new_year = message.text.split(None, 1)[1]
    old_year = post_sessions[uid]["movie_details"].get("year", "")
    post_sessions[uid]["movie_details"]["year"] = new_year
    smart_patch_imported_caption(post_sessions[uid], old_year, new_year)
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply("✅ Year updated!")


@Client.on_message(filters.command(["editlangs"]) & admin_filter, group=-4)
async def cmd_edit_langs(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/editlangs Hindi, English`")
    post_sessions[uid]["custom_languages"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",")
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply("✅ Languages updated!")


@Client.on_message(filters.command(["editresolutions"]) & admin_filter, group=-4)
async def cmd_edit_res(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/editresolutions 1080p, 720p`")
    post_sessions[uid]["custom_resolutions"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",")
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply("✅ Resolutions updated!")


@Client.on_message(filters.command(["editgenres"]) & admin_filter, group=-4)
async def cmd_edit_gen(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/editgenres Action, Drama`")
    post_sessions[uid]["custom_genres"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",")
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply("✅ Genres updated!")


@Client.on_message(filters.command(["editotts"]) & admin_filter, group=-4)
async def cmd_edit_ott(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/editotts Netflix, Prime`")
    post_sessions[uid]["custom_otts"] = [
        x.strip() for x in message.text.split(None, 1)[1].split(",")
    ]
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply("✅ OTT Platforms updated!")


@Client.on_message(filters.command(["editbutton"]) & admin_filter, group=-4)
async def cmd_edit_post_button(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2 or "|" not in message.text:
        return await message.reply(
            "⚠️ Usage: `/editbutton 1 Download Now | https://url.com`"
        )

    try:
        args = message.text.split(None, 1)[1]
        row_num_str, rest = args.split(" ", 1)
        row_idx = int(row_num_str) - 1
        btn_text, btn_url = [x.strip() for x in rest.split("|", 1)]
        if not btn_url.startswith(("http://", "https://", "tg://")):
            btn_url = "https://" + btn_url
        while len(post_sessions[uid]["buttons"]) <= row_idx:
            post_sessions[uid]["buttons"].append([])
        post_sessions[uid]["buttons"][row_idx] = [{"text": btn_text, "url": btn_url}]
        await save_session_to_db(uid, post_sessions[uid])
        await update_post_preview(client, uid, message.chat.id, force_resend=False)
        await message.reply(
            f"✅ Button row {row_idx + 1} updated! Click '✅ Post' to apply."
            if post_sessions[uid].get("edit_target")
            else f"✅ Button row {row_idx + 1} updated!"
        )
    except Exception as e:
        await message.reply(f"❌ Error updating button: {e}")


@Client.on_message(filters.command(["editbuttoncolour"]) & admin_filter, group=-4)
async def cmd_edit_btn_color(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 3:
        return await message.reply("⚠️ Usage: `/editbuttoncolour 1 green`")
    try:
        row_idx = int(message.command[1]) - 1
        color_str = message.command[2].lower()
        color_map = {"green": BTN_SUCCESS, "red": BTN_DANGER, "blue": BTN_PRIMARY}
        if color_str not in color_map:
            return await message.reply("❌ Invalid color. Use green, red, or blue.")
        if len(post_sessions[uid]["buttons"]) > row_idx:
            post_sessions[uid]["buttons"][row_idx] = [
                {
                    "text": btn.get("text"),
                    "url": btn.get("url"),
                    "callback_data": btn.get("callback_data"),
                    "style": color_map[color_str],
                }
                for btn in post_sessions[uid]["buttons"][row_idx]
            ]
            await save_session_to_db(uid, post_sessions[uid])
            await update_post_preview(client, uid, message.chat.id, force_resend=False)
            await message.reply(
                f"✅ Button row {row_idx + 1} color changed to {color_str}!"
            )
        else:
            await message.reply("❌ That button row doesn't exist.")
    except Exception as e:
        await message.reply(f"❌ Error updating button color: {e}")


@Client.on_message(filters.command(["editdirect"]) & admin_filter, group=-4)
async def cmd_edit_post_direct(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if len(message.command) < 2:
        return await message.reply("⚠️ Usage: `/editdirect Download Now 📥`")
    post_sessions[uid]["custom_direct_btn_text"] = message.text.split(None, 1)[
        1
    ].strip()
    await handle_add_get_files(client, post_sessions[uid], update_text_only=True)
    await save_session_to_db(uid, post_sessions[uid])
    await update_post_preview(client, uid, message.chat.id, force_resend=False)
    await message.reply("✅ Direct Search button text updated!")


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
            return None, "❌ Failed to download the image."
        url = await asyncio.to_thread(_upload_sync, file_io.getvalue())
        if not url:
            return None, "❌ All upload servers failed."
        return url, None
    except Exception as e:
        return None, f"❌ Internal Error: {e}"


@Client.on_message(
    filters.command(["editimage", "editipostmage"]) & admin_filter, group=-4
)
async def cmd_edit_img(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if getattr(message, "reply_to_message", None) and getattr(
        message.reply_to_message, "photo", None
    ):
        status_msg = await message.reply_text(
            "⏳ Uploading image (Testing 10 cloud hosters)..."
        )
        url, err = await upload_image_safely(client, message.reply_to_message)
        if url:
            post_sessions[uid]["custom_poster"] = url
            if not post_sessions[uid].get("edit_target"):
                post_sessions[uid]["photo_mode"] = False
            await status_msg.edit_text("✅ Poster updated successfully!")
        else:
            post_sessions[uid]["custom_poster"] = message.reply_to_message.photo.file_id
            post_sessions[uid]["photo_mode"] = True
            await status_msg.edit_text(
                "⚠️ Cloud blocked. Switched to 'Normal Image' Mode!"
            )
    elif len(message.command) > 1:
        post_sessions[uid]["custom_poster"] = message.text.split(None, 1)[1].strip()
        await message.reply("✅ Poster URL updated!")
    else:
        return await message.reply(
            "⚠️ Usage: Reply to an image or provide an Image URL."
        )
    await update_post_preview(client, uid, message.chat.id, force_resend=True)


@Client.on_message(filters.command(["editnormalimage"]) & admin_filter, group=-4)
async def cmd_edit_nimg(client: Client, message: Message):
    uid = get_uid(message)
    if uid not in post_sessions:
        return await message.reply("⚠️ No active session.")
    if getattr(message, "reply_to_message", None) and getattr(
        message.reply_to_message, "photo", None
    ):
        post_sessions[uid]["custom_poster"] = message.reply_to_message.photo.file_id
        post_sessions[uid]["photo_mode"] = True
        await message.reply_text("✅ Normal Image set instantly!")
    elif len(message.command) > 1:
        post_sessions[uid]["custom_poster"] = message.text.split(None, 1)[1].strip()
        post_sessions[uid]["photo_mode"] = True
        await message.reply("✅ Normal Image URL set!")
    else:
        return await message.reply(
            "⚠️ Usage: Reply to an image or provide an Image URL."
        )
    await update_post_preview(client, uid, message.chat.id, force_resend=True)


@Client.on_message(filters.command("post") & admin_filter, group=-4)
async def post_command(client: Client, message: Message):
    if len(message.command) == 1:
        return await message.reply_text(
            "Please provide a movie name. Usage: `/post Vikram`"
        )
    movie_name = " ".join(message.command[1:])
    user_id = get_uid(message)
    status_msg = await message.reply_text("⏳ Searching TMDB...")
    tmdb_key = getattr(info, "TMDB_API_KEY", "")
    if tmdb_key:
        try:
            url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&query={urllib.parse.quote(movie_name)}"
            res = await asyncio.to_thread(requests.get, url)
            results = res.json().get("results", [])[:5]
            if results:
                buttons, TEMP_SEARCH[user_id] = [], []
                for idx, r in enumerate(results):
                    title = r.get("title") or r.get("name")
                    if not title:
                        continue
                    date = r.get("release_date") or r.get("first_air_date") or ""
                    year = date[:4] if date else ""
                    display = f"{title} ({year})" if year else title
                    TEMP_SEARCH[user_id].append(f"{title} {year}".strip())
                    buttons.append([create_btn(display, callback_data=f"p_init:{idx}")])
                if buttons:
                    buttons.append([create_btn("❌ Cancel", callback_data="p_cancel")])
                    return await status_msg.edit_text(
                        f"🔍 **Found results:**\nSelect the exact movie:",
                        reply_markup=InlineKeyboardMarkup(buttons),
                    )
        except Exception:
            pass
    await status_msg.delete()
    await start_post_session(client, message, user_id, movie_name)


@Client.on_callback_query(filters.regex(r"^p_init:|^p_cancel$") & admin_filter)
async def tmdb_selector_callback(client: Client, query: CallbackQuery):
    user_id = (
        get_uid(query.message)
        if getattr(query.message, "from_user", None)
        else query.from_user.id
    )
    if query.data == "p_cancel":
        TEMP_SEARCH.pop(user_id, None)
        return await query.message.edit_text("❌ Cancelled.")
    idx = int(query.data.split(":")[1])
    if user_id not in TEMP_SEARCH or len(TEMP_SEARCH[user_id]) <= idx:
        return await query.answer("Search session expired.", show_alert=True)
    selected_name = TEMP_SEARCH[user_id][idx]
    TEMP_SEARCH.pop(user_id, None)
    await query.message.delete()
    await start_post_session(client, query.message, user_id, selected_name)


async def start_post_session(
    client: Client, message: Message, user_id: int, movie_name: str
):
    try:
        status_msg = await client.send_message(
            message.chat.id, "⏳ Fetching movie details..."
        )
        movie_details = await get_movie_detailsx(movie_name)
        if not movie_details:
            return await status_msg.edit_text("❌ TMDB failed.")
        post_sessions[user_id] = {
            "movie_name": movie_name,
            "caption": None,
            "is_manual_caption": False,
            "buttons": [],
            "photo_mode": False,
            "use_landscape": bool(movie_details.get("backdrop_url")),
            "custom_languages": [],
            "custom_resolutions": [],
            "custom_genres": [],
            "custom_otts": [],
            "schedule": 0,
            "auto_delete": 0,
            "last_preview_message_id": status_msg.id,
            "original_message_id": (
                message.id if hasattr(message, "id") else status_msg.id
            ),
            "original_chat_id": message.chat.id,
            "custom_poster": None,
            "watermark": DEFAULT_WATERMARK,
            "lang_format": LANGUAGES_FORMAT,
            "ott_format": OTT_FORMAT,
            "gen_format": GENRES_FORMAT,
            "res_format": RESOLUTIONS_FORMAT,
            "active_template": "clean_grid",
            "movie_details": movie_details,
            "custom_direct_btn_text": "Direct Search 🔎",
        }
        asyncio.create_task(expire_post_session(client, user_id, 1800))
        if getattr(info, "USE_GETFILE_BUTTON_BY_DEFAULT", True):
            await handle_add_get_files(client, post_sessions[user_id])
        await save_session_to_db(user_id, post_sessions[user_id])
        await update_post_preview(client, user_id, message.chat.id, force_resend=True)
    except Exception as e:
        await client.send_message(message.chat.id, f"❌ **SESSION ERROR:**\n`{e}`")


@Client.on_message(filters.command("resumepost") & admin_filter, group=-4)
async def resume_post_cmd(client: Client, message: Message):
    if drafts_db is None:
        return await message.reply("❌ DB not configured.")
    user_id = get_uid(message)
    draft = await drafts_db.find_one({"_id": user_id})
    if not draft:
        return await message.reply("⚠️ No saved draft found.")

    if draft.get("last_preview_message_id"):
        try:
            await client.delete_messages(
                draft.get("original_chat_id"), draft.get("last_preview_message_id")
            )
        except Exception:
            pass
    draft["last_preview_message_id"] = None
    post_sessions[user_id] = draft
    asyncio.create_task(expire_post_session(client, user_id, 1800))
    await update_post_preview(client, user_id, message.chat.id, force_resend=True)
    await message.reply("✅ Draft successfully restored from Database!")


@Client.on_message(filters.command("editpost") & admin_filter, group=-4)
async def edit_post_cmd(client: Client, message: Message):
    try:
        if len(message.command) < 2:
            return await message.reply_text("❌ Please provide a post link.")
        link = message.command[1]
        parts = link.split("/")
        chat_id = (
            int("-100" + parts[-2])
            if "t.me/c/" in link
            else ("@" + parts[-2] if not parts[-2].startswith("@") else parts[-2])
        )
        msg_id = int(parts[-1])
        status_msg = await message.reply_text("⏳ Fetching post...")
        target_msg = await client.get_messages(chat_id, msg_id)

        poster_url, html_text, is_photo_mode = None, "", False
        if target_msg.photo:
            html_text = target_msg.caption.html if target_msg.caption else ""
            poster_url, is_photo_mode = target_msg.photo.file_id, True
        else:
            html_text = target_msg.text.html if target_msg.text else ""
            match = re.search(
                r"<a href=['\"](https?://[^'\"]+)['\"]>&#8205;</a>", html_text
            )
            if match:
                poster_url = match.group(1)
                html_text = re.sub(
                    r"<a href=['\"](https?://[^'\"]+)['\"]>&#8205;</a>", "", html_text
                ).strip()

        title_match = re.search(r"<b>(.*?)</b>", html_text.split("\n")[0])
        search_name = title_match.group(1).strip() if title_match else "Unknown Movie"
        buttons = [
            [
                {
                    "text": b.text,
                    "url": b.url,
                    "callback_data": b.callback_data,
                    "style": (
                        b.style.value
                        if hasattr(b.style, "value")
                        else getattr(b, "style", None)
                    ),
                }
                for b in row
            ]
            for row in (
                target_msg.reply_markup.inline_keyboard
                if target_msg.reply_markup
                else []
            )
        ]

        user_id = get_uid(message)
        post_sessions[user_id] = {
            "movie_name": search_name,
            "caption": html_text,
            "is_manual_caption": True,
            "buttons": buttons,
            "photo_mode": is_photo_mode,
            "use_landscape": False,
            "custom_languages": [],
            "custom_resolutions": [],
            "custom_genres": [],
            "custom_otts": [],
            "schedule": 0,
            "auto_delete": 0,
            "last_preview_message_id": status_msg.id,
            "original_message_id": message.id,
            "original_chat_id": message.chat.id,
            "custom_poster": poster_url,
            "watermark": "",
            "lang_format": LANGUAGES_FORMAT,
            "ott_format": OTT_FORMAT,
            "gen_format": GENRES_FORMAT,
            "res_format": RESOLUTIONS_FORMAT,
            "active_template": "clean_grid",
            "movie_details": {"title": search_name, "year": ""},
            "edit_target": {"chat_id": chat_id, "message_id": msg_id},
            "custom_direct_btn_text": "Direct Search 🔎",
        }
        await save_session_to_db(user_id, post_sessions[user_id])
        asyncio.create_task(expire_post_session(client, user_id, 1800))
        await update_post_preview(client, user_id, message.chat.id, force_resend=True)
    except Exception as e:
        await message.reply_text(f"❌ Error: {e}")


class SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


async def _build_final_post_content(session: dict, session_id: int):
    movie_details = session.get("movie_details", {})
    if session.get("is_manual_caption") and session.get("caption"):
        return (
            session["caption"],
            build_keyboard(session, session_id),
            (
                None
                if session.get("custom_poster") == "BLANK"
                else session.get("custom_poster")
            ),
        )

    template_str = TEMPLATES.get(
        session.get("active_template"), TEMPLATES["clean_grid"]
    )
    val_title, val_year, rating_str, plot_str = (
        html.escape(movie_details.get("title", "")),
        html.escape(str(movie_details.get("year", ""))),
        html.escape(str(movie_details.get("rating", "N/A"))),
        html.escape(str(movie_details.get("plot", "N/A"))),
    )

    c_langs, c_res, c_gen, c_ott = (
        session.get("custom_languages", []),
        session.get("custom_resolutions", []),
        session.get("custom_genres", []),
        session.get("custom_otts", []),
    )
    langs_str = (
        "" if c_langs == ["BLANK"] else (", ".join(c_langs) if c_langs else "N/A")
    )
    res_str = "" if c_res == ["BLANK"] else (", ".join(c_res) if c_res else "N/A")
    genres_str = "" if c_gen == ["BLANK"] else (", ".join(c_gen) if c_gen else "N/A")
    otts_str = "" if c_ott == ["BLANK"] else (", ".join(c_ott) if c_ott else "N/A")

    val_langs = (
        session.get("lang_format", LANGUAGES_FORMAT).format_map(
            SafeDict(langs=langs_str, LANGUAGES=langs_str)
        )
        if langs_str != "N/A"
        else ""
    )
    val_res = (
        session.get("res_format", RESOLUTIONS_FORMAT).format_map(
            SafeDict(resolutions=res_str, RESOLUTIONS=res_str)
        )
        if res_str != "N/A"
        else ""
    )
    val_gens = (
        session.get("gen_format", GENRES_FORMAT).format_map(
            SafeDict(genres=genres_str, GENRES=genres_str)
        )
        if genres_str != "N/A"
        else ""
    )
    val_otts = (
        session.get("ott_format", OTT_FORMAT).format_map(
            SafeDict(otts=otts_str, OTT_PLATFORMS=otts_str)
        )
        if otts_str != "N/A"
        else ""
    )

    def render_text(plot_input):
        text = template_str.replace("{title}", val_title).replace("{year}", val_year)
        for tag, val in [
            ("{LANGUAGES}", val_langs),
            ("{RESOLUTIONS}", val_res),
            ("{GENRES}", val_gens),
            ("{OTT_PLATFORMS}", val_otts),
            ("{langs}", val_langs),
            ("{resolutions}", val_res),
            ("{genres}", val_gens),
            ("{otts}", val_otts),
        ]:
            text = (
                text.replace(tag, val)
                if val
                else re.sub(rf"[^\n]*{re.escape(tag)}[^\n]*\n?", "", text)
            )
        text = (
            text.replace("{size}", "Available In Files.")
            .replace("{rating}", rating_str)
            .replace("{plot}", plot_input)
        )
        if session.get("watermark"):
            text += f"\n\n{session['watermark']}"
        return text

    final_text = render_text(plot_str)
    if session.get("photo_mode") and len(final_text) > 1024:
        excess = len(final_text) - 1000
        final_text = (
            render_text(plot_str[: -(excess + 5)] + "...")
            if len(plot_str) > excess + 10
            else final_text[:1024]
        )

    poster_to_use = (
        None
        if session.get("custom_poster") == "BLANK"
        else (
            session.get("custom_poster")
            or (
                movie_details.get("backdrop_url")
                if session.get("use_landscape")
                else movie_details.get("poster_url")
            )
        )
    )
    return final_text, build_keyboard(session, session_id), poster_to_use


async def update_post_preview(
    client: Client, session_id: int, chat_id: int, force_resend: bool = False
):
    session = post_sessions.get(session_id)
    if not session:
        return

    is_new = not session.get("last_preview_message_id")
    if is_new or force_resend:
        if not is_new:
            try:
                await client.delete_messages(
                    chat_id, session["last_preview_message_id"]
                )
            except Exception:
                pass
        try:
            status_msg = await client.send_message(
                chat_id, "<i>Generating preview...</i>"
            )
            session["last_preview_message_id"] = status_msg.id
        except Exception:
            return

    try:
        final_caption, keyboard, poster_to_use = await _build_final_post_content(
            session, session_id
        )
    except Exception:
        return

    try:
        final_caption = final_caption + ("\u200b" * random.randint(1, 3))
        is_normal_photo = (
            session.get("photo_mode")
            and poster_to_use
            and str(poster_to_use).upper() != "BLANK"
        )
        if is_normal_photo:
            if force_resend:
                old_msg_id = session.get("last_preview_message_id")
                sent_msg = await client.send_photo(
                    chat_id,
                    photo=poster_to_use,
                    caption=final_caption,
                    reply_markup=keyboard,
                )
                session["last_preview_message_id"] = sent_msg.id
                if old_msg_id:
                    try:
                        await client.delete_messages(chat_id, old_msg_id)
                    except Exception:
                        pass
            else:
                await client.edit_message_caption(
                    chat_id,
                    session["last_preview_message_id"],
                    caption=final_caption,
                    reply_markup=keyboard,
                )
        else:
            text_content = (
                f"{final_caption}\n<a href='{poster_to_use}'>&#8205;</a>"
                if poster_to_use and str(poster_to_use).upper() != "BLANK"
                else final_caption
            )
            if force_resend:
                old_msg_id = session.get("last_preview_message_id")
                sent_msg = await client.send_message(
                    chat_id,
                    text=text_content,
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                )
                session["last_preview_message_id"] = sent_msg.id
                if old_msg_id:
                    try:
                        await client.delete_messages(chat_id, old_msg_id)
                    except Exception:
                        pass
            else:
                await client.edit_message_text(
                    chat_id,
                    session["last_preview_message_id"],
                    text=text_content,
                    reply_markup=keyboard,
                    disable_web_page_preview=False,
                )
        await save_session_to_db(session_id, session)
    except Exception:
        pass


def build_keyboard(session: dict, session_id: int):
    rows = []
    if session.get("buttons"):
        rows.extend(
            [
                [
                    create_btn(**{k: v for k, v in b_dict.items() if v is not None})
                    for b_dict in row
                ]
                for row in session["buttons"]
            ]
        )
    mode_btn = []
    if not session.get("edit_target"):
        mode_btn.append(
            create_btn(
                f"Mode: {'Normal Img' if session.get('photo_mode') else 'Preview Img'}",
                callback_data=f"post:toggle_mode:{session_id}",
            )
        )
    mode_btn.append(
        create_btn(
            f"Poster: {'Landscape' if session['use_landscape'] else 'Portrait'}",
            callback_data=f"post:toggle_poster:{session_id}",
        )
    )

    sched_val, del_val = session.get("schedule", 0), session.get("auto_delete", 0)
    timing_row = [
        create_btn(
            f"⏰ Sched: {sched_val}s" if sched_val else "⏰ Sched: OFF",
            callback_data=f"post:set_schedule:{session_id}",
        ),
        create_btn(
            f"🗑️ Auto-Del: {del_val}s" if del_val else "🗑️ Auto-Del: OFF",
            callback_data=f"post:set_autodelete:{session_id}",
        ),
    ]

    rows.extend(
        [
            [
                create_btn(
                    "✏️ Buttons", callback_data=f"post:buttons_menu:{session_id}"
                ),
                create_btn(
                    "✏️ Caption", callback_data=f"post:edit_caption:{session_id}"
                ),
            ],
            [
                create_btn("🖼️ Poster", callback_data=f"post:set_poster:{session_id}"),
                create_btn(
                    "✨ Templates", callback_data=f"post:templates:{session_id}"
                ),
                create_btn(
                    "💧 Watermark", callback_data=f"post:set_watermark:{session_id}"
                ),
            ],
            [
                create_btn("🔊", callback_data=f"post:languages:{session_id}"),
                create_btn("🖥️", callback_data=f"post:resolutions:{session_id}"),
                create_btn("🎥", callback_data=f"post:genres:{session_id}"),
                create_btn("📺", callback_data=f"post:otts:{session_id}"),
            ],
            timing_row,
            mode_btn,
            [
                create_btn(
                    "✅ Post",
                    callback_data=f"post:finalize:{session_id}",
                    style=BTN_SUCCESS,
                ),
                create_btn(
                    "❌ Cancel",
                    callback_data=f"post:cancel:{session_id}",
                    style=BTN_DANGER,
                ),
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show_target_selection(query: CallbackQuery, session_id: int, page: int = 1):
    all_chats_dict, db_titles, settings = {}, {}, await get_ap_settings()
    raw_chats = await _db.get_all_chats()
    db_chats = (
        [c async for c in raw_chats]
        if hasattr(raw_chats, "__aiter__")
        else list(raw_chats)
    )
    for chat in db_chats:
        chat_id = chat.get("id") or chat.get("chat_id")
        if chat_id:
            db_titles[int(chat_id)] = (
                chat.get("title") or chat.get("name") or str(chat_id)
            )

    info_muc = safe_ints(getattr(info, "MOVIE_UPDATE_CHANNEL", []))
    info_apc = safe_ints(getattr(info, "AUTOPOSTCHANNEL", []))
    for ch in set(
        info_muc
        + settings.get("muc_list", [])
        + info_apc
        + settings.get("apc_list", [])
    ):
        if ch:
            if int(ch) not in db_titles:
                try:
                    db_titles[int(ch)] = (await query._client.get_chat(ch)).title
                except Exception:
                    pass
            all_chats_dict[int(ch)] = f"🌟 {db_titles.get(int(ch), str(ch))[:20]}"

    for chat in db_chats:
        chat_id = chat.get("id") or chat.get("chat_id")
        if chat_id and int(chat_id) not in all_chats_dict:
            all_chats_dict[int(chat_id)] = (
                f"📢 {db_titles.get(int(chat_id), str(chat_id))[:20]}"
            )

    chat_list = list(all_chats_dict.items())
    total_pages = max(1, math.ceil(len(chat_list) / 10))
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * 10

    buttons = [
        [
            create_btn(
                "📢 Post to All Configured Channels",
                callback_data=f"post:send_all:{session_id}",
                style=BTN_SUCCESS,
            )
        ]
    ]
    for chat_id, title in chat_list[start_idx : start_idx + 10]:
        buttons.append(
            [
                create_btn(
                    title,
                    callback_data=f"post:send_to:{session_id}:{chat_id}",
                    style=BTN_PRIMARY,
                )
            ]
        )

    nav_row = []
    if page > 1:
        nav_row.append(
            create_btn(
                "⬅️ Prev",
                callback_data=f"post:target_page:{session_id}:{page-1}",
                style=BTN_SECONDARY,
            )
        )
    if page < total_pages:
        nav_row.append(
            create_btn(
                "Next ➡️",
                callback_data=f"post:target_page:{session_id}:{page+1}",
                style=BTN_SECONDARY,
            )
        )

    if nav_row:
        buttons.append(nav_row)
    buttons.append(
        [
            create_btn(
                "🔙 Back to Editor",
                callback_data=f"post:back_editor:{session_id}",
                style=BTN_DANGER,
            )
        ]
    )

    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


@Client.on_callback_query(filters.regex(r"^post:") & admin_filter, group=-4)
async def post_callbacks(client: Client, query: CallbackQuery):
    try:
        data_parts = query.data.split(":")
        action, session_id, extra_data = (
            data_parts[1],
            int(data_parts[2]),
            data_parts[3:],
        )
        session = post_sessions.get(session_id)
        if not session:
            return await query.answer("Session expired.", show_alert=True)
        force_resend = False

        if action == "back":
            await query.answer()
        elif action in [
            "languages",
            "resolutions",
            "templates",
            "buttons_menu",
            "remove_buttons_menu",
            "genres",
            "otts",
        ]:
            await query.answer()
            if action == "languages":
                await show_selection_menu(query, session_id, "languages")
            elif action == "resolutions":
                await show_selection_menu(query, session_id, "resolutions")
            elif action == "genres":
                await show_selection_menu(query, session_id, "genres")
            elif action == "otts":
                await show_selection_menu(query, session_id, "otts")
            elif action == "templates":
                await handle_templates_menu(query, session_id)
            elif action == "buttons_menu":
                await handle_buttons_menu(query, session_id)
            elif action == "remove_buttons_menu":
                await handle_remove_buttons_menu(query, session_id)
            return
        elif action in ["select_lang", "select_res", "select_gen", "select_ott"]:
            await query.answer()
            item = extra_data[0]
            arr_map = {
                "select_lang": "custom_languages",
                "select_res": "custom_resolutions",
                "select_gen": "custom_genres",
                "select_ott": "custom_otts",
            }
            k = arr_map[action]
            if "BLANK" in session[k]:
                session[k] = []
            if item not in session[k]:
                session[k].append(item)
            else:
                session[k].remove(item)
            await save_session_to_db(session_id, session)
            if action == "select_lang":
                await show_selection_menu(query, session_id, "languages")
            elif action == "select_res":
                await show_selection_menu(query, session_id, "resolutions")
            elif action == "select_gen":
                await show_selection_menu(query, session_id, "genres")
            elif action == "select_ott":
                await show_selection_menu(query, session_id, "otts")
            return
        elif action == "format_lang":
            return await format_updater(
                client, query, session_id, "lang_format", "{langs}", LANGUAGES_FORMAT
            )
        elif action == "format_res":
            return await format_updater(
                client,
                query,
                session_id,
                "res_format",
                "{resolutions}",
                RESOLUTIONS_FORMAT,
            )
        elif action == "format_gen":
            return await format_updater(
                client, query, session_id, "gen_format", "{genres}", GENRES_FORMAT
            )
        elif action == "format_ott":
            return await format_updater(
                client, query, session_id, "ott_format", "{otts}", OTT_FORMAT
            )
        elif action == "select_template":
            (
                session["active_template"],
                session["is_manual_caption"],
                session["caption"],
            ) = (extra_data[0], False, None)
        elif action == "edit_caption":
            response = await get_user_input(
                client, query, session, "Send the new caption text."
            )
            if response and response.text:
                session["caption"], session["is_manual_caption"] = response.text, True
            force_resend = False
        elif action == "set_poster":
            response = await get_user_input(
                client,
                query,
                session,
                "📸 Send a photo or an image URL.\n*(Or type `/reset` or `blank`)*",
            )
            if response:
                if getattr(response, "photo", None):
                    status_msg = await query.message.reply_text(
                        "⏳ Uploading image (Testing 10 cloud hosters)..."
                    )
                    url, err = await upload_image_safely(client, response)
                    if url:
                        session["custom_poster"] = url
                        if not session.get("edit_target"):
                            session["photo_mode"] = False
                        try:
                            await status_msg.edit_text(
                                "✅ Poster updated successfully!"
                            )
                        except Exception:
                            pass
                    else:
                        session["custom_poster"], session["photo_mode"] = (
                            response.photo.file_id,
                            True,
                        )
                        try:
                            await status_msg.edit_text(
                                "⚠️ Cloud blocked. Switched to 'Normal Image' Mode!"
                            )
                        except Exception:
                            pass
                elif response.text:
                    text_input = response.text.strip().lower()
                    if text_input == "/reset":
                        session["custom_poster"] = None
                        if not session.get("edit_target"):
                            session["photo_mode"] = False
                    elif text_input == "blank":
                        session["custom_poster"] = "BLANK"
                    elif response.text.startswith("http"):
                        session["custom_poster"] = response.text.strip()
                        if not session.get("edit_target"):
                            session["photo_mode"] = False
            force_resend = True
        elif action == "set_watermark":
            response = await get_user_input(
                client,
                query,
                session,
                "Send watermark. Send `blank` or `/reset` to remove. Send `/default` for default.",
            )
            if response and response.text:
                text_input = response.text.strip().lower()
                if text_input in ["/reset", "blank"]:
                    session["watermark"] = ""
                elif text_input == "/default":
                    session["watermark"] = DEFAULT_WATERMARK
                else:
                    session["watermark"] = response.text
            force_resend = False
        elif action == "edit_buttons":
            response = await get_user_input(
                client,
                query,
                session,
                "Send the button layout. Format:\n`Button 1 - URL1 | Button 2 - URL2`",
            )
            if response and response.text:
                new_layout = []
                for row_str in response.text.strip().split("\n"):
                    row_btns = []
                    for btn_str in row_str.split("|"):
                        if " - " in btn_str:
                            text, url = btn_str.split(" - ", 1)
                            clean_url = url.strip()
                            if not clean_url.startswith(
                                ("http://", "https://", "tg://")
                            ):
                                clean_url = "https://" + clean_url
                            clean_text = (
                                text.replace("[", "")
                                .replace("]", "")
                                .replace("(", "")
                                .replace(")", "")
                                .strip()
                            )
                            row_btns.append({"text": clean_text, "url": clean_url})
                    if row_btns:
                        new_layout.append(row_btns)
                session["buttons"] = new_layout
                await save_session_to_db(session_id, session)
            force_resend = False
        elif action == "remove_button":
            try:
                row_i, col_i = int(extra_data[0]), int(extra_data[1])
                session["buttons"][row_i].pop(col_i)
                if not session["buttons"][row_i]:
                    session["buttons"].pop(row_i)
            except (IndexError, ValueError):
                pass
            return await handle_remove_buttons_menu(query, session_id)
        elif action == "add_get_files":
            added = await handle_add_get_files(client, session)
            await query.answer(
                (
                    "✅ 'Get Files' button added!"
                    if added
                    else "⚠️ Button already exists!"
                ),
                show_alert=not added,
            )
        elif action == "set_schedule":
            response = await get_user_input(
                client,
                query,
                session,
                "⏰ Send schedule delay in **seconds** (e.g., `3600` for 1 hour).\nOr type `off`.",
            )
            if response and response.text:
                text_val = response.text.strip().lower()
                if text_val == "off":
                    session["schedule"] = 0
                elif text_val.isdigit():
                    session["schedule"] = int(text_val)
                await save_session_to_db(session_id, session)
            force_resend = False
        elif action == "set_autodelete":
            response = await get_user_input(
                client,
                query,
                session,
                "🗑️ Send auto-delete time in **seconds** (e.g., `86400` for 24 hours).\nOr type `off`.",
            )
            if response and response.text:
                text_val = response.text.strip().lower()
                if text_val == "off":
                    session["auto_delete"] = 0
                elif text_val.isdigit():
                    session["auto_delete"] = int(text_val)
                await save_session_to_db(session_id, session)
            force_resend = False
        elif action == "toggle_poster":
            session["use_landscape"] = not session["use_landscape"]
            force_resend = True
        elif action == "toggle_mode":
            if not session.get("edit_target"):
                session["photo_mode"] = not session.get("photo_mode")
                force_resend = True
            else:
                return await query.answer(
                    "Cannot switch image modes on imported post!", show_alert=True
                )
        elif action == "cancel":
            if session := post_sessions.pop(session_id, None):
                if session.get("last_preview_message_id"):
                    try:
                        await client.delete_messages(
                            query.message.chat.id, session["last_preview_message_id"]
                        )
                    except Exception:
                        pass
            try:
                await query.message.reply_text("❌ Post creation cancelled.")
            except Exception:
                pass
            return
        elif action == "finalize":
            if session.get("edit_target"):
                return await execute_post(
                    client, query, session_id, session["edit_target"]["chat_id"]
                )
            else:
                return await show_target_selection(query, session_id, 1)
        elif action == "target_page":
            return await show_target_selection(query, session_id, int(extra_data[0]))
        elif action == "send_to":
            return await execute_post(client, query, session_id, int(extra_data[0]))
        elif action == "send_all":
            return await execute_post_all(client, query, session_id)
        elif action == "back_editor":
            return await update_post_preview(
                client, session_id, query.message.chat.id, force_resend=False
            )

        await save_session_to_db(session_id, session)
        await update_post_preview(
            client, session_id, query.message.chat.id, force_resend
        )
    except Exception as e:
        await query.message.reply_text(f"❌ **CALLBACK ERROR:**\n`{e}`")


async def show_selection_menu(query: CallbackQuery, session_id: int, menu_type: str):
    session = post_sessions[session_id]
    if menu_type == "languages":
        items, selected, action_prefix, format_action = (
            LANGUAGES,
            session["custom_languages"],
            "select_lang",
            "format_lang",
        )
    elif menu_type == "resolutions":
        items, selected, action_prefix, format_action = (
            RESOLUTIONS,
            session["custom_resolutions"],
            "select_res",
            "format_res",
        )
    elif menu_type == "genres":
        items, selected, action_prefix, format_action = (
            GENRES,
            session["custom_genres"],
            "select_gen",
            "format_gen",
        )
    elif menu_type == "otts":
        items, selected, action_prefix, format_action = (
            OTT_PLATFORMS,
            session["custom_otts"],
            "select_ott",
            "format_ott",
        )
    else:
        return

    buttons = [
        create_btn(
            f"✅ {i}" if i in selected else i,
            callback_data=f"post:{action_prefix}:{session_id}:{i}",
        )
        for i in items
    ]
    keyboard = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    keyboard.append(
        [
            create_btn(
                "⚙ Change Format", callback_data=f"post:{format_action}:{session_id}"
            )
        ]
    )
    keyboard.append([create_btn("✅ Done", callback_data=f"post:back:{session_id}")])
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(keyboard))
    except MessageNotModified:
        pass


async def handle_templates_menu(query, session_id: int):
    session = post_sessions[session_id]
    buttons = [
        [
            create_btn(
                f"✅ {name}" if session.get("active_template") == name else name,
                callback_data=f"post:select_template:{session_id}:{name}",
            )
        ]
        for name in TEMPLATES
    ]
    buttons.append([create_btn("Back", callback_data=f"post:back:{session_id}")])
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


async def handle_buttons_menu(query, session_id):
    buttons = [
        [
            create_btn(
                "➕ Add/Edit Layout", callback_data=f"post:edit_buttons:{session_id}"
            )
        ],
        [
            create_btn(
                "📥 Add 'Get Files' Button",
                callback_data=f"post:add_get_files:{session_id}",
            )
        ],
        [
            create_btn(
                "🗑️ Remove a Button",
                callback_data=f"post:remove_buttons_menu:{session_id}",
            )
        ],
        [create_btn("Back", callback_data=f"post:back:{session_id}")],
    ]
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


async def handle_remove_buttons_menu(query, session_id: int):
    session = post_sessions[session_id]
    buttons = [
        [
            create_btn(
                f"❌ {btn.get('text', 'Button')}",
                callback_data=f"post:remove_button:{session_id}:{i}:{j}",
            )
        ]
        for i, row in enumerate(session["buttons"])
        for j, btn in enumerate(row)
    ]
    if not buttons:
        buttons.append([create_btn("No buttons to remove", callback_data="noop")])
    buttons.append([create_btn("Back", callback_data=f"post:back:{session_id}")])
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(buttons))
    except MessageNotModified:
        pass


async def get_user_input(client, query, session, prompt_text):
    try:
        ask_msg = await query.message.reply_text(
            prompt_text, reply_to_message_id=session.get("original_message_id")
        )
    except Exception:
        ask_msg = await query.message.reply_text(prompt_text)
    try:
        uid = (
            get_uid(query.message)
            if getattr(query.message, "from_user", None)
            else query.from_user.id
        )
        response = await native_listen(client, query.message.chat.id, uid, 120)
        try:
            await ask_msg.delete()
        except Exception:
            pass
        if response:
            try:
                await response.delete()
            except Exception:
                pass
            return response
    except asyncio.TimeoutError:
        pass
    return None


async def format_updater(
    client: Client,
    query: CallbackQuery,
    session_id: int,
    format_key: str,
    tag: str,
    default_format: str,
):
    session = post_sessions[session_id]
    response = await get_user_input(
        client,
        query,
        session,
        f"Send format. Must include `{tag}`. Send `/reset` for default.",
    )
    if response and response.text:
        if response.text == "/reset":
            session[format_key] = default_format
        elif tag not in response.text:
            try:
                await query.message.reply_text(f"⚠️ Must contain `{tag}`")
            except Exception:
                pass
        else:
            session[format_key] = response.text
    return await update_post_preview(
        client, session_id, query.message.chat.id, force_resend=False
    )


async def handle_add_get_files(
    client: Client, session: dict, update_text_only=False
) -> bool:
    movie_details = session["movie_details"]
    title = (
        str(movie_details.get("title", "movie"))
        .replace("(", "")
        .replace(")", "")
        .replace("[", "")
        .replace("]", "")
    )
    year = (
        str(movie_details.get("year", ""))
        .replace("(", "")
        .replace(")", "")
        .replace("[", "")
        .replace("]", "")
    )
    query_str = f"{title} {year}".strip()

    bot_username = temp.U_NAME or (await client.get_me()).username
    is_hydra = await get_hydra_status()

    if is_hydra:
        encoded = (
            base64.urlsafe_b64encode(query_str.encode("utf-8"))
            .decode("utf-8")
            .rstrip("=")
        )
        url = f"https://t.me/{bot_username}?start=hdra_{encoded}"
    else:
        safe_query = re.sub(r"[^a-zA-Z0-9_-]", "_", query_str).strip("_")[:50]
        url = f"https://t.me/{bot_username}?start=search_{safe_query}"

    btn_text = session.get("custom_direct_btn_text", "Direct Search 🔎")

    if update_text_only:
        for row in session["buttons"]:
            for btn in row:
                if btn.get("url") and (
                    "?start=search_" in btn.get("url")
                    or "?start=hdra_" in btn.get("url")
                ):
                    btn["text"], btn["url"] = btn_text, url
        return True

    for row in session["buttons"]:
        for btn in row:
            if btn.get("url") == url:
                return False

    session["buttons"].append(
        [
            {
                "text": "Group 1 🎬",
                "url": "https://t.me/Sandalwood_Kannada_Group",
                "style": BTN_PRIMARY,
            },
            {
                "text": "Group 2 🎬",
                "url": "https://t.me/+GLsPkRgLGGszMzY1",
                "style": BTN_PRIMARY,
            },
        ]
    )
    session["buttons"].append([{"text": btn_text, "url": url, "style": BTN_SUCCESS}])
    return True


def get_final_keyboard(session: dict):
    if not session.get("buttons"):
        return None
    return InlineKeyboardMarkup(
        [
            [
                create_btn(**{k: v for k, v in b_dict.items() if v is not None})
                for b_dict in row
            ]
            for row in session["buttons"]
        ]
    )


async def delayed_post_worker(
    client: Client, session: dict, targets: list, edit_target=None
):
    delay, auto_del = session.get("schedule", 0), session.get("auto_delete", 0)
    if delay > 0:
        await asyncio.sleep(delay)

    final_caption, _, poster_to_use = await _build_final_post_content(session, 0)
    final_keyboard = get_final_keyboard(session)
    is_normal_photo = (
        session.get("photo_mode")
        and poster_to_use
        and str(poster_to_use).upper() != "BLANK"
    )

    if edit_target:
        try:
            if is_normal_photo:
                await client.edit_message_media(
                    edit_target["chat_id"],
                    edit_target["message_id"],
                    media=InputMediaPhoto(media=poster_to_use, caption=final_caption),
                    reply_markup=final_keyboard,
                )
            else:
                text_content = (
                    f"{final_caption}\n<a href='{poster_to_use}'>&#8205;</a>"
                    if poster_to_use and str(poster_to_use).upper() != "BLANK"
                    else final_caption
                )
                await client.edit_message_text(
                    edit_target["chat_id"],
                    edit_target["message_id"],
                    text=text_content,
                    reply_markup=final_keyboard,
                    disable_web_page_preview=False,
                )
            if auto_del > 0:
                asyncio.create_task(
                    delete_after_delay(
                        client,
                        edit_target["chat_id"],
                        edit_target["message_id"],
                        auto_del,
                    )
                )
        except Exception:
            pass
        return

    for target in targets:
        try:
            sent_msg = None
            if is_normal_photo:
                sent_msg = await client.send_photo(
                    chat_id=target,
                    photo=poster_to_use,
                    caption=final_caption,
                    reply_markup=final_keyboard,
                )
            else:
                text_content = (
                    f"{final_caption}\n<a href='{poster_to_use}'>&#8205;</a>"
                    if poster_to_use and str(poster_to_use).upper() != "BLANK"
                    else final_caption
                )
                sent_msg = await client.send_message(
                    chat_id=target,
                    text=text_content,
                    reply_markup=final_keyboard,
                    disable_web_page_preview=False,
                )
            if sent_msg and auto_del > 0:
                asyncio.create_task(
                    delete_after_delay(client, target, sent_msg.id, auto_del)
                )
            await asyncio.sleep(0.5)
        except Exception:
            pass


async def delete_after_delay(client: Client, chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, message_id)
    except Exception:
        pass


async def execute_post(
    client: Client, query: CallbackQuery, session_id: int, target_chat_id: int
):
    session = post_sessions.pop(session_id, None)
    if not session:
        return
    try:
        await client.delete_messages(
            query.message.chat.id, session["last_preview_message_id"]
        )
    except Exception:
        pass

    delay = session.get("schedule", 0)
    if delay > 0:
        try:
            await query.message.reply_text(
                f"✅ **Post Scheduled!** It will automatically execute after `{delay}` seconds."
            )
        except Exception:
            pass
        asyncio.create_task(
            delayed_post_worker(
                client, session, [target_chat_id], session.get("edit_target")
            )
        )
    else:
        try:
            status_msg = await query.message.reply_text(
                "<i>Finalizing and posting...</i>"
            )
        except Exception:
            status_msg = None

        await delayed_post_worker(
            client, session, [target_chat_id], session.get("edit_target")
        )
        if status_msg:
            try:
                await status_msg.edit("✅ Post completed successfully!")
            except Exception:
                pass


async def execute_post_all(client: Client, query: CallbackQuery, session_id: int):
    session = post_sessions.pop(session_id, None)
    if not session:
        return
    try:
        await client.delete_messages(
            query.message.chat.id, session["last_preview_message_id"]
        )
    except Exception:
        pass

    settings = await get_ap_settings()
    info_muc = safe_ints(getattr(info, "MOVIE_UPDATE_CHANNEL", []))
    db_muc = safe_ints(settings.get("muc_list", []))
    info_apc = safe_ints(getattr(info, "AUTOPOSTCHANNEL", []))
    db_apc = safe_ints(settings.get("apc_list", []))
    all_targets = list(set(info_muc + db_muc + info_apc + db_apc))

    if not all_targets:
        try:
            return await query.message.reply_text("❌ No channels configured.")
        except Exception:
            return

    delay = session.get("schedule", 0)
    if delay > 0:
        try:
            await query.message.reply_text(
                f"✅ **Broadcast Scheduled!** Will post to `{len(all_targets)}` channels after `{delay}` seconds."
            )
        except Exception:
            pass
        asyncio.create_task(delayed_post_worker(client, session, all_targets))
    else:
        try:
            status_msg = await query.message.reply_text(
                f"<i>📢 Broadcasting to {len(all_targets)} channels...</i>"
            )
        except Exception:
            status_msg = None

        await delayed_post_worker(client, session, all_targets)
        if status_msg:
            try:
                await status_msg.edit(f"✅ **Broadcast Complete!**")
            except Exception:
                pass
