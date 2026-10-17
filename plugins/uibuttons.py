import asyncio
import builtins
import copy
import html
import importlib
import inspect
import math
import random
import re
import time
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import requests
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import Client, ContinuePropagation, StopPropagation, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    Forbidden,
    MediaCaptionTooLong,
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
from database.users_chats_db import db as _db
from info import LOG_CHANNEL, PICS
from Script import script
from utils import get_size, temp

logger = getLogger(__name__)
logger.setLevel(ERROR)

# In-memory cache of known registered user IDs so `/start file_...` never waits on MongoDB
_KNOWN_USERS_RAM: Set[int] = set()

# Safe optional imports
try:
    from database.ia_filterdb import Media
except Exception:
    Media = None

try:
    from database.plugin_dbs import plugin_db as _plugin_db
except Exception:
    _plugin_db = None

try:
    from utils import parse_text_and_markup as _parse_text_and_markup
except Exception:
    _parse_text_and_markup = None

try:
    from utils import get_settings, save_group_settings
except Exception:
    get_settings = None
    save_group_settings = None

MESSAGE_EMOJI_PLANE = '<tg-emoji emoji-id="5875465628285931233">✈️</tg-emoji> Telegram'
MESSAGE_EMOJI_LINK = '<tg-emoji emoji-id="5877465816030515018">🔗</tg-emoji> Link'

DEFAULT_CLEAN_WORDS = ["sandalwood", "mkv", "mp4", "avi", "webm", "zip", "rar"]

DEFAULT_CLEANFILE_TXT = (
    "🧹 <b>Clean Filename Words Manager</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "⚙️ <b>Current Mode:</b> {mode_str}\n"
    "🛡️ <b>Always Auto-Removed:</b> <code>Stylish Fonts</code>, <code>@usernames</code>, <code>links</code> & <code>symbols</code>\n"
    "🔢 <b>Total Active Words:</b> <code>{count}</code>\n\n"
    "📋 <b>Active Words Removed From Buttons:</b>\n{words_display}\n"
    "━━━━━━━━━━━━━━━━━━━━━\n"
    "<b>Commands & Interactive Controls:</b>\n"
    "• <code>/listcleanfilename</code> — View & manage active clean words.\n"
    "• <code>/addcleanfilename w1, w2</code> — Add words to existing list.\n"
    "• <code>/setcleanfilename w1, w2</code> — Replace custom words list.\n"
    "• <code>/defaultcleanfilename</code> — Restore default extensions & tags.\n"
    "• <code>/remcleanfilename</code> — Clear custom clean words."
)

# Direct Shortcut Command -> Help Section Key Map
HELP_SHORTCUT_MAP: Dict[str, str] = {
    "uistart": "helps_uistart",
    "uihelp": "helps_uihelp",
    "uiabout": "helps_uiabout",
    "welcome": "helps_welcome",
    "images": "helps_images",
    "spellcheck": "helps_spell",
    "autofilter": "helps_filters",
    "forcesub": "helps_forcesub",
    "forceadd": "helps_forceadd",
    "bans": "helps_bans",
    "deletes": "helps_delete",
    "broadcasts": "helps_promotions",
    "indexs": "helps_index",
    "settings": "helps_settings",
    "conection": "helps_connections",
    "connection": "helps_connections",
    "utilitys": "helps_utilities",
    "utilities": "helps_utilities",
    "custommessges": "helps_custommessages",
    "custommessages": "helps_custommessages",
    "customcaptions": "helps_customcaption",
    "backups": "helps_backup",
    "posthand": "helps_posthand",
}

# ============================================================
# 🎯 DIRECT PLUGIN FUNCTION ROUTING TABLE (ZERO SILENT FAILURES)
# ============================================================
_DIRECT_CMD_ROUTES: Dict[str, Tuple[str, str]] = {
    # UI Start / Help / About (plugins/editable.py)
    "setstarttext": ("plugins.editable", "set_start_text"),
    "defaultstarttext": ("plugins.editable", "def_start_text"),
    "setstartimage": ("plugins.editable", "set_start_img"),
    "remstartimage": ("plugins.editable", "rem_start_img"),
    "addstartbutton": ("plugins.editable", "add_start_btn"),
    "editstartbuttons": ("plugins.editable", "edit_start_btn"),
    "remstartbutton": ("plugins.editable", "rem_start_btn"),
    "sethelptext": ("plugins.editable", "set_help_text"),
    "defaulthelptext": ("plugins.editable", "def_help_text"),
    "sethelpimage": ("plugins.editable", "set_help_img"),
    "remhelpimage": ("plugins.editable", "rem_help_img"),
    "addhelpbutton": ("plugins.editable", "add_help_btn"),
    "edithelpbuttons": ("plugins.editable", "edit_help_btn"),
    "remhelpbutton": ("plugins.editable", "rem_help_btn"),
    "setabouttext": ("plugins.editable", "set_about_text"),
    "defaultabouttext": ("plugins.editable", "def_about_text"),
    "setaboutimage": ("plugins.editable", "set_about_img"),
    "remaboutimage": ("plugins.editable", "rem_about_img"),
    "addaboutbutton": ("plugins.editable", "add_about_btn"),
    "editaboutbuttons": ("plugins.editable", "edit_about_btn"),
    "remaboutbutton": ("plugins.editable", "rem_about_btn"),
    # Welcome & Global Images & Stopwords (plugins/custom_settings.py)
    "enablewelcome": ("plugins.custom_settings", "enable_welc"),
    "disablewelcome": ("plugins.custom_settings", "disable_welc"),
    "setwelcometxt": ("plugins.custom_settings", "set_welc_txt"),
    "setwelcomeimg": ("plugins.custom_settings", "set_welc_img"),
    "setwelcome": ("plugins.custom_settings", "set_welcome_both"),
    "delwelcome": ("plugins.custom_settings", "del_welcome"),
    "setautoimg": ("plugins.custom_settings", "set_auto_img"),
    "remautoimg": ("plugins.custom_settings", "rem_auto_img"),
    "setfilenotfoundimg": ("plugins.custom_settings", "set_fnf_img"),
    "remfilenotfoundimg": ("plugins.custom_settings", "rem_fnf_img"),
    "defaultfilenotfoundimg": ("plugins.custom_settings", "default_fnf_img"),
    "setfsubimg": ("plugins.custom_settings", "set_fsub_img"),
    "addstopwords": ("plugins.custom_settings", "add_stopwords"),
    "stopwords": ("plugins.custom_settings", "show_stopwords"),
    "remstopwords": ("plugins.custom_settings", "rem_stopwords"),
    "remallstopwords": ("plugins.custom_settings", "rem_all_stopwords"),
    "defaultstopwords": ("plugins.custom_settings", "default_stopwords"),
    "setnotfoundtext": ("plugins.custom_settings", "set_fnf_text"),
    "remnotfoundtext": ("plugins.custom_settings", "rem_fnf_text"),
    "defaultnotfoundtext": ("plugins.custom_settings", "def_fnf_text"),
    # Filters (plugins/pm_filter.py)
    "filter": ("plugins.pm_filter", "add_filter_cmd"),
    "addfilter": ("plugins.pm_filter", "add_premade_filter_cmd"),
    "filterimage": ("plugins.pm_filter", "edit_filter_image_cmd"),
    "editfiltercolur": ("plugins.pm_filter", "edit_filter_colour_cmd"),
    "editfiltercolour": ("plugins.pm_filter", "edit_filter_colour_cmd"),
    "delfilter": ("plugins.pm_filter", "del_filter_cmd"),
    "listfilters": ("plugins.pm_filter", "list_filters_cmd"),
    # Force Sub (plugins/fsub.py)
    "setfsub": ("plugins.fsub", "add_dynamic_fsub"),
    "rmfsub": ("plugins.fsub", "rem_fsub"),
    "rmallfsub": ("plugins.fsub", "rem_all_fsub"),
    "getallfsub": ("plugins.fsub", "get_all_fsub"),
    "totalrequests": ("plugins.fsub", "total_requests"),
    "purgerequests": ("plugins.fsub", "purge_requests"),
    "clearfsubusers": ("plugins.fsub", "clear_fsub_users_cmd"),
    # Force Add (plugins/force_add.py)
    "myadds": ("plugins.force_add", "my_adds"),
    "setforceadd": ("plugins.force_add", "set_force_add"),
    "remforceadd": ("plugins.force_add", "remove_force_add"),
    "getforceadd": ("plugins.force_add", "get_force_add"),
    "topaddall": ("plugins.force_add", "top_add_all"),
    "topadd24": ("plugins.force_add", "top_add_24"),
    "topadd7": ("plugins.force_add", "top_add_7"),
    "resetadddaily": ("plugins.force_add", "reset_add_daily"),
    "resetadd": ("plugins.force_add", "reset_all_adds"),
    # Bans, Enable/Disable, Admin Core (plugins/admincore.py & plugins/p_ttishow.py)
    "ban": ("plugins.admincore", "ban_user_cmd"),
    "unban": ("plugins.admincore", "unban_user_cmd"),
    "bannedusers": ("plugins.admincore", "check_banned_users"),
    "enable": ("plugins.p_ttishow", "enable_chat"),
    "disable": ("plugins.p_ttishow", "disable_chat"),
    "leave": ("plugins.channel", "leave_channel_cmd"),
    "leavechannel": ("plugins.channel", "leave_channel_cmd"),
    "channels": ("plugins.channel", "channels_dashboard_cmd"),
    "exportusers": ("plugins.channel", "export_users_cmd"),
    "exportgroups": ("plugins.channel", "export_groups_cmd"),
    "exportchannels": ("plugins.channel", "export_channels_cmd"),
    "connect": ("plugins.admincore", "addconnection_cmd"),
    "disconnect": ("plugins.admincore", "deleteconnection_cmd"),
    "connections": ("plugins.admincore", "connections_cmd"),
    "adminsettings": ("plugins.admincore", "show_admin_settings"),
    "logs": ("plugins.admincore", "get_logs_cmd"),
    "server": ("plugins.admincore", "server_stats_cmd"),
    "stats": ("plugins.admincore", "bot_stats_cmd"),
    "total": ("plugins.admincore", "bot_stats_cmd"),
    "optimize_db": ("plugins.admincore", "optimize_db_cmd"),
    "clearfiles": ("plugins.admincore", "wipe_prompt_cmd"),
    "clearusers": ("plugins.admincore", "wipe_prompt_cmd"),
    "cleanusers": ("plugins.admincore", "clean_users_cmd"),
    # Delete & Auto-Delete (plugins/delete.py)
    "delete": ("plugins.delete", "delete_single_file"),
    "delmulti": ("plugins.delete", "delete_multiple_files"),
    "autodelete": ("plugins.delete", "set_autodelete_timer"),
    "buttondel": ("plugins.delete", "set_button_autodelete_timer"),
    "purgeduplicates": ("plugins.delete", "purge_duplicates_cmd"),
    # Broadcast & Promotions (plugins/broadcast.py)
    "addpromo": ("plugins.broadcast", "add_promo_handler"),
    "delpromo": ("plugins.broadcast", "del_promo_handler"),
    "listpromos": ("plugins.broadcast", "list_promo_handler"),
    "broadcast": ("plugins.broadcast", "user_broadcast"),
    "group_broadcast": ("plugins.broadcast", "group_broadcast"),
    "listbroadcast": ("plugins.broadcast", "list_broadcast_status_cmd"),
    # Indexing (plugins/index.py)
    "index": ("plugins.index", "index_command"),
    "indexdoc": ("plugins.index", "index_command"),
    "indexvideo": ("plugins.index", "index_command"),
    "indexaudio": ("plugins.index", "index_command"),
    "resumeindex": ("plugins.index", "resume_index_command"),
    "setskip": ("plugins.index", "set_skip_number"),
    "deleteskip": ("plugins.index", "delete_skip_number"),
    "currentskip": ("plugins.index", "current_skip_number"),
    # Custom Messages & Captions (plugins/custommessage.py & plugins/caption.py)
    "infomsg": ("plugins.custommessage", "set_info_msg"),
    "infoimg": ("plugins.custommessage", "set_info_img"),
    "fsubmsg": ("plugins.custommessage", "set_fsub_msg"),
    "fsubimg": ("plugins.custommessage", "set_fsub_img"),
    "delmsg": ("plugins.custommessage", "set_del_msg"),
    "delimg": ("plugins.custommessage", "set_del_img"),
    "notfoundmsg": ("plugins.custommessage", "set_notfound_msg"),
    "notfoundimg": ("plugins.custommessage", "set_notfound_img"),
    "customcaption": ("plugins.caption", "set_file_caption_cmd"),
    "captionplus": ("plugins.caption", "set_caption_plus_cmd"),
    # Backup (plugins/backup.py)
    "dbbackup": ("plugins.backup", "db_backup_cmd"),
    "dbrestore": ("plugins.backup", "db_restore_cmd"),
    "dbstats": ("plugins.backup", "db_stats_cmd"),
    "dbschedule": ("plugins.backup", "db_schedule_cmd"),
    # Post Handler (plugins/post_handler.py)
    "post": ("plugins.post_handler", "post_command"),
    "editpost": ("plugins.post_handler", "edit_post_cmd"),
    "edittitle": ("plugins.post_handler", "cmd_edit_title"),
    "edityear": ("plugins.post_handler", "cmd_edit_year"),
    "editlangs": ("plugins.post_handler", "cmd_edit_langs"),
    "editresolutions": ("plugins.post_handler", "cmd_edit_res"),
    "editgenres": ("plugins.post_handler", "cmd_edit_gen"),
    "editotts": ("plugins.post_handler", "cmd_edit_ott"),
    "editbutton": ("plugins.post_handler", "cmd_edit_post_button"),
    "editbuttoncolour": ("plugins.post_handler", "cmd_edit_btn_color"),
    "editdirect": ("plugins.post_handler", "cmd_edit_post_direct"),
    "setpoststicker": ("plugins.post_handler", "set_autopost_sticker"),
    "rempoststicker": ("plugins.post_handler", "rem_autopost_sticker"),
}


# ============================================================
# 🔗 LIVE CONNECTION TO plugins/editable.py
# ============================================================
async def _ensure_ui_synced() -> Dict[str, Any]:
    """Ensures custom UI texts & images from plugins/editable.py are synced to runtime."""
    try:
        from plugins.editable import get_ui

        return await get_ui()
    except Exception:
        return {}


async def _get_custom_rows(menu_name: str) -> List[List[InlineKeyboardButton]]:
    """Fetches custom inline button rows configured via plugins/editable.py."""
    try:
        from plugins.editable import get_custom_menu_rows

        return await get_custom_menu_rows(menu_name)
    except Exception:
        return []


# ============================================================
# 🎨 SAFE TELEGRAM BUTTON STYLE DETECTION
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


async def is_user_banned(user_id: int) -> bool:
    """Fast 0ms RAM ban check via temp.BANNED_USERS."""
    if not user_id:
        return False
    if hasattr(temp, "BANNED_USERS") and int(user_id) in temp.BANNED_USERS:
        return True
    return False


def create_btn(
    text: str,
    url: Optional[str] = None,
    callback_data: Optional[str] = None,
    style: Any = None,
) -> InlineKeyboardButton:
    """Creates an InlineKeyboardButton safely. Pass style=None for no colour."""
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
# ⚡ CLOUD IMAGE UPLOADER & MARKUP PARSER HELPERS
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
            if res.status_code == 200:
                if url == "https://graph.org/upload":
                    return "https://graph.org" + res.json()[0]["src"]
                elif res.text.strip().startswith("http"):
                    return res.text.strip()
        except Exception:
            continue
    return None


async def upload_image_safely(client: Client, message: Message) -> Optional[str]:
    try:
        file_io = await client.download_media(message, in_memory=True)
        if not file_io:
            return None
        return await asyncio.to_thread(_upload_sync, file_io.getvalue())
    except Exception:
        return None


def safe_parse_text_and_markup(text: str, default_markup=None):
    if not text:
        return "", default_markup
    if callable(_parse_text_and_markup):
        try:
            parsed_text, parsed_markup = _parse_text_and_markup(text, default_markup)
            return parsed_text or text, parsed_markup or default_markup
        except Exception as e:
            logger.error(f"parse_text_and_markup fallback triggered: {e}")
    return text, default_markup


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return ""


def format_script_text(
    template: str,
    mention: str = "User",
    uname: str = None,
    bname: str = None,
    **extra_kwargs,
) -> str:
    if not template:
        return ""
    uname = uname or getattr(temp, "U_NAME", None) or "Bot"
    bname = bname or getattr(temp, "B_NAME", None) or "MovieBot"

    mapping = _SafeFormatDict(
        mention=mention,
        user=mention,
        uname=uname,
        bname=bname,
        bot=bname,
        plane_emoji=MESSAGE_EMOJI_PLANE,
        link_emoji=MESSAGE_EMOJI_LINK,
        **extra_kwargs,
    )

    try:
        return template.format_map(mapping)
    except (IndexError, ValueError, KeyError):
        try:
            return template.format(
                mention,
                uname,
                bname,
                MESSAGE_EMOJI_PLANE,
                MESSAGE_EMOJI_LINK,
                **mapping,
            )
        except Exception:
            try:
                return template.format(mention, bname)
            except Exception:
                return template


# ============================================================
# ⚙️ MONGODB & CACHED PM SETTINGS HELPERS
# ============================================================
pm_db = cw_db = _BOT_DB = None
_PM_SETTINGS_CACHE: Tuple[float, Dict[str, Any]] = (0.0, {})

try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    pm_db = _BOT_DB["pm_settings"]
    cw_db = _BOT_DB["clean_words"]
except Exception as e:
    logger.error(f"Failed to init databases in uibuttons: {e}")


async def get_pm_settings(force_refresh: bool = False) -> Dict[str, Any]:
    global _PM_SETTINGS_CACHE
    now = time.time()
    if (
        not force_refresh
        and _PM_SETTINGS_CACHE[1]
        and (now - _PM_SETTINGS_CACHE[0]) < 120
    ):
        return _PM_SETTINGS_CACHE[1]

    default_settings = {
        "enabled": True,
        "reply_admins": True,
        "text": (
            "<b>🚫 Don't Message Here, Message Here Only!</b>\n\n"
            "I do not respond to direct messages in PM. Please join our official group to request and download movies."
        ),
        "image": getattr(info, "NOT_FOUND_IMG", None),
        "button_text": "💬 Message Here Only",
        "button_url": "https://t.me/Sandalwood_Kannada_Group",
        "button_color": "red",
    }
    if pm_db is None:
        return default_settings
    try:
        settings = await pm_db.find_one({"id": "pm_config"})
        res = {**default_settings, **settings} if settings else default_settings
        _PM_SETTINGS_CACHE = (now, res)
        return res
    except Exception as e:
        logger.error(f"Error fetching PM settings: {e}")
        return default_settings


async def save_pm_settings(key: str, value: Any):
    global _PM_SETTINGS_CACHE
    if pm_db is not None:
        await pm_db.update_one({"id": "pm_config"}, {"$set": {key: value}}, upsert=True)
    _PM_SETTINGS_CACHE = (0.0, {})


async def get_clean_words_state() -> Tuple[bool, List[str]]:
    if cw_db is None:
        return True, list(DEFAULT_CLEAN_WORDS)
    try:
        doc = await cw_db.find_one({"id": "words"})
        if not doc or doc.get("use_default", True):
            return True, list(DEFAULT_CLEAN_WORDS)
        return False, list(doc.get("list", []))
    except Exception:
        return True, list(DEFAULT_CLEAN_WORDS)


def _invalidate_pm_filter_clean_cache():
    try:
        import plugins.pm_filter as _pmf

        _pmf._CLEAN_WORDS_CACHE = (0.0, DEFAULT_CLEAN_WORDS)
        for cache_attr in ("GLOBAL_SEARCH_CACHE", "POOL_SEARCH_CACHE"):
            c_obj = getattr(_pmf, cache_attr, None)
            if c_obj and hasattr(c_obj, "_data"):
                c_obj._data.clear()
    except Exception:
        pass


async def get_active_connected_group(
    user_id: int, fallback_chat: Optional[Message] = None
) -> Optional[int]:
    if fallback_chat and fallback_chat.chat.type in [
        enums.ChatType.GROUP,
        enums.ChatType.SUPERGROUP,
    ]:
        return int(fallback_chat.chat.id)
    try:
        from plugins.pm_filter import get_active_connection

        conn = await get_active_connection(str(user_id))
        if conn:
            return int(conn)
    except Exception:
        pass
    try:
        from database.connections_mdb import active_connection

        conn = await active_connection(str(user_id))
        if conn:
            return int(conn)
    except Exception:
        pass
    if _BOT_DB is not None:
        try:
            doc = await _BOT_DB["active_connections"].find_one(
                {"user_id": str(user_id)}
            )
            if doc and doc.get("group_id"):
                return int(doc["group_id"])
        except Exception:
            pass
    return None


# ============================================================
# 🔄 INTERACTIVE INPUT LISTENER FOR HELP BUTTONS (NON-STUCK)
# ============================================================
_CMD_WAITING_INPUT: Dict[int, Dict[str, Any]] = {}


def is_waiting_for_cmd_input(chat_id: int) -> bool:
    """Used by plugins/commands.py and plugins/index.py so auto-handlers do not interrupt interactive prompts."""
    return int(chat_id) in _CMD_WAITING_INPUT


def cancel_cmd_user_input(chat_id: int):
    session = _CMD_WAITING_INPUT.pop(int(chat_id), None)
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


@Client.on_message(filters.private | filters.group, group=-15)
async def _cmd_interactive_listener(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in _CMD_WAITING_INPUT:
        return
    if (
        message.text
        and message.text.startswith("/")
        and message.text.strip().lower() not in ["/cancel"]
    ):
        cancel_cmd_user_input(chat_id)
        return

    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    session = _CMD_WAITING_INPUT.get(chat_id)
    if session and (uid == session["user_id"] or uid == chat_id):
        fut = session["future"]
        if not fut.done():
            fut.set_result(message)
        message.stop_propagation()


async def wait_cmd_user_input(
    chat_id: int,
    user_id: int,
    prompt_msg: Optional[Message] = None,
    timeout: int = 90,
) -> Optional[Message]:
    cancel_cmd_user_input(chat_id)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _CMD_WAITING_INPUT[int(chat_id)] = {
        "future": future,
        "user_id": user_id,
        "prompt_msg": prompt_msg,
    }
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
    finally:
        if _CMD_WAITING_INPUT.get(int(chat_id), {}).get("future") is future:
            _CMD_WAITING_INPUT.pop(int(chat_id), None)
        if prompt_msg:
            try:
                await prompt_msg.delete()
            except Exception:
                pass


# ============================================================
# 🔘 KEYBOARD & BUTTON BUILDERS
# ============================================================
def get_close_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[create_btn("🔐 Close", callback_data="close_data")]])


def get_back_close_keyboard(back_cb: str = "help") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                create_btn("🔙 Back", callback_data=back_cb),
                create_btn("🔐 Close", callback_data="close_data"),
            ]
        ]
    )


def get_cancel_input_keyboard(back_cb: str = "help") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[create_btn("❌ Cancel", callback_data=f"hact#cancel_input#{back_cb}")]]
    )


def get_deleted_file_notify_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                create_btn(
                    "Movie Request 📝",
                    url="https://t.me/Sandalwood_Kannada_Group",
                    style=BTN_SUCCESS,
                )
            ]
        ]
    )


def get_default_file_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                create_btn(
                    text="🎥 ಕನ್ನಡ ಹೊಸ ಮೂವೀಗಳು 🎥",
                    url="https://t.me/sandalwood_kannada_moviesz",
                    style=BTN_SUCCESS,
                )
            ]
        ]
    )


def get_pm_autoreply_keyboard(
    btn_text: Optional[str],
    btn_url: Optional[str],
    btn_color_str: str = "red",
) -> Optional[InlineKeyboardMarkup]:
    if not btn_text or not btn_url:
        return None
    color_map = {
        "green": BTN_SUCCESS,
        "red": BTN_DANGER,
        "blue": BTN_PRIMARY,
        "normal": None,
    }
    return InlineKeyboardMarkup(
        [
            [
                create_btn(
                    btn_text,
                    url=btn_url,
                    style=color_map.get(str(btn_color_str).lower(), None),
                )
            ]
        ]
    )


