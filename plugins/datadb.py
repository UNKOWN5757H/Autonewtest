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
from database.ia_filterdb import (
    DYNAMIC_DB_META,
    clear_all_dynamic_db_entries,
    db as _primary_db,
    extract_db_label_from_uri,
    get_all_configured_dbs,
    get_all_dbs_stats,
    is_multi_db_enabled,
    make_db_id,
    mark_db_error,
    register_dynamic_db_entry,
    set_multi_db_enabled,
    unregister_dynamic_db_entry,
)

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
        logger.error(f"safe_reply_text error in datadb: {e}")
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
        logger.error(f"safe_edit_text error in datadb: {e}")
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


def get_size_str(bytes_size: int) -> str:
    if not bytes_size or bytes_size < 0:
        return "0.00 MB"
    size = float(bytes_size)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


# ============================================================
# 🗄️ REDUNDANT PERSISTENCE ENGINE FOR UNLIMITED /datadb URLS
# ============================================================
_DATADB_CONFIG_LOADED = False
_DATADB_LOCK = asyncio.Lock()
_DATADB_WAITING_INPUT: Dict[int, Dict[str, Any]] = {}


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


def _preload_env_uris_if_present() -> List[Dict[str, str]]:
    """Also picks up SECOND_DATABASE_URI / THIRD_DATABASE_URI from info.py if set."""
    env_entries: List[Dict[str, str]] = []
    for idx, attr_uri, attr_name, default_lbl in (
        (2, "SECOND_DATABASE_URI", "SECOND_DATABASE_NAME", "Env DB 2"),
        (3, "THIRD_DATABASE_URI", "THIRD_DATABASE_NAME", "Env DB 3"),
    ):
        raw_uri = (getattr(info, attr_uri, "") or "").strip()
        if raw_uri and raw_uri.startswith(("mongodb://", "mongodb+srv://")) and raw_uri != info.DATABASE_URI:
            auto_lbl, parsed_db = extract_db_label_from_uri(raw_uri, idx)
            cfg_db_name = (getattr(info, attr_name, "") or parsed_db or info.DATABASE_NAME).strip()
            env_entries.append(
                {
                    "id": make_db_id(raw_uri),
                    "name": auto_lbl or default_lbl,
                    "uri": raw_uri,
                    "db_name": cfg_db_name,
                }
            )
    return env_entries


async def load_datadb_config(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Loads saved MongoDB URLs across Primary DB + all currently known DBs,
    registers every URL in `ia_filterdb`, and checks newly registered DBs too
    so even if DB 1 was 512MB full when you added URL #2, URL #2's copy is found!
    """
    global _DATADB_CONFIG_LOADED
    async with _DATADB_LOCK:
        if _DATADB_CONFIG_LOADED and not force_refresh:
            return {
                "enabled": is_multi_db_enabled(),
                "urls": list(DYNAMIC_DB_META),
            }

        merged_by_id: Dict[str, Dict[str, Any]] = {}
        latest_ts = -1.0
        enabled_state = is_multi_db_enabled()

        # 1. Include any pre-configured env URIs first
        for e_item in _preload_env_uris_if_present():
            merged_by_id[e_item["id"]] = e_item
            register_dynamic_db_entry(
                e_item["uri"], custom_name=e_item["name"], db_name=e_item["db_name"]
            )

        # 2. Keep any in-memory entries already registered
        for m_item in list(DYNAMIC_DB_META):
            if m_item.get("id") and m_item.get("uri"):
                merged_by_id[m_item["id"]] = dict(m_item)

        # 3. Iteratively scan Primary DB + any newly discovered dynamic DBs
        scanned_uris = set()
        for _pass in range(2):
            current_dbs = get_all_configured_dbs(include_when_disabled=True)
            for idx, target_db, _ in current_dbs:
                if target_db is None:
                    continue
                db_key = f"{id(target_db)}"
                if db_key in scanned_uris:
                    continue
                scanned_uris.add(db_key)

                try:
                    doc = await asyncio.wait_for(
                        target_db["datadb_config"].find_one({"_id": "master_config"}),
                        timeout=5.0,
                    )
                    if not doc:
                        continue
                    doc_ts = float(doc.get("updated_at", 0.0) or 0.0)
                    if doc_ts >= latest_ts:
                        latest_ts = doc_ts
                        enabled_state = bool(doc.get("enabled", True))

                    # If this latest doc explicitly cleared all URLs, respect it
                    if doc_ts >= latest_ts and doc.get("cleared_all") and not doc.get("urls"):
                        merged_by_id.clear()
                        continue

                    for u_entry in doc.get("urls", []):
                        if not isinstance(u_entry, dict) or not u_entry.get("uri"):
                            continue
                        u_str = str(u_entry["uri"]).strip()
                        if u_str == info.DATABASE_URI:
                            continue
                        u_id = str(u_entry.get("id") or make_db_id(u_str))
                        u_name = str(u_entry.get("name") or "").strip()
                        u_db_name = str(
                            u_entry.get("db_name") or info.DATABASE_NAME
                        ).strip()
                        reg = register_dynamic_db_entry(
                            u_str, custom_name=u_name, db_name=u_db_name
                        )
                        if reg:
                            merged_by_id[u_id] = reg
                except Exception as e:
                    mark_db_error(idx, e)
                    continue

        set_multi_db_enabled(enabled_state)
        _DATADB_CONFIG_LOADED = True
        return {
            "enabled": is_multi_db_enabled(),
            "urls": list(DYNAMIC_DB_META),
        }


async def save_datadb_config(cleared_all: bool = False):
    """
    Saves the `/datadb` configuration redundantly across Primary DB AND every
    connected dynamic DB so even if Primary DB is 512MB full, your URLs are
    permanently stored in the newly added DBs!
    """
    now = time.time()
    payload = {
        "enabled": is_multi_db_enabled(),
        "urls": list(DYNAMIC_DB_META),
        "cleared_all": bool(cleared_all),
        "updated_at": now,
    }

    all_dbs = get_all_configured_dbs(include_when_disabled=True)
    for idx, target_db, _ in all_dbs:
        if target_db is None:
            continue
        try:
            await asyncio.wait_for(
                target_db["datadb_config"].update_one(
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
                    await target_db["datadb_config"].update_one(
                        {"_id": "master_config"},
                        {"$set": payload},
                        upsert=True,
                    )
                    continue
                except Exception as retry_err:
                    mark_db_error(idx, retry_err)
            else:
                mark_db_error(idx, e)


# ============================================================
# 🔄 INTERACTIVE URL INPUT LISTENER
# ============================================================
def cancel_datadb_input(chat_id: int):
    session = _DATADB_WAITING_INPUT.pop(int(chat_id), None)
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


@Client.on_message(filters.private | filters.group, group=-17)
async def _datadb_url_listener(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in _DATADB_WAITING_INPUT:
        return

    if (
        message.text
        and message.text.startswith("/")
        and message.text.strip().lower() not in ["/cancel"]
    ):
        cancel_datadb_input(chat_id)
        return

    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    session = _DATADB_WAITING_INPUT.get(chat_id)
    if session and (uid == session["user_id"] or uid == chat_id):
        fut = session["future"]
        if not fut.done():
            fut.set_result(message)
        message.stop_propagation()


async def wait_for_db_url(
    chat_id: int, user_id: int, prompt_msg: Optional[Message], timeout: int = 120
) -> Optional[Message]:
    cancel_datadb_input(chat_id)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _DATADB_WAITING_INPUT[int(chat_id)] = {
        "future": future,
        "user_id": int(user_id),
        "prompt_msg": prompt_msg,
    }
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
    finally:
        if _DATADB_WAITING_INPUT.get(int(chat_id), {}).get("future") is future:
            _DATADB_WAITING_INPUT.pop(int(chat_id), None)
        if prompt_msg:
            try:
                await prompt_msg.delete()
            except Exception:
                pass


# ============================================================
# 🎨 UI BUILDERS FOR /datadb AND PAGINATED URL LIST (UNLIMITED)
# ============================================================
async def build_main_datadb_panel() -> Tuple[str, InlineKeyboardMarkup]:
    await load_datadb_config()
    enabled = is_multi_db_enabled()
    urls = list(DYNAMIC_DB_META)
    url_count = len(urls)
    total_clusters = 1 + (url_count if enabled else 0)
    total_capacity_mb = total_clusters * 512

    st = await get_all_dbs_stats()
    total_files = st.get("total_files", 0)
    total_used_str = get_size_str(st.get("total_used_bytes", 0))
    total_free_str = get_size_str(st.get("total_free_bytes", 0))
    active_idx = st.get("active_db_index", 1)

    status_badge = (
        "🟢 ON (Unlimited Multi-DB Auto-Overflow)"
        if enabled
        else "🔴 OFF (Primary DB Only)"
    )

    text = (
        "🗄️ <b>Multi-Database Cluster Manager (<code>/datadb</code>)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Multi-DB Status:</b> <code>{status_badge}</code>\n"
        f"🔗 <b>Added Extra DB URLs:</b> <code>{url_count} (Unlimited)</code>\n"
        f"🗄️ <b>Active Clusters:</b> <code>{total_clusters} DB(s) ({total_capacity_mb:,} MB Capacity)</code>\n"
        f"⚡ <b>Current Write DB:</b> <code>Database #{active_idx}</code>\n"
        f"📁 <b>Combined Total Files:</b> <code>{total_files:,}</code>\n"
        f"💾 <b>Total Used / Free:</b> <code>{total_used_str} Used</code> | <code>{total_free_str} Free</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>How Unlimited Multi-DB Works:</b>\n"
        "• Click <b>Add url</b> and send any number of MongoDB Atlas URLs.\n"
        "• When Database 1 reaches 512 MB, indexing & saving automatically overflow to URL #2, #3, #4, etc. without stopping!\n"
        "• Auto-Filter searches across all connected databases simultaneously."
    )

    on_label = "✅ Multi Database On" if enabled else "Multi Database On"
    off_label = "🚫 Off" if not enabled else "Off"

    buttons = [
        [
            create_btn(
                on_label,
                callback_data="ddb#on",
                style=BTN_SUCCESS if enabled else None,
            ),
            create_btn(
                off_label,
                callback_data="ddb#off",
                style=BTN_DANGER if not enabled else None,
            ),
        ],
        [
            create_btn(
                "Add url",
                callback_data="ddb#add",
                style=BTN_PRIMARY,
            ),
            create_btn(
                f"url list ({url_count})",
                callback_data="ddb#list#0",
                style=BTN_PRIMARY,
            ),
        ],
        [
            create_btn(
                "Remove url",
                callback_data="ddb#list#0",
            ),
            create_btn(
                "Remove All url",
                callback_data="ddb#remall_ask",
                style=BTN_DANGER,
            ),
        ],
        [
            create_btn("🔄 Refresh Stats", callback_data="ddb#main"),
            create_btn("🔐 Close", callback_data="ddb#close"),
        ],
    ]
    return text, InlineKeyboardMarkup(buttons)


async def build_url_list_panel(page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    await load_datadb_config()
    urls = list(DYNAMIC_DB_META)

    if not urls:
        text = (
            "📋 <b>Database URL List (0 Added)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🗄️ <b>Primary DB (DB 1):</b> <code>🟢 Active (info.DATABASE_URI)</code>\n\n"
            "<i>No extra MongoDB URLs added yet. Click <b>Add url</b> below to add unlimited MongoDB clusters!</i>"
        )
        buttons = [
            [
                create_btn("Add url", callback_data="ddb#add", style=BTN_PRIMARY),
                create_btn("⬅️ Back", callback_data="ddb#main"),
            ]
        ]
        return text, InlineKeyboardMarkup(buttons)

    st = await get_all_dbs_stats()
    stats_by_idx = {d["index"]: d for d in st.get("all_dbs", [])}

    per_page = 8
    total_pages = max(1, math.ceil(len(urls) / per_page))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    sliced_urls = urls[start_idx : start_idx + per_page]

    db1_info = stats_by_idx.get(1, {})
    db1_files = db1_info.get("files", 0)
    db1_used = get_size_str(db1_info.get("used_bytes", 0))

    lines = [
        f"📋 <b>Configured Database URLs ({len(urls)} Added • Page {page + 1}/{total_pages})</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🏠 <b>Primary DB #1:</b> <code>{db1_files:,} files</code> (<code>{db1_used} / 512 MB</code>)",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]
    buttons: List[List[InlineKeyboardButton]] = []

    for idx, item in enumerate(sliced_urls, start=start_idx + 1):
        db_num = idx + 1  # Because Primary DB is #1
        u_id = item.get("id", "")
        u_name = html.escape(str(item.get("name", f"URL #{idx}")))
        d_stat = stats_by_idx.get(db_num, {})
        files_cnt = d_stat.get("files", 0)
        used_str = get_size_str(d_stat.get("used_bytes", 0))
        status_str = d_stat.get("status", "🟢 Connected")

        lines.append(
            f"<b>{idx}. {u_name} (DB #{db_num})</b> — <code>{status_str}</code>\n"
            f"   └ 📁 <code>{files_cnt:,} files</code> | 💾 <code>{used_str} / 512 MB</code>"
        )

        # Exact requested button layout: Url name | Remove url
        buttons.append(
            [
                create_btn(
                    f"🗄️ {item.get('name', f'URL #{idx}')[:20]}",
                    callback_data=f"ddb#info#{u_id}",
                ),
                create_btn(
                    "Remove url",
                    callback_data=f"ddb#del#{u_id}#{page}",
                    style=BTN_DANGER,
                ),
            ]
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    if total_pages > 1:
        nav_row: List[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                create_btn(
                    "⬅️ Prev", callback_data=f"ddb#list#{page - 1}", style=BTN_PRIMARY
                )
            )
        nav_row.append(
            create_btn(f"📄 {page + 1}/{total_pages}", callback_data="ddb#noop")
        )
        if page < total_pages - 1:
            nav_row.append(
                create_btn(
                    "Next ➡️", callback_data=f"ddb#list#{page + 1}", style=BTN_PRIMARY
                )
            )
        buttons.append(nav_row)

    # Bottom row: Remove all urls
    buttons.append(
        [
            create_btn(
                "Remove all urls",
                callback_data="ddb#remall_ask",
                style=BTN_DANGER,
            )
        ]
    )
    buttons.append(
        [
            create_btn("Add url", callback_data="ddb#add", style=BTN_PRIMARY),
            create_btn("⬅️ Back", callback_data="ddb#main"),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ============================================================
# 🎯 /datadb COMMAND & CALLBACK HANDLERS
# ============================================================
@Client.on_message(
    filters.command(["datadb", "multidb", "dburls"]) & filters.incoming, group=-2
)
async def datadb_command_handler(client: Client, message: Message):
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

    cancel_datadb_input(message.chat.id)
    status_msg = await safe_reply_text(
        message, "⏳ <b>Loading Multi-Database Cluster Status...</b>"
    )
    text, markup = await build_main_datadb_panel()
    if status_msg:
        await safe_edit_text(status_msg, text, reply_markup=markup)
    else:
        await safe_reply_text(message, text, reply_markup=markup)


@Client.on_callback_query(filters.regex(r"^ddb#"), group=-2)
async def datadb_callback_handler(client: Client, query: CallbackQuery):
    uid = query.from_user.id if query.from_user else 0
    if not await is_global_admin(uid):
        return await safe_answer(query, "❌ Global Bot Admin only!", show_alert=True)

    chat_id = query.message.chat.id
    parts = query.data.split("#")
    action = parts[1] if len(parts) > 1 else "main"

    if action == "noop":
        return await safe_answer(query)

    if action == "cancel_input":
        cancel_datadb_input(chat_id)
        await safe_answer(query, "❌ Cancelled.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    cancel_datadb_input(chat_id)

    if action == "close":
        await safe_answer(query, "Closed!")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if action == "main":
        await safe_answer(query, "Refreshing Multi-Database panel...")
        text, markup = await build_main_datadb_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action in ["on", "off"]:
        new_state = action == "on"
        await load_datadb_config()
        set_multi_db_enabled(new_state)
        await save_datadb_config()

        await safe_answer(
            query,
            f"🗄️ Multi-Database {'ENABLED' if new_state else 'DISABLED'}!",
            show_alert=True,
        )
        text, markup = await build_main_datadb_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "list":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await safe_answer(query, "Loading URL list...")
        text, markup = await build_url_list_panel(page=page)
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "info":
        target_id = parts[2] if len(parts) > 2 else ""
        for idx, item in enumerate(DYNAMIC_DB_META, start=2):
            if item.get("id") == target_id:
                return await safe_answer(
                    query,
                    f"🗄️ DB #{idx}: {item.get('name')}\n📂 DB Name: {item.get('db_name')}",
                    show_alert=True,
                )
        return await safe_answer(query, "Database URL Info", show_alert=True)

    if action == "del":
        target_id = parts[2] if len(parts) > 2 else ""
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

        removed_name = "URL"
        for item in DYNAMIC_DB_META:
            if item.get("id") == target_id:
                removed_name = item.get("name", "URL")
                break

        if unregister_dynamic_db_entry(target_id):
            await save_datadb_config(cleared_all=(len(DYNAMIC_DB_META) == 0))
            await safe_answer(query, f"🗑️ Removed {removed_name}!", show_alert=True)
        else:
            await safe_answer(query, "URL already removed.")

        text, markup = await build_url_list_panel(page=page)
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "remall_ask":
        if not DYNAMIC_DB_META:
            return await safe_answer(
                query, "⚠️ No extra database URLs to remove!", show_alert=True
            )

        await safe_answer(query)
        confirm_kb = InlineKeyboardMarkup(
            [
                [
                    create_btn(
                        "🔥 Yes, Remove All URLs",
                        callback_data="ddb#remall_confirm",
                        style=BTN_DANGER,
                    )
                ],
                [
                    create_btn("⬅️ Cancel", callback_data="ddb#main"),
                ],
            ]
        )
        return await safe_edit_text(
            query.message,
            f"⚠️ <b>Are you sure you want to remove all {len(DYNAMIC_DB_META)} extra Database URLs?</b>\n\n"
            f"<i>(Your Primary DB 1 will remain untouched.)</i>",
            reply_markup=confirm_kb,
        )

    if action == "remall_confirm":
        # First clear datadb_config on all dynamic DBs before unregistering them
        for _, target_db, _ in get_all_configured_dbs(include_when_disabled=True):
            if target_db is not None and target_db is not _primary_db:
                try:
                    await target_db["datadb_config"].delete_many({})
                except Exception:
                    pass

        clear_all_dynamic_db_entries()
        await save_datadb_config(cleared_all=True)
        await safe_answer(query, "🗑️ All Extra Database URLs Removed!", show_alert=True)
        text, markup = await build_main_datadb_panel()
        return await safe_edit_text(query.message, text, reply_markup=markup)

    if action == "add":
        await load_datadb_config()
        cur_count = len(DYNAMIC_DB_META)

        await safe_answer(query)
        cancel_kb = InlineKeyboardMarkup(
            [[create_btn("❌ Cancel", callback_data="ddb#cancel_input")]]
        )
        prompt_msg = await safe_reply_text(
            query.message,
            f"🗄️ <b>Add MongoDB URL(s) (Currently Added: {cur_count} • Unlimited)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🔗 <b>Send your MongoDB Connection URI(s):</b>\n\n"
            "• You can send <b>1 URL</b> or <b>multiple URLs</b> (one per line)!\n"
            "• Optional custom name format:\n"
            "  <code>MyDB2 | mongodb+srv://user:pass@cluster...</code>\n"
            "• Or just send the raw URL directly:\n"
            "  <code>mongodb+srv://user:pass@cluster0.xxxx.mongodb.net/?retryWrites=true&w=majority</code>",
            reply_markup=cancel_kb,
        )

        resp = await wait_for_db_url(chat_id, uid, prompt_msg, timeout=120)
        if not resp or not resp.text:
            return
        if resp.text.strip().lower() == "/cancel":
            try:
                await resp.delete()
            except Exception:
                pass
            return

        raw_text = resp.text.strip()
        # Delete the message containing sensitive MongoDB credentials immediately
        try:
            await resp.delete()
        except Exception:
            pass

        lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
        candidates: List[Tuple[Optional[str], str]] = []
        for ln in lines:
            if "|" in ln:
                c_name, c_uri = [x.strip() for x in ln.split("|", 1)]
                if c_uri.startswith(("mongodb://", "mongodb+srv://")):
                    candidates.append((c_name, c_uri))
                    continue
            uri_matches = re.findall(r"(mongodb(?:\+srv)?://\S+)", ln)
            for um in uri_matches:
                candidates.append((None, um.strip()))

        if not candidates:
            err_m = await safe_reply_text(
                query.message,
                "❌ <b>Invalid MongoDB URI!</b>\nMust start with <code>mongodb+srv://</code> or <code>mongodb://</code>.",
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
            f"⏳ <b>Testing & connecting {len(candidates)} MongoDB URL(s)...</b>",
        )

        added_labels: List[str] = []
        failed_reasons: List[str] = []

        for custom_label, uri_str in candidates:
            if uri_str == info.DATABASE_URI:
                failed_reasons.append("Already set as Primary DATABASE_URI")
                continue

            if any(x.get("uri") == uri_str for x in DYNAMIC_DB_META):
                failed_reasons.append("URL already added")
                continue

            try:
                test_client = AsyncIOMotorClient(
                    uri_str,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=8000,
                )
                await asyncio.wait_for(test_client.admin.command("ping"), timeout=6.0)
                entry = register_dynamic_db_entry(uri_str, custom_name=custom_label)
                if entry:
                    added_labels.append(
                        f"<b>{html.escape(entry['name'])}</b> (DB: <code>{html.escape(entry['db_name'])}</code>)"
                    )
                else:
                    failed_reasons.append("Could not register URI")
            except Exception as e:
                failed_reasons.append(str(e)[:65])

        # Save across all connected DBs (including the newly added ones!)
        await save_datadb_config(cleared_all=False)
        # Also re-save multibot worker bots so the new DB gets a backup copy of worker bots too
        try:
            from plugins.multibot import save_multibot_config

            await save_multibot_config()
        except Exception:
            pass

        if status_msg:
            summary = []
            if added_labels:
                summary.append(
                    f"✅ <b>Connected & Saved {len(added_labels)} Database URL(s):</b>\n• "
                    + "\n• ".join(added_labels[:20])
                )
            if failed_reasons:
                summary.append(
                    f"⚠️ <b>Skipped/Failed:</b> {', '.join(failed_reasons[:5])}"
                )
            await safe_edit_text(status_msg, "\n\n".join(summary))
            asyncio.create_task(_delete_later(status_msg, 8))

        text, markup = await build_main_datadb_panel()
        await safe_edit_text(query.message, text, reply_markup=markup)


async def _delete_later(msg: Message, delay: int = 8):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass
