import asyncio
import html
import inspect
import re
import time
from logging import ERROR, INFO, getLogger
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
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
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info

logger = getLogger(__name__)
logger.setLevel(INFO)

MAX_WORKER_BOTS = 10

# ============================================================
# 🛡️ PYROGRAM v2.2.26 COMPATIBILITY & BUTTON HELPERS
# ============================================================
BTN_PRIMARY = None
BTN_SUCCESS = None
BTN_DANGER = None
_SUPPORTS_BTN_STYLE = False

try:
    from pyrogram.enums import ButtonStyle

    if "style" in inspect.signature(InlineKeyboardButton.__init__).parameters:
        BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", None)
        BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", None)
        BTN_DANGER = getattr(ButtonStyle, "DANGER", None)
        _SUPPORTS_BTN_STYLE = True
except (ImportError, ValueError, TypeError):
    BTN_PRIMARY = BTN_SUCCESS = BTN_DANGER = None
    _SUPPORTS_BTN_STYLE = False


def create_btn(
    text: str,
    url: Optional[str] = None,
    callback_data: Optional[str] = None,
    style: Any = None,
) -> InlineKeyboardButton:
    kwargs: Dict[str, Any] = {"text": str(text)}
    if url:
        kwargs["url"] = str(url).strip()
    elif callback_data:
        kwargs["callback_data"] = str(callback_data)[:64]

    if _SUPPORTS_BTN_STYLE and style is not None:
        kwargs["style"] = style
    try:
        return InlineKeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


async def safe_answer(
    query: CallbackQuery, text: Optional[str] = None, show_alert: bool = False
):
    if not query:
        return
    try:
        if text is not None:
            await query.answer(str(text)[:200], show_alert=show_alert)
        else:
            await query.answer(show_alert=show_alert)
    except (QueryIdInvalid, Exception):
        pass


async def safe_reply_text(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    try:
        return await message.reply_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except (ChatWriteForbidden, Forbidden, UserIsBlocked, PeerIdInvalid):
        return None
    except Exception as e:
        logger.error(f"safe_reply_text error in multibot: {e}")
        return None


async def safe_edit_text(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    try:
        return await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=enums.ParseMode.HTML,
        )
    except (MessageNotModified, MessageIdInvalid, ChatWriteForbidden, Forbidden):
        return None
    except Exception as e:
        logger.error(f"safe_edit_text error in multibot: {e}")
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


async def is_global_admin(user_id: int) -> bool:
    if not user_id:
        return False
    return int(user_id) in get_admin_list()


# ============================================================
# 🤖 MONGODB & LIVE WORKER BOT POOL ENGINE
# ============================================================
_mb_col = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    _mb_col = _BOT_DB["multibot_workers"]
except Exception as e:
    logger.error(f"MultiBot DB init error: {e}")

# In-memory state & running Pyrogram worker clients
_MULTIBOT_CONFIG_CACHE: Dict[str, Any] = {
    "enabled": True,
    "bots": [],  # List of dicts: {"bot_id": int, "name": str, "username": str, "token": str}
}
_CONFIG_LOADED = False
ACTIVE_WORKER_CLIENTS: Dict[int, Client] = {}
_WORKER_LOCK = asyncio.Lock()

# Interactive token listener state: chat_id -> {"future": Future, "user_id": int, "prompt_msg": Message}
_MB_WAITING_INPUT: Dict[int, Dict[str, Any]] = {}


async def load_multibot_config(force_refresh: bool = False) -> Dict[str, Any]:
    global _CONFIG_LOADED, _MULTIBOT_CONFIG_CACHE
    if _CONFIG_LOADED and not force_refresh:
        return _MULTIBOT_CONFIG_CACHE

    if _mb_col is not None:
        try:
            doc = await _mb_col.find_one({"_id": "master_config"})
            if doc:
                _MULTIBOT_CONFIG_CACHE["enabled"] = bool(doc.get("enabled", True))
                _MULTIBOT_CONFIG_CACHE["bots"] = list(doc.get("bots", []))
        except Exception as e:
            logger.error(f"load_multibot_config error (using RAM cache): {e}")

    _CONFIG_LOADED = True
    return _MULTIBOT_CONFIG_CACHE


async def save_multibot_config():
    if _mb_col is not None:
        try:
            await _mb_col.update_one(
                {"_id": "master_config"},
                {
                    "$set": {
                        "enabled": bool(_MULTIBOT_CONFIG_CACHE.get("enabled", True)),
                        "bots": list(_MULTIBOT_CONFIG_CACHE.get("bots", [])),
                        "updated_at": time.time(),
                    }
                },
                upsert=True,
            )
        except Exception as e:
            logger.error(f"save_multibot_config error (RAM still updated): {e}")


async def start_single_worker(bot_data: Dict[str, Any]) -> Optional[Client]:
    """Starts an in-memory Pyrogram Worker Bot client for high-speed indexing."""
    bot_id = int(bot_data["bot_id"])
    token = str(bot_data["token"]).strip()

    if bot_id in ACTIVE_WORKER_CLIENTS:
        client = ACTIVE_WORKER_CLIENTS[bot_id]
        if getattr(client, "is_connected", False):
            return client

    try:
        worker_client = Client(
            name=f"worker_bot_{bot_id}",
            api_id=info.API_ID,
            api_hash=info.API_HASH,
            bot_token=token,
            in_memory=True,
            no_updates=True,  # Pure indexer worker: doesn't waste CPU polling group messages!
            workers=16,
            sleep_threshold=60,
        )
        await worker_client.start()
        ACTIVE_WORKER_CLIENTS[bot_id] = worker_client
        logger.info(
            f"✅ Started Multi-Bot Worker: {bot_data.get('name')} (@{bot_data.get('username')})"
        )
        return worker_client
    except Exception as e:
        logger.error(
            f"Failed to start worker bot {bot_id} (@{bot_data.get('username')}): {e}"
        )
        return None


async def stop_single_worker(bot_id: int):
    client = ACTIVE_WORKER_CLIENTS.pop(int(bot_id), None)
    if client:
        try:
            await client.stop()
        except Exception:
            pass


async def stop_all_workers():
    for bot_id in list(ACTIVE_WORKER_CLIENTS.keys()):
        await stop_single_worker(bot_id)


async def ensure_all_workers_started() -> List[Client]:
    """
    Ensures all configured worker bots are started and connected if Multi-Bot is ON.
    Returns the list of active worker Client instances.
    """
    async with _WORKER_LOCK:
        cfg = await load_multibot_config()
        if not cfg.get("enabled", True):
            return []

        bots_list = cfg.get("bots", [])
        if not bots_list:
            return []

        clients: List[Client] = []
        tasks = [start_single_worker(b) for b in bots_list[:MAX_WORKER_BOTS]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Client):
                clients.append(res)
        return clients


async def get_indexing_clients(main_bot: Client) -> List[Client]:
    """
    Used by plugins/index.py!
    Returns [main_bot] + all active Worker Bots when Multi-Bot Mode is ON.
    If Multi-Bot Mode is OFF or no worker bots exist, returns [main_bot].
    """
    cfg = await load_multibot_config()
    if not cfg.get("enabled", True) or not cfg.get("bots"):
        return [main_bot]

    workers = await ensure_all_workers_started()
    # Combine Main Bot + Worker Bots (deduplicated by bot token/ID)
    all_clients: List[Client] = [main_bot]
    seen_ids = {getattr(main_bot.me, "id", 0) if getattr(main_bot, "me", None) else 0}

    for w in workers:
        w_id = getattr(w.me, "id", None) if getattr(w, "me", None) else None
        if w_id and w_id not in seen_ids:
            seen_ids.add(w_id)
            all_clients.append(w)
        elif not w_id:
            all_clients.append(w)

    return all_clients


# ============================================================
# 🔄 INTERACTIVE TOKEN INPUT LISTENER
# ============================================================
def cancel_mb_input(chat_id: int):
    session = _MB_WAITING_INPUT.pop(int(chat_id), None)
    if session:
        fut = session.get("future")
        if fut and not fut.done():
            fut.cancel()
        prompt_msg = session.get("prompt_msg")
        if prompt_msg:
            try:
                asyncio.create_task(prompt_msg.delete())
            except Exception:
                pass


@Client.on_message(filters.private | filters.group, group=-16)
async def _mb_token_listener(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in _MB_WAITING_INPUT:
        return

    if (
        message.text
        and message.text.startswith("/")
        and message.text.strip().lower() not in ["/cancel"]
    ):
        cancel_mb_input(chat_id)
        return

    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    session = _MB_WAITING_INPUT.get(chat_id)
    if session and (uid == session["user_id"] or uid == chat_id):
        fut = session["future"]
        if not fut.done():
            fut.set_result(message)
        message.stop_propagation()


async def wait_for_bot_token(
    chat_id: int, user_id: int, prompt_msg: Optional[Message], timeout: int = 120
) -> Optional[Message]:
    cancel_mb_input(chat_id)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _MB_WAITING_INPUT[int(chat_id)] = {
        "future": future,
        "user_id": int(user_id),
        "prompt_msg": prompt_msg,
    }
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
    finally:
        if _MB_WAITING_INPUT.get(int(chat_id), {}).get("future") is future:
            _MB_WAITING_INPUT.pop(int(chat_id), None)
        if prompt_msg:
            try:
                await prompt_msg.delete()
            except Exception:
                pass


# ============================================================
# 🎨 UI BUILDERS FOR /bots AND BOT LIST
# ============================================================
async def build_main_bots_panel() -> Tuple[str, InlineKeyboardMarkup]:
    cfg = await load_multibot_config()
    enabled = bool(cfg.get("enabled", True))
    bots = cfg.get("bots", [])
    count = len(bots)
    online_count = len(
        [
            b
            for b in bots
            if int(b.get("bot_id", 0)) in ACTIVE_WORKER_CLIENTS
            and getattr(ACTIVE_WORKER_CLIENTS[int(b["bot_id"])], "is_connected", False)
        ]
    )

    status_badge = (
        "🟢 ON (Ultra-Fast Distributed Indexing)"
        if enabled
        else "🔴 OFF (Main Bot Only)"
    )
    speed_multiplier = (
        f"{count + 1}x Speed (Main + {count} Workers)"
        if (enabled and count > 0)
        else "1x Speed (Main Bot)"
    )

    text = (
        "🤖 <b>Multi-Bot Master-Worker Manager</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Multi-Bot Status:</b> <code>{status_badge}</code>\n"
        f"🤖 <b>Added Worker Bots:</b> <code>{count} / {MAX_WORKER_BOTS}</code>\n"
        f"⚡ <b>Online Workers:</b> <code>{online_count} Active</code>\n"
        f"🚀 <b>Indexing Speed:</b> <code>{speed_multiplier}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How Multi-Bot Works for 10 Lakh+ Files:</b>\n"
        "• Make your Worker Bots <b>Admin</b> in your file channel.\n"
        "• When you run <code>/index</code>, the channel is automatically split into equal parts across all bots simultaneously!\n"
        "• All bots save directly into your shared 3-Database MongoDB cluster."
    )

    on_label = "✅ Multi Bot On" if enabled else "Multi Bot On"
    off_label = "🚫 Off" if not enabled else "Off"

    buttons = [
        [
            create_btn(
                on_label,
                callback_data="mb#on",
                style=BTN_SUCCESS if enabled else None,
            ),
            create_btn(
                off_label,
                callback_data="mb#off",
                style=BTN_DANGER if not enabled else None,
            ),
        ],
        [
            create_btn(
                "➕ Add Bot",
                callback_data="mb#add",
                style=BTN_PRIMARY,
            ),
            create_btn(
                f"📋 Bot list ({count})",
                callback_data="mb#list",
                style=BTN_PRIMARY,
            ),
        ],
        [
            create_btn(
                "➖ Remove Bot",
                callback_data="mb#remmenu",
            ),
            create_btn(
                "🗑️ Remove All Bot",
                callback_data="mb#remall_ask",
                style=BTN_DANGER,
            ),
        ],
        [
            create_btn("🔐 Close", callback_data="mb#close"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def build_bot_list_panel() -> Tuple[str, InlineKeyboardMarkup]:
    cfg = await load_multibot_config()
    bots = cfg.get("bots", [])

    if not bots:
        text = (
            "📋 <b>Worker Bot List (0 / 10)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>No worker bots added yet. Click <b>➕ Add Bot</b> to add up to 10 bot tokens for ultra-fast parallel indexing!</i>"
        )
        buttons = [
            [
                create_btn("➕ Add Bot", callback_data="mb#add", style=BTN_PRIMARY),
                create_btn("⬅️ Back", callback_data="mb#main"),
            ]
        ]
        return text, InlineKeyboardMarkup(buttons)

    lines = [
        f"📋 <b>Configured Worker Bots ({len(bots)} / {MAX_WORKER_BOTS})</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    buttons: List[List[InlineKeyboardButton]] = []

    for idx, b in enumerate(bots, start=1):
        b_id = int(b.get("bot_id", 0))
        b_name = html.escape(str(b.get("name", f"Bot {idx}")))
        b_uname = html.escape(str(b.get("username", "unknown")))
        is_online = b_id in ACTIVE_WORKER_CLIENTS and getattr(
            ACTIVE_WORKER_CLIENTS[b_id], "is_connected", False
        )
        dot = "🟢" if is_online else "⚪"
        lines.append(f"{idx}. {dot} <b>{b_name}</b> (@{b_uname}) — <code>{b_id}</code>")

        # Exact layout requested: Bot name | Remove bot
        buttons.append(
            [
                create_btn(
                    f"🤖 {b.get('name', f'Bot {idx}')[:20]}",
                    url=(
                        f"https://t.me/{b.get('username', '')}"
                        if b.get("username")
                        else None
                    ),
                    callback_data=f"mb#info#{b_id}" if not b.get("username") else None,
                ),
                create_btn(
                    "Remove bot",
                    callback_data=f"mb#del#{b_id}",
                    style=BTN_DANGER,
                ),
            ]
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        "⚠️ <i>Remember to make all worker bots Admin in your indexing channel!</i>"
    )

    # Bottom row: Remove all bots + Back
    buttons.append(
        [
            create_btn(
                "🗑️ Remove all bots",
                callback_data="mb#remall_ask",
                style=BTN_DANGER,
            )
        ]
    )
    buttons.append(
        [
            create_btn("➕ Add Bot", callback_data="mb#add", style=BTN_PRIMARY),
            create_btn("⬅️ Back", callback_data="mb#main"),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ============================================================
# 🎯 /bots COMMAND & CALLBACK HANDLERS
# ============================================================
@Client.on_message(
    filters.command(["bots", "multibot", "workerbots"]) & filters.incoming, group=-2
)
async def bots_command_handler(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
        )

    cancel_mb_input(message.chat.id)
    cfg = await load_multibot_config()
    if cfg.get("enabled", True) and cfg.get("bots"):
        asyncio.create_task(ensure_all_workers_started())

    text, markup = await build_main_bots_panel()
    await safe_reply_text(message, text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^mb#"), group=-2)
async def multibot_callback_handler(client: Client, query: CallbackQuery):
    uid = query.from_user.id if query.from_user else 0
    if not await is_global_admin(uid):
        return await safe_answer(query, "❌ Global Bot Admin only!", show_alert=True)

    chat_id = query.message.chat.id
    parts = query.data.split("#")
    action = parts[1] if len(parts) > 1 else "main"

    if action == "cancel_input":
        cancel_mb_input(chat_id)
        await safe_answer(query, "❌ Cancelled.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    cancel_mb_input(chat_id)

    if action == "close":
        await safe_answer(query, "Closed!")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "main":
        await safe_answer(query)
        text, markup = await build_main_bots_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action in ["on", "off"]:
        new_state = action == "on"
        cfg = await load_multibot_config()
        cfg["enabled"] = new_state
        await save_multibot_config()

        if new_state:
            await safe_answer(query, "⚡ Multi-Bot Enabled! Starting workers...")
            await ensure_all_workers_started()
        else:
            await safe_answer(query, "🔴 Multi-Bot Disabled! Stopping workers...")
            await stop_all_workers()

        text, markup = await build_main_bots_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action in ["list", "remmenu"]:
        await safe_answer(query)
        text, markup = await build_bot_list_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "info":
        bot_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return await safe_answer(query, f"🤖 Worker Bot ID: {bot_id}", show_alert=True)

    if action == "del":
        if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
            return await safe_answer(query, "Invalid Bot ID!", show_alert=True)
        target_id = int(parts[2])
        cfg = await load_multibot_config()
        before_len = len(cfg.get("bots", []))
        removed_name = "Bot"
        new_bots = []
        for b in cfg.get("bots", []):
            if int(b.get("bot_id", 0)) == target_id:
                removed_name = b.get("name", "Bot")
            else:
                new_bots.append(b)

        cfg["bots"] = new_bots
        await stop_single_worker(target_id)
        await save_multibot_config()

        if len(new_bots) < before_len:
            await safe_answer(query, f"🗑️ Removed {removed_name}!", show_alert=True)
        else:
            await safe_answer(query, "Bot already removed.")

        text, markup = await build_bot_list_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "remall_ask":
        cfg = await load_multibot_config()
        if not cfg.get("bots"):
            return await safe_answer(
                query, "⚠️ No worker bots to remove!", show_alert=True
            )

        await safe_answer(query)
        confirm_kb = InlineKeyboardMarkup(
            [
                [
                    create_btn(
                        "🔥 Yes, Remove All Bots",
                        callback_data="mb#remall_confirm",
                        style=BTN_DANGER,
                    )
                ],
                [
                    create_btn("⬅️ Cancel", callback_data="mb#main"),
                ],
            ]
        )
        return await safe_edit_text(
            query.message,
            f"⚠️ <b>Are you sure you want to remove all {len(cfg['bots'])} Worker Bots?</b>",
            reply_markup=confirm_kb,
        )

    if action == "remall_confirm":
        cfg = await load_multibot_config()
        cfg["bots"] = []
        await stop_all_workers()
        await save_multibot_config()
        await safe_answer(query, "🗑️ All Worker Bots Removed!", show_alert=True)
        text, markup = await build_main_bots_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "add":
        cfg = await load_multibot_config()
        cur_bots = cfg.get("bots", [])
        if len(cur_bots) >= MAX_WORKER_BOTS:
            return await safe_answer(
                query,
                f"⚠️ Maximum limit of {MAX_WORKER_BOTS} Worker Bots reached! Remove one first.",
                show_alert=True,
            )

        await safe_answer(query)
        cancel_kb = InlineKeyboardMarkup(
            [[create_btn("❌ Cancel", callback_data="mb#cancel_input")]]
        )
        prompt_msg = await safe_reply_text(
            query.message,
            f"🤖 <b>Add Worker Bot ({len(cur_bots) + 1} / {MAX_WORKER_BOTS})</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🔑 <b>Send the Bot Token(s) from @BotFather:</b>\n\n"
            "• You can send <b>1 token</b> or <b>multiple tokens</b> (one per line) to add several bots at once!\n"
            "• Example: <code>123456789:AAHxyz...</code>\n\n"
            "⚠️ <i>Make sure the worker bot is an <b>Admin</b> in your File Channel!</i>",
            reply_markup=cancel_kb,
        )

        resp = await wait_for_bot_token(chat_id, uid, prompt_msg, timeout=120)
        if not resp or not resp.text:
            return
        if resp.text.strip().lower() == "/cancel":
            try:
                await resp.delete()
            except Exception:
                pass
            return

        raw_text = resp.text.strip()
        # Delete the message containing sensitive bot tokens immediately for security
        try:
            await resp.delete()
        except Exception:
            pass

        found_tokens = re.findall(r"\b\d{7,12}:[A-Za-z0-9_-]{30,50}\b", raw_text)
        if not found_tokens:
            err_m = await safe_reply_text(
                query.message,
                "❌ <b>Invalid Bot Token format!</b>\nPlease send a valid token from @BotFather.",
            )
            if err_m:
                await asyncio.sleep(5)
                try:
                    await err_m.delete()
                except Exception:
                    pass
            return

        status_msg = await safe_reply_text(
            query.message,
            f"⏳ <b>Verifying & starting {len(found_tokens)} worker bot(s)...</b>",
        )

        added_names: List[str] = []
        failed_reasons: List[str] = []

        for tok in found_tokens:
            if len(cfg["bots"]) >= MAX_WORKER_BOTS:
                failed_reasons.append(f"Max {MAX_WORKER_BOTS} limit reached")
                break

            if any(b.get("token") == tok for b in cfg["bots"]):
                failed_reasons.append("Token already added")
                continue

            if tok == getattr(info, "BOT_TOKEN", ""):
                failed_reasons.append("Cannot add Main Bot token as worker")
                continue

            try:
                temp_client = Client(
                    name=f"verify_worker_{int(time.time() * 1000)}",
                    api_id=info.API_ID,
                    api_hash=info.API_HASH,
                    bot_token=tok,
                    in_memory=True,
                    no_updates=True,
                    workers=16,
                    sleep_threshold=60,
                )
                await temp_client.start()
                me = await temp_client.get_me()

                bot_entry = {
                    "bot_id": int(me.id),
                    "name": me.first_name or f"Bot {me.id}",
                    "username": me.username or "",
                    "token": tok,
                }
                # Replace if same bot_id existed with old token
                cfg["bots"] = [
                    b for b in cfg["bots"] if int(b.get("bot_id", 0)) != int(me.id)
                ]
                cfg["bots"].append(bot_entry)

                if cfg.get("enabled", True):
                    ACTIVE_WORKER_CLIENTS[int(me.id)] = temp_client
                else:
                    await temp_client.stop()

                added_names.append(
                    f"<b>{html.escape(bot_entry['name'])}</b> (@{html.escape(bot_entry['username'])})"
                )
            except Exception as e:
                failed_reasons.append(str(e)[:60])

        await save_multibot_config()

        if status_msg:
            summary_lines = []
            if added_names:
                summary_lines.append(
                    f"✅ <b>Added {len(added_names)} Worker Bot(s):</b>\n• "
                    + "\n• ".join(added_names)
                )
            if failed_reasons:
                summary_lines.append(
                    f"⚠️ <b>Skipped/Failed:</b> {', '.join(failed_reasons)}"
                )
            await safe_edit_text(status_msg, "\n\n".join(summary_lines))
            asyncio.create_task(_delete_after_delay(status_msg, 8))

        text, markup = await build_main_bots_panel()
        await safe_edit_text(query.message, text, reply_markup=markup)


async def _delete_after_delay(msg: Message, delay: int = 8):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass
