import asyncio
import inspect
import re
import time
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Tuple

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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import info
from Script import script

logger = getLogger(__name__)
logger.setLevel(ERROR)

# Backup original Script.py and info.py defaults so /default... and /rem... restore them cleanly
_ORIG_START_TXT = getattr(script, "START_TXT", "")
_ORIG_HELP_TXT = getattr(script, "HELP_TXT", "")
_ORIG_ABOUT_TXT = getattr(script, "ABOUT_TXT", "")

_ORIG_START_IMG = getattr(info, "START_IMG", None)
_ORIG_HELP_IMG = getattr(info, "HELP_IMG", None)
_ORIG_ABOUT_IMG = getattr(info, "ABOUT_IMG", None)

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


# ============================================================
# 🎨 SAFE TELEGRAM BUTTON STYLE DETECTION
# ============================================================
BTN_PRIMARY = None
BTN_SECONDARY = None
BTN_SUCCESS = None
BTN_DANGER = None
_SUPPORTS_BTN_STYLE = False

try:
    from pyrogram.enums import ButtonStyle

    if "style" in inspect.signature(InlineKeyboardButton.__init__).parameters:
        BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", None)
        BTN_SECONDARY = getattr(ButtonStyle, "SECONDARY", None)
        BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", None)
        BTN_DANGER = getattr(ButtonStyle, "DANGER", None)
        _SUPPORTS_BTN_STYLE = True
except (ImportError, ValueError, TypeError):
    BTN_PRIMARY = BTN_SECONDARY = BTN_SUCCESS = BTN_DANGER = None
    _SUPPORTS_BTN_STYLE = False


def get_color_style(color_name: Optional[str]) -> Any:
    if not color_name or not _SUPPORTS_BTN_STYLE:
        return None
    return {
        "blue": BTN_PRIMARY,
        "green": BTN_SUCCESS,
        "red": BTN_DANGER,
        "gray": BTN_SECONDARY,
        "normal": None,
    }.get(str(color_name).strip().lower(), None)


def create_styled_btn(
    text: str,
    url: Optional[str] = None,
    callback_data: Optional[str] = None,
    color: Optional[str] = None,
) -> InlineKeyboardButton:
    kwargs: Dict[str, Any] = {"text": str(text)}
    if url:
        kwargs["url"] = str(url).strip()
    elif callback_data:
        kwargs["callback_data"] = str(callback_data)[:64]

    style_obj = get_color_style(color)
    if style_obj is not None:
        kwargs["style"] = style_obj

    try:
        return InlineKeyboardButton(**kwargs)
    except Exception:
        kwargs.pop("style", None)
        return InlineKeyboardButton(**kwargs)


# ============================================================
# ⚙️ MONGODB SETUP & ULTRA-FAST IN-MEMORY CACHE
# ============================================================
_ui_settings = None
_UI_CACHE: Optional[Dict[str, Any]] = None
_UI_CACHE_TS: float = 0.0


def get_db():
    global _ui_settings
    if _ui_settings is None:
        client = AsyncIOMotorClient(info.DATABASE_URI)
        _ui_settings = client[info.DATABASE_NAME]["ui_config"]
    return _ui_settings


def _sync_ui_to_runtime(doc: Dict[str, Any]):
    """Synchronizes custom UI texts and images directly into Script.py and info.py."""
    if not isinstance(doc, dict):
        return

    script.START_TXT = doc.get("start_text") or _ORIG_START_TXT
    script.HELP_TXT = doc.get("help_text") or _ORIG_HELP_TXT
    script.ABOUT_TXT = doc.get("about_text") or _ORIG_ABOUT_TXT

    if "start_img" in doc:
        info.START_IMG = doc["start_img"] if doc["start_img"] else _ORIG_START_IMG
    if "help_img" in doc:
        info.HELP_IMG = doc["help_img"] if doc["help_img"] else _ORIG_HELP_IMG
    if "about_img" in doc:
        info.ABOUT_IMG = doc["about_img"] if doc["about_img"] else _ORIG_ABOUT_IMG


async def get_ui(force_refresh: bool = False) -> Dict[str, Any]:
    global _UI_CACHE, _UI_CACHE_TS
    now = time.time()
    if not force_refresh and _UI_CACHE is not None and (now - _UI_CACHE_TS) < 60:
        return _UI_CACHE

    try:
        doc = await get_db().find_one({"_id": "bot_ui"}) or {}
    except Exception as e:
        logger.error(f"get_ui DB error: {e}")
        doc = _UI_CACHE or {}

    _UI_CACHE = doc
    _UI_CACHE_TS = now
    _sync_ui_to_runtime(doc)
    return doc


async def update_ui(key: str, value: Any):
    global _UI_CACHE, _UI_CACHE_TS
    try:
        await get_db().update_one(
            {"_id": "bot_ui"}, {"$set": {key: value}}, upsert=True
        )
    except Exception as e:
        logger.error(f"update_ui DB error: {e}")

    if _UI_CACHE is None:
        _UI_CACHE = {}
    _UI_CACHE[key] = value
    _UI_CACHE_TS = time.time()
    _sync_ui_to_runtime(_UI_CACHE)


def build_custom_button_rows(
    btns_data: List[Dict[str, Any]],
) -> List[List[InlineKeyboardButton]]:
    """Builds inline button rows honoring sidebyside / belowside layout and button colors."""
    rows: List[List[InlineKeyboardButton]] = []
    if not btns_data or not isinstance(btns_data, list):
        return rows

    for b in btns_data:
        if not isinstance(b, dict) or not b.get("text") or not b.get("url"):
            continue
        btn_obj = create_styled_btn(
            text=b["text"],
            url=b["url"],
            color=b.get("color", "blue"),
        )
        if b.get("layout") == "sidebyside" and rows and len(rows[-1]) < 2:
            rows[-1].append(btn_obj)
        else:
            rows.append([btn_obj])
    return rows


async def get_custom_menu_rows(menu_name: str) -> List[List[InlineKeyboardButton]]:
    """Used by plugins/uibuttons.py to attach custom start/help/about buttons."""
    ui = await get_ui()
    btns_data = ui.get(f"{menu_name}_buttons", [])
    return build_custom_button_rows(btns_data)


# ============================================================
# 👑 FOOLPROOF ADMIN CHECKER
# ============================================================
def get_admin_list() -> List[int]:
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        return [
            int(x)
            for x in str(raw_admins).replace(",", " ").split()
            if x.strip().lstrip("-").isdigit()
        ]
    elif isinstance(raw_admins, (list, tuple, set)):
        return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]
    return []


async def is_global_admin(message: Message) -> bool:
    admins = get_admin_list()
    if message.from_user and message.from_user.id in admins:
        return True
    if message.sender_chat and message.sender_chat.id in admins:
        return True
    return False


# ============================================================
# ⚡ 10-LAYER TITANIUM UPLOAD ENGINE (ZERO-FAIL)
# ============================================================
def _upload_sync(file_bytes: bytes) -> Optional[str]:
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
        (
            "https://pixeldrain.com/api/file",
            {"file": ("img.jpg", file_bytes, "image/jpeg")},
            None,
        ),
        ("https://file.io", {"file": ("img.jpg", file_bytes, "image/jpeg")}, None),
        (
            "https://telegra.ph/upload",
            {"file": ("img.jpg", file_bytes, "image/jpeg")},
            None,
        ),
    ]

    for url, files_dict, data_dict in endpoints:
        try:
            res = requests.post(
                url, data=data_dict, files=files_dict, headers=headers, timeout=6
            )
            if res.status_code in [200, 201]:
                if url in ["https://graph.org/upload", "https://telegra.ph/upload"]:
                    return url.rsplit("/", 1)[0] + res.json()[0]["src"]
                elif "pixeldrain" in url and res.json().get("success"):
                    return "https://pixeldrain.com/api/file/" + res.json()["id"]
                elif "file.io" in url and res.json().get("success"):
                    return res.json()["link"]
                elif res.text.strip().startswith("http"):
                    return res.text.strip()
        except Exception:
            continue
    return None


async def upload_image_safely(
    client: Client, message: Message
) -> Tuple[Optional[str], Optional[str]]:
    try:
        file_io = await client.download_media(message, in_memory=True)
        if not file_io:
            return None, "❌ Failed to download the image."
        url = await asyncio.to_thread(_upload_sync, file_io.getvalue())
        if not url:
            return None, "❌ All cloud uploaders failed."
        return url, None
    except Exception as e:
        return None, f"❌ Internal Error: {e}"


# ============================================================
# 🛠️ TEXT & BUTTON PARSER HELPERS
# ============================================================
def extract_custom_html_text(message: Message) -> Optional[str]:
    """Extracts HTML-formatted text from either a command argument or a replied message."""
    if (
        message.reply_to_message
        and len(getattr(message, "command", [])) == 1
        and (message.reply_to_message.text or message.reply_to_message.caption)
    ):
        raw = message.reply_to_message.text or message.reply_to_message.caption
        return getattr(raw, "html", str(raw)).strip()

    if message.text and len(getattr(message, "command", [])) > 1:
        html_str = getattr(message.text, "html", str(message.text))
        cleaned = re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s+", "", html_str, count=1).strip()
        return cleaned if cleaned else None

    if message.text and not message.text.startswith("/"):
        html_str = getattr(message.text, "html", str(message.text))
        return html_str.strip()

    return None


def _normalize_url(raw_url: str) -> str:
    u = raw_url.strip()
    if not u.startswith(("http://", "https://", "tg://")):
        u = "https://" + u
    return u


def parse_button_cmd(
    message: Message,
) -> Tuple[Optional[str], Optional[str], str, str]:
    """
    Parses button text, URL, optional layout, and optional color from:
    - /addstartbutton "Text" https://link.com [sidebyside/belowside] [green/red/blue/gray]
    - /addstartbutton Text | https://link.com [| sidebyside | green]
    - /addstartbutton Text - https://link.com
    """
    raw = message.text or message.caption or ""
    body = re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s*", "", raw, count=1).strip()
    if not body and message.reply_to_message:
        body = (
            message.reply_to_message.text or message.reply_to_message.caption or ""
        ).strip()

    if not body:
        return None, None, "belowside", "blue"

    layout = "belowside"
    color = "blue"

    if "|" in body:
        parts = [p.strip() for p in body.split("|") if p.strip()]
        if len(parts) >= 2:
            btn_text = parts[0].strip("\"'“”")
            url = _normalize_url(parts[1])
            for extra in parts[2:]:
                el = extra.lower()
                if el in ["sidebyside", "belowside"]:
                    layout = el
                elif el in ["green", "red", "blue", "gray", "normal"]:
                    color = el
            return btn_text, url, layout, color

    match = re.search(r'["“\']([^"”\']+)["”\']\s+(\S+)(?:\s+(\S+))?(?:\s+(\S+))?', body)
    if match:
        btn_text = match.group(1).strip()
        url = _normalize_url(match.group(2).strip())
        for extra in (match.group(3), match.group(4)):
            if extra:
                el = extra.lower()
                if el in ["sidebyside", "belowside"]:
                    layout = el
                elif el in ["green", "red", "blue", "gray", "normal"]:
                    color = el
        return btn_text, url, layout, color

    if " - " in body:
        parts = body.split(" - ", 1)
        return parts[0].strip("\"'“” "), _normalize_url(parts[1]), layout, color

    tokens = body.split()
    if len(tokens) >= 2 and (
        tokens[-1].startswith(("http://", "https://", "t.me/", "tg://"))
        or "." in tokens[-1]
    ):
        url = _normalize_url(tokens[-1])
        btn_text = " ".join(tokens[:-1]).strip("\"'“” ")
        if btn_text:
            return btn_text, url, layout, color

    return None, None, layout, color


def parse_edit_button_args(
    text: str,
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Parses button number, layout, and color in any order:
    e.g., `/editstartbuttons 1 sidebyside green` or `/editstartbuttons 1 green sidebyside`
    """
    raw = re.sub(r"^/\w+(?:@[a-zA-Z0-9_]+)?\s*", "", text or "", count=1).strip()
    parts = [p.strip().lower() for p in raw.replace("|", " ").split() if p.strip()]
    if len(parts) < 2 or not parts[0].isdigit():
        return None, None, None

    idx = int(parts[0]) - 1
    layout = None
    color = None

    for token in parts[1:]:
        if token in ["sidebyside", "belowside"]:
            layout = token
        elif token in ["green", "red", "blue", "gray", "normal"]:
            color = token

    return idx, layout, color


def format_btn_list(btns: List[Dict[str, Any]]) -> str:
    if not btns:
        return "<i>No custom buttons configured.</i>"
    lines = []
    for i, b in enumerate(btns, 1):
        lines.append(
            f"<b>{i}.</b> <code>{b.get('text')}</code> — Layout: <code>{b.get('layout', 'belowside')}</code> | Color: <code>{b.get('color', 'blue')}</code>"
        )
    return "\n".join(lines)


async def process_image_upload(
    client: Client, message: Message, db_key: str, menu_name: str
):
    target_msg = None
    if message.reply_to_message and (
        message.reply_to_message.photo or message.reply_to_message.document
    ):
        target_msg = message.reply_to_message
    elif message.photo or message.document:
        target_msg = message

    # Also allow passing a direct image URL: /setstartimage https://...
    if target_msg is None and message.text and len(getattr(message, "command", [])) > 1:
        arg = message.text.split(None, 1)[1].strip()
        if arg.startswith(("http://", "https://")) or len(arg) > 20:
            await update_ui(db_key, arg)
            return await safe_reply_text(
                message,
                f"✅ <b>{menu_name} Image updated successfully!</b>",
                parse_mode=enums.ParseMode.HTML,
            )

    if target_msg is None:
        return await safe_reply_text(
            message,
            f"⚠️ <b>Usage:</b> Reply to a photo with <code>/{message.command[0]}</code> (or provide an image URL).",
            parse_mode=enums.ParseMode.HTML,
        )

    status = await safe_reply_text(
        message,
        "⏳ <b>Uploading to Cloud (Testing 10 Hosters)...</b>",
        parse_mode=enums.ParseMode.HTML,
    )
    url, _ = await upload_image_safely(client, target_msg)

    if url:
        await update_ui(db_key, url)
        if status:
            await safe_edit_text(
                status,
                f"✅ <b>{menu_name} Image updated!</b>\n<i>(Saved permanently to cloud)</i>",
                parse_mode=enums.ParseMode.HTML,
            )
    else:
        file_id = (
            target_msg.photo.file_id
            if target_msg.photo
            else getattr(target_msg.document, "file_id", None)
        )
        if not file_id:
            if status:
                return await safe_edit_text(
                    status,
                    "❌ <b>Could not extract valid photo!</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return
        await update_ui(db_key, file_id)
        if status:
            await safe_edit_text(
                status,
                f"⚠️ Cloud blocked. <b>{menu_name} Image updated!</b>\n<i>(Saved as Telegram File_ID)</i>",
                parse_mode=enums.ParseMode.HTML,
            )


# ============================================================
# 🏠 START MENU CUSTOMIZATION
# ============================================================
@Client.on_message(filters.command("setstarttext") & filters.incoming, group=-2)
async def set_start_text(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    text = extract_custom_html_text(message)
    if not text:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/setstarttext &lt;text&gt;</code> (or reply to a message).",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("start_text", text)
    await safe_reply_text(
        message,
        "✅ <b>Start Menu Text updated!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("defaultstarttext") & filters.incoming, group=-2)
async def def_start_text(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("start_text", None)
    await safe_reply_text(
        message,
        "✅ <b>Start Menu Text reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setstartimage") & filters.incoming, group=-2)
async def set_start_img(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await process_image_upload(client, message, "start_img", "Start Menu")


@Client.on_message(filters.command("remstartimage") & filters.incoming, group=-2)
async def rem_start_img(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("start_img", None)
    await safe_reply_text(
        message,
        "🗑️ <b>Start Menu Image removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("addstartbutton") & filters.incoming, group=-2)
async def add_start_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    btn_text, url, layout, color = parse_button_cmd(message)
    if not btn_text or not url:
        return await safe_reply_text(
            message,
            '⚠️ <b>Usage:</b> <code>/addstartbutton "Button Text" https://link.com</code>\n'
            "Or: <code>/addstartbutton Button Text | https://link.com</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    ui = await get_ui()
    btns = list(ui.get("start_buttons", []))
    btns.append({"text": btn_text, "url": url, "layout": layout, "color": color})
    await update_ui("start_buttons", btns)
    await safe_reply_text(
        message,
        f"✅ <b>Start Button Added!</b>\n\n<b>Current Buttons:</b>\n{format_btn_list(btns)}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("editstartbuttons") & filters.incoming, group=-2)
async def edit_start_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    idx, layout, color = parse_edit_button_args(message.text or "")
    ui = await get_ui()
    btns = list(ui.get("start_buttons", []))

    if idx is None or idx < 0 or idx >= len(btns) or (not layout and not color):
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editstartbuttons &lt;number&gt; &lt;sidebyside/belowside&gt; &lt;color&gt;</code>\n"
            "<i>Example:</i> <code>/editstartbuttons 1 sidebyside green</code>\n\n"
            f"<b>Current Buttons:</b>\n{format_btn_list(btns)}",
            parse_mode=enums.ParseMode.HTML,
        )

    if layout:
        btns[idx]["layout"] = layout
    if color:
        btns[idx]["color"] = color
    await update_ui("start_buttons", btns)
    await safe_reply_text(
        message,
        f"✅ <b>Start Button #{idx + 1} Updated!</b>\n\n{format_btn_list(btns)}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remstartbutton") & filters.incoming, group=-2)
async def rem_start_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("start_buttons", [])
    await safe_reply_text(
        message,
        "🗑️ <b>All custom Start Menu buttons cleared.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# ℹ️ HELP MENU CUSTOMIZATION
# ============================================================
@Client.on_message(filters.command("sethelptext") & filters.incoming, group=-2)
async def set_help_text(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    text = extract_custom_html_text(message)
    if not text:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/sethelptext &lt;text&gt;</code> (or reply to a message).",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("help_text", text)
    await safe_reply_text(
        message,
        "✅ <b>Help Menu Text updated!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("defaulthelptext") & filters.incoming, group=-2)
async def def_help_text(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("help_text", None)
    await safe_reply_text(
        message,
        "✅ <b>Help Menu Text reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("sethelpimage") & filters.incoming, group=-2)
async def set_help_img(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await process_image_upload(client, message, "help_img", "Help Menu")


@Client.on_message(filters.command("remhelpimage") & filters.incoming, group=-2)
async def rem_help_img(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("help_img", None)
    await safe_reply_text(
        message,
        "🗑️ <b>Help Menu Image removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("addhelpbutton") & filters.incoming, group=-2)
async def add_help_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    btn_text, url, layout, color = parse_button_cmd(message)
    if not btn_text or not url:
        return await safe_reply_text(
            message,
            '⚠️ <b>Usage:</b> <code>/addhelpbutton "Button Text" https://link.com</code>\n'
            "Or: <code>/addhelpbutton Button Text | https://link.com</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    ui = await get_ui()
    btns = list(ui.get("help_buttons", []))
    btns.append({"text": btn_text, "url": url, "layout": layout, "color": color})
    await update_ui("help_buttons", btns)
    await safe_reply_text(
        message,
        f"✅ <b>Help Button Added!</b>\n\n<b>Current Buttons:</b>\n{format_btn_list(btns)}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("edithelpbuttons") & filters.incoming, group=-2)
async def edit_help_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    idx, layout, color = parse_edit_button_args(message.text or "")
    ui = await get_ui()
    btns = list(ui.get("help_buttons", []))

    if idx is None or idx < 0 or idx >= len(btns) or (not layout and not color):
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/edithelpbuttons &lt;number&gt; &lt;sidebyside/belowside&gt; &lt;color&gt;</code>\n"
            "<i>Example:</i> <code>/edithelpbuttons 1 belowside red</code>\n\n"
            f"<b>Current Buttons:</b>\n{format_btn_list(btns)}",
            parse_mode=enums.ParseMode.HTML,
        )

    if layout:
        btns[idx]["layout"] = layout
    if color:
        btns[idx]["color"] = color
    await update_ui("help_buttons", btns)
    await safe_reply_text(
        message,
        f"✅ <b>Help Button #{idx + 1} Updated!</b>\n\n{format_btn_list(btns)}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remhelpbutton") & filters.incoming, group=-2)
async def rem_help_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("help_buttons", [])
    await safe_reply_text(
        message,
        "🗑️ <b>All custom Help Menu buttons cleared.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 📖 ABOUT MENU CUSTOMIZATION
# ============================================================
@Client.on_message(filters.command("setabouttext") & filters.incoming, group=-2)
async def set_about_text(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    text = extract_custom_html_text(message)
    if not text:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/setabouttext &lt;text&gt;</code> (or reply to a message).",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("about_text", text)
    await safe_reply_text(
        message,
        "✅ <b>About Menu Text updated!</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("defaultabouttext") & filters.incoming, group=-2)
async def def_about_text(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("about_text", None)
    await safe_reply_text(
        message,
        "✅ <b>About Menu Text reset to default.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("setaboutimage") & filters.incoming, group=-2)
async def set_about_img(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await process_image_upload(client, message, "about_img", "About Menu")


@Client.on_message(filters.command("remaboutimage") & filters.incoming, group=-2)
async def rem_about_img(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("about_img", None)
    await safe_reply_text(
        message,
        "🗑️ <b>About Menu Image removed.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("addaboutbutton") & filters.incoming, group=-2)
async def add_about_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    btn_text, url, layout, color = parse_button_cmd(message)
    if not btn_text or not url:
        return await safe_reply_text(
            message,
            '⚠️ <b>Usage:</b> <code>/addaboutbutton "Button Text" https://link.com</code>\n'
            "Or: <code>/addaboutbutton Button Text | https://link.com</code>",
            parse_mode=enums.ParseMode.HTML,
        )
    ui = await get_ui()
    btns = list(ui.get("about_buttons", []))
    btns.append({"text": btn_text, "url": url, "layout": layout, "color": color})
    await update_ui("about_buttons", btns)
    await safe_reply_text(
        message,
        f"✅ <b>About Button Added!</b>\n\n<b>Current Buttons:</b>\n{format_btn_list(btns)}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("editaboutbuttons") & filters.incoming, group=-2)
async def edit_about_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    idx, layout, color = parse_edit_button_args(message.text or "")
    ui = await get_ui()
    btns = list(ui.get("about_buttons", []))

    if idx is None or idx < 0 or idx >= len(btns) or (not layout and not color):
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b> <code>/editaboutbuttons &lt;number&gt; &lt;sidebyside/belowside&gt; &lt;color&gt;</code>\n"
            "<i>Example:</i> <code>/editaboutbuttons 1 sidebyside blue</code>\n\n"
            f"<b>Current Buttons:</b>\n{format_btn_list(btns)}",
            parse_mode=enums.ParseMode.HTML,
        )

    if layout:
        btns[idx]["layout"] = layout
    if color:
        btns[idx]["color"] = color
    await update_ui("about_buttons", btns)
    await safe_reply_text(
        message,
        f"✅ <b>About Button #{idx + 1} Updated!</b>\n\n{format_btn_list(btns)}",
        parse_mode=enums.ParseMode.HTML,
    )


@Client.on_message(filters.command("remaboutbutton") & filters.incoming, group=-2)
async def rem_about_btn(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )
    await update_ui("about_buttons", [])
    await safe_reply_text(
        message,
        "🗑️ <b>All custom About Menu buttons cleared.</b>",
        parse_mode=enums.ParseMode.HTML,
    )


# ============================================================
# 🎯 LIVE UI PREVIEW ENGINE
# ============================================================
@Client.on_message(
    filters.command(["preview_ui", "preview"]) & filters.incoming, group=-2
)
async def preview_ui_cmd(client: Client, message: Message):
    if not await is_global_admin(message):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Global Bot Admin only.",
            parse_mode=enums.ParseMode.HTML,
        )

    if len(message.command) < 2 or message.command[1].lower() not in [
        "start",
        "help",
        "about",
    ]:
        return await safe_reply_text(
            message,
            "⚠️ <b>Usage:</b>\n<code>/preview_ui start</code>\n<code>/preview_ui help</code>\n<code>/preview_ui about</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    menu = message.command[1].lower()
    ui = await get_ui(force_refresh=True)

    fallback_map = {
        "start": getattr(script, "START_TXT", "Hello {mention}!"),
        "help": getattr(script, "HELP_TXT", "<b>Help Menu</b>"),
        "about": getattr(script, "ABOUT_TXT", "<b>About Bot</b>"),
    }
    text = ui.get(f"{menu}_text") or fallback_map[menu]
    img = ui.get(f"{menu}_img") or getattr(info, f"{menu.upper()}_IMG", None)
    btns_data = ui.get(f"{menu}_buttons", [])

    mention = message.from_user.mention if message.from_user else "Admin"
    text = text.replace("{mention}", mention)

    keyboard = build_custom_button_rows(btns_data)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    try:
        if img and len(text) <= 1024:
            await message.reply_photo(
                photo=img,
                caption=text,
                reply_markup=reply_markup,
                parse_mode=enums.ParseMode.HTML,
            )
        else:
            await safe_reply_text(
                message,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        await safe_reply_text(
            message,
            f"❌ <b>Preview Render Error:</b>\n<code>{e}</code>\n\n<i>(Check if your image URL or File ID is valid)</i>",
            parse_mode=enums.ParseMode.HTML,
        )