def append_fsub_back_button(
    f_markup: Optional[InlineKeyboardMarkup],
) -> InlineKeyboardMarkup:
    rows = (
        list(f_markup.inline_keyboard)
        if f_markup and getattr(f_markup, "inline_keyboard", None)
        else []
    )
    rows.append(
        [
            create_btn("🔙 Back", callback_data="helps_forcesub"),
            create_btn("🔐 Close", callback_data="close_data"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def get_start_keyboard(client: Client, user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            create_btn(
                "✈️ Gʀᴏᴜᴘ 1",
                url="https://t.me/Sandalwood_Kannada_Group",
                style=BTN_PRIMARY,
            ),
            create_btn(
                "✈️ Gʀᴏᴜᴘ 2",
                url="https://t.me/Kannada_Filmy_Group",
                style=BTN_PRIMARY,
            ),
            create_btn(
                "✈️ Gʀᴏᴜᴘ 3",
                url="https://t.me/+GLsPkRgLGGszMzY1",
                style=BTN_PRIMARY,
            ),
        ]
    ]
    custom_rows = await _get_custom_rows("start")
    if custom_rows:
        buttons.extend(custom_rows)

    if await is_global_admin(user_id):
        buttons.append(
            [
                create_btn("ℹ️ 𝙷𝚎𝚕𝚙", callback_data="help"),
                create_btn("😊 𝙰𝚋𝚘𝚞𝚝", callback_data="about"),
            ]
        )
    buttons.append(
        [
            create_btn(
                "🔗 Nᴇᴡ Rᴇʟᴇᴀꜱᴇꜱ & Oᴛᴛ Uᴘᴅᴀᴛᴇꜱ",
                url="https://t.me/sandalwood_kannada_moviesz",
                style=BTN_SUCCESS,
            )
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def get_help_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            create_btn("🖥️ UI Start", callback_data="helps_uistart"),
            create_btn("🖥️ UI Help", callback_data="helps_uihelp"),
            create_btn("🖥️ UI About", callback_data="helps_uiabout"),
        ],
        [
            create_btn("👋 Welcome", callback_data="helps_welcome"),
            create_btn("🖼️ Images", callback_data="helps_images"),
        ],
        [
            create_btn("🔍 Spell Check", callback_data="helps_spell"),
            create_btn("📝 Filters", callback_data="helps_filters"),
        ],
        [
            create_btn("📱 Force Sub", callback_data="helps_forcesub"),
            create_btn("👥 Force Add", callback_data="helps_forceadd"),
        ],
        [
            create_btn("🚫 Bans", callback_data="helps_bans"),
            create_btn("🗑️ Delete", callback_data="helps_delete"),
        ],
        [
            create_btn("📢 Promotions", callback_data="helps_promotions"),
            create_btn("📚 Index", callback_data="helps_index"),
        ],
        [
            create_btn("⚙️ Settings", callback_data="helps_settings"),
            create_btn("🌐 Connections", callback_data="helps_connections"),
        ],
        [
            create_btn("📊 Utilities", callback_data="helps_utilities"),
            create_btn("💬 Custom Messages", callback_data="helps_custommessages"),
        ],
        [
            create_btn("📝 Post Handle", callback_data="helps_posthand"),
            create_btn("📝 Custom Captions", callback_data="helps_customcaption"),
        ],
        [
            create_btn("💬 PM Auto Reply", callback_data="helps_pmautoreply"),
            create_btn("🧹 Clean Filename", callback_data="helps_cleanfile"),
        ],
        [create_btn("💾 Backup", callback_data="helps_backup")],
    ]
    custom_rows = await _get_custom_rows("help")
    if custom_rows:
        rows.extend(custom_rows)
    rows.append(
        [
            create_btn("🔙 Back", callback_data="start"),
            create_btn("🔐 Close", callback_data="close_data"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def get_about_keyboard() -> InlineKeyboardMarkup:
    rows = []
    custom_rows = await _get_custom_rows("about")
    if custom_rows:
        rows.extend(custom_rows)
    rows.extend(
        [
            [create_btn("📊 Sᴛᴀᴛᴜs", callback_data="stats")],
            [
                create_btn("🏘 Hᴏᴍᴇ", callback_data="start"),
                create_btn("🔐 Cʟᴏsᴇ", callback_data="close_data"),
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def get_stats_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                create_btn("🔙 Back", callback_data="about"),
                create_btn("🔄 Refresh", callback_data="rfrsh"),
            ]
        ]
    )


def get_cleanwords_keyboard(use_default: bool = True) -> InlineKeyboardMarkup:
    mode_label = "defaultcleanfilename ✅" if use_default else "defaultcleanfilename"
    return InlineKeyboardMarkup(
        [
            [
                create_btn("➕ Add Words", callback_data="hact#cw_add"),
                create_btn("setcleanfilename", callback_data="hact#cw_set"),
            ],
            [
                create_btn(mode_label, callback_data="hact#cw_default"),
                create_btn("remcleanfilename", callback_data="hact#cw_clear"),
            ],
            [
                create_btn("🔙 Back", callback_data="help"),
                create_btn("🔐 Close", callback_data="close_data"),
            ],
        ]
    )


def get_help_section_keyboard(
    section_key: str, state: Optional[Dict[str, Any]] = None
) -> InlineKeyboardMarkup:
    st = state or {}
    rows: List[List[InlineKeyboardButton]] = []

    if section_key == "helps_uistart":
        rows.extend(
            [
                [
                    create_btn("setstarttext", callback_data="hact#cmd#setstarttext"),
                    create_btn(
                        "defaultstarttext", callback_data="hact#cmd#defaultstarttext"
                    ),
                    create_btn("setstartimage", callback_data="hact#cmd#setstartimage"),
                ],
                [
                    create_btn("remstartimage", callback_data="hact#cmd#remstartimage"),
                    create_btn(
                        "addstartbutton", callback_data="hact#cmd#addstartbutton"
                    ),
                ],
                [
                    create_btn(
                        "editstartbuttons", callback_data="hact#cmd#editstartbuttons"
                    ),
                    create_btn(
                        "remstartbutton", callback_data="hact#cmd#remstartbutton"
                    ),
                ],
            ]
        )
    elif section_key == "helps_uihelp":
        rows.extend(
            [
                [
                    create_btn("sethelptext", callback_data="hact#cmd#sethelptext"),
                    create_btn(
                        "defaulthelptext", callback_data="hact#cmd#defaulthelptext"
                    ),
                    create_btn("sethelpimage", callback_data="hact#cmd#sethelpimage"),
                ],
                [
                    create_btn("remhelpimage", callback_data="hact#cmd#remhelpimage"),
                    create_btn("addhelpbutton", callback_data="hact#cmd#addhelpbutton"),
                ],
                [
                    create_btn(
                        "edithelpbuttons", callback_data="hact#cmd#edithelpbuttons"
                    ),
                    create_btn("remhelpbutton", callback_data="hact#cmd#remhelpbutton"),
                ],
            ]
        )
    elif section_key == "helps_uiabout":
        rows.extend(
            [
                [
                    create_btn("setabouttext", callback_data="hact#cmd#setabouttext"),
                    create_btn(
                        "defaultabouttext", callback_data="hact#cmd#defaultabouttext"
                    ),
                    create_btn("setaboutimage", callback_data="hact#cmd#setaboutimage"),
                ],
                [
                    create_btn("remaboutimage", callback_data="hact#cmd#remaboutimage"),
                    create_btn(
                        "addaboutbutton", callback_data="hact#cmd#addaboutbutton"
                    ),
                ],
                [
                    create_btn(
                        "editaboutbuttons", callback_data="hact#cmd#editaboutbuttons"
                    ),
                    create_btn(
                        "remaboutbutton", callback_data="hact#cmd#remaboutbutton"
                    ),
                ],
            ]
        )
    elif section_key == "helps_welcome":
        welc_on = bool(st.get("welc_on", True))
        rows.extend(
            [
                [
                    create_btn(
                        f"On/Off: {'ON ✅' if welc_on else 'OFF ❌'}",
                        callback_data="hact#welc_toggle",
                    )
                ],
                [
                    create_btn("Set text", callback_data="hact#welc_settxt"),
                    create_btn("set image", callback_data="hact#cmd#setwelcomeimg"),
                ],
                [
                    create_btn("Set welcome", callback_data="hact#cmd#setwelcome"),
                    create_btn("del welcome", callback_data="hact#welc_del"),
                ],
            ]
        )
    elif section_key == "helps_images":
        rows.extend(
            [
                [
                    create_btn("setautoimg", callback_data="hact#cmd#setautoimg"),
                    create_btn("remautoimg", callback_data="hact#cmd#remautoimg"),
                ],
                [
                    create_btn(
                        "setfilenotfoundimg",
                        callback_data="hact#cmd#setfilenotfoundimg",
                    ),
                    create_btn(
                        "remfilenotfoundimg",
                        callback_data="hact#cmd#remfilenotfoundimg",
                    ),
                ],
                [
                    create_btn(
                        "defaultfilenotfoundimg",
                        callback_data="hact#cmd#defaultfilenotfoundimg",
                    ),
                    create_btn("setfsubimg", callback_data="hact#cmd#setfsubimg"),
                ],
            ]
        )
    elif section_key == "helps_spell":
        at_on = bool(st.get("at_on", True))
        mc_on = bool(st.get("mc_on", True))
        spell_on = bool(st.get("spell_on", True))
        stops_count = int(st.get("stops_count", 0))
        rows.extend(
            [
                [
                    create_btn(
                        f"autotranslate: {'ON ✅' if at_on else 'OFF ❌'}",
                        callback_data="hact#at_toggle",
                    ),
                    create_btn(
                        f"multichoice: {'ON ✅' if mc_on else 'OFF ❌'}",
                        callback_data="hact#mc_toggle",
                    ),
                ],
                [
                    create_btn(
                        f"spellcheck: {'ON ✅' if spell_on else 'OFF ❌'}",
                        callback_data="hact#spell_toggle",
                    ),
                    create_btn(
                        f"stopwords ({stops_count})", callback_data="hact#sw_list"
                    ),
                ],
                [
                    create_btn("addstopwords", callback_data="hact#sw_add"),
                    create_btn("remstopwords", callback_data="hact#cmd#remstopwords"),
                ],
                [
                    create_btn("defaultstopwords", callback_data="hact#sw_def"),
                    create_btn("remallstopwords", callback_data="hact#sw_clear"),
                ],
                [
                    create_btn(
                        "setnotfoundtext", callback_data="hact#cmd#setnotfoundtext"
                    ),
                    create_btn(
                        "defaultnotfoundtext",
                        callback_data="hact#cmd#defaultnotfoundtext",
                    ),
                ],
            ]
        )
    elif section_key == "helps_filters":
        rows.extend(
            [
                [
                    create_btn("filter", callback_data="hact#cmd#filter"),
                    create_btn("addfilter", callback_data="hact#cmd#addfilter"),
                ],
                [
                    create_btn("filterimage", callback_data="hact#cmd#filterimage"),
                    create_btn(
                        "editfiltercolur", callback_data="hact#cmd#editfiltercolur"
                    ),
                ],
                [
                    create_btn("delfilter", callback_data="hact#cmd#delfilter"),
                    create_btn("listfilters", callback_data="hact#flt_list"),
                ],
            ]
        )
    elif section_key == "helps_forcesub":
        f_on = bool(st.get("f_on", True))
        rot_on = bool(st.get("rot_on", False))
        rows.extend(
            [
                [
                    create_btn(
                        f"On/Off: {'ON ✅' if f_on else 'OFF ❌'}",
                        callback_data="hact#fsub_toggle",
                    ),
                    create_btn(
                        f"auto rotate: {'ON ✅' if rot_on else 'OFF ❌'}",
                        callback_data="hact#fsub_rot",
                    ),
                ],
                [
                    create_btn("Set fsub", callback_data="hact#cmd#setfsub"),
                    create_btn("rm fsub", callback_data="hact#cmd#rmfsub"),
                ],
                [
                    create_btn("Getallfsub", callback_data="hact#fsub_list"),
                    create_btn("totalrequests", callback_data="hact#cmd#totalrequests"),
                ],
                [create_btn("purgerequests", callback_data="hact#cmd#purgerequests")],
            ]
        )
    elif section_key == "helps_forceadd":
        fa_on = bool(st.get("fa_on", False))
        rows.extend(
            [
                [
                    create_btn(
                        f"On/Off: {'ON ✅' if fa_on else 'OFF ❌'}",
                        callback_data="hact#fa_toggle",
                    ),
                    create_btn("myadds", callback_data="hact#cmd#myadds"),
                ],
                [
                    create_btn("Set forceadd", callback_data="hact#cmd#setforceadd"),
                    create_btn("remforceadd", callback_data="hact#cmd#remforceadd"),
                ],
                [
                    create_btn("getforceadd", callback_data="hact#cmd#getforceadd"),
                    create_btn("topaddall", callback_data="hact#cmd#topaddall"),
                ],
                [
                    create_btn("topadd24", callback_data="hact#cmd#topadd24"),
                    create_btn("topadd7", callback_data="hact#cmd#topadd7"),
                ],
                [
                    create_btn("resetadddaily", callback_data="hact#cmd#resetadddaily"),
                    create_btn("resetadd", callback_data="hact#cmd#resetadd"),
                ],
            ]
        )
    elif section_key == "helps_bans":
        rows.extend(
            [
                [
                    create_btn("ban", callback_data="hact#cmd#ban"),
                    create_btn("unban", callback_data="hact#cmd#unban"),
                ],
                [
                    create_btn("Banned users", callback_data="hact#cmd#bannedusers"),
                    create_btn("leave", callback_data="hact#cmd#leave"),
                ],
                [
                    create_btn("Enable", callback_data="hact#cmd#enable"),
                    create_btn("disable", callback_data="hact#cmd#disable"),
                ],
            ]
        )
    elif section_key == "helps_delete":
        rows.extend(
            [
                [
                    create_btn("Delete", callback_data="hact#cmd#delete"),
                    create_btn("del multi", callback_data="hact#cmd#delmulti"),
                ],
                [
                    create_btn("Autodelete", callback_data="hact#cmd#autodelete"),
                    create_btn("buttondel", callback_data="hact#cmd#buttondel"),
                    create_btn(
                        "purgeduplicates", callback_data="hact#cmd#purgeduplicates"
                    ),
                ],
            ]
        )
    elif section_key == "helps_promotions":
        rows.extend(
            [
                [
                    create_btn("addpromo", callback_data="hact#cmd#addpromo"),
                    create_btn("delpromo", callback_data="hact#cmd#delpromo"),
                ],
                [
                    create_btn("broadcast", callback_data="hact#cmd#broadcast"),
                    create_btn(
                        "group_broadcast", callback_data="hact#cmd#group_broadcast"
                    ),
                ],
                [
                    create_btn("listpromos", callback_data="hact#cmd#listpromos"),
                    create_btn("listbroadcast", callback_data="hact#cmd#listbroadcast"),
                ],
            ]
        )
    elif section_key == "helps_index":
        rows.extend(
            [
                [
                    create_btn("index", callback_data="hact#cmd#index"),
                    create_btn("indexdoc", callback_data="hact#cmd#indexdoc"),
                    create_btn("indexvideo", callback_data="hact#cmd#indexvideo"),
                ],
                [
                    create_btn("indexaudio", callback_data="hact#cmd#indexaudio"),
                    create_btn("resumeindex", callback_data="hact#cmd#resumeindex"),
                ],
                [
                    create_btn("setskip", callback_data="hact#cmd#setskip"),
                    create_btn("deleteskip", callback_data="hact#cmd#deleteskip"),
                    create_btn("currentskip", callback_data="hact#cmd#currentskip"),
                ],
            ]
        )
    elif section_key == "helps_settings":
        rep_on = bool(st.get("rep_on", False))
        react_on = bool(st.get("react_on", True))
        rows.extend(
            [
                [
                    create_btn(
                        f"repairmode: {'ON ✅' if rep_on else 'OFF ❌'}",
                        callback_data="hact#repair_toggle",
                    ),
                    create_btn("adminsettings", callback_data="hact#cmd#adminsettings"),
                ],
                [
                    create_btn(
                        f"enablereaction {'✅' if react_on else ''}".strip(),
                        callback_data="hact#cmd#enablereaction",
                    ),
                    create_btn(
                        f"disablereaction {'✅' if not react_on else ''}".strip(),
                        callback_data="hact#cmd#disablereaction",
                    ),
                ],
            ]
        )
    elif section_key == "helps_connections":
        rows.extend(
            [
                [
                    create_btn("Connect", callback_data="hact#cmd#connect"),
                    create_btn("disconnect", callback_data="hact#cmd#disconnect"),
                ],
                [
                    create_btn("Connections", callback_data="hact#cmd#connections"),
                    create_btn("exportusers", callback_data="hact#cmd#exportusers"),
                ],
                [
                    create_btn("exportgroups", callback_data="hact#cmd#exportgroups"),
                    create_btn(
                        "exportchannels", callback_data="hact#cmd#exportchannels"
                    ),
                ],
                [
                    create_btn("channels", callback_data="hact#cmd#channels"),
                    create_btn("leavechannel", callback_data="hact#cmd#leavechannel"),
                ],
            ]
        )
    elif section_key == "helps_utilities":
        rows.extend(
            [
                [
                    create_btn("Logs", callback_data="hact#cmd#logs"),
                    create_btn("server", callback_data="hact#cmd#server"),
                    create_btn("stats", callback_data="hact#cmd#stats"),
                ],
                [
                    create_btn("Optimize_db", callback_data="hact#cmd#optimize_db"),
                    create_btn("total", callback_data="hact#cmd#total"),
                    create_btn(
                        "clearfsubusers", callback_data="hact#cmd#clearfsubusers"
                    ),
                ],
                [
                    create_btn("clearfiles", callback_data="hact#cmd#clearfiles"),
                    create_btn("clearusers", callback_data="hact#cmd#clearusers"),
                    create_btn("cleanusers", callback_data="hact#cmd#cleanusers"),
                ],
            ]
        )
    elif section_key == "helps_custommessages":
        rows.extend(
            [
                [
                    create_btn("infomsg", callback_data="hact#cmd#infomsg"),
                    create_btn("infoimg", callback_data="hact#cmd#infoimg"),
                    create_btn("fsubmsg", callback_data="hact#cmd#fsubmsg"),
                ],
                [
                    create_btn("delmsg", callback_data="hact#cmd#delmsg"),
                    create_btn("delimg", callback_data="hact#cmd#delimg"),
                    create_btn("fsubimg", callback_data="hact#cmd#fsubimg"),
                ],
                [
                    create_btn("notfoundmsg", callback_data="hact#cmd#notfoundmsg"),
                    create_btn("notfoundimg", callback_data="hact#cmd#notfoundimg"),
                ],
            ]
        )
    elif section_key == "helps_customcaption":
        rows.extend(
            [
                [
                    create_btn("customcaption", callback_data="hact#cmd#customcaption"),
                    create_btn("captionplus", callback_data="hact#cmd#captionplus"),
                ],
            ]
        )
    elif section_key == "helps_backup":
        rows.extend(
            [
                [
                    create_btn("dbbackup", callback_data="hact#cmd#dbbackup"),
                    create_btn("dbrestore", callback_data="hact#cmd#dbrestore"),
                ],
                [
                    create_btn("dbstats", callback_data="hact#cmd#dbstats"),
                    create_btn("dbschedule", callback_data="hact#cmd#dbschedule"),
                ],
            ]
        )
    elif section_key == "helps_posthand":
        rows.extend(
            [
                [
                    create_btn("post", callback_data="hact#cmd#post"),
                    create_btn("editpost", callback_data="hact#cmd#editpost"),
                ],
                [
                    create_btn("edittitle", callback_data="hact#cmd#edittitle"),
                    create_btn("edityear", callback_data="hact#cmd#edityear"),
                ],
                [
                    create_btn("editlangs", callback_data="hact#cmd#editlangs"),
                    create_btn(
                        "editresolutions", callback_data="hact#cmd#editresolutions"
                    ),
                ],
                [
                    create_btn("editgenres", callback_data="hact#cmd#editgenres"),
                    create_btn("editotts", callback_data="hact#cmd#editotts"),
                ],
                [
                    create_btn("editbutton", callback_data="hact#cmd#editbutton"),
                    create_btn(
                        "editbuttoncolour", callback_data="hact#cmd#editbuttoncolour"
                    ),
                ],
                [
                    create_btn("editdirect", callback_data="hact#cmd#editdirect"),
                    create_btn(
                        "setpoststicker", callback_data="hact#cmd#setpoststicker"
                    ),
                    create_btn(
                        "rempoststicker", callback_data="hact#cmd#rempoststicker"
                    ),
                ],
            ]
        )
    elif section_key == "helps_pmautoreply":
        pm_enabled = bool(st.get("pm_enabled", True))
        reply_admins = bool(st.get("reply_admins", True))
        rows.extend(
            [
                [
                    create_btn(
                        f"On/Off: {'ON ✅' if pm_enabled else 'OFF ❌'}",
                        callback_data="hact#pm_toggle",
                    ),
                    create_btn(
                        f"Reply Admins: {'ON ✅' if reply_admins else 'OFF ❌'}",
                        callback_data="hact#pm_adm_toggle",
                    ),
                ],
                [
                    create_btn("setpmtext", callback_data="hact#pm_settext"),
                    create_btn("rempmtext", callback_data="hact#pm_deftext"),
                ],
                [
                    create_btn("setpmimage", callback_data="hact#cmd#setpmimage"),
                    create_btn("rempmimage", callback_data="hact#pm_remimg"),
                ],
                [
                    create_btn("setpmbutton", callback_data="hact#pm_setbtn"),
                    create_btn("rempmbutton", callback_data="hact#pm_rembtn"),
                ],
            ]
        )

    rows.append(
        [
            create_btn("🔙 Back", callback_data="help"),
            create_btn("🔐 Close", callback_data="close_data"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def get_channels_page_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    nav_row: List[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(
            create_btn("⬅️ Previous", callback_data=f"channels_page#{page - 1}")
        )
    if page < total_pages:
        nav_row.append(create_btn("Next ➡️", callback_data=f"channels_page#{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([create_btn("🔐 Close", callback_data="close_data")])
    return InlineKeyboardMarkup(buttons)


# ============================================================
# 🎛️ HELP UI SECTION BUILDERS & CHANNELS PAGE BUILDER
# ============================================================
async def get_channels_page(
    client: Client, page: int = 1
) -> Tuple[str, InlineKeyboardMarkup]:
    raw_chats = await _db.get_all_chats()
    all_chats = (
        await raw_chats.to_list(length=None)
        if hasattr(raw_chats, "to_list")
        else list(raw_chats)
    )

    total_chats, page_size = len(all_chats), 15
    total_pages = max(1, math.ceil(total_chats / page_size))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * page_size
    text = f"📑 <b>All Connected Channels & Groups</b> (Page {page}/{total_pages})\n\n"

    if not all_chats:
        text += "<i>No connected chats found in database.</i>\n"

    for i, chat in enumerate(
        all_chats[start_idx : start_idx + page_size], start=start_idx + 1
    ):
        chat_id = chat.get("id") or chat.get("chat_id")
        title = chat.get("title") or chat.get("name") or "Unknown"
        username = chat.get("username")
        link = f"https://t.me/{username}" if username else "Private / No Link"
        if not username and chat_id:
            try:
                chat_obj = await client.get_chat(chat_id)
                link = (
                    f"https://t.me/{chat_obj.username}"
                    if chat_obj.username
                    else chat_obj.invite_link
                    or await client.export_chat_invite_link(chat_id)
                )
            except Exception:
                clean_id = (
                    str(chat_id)[4:]
                    if str(chat_id).startswith("-100")
                    else str(chat_id)
                )
                link = f"https://t.me/c/{clean_id}/1"
        text += (
            f"<b>{i}. {title}</b>\n🔗 Link: {link}\n🆔 ID: <code>{chat_id}</code>\n\n"
        )

    return text, get_channels_page_keyboard(page, total_pages)


async def build_status_text() -> str:
    total_files = 0
    users = 0
    chats = 0
    monsize = "0 B"
    free = "512 MB"

    try:
        if Media is not None:
            col = getattr(Media, "collection", Media)
            total_files = await col.count_documents({})
    except Exception:
        pass

    try:
        users = await _db.total_users_count()
        chats = await _db.total_chat_count()
        raw_size = await _db.get_db_size()
        monsize = get_size(raw_size)
        free = get_size(536870912 - raw_size)
    except Exception:
        pass

    status_template = getattr(script, "STATUS_TXT", None)
    if status_template:
        try:
            return status_template.format(total_files, users, chats, monsize, free)
        except Exception:
            pass

    return (
        f"📊 <b>Bot Database Status</b>\n\n"
        f"📁 <b>Total Files:</b> <code>{total_files}</code>\n"
        f"👤 <b>Total Users:</b> <code>{users}</code>\n"
        f"👥 <b>Total Chats:</b> <code>{chats}</code>\n"
        f"💾 <b>Used Storage:</b> <code>{monsize}</code>\n"
        f"🆓 <b>Free Storage:</b> <code>{free}</code>"
    )


async def build_cleanwords_panel_ui() -> Tuple[str, InlineKeyboardMarkup]:
    use_default, words = await get_clean_words_state()
    mode_str = "🟢 Default Clean Words" if use_default else "🔵 Custom Clean Words"
    words_display = (
        ", ".join(f"<code>{html.escape(str(w))}</code>" for w in words)
        if words
        else "<i>None (Only Stylish Fonts, @usernames, links & symbols are stripped)</i>"
    )
    tpl = getattr(script, "CLEANFILE_TXT", DEFAULT_CLEANFILE_TXT)
    try:
        text = tpl.format(
            mode_str=mode_str,
            count=len(words),
            words_display=words_display,
        )
    except Exception:
        text = DEFAULT_CLEANFILE_TXT.format(
            mode_str=mode_str,
            count=len(words),
            words_display=words_display,
        )
    if "@usernames" not in text:
        text = text.replace(
            f"⚙️ <b>Current Mode:</b> {mode_str}\n",
            f"⚙️ <b>Current Mode:</b> {mode_str}\n"
            f"🛡️ <b>Always Auto-Removed:</b> <code>Stylish Fonts</code>, <code>@usernames</code>, <code>links</code> & <code>symbols</code>\n",
        )
    return text, get_cleanwords_keyboard(use_default=use_default)


async def build_interactive_help_section(
    client: Client,
    section_key: str,
    user_id: int,
    user_mention: str,
    msg_context: Optional[Message] = None,
) -> Tuple[str, InlineKeyboardMarkup]:
    if section_key == "helps_cleanfile":
        return await build_cleanwords_panel_ui()

    ui_doc = await _ensure_ui_synced()
    bot_uname = getattr(temp, "U_NAME", None) or "my_bot"
    b_name = getattr(temp, "B_NAME", None) or "MovieBot"

    help_dict = {
        "helps_uistart": ("UISTART_TXT", "🎨 <b>UI Start Menu</b>"),
        "helps_uihelp": ("UIHELP_TXT", "🎨 <b>UI Help Menu</b>"),
        "helps_uiabout": ("UIABOUT_TXT", "🎨 <b>UI About Menu</b>"),
        "helps_welcome": ("WELCOME_TXT", "👋 <b>Welcome Help</b>"),
        "helps_images": ("IMAGES_TXT", "🖼️ <b>Images Help</b>"),
        "helps_spell": ("SPELLCHECK_TXT", "🔍 <b>Spell Check & Smart Search Help</b>"),
        "helps_filters": ("FILTERS_TXT", "📝 <b>Filters Help</b>"),
        "helps_forcesub": ("FORCESUB_TXT", "📱 <b>Force Sub Help</b>"),
        "helps_forceadd": ("FORCEADD_TXT", "👥 <b>Force Add Help</b>"),
        "helps_bans": ("BANS_TXT", "🚫 <b>Bans Help</b>"),
        "helps_delete": ("DELETE_TXT", "🗑️ <b>Delete Help</b>"),
        "helps_promotions": ("PROMOTIONS_TXT", "📢 <b>Promotions Help</b>"),
        "helps_index": ("INDEX_TXT", "📚 <b>Index Help</b>"),
        "helps_settings": ("SETTINGS_TXT", "⚙️ <b>Settings Help</b>"),
        "helps_connections": ("CONNECTIONS_TXT", "🌐 <b>Connections Help</b>"),
        "helps_utilities": ("UTILITIES_TXT", "📊 <b>Utilities Help</b>"),
        "helps_custommessages": ("CUSTOMMESSAGES_TXT", "💬 <b>Custom Messages</b>"),
        "helps_posthand": ("POSTHAND_TXT", "📝 <b>Post Handle Help</b>"),
        "helps_customcaption": ("CUSTOMCAPTION_TXT", "📝 <b>Custom Captions Help</b>"),
        "helps_backup": ("BACKUP_TXT", "💾 <b>Backup Help</b>"),
        "helps_pmautoreply": ("PMAUTOREPLY_TXT", "💬 <b>PM Auto Reply Help</b>"),
    }

    target_var, default_text = help_dict.get(
        section_key, ("HELP_TXT", "Help unavailable.")
    )
    raw_section = getattr(script, target_var, default_text)
    base_text = format_script_text(
        raw_section, mention=user_mention, uname=bot_uname, bname=b_name
    )

    state: Dict[str, Any] = {}

    if section_key in ["helps_uistart", "helps_uihelp", "helps_uiabout"]:
        menu_prefix = section_key.replace("helps_ui", "")
        has_txt = bool(ui_doc.get(f"{menu_prefix}_text"))
        has_img = bool(ui_doc.get(f"{menu_prefix}_img"))
        btns_cnt = len(ui_doc.get(f"{menu_prefix}_buttons", []) or [])
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live {menu_prefix.title()} UI Status:</b>\n"
            f"• <b>Custom Text:</b> <code>{'Active ✅' if has_txt else 'Default'}</code>\n"
            f"• <b>Custom Image:</b> <code>{'Active ✅' if has_img else 'Default'}</code>\n"
            f"• <b>Custom Buttons:</b> <code>{btns_cnt}</code>"
        )

    elif section_key == "helps_spell":
        try:
            from plugins.pm_filter import get_feature_flags

            at_on, mc_on = await get_feature_flags()
        except Exception:
            at_on, mc_on = True, True

        try:
            from plugins.custom_settings import get_stopwords

            stops = get_stopwords()
        except Exception:
            stops = []

        conn_gid = await get_active_connected_group(user_id, msg_context)
        spell_on = True
        if conn_gid and callable(get_settings):
            try:
                g_set = await get_settings(conn_gid)
                spell_on = bool(g_set.get("spell_check", True))
            except Exception:
                pass

        state = {
            "at_on": at_on,
            "mc_on": mc_on,
            "spell_on": spell_on,
            "stops_count": len(stops),
        }
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live Smart Search Status:</b>\n"
            f"• <b>Auto-Translate:</b> <code>{'ON ✅' if at_on else 'OFF ❌'}</code>\n"
            f"• <b>Multi-Choice:</b> <code>{'ON ✅' if mc_on else 'OFF ❌'}</code>\n"
            f"• <b>Group Spell Check:</b> <code>{'ON ✅' if spell_on else 'OFF ❌'}</code> "
            f"(Group: <code>{conn_gid or 'Not Connected'}</code>)\n"
            f"• <b>Active Stopwords:</b> <code>{len(stops)}</code>"
        )

    elif section_key == "helps_pmautoreply":
        pm_cfg = await get_pm_settings()
        pm_enabled = bool(pm_cfg.get("enabled", True))
        reply_admins = bool(pm_cfg.get("reply_admins", True))
        btn_txt = pm_cfg.get("button_text") or "None"
        btn_color = str(pm_cfg.get("button_color", "red")).upper()

        state = {
            "pm_enabled": pm_enabled,
            "reply_admins": reply_admins,
        }
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live PM Auto-Reply Status:</b>\n"
            f"• <b>PM Auto-Reply:</b> <code>{'ON ✅' if pm_enabled else 'OFF ❌'}</code>\n"
            f"• <b>Reply to Admins:</b> <code>{'ON ✅' if reply_admins else 'OFF ❌'}</code>\n"
            f"• <b>Active Button:</b> <code>{html.escape(str(btn_txt))}</code> (<code>{btn_color}</code>)"
        )

    elif section_key == "helps_forcesub":
        try:
            from plugins.fsub import get_fsub_config, get_total_join_requests

            f_cfg = await get_fsub_config()
            f_on = bool(f_cfg.get("is_enabled", True))
            rot_on = bool(f_cfg.get("rotation_enabled", False))
            ch_count = len(f_cfg.get("channels", {}))
            req_count = await get_total_join_requests()
        except Exception:
            f_on, rot_on, ch_count, req_count = True, False, 0, 0

        state = {
            "f_on": f_on,
            "rot_on": rot_on,
            "ch_count": ch_count,
        }
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live ForceSub Status:</b>\n"
            f"• <b>ForceSub:</b> <code>{'ON ✅' if f_on else 'OFF ❌'}</code>\n"
            f"• <b>Auto-Rotation:</b> <code>{'ON ✅' if rot_on else 'OFF ❌'}</code>\n"
            f"• <b>Channels:</b> <code>{ch_count}</code> | <b>Join Requests:</b> <code>{req_count}</code>"
        )

    elif section_key == "helps_forceadd":
        conn_gid = await get_active_connected_group(user_id, msg_context)
        fa_on = False
        if conn_gid:
            try:
                from plugins.force_add import get_fa_config

                fa_cfg = await get_fa_config(conn_gid, force_refresh=True)
                fa_on = bool(fa_cfg.get("enabled", False))
            except Exception:
                if _BOT_DB is not None:
                    try:
                        fa_doc = await _BOT_DB["force_add_settings"].find_one(
                            {"chat_id": int(conn_gid)}
                        )
                        if fa_doc:
                            fa_on = bool(
                                fa_doc.get("enabled", fa_doc.get("is_enabled", False))
                            )
                    except Exception:
                        pass
        state = {"fa_on": fa_on}
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live Force Add Status:</b>\n"
            f"• <b>Connected Group:</b> <code>{conn_gid or 'None (/connect first)'}</code>\n"
            f"• <b>Force Add:</b> <code>{'ON ✅' if fa_on else 'OFF ❌'}</code>"
        )

    elif section_key == "helps_welcome":
        conn_gid = await get_active_connected_group(user_id, msg_context)
        welc_on = True
        if conn_gid:
            try:
                from plugins.custom_settings import get_group_welcome

                w_doc = await get_group_welcome(conn_gid)
                welc_on = bool(w_doc.get("welcome", True))
            except Exception:
                pass

        state = {"welc_on": welc_on}
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live Welcome Status:</b>\n"
            f"• <b>Connected Group:</b> <code>{conn_gid or 'None (/connect first)'}</code>\n"
            f"• <b>Welcome Message:</b> <code>{'ON ✅' if welc_on else 'OFF ❌'}</code>"
        )

    elif section_key == "helps_filters":
        conn_gid = await get_active_connected_group(user_id, msg_context)
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <b>Connected Group:</b> <code>{conn_gid or 'None (Use /connect <group_id>)'}</code>"
        )

    elif section_key == "helps_settings":
        rep_on = bool(getattr(info, "REPAIR_MODE", False))
        react_on = bool(getattr(info, "ENABLE_REACTION", True))
        state = {"rep_on": rep_on, "react_on": react_on}
        base_text += (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎛️ <b>Live System Status:</b>\n"
            f"• <b>Repair / Maintenance Mode:</b> <code>{'ON ✅' if rep_on else 'OFF ❌'}</code>\n"
            f"• <b>Auto Reactions:</b> <code>{'ON ✅' if react_on else 'OFF ❌'}</code>"
        )

    return base_text, get_help_section_keyboard(section_key, state)


async def transition_ui_message(
    client: Client, msg_obj: Message, text: str, keyboard, img=None
):
    has_media = bool(
        msg_obj.photo or msg_obj.video or msg_obj.document or msg_obj.animation
    )
    can_use_caption = len(text or "") <= 1024

    try:
        if img and can_use_caption:
            if has_media:
                await msg_obj.edit_caption(
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
                return
            else:
                try:
                    await client.send_photo(
                        chat_id=msg_obj.chat.id,
                        photo=img,
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    await msg_obj.delete()
                    return
                except Exception:
                    pass

        if has_media:
            if can_use_caption and img is None:
                try:
                    await msg_obj.edit_caption(
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    return
                except MediaCaptionTooLong:
                    pass
            await safe_send_message(
                client,
                chat_id=msg_obj.chat.id,
                text=text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            try:
                await msg_obj.delete()
            except Exception:
                pass
        else:
            await safe_edit_text(
                msg_obj,
                text=text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except (MessageNotModified, MessageIdInvalid):
        pass
    except MediaCaptionTooLong:
        try:
            await safe_send_message(
                client,
                chat_id=msg_obj.chat.id,
                text=text,
                reply_markup=keyboard,
                parse_mode=enums.ParseMode.HTML,
                disable_web_page_preview=True,
            )
            await msg_obj.delete()
        except Exception as e:
            logger.error(f"Caption Fallback Error: {e}")
    except Exception as e:
        logger.error(f"UI Transition Error: {e}")


# ============================================================
# ⚡ DIRECT HELP SECTION SHORTCUT COMMANDS (/uistart, /welcome, etc.)
# ============================================================
@Client.on_message(
    filters.command(list(HELP_SHORTCUT_MAP.keys())) & filters.incoming,
    group=-2,
)
async def help_category_shortcut_cmds(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    cancel_cmd_user_input(message.chat.id)

    if not await is_global_admin(uid):
        return await safe_reply_text(
            message,
            "❌ <b>Unauthorized:</b> Help menus and controls are restricted to Bot Admins only.",
            parse_mode=enums.ParseMode.HTML,
        )

    cmd_name = message.command[0].lower().strip()
    section_key = HELP_SHORTCUT_MAP.get(cmd_name, "helps_uistart")
    user_mention = (
        message.from_user.mention
        if message.from_user
        else (message.chat.title or "Admin")
    )

    text, markup = await build_interactive_help_section(
        client, section_key, uid, user_mention, message
    )
    await safe_reply_text(
        message,
        text=text,
        reply_markup=markup,
        parse_mode=enums.ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _register_user_in_bg(client: Client, user):
    """Registers a new PM user in the background so file delivery is never delayed."""
    if not user or user.id in _KNOWN_USERS_RAM:
        return
    _KNOWN_USERS_RAM.add(user.id)
    try:
        if not await _db.is_user_exist(user.id):
            await _db.add_user(user.id, user.first_name)
            if LOG_CHANNEL:
                await safe_send_message(
                    client,
                    LOG_CHANNEL,
                    script.LOG_TEXT_P.format(user.id, user.mention),
                )
    except Exception:
        pass


# ============================================================
# 🎯 MAIN START & HELP COMMANDS (INSTANT FILE DELIVERY)
# ============================================================
@Client.on_message(filters.command(["start", "help"]) & filters.incoming)
async def start_and_help_cmd(client: Client, message: Message):
    uid = (
        message.from_user.id
        if message.from_user
        else getattr(message.sender_chat, "id", 0)
    )
    cancel_cmd_user_input(message.chat.id)

    is_admin = await is_global_admin(uid)

    if getattr(info, "REPAIR_MODE", False) and not is_admin:
        return await safe_reply_text(
            message,
            "🛠️ <b>Bot is currently under maintenance!</b>",
            parse_mode=enums.ParseMode.HTML,
        )

    if message.from_user and await is_user_banned(uid):
        return

    # ⚡ FAST-PATH FOR FILE & SEARCH DEEP-LINKS (0ms Overhead):
    # Skip UI sync and run user registration in background so file sends immediately!
    if (
        message.chat.type == enums.ChatType.PRIVATE
        and len(message.command) == 2
        and message.command[1] not in ["help", "error", "okay", "hehe"]
    ):
        if message.from_user and uid not in _KNOWN_USERS_RAM:
            asyncio.create_task(_register_user_in_bg(client, message.from_user))

        cmd_data = message.command[1]
        from plugins.commands import run_force_sub, send_requested_file

        if cmd_data == "subscribe":
            return await run_force_sub(client, message)

        if cmd_data.startswith("search_"):
            query_str = re.sub(
                r"[_+\-]+", " ", cmd_data.replace("search_", "", 1)
            ).strip()
            try:
                from plugins.pm_filter import auto_filter

                message.text = query_str
                message.command = None
                await auto_filter(client, message)
            except Exception as e:
                logger.error(f"Search interceptor failed: {e}")
                await safe_reply_text(
                    message,
                    f"🔍 <b>Search Query:</b> <code>{query_str}</code>\n\nSend this text in our group to search!",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        kk, file_id = cmd_data.split("_", 1) if "_" in cmd_data else (False, False)
        pre = ("checksubp" if kk == "filep" else "checksub") if kk else False
        target_file_id = file_id or cmd_data

        status = await run_force_sub(client, message, file_id=target_file_id, mode=pre)
        if not status:
            return

        return await send_requested_file(
            client=client,
            user_id=uid,
            file_id=target_file_id,
            ident=kk or "file",
            reply_target=message,
        )

    await _ensure_ui_synced()

    if not getattr(temp, "U_NAME", None) or not getattr(temp, "B_NAME", None):
        try:
            me = await client.get_me()
            temp.U_NAME = me.username
            temp.B_NAME = me.first_name
            temp.ME = me.id
        except Exception:
            pass

    bot_uname = getattr(temp, "U_NAME", None) or "my_bot"
    b_name = getattr(temp, "B_NAME", None) or "MovieBot"
    user_mention = (
        message.from_user.mention
        if message.from_user
        else (message.chat.title or "User")
    )

    is_help_command = (message.command[0].lower() == "help") or (
        len(message.command) == 2 and message.command[1].lower() == "help"
    )

    if is_help_command:
        if not is_admin:
            return await safe_reply_text(
                message,
                "❌ <b>Unauthorized:</b> Help menu and controls are restricted to Bot Admins only.",
                parse_mode=enums.ParseMode.HTML,
            )

        raw_help = getattr(script, "HELP_TXT", "<b>Help Menu</b>")
        text = format_script_text(
            raw_help, mention=user_mention, uname=bot_uname, bname=b_name
        )
        final_text, final_keyboard = safe_parse_text_and_markup(
            text, await get_help_keyboard()
        )
        img = getattr(info, "HELP_IMG", None)
        if img and len(final_text) <= 1024:
            try:
                return await message.reply_photo(
                    photo=img,
                    caption=final_text,
                    reply_markup=final_keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass
        return await safe_reply_text(
            message,
            text=final_text,
            reply_markup=final_keyboard,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )

    # Group /start handling
    if message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
        raw_text = format_script_text(
            getattr(script, "START_TXT", "Hello {mention}!"),
            mention=user_mention,
            uname=bot_uname,
            bname=b_name,
        )
        final_text, final_keyboard = safe_parse_text_and_markup(
            raw_text, await get_start_keyboard(client, uid)
        )
        await safe_reply_text(
            message,
            final_text,
            reply_markup=final_keyboard,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if not await _db.get_chat(message.chat.id):
            try:
                total = await client.get_chat_members_count(message.chat.id)
                if LOG_CHANNEL:
                    await safe_send_message(
                        client,
                        LOG_CHANNEL,
                        script.LOG_TEXT_G.format(
                            message.chat.title, message.chat.id, total, "Unknown"
                        ),
                    )
            except Exception:
                pass
            await _db.add_chat(message.chat.id, message.chat.title)
        return

    # Register new private user in background
    if message.from_user and uid not in _KNOWN_USERS_RAM:
        asyncio.create_task(_register_user_in_bg(client, message.from_user))

    # Standard /start with no deep-link payload
    raw_text = format_script_text(
        getattr(script, "START_TXT", "Hello {mention}!"),
        mention=user_mention,
        uname=bot_uname,
        bname=b_name,
    )
    final_text, final_keyboard = safe_parse_text_and_markup(
        raw_text, await get_start_keyboard(client, uid)
    )
    db_img = getattr(info, "START_IMG", None)
    photo_to_send = db_img if db_img else (random.choice(PICS) if PICS else None)

    try:
        if photo_to_send and len(final_text) <= 1024:
            try:
                return await message.reply_photo(
                    photo=photo_to_send,
                    caption=final_text,
                    reply_markup=final_keyboard,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass
        await safe_reply_text(
            message,
            text=final_text,
            reply_markup=final_keyboard,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except (UserIsBlocked, PeerIdInvalid):
        pass


# ============================================================
# 🚀 UNIVERSAL COMMAND DISPATCHER FOR HELP MENU BUTTONS
# ============================================================
_CMD_INPUT_PROMPTS: Dict[str, Tuple[str, str]] = {
    "setstarttext": (
        "helps_uistart",
        "✏️ <b>Send the new Custom Start Text (HTML/Markdown supported):</b>",
    ),
    "setstartimage": (
        "helps_uistart",
        "🖼️ <b>Send the Photo you want to set as the Start Image:</b>",
    ),
    "addstartbutton": (
        "helps_uistart",
        '➕ <b>Send the Start Button in format:</b>\n<code>"Button Text" https://t.me/link</code>',
    ),
    "editstartbuttons": (
        "helps_uistart",
        "🎨 <b>Send Start Button layout & colour:</b>\nExample: <code>1 sidebyside green</code>",
    ),
    "sethelptext": (
        "helps_uihelp",
        "✏️ <b>Send the new Custom Help Text (HTML/Markdown supported):</b>",
    ),
    "sethelpimage": (
        "helps_uihelp",
        "🖼️ <b>Send the Photo you want to set as the Help Image:</b>",
    ),
    "addhelpbutton": (
        "helps_uihelp",
        '➕ <b>Send the Help Button in format:</b>\n<code>"Button Text" https://t.me/link</code>',
    ),
    "edithelpbuttons": (
        "helps_uihelp",
        "🎨 <b>Send Help Button layout & colour:</b>\nExample: <code>1 belowside red</code>",
    ),
    "setabouttext": (
        "helps_uiabout",
        "✏️ <b>Send the new Custom About Text (HTML/Markdown supported):</b>",
    ),
    "setaboutimage": (
        "helps_uiabout",
        "🖼️ <b>Send the Photo you want to set as the About Image:</b>",
    ),
    "addaboutbutton": (
        "helps_uiabout",
        '➕ <b>Send the About Button in format:</b>\n<code>"Button Text" https://t.me/link</code>',
    ),
    "editaboutbuttons": (
        "helps_uiabout",
        "🎨 <b>Send About Button layout & colour:</b>\nExample: <code>1 sidebyside blue</code>",
    ),
    "setwelcomeimg": (
        "helps_welcome",
        "🖼️ <b>Send the Photo you want to set as the Group Welcome Image:</b>",
    ),
    "setwelcome": (
        "helps_welcome",
        "👋 <b>Send a Photo with Caption to set both Welcome Image & Text:</b>",
    ),
    "setautoimg": (
        "helps_images",
        "🖼️ <b>Send the Photo to set as the Global Auto-Filter Image:</b>",
    ),
    "setfilenotfoundimg": (
        "helps_images",
        "🖼️ <b>Send the Photo to set as the File Not Found Image:</b>",
    ),
    "setfsubimg": (
        "helps_images",
        "🖼️ <b>Send the Photo to set as the Force Subscribe Alert Image:</b>",
    ),
    "remstopwords": (
        "helps_spell",
        "🛑 <b>Send the stopword(s) you want to remove:</b>",
    ),
    "setnotfoundtext": (
        "helps_spell",
        "✏️ <b>Send the new File Not Found text (HTML supported):</b>",
    ),
    "editfiltercolur": (
        "helps_filters",
        "🎨 <b>Send filter keyword, button number & colour:</b>\nExample: <code>kantara 1 green</code>",
    ),
    "delfilter": (
        "helps_filters",
        "🗑️ <b>Send the filter keyword you want to delete:</b>",
    ),
    "setfsub": (
        "helps_forcesub",
        "📢 <b>Send the Channel ID to add to Force Subscribe:</b>\nExample: <code>-1001234567890</code>",
    ),
    "rmfsub": (
        "helps_forcesub",
        "🗑️ <b>Send the Channel ID to remove from Force Subscribe:</b>\nExample: <code>-1001234567890</code>",
    ),
    "setforceadd": (
        "helps_forceadd",
        "👥 <b>Send the required member count for Force Add:</b>\nExample: <code>5</code>",
    ),
    "ban": (
        "helps_bans",
        "🚫 <b>Send the User ID (or username) to Ban from the bot:</b>",
    ),
    "unban": (
        "helps_bans",
        "✅ <b>Send the User ID (or username) to Unban:</b>",
    ),
    "leave": (
        "helps_bans",
        "🚪 <b>Send the Group/Channel Chat ID for the bot to leave:</b>",
    ),
    "enable": (
        "helps_bans",
        "🟢 <b>Send the Group Chat ID to Enable / Whitelist:</b>",
    ),
    "disable": (
        "helps_bans",
        "🔴 <b>Send the Group Chat ID to Disable / Blacklist:</b>",
    ),
    "delete": (
        "helps_delete",
        "🗑️ <b>Forward the file or send the exact filename to delete from DB:</b>",
    ),
    "delmulti": (
        "helps_delete",
        "🗑️ <b>Send the keyword to delete all matching files from DB:</b>",
    ),
    "autodelete": (
        "helps_delete",
        "⏳ <b>Send the PM File Auto-Delete timer in seconds (e.g. <code>1800</code>):</b>",
    ),
    "buttondel": (
        "helps_delete",
        "⏳ <b>Send the Group Search Results Auto-Delete timer in seconds (e.g. <code>300</code>):</b>",
    ),
    "addpromo": (
        "helps_promotions",
        '📢 <b>Send the Promo Button text & URL:</b>\nExample: <code>"Join Channel" https://t.me/channel</code>',
    ),
    "delpromo": (
        "helps_promotions",
        "🗑️ <b>Send the Promo URL (or button name) to delete:</b>",
    ),
    "broadcast": (
        "helps_promotions",
        "📡 <b>Send or forward the message you want to Broadcast to all Users:</b>",
    ),
    "group_broadcast": (
        "helps_promotions",
        "📡 <b>Send or forward the message you want to Broadcast to all Groups:</b>",
    ),
    "index": (
        "helps_index",
        "📚 <b>Forward the last message from your channel or send the Channel Post Link to Index All Media:</b>",
    ),
    "indexdoc": (
        "helps_index",
        "📄 <b>Forward the last message from your channel or send the Channel Post Link to Index Documents:</b>",
    ),
    "indexvideo": (
        "helps_index",
        "🎥 <b>Forward the last message from your channel or send the Channel Post Link to Index Videos:</b>",
    ),
    "indexaudio": (
        "helps_index",
        "🎵 <b>Forward the last message from your channel or send the Channel Post Link to Index Audio:</b>",
    ),
    "resumeindex": (
        "helps_index",
        "▶️ <b>Send the Channel Chat ID to Resume Indexing:</b>\nExample: <code>-1001234567890</code>",
    ),
    "setskip": (
        "helps_index",
        "⏩ <b>Send the starting message Skip Number (e.g. <code>100</code>):</b>",
    ),
    "connect": (
        "helps_connections",
        "🔗 <b>Send the Group Chat ID to Connect to your PM:</b>\nExample: <code>-1001234567890</code>",
    ),
    "disconnect": (
        "helps_connections",
        "🔌 <b>Send the Group Chat ID to Disconnect:</b>",
    ),
    "leavechannel": (
        "helps_connections",
        "🚪 <b>Send the Channel/Group ID to Leave and Remove from DB:</b>",
    ),
    "infomsg": (
        "helps_custommessages",
        "💬 <b>Send the Info Message text (or <code>off</code> to disable):</b>",
    ),
    "infoimg": (
        "helps_custommessages",
        "🖼️ <b>Send the Photo for Info Image (or <code>off</code> to remove):</b>",
    ),
    "fsubmsg": (
        "helps_custommessages",
        "💬 <b>Send the custom Force Subscribe Message text (or <code>off</code>):</b>",
    ),
    "fsubimg": (
        "helps_custommessages",
        "🖼️ <b>Send the Photo for Force Subscribe Image (or <code>off</code>):</b>",
    ),
    "delmsg": (
        "helps_custommessages",
        "💬 <b>Send the custom Auto-Delete Warning Message (or <code>off</code>):</b>",
    ),
    "delimg": (
        "helps_custommessages",
        "🖼️ <b>Send the Photo for Auto-Delete Warning Image (or <code>off</code>):</b>",
    ),
    "notfoundmsg": (
        "helps_custommessages",
        "💬 <b>Send the custom File Not Found Message (or <code>off</code>):</b>",
    ),
    "notfoundimg": (
        "helps_custommessages",
        "🖼️ <b>Send the Photo for File Not Found Image (or <code>off</code>):</b>",
    ),
    "customcaption": (
        "helps_customcaption",
        "📝 <b>Send the Custom File Caption template (or <code>off</code>):</b>\nVariables: <code>{file_name}</code>, <code>{file_size}</code>, <code>{file_caption}</code>",
    ),
    "captionplus": (
        "helps_customcaption",
        "📝 <b>Send the extra Footer Caption (Caption Plus) text (or <code>off</code>):</b>",
    ),
    "dbrestore": (
        "helps_backup",
        "💾 <b>Send the <code>.json</code> Backup File to Restore your Database:</b>",
    ),
    "post": (
        "helps_posthand",
        "🎬 <b>Send the Movie Name to create an interactive Post:</b>\nExample: <code>Kantara</code>",
    ),
    "editpost": (
        "helps_posthand",
        "✏️ <b>Send the Telegram Channel Post Link to import and edit:</b>",
    ),
    "edittitle": (
        "helps_posthand",
        "✏️ <b>Send the new Movie Title for your active post draft:</b>",
    ),
    "edityear": (
        "helps_posthand",
        "📅 <b>Send the new Release Year for your active post draft:</b>",
    ),
    "editlangs": (
        "helps_posthand",
        "🔊 <b>Send the Languages separated by commas:</b>\nExample: <code>Kannada, Hindi, English</code>",
    ),
    "editresolutions": (
        "helps_posthand",
        "🖥️ <b>Send the Resolutions separated by commas:</b>\nExample: <code>720p, 1080p, 4K</code>",
    ),
    "editgenres": (
        "helps_posthand",
        "🎭 <b>Send the Genres separated by commas:</b>\nExample: <code>Action, Thriller</code>",
    ),
    "editotts": (
        "helps_posthand",
        "📺 <b>Send the OTT Platforms separated by commas:</b>\nExample: <code>Netflix, AmazonPrime</code>",
    ),
    "editbutton": (
        "helps_posthand",
        "🔘 <b>Send Row Number, Button Text & URL:</b>\nExample: <code>1 Download Now | https://t.me/link</code>",
    ),
    "editbuttoncolour": (
        "helps_posthand",
        "🎨 <b>Send Row Number & Colour (green, red, blue):</b>\nExample: <code>1 green</code>",
    ),
    "editdirect": (
        "helps_posthand",
        "🔗 <b>Send new Direct Search button text (and optional link with <code>|</code>):</b>",
    ),
    "setpoststicker": (
        "helps_posthand",
        "🀄 <b>Send the Sticker you want to set after every Channel Post:</b>",
    ),
    "setpmimage": (
        "helps_pmautoreply",
        "🖼️ <b>Send the Photo you want to set as the PM Auto-Reply Image:</b>",
    ),
}


class _SyntheticText(str):
    """Preserves .html attribute when user sends formatted HTML/Markdown text."""

    def __new__(cls, value: str, html_val: Optional[str] = None):
        obj = super().__new__(cls, value)
        obj.html = html_val if html_val is not None else value
        obj.markdown = value
        return obj


async def _dispatch_synthetic_command(
    client: Client,
    base_msg: Message,
    user,
    cmd_name: str,
    arg_text: str = "",
    arg_html: Optional[str] = None,
    reply_msg: Optional[Message] = None,
) -> bool:
    clean_arg = arg_text.strip()
    full_text = f"/{cmd_name}" + (f" {clean_arg}" if clean_arg else "")
    full_html = f"/{cmd_name}" + (
        f" {arg_html.strip()}"
        if arg_html and arg_html.strip()
        else (f" {clean_arg}" if clean_arg else "")
    )
    cmd_parts = [cmd_name] + (clean_arg.split() if clean_arg else [])

    try:
        synth_msg = copy.copy(base_msg)
        synth_msg._client = client
        synth_msg.text = _SyntheticText(full_text, full_html)
        synth_msg.caption = None
        synth_msg.photo = None
        synth_msg.video = None
        synth_msg.document = None
        synth_msg.sticker = None
        synth_msg.animation = None
        synth_msg.audio = None
        synth_msg.command = cmd_parts
        synth_msg.from_user = user
        synth_msg.sender_chat = None
        synth_msg.outgoing = False
        if reply_msg is not None:
            synth_msg.reply_to_message = reply_msg
    except Exception as e:
        logger.error(f"Failed building synthetic message for /{cmd_name}: {e}")
        return False

    # 1. Direct Plugin Route Lookup (Fast, 100% Reliable, Never Silent)
    route = _DIRECT_CMD_ROUTES.get(cmd_name.lower())
    if route:
        mod_path, func_name = route
        for candidate_mod in (mod_path, mod_path.replace("plugins.", "", 1)):
            try:
                mod = importlib.import_module(candidate_mod)
                handler_fn = getattr(mod, func_name, None)
                if callable(handler_fn):
                    await handler_fn(client, synth_msg)
                    return True
            except (StopPropagation, ContinuePropagation):
                return True
            except ImportError:
                continue
            except Exception as e:
                logger.error(
                    f"Direct execution error in {mod_path}.{func_name} for /{cmd_name}: {e}"
                )
                await safe_reply_text(
                    base_msg,
                    f"⚠️ <b>Error running <code>/{cmd_name}</code>:</b>\n<code>{html.escape(str(e))}</code>",
                    parse_mode=enums.ParseMode.HTML,
                )
                return True

    # 2. Fallback Pyrogram Dispatcher Loop
    dispatcher = getattr(client, "dispatcher", None)
    if not dispatcher or not getattr(dispatcher, "groups", None):
        return False

    skip_names = {
        "start_and_help_cmd",
        "help_category_shortcut_cmds",
        "_cmd_interactive_listener",
        "pm_auto_reply",
    }

    for grp_id in sorted(dispatcher.groups.keys()):
        for handler in dispatcher.groups[grp_id]:
            cb = getattr(handler, "callback", None)
            if not cb or getattr(cb, "__name__", "") in skip_names:
                continue
            try:
                if await handler.check(client, synth_msg):
                    await cb(client, synth_msg)
                    return True
            except StopPropagation:
                return True
            except ContinuePropagation:
                continue
            except Exception as e:
                logger.error(f"Error executing dispatched command /{cmd_name}: {e}")
                return True

    return False


async def _handle_help_cmd_button(client: Client, query: CallbackQuery, cmd_name: str):
    uid = query.from_user.id if query.from_user else 0
    user_mention = query.from_user.mention if query.from_user else "Admin"
    chat_id = query.message.chat.id
    cmd_clean = cmd_name.strip().lower()

    if cmd_clean == "deleteskip":
        temp.CURRENT = 0
        return await safe_answer(query, "✅ Skip number reset to 0!", show_alert=True)

    if cmd_clean == "currentskip":
        cur = getattr(temp, "CURRENT", 0)
        return await safe_answer(
            query, f"⏩ Current Skip Number: {cur}", show_alert=True
        )

    if cmd_clean in ["enablereaction", "disablereaction"]:
        new_react = cmd_clean == "enablereaction"
        info.ENABLE_REACTION = new_react
        try:
            from plugins.auto_reaction import save_reaction_state

            await save_reaction_state(new_react)
        except Exception:
            pass
        await safe_answer(
            query,
            f"❤️ Auto-Reactions: {'ENABLED ✅' if info.ENABLE_REACTION else 'DISABLED ❌'}",
        )
        text, markup = await build_interactive_help_section(
            client, "helps_settings", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if cmd_clean in ["stats", "total"]:
        await safe_answer(query, "📊 Loading database stats...")
        status_text = await build_status_text()
        return await transition_ui_message(
            client,
            query.message,
            status_text,
            get_back_close_keyboard("helps_utilities"),
        )

    if cmd_clean in ["filter", "addfilter", "filterimage"]:
        await safe_answer(query)
        usage_map = {
            "filter": "Reply to any text or media message with:\n<code>/filter keyword</code>",
            "addfilter": "Reply to a message with inline buttons with:\n<code>/addfilter keyword</code>",
            "filterimage": "Reply to a photo/video with:\n<code>/filterimage keyword</code>",
        }
        return await safe_send_message(
            client,
            chat_id=chat_id,
            text=f"📝 <b>How to use <code>/{cmd_clean}</code>:</b>\n\n{usage_map[cmd_clean]}",
            reply_markup=get_close_keyboard(),
            parse_mode=enums.ParseMode.HTML,
        )

    if cmd_clean in _CMD_INPUT_PROMPTS:
        back_sec, prompt_html = _CMD_INPUT_PROMPTS[cmd_clean]
        await safe_answer(query)
        prompt_msg = await safe_reply_text(
            query.message,
            prompt_html,
            reply_markup=get_cancel_input_keyboard(back_sec),
            parse_mode=enums.ParseMode.HTML,
        )
        resp = await wait_cmd_user_input(
            chat_id, uid, prompt_msg=prompt_msg, timeout=90
        )
        if not resp:
            return
        if resp.text and resp.text.strip().lower() == "/cancel":
            try:
                await resp.delete()
            except Exception:
                pass
            return

        if cmd_clean == "setpmimage" and resp.photo:
            status = await safe_reply_text(
                resp, "⏳ <b>Uploading to Cloud...</b>", parse_mode=enums.ParseMode.HTML
            )
            url = await upload_image_safely(client, resp)
            await save_pm_settings("image", url or resp.photo.file_id)
            if status:
                await safe_edit_text(
                    status,
                    "✅ <b>PM Auto-Reply Image updated!</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            text, markup = await build_interactive_help_section(
                client, "helps_pmautoreply", uid, user_mention, query.message
            )
            return await transition_ui_message(client, query.message, text, markup)

        has_media_or_fwd = bool(
            resp.photo
            or resp.video
            or resp.document
            or resp.sticker
            or resp.audio
            or resp.animation
            or getattr(resp, "forward_from_chat", None)
            or getattr(resp, "forward_origin", None)
        )
        raw_content = resp.text or resp.caption or ""
        arg_str = str(raw_content).strip()
        arg_html = getattr(raw_content, "html", arg_str) if raw_content else ""

        # Allow commands like /broadcast or /group_broadcast to use the user's message as reply_to_message
        needs_reply_obj = has_media_or_fwd or cmd_clean in [
            "broadcast",
            "group_broadcast",
        ]

        if needs_reply_obj and arg_str.lower() != "off":
            handled = await _dispatch_synthetic_command(
                client,
                resp,
                query.from_user,
                cmd_clean,
                arg_text=(
                    "" if cmd_clean in ["broadcast", "group_broadcast"] else arg_str
                ),
                arg_html=(
                    "" if cmd_clean in ["broadcast", "group_broadcast"] else arg_html
                ),
                reply_msg=resp,
            )
        else:
            handled = await _dispatch_synthetic_command(
                client,
                resp,
                query.from_user,
                cmd_clean,
                arg_text=arg_str,
                arg_html=arg_html,
                reply_msg=None,
            )

        if back_sec in [
            "helps_uistart",
            "helps_uihelp",
            "helps_uiabout",
            "helps_spell",
            "helps_forcesub",
            "helps_forceadd",
            "helps_welcome",
            "helps_filters",
        ]:
            text, markup = await build_interactive_help_section(
                client, back_sec, uid, user_mention, query.message
            )
            await transition_ui_message(client, query.message, text, markup)

        if not handled:
            await safe_reply_text(
                resp,
                f"ℹ️ <b>Command:</b> <code>/{cmd_clean} {html.escape(arg_str)}</code>",
                reply_markup=get_close_keyboard(),
                parse_mode=enums.ParseMode.HTML,
            )
        return

    await safe_answer(query, f"⚡ Executing /{cmd_clean}...")
    handled = await _dispatch_synthetic_command(
        client, query.message, query.from_user, cmd_clean
    )

    if cmd_clean in [
        "defaultstarttext",
        "remstartimage",
        "remstartbutton",
        "defaulthelptext",
        "remhelpimage",
        "remhelpbutton",
        "defaultabouttext",
        "remaboutimage",
        "remaboutbutton",
    ]:
        sec = (
            "helps_uistart"
            if "start" in cmd_clean
            else ("helps_uihelp" if "help" in cmd_clean else "helps_uiabout")
        )
        text, markup = await build_interactive_help_section(
            client, sec, uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if cmd_clean in ["remforceadd", "resetadddaily", "resetadd"]:
        text, markup = await build_interactive_help_section(
            client, "helps_forceadd", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if not handled:
        await safe_send_message(
            client,
            chat_id=chat_id,
            text=f"⚙️ <b>Command:</b> <code>/{cmd_clean}</code>\n\n<i>Tap the command above to copy and run it.</i>",
            reply_markup=get_close_keyboard(),
            parse_mode=enums.ParseMode.HTML,
        )


# ============================================================
# 🎛️ INTERACTIVE HELP ACTION CALLBACKS (hact#...) - ADMIN ONLY
# ============================================================
@Client.on_callback_query(filters.regex(r"^hact#"), group=-2)
async def interactive_help_actions_cb(client: Client, query: CallbackQuery):
    uid = query.from_user.id if query.from_user else 0
    user_mention = query.from_user.mention if query.from_user else "Admin"
    if not await is_global_admin(uid):
        return await safe_answer(
            query, "❌ Unauthorized: Bot Admin only!", show_alert=True
        )

    chat_id = query.message.chat.id
    action_raw = query.data.split("#", 1)[1]

    if action_raw.startswith("cancel_input"):
        cancel_cmd_user_input(chat_id)
        await safe_answer(query, "❌ Cancelled.")
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    cancel_cmd_user_input(chat_id)
    action = action_raw

    if action.startswith("cmd#"):
        cmd_name = action.split("#", 1)[1]
        return await _handle_help_cmd_button(client, query, cmd_name)

    if action in ["cw_default", "cw_clear"]:
        if cw_db is not None:
            if action == "cw_default":
                await cw_db.update_one(
                    {"id": "words"}, {"$set": {"use_default": True}}, upsert=True
                )
                await safe_answer(query, "✅ Reverted to Default Clean Words!")
            else:
                await cw_db.update_one(
                    {"id": "words"},
                    {"$set": {"list": [], "use_default": False}},
                    upsert=True,
                )
                await safe_answer(query, "🗑️ Cleared all Clean Words!")
            _invalidate_pm_filter_clean_cache()
        text, markup = await build_cleanwords_panel_ui()
        return await transition_ui_message(client, query.message, text, markup)

    if action in ["cw_add", "cw_set"]:
        await safe_answer(query)
        mode_label = (
            "ADD to existing clean words"
            if action == "cw_add"
            else "REPLACE all clean words"
        )
        prompt = await safe_reply_text(
            query.message,
            f"🧹 <b>Send the words separated by commas to {mode_label}:</b>\n\n"
            f"Example: <code>sandalwood, mkv, @MyChannel, www.site.com</code>",
            reply_markup=get_cancel_input_keyboard("helps_cleanfile"),
            parse_mode=enums.ParseMode.HTML,
        )
        resp = await wait_cmd_user_input(chat_id, uid, prompt_msg=prompt, timeout=90)
        if not resp or not resp.text or resp.text.strip().lower() == "/cancel":
            return
        try:
            await resp.delete()
        except Exception:
            pass

        new_words = [w.strip() for w in resp.text.split(",") if w.strip()]
        if cw_db is not None and new_words:
            if action == "cw_add":
                _, cur_words = await get_clean_words_state()
                merged = list(dict.fromkeys(cur_words + new_words))
            else:
                merged = new_words
            await cw_db.update_one(
                {"id": "words"},
                {"$set": {"list": merged, "use_default": False}},
                upsert=True,
            )
            _invalidate_pm_filter_clean_cache()
        text, markup = await build_cleanwords_panel_ui()
        return await transition_ui_message(client, query.message, text, markup)

    if action in ["at_toggle", "at_on", "at_off", "mc_toggle", "mc_on", "mc_off"]:
        try:
            from plugins.pm_filter import get_feature_flags, set_feature_flag

            at_cur, mc_cur = await get_feature_flags()
            if action.startswith("at_"):
                new_state = (
                    (not at_cur) if action == "at_toggle" else (action == "at_on")
                )
                await set_feature_flag("autotranslate", new_state)
                await safe_answer(
                    query,
                    f"🔤 Auto-Translate: {'ON ✅' if new_state else 'OFF ❌'}",
                )
            else:
                new_state = (
                    (not mc_cur) if action == "mc_toggle" else (action == "mc_on")
                )
                await set_feature_flag("multichoice", new_state)
                await safe_answer(
                    query,
                    f"💡 Multi-Choice: {'ON ✅' if new_state else 'OFF ❌'}",
                )
        except Exception as e:
            return await safe_answer(query, f"Error: {e}", show_alert=True)

        text, markup = await build_interactive_help_section(
            client, "helps_spell", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action in ["spell_toggle", "spell_on", "spell_off"]:
        conn_gid = await get_active_connected_group(uid, query.message)
        if not conn_gid:
            return await safe_answer(
                query,
                "⚠️ Connect a group first using /connect <group_id>!",
                show_alert=True,
            )
        cur_spell = True
        if callable(get_settings):
            try:
                g_set = await get_settings(conn_gid)
                cur_spell = bool(g_set.get("spell_check", True))
            except Exception:
                pass
        new_state = (
            (not cur_spell) if action == "spell_toggle" else (action == "spell_on")
        )
        if callable(save_group_settings):
            await save_group_settings(conn_gid, "spell_check", new_state)
        await safe_answer(
            query,
            f"🔍 Spell Check {'ON ✅' if new_state else 'OFF ❌'} for {conn_gid}!",
        )
        text, markup = await build_interactive_help_section(
            client, "helps_spell", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action in ["sw_list", "sw_def", "sw_clear", "sw_add"]:
        try:
            import plugins.custom_settings as _cs
        except Exception:
            return await safe_answer(
                query, "custom_settings plugin unavailable!", show_alert=True
            )

        if action == "sw_list":
            stops = _cs.get_stopwords()
            if not stops:
                return await safe_answer(
                    query, "🛑 No active stopwords configured.", show_alert=True
                )
            await safe_answer(query, "📋 Sending stopwords list...")
            formatted = ", ".join(f"<code>{html.escape(str(w))}</code>" for w in stops)
            msg_txt = (
                f"🛑 <b>Active Stopwords ({len(stops)})</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"{formatted}\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>Tap any word above to copy it, or use <code>/remstopwords word</code></i>"
            )
            return await safe_send_message(
                client,
                chat_id=chat_id,
                text=msg_txt[:4090],
                reply_markup=get_close_keyboard(),
                parse_mode=enums.ParseMode.HTML,
            )
        elif action == "sw_def":
            _cs.ACTIVE_STOPWORDS = _cs.DEFAULT_STOPWORDS.copy()
            await _cs.update_bot_settings("custom_stopwords", _cs.ACTIVE_STOPWORDS)
            await safe_answer(query, "✅ Stopwords reset to default!")
        elif action == "sw_clear":
            _cs.ACTIVE_STOPWORDS = []
            await _cs.update_bot_settings("custom_stopwords", [])
            await safe_answer(query, "🗑️ All stopwords cleared!")
        elif action == "sw_add":
            await safe_answer(query)
            prompt = await safe_reply_text(
                query.message,
                "🛑 <b>Send the stopwords separated by commas to add:</b>\n\n"
                "Example: <code>dubbed, kannada, fullmovie, link</code>",
                reply_markup=get_cancel_input_keyboard("helps_spell"),
                parse_mode=enums.ParseMode.HTML,
            )
            resp = await wait_cmd_user_input(
                chat_id, uid, prompt_msg=prompt, timeout=90
            )
            if resp and resp.text and resp.text.strip().lower() != "/cancel":
                try:
                    await resp.delete()
                except Exception:
                    pass
                new_w = [w.strip().lower() for w in resp.text.split(",") if w.strip()]
                _cs.ACTIVE_STOPWORDS = list(dict.fromkeys(_cs.ACTIVE_STOPWORDS + new_w))
                await _cs.update_bot_settings("custom_stopwords", _cs.ACTIVE_STOPWORDS)

        text, markup = await build_interactive_help_section(
            client, "helps_spell", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action in [
        "pm_toggle",
        "pm_on",
        "pm_off",
        "pm_adm_toggle",
        "pm_deftext",
        "pm_remimg",
        "pm_rembtn",
        "pm_settext",
        "pm_setbtn",
    ]:
        if action in ["pm_toggle", "pm_on", "pm_off"]:
            cfg = await get_pm_settings(force_refresh=True)
            cur = bool(cfg.get("enabled", True))
            new_state = (not cur) if action == "pm_toggle" else (action == "pm_on")
            await save_pm_settings("enabled", new_state)
            await safe_answer(
                query,
                f"💬 PM Auto-Reply: {'ON ✅' if new_state else 'OFF ❌'}",
            )
        elif action == "pm_adm_toggle":
            cfg = await get_pm_settings(force_refresh=True)
            new_adm = not bool(cfg.get("reply_admins", True))
            await save_pm_settings("reply_admins", new_adm)
            await safe_answer(
                query,
                f"👑 Reply to Admins: {'ON ✅' if new_adm else 'OFF ❌'}",
            )
        elif action == "pm_deftext":
            await save_pm_settings(
                "text",
                "<b>🚫 Don't Message Here, Message Here Only!</b>\n\nI do not respond to direct messages in PM. Please join our official group to request and download movies.",
            )
            await safe_answer(query, "✅ PM Text reset to default!")
        elif action == "pm_remimg":
            await save_pm_settings("image", None)
            await safe_answer(query, "🗑️ PM Image removed!")
        elif action == "pm_rembtn":
            await save_pm_settings("button_text", None)
            await save_pm_settings("button_url", None)
            await safe_answer(query, "🗑️ PM Button removed!")
        elif action == "pm_settext":
            await safe_answer(query)
            prompt = await safe_reply_text(
                query.message,
                "✏️ <b>Send the new PM Auto-Reply Text (HTML supported):</b>",
                reply_markup=get_cancel_input_keyboard("helps_pmautoreply"),
                parse_mode=enums.ParseMode.HTML,
            )
            resp = await wait_cmd_user_input(
                chat_id, uid, prompt_msg=prompt, timeout=90
            )
            if resp and (resp.text or resp.caption):
                raw_txt = resp.text or resp.caption
                if str(raw_txt).strip().lower() != "/cancel":
                    await save_pm_settings(
                        "text", getattr(raw_txt, "html", str(raw_txt))
                    )
                try:
                    await resp.delete()
                except Exception:
                    pass
        elif action == "pm_setbtn":
            await safe_answer(query)
            prompt = await safe_reply_text(
                query.message,
                "🔗 <b>Send the new PM Button in this format:</b>\n"
                "<code>Button Text | https://t.me/your_group | green</code>\n\n"
                "<i>Colors: green, red, blue, normal</i>",
                reply_markup=get_cancel_input_keyboard("helps_pmautoreply"),
                parse_mode=enums.ParseMode.HTML,
            )
            resp = await wait_cmd_user_input(
                chat_id, uid, prompt_msg=prompt, timeout=90
            )
            if resp and resp.text and "|" in resp.text:
                parts = [p.strip() for p in resp.text.split("|")]
                if len(parts) >= 2:
                    b_txt, b_url = parts[0], parts[1]
                    b_col = parts[2].lower() if len(parts) > 2 else "green"
                    if b_col not in ["green", "red", "blue", "normal"]:
                        b_col = "green"
                    await save_pm_settings("button_text", b_txt)
                    await save_pm_settings("button_url", b_url)
                    await save_pm_settings("button_color", b_col)
                try:
                    await resp.delete()
                except Exception:
                    pass

        text, markup = await build_interactive_help_section(
            client, "helps_pmautoreply", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action in ["fsub_toggle", "fsub_on", "fsub_off", "fsub_rot", "fsub_list"]:
        try:
            from plugins.fsub import (
                build_fsub_list_ui,
                get_fsub_config,
                update_fsub_config,
            )

            if action in ["fsub_toggle", "fsub_on", "fsub_off"]:
                cfg = await get_fsub_config(force_refresh=True)
                cur = bool(cfg.get("is_enabled", True))
                new_state = (
                    (not cur) if action == "fsub_toggle" else (action == "fsub_on")
                )
                await update_fsub_config("is_enabled", new_state)
                await safe_answer(
                    query,
                    f"📱 ForceSub: {'ON ✅' if new_state else 'OFF ❌'}",
                )
            elif action == "fsub_rot":
                cfg = await get_fsub_config(force_refresh=True)
                new_rot = not bool(cfg.get("rotation_enabled", False))
                await update_fsub_config("rotation_enabled", new_rot)
                await safe_answer(
                    query,
                    f"🔄 Auto-Rotation: {'ON ✅' if new_rot else 'OFF ❌'}",
                )
            elif action == "fsub_list":
                await safe_answer(query)
                f_text, f_markup = await build_fsub_list_ui("All")
                return await transition_ui_message(
                    client,
                    query.message,
                    f_text,
                    append_fsub_back_button(f_markup),
                )
        except Exception as e:
            return await safe_answer(query, f"FSub error: {e}", show_alert=True)

        text, markup = await build_interactive_help_section(
            client, "helps_forcesub", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action == "fa_toggle":
        conn_gid = await get_active_connected_group(uid, query.message)
        if not conn_gid:
            return await safe_answer(
                query,
                "⚠️ Connect a group first using /connect <group_id>!",
                show_alert=True,
            )
        new_fa = True
        try:
            from plugins.force_add import get_fa_config, set_fa_config

            cur_cfg = await get_fa_config(conn_gid, force_refresh=True)
            new_fa = not bool(cur_cfg.get("enabled", False))
            await set_fa_config(conn_gid, enabled=new_fa)
        except Exception:
            if _BOT_DB is not None:
                try:
                    col = _BOT_DB["force_add_settings"]
                    doc = await col.find_one({"chat_id": int(conn_gid)})
                    cur_fa = bool(doc.get("enabled", False)) if doc else False
                    new_fa = not cur_fa
                    await col.update_one(
                        {"chat_id": int(conn_gid)},
                        {"$set": {"enabled": new_fa, "is_enabled": new_fa}},
                        upsert=True,
                    )
                except Exception:
                    pass
        await safe_answer(
            query,
            f"👥 Force Add: {'ON ✅' if new_fa else 'OFF ❌'} for {conn_gid}!",
        )
        text, markup = await build_interactive_help_section(
            client, "helps_forceadd", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action in ["welc_toggle", "welc_on", "welc_off", "welc_settxt", "welc_del"]:
        conn_gid = await get_active_connected_group(uid, query.message)
        if not conn_gid:
            return await safe_answer(
                query,
                "⚠️ Connect a group first using /connect <group_id>!",
                show_alert=True,
            )
        from plugins.custom_settings import get_group_welcome, update_group_welcome

        if action in ["welc_toggle", "welc_on", "welc_off"]:
            w_doc = await get_group_welcome(conn_gid)
            cur = bool(w_doc.get("welcome", True))
            new_state = (not cur) if action == "welc_toggle" else (action == "welc_on")
            await update_group_welcome(conn_gid, "welcome", new_state)
            await safe_answer(
                query,
                f"👋 Welcome {'ON ✅' if new_state else 'OFF ❌'} for {conn_gid}!",
            )
        elif action == "welc_del":
            await update_group_welcome(conn_gid, "img", None)
            await update_group_welcome(conn_gid, "text", None)
            await update_group_welcome(conn_gid, "welcome", False)
            await safe_answer(query, "🗑️ Welcome settings reset & disabled!")
        elif action == "welc_settxt":
            await safe_answer(query)
            prompt = await safe_reply_text(
                query.message,
                "👋 <b>Send the new Welcome Message text:</b>\n"
                "Variables: <code>{mention}</code>, <code>{title}</code>, <code>{count}</code>",
                reply_markup=get_cancel_input_keyboard("helps_welcome"),
                parse_mode=enums.ParseMode.HTML,
            )
            resp = await wait_cmd_user_input(
                chat_id, uid, prompt_msg=prompt, timeout=90
            )
            if resp and resp.text and resp.text.strip().lower() != "/cancel":
                await update_group_welcome(conn_gid, "text", resp.text)
                await update_group_welcome(conn_gid, "welcome", True)
                try:
                    await resp.delete()
                except Exception:
                    pass

        text, markup = await build_interactive_help_section(
            client, "helps_welcome", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)

    if action == "flt_list":
        conn_gid = await get_active_connected_group(uid, query.message)
        if not conn_gid:
            return await safe_answer(
                query,
                "⚠️ Connect a group first using /connect <group_id>!",
                show_alert=True,
            )
        try:
            from database.filters_mdb import get_filters

            kws = await get_filters(conn_gid)
            if not kws:
                return await safe_answer(
                    query, f"ℹ️ No filters found in group {conn_gid}.", show_alert=True
                )

            await safe_answer(query, f"📋 Sending {len(kws)} filters as text...")

            chat_title = str(conn_gid)
            try:
                c_obj = await client.get_chat(conn_gid)
                if c_obj and c_obj.title:
                    chat_title = html.escape(c_obj.title)
            except Exception:
                pass

            header = (
                f"📋 <b>Group Filters — {chat_title}</b>\n"
                f"🆔 <b>Group ID:</b> <code>{conn_gid}</code>\n"
                f"🔢 <b>Total Filters:</b> <code>{len(kws)}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
            )
            footer = (
                f"\n━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 <i>Tap any filter above to copy it!</i>\n"
                f"🗑️ <i>To delete:</i> <code>/delfilter filter_name</code>"
            )

            lines = [
                f"{idx}. <code>{html.escape(str(kw))}</code>"
                for idx, kw in enumerate(kws, start=1)
            ]

            chunks: List[str] = []
            current_chunk = header
            for line in lines:
                if len(current_chunk) + len(line) + len(footer) + 2 > 4000:
                    chunks.append(current_chunk + footer)
                    current_chunk = (
                        f"📋 <b>Group Filters (Cont.) — {chat_title}</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n{line}\n"
                    )
                else:
                    current_chunk += line + "\n"
            chunks.append(current_chunk + footer)

            for chunk_text in chunks:
                await safe_send_message(
                    client,
                    chat_id=chat_id,
                    text=chunk_text,
                    reply_markup=get_close_keyboard(),
                    parse_mode=enums.ParseMode.HTML,
                )
            return
        except Exception as e:
            return await safe_answer(query, f"Error: {e}", show_alert=True)

    if action == "repair_toggle":
        info.REPAIR_MODE = not bool(getattr(info, "REPAIR_MODE", False))
        await safe_answer(
            query,
            f"🛠️ Repair Mode: {'ON ✅' if info.REPAIR_MODE else 'OFF ❌'}",
        )
        text, markup = await build_interactive_help_section(
            client, "helps_settings", uid, user_mention, query.message
        )
        return await transition_ui_message(client, query.message, text, markup)


# ============================================================
# 🎛️ UI NAVIGATION CALLBACK HANDLER (start, help, about, stats)
# ============================================================
@Client.on_callback_query(
    filters.regex(
        r"^(close_data|channels_page#.*|alertmessage.*|pages|start|help|about|helps_.*|stats|rfrsh)$"
    )
)
async def ui_cb_handler(client: Client, query: CallbackQuery):
    uid = (
        query.from_user.id
        if query.from_user
        else getattr(query.message.sender_chat, "id", 0)
    )
    user_mention = query.from_user.mention if query.from_user else "User"
    bot_uname = getattr(temp, "U_NAME", None) or "my_bot"
    b_name = getattr(temp, "B_NAME", None) or "MovieBot"

    cancel_cmd_user_input(query.message.chat.id)
    is_admin = await is_global_admin(uid)

    if (
        getattr(info, "REPAIR_MODE", False)
        and not is_admin
        and query.data != "close_data"
    ):
        return await safe_answer(query, "🛠️ Bot is under maintenance!", show_alert=True)

    if (
        query.data in ["help", "about", "stats", "rfrsh"]
        or query.data.startswith("helps_")
        or query.data.startswith("channels_page#")
    ):
        if not is_admin:
            return await safe_answer(
                query,
                "❌ Unauthorized: This menu is restricted to Bot Admins only!",
                show_alert=True,
            )

    try:
        if query.data == "close_data":
            await safe_answer(query, "Closed!")
            try:
                await query.message.delete()
            except Exception:
                pass
            if query.message.reply_to_message:
                try:
                    await query.message.reply_to_message.delete()
                except Exception:
                    pass

        elif query.data == "pages":
            await safe_answer(query, "📄 Page Indicator")

        elif query.data.startswith("alertmessage"):
            parts = (
                query.data.split(":", 1)
                if ":" in query.data
                else query.data.split("#", 1)
            )
            alert_txt = parts[1] if len(parts) > 1 else "Alert!"
            await safe_answer(query, alert_txt[:200], show_alert=True)

        elif query.data.startswith("channels_page#"):
            page_num = int(query.data.split("#")[1])
            await safe_answer(query)
            text, reply_markup = await get_channels_page(client, page=page_num)
            await transition_ui_message(client, query.message, text, reply_markup)

        elif query.data == "start":
            await safe_answer(query)
            await _ensure_ui_synced()
            raw_text = format_script_text(
                getattr(script, "START_TXT", "Hello {mention}!"),
                mention=user_mention,
                uname=bot_uname,
                bname=b_name,
            )
            final_text, final_keyboard = safe_parse_text_and_markup(
                raw_text, await get_start_keyboard(client, uid)
            )
            await transition_ui_message(
                client,
                query.message,
                final_text,
                final_keyboard,
                getattr(info, "START_IMG", None),
            )

        elif query.data == "help":
            await safe_answer(query)
            await _ensure_ui_synced()
            raw_text = format_script_text(
                getattr(script, "HELP_TXT", "<b>Help Menu</b>"),
                mention=user_mention,
                uname=bot_uname,
                bname=b_name,
            )
            final_text, final_keyboard = safe_parse_text_and_markup(
                raw_text, await get_help_keyboard()
            )
            await transition_ui_message(
                client,
                query.message,
                final_text,
                final_keyboard,
                getattr(info, "HELP_IMG", None),
            )

        elif query.data == "about":
            await safe_answer(query)
            await _ensure_ui_synced()
            raw_text = format_script_text(
                getattr(script, "ABOUT_TXT", "<b>About {bname}</b>"),
                mention=user_mention,
                uname=bot_uname,
                bname=b_name,
            )
            final_text, final_keyboard = safe_parse_text_and_markup(
                raw_text, await get_about_keyboard()
            )
            await transition_ui_message(
                client,
                query.message,
                final_text,
                final_keyboard,
                getattr(info, "ABOUT_IMG", None),
            )

        elif query.data in ["stats", "rfrsh"]:
            await safe_answer(
                query,
                (
                    "Refreshing Database Stats..."
                    if query.data == "rfrsh"
                    else "Loading Stats..."
                ),
            )
            status_text = await build_status_text()
            await transition_ui_message(
                client, query.message, status_text, get_stats_keyboard()
            )

        elif query.data.startswith("helps_"):
            await safe_answer(query)
            text, buttons = await build_interactive_help_section(
                client, query.data, uid, user_mention, query.message
            )
            await transition_ui_message(client, query.message, text, buttons)

    except Exception as e:
        logger.error(f"UI Callback Handler Error: {e}")
