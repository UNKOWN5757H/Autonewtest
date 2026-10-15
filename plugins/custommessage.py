import asyncio
import inspect
import re
from logging import ERROR, getLogger
from typing import Optional

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    Forbidden,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import Message

import info

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


# ============================================================
# ⚙️ MONGODB PERMANENT SETTINGS
# ============================================================
_custom_db = None
_global_settings_db = None
_CUSTOM_BOOT_LOADED = False

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _custom_db = _BOT_DB["custom_captions"]
    _global_settings_db = _BOT_DB["global_bot_settings"]
except Exception as e:
    logger.error(f"Failed to init custommessage DB: {e}")

# Sync map between info attributes and global_bot_settings keys
_REVERSE_GLOBAL_MAP = {
    "FSUB_IMG": "fsub_img",
    "FSUB_MSG": "fsub_msg",
    "NOT_FOUND_IMG": "not_found_img",
    "NOT_FOUND_MSG": "not_found_text",
    "INFO_MSG": "info_msg",
    "INFO_IMG": "info_img",
    "DEL_MSG": "del_msg",
    "DEL_IMG": "del_img",
}


async def save_msg_data(key: str, value):
    setattr(info, key, value)
    if _custom_db is not None:
        try:
            await _custom_db.update_one(
                {"_id": "settings"}, {"$set": {key: value}}, upsert=True
            )
        except Exception as e:
            logger.error(f"Error saving {key} to custom_captions: {e}")

    global_key = _REVERSE_GLOBAL_MAP.get(key)
    if global_key:
        if _global_settings_db is not None:
            try:
                await _global_settings_db.update_one(
                    {"_id": "bot_config"}, {"$set": {global_key: value}}, upsert=True
                )
            except Exception:
                pass
        try:
            from plugins.custom_settings import update_bot_settings

            await update_bot_settings(global_key, value)
        except Exception:
            pass


@Client.on_message(filters.all, group=-998)
async def load_custom_messages_on_boot(client: Client, message: Message):
    """Automatically restores saved custom messages & images into `info` on bot startup."""
    global _CUSTOM_BOOT_LOADED
    if _CUSTOM_BOOT_LOADED:
        return
    _CUSTOM_BOOT_LOADED = True
    if _custom_db is None:
        return
    try:
        doc = await _custom_db.find_one({"_id": "settings"})
        if doc:
            for k, v in doc.items():
                if k != "_id" and v is not None:
                    setattr(info, k, v)
    except Exception as e:
        logger.error(f"Failed to load custom messages on boot: {e}")


# ============================================================
# 👑 FULL POWER ADMIN CHECKER (GLOBAL + CHAT + ANONYMOUS)
# ============================================================
def get_admin_list():
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


# ============================================================
# ☁️ CLOUD IMAGE UPLOADER
# ============================================================
def _upload_sync(file_bytes):
    headers = {"User-Agent": "Mozilla/5.0"}
    endpoints = [
        (
            "https://catbox.moe/user/api.php",
            {"fileToUpload": ("img.jpg", file_bytes, "image/jpeg")},
            {"reqtype": "fileupload"},
        ),
        (
            "https://graph.org/upload",
            {"file": ("img.jpg", file_bytes, "image/jpeg")},
            None,
        ),
        ("https://envs.sh", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        ("https://x0.at", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        ("https://ttm.sh", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        ("https://0x0.st", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
    ]
    for url, files_dict, data_dict in endpoints:
        try:
            res = requests.post(
                url, data=data_dict, files=files_dict, headers=headers, timeout=8
            )
            if res.status_code in [200, 201]:
                if url == "https://graph.org/upload":
                    return "https://graph.org" + res.json()[0]["src"]
                elif res.text.strip().startswith("http"):
                    return res.text.strip()
        except Exception:
            continue
    return None


async def upload_image_safely(client: Client, message: Message):
    try:
        file_io = await client.download_media(message, in_memory=True)
        if not file_io:
            return None
        return await asyncio.to_thread(_upload_sync, file_io.getvalue())
    except Exception:
        return None


# ============================================================
# 🛠️ TEXT & IMAGE SETTING PROCESSORS
# ============================================================
async def process_text_setting(
    client: Client, message: Message, var_name: str, setting_name: str
):
    if not await is_user_admin(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) > 1 and message.command[1].strip().lower() in [
        "off",
        "reset",
        "clear",
        "default",
        "none",
    ]:
        await save_msg_data(var_name, None)
        return await safe_reply_text(
            message,
            f"✅ <b>{setting_name} has been REMOVED.</b>\nSystem will now use the default message.",
            parse_mode=enums.ParseMode.HTML,
        )

    raw_text = None
    if (
        message.reply_to_message
        and len(getattr(message, "command", [])) == 1
        and (message.reply_to_message.text or message.reply_to_message.caption)
    ):
        replied = message.reply_to_message
        content = replied.text or replied.caption
        raw_text = getattr(content, "html", str(content)).strip()
    elif message.text and len(message.command) > 1:
        html_text = getattr(message.text, "html", str(message.text))
        raw_text = re.sub(
            r"^/\w+(?:@[a-zA-Z0-9_]+)?\s+", "", html_text, count=1
        ).strip()

    if not raw_text:
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b>\n"
            f"• Reply to a text message with <code>/{message.command[0]}</code>\n"
            f"• Or send <code>/{message.command[0]} Your Text Here</code>\n"
            f"• Or use <code>/{message.command[0]} off</code> to reset to default.",
            parse_mode=enums.ParseMode.HTML,
        )

    await save_msg_data(var_name, raw_text)
    await safe_reply_text(
        message,
        f"✅ <b>{setting_name} successfully updated!</b>\n\n"
        f"<b>New Message:</b>\n{raw_text}",
        disable_web_page_preview=True,
        parse_mode=enums.ParseMode.HTML,
    )


async def process_image_setting(
    client: Client, message: Message, var_name: str, setting_name: str
):
    if not await is_user_admin(client, message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Admin access required.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) > 1 and message.command[1].strip().lower() in [
        "off",
        "reset",
        "clear",
        "default",
        "none",
    ]:
        await save_msg_data(var_name, None)
        return await safe_reply_text(
            message,
            f"✅ <b>{setting_name} has been REMOVED.</b>\nSystem will now use the default image.",
            parse_mode=enums.ParseMode.HTML,
        )

    target_msg = (
        message.reply_to_message
        if (message.reply_to_message and message.reply_to_message.photo)
        else (message if message.photo else None)
    )

    if target_msg is None and len(message.command) > 1:
        arg = message.text.split(None, 1)[1].strip()
        if arg.startswith(("http://", "https://")) or len(arg) > 20:
            await save_msg_data(var_name, arg)
            return await safe_reply_text(
                message,
                f"✅ <b>{setting_name} updated!</b>",
                parse_mode=enums.ParseMode.HTML,
            )

    if not target_msg or not target_msg.photo:
        return await safe_reply_text(
            message,
            f"⚙️ <b>Usage:</b>\n"
            f"• Reply to a photo with <code>/{message.command[0]}</code> to set it.\n"
            f"• Or use <code>/{message.command[0]} off</code> to disable.",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message, "⏳ <b>Uploading to Cloud...</b>", parse_mode=enums.ParseMode.HTML
    )
    url = await upload_image_safely(client, target_msg)
    if url:
        await save_msg_data(var_name, url)
        if status:
            await safe_edit_text(
                status,
                f"✅ <b>{setting_name} updated to Cloud Image!</b>",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        photo_id = target_msg.photo.file_id
        await save_msg_data(var_name, photo_id)
        if status:
            await safe_edit_text(
                status,
                f"⚠️ Cloud upload failed. <b>{setting_name} saved as Telegram File ID!</b>",
                parse_mode=enums.ParseMode.HTML,
            )


# ============================================================
# 🎯 COMMAND HANDLERS (SINGLE SOURCE FOR CUSTOM MESSAGES)
# ============================================================
@Client.on_message(filters.command("infomsg") & filters.incoming, group=-2)
async def set_info_msg(bot: Client, message: Message):
    await process_text_setting(bot, message, "INFO_MSG", "Info Message")


@Client.on_message(filters.command("infoimg") & filters.incoming, group=-2)
async def set_info_img(bot: Client, message: Message):
    await process_image_setting(bot, message, "INFO_IMG", "Info Image")


@Client.on_message(filters.command("delmsg") & filters.incoming, group=-2)
async def set_del_msg(bot: Client, message: Message):
    await process_text_setting(bot, message, "DEL_MSG", "Delete Message")


@Client.on_message(filters.command("delimg") & filters.incoming, group=-2)
async def set_del_img(bot: Client, message: Message):
    await process_image_setting(bot, message, "DEL_IMG", "Delete Image")


@Client.on_message(filters.command("notfoundmsg") & filters.incoming, group=-2)
async def set_notfound_msg(bot: Client, message: Message):
    await process_text_setting(bot, message, "NOT_FOUND_MSG", "File Not Found Message")


@Client.on_message(filters.command("notfoundimg") & filters.incoming, group=-2)
async def set_notfound_img(bot: Client, message: Message):
    await process_image_setting(bot, message, "NOT_FOUND_IMG", "File Not Found Image")


@Client.on_message(filters.command("fsubmsg") & filters.incoming, group=-2)
async def set_fsub_msg(bot: Client, message: Message):
    await process_text_setting(bot, message, "FSUB_MSG", "Force Subscribe Message")


@Client.on_message(filters.command("fsubimg") & filters.incoming, group=-2)
async def set_fsub_img(bot: Client, message: Message):
    await process_image_setting(bot, message, "FSUB_IMG", "Force Subscribe Image")
