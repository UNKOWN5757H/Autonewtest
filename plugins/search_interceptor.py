import html
import inspect
import re
from logging import ERROR, getLogger
from typing import List, Optional

from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    Forbidden,
    PeerIdInvalid,
    UserIsBlocked,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import info
from database.ia_filterdb import get_search_results
from database.users_chats_db import db as _db
from Script import script
from utils import get_size, temp

# Optional ban database
try:
    from database.plugin_dbs import plugin_db as _plugin_db
except ImportError:
    _plugin_db = None

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


def get_admin_list() -> List[int]:
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        raw_admins = str(raw_admins).replace(",", " ").split()
    elif isinstance(raw_admins, (list, tuple, set)):
        raw_admins = list(raw_admins)
    else:
        raw_admins = []
    return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]


async def _is_user_banned(uid: int) -> bool:
    if not uid:
        return False
    if hasattr(temp, "BANNED_USERS") and uid in temp.BANNED_USERS:
        return True
    if _plugin_db is not None and hasattr(_plugin_db, "is_banned"):
        try:
            if await _plugin_db.is_banned(uid):
                return True
        except Exception:
            pass
    try:
        from plugins.admincore import check_user_banned

        if await check_user_banned(uid):
            return True
    except Exception:
        pass
    return False


async def _fallback_pm_search(client: Client, message: Message, query: str):
    """Fallback direct database search if pm_filter.auto_filter is unavailable or fails."""
    try:
        files, _, total_results = await get_search_results(
            query.lower(), offset=0, filter=True
        )
    except TypeError:
        files, _, total_results = await get_search_results(query.lower(), offset=0)

    safe_query = html.escape(query)
    if not files:
        return await safe_reply_text(
            message,
            f"❌ <b>No files found for:</b> <code>{safe_query}</code>\n\n"
            f"Please check the spelling or request it in our group!",
            parse_mode=enums.ParseMode.HTML,
        )

    btn_rows = []
    for file in files[:10]:
        f_id = (
            file.get("file_id")
            if isinstance(file, dict)
            else getattr(file, "file_id", "")
        )
        f_name = (
            file.get("file_name", "File")
            if isinstance(file, dict)
            else getattr(file, "file_name", "File")
        )
        f_size = get_size(
            int(
                file.get("file_size", 0)
                if isinstance(file, dict)
                else getattr(file, "file_size", 0)
            )
        )
        if f_id:
            btn_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"[{f_size}] {f_name}",
                        callback_data=f"file#{f_id}",
                    )
                ]
            )

    btn_rows.append([InlineKeyboardButton("🔐 Close", callback_data="close_data")])

    await safe_reply_text(
        message,
        f"🔍 <b>Search Results for:</b> <code>{safe_query}</code>\n"
        f"📁 <b>Total Found:</b> <code>{total_results}</code>\n\n"
        f"👇 <i>Click on a file button below to download:</i>",
        reply_markup=InlineKeyboardMarkup(btn_rows),
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 🚨 CLEAN DEEP-LINK SEARCH INTERCEPTOR
# ============================================================
@Client.on_message(
    filters.command("start") & filters.private & filters.incoming, group=-99
)
async def intercept_direct_search(client: Client, message: Message):
    if len(message.command) < 2 or not message.command[1].startswith("search_"):
        return

    user = message.from_user
    uid = user.id if user else 0

    # 1. Maintenance Mode Check
    if getattr(info, "REPAIR_MODE", False) and uid not in get_admin_list():
        await safe_reply_text(
            message,
            "🛠️ <b>Bot is currently under maintenance!</b>",
            parse_mode=enums.ParseMode.HTML,
        )
        raise StopPropagation

    # 2. Ban Check (RAM + Multi-DB)
    if uid and await _is_user_banned(uid):
        raise StopPropagation

    # 3. Ensure New Users Clicking Channel Search Buttons Are Registered in DB
    if user:
        try:
            if not await _db.is_user_exist(uid):
                await _db.add_user(uid, user.first_name)
                log_channel = getattr(info, "LOG_CHANNEL", None)
                if log_channel and hasattr(script, "LOG_TEXT_P"):
                    try:
                        await client.send_message(
                            int(log_channel),
                            script.LOG_TEXT_P.format(uid, user.mention),
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"User registration in search interceptor failed: {e}")

    # 4. Extract & Clean the Search Query
    # Example: "search_Khalifa__The_Ruler_2026" -> "Khalifa The Ruler 2026"
    raw_query = message.command[1][len("search_") :]
    query = re.sub(r"[_+\-]+", " ", raw_query).strip()

    if not query:
        return

    # 5. Execute Search via pm_filter.auto_filter with Direct DB Fallback
    try:
        try:
            from plugins.pm_filter import auto_filter
        except ImportError:
            from pm_filter import auto_filter

        # Mutate message so auto_filter treats it as a plain text search query
        message.text = query
        message.command = None
        await auto_filter(client, message)
    except Exception as e:
        logger.error(f"auto_filter failed in search_interceptor, using fallback: {e}")
        try:
            await _fallback_pm_search(client, message, query)
        except Exception as fallback_err:
            logger.error(f"Fallback search also failed: {fallback_err}")
            await safe_reply_text(
                message,
                f"🔍 <b>Search Query:</b> <code>{html.escape(query)}</code>\n\n"
                f"Tap the text above to copy it, then send it to me to search!",
                parse_mode=enums.ParseMode.HTML,
            )

    # 6. Stop default /start handler from firing after search results are sent
    raise StopPropagation
