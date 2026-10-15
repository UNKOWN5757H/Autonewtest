import asyncio
import random
from logging import ERROR, getLogger

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait, MessageNotModified, QueryIdInvalid
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info

# Optional plugin_db import with safe fallback
try:
    from database.plugin_dbs import plugin_db as _plugin_db
except ImportError:
    _plugin_db = None

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚡ HIGH-SPEED IN-MEMORY CACHE + MONGODB PERSISTENCE
# ============================================================
# Standard Telegram MTProto valid reaction emojis (Note: "❤" without U+FE0F is required by Telegram API)
VALID_REACTIONS = ["❤", "🔥", "👍", "🎉", "⚡", "👏", "🤩"]

_react_col = None
_REACTION_STATE = {
    "loaded": False,
    "enabled": True,
    "mode": "heart",  # "heart" ("❤") or "random" (random from VALID_REACTIONS)
}

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _react_col = _BOT_DB["auto_reaction_settings"]
except Exception as e:
    logger.error(f"Failed to init auto_reaction DB: {e}")


async def get_reaction_status() -> bool:
    """Returns cached reaction status in O(1) time without hitting MongoDB on every message."""
    if _REACTION_STATE["loaded"]:
        return _REACTION_STATE["enabled"]

    _REACTION_STATE["loaded"] = True

    # 1. Check dedicated collection first
    if _react_col is not None:
        try:
            doc = await _react_col.find_one({"_id": "config"})
            if doc is not None:
                _REACTION_STATE["enabled"] = bool(doc.get("enabled", True))
                _REACTION_STATE["mode"] = doc.get("mode", "heart")
                return _REACTION_STATE["enabled"]
        except Exception:
            pass

    # 2. Fallback to _plugin_db if available
    if _plugin_db is not None and hasattr(_plugin_db, "get_reaction_status"):
        try:
            status = await _plugin_db.get_reaction_status()
            _REACTION_STATE["enabled"] = bool(status)
            return _REACTION_STATE["enabled"]
        except Exception:
            pass

    return _REACTION_STATE["enabled"]


async def set_reaction_status(enabled: bool, mode: str = None):
    _REACTION_STATE["loaded"] = True
    _REACTION_STATE["enabled"] = bool(enabled)
    if mode:
        _REACTION_STATE["mode"] = mode

    if _react_col is not None:
        try:
            await _react_col.update_one(
                {"_id": "config"},
                {
                    "$set": {
                        "enabled": _REACTION_STATE["enabled"],
                        "mode": _REACTION_STATE["mode"],
                    }
                },
                upsert=True,
            )
        except Exception:
            pass

    if _plugin_db is not None and hasattr(_plugin_db, "set_reaction_status"):
        try:
            await _plugin_db.set_reaction_status(bool(enabled))
        except Exception:
            pass


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

    # 1. Global Admin Check (Direct User)
    if message.from_user and message.from_user.id in admin_list:
        return True

    # 2. Global Admin Check (Anonymous/Channel sending as Chat)
    if message.sender_chat and message.sender_chat.id in admin_list:
        return True

    # 3. Group Admin Check (Normal & Anonymous Admins)
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


async def is_callback_user_admin(client: Client, query: CallbackQuery) -> bool:
    if not query or not query.from_user:
        return False
    if query.from_user.id in get_admin_list():
        return True
    if (
        query.message
        and query.message.chat
        and query.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    ):
        try:
            member = await client.get_chat_member(
                query.message.chat.id, query.from_user.id
            )
            return member.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]
        except Exception:
            pass
    return False


async def admin_check(_, client: Client, message: Message) -> bool:
    return await is_user_admin(client, message)


admin_filter = filters.create(admin_check)


# ============================================================
# ⚙️ CONFIGURATION COMMANDS & INTERACTIVE DASHBOARD
# ============================================================
async def _build_reaction_panel() -> tuple[str, InlineKeyboardMarkup]:
    enabled = await get_reaction_status()
    mode = _REACTION_STATE.get("mode", "heart")
    status_str = "✅ ENABLED" if enabled else "🚫 DISABLED"
    mode_str = "❤️ Heart Only" if mode == "heart" else "🎲 Random (❤️ 🔥 👍 🎉 ⚡)"

    text = (
        f"❤️ <b>Auto-Reaction Settings</b>\n\n"
        f"• <b>Global Status:</b> <code>{status_str}</code>\n"
        f"• <b>Reaction Style:</b> <code>{mode_str}</code>"
    )
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Enable", callback_data="react_ctl:on"),
                InlineKeyboardButton("🚫 Disable", callback_data="react_ctl:off"),
            ],
            [
                InlineKeyboardButton("❤️ Heart Mode", callback_data="react_ctl:heart"),
                InlineKeyboardButton(
                    "🎲 Random Mode", callback_data="react_ctl:random"
                ),
            ],
            [InlineKeyboardButton("🔐 Close", callback_data="react_ctl:close")],
        ]
    )
    return text, buttons


@Client.on_message(filters.command("enablereaction") & admin_filter)
async def enable_react(bot: Client, message: Message):
    await set_reaction_status(True)
    await message.reply_text(
        "✅ <b>Auto-Reaction has been ENABLED globally!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("disablereaction") & admin_filter)
async def disable_react(bot: Client, message: Message):
    await set_reaction_status(False)
    await message.reply_text(
        "🚫 <b>Auto-Reaction has been DISABLED globally.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command(["reaction", "autoreaction"]) & admin_filter)
async def reaction_panel_cmd(bot: Client, message: Message):
    text, markup = await _build_reaction_panel()
    await message.reply_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^react_ctl:(on|off|heart|random|close)$"))
async def reaction_panel_cb(client: Client, query: CallbackQuery):
    if not await is_callback_user_admin(client, query):
        return await query.answer("❌ Unauthorized: Admins only!", show_alert=True)

    action = query.matches[0].group(1)
    try:
        if action == "close":
            await query.answer("Closed!")
            return await query.message.delete()
        elif action == "on":
            await set_reaction_status(True)
            await query.answer("✅ Auto-Reaction Enabled!")
        elif action == "off":
            await set_reaction_status(False)
            await query.answer("🚫 Auto-Reaction Disabled!")
        elif action == "heart":
            await set_reaction_status(_REACTION_STATE["enabled"], mode="heart")
            await query.answer("❤️ Switched to Heart Mode!")
        elif action == "random":
            await set_reaction_status(_REACTION_STATE["enabled"], mode="random")
            await query.answer("🎲 Switched to Random Emoji Mode!")

        text, markup = await _build_reaction_panel()
        await query.message.edit_text(
            text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )
    except MessageNotModified:
        pass
    except QueryIdInvalid:
        pass
    except Exception as e:
        logger.error(f"Reaction panel callback error: {e}")


# ============================================================
# ❤️ ZERO-FAIL AUTO REACTION ENGINE
# ============================================================
@Client.on_message(
    (filters.group | filters.channel)
    & filters.incoming
    & ~filters.service
    & ~filters.bot,
    group=-5,
)
async def auto_react_heart(bot: Client, message: Message):
    if not await get_reaction_status():
        return

    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption
    if text and str(text).startswith("/"):
        return

    chosen_emoji = (
        "❤"
        if _REACTION_STATE.get("mode") == "heart"
        else random.choice(VALID_REACTIONS)
    )

    try:
        await bot.send_reaction(
            chat_id=message.chat.id,
            message_id=message.id,
            emoji=chosen_emoji,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
    except Exception:
        # Fallback if a specific emoji is restricted in the chat
        if chosen_emoji != "👍":
            try:
                await bot.send_reaction(
                    chat_id=message.chat.id,
                    message_id=message.id,
                    emoji="👍",
                )
            except Exception:
                pass
