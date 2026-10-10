import ast
import asyncio
import os
import platform
import re
import shutil
from logging import ERROR, getLogger

from database.connections_mdb import active_connection
from database.filters_mdb import add_filter, delete_filter, find_filter, get_filters
from database.ia_filterdb import Media as _Media
from database.ia_filterdb import get_search_results
from database.plugin_dbs import plugin_db as _plugin_db
from database.users_chats_db import db as _db
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, StopPropagation, emoji, enums, filters
from pyrogram.enums import ButtonStyle
from pyrogram.errors import (
    FloodWait,
    InputUserDeactivated,
    MessageNotModified,
    PeerIdInvalid,
    UserDeactivatedBan,
    UserIsBlocked,
)
from pyrogram.errors.exceptions.bad_request_400 import QueryIdInvalid
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultCachedDocument,
    Message,
)

import info

# 👑 IMPORTING THE GOD-TIER UNIVERSAL ADMIN ENGINE
from utils import admin_filter, get_admin_list, get_size, get_uid, is_subscribed, temp

logger = getLogger(__name__)
logger.setLevel(ERROR)

try:
    import psutil
except ImportError:
    psutil = None

# ============================================================
# ⚙️ MONGODB FAILOVER LAYER
# ============================================================
_filter_col = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _filter_col = _BOT_DB["filters"]
except Exception as e:
    logger.error(f"Filters Direct DB fallback init failed: {e}")


# ============================================================
# 🛡️ ADMIN VALIDATION FOR FILTERS
# ============================================================
async def is_admin(client: Client, message: Message, grp_id: int = None) -> bool:
    if get_uid(message) in get_admin_list():
        return True
    target_chat_id = grp_id or message.chat.id
    try:
        member = await client.get_chat_member(target_chat_id, get_uid(message))
        return member.status in [
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
        ]
    except Exception:
        return False


async def get_target_group(client: Client, message: Message):
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        grp_id = message.chat.id
    else:
        grp_id = await active_connection(str(get_uid(message)))
        if not grp_id:
            await message.reply_text(
                "⚠️ **You are not connected to any active group!**\n\nUse `/connect <group_id>` to connect to a group first."
            )
            return None, False
    if not await is_admin(client, message, grp_id=grp_id):
        await message.reply_text(
            "⚠️ **You must be an admin of the connected group to use this command.**"
        )
        return None, False
    return grp_id, True


# ============================================================
# 🎯 SEARCH INTERCEPTOR
# ============================================================
@Client.on_message(filters.command("start") & filters.private, group=-99)
async def intercept_direct_search(client: Client, message: Message):
    if len(message.command) > 1 and message.command[1].startswith("search_"):
        query = message.command[1].replace("search_", "").replace("_", " ")
        try:
            from plugins.pm_filter import auto_filter

            message.text = query
            await auto_filter(client, message)
        except Exception as e:
            logger.error(f"Failed to intercept search: {e}")
            await message.reply_text(
                f"🔍 **Search Query:** `{query}`\n\nTap the text above to copy it, then send it to me to search!"
            )
        raise StopPropagation


# ============================================================
# 🗄️ DUAL-LAYER DATABASE ADAPTER
# ============================================================
async def db_add_filter(
    grp_id: int, keyword: str, text: str, btn: str, alert: str, fileid: str
):
    try:
        return await add_filter(grp_id, keyword, text, btn, alert, fileid)
    except Exception as e:
        logger.warning(f"Layer 1 add_filter failed, attempting direct DB: {e}")
    if _filter_col is not None:
        await _filter_col.update_one(
            {"chat_id": int(grp_id), "keyword": keyword},
            {"$set": {"text": text, "btn": btn, "alert": alert, "fileid": fileid}},
            upsert=True,
        )


async def db_find_filter(grp_id: int, keyword: str):
    try:
        reply_text, btn, alert, fileid = await find_filter(grp_id, keyword)
        if reply_text or (fileid and fileid != "None"):
            return reply_text, btn, alert, fileid
    except Exception as e:
        logger.warning(f"Layer 1 find_filter failed, attempting direct DB: {e}")
    if _filter_col is not None:
        doc = await _filter_col.find_one({"chat_id": int(grp_id), "keyword": keyword})
        if doc:
            return (
                doc.get("text", ""),
                doc.get("btn", "[]"),
                doc.get("alert", "[]"),
                doc.get("fileid", "None"),
            )
    return None, "[]", "[]", "None"


async def db_delete_filter(message: Message, keyword: str, grp_id: int):
    try:
        await delete_filter(message, keyword, grp_id)
        return True
    except Exception as e:
        logger.warning(f"Layer 1 delete_filter failed, attempting direct DB: {e}")
    if _filter_col is not None:
        await _filter_col.delete_one({"chat_id": int(grp_id), "keyword": keyword})
        return True
    return False


async def db_get_filters(grp_id: int):
    try:
        res = await get_filters(grp_id)
        if res is not None:
            return res
    except Exception as e:
        logger.warning(f"Layer 1 get_filters failed, attempting direct DB: {e}")
    if _filter_col is not None:
        return [
            d["keyword"]
            for d in await _filter_col.find({"chat_id": int(grp_id)}).to_list(
                length=None
            )
            if "keyword" in d
        ]
    return []


# ============================================================
# 🎨 KEYBOARD PARSERS
# ============================================================
def create_button(text, url=None, callback_data=None, style=None):
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


def build_keyboard(btn_str: str):
    if not btn_str or btn_str in ["[]", "None", "False", ""]:
        return None
    try:
        parsed_btn = ast.literal_eval(btn_str)
        button_layout = []
        for row in parsed_btn:
            btn_row = []
            for b in row:
                if isinstance(b, dict):
                    b_copy = b.copy()
                    style_val = b_copy.pop("style", None)
                    final_style = {
                        1: getattr(ButtonStyle, "PRIMARY", 1),
                        3: getattr(ButtonStyle, "SUCCESS", 3),
                        4: getattr(ButtonStyle, "DANGER", 4),
                    }.get(style_val, style_val)
                    btn_row.append(
                        create_button(
                            text=b_copy.get("text", "Button"),
                            url=b_copy.get("url"),
                            callback_data=b_copy.get("callback_data"),
                            style=final_style,
                        )
                    )
                else:
                    btn_row.append(b)
            button_layout.append(btn_row)
        return button_layout
    except Exception as e:
        logger.error(f"Button parsing error: {e}")
        return None


def parse_markdown_buttons(text: str):
    if not text:
        return "", "[]"
    buttons, clean_lines = [], []
    for line in text.split("\n"):
        row_btns = []
        matches = list(re.finditer(r"\[([^\[\]]+)\]\(([^()]+)\)", line)) + list(
            re.finditer(r"\[([^\[\]]+)\|([^()]+)\]", line)
        )
        matches.sort(key=lambda m: m.start())
        for match in matches:
            row_btns.append(
                {"text": match.group(1).strip(), "url": match.group(2).strip()}
            )
        if row_btns:
            buttons.append(row_btns)
        clean_line = re.sub(
            r"\[([^\[\]]+)\|([^()]+)\]",
            "",
            re.sub(r"\[([^\[\]]+)\]\(([^()]+)\)", "", line),
        ).strip()
        if clean_line:
            clean_lines.append(clean_line)
        elif not matches:
            clean_lines.append("")
    return "\n".join(clean_lines).strip(), str(buttons) if buttons else "[]"


# ============================================================
# 🎯 FILTER COMMANDS
# ============================================================
@Client.on_message(filters.command("filter") & (filters.group | filters.private))
async def add_filter_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok:
        return
    if not message.reply_to_message:
        return await message.reply_text(
            "⚠️ **Reply to a message to set it as a filter.**"
        )
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/filter <keyword>`")

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
    await message.reply_text(
        f"✅ **Filter successfully added!**\n\n**Keyword:** `{keyword}`"
    )


@Client.on_message(filters.command("addfilter") & (filters.group | filters.private))
async def add_premade_filter_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok:
        return
    if not message.reply_to_message:
        return await message.reply_text(
            "⚠️ **Reply to a message containing inline buttons.**"
        )
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/addfilter <keyword>`")

    keyword = message.text.split(None, 1)[1].lower().strip()
    replied = message.reply_to_message
    buttons = []
    if replied.reply_markup and replied.reply_markup.inline_keyboard:
        for row in replied.reply_markup.inline_keyboard:
            row_btns = []
            for btn in row:
                btn_dict = {"text": btn.text}
                if btn.url:
                    btn_dict["url"] = btn.url
                elif btn.callback_data:
                    btn_dict["callback_data"] = btn.callback_data
                if hasattr(btn, "style") and btn.style:
                    btn_dict["style"] = int(btn.style)
                row_btns.append(btn_dict)
            if row_btns:
                buttons.append(row_btns)

    fileid = "None"
    for media_attr in ["photo", "video", "document", "audio", "animation", "sticker"]:
        media_obj = getattr(replied, media_attr, None)
        if media_obj:
            fileid = media_obj.file_id
            break

    await db_add_filter(
        grp_id,
        keyword,
        replied.text or replied.caption or "",
        str(buttons) if buttons else "[]",
        "[]",
        fileid,
    )
    await message.reply_text(
        f"✅ **Filter with buttons added!**\n\n**Keyword:** `{keyword}`"
    )


@Client.on_message(filters.command("filterimage") & (filters.group | filters.private))
async def edit_filter_image_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok:
        return
    if not message.reply_to_message or not message.reply_to_message.media:
        return await message.reply_text(
            "⚠️ **Reply to a photo, video, or document to set the new image.**"
        )
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/filterimage <keyword>`")

    keyword = message.text.split(None, 1)[1].lower().strip()
    reply_text, btn, alert, old_fileid = await db_find_filter(grp_id, keyword)
    if not reply_text and (not old_fileid or old_fileid == "None"):
        return await message.reply_text(f"❌ Filter `{keyword}` not found.")

    fileid = "None"
    for media_attr in ["photo", "video", "document", "audio", "animation", "sticker"]:
        media_obj = getattr(message.reply_to_message, media_attr, None)
        if media_obj:
            fileid = media_obj.file_id
            break

    if fileid == "None":
        return await message.reply_text("❌ Could not extract valid media.")
    await db_add_filter(grp_id, keyword, reply_text, btn, alert, fileid)
    await message.reply_text(
        f"✅ **Filter image updated!**\n\n**Keyword:** `{keyword}`"
    )


@Client.on_message(
    filters.command(["editfiltercolur", "editfiltercolour"])
    & (filters.group | filters.private)
)
async def edit_filter_colour_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok:
        return
    args = message.command
    if len(args) < 4:
        return await message.reply_text(
            "❌ **Usage:** `/editfiltercolur <keyword> <button_number> <colour>`\n\n**Colours:** `green`, `red`, `blue`"
        )

    try:
        btn_num, color_str, keyword = (
            int(args[-2]),
            args[-1].lower(),
            " ".join(args[1:-2]).lower().strip(),
        )
    except ValueError:
        return await message.reply_text("❌ Button number must be an integer.")

    color_map = {"green": 3, "red": 4, "blue": 1}
    if color_str not in color_map:
        return await message.reply_text(
            "❌ Invalid colour. Choose from: `green`, `red`, `blue`."
        )

    reply_text, btn, alert, fileid = await db_find_filter(grp_id, keyword)
    if not reply_text and (not fileid or fileid == "None"):
        return await message.reply_text(f"❌ Filter `{keyword}` not found.")
    if not btn or btn in ["[]", "None", "False", ""]:
        return await message.reply_text(f"❌ Filter `{keyword}` has no buttons.")

    try:
        button_data = ast.literal_eval(btn)
    except Exception:
        return await message.reply_text("❌ Corrupted button format.")

    count, found = 0, False
    for r_idx, row in enumerate(button_data):
        for c_idx, b in enumerate(row):
            count += 1
            if count == btn_num:
                button_data[r_idx][c_idx]["style"] = color_map[color_str]
                found = True
                break
        if found:
            break

    if not found:
        return await message.reply_text(
            f"❌ Button {btn_num} not found. Filter has {count} buttons."
        )
    await db_add_filter(grp_id, keyword, reply_text, str(button_data), alert, fileid)
    await message.reply_text(
        f"✅ Filter `{keyword}` Button {btn_num} colour changed to {color_str.title()}!"
    )


@Client.on_message(filters.command("delfilter") & (filters.group | filters.private))
async def del_filter_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok:
        return
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/delfilter <keyword>`")
    keyword = message.text.split(None, 1)[1].lower().strip()
    await db_delete_filter(message, keyword, grp_id)
    await message.reply_text(f"🗑️ **Filter `{keyword}` deleted.**")


@Client.on_message(filters.command("listfilters") & (filters.group | filters.private))
async def list_filters_cmd(client: Client, message: Message):
    grp_id, ok = await get_target_group(client, message)
    if not ok:
        return
    keywords = await db_get_filters(grp_id)
    if not keywords:
        return await message.reply_text("⚠️ **No active filters found.**")
    await message.reply_text(
        "📋 **Current Filters:**\n\n" + "\n".join([f"• `{kw}`" for kw in keywords])
    )


# ============================================================
# 📤 4-LAYER MULTI-MEDIA FILTER DISPATCHER
# ============================================================
async def send_filter_media(
    client: Client,
    chat_id: int,
    fileid: str,
    reply_text: str,
    reply_markup,
    reply_id: int,
):
    try:
        return await client.send_cached_media(
            chat_id,
            fileid,
            caption=reply_text,
            reply_markup=reply_markup,
            reply_to_message_id=reply_id,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await send_filter_media(
            client, chat_id, fileid, reply_text, reply_markup, reply_id
        )
    except (UserIsBlocked, PeerIdInvalid):
        return None
    except Exception:
        pass

    try:
        return await client.send_cached_media(
            chat_id, fileid, caption=reply_text, reply_markup=reply_markup
        )
    except Exception:
        pass

    for send_fn, arg_name in [
        (client.send_photo, "photo"),
        (client.send_video, "video"),
        (client.send_document, "document"),
        (client.send_animation, "animation"),
    ]:
        try:
            return await send_fn(
                **{
                    "chat_id": chat_id,
                    arg_name: fileid,
                    "caption": reply_text,
                    "reply_markup": reply_markup,
                }
            )
        except Exception:
            continue

    try:
        return await client.send_message(
            chat_id, text=reply_text or "Here is your file:", reply_markup=reply_markup
        )
    except Exception as err:
        logger.error(f"Media filter failed all 4 layers: {err}")
        return None


async def safe_send_text_filter(
    client: Client, chat_id: int, text: str, reply_markup, reply_id: int
):
    try:
        return await client.send_message(
            chat_id,
            text,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
            reply_to_message_id=reply_id,
        )
    except Exception:
        try:
            return await client.send_message(
                chat_id, text, disable_web_page_preview=True, reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Text filter failed: {e}")
            return None


@Client.on_message(filters.group & filters.text & ~filters.bot, group=5)
async def filter_listener_hook(client: Client, message: Message):
    if message.text.startswith("/"):
        return
    if getattr(info, "REPAIR_MODE", False):
        if get_uid(message) not in get_admin_list():
            return

    group_id = message.chat.id
    if message.chat.type == enums.ChatType.PRIVATE:
        active_grp = await active_connection(str(get_uid(message)))
        if active_grp:
            group_id = active_grp

    name = message.text or message.caption or ""
    reply_id = (
        message.reply_to_message.id
        if getattr(message, "reply_to_message", None)
        else message.id
    )
    keywords = await db_get_filters(group_id)
    if not keywords:
        return

    for keyword in reversed(sorted(keywords, key=len)):
        if re.search(
            r"( |^|[^\w])" + re.escape(keyword) + r"( |$|[^\w])",
            name,
            flags=re.IGNORECASE,
        ):
            reply_text, btn, alert, fileid = await db_find_filter(group_id, keyword)
            if reply_text:
                reply_text = reply_text.replace("\\n", "\n").replace("\\t", "\t")
            button_layout = build_keyboard(btn)
            reply_markup = (
                InlineKeyboardMarkup(button_layout) if button_layout else None
            )
            fileid_str = str(fileid).strip()

            sent_msg = (
                await safe_send_text_filter(
                    client, message.chat.id, reply_text or "", reply_markup, reply_id
                )
                if not fileid or fileid_str in ["None", "[]", "", "False"]
                else await send_filter_media(
                    client,
                    message.chat.id,
                    fileid,
                    reply_text or "",
                    reply_markup,
                    reply_id,
                )
            )

            if sent_msg:
                delete_timer = getattr(info, "BUTTON_AUTO_DELETE", 1800)
                if delete_timer > 0:
                    asyncio.create_task(
                        client.delete_messages(sent_msg.chat.id, sent_msg.id)
                        if delete_timer == 0
                        else asyncio.sleep(0)
                    )
            return


# ============================================================
# 🔍 INLINE SEARCH SYSTEM
# ============================================================
async def inline_users(query: InlineQuery):
    auth_users = getattr(info, "AUTH_USERS", [])
    if auth_users:
        return query.from_user and query.from_user.id in auth_users
    return query.from_user and query.from_user.id not in temp.BANNED_USERS


@Client.on_inline_query()
async def inline_answer(bot, query: InlineQuery):
    cache_time = (
        0
        if getattr(info, "AUTH_USERS", []) or getattr(info, "AUTH_CHANNEL", None)
        else getattr(info, "CACHE_TIME", 300)
    )
    if not await inline_users(query):
        return await query.answer(
            results=[], cache_time=0, switch_pm_text="okDa", switch_pm_parameter="hehe"
        )
    if (
        getattr(info, "AUTH_CHANNEL", None) or getattr(info, "REQ_CHANNEL", None)
    ) and not await is_subscribed(bot, query):
        return await query.answer(
            results=[],
            cache_time=0,
            switch_pm_text="You have to subscribe my channel to use the bot",
            switch_pm_parameter="subscribe",
        )

    string, file_type = (
        (
            query.query.split("|", 1)[0].strip(),
            query.query.split("|", 1)[1].strip().lower(),
        )
        if "|" in query.query
        else (query.query.strip(), None)
    )
    files, next_offset, total = await get_search_results(
        string, file_type=file_type, max_results=10, offset=int(query.offset or 0)
    )

    results = []
    for file in files:
        f_caption = file.caption
        if getattr(info, "CUSTOM_FILE_CAPTION", None):
            try:
                f_caption = info.CUSTOM_FILE_CAPTION.format(
                    file_name="" if file.file_name is None else file.file_name,
                    file_size=(
                        ""
                        if get_size(file.file_size) is None
                        else get_size(file.file_size)
                    ),
                    file_caption="" if f_caption is None else f_caption,
                )
            except Exception:
                pass
        results.append(
            InlineQueryResultCachedDocument(
                title=file.file_name,
                document_file_id=file.file_id,
                caption=f_caption or f"{file.file_name}",
                description=f"Size: {get_size(file.file_size)}\nType: {file.file_type}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "♻️ 𝗦𝗲𝗮𝗿𝗰𝗵 𝗔𝗴𝗮𝗶𝗻 ♻️",
                                switch_inline_query_current_chat=string,
                            )
                        ]
                    ]
                ),
            )
        )

    if results:
        try:
            await query.answer(
                results=results,
                is_personal=True,
                cache_time=cache_time,
                switch_pm_text=(
                    f"{emoji.FILE_FOLDER} Results - {total} for {string}"
                    if string
                    else f"{emoji.FILE_FOLDER} Results - {total}"
                ),
                switch_pm_parameter="start",
                next_offset=str(next_offset),
            )
        except QueryIdInvalid:
            pass
        except Exception as e:
            logger.exception(str(e))
    else:
        await query.answer(
            results=[],
            is_personal=True,
            cache_time=cache_time,
            switch_pm_text=(
                f'{emoji.CROSS_MARK} No results for "{string}"'
                if string
                else f"{emoji.CROSS_MARK} No results"
            ),
            switch_pm_parameter="okay",
        )


# ============================================================
# 📊 BOT & SERVER LOGS/UTILITIES
# ============================================================
def get_size_str(bytes_size):
    if not bytes_size:
        return "0.00 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} PB"


@Client.on_message(filters.command("logs") & admin_filter, group=3)
async def get_logs_cmd(bot: Client, message: Message):
    if not os.path.exists("TelegramBot.log"):
        return await message.reply_text(
            "⚠️ **Log file not found!** No errors recorded yet."
        )
    try:
        await message.reply_document(
            document="TelegramBot.log", caption="📜 **Here are the latest bot logs.**"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Failed to send logs:**\n`{e}`")


@Client.on_message(filters.command("server") & admin_filter, group=3)
async def server_stats_cmd(bot: Client, message: Message):
    msg = await message.reply_text("⏳ **Fetching server statistics...**")
    text = "🖥 **Server Statistics**\n━━━━━━━━━━━━━━\n"
    if psutil:
        try:
            ram = psutil.virtual_memory()
            text += f"🧠 **CPU Usage:** `{psutil.cpu_percent(interval=0.5)}%`\n📉 **RAM Usage:** `{ram.percent}%`\n💾 **RAM Total:** `{get_size_str(ram.total)}`\n💿 **RAM Free:** `{get_size_str(ram.available)}`\n\n"
        except Exception:
            pass
    else:
        text += f"🧠 **CPU Cores:** `{os.cpu_count()}`\n💻 **OS System:** `{platform.system()} {platform.release()}`\n\n"

    try:
        total, used, free = shutil.disk_usage("/")
        text += f"💽 **Disk Total:** `{get_size_str(total)}`\n📀 **Disk Used:** `{get_size_str(used)}` (`{(used/total)*100:.1f}%`)\n💿 **Disk Free:** `{get_size_str(free)}`\n"
    except Exception:
        pass
    await msg.edit_text(text + "━━━━━━━━━━━━━━")


@Client.on_message(filters.command("restart") & admin_filter, group=3)
async def restart_bot_cmd(bot: Client, message: Message):
    await message.reply_text(
        "⚠️ **Are you sure you want to restart the bot?**",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Confirm Restart", callback_data="util_restart")]]
        ),
    )


@Client.on_callback_query(filters.regex("^util_restart$"), group=3)
async def confirm_restart_cb(bot: Client, query: CallbackQuery):
    if get_uid(query.message) not in get_admin_list():
        return
    await query.answer("♻️ Restarting...", show_alert=True)
    msg = await query.edit_message_text("♻️ **Bot is restarting... Please wait.**")
    try:
        with open("restart.txt", "w") as f:
            f.write(f"{msg.chat.id}\n{msg.id}")
    except Exception:
        pass
    await asyncio.sleep(2)
    os._exit(1)


@Client.on_message(filters.command("stats") & admin_filter, group=3)
async def bot_stats_cmd(bot: Client, message: Message):
    status_msg = await message.reply_text("⏳ **Fetching Database Stats...**")
    try:
        total_users, total_chats = (
            await _db.total_users_count(),
            await _db.total_chat_count(),
        )
        try:
            total_files = await _Media.count_documents({})
        except:
            try:
                total_files = await _Media.count_documents()
            except:
                total_files = "Unknown"
        await status_msg.edit_text(
            f"📊 **Bot Database Statistics**\n━━━━━━━━━━━━━━\n👥 **Total Users:** `{total_users}`\n🏘 **Total Groups:** `{total_chats}`\n📁 **Total Files:** `{total_files}`\n💾 **DB Size:** `{get_size_str(await _db.get_db_size())}`\n━━━━━━━━━━━━━━"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ **Error fetching stats:** `{e}`")


@Client.on_message(filters.command("total") & admin_filter, group=3)
async def total_files_cmd(bot: Client, message: Message):
    msg = await message.reply_text("⏳ **Calculating total files in database...**")
    try:
        total = await _Media.count_documents({})
    except:
        try:
            total = await _Media.count_documents()
        except:
            total = "Error"
    await msg.edit_text(f"📁 **Total Files in Database:** `{total}`")


@Client.on_message(filters.command("cleanusers") & admin_filter, group=3)
async def clean_users_cmd(bot: Client, message: Message):
    status_msg = await message.reply_text(
        "⏳ **Starting Deep Clean...** (This is a safe background process)"
    )
    try:
        users_cursor = await _db.get_all_users()
        users = (
            await users_cursor.to_list(length=None)
            if hasattr(users_cursor, "to_list")
            else list(users_cursor)
        )
    except Exception as e:
        return await status_msg.edit_text(f"❌ **Failed to fetch users:** `{e}`")

    active, blocked, total_users = 0, 0, len(users)

    async def check_user(u):
        user_id = u.get("id") or u.get("user_id")
        if not user_id:
            return "blocked"
        try:
            await bot.send_chat_action(user_id, enums.ChatAction.TYPING)
            return "active"
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            return "active"
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid, UserDeactivatedBan):
            await _db.delete_user(user_id)
            return "blocked"
        except Exception:
            return "active"

    for i in range(0, total_users, 30):
        chunk = users[i : i + 30]
        results = await asyncio.gather(*[check_user(u) for u in chunk])
        active += results.count("active")
        blocked += results.count("blocked")
        if (i + len(chunk)) % 300 == 0 or (i + len(chunk)) == total_users:
            try:
                await status_msg.edit_text(
                    f"⏳ **Cleaning Database...**\nScanned: `{i + len(chunk)} / {total_users}`"
                )
            except Exception:
                pass
        await asyncio.sleep(2)
    await status_msg.edit_text(
        f"✅ **Deep Clean Completed!**\n\n🟢 **Active Users Kept:** `{active}`\n🔴 **Dead Accounts Removed:** `{blocked}`"
    )


@Client.on_message(filters.command("clearfiles") & admin_filter, group=3)
async def clear_files_cmd(bot: Client, message: Message):
    await message.reply_text(
        "⚠️ **WARNING!** ⚠️\nDelete **ALL** files indexed in your database?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔥 YES, DELETE ALL FILES", callback_data="nuke_files"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="close_data")],
            ]
        ),
    )


@Client.on_message(filters.command("clearusers") & admin_filter, group=3)
async def clear_users_cmd(bot: Client, message: Message):
    await message.reply_text(
        "⚠️ **WARNING!** ⚠️\nDelete **ALL** users from your database?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔥 YES, DELETE ALL USERS", callback_data="nuke_users"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="close_data")],
            ]
        ),
    )


@Client.on_message(filters.command("clearfsubusers") & admin_filter, group=3)
async def clear_fsub_cmd(bot: Client, message: Message):
    await message.reply_text(
        "⚠️ **WARNING!** ⚠️\nClear the Force Sub Database?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔥 YES, CLEAR FSUB", callback_data="nuke_fsub")],
                [InlineKeyboardButton("❌ Cancel", callback_data="close_data")],
            ]
        ),
    )


@Client.on_callback_query(filters.regex(r"^nuke_(files|users|fsub)$"), group=3)
async def nuke_callbacks(bot: Client, query: CallbackQuery):
    if get_uid(query.message) not in get_admin_list():
        return
    await query.answer("Processing request...", show_alert=False)
    action = query.data.split("_")[1]
    await query.message.edit_text("⏳ **Executing request... This may take a moment.**")

    try:
        if action == "files":
            try:
                res = await _Media.collection.delete_many({})
            except:
                res = await _Media.delete_many({})
            await query.message.edit_text(
                f"✅ **Database Wiped!**\n🗑 **Deleted Files:** `{res.deleted_count if hasattr(res, 'deleted_count') else 'All'}`"
            )
        elif action == "users":
            try:
                res = await _db.col.delete_many({})
            except:
                res = await _db.delete_many({})
            await query.message.edit_text(
                f"✅ **Database Wiped!**\n🗑 **Deleted Users:** `{res.deleted_count if hasattr(res, 'deleted_count') else 'All'}`"
            )
        elif action == "fsub":
            await _plugin_db.clear_fsub_users()
            await query.message.edit_text(
                "✅ **Force Subscribe Database has been completely cleared.**"
            )
    except Exception as e:
        logger.exception(f"Error during nuke_{action}")
        await query.message.edit_text(f"❌ **Error occurred:**\n`{e}`")
