import asyncio
import inspect
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from logging import ERROR, getLogger

from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatAdminRequired,
    FloodWait,
    MessageDeleteForbidden,
    MessageNotModified,
    QueryIdInvalid,
    UserAdminInvalid,
)
from pyrogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚙️ FLOOD THRESHOLDS & STATE TRACKERS
# ============================================================
FLOOD_ENABLED = True
FLOOD_WINDOW_SECONDS = 3
MAX_MESSAGES_PER_WINDOW = 5
MUTE_DURATION_SECONDS = 600  # 10 minutes

# Keyed by (chat_id, user_id) so mutes & flood windows are isolated per chat
USER_MESSAGE_LOG = defaultdict(list)
MUTED_USERS = {}  # {(chat_id, user_id): expiry_timestamp}
MUTED_PROCESSING_LOCK = set()

# Deduplicate media albums so sending 1 album of 6-10 photos counts as 1 message
SEEN_MEDIA_GROUPS = {}  # {media_group_id: timestamp}

# Cache for group admin checks to avoid spamming get_chat_member
ADMIN_CACHE = {}  # {(chat_id, user_id): (expiry_timestamp, is_admin)}

# ============================================================
# 🛡️ SAFE CHAT PERMISSIONS BUILDER (CROSS-VERSION COMPATIBLE)
# ============================================================
_VALID_PERM_PARAMS = set(
    inspect.signature(ChatPermissions.__init__).parameters.keys()
) - {"self"}


def build_chat_permissions(can_send: bool) -> ChatPermissions:
    desired = {
        "can_send_messages": can_send,
        "can_send_media_messages": can_send,
        "can_send_other_messages": can_send,
        "can_add_web_page_previews": can_send,
        "can_send_audios": can_send,
        "can_send_documents": can_send,
        "can_send_photos": can_send,
        "can_send_videos": can_send,
        "can_send_video_notes": can_send,
        "can_send_voice_notes": can_send,
        "can_send_polls": can_send,
        "can_invite_users": True,
    }
    valid_kwargs = {k: v for k, v in desired.items() if k in _VALID_PERM_PARAMS}
    return ChatPermissions(**valid_kwargs)


async def restrict_or_unrestrict_member(
    client: Client, chat_id: int, user_id: int, can_send: bool, duration_sec: int = 0
):
    """Cross-version wrapper for restrict_chat_member that handles datetime vs int until_date."""
    perms = build_chat_permissions(can_send=can_send)
    if not can_send and duration_sec > 0:
        until_dt = datetime.now(timezone.utc) + timedelta(seconds=duration_sec)
        try:
            await client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=perms,
                until_date=until_dt,
            )
        except TypeError:
            await client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=perms,
                until_date=int(time.time() + duration_sec),
            )
    else:
        await client.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=perms,
        )


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

    # 3. Linked Channel Auto-Forward Check
    if getattr(message, "automatic_forward", False):
        return True

    # 4. Group Admin Check (Normal & Anonymous Admins)
    if message.chat and message.chat.type in [
        enums.ChatType.GROUP,
        enums.ChatType.SUPERGROUP,
    ]:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True

        if message.from_user:
            chat_id = message.chat.id
            user_id = message.from_user.id
            cache_key = (chat_id, user_id)
            now = time.time()

            cached = ADMIN_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

            try:
                member = await client.get_chat_member(chat_id, user_id)
                is_admin = member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
                ADMIN_CACHE[cache_key] = (now + 300, is_admin)
                return is_admin
            except Exception:
                return False

    return False


async def is_callback_user_admin(
    client: Client, query: CallbackQuery, target_chat_id: int = None
) -> bool:
    if not query or not query.from_user:
        return False
    user_id = query.from_user.id
    if user_id in get_admin_list():
        return True

    chat_id = target_chat_id or (query.message.chat.id if query.message else None)
    if chat_id and int(chat_id) < 0:
        cache_key = (int(chat_id), user_id)
        now = time.time()
        cached = ADMIN_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        try:
            member = await client.get_chat_member(int(chat_id), user_id)
            is_admin = member.status in [
                enums.ChatMemberStatus.ADMINISTRATOR,
                enums.ChatMemberStatus.OWNER,
            ]
            ADMIN_CACHE[cache_key] = (now + 300, is_admin)
            return is_admin
        except Exception:
            pass
    return False


async def _admin_filter_func(_, client: Client, message: Message) -> bool:
    return await is_user_admin(client, message)


admin_filter = filters.create(_admin_filter_func)


# ============================================================
# 🧹 RAM GARBAGE COLLECTOR (PREVENTS LEAKS)
# ============================================================
async def memory_cleanup_worker():
    while True:
        await asyncio.sleep(300)
        now = time.time()

        expired_mutes = [k for k, expiry in MUTED_USERS.items() if expiry <= now]
        for k in expired_mutes:
            MUTED_USERS.pop(k, None)

        inactive_keys = [
            k
            for k, timestamps in USER_MESSAGE_LOG.items()
            if not timestamps or (now - timestamps[-1]) > 60
        ]
        for k in inactive_keys:
            USER_MESSAGE_LOG.pop(k, None)

        expired_admin_checks = [k for k, (exp, _) in ADMIN_CACHE.items() if exp <= now]
        for k in expired_admin_checks:
            ADMIN_CACHE.pop(k, None)

        expired_albums = [
            mgid for mgid, ts in SEEN_MEDIA_GROUPS.items() if (now - ts) > 30
        ]
        for mgid in expired_albums:
            SEEN_MEDIA_GROUPS.pop(mgid, None)


_GC_STARTED = False


@Client.on_message(filters.all, group=-150)
async def init_antiflood_gc(client: Client, message: Message):
    global _GC_STARTED
    if not _GC_STARTED:
        _GC_STARTED = True
        asyncio.create_task(memory_cleanup_worker())


# ============================================================
# 🎛️ ADMIN COMMANDS & INTERACTIVE BUTTONS
# ============================================================
def _build_flood_panel() -> tuple[str, InlineKeyboardMarkup]:
    status_str = "✅ ENABLED" if FLOOD_ENABLED else "🚫 DISABLED"
    active_mutes = sum(1 for exp in MUTED_USERS.values() if exp > time.time())
    text = (
        f"🛡️ <b>Anti-Flood Shield Dashboard</b>\n\n"
        f"• <b>Status:</b> <code>{status_str}</code>\n"
        f"• <b>Max Messages:</b> <code>{MAX_MESSAGES_PER_WINDOW} msgs / {FLOOD_WINDOW_SECONDS}s</code>\n"
        f"• <b>Mute Duration:</b> <code>{MUTE_DURATION_SECONDS // 60} minutes</code>\n"
        f"• <b>Currently Muted Users:</b> <code>{active_mutes}</code>"
    )
    toggle_label = "🚫 Disable Shield" if FLOOD_ENABLED else "✅ Enable Shield"
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(toggle_label, callback_data="af_toggle"),
                InlineKeyboardButton("🧹 Clear Mutes", callback_data="af_clear_mutes"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="af_refresh"),
                InlineKeyboardButton("🔐 Close", callback_data="af_close"),
            ],
        ]
    )
    return text, buttons


@Client.on_message(filters.command(["antiflood", "floodstatus"]) & admin_filter)
async def antiflood_dashboard_cmd(client: Client, message: Message):
    text, markup = _build_flood_panel()
    await message.reply_text(text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command(["unflood", "unmute"]) & admin_filter)
async def unflood_user_cmd(client: Client, message: Message):
    target_uid = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_uid = message.reply_to_message.from_user.id
    elif len(message.command) > 1 and message.command[1].lstrip("-").isdigit():
        target_uid = int(message.command[1])

    if not target_uid:
        return await message.reply_text(
            "⚙️ <b>Usage:</b> Reply to a muted user with <code>/unflood</code> or send <code>/unflood &lt;user_id&gt;</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    # Remove from all in-memory flood & mute records
    keys_to_remove = [k for k in MUTED_USERS if k[1] == target_uid]
    for k in keys_to_remove:
        MUTED_USERS.pop(k, None)
    log_keys = [k for k in USER_MESSAGE_LOG if k[1] == target_uid]
    for k in log_keys:
        USER_MESSAGE_LOG.pop(k, None)

    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        try:
            await restrict_or_unrestrict_member(
                client, message.chat.id, target_uid, can_send=True
            )
        except Exception:
            pass

    await message.reply_text(
        f"🔓 <b>User <code>{target_uid}</code> has been unmuted and cleared from the Anti-Flood filter!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_callback_query(
    filters.regex(r"^af_(toggle|clear_mutes|refresh|close|unmute:(-?\d+):(\d+))$")
)
async def antiflood_callbacks(client: Client, query: CallbackQuery):
    global FLOOD_ENABLED
    data = query.data

    if data.startswith("af_unmute:"):
        _, raw_cid, raw_uid = data.split(":")
        chat_id, target_uid = int(raw_cid), int(raw_uid)

        if not await is_callback_user_admin(client, query, target_chat_id=chat_id):
            return await query.answer(
                "❌ Only admins can unmute users!", show_alert=True
            )

        MUTED_USERS.pop((chat_id, target_uid), None)
        USER_MESSAGE_LOG.pop((chat_id, target_uid), None)

        try:
            await restrict_or_unrestrict_member(
                client, chat_id, target_uid, can_send=True
            )
        except Exception:
            pass

        try:
            await query.answer("🔓 User unmuted successfully!", show_alert=True)
            await query.message.edit_text(
                f"🔓 <b>User <code>{target_uid}</code> was unmuted by {query.from_user.mention}.</b>",
                parse_mode=enums.ParseMode.HTML,
            )
            asyncio.create_task(delete_after_delay(query.message, 10))
        except Exception:
            pass
        return

    if not await is_callback_user_admin(client, query):
        return await query.answer("❌ Unauthorized: Admins only!", show_alert=True)

    try:
        if data == "af_close":
            await query.answer("Closed!")
            return await query.message.delete()

        if data == "af_toggle":
            FLOOD_ENABLED = not FLOOD_ENABLED
            await query.answer(
                f"Anti-Flood is now {'ENABLED' if FLOOD_ENABLED else 'DISABLED'}!"
            )
        elif data == "af_clear_mutes":
            MUTED_USERS.clear()
            USER_MESSAGE_LOG.clear()
            await query.answer("🧹 All active flood mutes cleared!", show_alert=True)
        else:
            await query.answer("Refreshed!")

        text, markup = _build_flood_panel()
        await query.message.edit_text(
            text, reply_markup=markup, parse_mode=enums.ParseMode.HTML
        )
    except MessageNotModified:
        pass
    except QueryIdInvalid:
        pass
    except Exception as e:
        logger.error(f"Anti-flood callback error: {e}")


# ============================================================
# 🛡️ IRONCLAD ANTI-FLOOD LISTENER
# ============================================================
@Client.on_message(filters.incoming & ~filters.service, group=-50)
async def ironclad_antiflood_shield(client: Client, message: Message):
    if not FLOOD_ENABLED:
        return

    if not message.from_user or message.from_user.is_bot:
        return

    user_id = message.from_user.id
    chat_id = message.chat.id
    chat_type = message.chat.type
    mute_key = (chat_id, user_id)

    # Verify immunity (Global Admins, Group Admins, Anonymous Admins, Linked Channels)
    if await is_user_admin(client, message):
        return

    now = time.time()

    # 1. Silently drop and delete messages if user is already muted in this chat
    if mute_key in MUTED_USERS:
        if now < MUTED_USERS[mute_key]:
            try:
                await message.delete()
            except (MessageDeleteForbidden, FloodWait, Exception):
                pass
            raise StopPropagation
        else:
            MUTED_USERS.pop(mute_key, None)

    # 2. Deduplicate media albums so 1 album of multiple photos/videos counts as 1 message
    if message.media_group_id:
        mg_id = str(message.media_group_id)
        if mg_id in SEEN_MEDIA_GROUPS:
            return
        SEEN_MEDIA_GROUPS[mg_id] = now

    # 3. Record message timestamps and clear outside the sliding window
    timestamps = USER_MESSAGE_LOG[mute_key]
    USER_MESSAGE_LOG[mute_key] = [
        t for t in timestamps if now - t <= FLOOD_WINDOW_SECONDS
    ]
    USER_MESSAGE_LOG[mute_key].append(now)

    # 4. Threshold check
    if len(USER_MESSAGE_LOG[mute_key]) > MAX_MESSAGES_PER_WINDOW:
        if mute_key in MUTED_PROCESSING_LOCK:
            raise StopPropagation

        MUTED_PROCESSING_LOCK.add(mute_key)
        try:
            MUTED_USERS[mute_key] = now + MUTE_DURATION_SECONDS
            USER_MESSAGE_LOG[mute_key].clear()

            # Delete trigger message
            try:
                await message.delete()
            except Exception:
                pass

            mins = MUTE_DURATION_SECONDS // 60
            mention = message.from_user.mention

            # Apply group mute or private warning
            if chat_type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                unmute_kb = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔓 Unmute User (Admin)",
                                callback_data=f"af_unmute:{chat_id}:{user_id}",
                            )
                        ]
                    ]
                )
                try:
                    await restrict_or_unrestrict_member(
                        client=client,
                        chat_id=chat_id,
                        user_id=user_id,
                        can_send=False,
                        duration_sec=MUTE_DURATION_SECONDS,
                    )
                    warn = await client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🛑 <b>Anti-Flood Triggered!</b>\n\n"
                            f"{mention} has been muted for <b>{mins} minutes</b> for spamming messages."
                        ),
                        reply_markup=unmute_kb,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    asyncio.create_task(delete_after_delay(warn, 30))
                except (ChatAdminRequired, UserAdminInvalid):
                    warn = await client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ {mention}, please slow down! "
                            f"Your requests are being ignored for <b>{mins} minutes</b>."
                        ),
                        reply_markup=unmute_kb,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    asyncio.create_task(delete_after_delay(warn, 20))
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    logger.error(f"Anti-flood group mute failed: {e}")
            else:
                try:
                    warn = await client.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🛑 <b>Anti-Flood Shield Triggered!</b>\n\n"
                            f"You sent too many messages too quickly. "
                            f"Please wait <b>{mins} minutes</b> before sending more commands."
                        ),
                        parse_mode=enums.ParseMode.HTML,
                    )
                    asyncio.create_task(delete_after_delay(warn, 15))
                except Exception:
                    pass
        finally:
            MUTED_PROCESSING_LOCK.discard(mute_key)

        raise StopPropagation


async def delete_after_delay(msg: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass
