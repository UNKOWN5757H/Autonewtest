import asyncio
import builtins
import inspect
import re
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Set, Tuple, Union

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
from pyrogram.types import CallbackQuery, Message

# Ensure typing aliases exist in builtins before importing other plugins
for _t_name, _t_obj in (
    ("Tuple", Tuple),
    ("List", List),
    ("Dict", Dict),
    ("Optional", Optional),
    ("Union", Union),
    ("Set", Set),
    ("Any", Any),
):
    if not hasattr(builtins, _t_name):
        setattr(builtins, _t_name, _t_obj)

import info
from database.ia_filterdb import get_file_details
from database.users_chats_db import db as _db
from info import AUTH_CHANNEL, CUSTOM_FILE_CAPTION, REQ_CHANNEL
from utils import get_size, is_subscribed, temp

try:
    from plugins.uibuttons import (
        _invalidate_pm_filter_clean_cache,
        build_cleanwords_panel_ui,
        build_interactive_help_section,
        cw_db,
        get_default_file_keyboard,
        get_deleted_file_notify_keyboard,
        get_pm_autoreply_keyboard,
        get_pm_settings,
        is_global_admin,
        is_user_banned,
        is_waiting_for_cmd_input,
        safe_parse_text_and_markup,
        save_pm_settings,
        upload_image_safely,
    )
except ImportError:
    from uibuttons import (
        _invalidate_pm_filter_clean_cache,
        build_cleanwords_panel_ui,
        build_interactive_help_section,
        cw_db,
        get_default_file_keyboard,
        get_deleted_file_notify_keyboard,
        get_pm_autoreply_keyboard,
        get_pm_settings,
        is_global_admin,
        is_user_banned,
        is_waiting_for_cmd_input,
        safe_parse_text_and_markup,
        save_pm_settings,
        upload_image_safely,
    )

logger = getLogger(__name__)
logger.setLevel(ERROR)

# Safe optional import for ForceSub
try:
    from plugins.fsub import ForceSub
except Exception as _fsub_err:
    logger.error(f"Could not import ForceSub from plugins.fsub: {_fsub_err}")
    ForceSub = None


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

_SEND_MSG_PARAMS = set(inspect.signature(Client.send_message).parameters.keys())
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
):
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
):
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


async def safe_send_message(
    client: Client,
    chat_id: int,
    text: str,
    disable_web_page_preview: Optional[bool] = True,
    **kwargs,
):
    compat_kw = _prepare_compat_kwargs(
        kwargs, _SEND_MSG_PARAMS, disable_preview=disable_web_page_preview
    )
    try:
        return await client.send_message(chat_id=chat_id, text=text, **compat_kw)
    except TypeError:
        compat_kw.pop("disable_web_page_preview", None)
        compat_kw.pop("link_preview_options", None)
        compat_kw.pop("reply_parameters", None)
        compat_kw.pop("reply_to_message_id", None)
        return await client.send_message(chat_id=chat_id, text=text, **compat_kw)
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None


async def safe_answer(
    query: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
    url: Optional[str] = None,
):
    """Safely answers a CallbackQuery without ever raising QueryIdInvalid."""
    if not query:
        return
    try:
        kwargs: Dict[str, Any] = {"show_alert": show_alert}
        if text is not None:
            kwargs["text"] = str(text)[:200]
        if url is not None:
            kwargs["url"] = url
        await query.answer(**kwargs)
    except (QueryIdInvalid, Exception):
        pass


# ============================================================
# 🛠️ HELPER FUNCTIONS
# ============================================================
def get_html_text(message: Message) -> Optional[str]:
    if message.reply_to_message:
        reply_msg = message.reply_to_message
        raw = reply_msg.text or reply_msg.caption
        if raw:
            return getattr(raw, "html", str(raw))
    if message.text and len(message.command) > 1:
        html_text = getattr(message.text, "html", str(message.text))
        return re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s+", "", html_text, count=1)
    return None


def parse_timer(var_name: str, default_time: int = 1800) -> int:
    try:
        global_switch = getattr(info, "AUTO_DELETE", True)
        if str(global_switch).strip().lower() in ["false", "off", "0", "no"]:
            return 0
        val = getattr(info, "AUTO_DELETE_TIME", getattr(info, var_name, default_time))
        if isinstance(val, bool):
            return 1800 if val else 0
        parsed_time = int(str(val).strip())
        return 1800 if 0 < parsed_time < 10 else max(0, parsed_time)
    except Exception:
        return 1800


async def run_force_sub(client: Client, message: Message, file_id=False, mode=False):
    if not callable(ForceSub):
        return True
    try:
        return await ForceSub(client, message, file_id=file_id, mode=mode)
    except TypeError:
        try:
            return await ForceSub(client, message, file_id=file_id)
        except TypeError:
            return await ForceSub(client, message)
    except Exception as e:
        logger.error(f"ForceSub error: {e}")
        return True


# ============================================================
# ⏳ SILENT AUTO-DELETE ENGINE
# ============================================================
AUTO_DELETE_TASKS = set()


async def auto_delete_all_messages(
    client: Client, chat_id: int, is_pm: bool, delay: int, *messages
):
    await asyncio.sleep(delay)

    msg_ids = [m.id for m in messages if m is not None and hasattr(m, "id") and m.id]
    if msg_ids:
        try:
            await client.delete_messages(chat_id, msg_ids)
        except Exception:
            for msg in messages:
                if msg:
                    try:
                        await msg.delete()
                    except Exception:
                        pass

    if is_pm:
        notify_text = (
            dict_del_msg := getattr(info, "DEL_MSG", None)
        ) or "<b>Successfully deleted your file, if you want request again in group.</b>"
        markup = get_deleted_file_notify_keyboard()
        try:
            await safe_send_message(
                client,
                chat_id,
                text=notify_text,
                reply_markup=markup,
                parse_mode=enums.ParseMode.HTML,
            )
        except Exception:
            pass


def schedule_pm_auto_delete(
    client: Client, chat_id: int, is_pm: bool, delay: int, *messages
):
    if delay <= 0:
        return
    task = asyncio.create_task(
        auto_delete_all_messages(client, chat_id, is_pm, delay, *messages)
    )
    AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(AUTO_DELETE_TASKS.discard)


# ============================================================
# 📦 SHARED FILE SENDER LOGIC & FILE CALLBACK HANDLER
# ============================================================
async def send_requested_file(
    client: Client,
    user_id: int,
    file_id: str,
    ident: str = "file",
    reply_target: Message = None,
    query: CallbackQuery = None,
):
    files_ = await get_file_details(file_id)
    if not files_:
        if query:
            await safe_answer(query, "⚠️ No such file exists.", show_alert=True)
        elif reply_target:
            await safe_reply_text(
                reply_target,
                "⚠️ No such file exists.",
                parse_mode=enums.ParseMode.HTML,
            )
        return

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
        files.get("full_file_id", files.get("file_id", file_id))
        if isinstance(files, dict)
        else getattr(files, "full_file_id", getattr(files, "file_id", file_id))
    )

    custom_cap = getattr(info, "CUSTOM_FILE_CAPTION", CUSTOM_FILE_CAPTION)
    if custom_cap:
        try:
            f_caption = custom_cap.format(
                file_name="" if title == "Unknown" else title,
                file_size="" if size == "0B" else size,
                file_caption="" if not f_caption else f_caption,
            )
        except Exception:
            pass

    if not f_caption:
        f_caption = f"<b>{title}</b>"
    elif not f_caption.strip().startswith("<b>"):
        f_caption = f"<b>{f_caption}</b>"

    if getattr(info, "CAPTION_PLUS", None):
        f_caption += f"\n\n{info.CAPTION_PLUS}"

    default_btn = get_default_file_keyboard()
    final_caption, final_keyboard = safe_parse_text_and_markup(f_caption, default_btn)
    protect = True if ident in ["filep", "checksubp"] else False

    # Send optional Info Message / Info Image before delivering the file if configured
    info_msg_txt = getattr(info, "INFO_MSG", None)
    info_img_val = getattr(info, "INFO_IMG", None)
    pre_info_msg = None
    if info_msg_txt or info_img_val:
        try:
            if info_img_val and str(info_img_val).lower() != "none":
                pre_info_msg = await client.send_photo(
                    chat_id=user_id,
                    photo=info_img_val,
                    caption=info_msg_txt or "",
                    parse_mode=enums.ParseMode.HTML,
                )
            elif info_msg_txt:
                pre_info_msg = await safe_send_message(
                    client,
                    chat_id=user_id,
                    text=info_msg_txt,
                    parse_mode=enums.ParseMode.HTML,
                )
        except Exception:
            pre_info_msg = None

    msg = None
    try:
        msg = await client.send_cached_media(
            chat_id=user_id,
            file_id=db_file_id,
            caption=final_caption,
            reply_markup=final_keyboard,
            protect_content=protect,
            parse_mode=enums.ParseMode.HTML,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        msg = await client.send_cached_media(
            chat_id=user_id,
            file_id=db_file_id,
            caption=final_caption,
            reply_markup=final_keyboard,
            protect_content=protect,
            parse_mode=enums.ParseMode.HTML,
        )
    except (UserIsBlocked, PeerIdInvalid):
        if query and getattr(temp, "U_NAME", None):
            return await safe_answer(
                query, url=f"https://t.me/{temp.U_NAME}?start={ident}_{file_id}"
            )
        return
    except Exception:
        try:
            msg = await client.send_document(
                chat_id=user_id,
                document=db_file_id,
                caption=final_caption,
                reply_markup=final_keyboard,
                protect_content=protect,
                parse_mode=enums.ParseMode.HTML,
            )
        except (UserIsBlocked, PeerIdInvalid):
            if query and getattr(temp, "U_NAME", None):
                return await safe_answer(
                    query, url=f"https://t.me/{temp.U_NAME}?start={ident}_{file_id}"
                )
            return
        except Exception as err:
            if query:
                return await safe_answer(query, f"⚠️ Error: {err}", show_alert=True)
            if reply_target:
                return await safe_reply_text(
                    reply_target,
                    f"⚠️ <b>Error:</b>\n<code>{err}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

    if query:
        await safe_answer(query, "📥 File sent to your PM!")

    if msg:
        try:
            delete_timer = parse_timer("FILE_AUTO_DELETE", 1800)
            m, s = divmod(delete_timer, 60)
            time_str = f"{m} Minutes {s} Seconds" if s > 0 else f"{m} Minutes"

            warn_text = (
                f"<b>📢 Please Note\n\n"
                f"✅ The above file will be autodeleted in {time_str} to avoid copyright issues.\n\n"
                f"✅ Please forward this file to your saved messages and start downloading from there.\n\n"
                f"Tᴇᴀᴍ: @KR_Picture</b>"
            )
            k = await safe_send_message(
                client,
                chat_id=user_id,
                text=warn_text,
                reply_to_message_id=msg.id,
                parse_mode=enums.ParseMode.HTML,
            )

            if delete_timer > 0:
                schedule_pm_auto_delete(
                    client, user_id, True, delete_timer, pre_info_msg, msg, k
                )
        except Exception as e:
            logger.error(f"Auto-delete warning failed to send: {e}")


@Client.on_callback_query(filters.regex(r"^(file|checksub)"))
async def file_cb_handler(client: Client, query: CallbackQuery):
    uid = (
        query.from_user.id
        if query.from_user
        else getattr(query.message.sender_chat, "id", 0)
    )
    if getattr(info, "REPAIR_MODE", False) and not await is_global_admin(uid):
        return await safe_answer(query, "🛠️ Bot is under maintenance!", show_alert=True)

    if (
        query.data.startswith("checksub")
        and (AUTH_CHANNEL or REQ_CHANNEL)
        and not await is_subscribed(client, query)
    ):
        return await safe_answer(
            query,
            "⚠️ Please join our channel first, then click Try Again! Team: @KR_PICTURE",
            show_alert=True,
        )

    try:
        ident, file_id = query.data.split("#", 1)
    except ValueError:
        return await safe_answer(query, "⚠️ Invalid button data!", show_alert=True)

    await send_requested_file(
        client=client,
        user_id=query.from_user.id,
        file_id=file_id,
        ident=ident,
        query=query,
    )


# ============================================================
# ⚙️ CLEAN FILENAME COMMANDS (ADMIN ONLY)
# ============================================================
@Client.on_message(
    filters.command(["listcleanfilename", "cleanfilenames", "cleanwords"])
    & filters.incoming,
    group=-2,
)
async def list_clean_words_cmd(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    text, markup = await build_cleanwords_panel_ui()
    await safe_reply_text(
        message,
        text,
        reply_markup=markup,
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setcleanfilename") & filters.incoming, group=-2)
async def set_clean_words(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2:
        text, markup = await build_cleanwords_panel_ui()
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/setcleanfilename word1, word2, @ChannelName</code>\n\n"
            + text,
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
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
        _invalidate_pm_filter_clean_cache()
        text, markup = await build_cleanwords_panel_ui()
        await safe_reply_text(
            message,
            f"✅ <b>Clean Words Updated Successfully!</b>\n\n{text}",
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("defaultcleanfilename") & filters.incoming, group=-2)
async def default_clean_words(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if cw_db is not None:
        await cw_db.update_one(
            {"id": "words"}, {"$set": {"use_default": True}}, upsert=True
        )
        _invalidate_pm_filter_clean_cache()
        text, markup = await build_cleanwords_panel_ui()
        await safe_reply_text(
            message,
            f"✅ <b>Reverted to Default Clean Words!</b>\n\n{text}",
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )


@Client.on_message(filters.command("remcleanfilename") & filters.incoming, group=-2)
async def rem_clean_words(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if cw_db is not None:
        await cw_db.update_one(
            {"id": "words"}, {"$set": {"list": [], "use_default": False}}, upsert=True
        )
        _invalidate_pm_filter_clean_cache()
        text, markup = await build_cleanwords_panel_ui()
        await safe_reply_text(
            message,
            f"🗑️ <b>All Clean Words Removed!</b>\n\n{text}",
            reply_markup=markup,
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# ⚙️ PM REPLY SETTINGS SETUP COMMANDS (ADMIN ONLY)
# ============================================================
@Client.on_message(filters.command("setpmtext") & filters.incoming, group=-2)
async def set_pm_text(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        text = get_html_text(message)
        if not text:
            return await safe_reply_text(
                message,
                "⚙️ <b>Usage:</b> <code>/setpmtext Your Text Here</code> or reply to a message.",
                parse_mode=enums.ParseMode.HTML,
            )
        await save_pm_settings("text", text)
        await safe_reply_text(
            message,
            f"✅ <b>PM Text updated successfully to:</b>\n\n{text}",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(message, f"⚠️ Error: {e}")


@Client.on_message(filters.command("rempmtext") & filters.incoming, group=-2)
async def rem_pm_text(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await save_pm_settings(
        "text",
        "<b>🚫 Don't Message Here, Message Here Only!</b>\n\nI do not respond to direct messages in PM. Please join our official group to request and download movies.",
    )
    await safe_reply_text(
        message,
        "✅ <b>PM Text reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setpmimage") & filters.incoming, group=-2)
async def set_pm_image(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    if not message.reply_to_message or not message.reply_to_message.photo:
        return await safe_reply_text(
            message,
            "⚙️ <b>Usage:</b> Reply to a photo with <code>/setpmimage</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message, "⏳ <b>Uploading to Cloud...</b>", parse_mode=enums.ParseMode.HTML
    )
    url = await upload_image_safely(client, message.reply_to_message)
    if url:
        await save_pm_settings("image", url)
        if status:
            await safe_edit_text(
                status,
                "✅ <b>PM Image updated successfully!</b>",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        file_id = message.reply_to_message.photo.file_id
        await save_pm_settings("image", file_id)
        if status:
            await safe_edit_text(
                status,
                "⚠️ Cloud upload failed. <b>PM Image saved using Telegram File_ID!</b>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_message(filters.command("rempmimage") & filters.incoming, group=-2)
async def rem_pm_image(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await save_pm_settings("image", None)
    await safe_reply_text(
        message, "✅ <b>PM Image removed.</b>", parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.command("setpmbutton") & filters.incoming, group=-2)
async def set_pm_button(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    try:
        if len(message.command) < 2:
            return await safe_reply_text(
                message,
                "⚙️ <b>Usage:</b> <code>/setpmbutton Text | URL | Color</code>",
                parse_mode=enums.ParseMode.HTML,
            )
        args = message.text.split(None, 1)[1].split("|")
        if len(args) < 2:
            return await safe_reply_text(
                message,
                "❌ Invalid format. Use: <code>Text | URL | Color</code>",
                parse_mode=enums.ParseMode.HTML,
            )

        btn_text, btn_url = args[0].strip(), args[1].strip()
        btn_color = args[2].strip().lower() if len(args) > 2 else "normal"
        if btn_color not in ["green", "red", "blue", "normal"]:
            btn_color = "normal"

        await save_pm_settings("button_text", btn_text)
        await save_pm_settings("button_url", btn_url)
        await save_pm_settings("button_color", btn_color)
        await safe_reply_text(
            message,
            f"✅ <b>PM Button updated!</b>\n<b>Text:</b> {btn_text}\n<b>URL:</b> {btn_url}\n<b>Color:</b> {btn_color}",
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        await safe_reply_text(message, f"⚠️ Error: {e}")


@Client.on_message(filters.command("rempmbutton") & filters.incoming, group=-2)
async def rem_pm_button(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await save_pm_settings("button_text", None)
    await save_pm_settings("button_url", None)
    await safe_reply_text(
        message,
        "✅ <b>PM Button removed completely.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(
    filters.command(["pmautoreply", "pmreply"]) & filters.incoming, group=-2
)
async def toggle_pm_autoreply_cmd(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    settings = await get_pm_settings()
    cur = bool(settings.get("enabled", True))
    if len(message.command) > 1:
        arg = message.command[1].strip().lower()
        new_val = (
            True
            if arg in ["on", "enable", "true", "1", "yes"]
            else False if arg in ["off", "disable", "false", "0", "no"] else (not cur)
        )
    else:
        new_val = not cur

    await save_pm_settings("enabled", new_val)
    text, markup = await build_interactive_help_section(
        client,
        "helps_pmautoreply",
        uid,
        message.from_user.mention if message.from_user else "Admin",
        message,
    )
    await safe_reply_text(
        message,
        text,
        reply_markup=markup,
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 📩 PM AUTO-REPLY ENGINE
# ============================================================
@Client.on_message(
    filters.private & filters.incoming & ~filters.bot & ~filters.service,
    group=1,
)
async def pm_auto_reply(client: Client, message: Message):
    raw_txt = (message.text or message.caption or "").strip()
    if raw_txt.startswith("/"):
        return

    if is_waiting_for_cmd_input(message.chat.id):
        return

    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )

    if getattr(info, "REPAIR_MODE", False) and not await is_global_admin(uid):
        return
    if message.from_user and await is_user_banned(uid):
        return

    settings = await get_pm_settings()
    if not settings.get("enabled", True):
        return

    if await is_global_admin(uid):
        if message.forward_from_chat or getattr(message, "forward_origin", None):
            return
        if message.document or message.video or message.audio:
            return
        if not settings.get("reply_admins", True):
            return

    text = settings.get("text", "<b>🚫 Don't Message Here, Message Here Only!</b>")
    pm_img = settings.get("image", getattr(info, "NOT_FOUND_IMG", None))
    btn_text = settings.get("button_text", "💬 Message Here Only")
    btn_url = settings.get("button_url", "https://t.me/Sandalwood_Kannada_Group")
    btn_color_str = str(settings.get("button_color", "red")).lower()

    default_markup = get_pm_autoreply_keyboard(btn_text, btn_url, btn_color_str)

    final_text, final_markup = safe_parse_text_and_markup(text, default_markup)
    final_text = final_text or text

    try:
        if pm_img and str(pm_img).lower() != "none" and len(final_text) <= 1024:
            try:
                await message.reply_photo(
                    photo=pm_img,
                    caption=final_text,
                    reply_markup=final_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            except Exception:
                pass
        await safe_reply_text(
            message,
            text=final_text,
            reply_markup=final_markup,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"PM Reply Error: {e}")
