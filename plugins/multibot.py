import asyncio
import html
import inspect
import math
import re
import time
from logging import INFO, getLogger
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
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

try:
    from database.ia_filterdb import (
        db as _db1,
        get_all_configured_dbs,
        get_all_healthy_read_dbs,
        mark_db_error,
    )
except ImportError:
    _db1 = None
    get_all_configured_dbs = None
    get_all_healthy_read_dbs = None
    mark_db_error = None

logger = getLogger(__name__)
logger.setLevel(INFO)

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
# 🤖 REDUNDANT MULTI-DB & UNLIMITED WORKER BOT ENGINE
# ============================================================
_FALLBACK_DB = None
try:
    if _db1 is None:
        _DB_CLIENT = AsyncIOMotorClient(
            info.DATABASE_URI,
            serverSelectionTimeoutMS=4000,
            connectTimeoutMS=4000,
            socketTimeoutMS=8000,
        )
        _FALLBACK_DB = _DB_CLIENT[info.DATABASE_NAME]
except Exception:
    _FALLBACK_DB = None

_MULTIBOT_CONFIG_CACHE: Dict[str, Any] = {
    "enabled": bool(getattr(info, "MULTI_BOT_ENABLED", True)),
    "bots": [],  # Unlimited list of dicts: {"bot_id": int, "name": str, "username": str, "token": str}
}
_CONFIG_LOADED = False
ACTIVE_WORKER_CLIENTS: Dict[int, Client] = {}
_WORKER_LOCK = asyncio.Lock()
_MB_WAITING_INPUT: Dict[int, Dict[str, Any]] = {}


def _get_all_candidate_dbs() -> List[Tuple[int, Any]]:
    """Returns all configured databases (Primary DB + all /datadb URLs) even if Multi-DB search is toggled off."""
    if callable(get_all_configured_dbs):
        dbs = [
            (idx, handle)
            for idx, handle, _ in get_all_configured_dbs(include_when_disabled=True)
            if handle is not None
        ]
        if dbs:
            return dbs
    if callable(get_all_healthy_read_dbs):
        dbs = get_all_healthy_read_dbs()
        if dbs:
            return dbs
    out = []
    for idx, d in ((1, _db1), (1, _FALLBACK_DB)):
        if d is not None and all(x[1] is not d for x in out):
            out.append((idx, d))
    return out


def _is_quota_err(exc: Exception) -> bool:
    s = str(exc).lower()
    return "space quota" in s or "over your space" in s or "8000" in s


async def _free_temp_space_on_db(target_db: Any):
    """Frees temporary collections on a full 512MB DB so tiny config documents can be saved."""
    for temp_col in (
        "fsub_join_requests",
        "fsub_verified_users",
        "index_checkpoints",
        "index_resume",
        "broadcast_tasks",
        "countdown_tasks",
    ):
        try:
            await target_db[temp_col].delete_many({})
        except Exception:
            pass


async def load_multibot_config(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Loads and merges saved Worker Bots across Primary DB + all /datadb URLs
    plus any tokens in info.WORKER_BOT_TOKENS so no worker bots are ever lost on restart!
    """
    global _CONFIG_LOADED, _MULTIBOT_CONFIG_CACHE
    if _CONFIG_LOADED and not force_refresh:
        return _MULTIBOT_CONFIG_CACHE

    # Ensure dynamic /datadb URLs are loaded first so we scan all extra databases too
    try:
        from plugins.datadb import load_datadb_config

        await load_datadb_config()
    except Exception:
        pass

    merged_bots_by_id: Dict[int, Dict[str, Any]] = {}
    latest_updated_at = -1.0
    enabled_state = bool(_MULTIBOT_CONFIG_CACHE.get("enabled", True))
    cleared_all_ts = -1.0

    for idx, target_db in _get_all_candidate_dbs():
        try:
            doc = await asyncio.wait_for(
                target_db["multibot_workers"].find_one({"_id": "master_config"}),
                timeout=5.0,
            )
            if not doc:
                continue
            doc_ts = float(doc.get("updated_at", 0.0) or 0.0)
            if doc_ts >= latest_updated_at:
                latest_updated_at = doc_ts
                enabled_state = bool(doc.get("enabled", True))

            if doc.get("cleared_all") and not doc.get("bots"):
                cleared_all_ts = max(cleared_all_ts, doc_ts)
                continue

            for b in doc.get("bots", []):
                if not isinstance(b, dict) or not b.get("token"):
                    continue
                tok_str = str(b["token"]).strip()
                b_id = int(b.get("bot_id") or tok_str.split(":", 1)[0])
                merged_bots_by_id[b_id] = {
                    "bot_id": b_id,
                    "name": str(b.get("name") or f"Bot {b_id}"),
                    "username": str(b.get("username") or ""),
                    "token": tok_str,
                }
        except Exception as e:
            if callable(mark_db_error):
                mark_db_error(idx, e)
            continue

    # If the most recent action was "Remove All Bots", honor it
    if cleared_all_ts >= latest_updated_at and cleared_all_ts > 0:
        merged_bots_by_id.clear()

    # Merge any existing in-memory bots not yet flushed
    for b in _MULTIBOT_CONFIG_CACHE.get("bots", []):
        if isinstance(b, dict) and b.get("token"):
            b_id = int(b["bot_id"])
            if b_id not in merged_bots_by_id and cleared_all_ts < latest_updated_at:
                merged_bots_by_id[b_id] = b

    # Merge any tokens pre-configured via info.WORKER_BOT_TOKENS env var
    env_tokens = getattr(info, "WORKER_BOT_TOKENS", []) or []
    existing_tokens = {b["token"] for b in merged_bots_by_id.values()}
    for tok in env_tokens:
        tok_str = str(tok).strip()
        if tok_str and tok_str not in existing_tokens:
            prefix = tok_str.split(":", 1)[0]
            if prefix.isdigit():
                b_id = int(prefix)
                merged_bots_by_id[b_id] = {
                    "bot_id": b_id,
                    "name": f"Worker {prefix[-4:]}",
                    "username": "",
                    "token": tok_str,
                }
                existing_tokens.add(tok_str)

    _MULTIBOT_CONFIG_CACHE["enabled"] = enabled_state
    _MULTIBOT_CONFIG_CACHE["bots"] = list(merged_bots_by_id.values())
    _CONFIG_LOADED = True
    return _MULTIBOT_CONFIG_CACHE


async def save_multibot_config(cleared_all: bool = False):
    """
    Saves Multi-Bot configuration redundantly across Primary DB AND all /datadb URLs.
    If a DB hits the 512MB quota, automatically purges temporary collections and retries
    so worker bots never disappear on restart!
    """
    now = time.time()
    payload = {
        "enabled": bool(_MULTIBOT_CONFIG_CACHE.get("enabled", True)),
        "bots": list(_MULTIBOT_CONFIG_CACHE.get("bots", [])),
        "cleared_all": bool(cleared_all),
        "updated_at": now,
    }

    for idx, target_db in _get_all_candidate_dbs():
        try:
            await asyncio.wait_for(
                target_db["multibot_workers"].update_one(
                    {"_id": "master_config"},
                    {"$set": payload},
                    upsert=True,
                ),
                timeout=6.0,
            )
        except Exception as e:
            if _is_quota_err(e):
                try:
                    await _free_temp_space_on_db(target_db)
                    await target_db["multibot_workers"].update_one(
                        {"_id": "master_config"},
                        {"$set": payload},
                        upsert=True,
                    )
                    continue
                except Exception as retry_err:
                    if callable(mark_db_error):
                        mark_db_error(idx, retry_err)
            else:
                if callable(mark_db_error):
                    mark_db_error(idx, e)


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
            no_updates=True,
            workers=16,
            sleep_threshold=60,
        )
        await worker_client.start()
        if getattr(worker_client, "me", None):
            bot_data["name"] = worker_client.me.first_name or bot_data.get(
                "name", f"Bot {bot_id}"
            )
            bot_data["username"] = worker_client.me.username or bot_data.get(
                "username", ""
            )
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
    Ensures all configured worker bots (unlimited) are started and connected if Multi-Bot is ON.
    """
    async with _WORKER_LOCK:
        cfg = await load_multibot_config()
        if not cfg.get("enabled", True):
            return []

        bots_list = cfg.get("bots", [])
        if not bots_list:
            return []

        clients: List[Client] = []
        updated_metadata = False

        for i in range(0, len(bots_list), 15):
            batch = bots_list[i : i + 15]
            results = await asyncio.gather(
                *[start_single_worker(b) for b in batch], return_exceptions=True
            )
            for b, res in zip(batch, results):
                if isinstance(res, Client):
                    clients.append(res)
                    if b.get("username"):
                        updated_metadata = True

        if updated_metadata:
            asyncio.create_task(save_multibot_config())

        return clients


async def get_indexing_clients(main_bot: Client) -> List[Client]:
    """
    Returns [main_bot] + all active Worker Bots (unlimited) when Multi-Bot Mode is ON.
    """
    cfg = await load_multibot_config()
    if not cfg.get("enabled", True) or not cfg.get("bots"):
        return [main_bot]

    workers = await ensure_all_workers_started()
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
# 🎨 UI BUILDERS FOR /bots AND PAGINATED BOT LIST (UNLIMITED BOTS)
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
        f"🤖 <b>Added Worker Bots:</b> <code>{count} (Unlimited)</code>\n"
        f"⚡ <b>Online Workers:</b> <code>{online_count} Active</code>\n"
        f"🚀 <b>Indexing Speed:</b> <code>{speed_multiplier}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How Multi-Bot Works for 10 Lakh+ Files:</b>\n"
        "• Make your Worker Bots <b>Admin</b> in your file channel.\n"
        "• When you run <code>/index</code>, the channel is automatically split into equal parts across all bots simultaneously!\n"
        "• Saved permanently across your Multi-Database cluster (<code>/datadb</code>)."
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
                callback_data="mb#list#0",
                style=BTN_PRIMARY,
            ),
        ],
        [
            create_btn(
                "➖ Remove Bot",
                callback_data="mb#list#0",
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


async def build_bot_list_panel(page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = await load_multibot_config()
    bots = cfg.get("bots", [])

    if not bots:
        text = (
            "📋 <b>Worker Bot List (0 Added)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>No worker bots added yet. Click <b>➕ Add Bot</b> to add unlimited bot tokens for ultra-fast parallel indexing!</i>"
        )
        buttons = [
            [
                create_btn("➕ Add Bot", callback_data="mb#add", style=BTN_PRIMARY),
                create_btn("⬅️ Back", callback_data="mb#main"),
            ]
        ]
        return text, InlineKeyboardMarkup(buttons)

    per_page = 10
    total_pages = max(1, math.ceil(len(bots) / per_page))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    sliced_bots = bots[start_idx : start_idx + per_page]

    lines = [
        f"📋 <b>Configured Worker Bots ({len(bots)} Total • Page {page + 1}/{total_pages})</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    buttons: List[List[InlineKeyboardButton]] = []

    for idx, b in enumerate(sliced_bots, start=start_idx + 1):
        b_id = int(b.get("bot_id", 0))
        b_name = html.escape(str(b.get("name", f"Bot {idx}")))
        b_uname = html.escape(str(b.get("username", "unknown")))
        is_online = b_id in ACTIVE_WORKER_CLIENTS and getattr(
            ACTIVE_WORKER_CLIENTS[b_id], "is_connected", False
        )
        dot = "🟢" if is_online else "⚪"
        lines.append(f"{idx}. {dot} <b>{b_name}</b> (@{b_uname}) — <code>{b_id}</code>")

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
                    callback_data=f"mb#del#{b_id}#{page}",
                    style=BTN_DANGER,
                ),
            ]
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━")
    lines.append(
        "⚠️ <i>Remember to make all worker bots Admin in your indexing channel!</i>"
    )

    if total_pages > 1:
        nav_row: List[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                create_btn(
                    "⬅️ Prev", callback_data=f"mb#list#{page - 1}", style=BTN_PRIMARY
                )
            )
        nav_row.append(
            create_btn(f"📄 {page + 1}/{total_pages}", callback_data="mb#noop")
        )
        if page < total_pages - 1:
            nav_row.append(
                create_btn(
                    "Next ➡️", callback_data=f"mb#list#{page + 1}", style=BTN_PRIMARY
                )
            )
        buttons.append(nav_row)

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

    if action == "noop":
        return await safe_answer(query)

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

    if action == "list":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await safe_answer(query)
        text, markup = await build_bot_list_panel(page=page)
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "info":
        bot_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        return await safe_answer(query, f"🤖 Worker Bot ID: {bot_id}", show_alert=True)

    if action == "del":
        if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
            return await safe_answer(query, "Invalid Bot ID!", show_alert=True)
        target_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

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
        await save_multibot_config(cleared_all=(len(new_bots) == 0))

        if len(new_bots) < before_len:
            await safe_answer(query, f"🗑️ Removed {removed_name}!", show_alert=True)
        else:
            await safe_answer(query, "Bot already removed.")

        text, markup = await build_bot_list_panel(page=page)
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
        await save_multibot_config(cleared_all=True)
        await safe_answer(query, "🗑️ All Worker Bots Removed!", show_alert=True)
        text, markup = await build_main_bots_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "add":
        cfg = await load_multibot_config()
        cur_bots = cfg.get("bots", [])

        await safe_answer(query)
        cancel_kb = InlineKeyboardMarkup(
            [[create_btn("❌ Cancel", callback_data="mb#cancel_input")]]
        )
        prompt_msg = await safe_reply_text(
            query.message,
            f"🤖 <b>Add Worker Bot(s) (Currently Added: {len(cur_bots)} • Unlimited)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🔑 <b>Send the Bot Token(s) from @BotFather:</b>\n\n"
            "• You can send <b>1 token</b> or <b>multiple tokens</b> (one per line) to add unlimited bots at once!\n"
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

        await save_multibot_config(cleared_all=False)

        if status_msg:
            summary_lines = []
            if added_names:
                summary_lines.append(
                    f"✅ <b>Added {len(added_names)} Worker Bot(s):</b>\n• "
                    + "\n• ".join(added_names[:25])
                )
            if failed_reasons:
                summary_lines.append(
                    f"⚠️ <b>Skipped/Failed:</b> {', '.join(failed_reasons[:10])}"
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
