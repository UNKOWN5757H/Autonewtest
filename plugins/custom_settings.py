import asyncio
import base64
import json
import os
import re
import time
from collections import defaultdict
from logging import ERROR, getLogger
from typing import Dict, Tuple

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChannelInvalid,
    ChatAdminRequired,
    FloodWait,
    MessageDeleteForbidden,
    MessageNotModified,
    UserAdminInvalid,
    UsernameInvalid,
    UsernameNotModified,
)
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid as ChannelInvalid_req,
)
from pyrogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from database.ia_filterdb import Media as _Media
from database.ia_filterdb import save_batch, unpack_new_file_id
from database.plugin_dbs import plugin_db as _plugin_db
from utils import admin_filter, get_admin_list, get_uid, temp

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚙️ CENTRALIZED MONGODB SETUP
# ============================================================
_DB_CLIENT = _BOT_DB = None
b_settings_db = g_welcome_db = _caption_db = _custom_db = None
resume_db = cw_db = idx_db = fa_config_db = fa_users_db = _ui_settings = (
    search_stats_col
) = None

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    b_settings_db = _BOT_DB["global_bot_settings"]
    g_welcome_db = _BOT_DB["group_welcome_settings"]
    _caption_db = _BOT_DB["custom_captions"]
    _custom_db = _BOT_DB["custom_captions"]  # Alias for compatibility
    pm_db = _BOT_DB["pm_settings"]
    resume_db = _BOT_DB["index_resume"]
    cw_db = _BOT_DB["clean_words"]
    idx_db = _BOT_DB["index_settings"]
    fa_config_db = _BOT_DB["forceadd_config"]
    fa_users_db = _BOT_DB["forceadd_users"]
    _ui_settings = _BOT_DB["ui_config"]
    search_stats_col = _BOT_DB["search_analytics"]
except Exception as e:
    logger.error(f"Failed to init custom settings DB: {e}")

# 🟢 RESTORED BUTTON COLORS
try:
    from pyrogram.enums import ButtonStyle

    BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", 1)
    BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", 3)
    BTN_DANGER = getattr(ButtonStyle, "DANGER", 4)
    BTN_SECONDARY = getattr(ButtonStyle, "SECONDARY", 2)
except ImportError:
    BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER, BTN_SECONDARY = 1, 3, 4, 2


# ============================================================
# 🛠️ GENERAL DATABASE GETTERS & SETTERS
# ============================================================
async def get_bot_settings():
    doc = (
        await b_settings_db.find_one({"_id": "bot_config"})
        if b_settings_db is not None
        else None
    )
    return doc or {}


async def update_bot_settings(key, value):
    if b_settings_db is not None:
        await b_settings_db.update_one(
            {"_id": "bot_config"}, {"$set": {key: value}}, upsert=True
        )


async def get_group_welcome(chat_id):
    doc = (
        await g_welcome_db.find_one({"_id": chat_id})
        if g_welcome_db is not None
        else None
    )
    return doc or {}


async def update_group_welcome(chat_id, key, value):
    if g_welcome_db is not None:
        await g_welcome_db.update_one(
            {"_id": chat_id}, {"$set": {key: value}}, upsert=True
        )


async def save_custom_data(key: str, value: str):
    if _caption_db is not None:
        await _caption_db.update_one(
            {"_id": "settings"}, {"$set": {key: value}}, upsert=True
        )
    setattr(info, key, value)


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


async def get_pm_settings():
    settings = await pm_db.find_one({"id": "pm_config"}) if pm_db is not None else None
    return settings or {
        "text": "<b>🚫 Don't Message Here, Message Here Only!</b>\n\nI do not respond to direct messages in PM. Please join our official group to request and download movies.",
        "image": getattr(info, "NOT_FOUND_IMG", None),
        "button_text": "💬 Message Here Only",
        "button_url": "https://t.me/Sandalwood_Kannada_Group",
        "button_color": "green",
    }


async def save_pm_settings(key, value):
    if pm_db is not None:
        await pm_db.update_one({"id": "pm_config"}, {"$set": {key: value}}, upsert=True)


# ============================================================
# 🛑 SMART STOPWORDS ENGINE
# ============================================================
DEFAULT_STOPWORDS = [
    "send",
    "snd",
    "give",
    "gib",
    "pls",
    "plz",
    "please",
    "need",
    "want",
    "upload",
    "uplod",
    "drop",
    "share",
    "find",
    "search",
    "provide",
    "post",
    "movie",
    "movies",
    "film",
    "films",
    "cinema",
    "cinemas",
    "full",
    "fullmovie",
    "download",
    "downlod",
    "link",
    "links",
    "file",
    "files",
    "print",
    "audio",
    "ott",
    "hd",
    "hq",
    "bluray",
    "rip",
    "watch",
    "online",
    "admin",
    "beku",
    "bekithu",
    "bekittu",
    "bekagide",
    "kodi",
    "kodro",
    "kalsi",
    "kalsro",
    "kalisi",
    "haki",
    "haku",
    "hakro",
    "ideya",
    "irboda",
    "bidi",
    "madu",
    "yaradru",
    "kannadadalli",
    "sandalwood",
    "kr_picture",
    "kannada_filmy_group",
    "telegram",
    "dubbed",
]
ACTIVE_STOPWORDS = DEFAULT_STOPWORDS.copy()
_stopwords_loaded = False


def get_stopwords():
    return ACTIVE_STOPWORDS


@Client.on_message(filters.all, group=-999)
async def load_stopwords_on_boot(client, message):
    global _stopwords_loaded, ACTIVE_STOPWORDS
    if not _stopwords_loaded:
        settings = await get_bot_settings()
        custom_stops = settings.get("custom_stopwords")
        ACTIVE_STOPWORDS = (
            custom_stops if custom_stops is not None else DEFAULT_STOPWORDS.copy()
        )
        _stopwords_loaded = True


@Client.on_message(filters.command("addstopwords") & admin_filter)
async def add_stopwords(client, message):
    global ACTIVE_STOPWORDS
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **Usage:** `/addstopwords word1, word2`")
    new_words = [
        w.strip().lower()
        for w in message.text.split(None, 1)[1].split(",")
        if w.strip()
    ]
    ACTIVE_STOPWORDS = list(set(ACTIVE_STOPWORDS + new_words))
    await update_bot_settings("custom_stopwords", ACTIVE_STOPWORDS)
    await message.reply_text(
        f"✅ **Added {len(new_words)} stopwords.**\nTotal Active Stopwords: `{len(ACTIVE_STOPWORDS)}`"
    )


@Client.on_message(filters.command("stopwords") & admin_filter)
async def show_stopwords(client, message):
    if not ACTIVE_STOPWORDS:
        return await message.reply_text("ℹ️ **No stopwords are currently active.**")
    await message.reply_text(
        f"🛑 **Current Active Stopwords:**\n\n`{', '.join(ACTIVE_STOPWORDS)}`\n\n**Total:** `{len(ACTIVE_STOPWORDS)}`"
    )


@Client.on_message(filters.command("remstopwords") & admin_filter)
async def rem_stopwords(client, message):
    global ACTIVE_STOPWORDS
    if len(message.command) < 2:
        return await message.reply_text("⚠️ **Usage:** `/remstopwords word1, word2`")
    words_to_remove = [
        w.strip().lower()
        for w in message.text.split(None, 1)[1].split(",")
        if w.strip()
    ]
    if not ACTIVE_STOPWORDS:
        return await message.reply_text("ℹ️ **There are no stopwords to remove.**")
    original_count = len(ACTIVE_STOPWORDS)
    ACTIVE_STOPWORDS = [w for w in ACTIVE_STOPWORDS if w not in words_to_remove]
    await update_bot_settings("custom_stopwords", ACTIVE_STOPWORDS)
    await message.reply_text(
        f"✅ **Removed {original_count - len(ACTIVE_STOPWORDS)} stopwords.**\nTotal Stopwords left: `{len(ACTIVE_STOPWORDS)}`"
    )


@Client.on_message(filters.command("remallstopwords") & admin_filter)
async def rem_all_stopwords(client, message):
    global ACTIVE_STOPWORDS
    ACTIVE_STOPWORDS = []
    await update_bot_settings("custom_stopwords", [])
    await message.reply_text("🗑️ **All stopwords have been completely removed.**")


@Client.on_message(filters.command("defaultstopwords") & admin_filter)
async def default_stopwords(client, message):
    global ACTIVE_STOPWORDS
    ACTIVE_STOPWORDS = DEFAULT_STOPWORDS.copy()
    await update_bot_settings("custom_stopwords", ACTIVE_STOPWORDS)
    await message.reply_text(
        "✅ **Stopwords have been successfully reset to the repository default list.**"
    )


# ============================================================
# ⚡ 10-LAYER TITANIUM UPLOAD ENGINE
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

    try:
        res = requests.post(
            "https://x0.at",
            files={"file": ("img.jpg", file_bytes, "image/jpeg")},
            headers=headers,
            timeout=6,
        )
    except:
        res = None
    if res and res.status_code == 200 and res.text.startswith("http"):
        return res.text.strip()

    try:
        res = requests.post(
            "https://ttm.sh",
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
    client: Client, message: Message, db_key: str, menu_name: str, use_ui_db=True
):
    if not message.reply_to_message or not getattr(
        message.reply_to_message, "photo", None
    ):
        return await message.reply_text(
            "⚠️ **Usage:** Reply to a photo with the command."
        )
    status = await message.reply_text(
        "⏳ **Uploading to Cloud (Testing 10 Hosters)...**"
    )
    url = await upload_image_safely(client, message.reply_to_message)
    if url:
        if use_ui_db:
            await update_ui(db_key, url)
        else:
            await save_custom_data(db_key, url)
        await status.edit_text(
            f"✅ **{menu_name} Image updated!**\n*(Saved permanently to cloud)*"
        )
    else:
        file_id = message.reply_to_message.photo.file_id
        if use_ui_db:
            await update_ui(db_key, file_id)
        else:
            await save_custom_data(db_key, file_id)
        await status.edit_text(
            f"⚠️ Cloud blocked. **{menu_name} Image updated!**\n*(Saved as Telegram File_ID)*"
        )


def get_html_text(message: Message):
    if message.reply_to_message and message.reply_to_message.text:
        return message.reply_to_message.text.html
    elif len(message.command) > 1:
        return re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s+", "", message.text.html, count=1)
    return None


# ============================================================
# 🔗 GENERATE LINKS (/link, /plink, /batch, /pbatch)
# ============================================================
def is_file_store_channel(chat_id: int) -> bool:
    fsc = getattr(info, "FILE_STORE_CHANNEL", [])
    if isinstance(fsc, int):
        return chat_id == fsc
    if isinstance(fsc, str):
        return str(chat_id) in [
            x.strip() for x in fsc.replace(",", " ").split() if x.strip()
        ]
    if isinstance(fsc, list):
        return chat_id in [int(x) for x in fsc if str(x).strip().lstrip("-").isdigit()]
    return False


async def allowed(_, __, message):
    if getattr(info, "PUBLIC_FILE_STORE", False):
        return True
    return bool(get_uid(message) in get_admin_list())


@Client.on_message(filters.command(["link", "plink"]) & filters.create(allowed))
async def gen_link_s(bot: Client, message):
    replied = message.reply_to_message
    if not replied:
        return await message.reply("⚠️ Reply to a message to get a shareable link.")
    file_type = getattr(replied, "media", None)
    if not file_type or file_type not in [
        enums.MessageMediaType.VIDEO,
        enums.MessageMediaType.AUDIO,
        enums.MessageMediaType.DOCUMENT,
        enums.MessageMediaType.PHOTO,
        enums.MessageMediaType.ANIMATION,
    ]:
        return await message.reply("⚠️ Reply to a valid media file.")
    if (
        getattr(message, "has_protected_content", False)
        and get_uid(message) not in get_admin_list()
    ):
        return await message.reply("❌ Protected media cannot be shared.")

    media_obj = getattr(replied, file_type.value)
    file_id, _ = unpack_new_file_id(media_obj.file_id)
    string = (
        f"filep_{file_id}"
        if message.command[0].lower() == "plink"
        else f"file_{file_id}"
    )
    outstr = base64.urlsafe_b64encode(string.encode("ascii")).decode().rstrip("=")
    bot_username = temp.U_NAME or (await bot.get_me()).username
    await message.reply(
        f"🔗 **Shareable Link Generated:**\nhttps://t.me/{bot_username}?start={outstr}"
    )


@Client.on_message(filters.command(["batch", "pbatch"]) & filters.create(allowed))
async def gen_link_batch(bot: Client, message):
    links = message.text.strip().split()
    if len(links) != 3:
        return await message.reply(
            "⚙️ **Usage Format:**\n`/batch <first_link> <last_link>`"
        )

    cmd, first, last = links
    regex = re.compile(
        r"(https?://)?(t(elegram)?\.(me|dog)|telegram\.org)/(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"
    )
    match_first, match_last = regex.match(first), regex.match(last)
    if not (match_first and match_last):
        return await message.reply("❌ Invalid Telegram message links.")

    f_chat_raw, f_msg_id = match_first.group(6), int(match_first.group(7))
    l_chat_raw, l_msg_id = match_last.group(6), int(match_last.group(7))
    f_chat_id = int(f"-100{f_chat_raw}") if f_chat_raw.isdigit() else f_chat_raw
    l_chat_id = int(f"-100{l_chat_raw}") if l_chat_raw.isdigit() else l_chat_raw

    if f_chat_id != l_chat_id:
        return await message.reply(
            "❌ Both links must originate from the same channel."
        )

    try:
        chat_id = (await bot.get_chat(f_chat_id)).id
    except ChannelInvalid_req:
        return await message.reply(
            "❌ Bot lacks access. Add this bot as an admin in the target channel."
        )
    except Exception as e:
        return await message.reply(f"❌ Error accessing channel: `{e}`")

    sts = await message.reply("⏳ **Initiating Batch Generation...**")
    bot_username = temp.U_NAME or (await bot.get_me()).username

    if is_file_store_channel(chat_id):
        b_64 = (
            base64.urlsafe_b64encode(
                f"{f_msg_id}_{l_msg_id}_{chat_id}_{cmd.lower().strip()}".encode("ascii")
            )
            .decode()
            .rstrip("=")
        )
        return await sts.edit(
            f"🔗 **Batch File Store Link:**\nhttps://t.me/{bot_username}?start=DSTORE-{b_64}"
        )

    outlist, og_msg = [], 0
    total_range = list(range(min(f_msg_id, l_msg_id), max(f_msg_id, l_msg_id) + 1))
    total_count = len(total_range)

    for i in range(0, total_count, 200):
        chunk_ids = total_range[i : i + 200]
        try:
            messages = await bot.get_messages(chat_id, message_ids=chunk_ids)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            messages = await bot.get_messages(chat_id, message_ids=chunk_ids)
        except Exception:
            continue

        for msg in messages:
            if (
                not msg
                or getattr(msg, "empty", False)
                or getattr(msg, "service", False)
                or not getattr(msg, "media", None)
            ):
                continue
            try:
                media_obj = getattr(msg, msg.media.value)
                outlist.append(
                    {
                        "file_id": media_obj.file_id,
                        "caption": (
                            msg.caption.html if getattr(msg, "caption", None) else ""
                        ),
                        "title": getattr(media_obj, "file_name", ""),
                        "size": getattr(media_obj, "file_size", 0),
                        "protect": cmd.lower().strip() == "/pbatch",
                    }
                )
                og_msg += 1
            except Exception:
                pass
        try:
            await sts.edit(
                f"⏳ **Batch Link Progress:**\nProcessed: `{min(i + 200, total_count)}/{total_count}` messages\nFiles Found: `{og_msg}`"
            )
        except Exception:
            pass

    if not outlist:
        return await sts.edit(
            "❌ No downloadable media found within the provided message range."
        )

    batch_file_path = f"batchmode_{message.from_user.id}.json"
    with open(batch_file_path, "w+", encoding="utf-8") as out:
        json.dump(outlist, out)

    post, log_chan = None, getattr(info, "LOG_CHANNEL", None)
    if log_chan:
        try:
            post = await bot.send_document(
                log_chan,
                batch_file_path,
                file_name="Batch.json",
                caption=f"⚠️ Batch generated for {message.from_user.mention} ({og_msg} files).",
            )
        except Exception:
            pass

    if not post:
        try:
            post = await bot.send_document(
                message.chat.id,
                batch_file_path,
                file_name="Batch.json",
                caption="⚠️ Batch generated locally.",
            )
        except Exception as err:
            if os.path.exists(batch_file_path):
                os.remove(batch_file_path)
            return await sts.edit(f"❌ Failed to archive batch: `{err}`")

    if os.path.exists(batch_file_path):
        os.remove(batch_file_path)
    file_id, _ = unpack_new_file_id(post.document.file_id)
    await sts.edit(
        f"✅ **Batch Link Complete!**\nFiles Included: `{og_msg}`\n\n🔗 https://t.me/{bot_username}?start=BATCH-{file_id}"
    )


# ============================================================
# 📚 SMART AUTO-INDEXING SYSTEM
# ============================================================
async def save_resume_state(chat_id, last_processed_id):
    if resume_db is not None:
        await resume_db.update_one(
            {"chat_id": chat_id}, {"$set": {"last_id": last_processed_id}}, upsert=True
        )


async def get_resume_state(chat_id):
    doc = (
        await resume_db.find_one({"chat_id": chat_id})
        if resume_db is not None
        else None
    )
    return doc.get("last_id", 0) if doc else 0


async def get_clean_words():
    if cw_db is None:
        return DEFAULT_CLEAN_WORDS
    try:
        doc = await cw_db.find_one({"id": "words"})
        if doc is None or doc.get("use_default", True):
            return DEFAULT_CLEAN_WORDS
        return doc.get("list", [])
    except Exception:
        return DEFAULT_CLEAN_WORDS


async def get_idx_settings():
    default_settings = {
        "min_size": 0,
        "blacklist": ["trailer", "promo", "teaser", "sample"],
        "whitelist": [],
        "auto_backup": False,
        "backup_channel": None,
    }
    if idx_db is None:
        return default_settings
    try:
        doc = await idx_db.find_one({"id": "config"})
        if not doc:
            return default_settings
        for k, v in default_settings.items():
            if k not in doc:
                doc[k] = v
        return doc
    except Exception:
        return default_settings


async def save_idx_settings(key, value):
    if idx_db is not None:
        await idx_db.update_one({"id": "config"}, {"$set": {key: value}}, upsert=True)


def clean_filename(name: str, clean_words: list) -> str:
    if not name:
        return "File"
    if any(w.lower() in ["mkv", "sandalwood"] for w in clean_words):
        name = re.sub(r"(?i)\[?@?sandalwood[^\]\s]*\]?", "", name)
        name = re.sub(r"(?i)\b(sandalwood|mkv|mp4|avi|webm|zip|rar)\b", "", name)
    name = re.sub(r"[_.-]", " ", name)
    for word in clean_words:
        if word.lower() in ["mkv", "sandalwood", "mp4", "avi", "webm", "zip", "rar"]:
            continue
        name = re.sub(rf"(?i){re.escape(word)}", "", name)
    return re.sub(r"\s+", " ", name).strip()


def parse_channels(chan_var):
    if isinstance(chan_var, list):
        return [int(x) for x in chan_var if str(x).lstrip("-").isdigit()]
    if isinstance(chan_var, str):
        return [
            int(x)
            for x in chan_var.replace(",", " ").split()
            if x.strip().lstrip("-").isdigit()
        ]
    if isinstance(chan_var, int):
        return [chan_var]
    return []


AUTO_INDEX_CHANNELS = list(
    set(
        parse_channels(getattr(info, "CHANNELS", []))
        + parse_channels(getattr(info, "INDEX_CHANNELS", []))
    )
)
idx_lock = asyncio.Lock()


class SafeMedia:
    def __init__(self, media_obj, file_type, caption, override_name=None):
        self.file_id = getattr(media_obj, "file_id", "")
        self.file_unique_id = getattr(media_obj, "file_unique_id", "")
        self.file_name = (
            override_name if override_name else getattr(media_obj, "file_name", "")
        )
        self.file_size = getattr(media_obj, "file_size", 0)
        self.mime_type = getattr(media_obj, "mime_type", "")
        self.file_type = file_type
        self.caption = caption


@Client.on_message(
    filters.channel
    & (filters.document | filters.video | filters.audio)
    & ~filters.forwarded,
    group=-5,
)
async def auto_index_new_files(bot: Client, message: Message):
    if AUTO_INDEX_CHANNELS and message.chat.id not in AUTO_INDEX_CHANNELS:
        return
    media_obj = getattr(message, message.media.value, None)
    if not media_obj:
        return

    idx_settings = await get_idx_settings()
    if media_obj.file_size < idx_settings.get("min_size", 0):
        return
    raw_name = getattr(media_obj, "file_name", "Unknown")
    raw_caption = message.caption if getattr(message, "caption", None) else ""

    if any(
        b.lower() in raw_name.lower() or b.lower() in raw_caption.lower()
        for b in idx_settings.get("blacklist", [])
    ):
        return
    if idx_settings.get("whitelist") and not any(
        w.lower() in raw_name.lower() or w.lower() in raw_caption.lower()
        for w in idx_settings.get("whitelist", [])
    ):
        return

    target_media, target_caption = media_obj, getattr(message, "caption", None)
    if idx_settings.get("auto_backup") and idx_settings.get("backup_channel"):
        try:
            copied_msg = await message.copy(chat_id=idx_settings.get("backup_channel"))
            target_media, target_caption = getattr(
                copied_msg, copied_msg.media.value, media_obj
            ), getattr(copied_msg, "caption", None)
        except Exception as e:
            logger.error(f"Auto-backup failed for {message.chat.id}: {e}")

    clean_words = await get_clean_words()
    safe_media = SafeMedia(
        target_media,
        message.media.value,
        target_caption,
        clean_filename(getattr(target_media, "file_name", "Unknown"), clean_words),
    )
    try:
        await save_batch([safe_media])
    except Exception as e:
        logger.error(f"Auto-index failed for {message.chat.title}: {e}")


@Client.on_message(
    filters.private & (filters.document | filters.video | filters.audio) & admin_filter,
    group=-5,
)
async def pm_media_handler(bot: Client, message: Message):
    if (
        getattr(message, "forward_from_chat", None)
        and message.forward_from_chat.type == enums.ChatType.CHANNEL
    ):
        chat_id = message.forward_from_chat.username or message.forward_from_chat.id
        buttons = [
            [
                InlineKeyboardButton(
                    "Index Whole Channel",
                    callback_data=f"idx#ac#{chat_id}#{message.forward_from_message_id}#{get_uid(message)}#all",
                )
            ],
            [
                InlineKeyboardButton(
                    "Save Only This File", callback_data=f"save_single#{message.id}"
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data="close_data")],
        ]
        await message.reply(
            "📂 **File received from a channel.**\n\nWhat would you like to do?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await save_single_file(bot, message)


@Client.on_callback_query(filters.regex(r"^save_single#"), group=-5)
async def save_single_callback(bot: Client, query: CallbackQuery):
    msg_id = int(query.data.split("#")[1])
    try:
        original_message = await bot.get_messages(query.message.chat.id, msg_id)
        await query.message.delete()
        if original_message:
            await save_single_file(bot, original_message)
    except Exception:
        await query.answer("Failed to process file.", show_alert=True)


async def save_single_file(bot: Client, message: Message):
    media_obj = getattr(message, message.media.value, None)
    if not media_obj:
        return
    idx_settings = await get_idx_settings()
    if media_obj.file_size < idx_settings.get("min_size", 0):
        return await message.reply(
            "⚠️ Ignored: File size is smaller than minimum limit."
        )

    raw_name = getattr(media_obj, "file_name", "Unknown")
    raw_caption = getattr(message, "caption", "") or ""

    if any(
        b.lower() in raw_name.lower() or b.lower() in raw_caption.lower()
        for b in idx_settings.get("blacklist", [])
    ):
        return await message.reply("⚠️ Ignored: File contains blacklisted words.")
    if idx_settings.get("whitelist") and not any(
        w.lower() in raw_name.lower() or w.lower() in raw_caption.lower()
        for w in idx_settings.get("whitelist", [])
    ):
        return await message.reply(
            "⚠️ Ignored: File does not contain whitelisted words."
        )

    target_media, target_caption = media_obj, getattr(message, "caption", None)
    if idx_settings.get("auto_backup") and idx_settings.get("backup_channel"):
        try:
            copied_msg = await message.copy(chat_id=idx_settings.get("backup_channel"))
            target_media, target_caption = getattr(
                copied_msg, copied_msg.media.value, media_obj
            ), getattr(copied_msg, "caption", None)
        except Exception:
            pass

    clean_words = await get_clean_words()
    cleaned_name = clean_filename(
        getattr(target_media, "file_name", "Unknown"), clean_words
    )
    safe_media = SafeMedia(
        target_media, message.media.value, target_caption, cleaned_name
    )
    try:
        await save_batch([safe_media])
        await message.reply(
            f"✅ **Successfully Saved to Database!**\n\n`{cleaned_name}`"
        )
    except Exception as e:
        await message.reply(f"❌ **Failed to index:** `{e}`")


async def process_index_request(
    bot: Client, message: Message, chat_id, last_msg_id, filter_type="all"
):
    try:
        chat_id = (await bot.get_chat(chat_id)).id
    except Exception as e:
        return await message.reply(f"⚠️ Error: `{e}`")
    skip_count = getattr(temp, "CURRENT", 0)
    buttons = [
        [
            InlineKeyboardButton(
                "Yes, Start Indexing",
                callback_data=f"idx#ac#{chat_id}#{last_msg_id}#{get_uid(message)}#{filter_type}",
            )
        ],
        [InlineKeyboardButton("Close", callback_data="close_data")],
    ]
    return await message.reply(
        f"**Ready to Index ({filter_type.title()})**\n\n**Chat ID:** <code>{chat_id}</code>\n**Last Message:** <code>{last_msg_id}</code>\n**Skip Count:** <code>{skip_count}</code>",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_message(
    filters.command(["index", "indexvideo", "indexdoc", "indexaudio"]) & admin_filter,
    group=-5,
)
async def index_command(bot: Client, message: Message):
    cmd = message.command[0].lower()
    filter_type = (
        "video"
        if cmd == "indexvideo"
        else (
            "document"
            if cmd == "indexdoc"
            else ("audio" if cmd == "indexaudio" else "all")
        )
    )
    if (
        getattr(message, "reply_to_message", None)
        and getattr(message.reply_to_message, "forward_from_chat", None)
        and message.reply_to_message.forward_from_chat.type == enums.ChatType.CHANNEL
    ):
        return await process_index_request(
            bot,
            message,
            message.reply_to_message.forward_from_chat.username
            or message.reply_to_message.forward_from_chat.id,
            message.reply_to_message.forward_from_message_id,
            filter_type,
        )
    if len(message.command) != 3:
        return await message.reply(f"**Usage:** `/{cmd} <chat_id> <last_message_id>`")
    chat_id = (
        int(message.command[1])
        if message.command[1].lstrip("-").isdigit()
        else message.command[1]
    )
    try:
        last_msg_id = int(message.command[2])
    except ValueError:
        return await message.reply("⚠️ Last message ID must be an integer.")
    await process_index_request(bot, message, chat_id, last_msg_id, filter_type)


@Client.on_message(filters.command("resumeindex") & admin_filter, group=-5)
async def resume_index_command(bot: Client, message: Message):
    if len(message.command) != 2:
        return await message.reply("**Usage:** `/resumeindex <chat_id>`")
    chat_id = (
        int(message.command[1])
        if message.command[1].lstrip("-").isnumeric()
        else message.command[1]
    )
    last_processed = await get_resume_state(chat_id)
    if last_processed <= 0:
        return await message.reply("⚠️ No crash-resume state found.")
    status = await message.reply("⏳ Fetching highest message ID...")
    try:
        last_msg_id = last_processed + 1000
        async for m in bot.get_chat_history(chat_id, limit=1):
            last_msg_id = m.id
            break
        await status.delete()
        temp.CURRENT = last_processed
        await process_index_request(bot, message, chat_id, last_msg_id, "all")
    except Exception as e:
        await status.edit_text(f"❌ Failed to resume: {e}")


@Client.on_message(
    filters.text
    & filters.regex(
        r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"
    )
    & admin_filter,
    group=-5,
)
async def send_for_index(bot: Client, message: Message):
    match = re.compile(
        r"(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$"
    ).match(message.text)
    if match:
        await process_index_request(
            bot,
            message,
            (
                int(f"-100{match.group(4)}")
                if match.group(4).isnumeric()
                else match.group(4)
            ),
            int(match.group(5)),
            "all",
        )


@Client.on_message(
    filters.private & filters.forwarded & filters.text & admin_filter, group=-5
)
async def pm_forward_text_indexer(bot: Client, message: Message):
    if (
        getattr(message, "forward_from_chat", None)
        and message.forward_from_chat.type == enums.ChatType.CHANNEL
    ):
        await process_index_request(
            bot,
            message,
            message.forward_from_chat.username or message.forward_from_chat.id,
            message.forward_from_message_id,
            "all",
        )


async def index_files_to_db(
    lst_msg_id,
    chat,
    msg: Message,
    bot: Client,
    skip_number: int = 0,
    filter_type: str = "all",
):
    total_files = duplicate = errors = deleted = no_media = unsupported = (
        fetched_count
    ) = 0
    clean_words, idx_settings = await get_clean_words(), await get_idx_settings()
    min_size, blacklist, whitelist, auto_backup, backup_channel = (
        idx_settings.get("min_size", 0),
        idx_settings.get("blacklist", []),
        idx_settings.get("whitelist", []),
        idx_settings.get("auto_backup", False),
        idx_settings.get("backup_channel"),
    )
    delay_speed = getattr(temp, "INDEX_SPEED", 0)

    if not hasattr(temp, "INDEX_CANCEL"):
        temp.INDEX_CANCEL = {}
    temp.INDEX_CANCEL[chat] = False

    async with idx_lock:
        try:
            last_update_time = time.time()
            message_ids = list(range(max(1, skip_number), lst_msg_id + 1))
            for i in range(0, len(message_ids), 200):
                if temp.INDEX_CANCEL.get(chat):
                    try:
                        await msg.edit(
                            f"**⛔ Cancelled!** Saved: `{total_files}` files."
                        )
                    except Exception:
                        pass
                    return

                chunk = message_ids[i : i + 200]
                messages = []
                for _ in range(3):
                    try:
                        messages = await bot.get_messages(chat, chunk)
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value + 1)
                    except Exception:
                        break

                if not messages:
                    continue

                media_to_save = []
                for message in messages:
                    fetched_count += 1
                    if time.time() - last_update_time > 8:
                        try:
                            await msg.edit_text(
                                text=f"⚡ **Indexing ({filter_type})...**\nFetched: `{fetched_count}` | Saved: `{total_files}`",
                                reply_markup=InlineKeyboardMarkup(
                                    [
                                        [
                                            InlineKeyboardButton(
                                                "Cancel",
                                                callback_data=f"idx_cancel#{chat}",
                                            )
                                        ]
                                    ]
                                ),
                            )
                        except Exception:
                            pass
                        last_update_time = time.time()

                    if getattr(message, "empty", False):
                        deleted += 1
                        continue
                    elif not getattr(message, "media", None):
                        no_media += 1
                        continue
                    elif message.media not in [
                        enums.MessageMediaType.VIDEO,
                        enums.MessageMediaType.AUDIO,
                        enums.MessageMediaType.DOCUMENT,
                    ]:
                        unsupported += 1
                        continue
                    if filter_type != "all" and message.media.value != filter_type:
                        unsupported += 1
                        continue

                    media_obj = getattr(message, message.media.value, None)
                    if not media_obj or media_obj.file_size < min_size:
                        unsupported += 1
                        continue

                    raw_name = getattr(media_obj, "file_name", "Unknown")
                    raw_caption = getattr(message, "caption", "") or ""

                    if any(
                        b.lower() in raw_name.lower()
                        or b.lower() in raw_caption.lower()
                        for b in blacklist
                    ):
                        unsupported += 1
                        continue
                    if whitelist and not any(
                        w.lower() in raw_name.lower()
                        or w.lower() in raw_caption.lower()
                        for w in whitelist
                    ):
                        unsupported += 1
                        continue

                    target_media, target_caption = media_obj, getattr(
                        message, "caption", None
                    )
                    if auto_backup and backup_channel:
                        try:
                            copied_msg = await message.copy(chat_id=backup_channel)
                            target_media, target_caption = getattr(
                                copied_msg, copied_msg.media.value, media_obj
                            ), getattr(copied_msg, "caption", None)
                            await asyncio.sleep(1)
                        except Exception:
                            pass

                    media_to_save.append(
                        SafeMedia(
                            target_media,
                            message.media.value,
                            target_caption,
                            clean_filename(
                                getattr(target_media, "file_name", ""), clean_words
                            ),
                        )
                    )

                if media_to_save:
                    try:
                        result = await save_batch(media_to_save)
                        if isinstance(result, tuple) and len(result) == 3:
                            saved, dups, errs = result
                            total_files += saved
                            duplicate += dups
                            errors += errs
                        else:
                            total_files += len(media_to_save)
                    except Exception:
                        errors += len(media_to_save)

                await save_resume_state(chat, message.id)
                if delay_speed > 0:
                    await asyncio.sleep(delay_speed)
        except Exception as e:
            try:
                await msg.edit(f"⚠️ Error: `{e}`")
            except Exception:
                pass
        else:
            if not temp.INDEX_CANCEL.get(chat):
                try:
                    await msg.edit(
                        f"🎉 **Indexing Complete!**\nSaved: `{total_files}` files | Duplicates: `{duplicate}`"
                    )
                except Exception:
                    pass


@Client.on_callback_query(filters.regex(r"^(idx|idx_cancel)"), group=-5)
async def index_files(bot: Client, query: CallbackQuery):
    if query.data.startswith("idx_cancel"):
        try:
            temp.INDEX_CANCEL[int(query.data.split("#")[1])] = True
            await query.message.edit("❌ Indexing Cancelled.")
            return await query.answer("Cancelling Indexing...", show_alert=True)
        except Exception:
            return await query.answer("Cancel signal sent.", show_alert=True)

    data_parts = query.data.split("#")
    if len(data_parts) != 6:
        return await query.answer("Invalid callback data structure.", show_alert=True)
    if idx_lock.locked():
        return await query.answer("Another index process is running.", show_alert=True)

    _, _, chat, lst_msg_id, _, filter_type = data_parts
    await query.answer("Starting Indexing...⏳", show_alert=True)
    chat = int(chat) if chat.lstrip("-").isdigit() else chat
    try:
        await query.message.edit(
            "Starting Indexing Engine...",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data=f"idx_cancel#{chat}")]]
            ),
        )
    except MessageNotModified:
        pass
    asyncio.create_task(
        index_files_to_db(
            int(lst_msg_id),
            chat,
            query.message,
            bot,
            getattr(temp, "CURRENT", 0),
            filter_type,
        )
    )


@Client.on_message(filters.command("setskip") & admin_filter, group=-5)
async def set_skip_number(bot: Client, message: Message):
    if len(message.command) > 1:
        try:
            skip = int(message.command[1])
            if skip < 0:
                return await message.reply("⚠️ Skip number must be 0 or greater.")
            temp.CURRENT = skip
            await message.reply(f"✅ Successfully set default SKIP number to `{skip}`.")
        except ValueError:
            await message.reply("⚠️ Skip number must be an integer.")
    else:
        await message.reply("⚠️ Usage: `/setskip 100`")


@Client.on_message(filters.command("setindexspeed") & admin_filter, group=-5)
async def set_index_speed(bot: Client, message: Message):
    if len(message.command) > 1:
        try:
            temp.INDEX_SPEED = float(message.command[1])
            await message.reply(f"✅ Fetch delay set to `{temp.INDEX_SPEED}` seconds.")
        except ValueError:
            await message.reply("⚠️ Speed must be a number.")
    else:
        await message.reply("⚠️ Usage: `/setindexspeed 1.5`")


@Client.on_message(filters.command("currentskip") & admin_filter, group=-5)
async def current_skip_number(bot: Client, message: Message):
    await message.reply(
        f"ℹ️ The current default SKIP number is: `{getattr(temp, 'CURRENT', 0)}`"
    )


@Client.on_message(filters.command("deleteskip") & admin_filter, group=-5)
async def delete_skip_number(bot: Client, message: Message):
    temp.CURRENT = 0
    await message.reply("✅ Successfully reset SKIP number to `0`.")


@Client.on_message(filters.command("setminsize") & admin_filter, group=-5)
async def set_min_size(bot: Client, message: Message):
    if len(message.command) > 1:
        try:
            mb_size = int(message.command[1])
            await save_idx_settings("min_size", mb_size * 1024 * 1024)
            await message.reply(f"✅ Minimum size set to `{mb_size} MB`.")
        except ValueError:
            await message.reply("⚠️ Size must be an integer.")
    else:
        await message.reply("⚠️ Usage: `/setminsize 50`")


@Client.on_message(filters.command("setblacklist") & admin_filter, group=-5)
async def set_blacklist(bot: Client, message: Message):
    if len(message.command) > 1:
        words_list = [
            w.strip().lower()
            for w in message.text.split(None, 1)[1].split(",")
            if w.strip()
        ]
        settings = await get_idx_settings()
        updated_list = list(set(settings.get("blacklist", []) + words_list))
        await save_idx_settings("blacklist", updated_list)
        await message.reply(f"✅ Blacklist added: `{', '.join(words_list)}`")
    else:
        await message.reply("⚠️ Usage: `/setblacklist promo, trailer`")


@Client.on_message(filters.command("remblacklist") & admin_filter, group=-5)
async def rem_blacklist(bot: Client, message: Message):
    if len(message.command) > 1:
        word = message.command[1].strip().lower()
        settings = await get_idx_settings()
        current_list = settings.get("blacklist", [])
        if word in current_list:
            current_list.remove(word)
            await save_idx_settings("blacklist", current_list)
            await message.reply(f"🗑️ Removed `{word}` from blacklist.")
        else:
            await message.reply("⚠️ Word not found.")
    else:
        await message.reply("⚠️ Usage: `/remblacklist promo`")


@Client.on_message(filters.command("allblacklist") & admin_filter, group=-5)
async def all_blacklist(bot: Client, message: Message):
    settings = await get_idx_settings()
    current_list = settings.get("blacklist", [])
    if not current_list:
        return await message.reply("ℹ️ Blacklist is empty.")
    await message.reply(f"🚫 **Blacklist Words:**\n\n`{', '.join(current_list)}`")


@Client.on_message(filters.command("setwhitelist") & admin_filter, group=-5)
async def set_whitelist(bot: Client, message: Message):
    if len(message.command) > 1:
        words_list = [
            w.strip().lower()
            for w in message.text.split(None, 1)[1].split(",")
            if w.strip()
        ]
        settings = await get_idx_settings()
        updated_list = list(set(settings.get("whitelist", []) + words_list))
        await save_idx_settings("whitelist", updated_list)
        await message.reply(f"✅ Whitelist added: `{', '.join(words_list)}`")
    else:
        await message.reply("⚠️ Usage: `/setwhitelist 1080p, Kannada`")


@Client.on_message(filters.command("remwhitelist") & admin_filter, group=-5)
async def rem_whitelist(bot: Client, message: Message):
    if len(message.command) > 1:
        word = message.command[1].strip().lower()
        settings = await get_idx_settings()
        current_list = settings.get("whitelist", [])
        if word in current_list:
            current_list.remove(word)
            await save_idx_settings("whitelist", current_list)
            await message.reply(f"🗑️ Removed `{word}` from whitelist.")
        else:
            await message.reply("⚠️ Word not found.")
    else:
        await message.reply("⚠️ Usage: `/remwhitelist 1080p`")


@Client.on_message(filters.command("allwhitelist") & admin_filter, group=-5)
async def all_whitelist(bot: Client, message: Message):
    settings = await get_idx_settings()
    current_list = settings.get("whitelist", [])
    if not current_list:
        return await message.reply("ℹ️ Whitelist is empty.")
    await message.reply(f"🎯 **Whitelist Words:**\n\n`{', '.join(current_list)}`")


@Client.on_message(filters.command("setbackupchannel") & admin_filter, group=-5)
async def set_backup_channel(bot: Client, message: Message):
    if len(message.command) > 1:
        try:
            chat_id = int(message.command[1])
            await save_idx_settings("backup_channel", chat_id)
            await message.reply(f"✅ Backup channel set to `{chat_id}`.")
        except ValueError:
            await message.reply("❌ Invalid Chat ID.")
    else:
        await message.reply("⚠️ Usage: `/setbackupchannel -100xxxxxx`")


@Client.on_message(filters.command("autobackup") & admin_filter, group=-5)
async def toggle_autobackup(bot: Client, message: Message):
    settings = await get_idx_settings()
    if len(message.command) < 2:
        return await message.reply(
            f"**Auto-Backup Status:** {'🟢 ON' if settings.get('auto_backup') else '🔴 OFF'}\n**Channel:** `{settings.get('backup_channel', 'Not Set')}`"
        )
    cmd = message.command[1].lower()
    if cmd == "on":
        if not settings.get("backup_channel"):
            return await message.reply(
                "⚠️ Set backup channel first using `/setbackupchannel`"
            )
        await save_idx_settings("auto_backup", True)
        await message.reply("🛡️ **Auto-Backup Shield is ON!**")
    elif cmd == "off":
        await save_idx_settings("auto_backup", False)
        await message.reply("🔴 **Auto-Backup OFF!**")


@Client.on_message(filters.command("cleandeadlinks") & admin_filter, group=-5)
async def clean_dead_links(bot: Client, message: Message):
    limit = 500
    if len(message.command) > 1:
        try:
            limit = int(message.command[1])
        except ValueError:
            pass
    msg = await message.reply(f"⏳ Checking last `{limit}` files for dead links...")
    deleted = checked = 0
    async for file in _Media.collection.find().sort("file_id", -1).limit(limit):
        checked += 1
        if checked % 50 == 0:
            try:
                await msg.edit(
                    f"⏳ Scanning... Checked: `{checked}` / `{limit}` | Deleted: `{deleted}`"
                )
            except MessageNotModified:
                pass
        try:
            await bot.get_file(file["file_id"])
            await asyncio.sleep(0.5)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            await _Media.collection.delete_one({"_id": file["_id"]})
            deleted += 1
    await msg.edit(
        f"✅ **Dead Link Scan Complete!**\nChecked: `{checked}`\nRemoved: `{deleted}` broken links."
    )


# ============================================================
# 👥 FORCE ADD ENGINE & LEADERBOARDS
# ============================================================
async def set_fa_config(chat_id: int, limit: int, mode: str):
    if fa_config_db is not None:
        await fa_config_db.update_one(
            {"chat_id": chat_id}, {"$set": {"limit": limit, "mode": mode}}, upsert=True
        )


async def get_fa_config(chat_id: int):
    doc = (
        await fa_config_db.find_one({"chat_id": chat_id})
        if fa_config_db is not None
        else None
    )
    return doc if doc else {"limit": 0, "mode": "all"}


async def add_user_score(chat_id: int, user_id: int, count: int):
    if fa_users_db is not None:
        await fa_users_db.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$push": {"adds": {"$each": [int(time.time())] * count}}},
            upsert=True,
        )


async def get_user_score(chat_id: int, user_id: int):
    doc = (
        await fa_users_db.find_one({"chat_id": chat_id, "user_id": user_id})
        if fa_users_db is not None
        else None
    )
    return len(doc["adds"]) if doc and "adds" in doc else 0


async def mark_new_user(chat_id: int, user_id: int):
    if fa_users_db is not None:
        await fa_users_db.update_one(
            {"chat_id": chat_id, "user_id": user_id},
            {"$set": {"is_new": True}},
            upsert=True,
        )


async def is_new_user(chat_id: int, user_id: int):
    doc = (
        await fa_users_db.find_one({"chat_id": chat_id, "user_id": user_id})
        if fa_users_db is not None
        else None
    )
    return doc.get("is_new", False) if doc else False


@Client.on_message(filters.command("setforceadd") & filters.group)
async def set_force_add(bot: Client, message: Message):
    if get_uid(message) not in get_admin_list():
        try:
            member = await bot.get_chat_member(message.chat.id, get_uid(message))
            if member.status not in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]:
                return await message.reply_text(
                    "❌ **Only group admins can use this command.**"
                )
        except Exception:
            return await message.reply_text(
                "❌ **Only group admins can use this command.**"
            )

    if len(message.command) < 2:
        return await message.reply_text(
            "⚙️ **Usage:** `/setforceadd <number>`\nExample: `/setforceadd 5`"
        )
    try:
        limit = int(message.command[1])
        if limit < 0:
            raise ValueError
    except ValueError:
        return await message.reply_text("❌ Please provide a valid positive number.")

    admin_id, grp_id = get_uid(message), message.chat.id
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👥 Force Add for ALL Members",
                    callback_data=f"fa_set_{limit}_all_{admin_id}_{grp_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🆕 Force Add for ONLY NEW Members",
                    callback_data=f"fa_set_{limit}_new_{admin_id}_{grp_id}",
                )
            ],
        ]
    )
    await message.reply_text(
        "🎯 **Who should this requirement apply to?**\n\n*(Select an option below)*",
        reply_markup=kb,
    )


@Client.on_callback_query(filters.regex(r"^fa_set_(\d+)_([a-z]+)_(\d+)_(-?\d+)$"))
async def set_forceadd_callback(bot: Client, query: CallbackQuery):
    limit, mode, admin_id, grp_id = (
        int(query.matches[0].group(1)),
        query.matches[0].group(2),
        int(query.matches[0].group(3)),
        int(query.matches[0].group(4)),
    )
    if query.from_user.id != admin_id:
        return await query.answer(
            "❌ Only the admin who ran the command can choose this.", show_alert=True
        )

    await set_fa_config(grp_id, limit, mode)
    try:
        await query.message.edit_text(
            f"✅ **Force Add Configured Successfully!**\n\n🔢 **Limit:** `{limit} members`\n🎯 **Target:** `{'ALL MEMBERS' if mode == 'all' else 'ONLY NEW MEMBERS (who join from now on)'}`\n*(Engine is now 100% Active)*"
        )
    except MessageNotModified:
        pass
    await query.answer()


@Client.on_message(filters.command("remforceadd") & filters.group)
async def remove_force_add(bot: Client, message: Message):
    if get_uid(message) not in get_admin_list():
        try:
            member = await bot.get_chat_member(message.chat.id, get_uid(message))
            if member.status not in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]:
                return
        except Exception:
            return
    await set_fa_config(message.chat.id, 0, "all")
    await message.reply_text(
        "🗑️ **Force Add requirement has been completely removed.**"
    )


@Client.on_message(filters.command("getforceadd") & filters.group)
async def get_force_add(bot: Client, message: Message):
    settings = await get_fa_config(message.chat.id)
    if settings["limit"] == 0:
        await message.reply_text(
            "ℹ️ **Force Add is currently DISABLED in this group.**"
        )
    else:
        await message.reply_text(
            f"ℹ️ **Current Requirement:** Users must add {settings['limit']} members.\n🎯 **Applies to:** {'Everyone' if settings['mode'] == 'all' else 'Only New Members'}"
        )


async def generate_leaderboard(
    bot: Client, message: Message, title: str, time_limit_seconds: int = None
):
    if fa_users_db is None:
        return await message.reply("Database Error.")
    pipeline = [{"$match": {"chat_id": message.chat.id}}]
    if time_limit_seconds:
        cutoff = int(time.time()) - time_limit_seconds
        pipeline.extend(
            [
                {
                    "$project": {
                        "user_id": 1,
                        "valid_adds": {
                            "$filter": {
                                "input": "$adds",
                                "as": "timestamp",
                                "cond": {"$gte": ["$$timestamp", cutoff]},
                            }
                        },
                    }
                },
                {
                    "$project": {
                        "user_id": 1,
                        "score": {"$size": {"$ifNull": ["$valid_adds", []]}},
                    }
                },
            ]
        )
    else:
        pipeline.append(
            {"$project": {"user_id": 1, "score": {"$size": {"$ifNull": ["$adds", []]}}}}
        )

    pipeline.extend(
        [{"$match": {"score": {"$gt": 0}}}, {"$sort": {"score": -1}}, {"$limit": 10}]
    )
    top_users = await fa_users_db.aggregate(pipeline).to_list(length=10)

    if not top_users:
        return await message.reply_text(
            f"📊 **{title}**\n\nNo members have added anyone yet!"
        )

    text = f"📊 **{title} (Top 10)**\n\n"
    for i, data in enumerate(top_users, 1):
        uid, score = data["user_id"], data["score"]
        try:
            user_name = (await bot.get_users(uid)).mention
        except Exception:
            user_name = f"User {uid}"
        text += f"**{i}.** {user_name} ➔ `{score}` added\n"
    await message.reply_text(text, disable_web_page_preview=True)


@Client.on_message(filters.command("topaddall") & filters.group)
async def top_add_all(bot: Client, message: Message):
    await generate_leaderboard(bot, message, "All-Time Top Adders", None)


@Client.on_message(filters.command("topadd24") & filters.group)
async def top_add_24(bot: Client, message: Message):
    await generate_leaderboard(bot, message, "Top Adders (Past 24 Hours)", 86400)


@Client.on_message(filters.command("topadd7") & filters.group)
async def top_add_7(bot: Client, message: Message):
    await generate_leaderboard(bot, message, "Top Adders (Past 7 Days)", 604800)


@Client.on_message(filters.command("resetadddaily") & filters.group)
async def reset_add_daily(bot: Client, message: Message):
    if get_uid(message) not in get_admin_list():
        try:
            member = await bot.get_chat_member(message.chat.id, get_uid(message))
            if member.status not in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]:
                return
        except Exception:
            return
    if fa_users_db is not None:
        await fa_users_db.update_many(
            {"chat_id": message.chat.id},
            {"$pull": {"adds": {"$lt": int(time.time()) - 86400}}},
        )
        await message.reply_text(
            "♻️ **Daily limits reset!** Leaderboards for past 24h have been wiped."
        )


@Client.on_message(filters.command("resetadd") & filters.group)
async def reset_all_adds(bot: Client, message: Message):
    if get_uid(message) not in get_admin_list():
        try:
            member = await bot.get_chat_member(message.chat.id, get_uid(message))
            if member.status not in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]:
                return
        except Exception:
            return
    if fa_users_db is not None:
        await fa_users_db.update_many(
            {"chat_id": message.chat.id}, {"$set": {"adds": []}}
        )
        await message.reply_text(
            "💥 **TOTAL RESET!** All members' scores are now 0. Everyone must add members again."
        )


@Client.on_message(filters.command("myadds") & filters.group)
async def my_adds(bot: Client, message: Message):
    settings = await get_fa_config(message.chat.id)
    if settings["limit"] == 0:
        return await message.reply_text("ℹ️ Force Add is not active in this group.")

    current_adds = await get_user_score(message.chat.id, message.from_user.id)
    if current_adds >= settings["limit"]:
        await message.reply_text(
            f"✅ You have added **{current_adds}** members. You are cleared to chat freely!"
        )
    else:
        await message.reply_text(
            f"⚠️ You have added **{current_adds}/{settings['limit']}** members."
        )


@Client.on_message(filters.new_chat_members & filters.group, group=10)
async def track_added_members(bot: Client, message: Message):
    if not getattr(message, "from_user", None):
        return
    for u in message.new_chat_members:
        await mark_new_user(message.chat.id, u.id)

    settings = await get_fa_config(message.chat.id)
    if settings["limit"] == 0:
        return

    adder_id = message.from_user.id
    added_others = [
        u for u in message.new_chat_members if u.id != adder_id and not u.is_bot
    ]
    if not added_others:
        return

    await add_user_score(message.chat.id, adder_id, len(added_others))
    current_adds = await get_user_score(message.chat.id, adder_id)

    if current_adds >= settings["limit"]:
        try:
            await bot.restrict_chat_member(
                message.chat.id,
                adder_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_invite_users=True,
                ),
            )
            msg = await message.reply_text(
                f"🎉 **Congratulations {message.from_user.mention}!**\n\nYou've added {current_adds} members and met the group requirement. **You can now chat freely!**"
            )
            await asyncio.sleep(15)
            await msg.delete()
        except Exception:
            pass


class TTLCache:
    def __init__(self, maxsize: int = 5000, ttl: int = 60):
        self._data, self._maxsize, self._ttl = {}, maxsize, ttl

    def is_rate_limited(self, key: str) -> bool:
        now = time.time()
        if key in self._data and now < self._data[key]:
            return True
        expired = [k for k, exp in self._data.items() if now >= exp]
        for k in expired:
            del self._data[k]
        if len(self._data) >= self._maxsize:
            self._data.clear()
        self._data[key] = now + self._ttl
        return False


WARN_CACHE = TTLCache(ttl=60)


@Client.on_message(
    filters.group
    & ~filters.service
    & ~filters.new_chat_members
    & ~filters.left_chat_member,
    group=-1,
)
async def enforce_force_add(bot: Client, message: Message):
    if not getattr(message, "from_user", None):
        return
    chat_id, user_id = message.chat.id, message.from_user.id

    settings = await get_fa_config(chat_id)
    limit = settings["limit"]
    if limit == 0:
        return
    if settings["mode"] == "new" and not await is_new_user(chat_id, user_id):
        return
    if getattr(message, "text", "") and str(message.text).startswith("/"):
        return

    if user_id in get_admin_list():
        return
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]:
            return
    except Exception:
        pass

    current_adds = await get_user_score(chat_id, user_id)
    if current_adds < limit:
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False, can_invite_users=True
                ),
                until_date=0,
            )
        except Exception:
            pass

        if not WARN_CACHE.is_rate_limited(f"warn_{chat_id}_{user_id}"):
            try:
                warn_msg = await message.reply_text(
                    f"🛑 **Hold on, {message.from_user.mention}!**\n\nYou must add **{limit - current_adds} more member(s)** to this group before you can send messages.\n\n🔇 **You have been restricted from messaging.**",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "📊 How many users have I added?",
                                    callback_data="forceadd_check",
                                )
                            ]
                        ]
                    ),
                )
                await asyncio.sleep(60)
                await warn_msg.delete()
            except Exception:
                pass
        raise StopPropagation


@Client.on_callback_query(filters.regex("^forceadd_check$"))
async def check_adds_button(bot: Client, query: CallbackQuery):
    chat_id, user_id = query.message.chat.id, query.from_user.id
    settings = await get_fa_config(chat_id)
    if settings["limit"] == 0:
        return await query.answer(
            "Force Add is not active in this group.", show_alert=True
        )
    current_adds = await get_user_score(chat_id, user_id)
    if current_adds >= settings["limit"]:
        await query.answer(
            f"✅ You have added {current_adds} members.\nYou are cleared to chat freely!",
            show_alert=True,
        )
    else:
        await query.answer(
            f"⚠️ You have added {current_adds}/{settings['limit']} members.\n\nYou need {settings['limit'] - current_adds} more to chat.",
            show_alert=True,
        )


# ============================================================
# 🛡️ IRONCLAD ANTI-FLOOD SHIELD
# ============================================================
USER_MESSAGE_LOG, MUTED_USERS, MUTED_PROCESSING_LOCK = defaultdict(list), {}, set()
ADMIN_CACHE = {}
_GC_STARTED = False


async def _delete_after(msg: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def is_exempt(
    client: Client, chat_id: int, user_id: int, chat_type: enums.ChatType
) -> bool:
    if user_id in get_admin_list():
        return True
    if chat_type == enums.ChatType.PRIVATE:
        return False
    now = time.time()
    cached = ADMIN_CACHE.get((chat_id, user_id))
    if cached and cached[0] > now:
        return cached[1]
    try:
        member = await client.get_chat_member(chat_id, user_id)
        is_admin = member.status in [
            enums.ChatMemberStatus.ADMINISTRATOR,
            enums.ChatMemberStatus.OWNER,
        ]
        ADMIN_CACHE[(chat_id, user_id)] = (now + 300, is_admin)
        return is_admin
    except Exception:
        return False


async def memory_cleanup_worker():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        for uid in [uid for uid, expiry in MUTED_USERS.items() if expiry <= now]:
            del MUTED_USERS[uid]
        for uid in [
            uid
            for uid, timestamps in USER_MESSAGE_LOG.items()
            if not timestamps or (now - timestamps[-1]) > 60
        ]:
            del USER_MESSAGE_LOG[uid]
        for k in [k for k, (exp, _) in ADMIN_CACHE.items() if exp <= now]:
            del ADMIN_CACHE[k]


@Client.on_message(group=-150)
async def init_antiflood_gc(client: Client, message: Message):
    global _GC_STARTED
    if not _GC_STARTED:
        _GC_STARTED = True
        asyncio.create_task(memory_cleanup_worker())


@Client.on_message(filters.incoming & ~filters.service, group=-50)
async def ironclad_antiflood_shield(client: Client, message: Message):
    if not getattr(message, "from_user", None):
        return
    user_id, chat_id, chat_type = (
        message.from_user.id,
        message.chat.id,
        message.chat.type,
    )
    if await is_exempt(client, chat_id, user_id, chat_type):
        return

    now = time.time()
    if user_id in MUTED_USERS:
        if now < MUTED_USERS[user_id]:
            try:
                await message.delete()
            except (MessageDeleteForbidden, FloodWait, Exception):
                pass
            raise StopPropagation
        else:
            del MUTED_USERS[user_id]

    USER_MESSAGE_LOG[user_id] = [t for t in USER_MESSAGE_LOG[user_id] if now - t <= 3]
    USER_MESSAGE_LOG[user_id].append(now)

    if len(USER_MESSAGE_LOG[user_id]) > 5:
        if user_id in MUTED_PROCESSING_LOCK:
            raise StopPropagation

        MUTED_PROCESSING_LOCK.add(user_id)
        MUTED_USERS[user_id] = now + 600
        USER_MESSAGE_LOG[user_id].clear()

        try:
            await message.delete()
        except Exception:
            pass

        if chat_type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            try:
                await client.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=int(now + 600),
                )
                warn = await client.send_message(
                    chat_id=chat_id,
                    text=f"🛑 **Anti-Flood Triggered!**\n\n{message.from_user.mention} has been muted for **10 minutes** for spamming requests.",
                )
                asyncio.create_task(_delete_after(warn, 10))
            except (ChatAdminRequired, UserAdminInvalid):
                warn = await client.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {message.from_user.mention}, please slow down! Your requests are being ignored for **10 minutes**.",
                )
                asyncio.create_task(_delete_after(warn, 10))
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                logger.error(f"Anti-flood group mute failed: {e}")
        else:
            try:
                warn = await client.send_message(
                    chat_id=chat_id,
                    text="🛑 **Anti-Flood Shield Triggered!**\nYou sent too many commands. Please wait 10 minutes before sending more messages.",
                )
                asyncio.create_task(_delete_after(warn, 10))
            except Exception:
                pass

        MUTED_PROCESSING_LOCK.discard(user_id)
        raise StopPropagation


# ============================================================
# 📝 CUSTOM UI SETTINGS COMMANDS
# ============================================================


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


def format_btn_list(btns):
    if not btns:
        return "No custom buttons."
    return "".join(
        [
            f"**{i}.** `{b['text']}` | Layout: `{b['layout']}` | Color: `{b['color']}`\n"
            for i, b in enumerate(btns, 1)
        ]
    )


@Client.on_message(filters.command("setstarttext") & admin_filter)
async def set_start_text(client, message):
    try:
        if len(message.command) < 2:
            return await message.reply_text("⚠️ **Usage:** `/setstarttext <text>`")
        await update_ui("start_text", message.text.split(None, 1)[1])
        await message.reply_text("✅ **Start Menu Text updated!**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("defaultstarttext") & admin_filter)
async def def_start_text(client, message):
    try:
        await update_ui("start_text", None)
        await message.reply_text("✅ **Start Menu Text reset to default.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("setstartimage") & admin_filter)
async def set_start_img(client, message):
    await process_image_upload(client, message, "start_img", "Start Menu")


@Client.on_message(filters.command("remstartimage") & admin_filter)
async def rem_start_img(client, message):
    try:
        await update_ui("start_img", None)
        await message.reply_text("🗑️ **Start Menu Image removed.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("addstartbutton") & admin_filter)
async def add_start_btn(client, message):
    try:
        btn_text, url = parse_button_cmd(message.text)
        if not btn_text:
            return await message.reply_text(
                "⚠️ **Usage:** `/addstartbutton Text | https://link.com`"
            )
        ui = await get_ui()
        btns = ui.get("start_buttons", [])
        btns.append(
            {"text": btn_text, "url": url, "layout": "belowside", "color": "blue"}
        )
        await update_ui("start_buttons", btns)
        await message.reply_text(
            f"✅ **Start Button Added!**\n\nCurrent Buttons:\n{format_btn_list(btns)}"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


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
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("remstartbutton") & admin_filter)
async def rem_start_btn(client, message):
    try:
        await update_ui("start_buttons", [])
        await message.reply_text("🗑️ **All custom Start Menu buttons cleared.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("sethelptext") & admin_filter)
async def set_help_text(client, message):
    try:
        if len(message.command) < 2:
            return await message.reply_text("⚠️ **Usage:** `/sethelptext <text>`")
        await update_ui("help_text", message.text.split(None, 1)[1])
        await message.reply_text("✅ **Help Menu Text updated!**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("defaulthelptext") & admin_filter)
async def def_help_text(client, message):
    try:
        await update_ui("help_text", None)
        await message.reply_text("✅ **Help Menu Text reset to default.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("sethelpimage") & admin_filter)
async def set_help_img(client, message):
    await process_image_upload(client, message, "help_img", "Help Menu")


@Client.on_message(filters.command("remhelpimage") & admin_filter)
async def rem_help_img(client, message):
    try:
        await update_ui("help_img", None)
        await message.reply_text("🗑️ **Help Menu Image removed.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("addhelpbutton") & admin_filter)
async def add_help_btn(client, message):
    try:
        btn_text, url = parse_button_cmd(message.text)
        if not btn_text:
            return await message.reply_text(
                "⚠️ **Usage:** `/addhelpbutton Text | https://link.com`"
            )
        ui = await get_ui()
        btns = ui.get("help_buttons", [])
        btns.append(
            {"text": btn_text, "url": url, "layout": "belowside", "color": "blue"}
        )
        await update_ui("help_buttons", btns)
        await message.reply_text(
            f"✅ **Help Button Added!**\n\nCurrent Buttons:\n{format_btn_list(btns)}"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


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
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("remhelpbutton") & admin_filter)
async def rem_help_btn(client, message):
    try:
        await update_ui("help_buttons", [])
        await message.reply_text("🗑️ **All custom Help Menu buttons cleared.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("setabouttext") & admin_filter)
async def set_about_text(client, message):
    try:
        if len(message.command) < 2:
            return await message.reply_text("⚠️ **Usage:** `/setabouttext <text>`")
        await update_ui("about_text", message.text.split(None, 1)[1])
        await message.reply_text("✅ **About Menu Text updated!**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("defaultabouttext") & admin_filter)
async def def_about_text(client, message):
    try:
        await update_ui("about_text", None)
        await message.reply_text("✅ **About Menu Text reset to default.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("setaboutimage") & admin_filter)
async def set_about_img(client, message):
    await process_image_upload(client, message, "about_img", "About Menu")


@Client.on_message(filters.command("remaboutimage") & admin_filter)
async def rem_about_img(client, message):
    try:
        await update_ui("about_img", None)
        await message.reply_text("🗑️ **About Menu Image removed.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("addaboutbutton") & admin_filter)
async def add_about_btn(client, message):
    try:
        btn_text, url = parse_button_cmd(message.text)
        if not btn_text:
            return await message.reply_text(
                "⚠️ **Usage:** `/addaboutbutton Text | https://link.com`"
            )
        ui = await get_ui()
        btns = ui.get("about_buttons", [])
        btns.append(
            {"text": btn_text, "url": url, "layout": "belowside", "color": "blue"}
        )
        await update_ui("about_buttons", btns)
        await message.reply_text(
            f"✅ **About Button Added!**\n\nCurrent Buttons:\n{format_btn_list(btns)}"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


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
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("remaboutbutton") & admin_filter)
async def rem_about_btn(client, message):
    try:
        await update_ui("about_buttons", [])
        await message.reply_text("🗑️ **All custom About Menu buttons cleared.**")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


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
    try:
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
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("defaultcleanfilename") & admin_filter)
async def default_clean_words(client: Client, message: Message):
    try:
        if cw_db is not None:
            await cw_db.update_one(
                {"id": "words"}, {"$set": {"use_default": True}}, upsert=True
            )
            await message.reply_text(
                "✅ **Reverted to Default Clean Words!**\n(Standard extensions and default tags will be removed)."
            )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("remcleanfilename") & admin_filter)
async def rem_clean_words(client: Client, message: Message):
    try:
        if cw_db is not None:
            await cw_db.update_one(
                {"id": "words"},
                {"$set": {"list": [], "use_default": False}},
                upsert=True,
            )
            await message.reply_text(
                "🗑️ **All Clean Words Removed.**\nFilenames will now display exactly as they are uploaded."
            )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("setpmtext") & admin_filter)
async def set_pm_text(client: Client, message: Message):
    try:
        text = get_html_text(message)
        if not text:
            return await message.reply_text(
                "Usage: `/setpmtext Your Text Here` (Supports HTML bold, quotes, links, etc.) or reply to a text."
            )
        await save_pm_settings("text", text)
        await message.reply_text(f"✅ PM Text updated successfully to:\n\n{text}")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("rempmtext") & admin_filter)
async def rem_pm_text(client: Client, message: Message):
    try:
        await save_pm_settings(
            "text",
            "<b>🚫 Don't Message Here, Message Here Only!</b>\n\nI do not respond to direct messages in PM.",
        )
        await message.reply_text("✅ PM Text reset to default.")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("setpmimage") & admin_filter)
async def set_pm_image(client: Client, message: Message):
    try:
        if not getattr(message.reply_to_message, "photo", None):
            return await message.reply_text(
                "⚙️ **Usage:** Reply to a photo with `/setpmimage`"
            )
        status = await message.reply_text("⏳ **Uploading to Cloud...**")
        url = await upload_image_safely(client, message.reply_to_message)
        if url:
            await save_pm_settings("image", url)
            await status.edit_text(
                "✅ **PM Image updated successfully!**\n*(Saved permanently to cloud)*"
            )
        else:
            await save_pm_settings("image", message.reply_to_message.photo.file_id)
            await status.edit_text(
                "⚠️ Cloud blocked. **PM Image updated successfully!**\n*(Saved as Telegram File_ID)*"
            )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("rempmimage") & admin_filter)
async def rem_pm_image(client: Client, message: Message):
    try:
        await save_pm_settings("image", None)
        await message.reply_text("✅ PM Image removed.")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("setpmbutton") & admin_filter)
async def set_pm_button(client: Client, message: Message):
    try:
        if len(message.command) < 2:
            return await message.reply_text("Usage: `/setpmbutton Text | URL | Color`")
        args = message.text.split(None, 1)[1].split("|")
        if len(args) < 2:
            return await message.reply_text(
                "❌ Invalid format. Use: `Text | URL | Color`"
            )
        btn_text, btn_url, btn_color = (
            args[0].strip(),
            args[1].strip(),
            args[2].strip().lower() if len(args) > 2 else "normal",
        )
        if btn_color not in ["green", "red", "blue", "normal"]:
            btn_color = "normal"
        await save_pm_settings("button_text", btn_text)
        await save_pm_settings("button_url", btn_url)
        await save_pm_settings("button_color", btn_color)
        await message.reply_text(
            f"✅ PM Button updated!\nText: {btn_text}\nURL: {btn_url}\nColor: {btn_color}"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.command("rempmbutton") & admin_filter)
async def rem_pm_button(client: Client, message: Message):
    try:
        await save_pm_settings("button_text", None)
        await save_pm_settings("button_url", None)
        await message.reply_text("✅ PM Button removed completely.")
    except Exception as e:
        await message.reply_text(f"❌ **Error:**\n`{str(e)}`")


@Client.on_message(filters.private & filters.text & filters.incoming, group=-100)
async def pm_auto_reply(client: Client, message: Message):
    if message.text.startswith("/") or re.search(
        r"(https?://)?(t(elegram)?\.(me|dog)|telegram\.org)/(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$",
        message.text,
    ):
        return
    uid = get_uid(message)
    if getattr(info, "REPAIR_MODE", False) and uid not in get_admin_list():
        return
    if uid and await _plugin_db.is_banned(uid):
        return

    try:
        settings = await get_pm_settings()
        text = settings.get(
            "text",
            "<b>🚫 Don't Message Here, Message Here Only!</b>\n\nI do not respond to direct messages in PM.",
        )
        pm_img = settings.get("image", getattr(info, "NOT_FOUND_IMG", None))
        btn_text, btn_url, btn_color_str = (
            settings.get("button_text", "💬 Message Here Only"),
            settings.get("button_url", "https://t.me/Sandalwood_Kannada_Group"),
            settings.get("button_color", "green"),
        )

        reply_markup = None
        if btn_text and btn_url:
            color_map = {
                "green": BTN_SUCCESS,
                "red": BTN_DANGER,
                "blue": BTN_PRIMARY,
                "normal": None,
            }
            btn_style = color_map.get(btn_color_str)
            if btn_style:
                reply_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(btn_text, url=btn_url, style=btn_style)]]
                )
            else:
                reply_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(btn_text, url=btn_url)]]
                )

        if pm_img and str(pm_img).lower() != "none":
            await message.reply_photo(
                photo=pm_img,
                caption=text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await message.reply_text(
                text=text, reply_markup=reply_markup, parse_mode=enums.ParseMode.HTML
            )
    except Exception as e:
        logger.error(f"PM Reply Error: {e}")
