import asyncio
import difflib
import math
import re
import time
import urllib.parse
import warnings
from datetime import datetime
from io import BytesIO
from logging import ERROR, getLogger
from typing import Dict, Optional, Tuple

import aiohttp
import requests
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ButtonUrlInvalid,
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
from database.connections_mdb import active_connection
from database.filters_mdb import find_filter, get_filters
from database.ia_filterdb import Media as _Media
from database.ia_filterdb import get_search_results
from database.plugin_dbs import plugin_db as _plugin_db
from database.users_chats_db import db as _db
from plugins.custom_settings import get_bot_settings, get_stopwords

# 👑 IMPORTING THE GOD-TIER UNIVERSAL ADMIN ENGINE
from utils import (
    admin_filter,
    get_admin_list,
    get_settings,
    get_size,
    get_uid,
    parse_text_and_markup,
    temp,
)

logger = getLogger(__name__)
logger.setLevel(ERROR)

# ============================================================
# ⚙️ GLOBAL CONFIG & MONGODB SETUP
# ============================================================
cw_db = None
try:
    _DB_CLIENT = AsyncIOMotorClient(info.DATABASE_URI)
    _BOT_DB = _DB_CLIENT[info.DATABASE_NAME]
    cw_db = _BOT_DB["clean_words"]
except Exception as e:
    logger.error(f"Failed to init cw_db: {e}")

DEFAULT_CLEAN_WORDS = ["sandalwood", "mkv", "mp4", "avi", "webm", "zip", "rar"]

try:
    from pyrogram.enums import ButtonStyle

    BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", 1)
    BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", 3)
    BTN_DANGER = getattr(ButtonStyle, "DANGER", 4)
except ImportError:
    BTN_PRIMARY, BTN_SUCCESS, BTN_DANGER = 1, 3, 4


def create_btn(text, url=None, callback_data=None, style=None):
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


# ============================================================
# 🎬 TMDB & POSTER ENGINE (Imdbposter.py Integration)
# ============================================================
Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

LONG_IMDB_DESCRIPTION = False
TMDB_BEARER_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI2ZGU3YTIyZGU1YjE5YTFjNmUyZGU5ZWEyMzE2ZmQxMCIs"
    "Im5iZiI6MTc0NTMyMjQ2Mi41MzMsInN1YiI6IjY4MDc4MWRlYzVjODAzNWZiMDhhNjExNCIsInNjb3"
    "BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.rMMJ2-PBIv8Y7ybxPIEpIlzTEXzuwrm9ruKxAUCAsbw"
)
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/original"
_session: aiohttp.ClientSession | None = None


async def get_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


async def fetch_image(url, size=(860, 1200)):
    if not getattr(info, "IMAGE_FETCH", True) or not url:
        return url
    try:
        session = await get_session()
        async with session.get(url) as response:
            if response.status != 200:
                return None
            data = await response.read()
            img = Image.open(BytesIO(data)).resize(size, Image.LANCZOS)
            out = BytesIO()
            img.save(out, format="JPEG")
            out.seek(0)
            return out
    except Exception as e:
        logger.error(f"Error in fetch_image: {e}")
    return None


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()


def list_to_str(lst):
    return ", ".join(map(str, lst)) if lst else ""


def _list_to_str_tmdb(data_list, limit=10, key=None):
    if not data_list or not isinstance(data_list, list):
        return None
    items = data_list[:limit]
    if key:
        return ", ".join(
            str(item.get(key, "")) for item in items if item and item.get(key)
        )
    return ", ".join(str(item) for item in items if item)


def _sanitize_and_extract(query: str):
    q = str(query).strip()
    year = None
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", q)
    if year_match:
        year = int(year_match.group(1))
        q = q.replace(year_match.group(1), "")
    q = re.sub(
        r"(?i)\b(hdrip|web-dl|webrip|bluray|brrip|dvdrip|dvdscr|tsrip|camrip|hdtc|hevc|x264|x265|1080p|720p|480p|2160p|4k|hindi|kannada|telugu|tamil|malayalam|english|movie|series|season\s*\d+|ep\s*\d+)\b",
        "",
        q,
    )
    q = re.sub(r"[\(\)\[\]\{\}\-_.:]", " ", q)
    return (
        re.sub(r"\s+", " ", q).strip()
        if re.sub(r"\s+", " ", q).strip()
        else str(query).strip()
    ), year


async def _tmdb_get(path, params=None, api_key=None):
    url = f"{TMDB_BASE_URL}/{path.lstrip('/')}"
    _params = params.copy() if params else {}
    _headers = {}
    key_to_use = api_key or getattr(info, "TMDB_API_KEY", None)
    if key_to_use:
        _params["api_key"] = key_to_use
    elif TMDB_BEARER_TOKEN:
        _headers = {
            "Authorization": f"Bearer {TMDB_BEARER_TOKEN}",
            "Content-Type": "application/json;charset=utf-8",
        }
    session = await get_session()
    async with session.get(url, params=_params, headers=_headers, ssl=False) as resp:
        resp.raise_for_status()
        return await resp.json()


async def _fetch_media_details(media_type: str, media_id: int, api_key=None):
    params = {
        "append_to_response": "credits,external_ids,alternative_titles,release_dates,images"
    }
    return await _tmdb_get(f"{media_type}/{media_id}", params=params, api_key=api_key)


async def _search_media_id(query: str, api_key=None):
    clean_title, year = _sanitize_and_extract(query)
    queries_to_try = [clean_title]
    words = clean_title.split()
    if len(words) > 2:
        queries_to_try.append(" ".join(words[:2]))
    if len(words) > 1:
        queries_to_try.append(words[0])
    queries_to_try = list(dict.fromkeys(queries_to_try))

    multi_results = []
    for target_query in queries_to_try:
        if not target_query:
            continue
        params = {
            "query": target_query,
            "language": "en-US",
            "page": 1,
            "include_adult": "false",
        }
        if year:
            params["year"] = year
        try:
            result = await _tmdb_get("search/multi", params=params, api_key=api_key)
            multi_results = result.get("results", [])
            if multi_results:
                break
        except Exception:
            continue

    if not multi_results and year:
        try:
            result = await _tmdb_get(
                "search/multi",
                params={
                    "query": clean_title,
                    "language": "en-US",
                    "page": 1,
                    "include_adult": "false",
                },
                api_key=api_key,
            )
            multi_results = result.get("results", [])
        except Exception:
            pass

    if not multi_results:
        return None, None

    def get_ratio(s1, s2):
        return (
            difflib.SequenceMatcher(None, str(s1).lower(), str(s2).lower()).ratio()
            if s1 and s2
            else 0
        )

    valid_candidates = []
    for r in multi_results:
        mtype = r.get("media_type")
        if mtype not in ["movie", "tv"]:
            continue
        ratio = get_ratio(r.get("title") or r.get("name") or "", clean_title)
        rd_str = r.get("release_date") or r.get("first_air_date") or ""
        match_year = (
            int(rd_str[:4])
            if rd_str and len(rd_str) >= 4 and rd_str[:4].isdigit()
            else None
        )

        year_score = 1.0
        if year and match_year:
            diff = abs(match_year - year)
            if diff == 0:
                year_score = 1.2
            elif diff == 1:
                year_score = 0.9
            else:
                year_score = 0.5

        final_score = (ratio * 100) * year_score + (r.get("popularity", 0) / 10)
        valid_candidates.append((mtype, r["id"], final_score))

    if not valid_candidates:
        first = multi_results[0]
        if first.get("media_type") in ["movie", "tv"]:
            return first["media_type"], first["id"]
        return None, None

    valid_candidates.sort(key=lambda x: x[2], reverse=True)
    return valid_candidates[0][0], valid_candidates[0][1]


def _process_images(images_data):
    posters_by_lang, backdrops_by_lang = {}, {}
    for img in images_data.get("posters", []):
        lang = img.get("iso_639_1") or "no_lang"
        posters_by_lang.setdefault(lang, []).append(
            f"{TMDB_IMAGE_BASE_URL}{img['file_path']}"
        )
    for img in images_data.get("backdrops", []):
        lang = img.get("iso_639_1") or "no_lang"
        backdrops_by_lang.setdefault(lang, []).append(
            f"{TMDB_IMAGE_BASE_URL}{img['file_path']}"
        )
    posters_by_lang["all"] = [
        f"{TMDB_IMAGE_BASE_URL}{i['file_path']}" for i in images_data.get("posters", [])
    ]
    backdrops_by_lang["all"] = [
        f"{TMDB_IMAGE_BASE_URL}{i['file_path']}"
        for i in images_data.get("backdrops", [])
    ]
    return {
        "posters": posters_by_lang,
        "backdrops": backdrops_by_lang,
        "available_languages": sorted(set(posters_by_lang) | set(backdrops_by_lang)),
    }


async def _fetch_tmdb_data(query: str, api_key=None):
    media_type, media_id = await _search_media_id(query, api_key=api_key)
    if not media_id:
        return None

    details = await _fetch_media_details(media_type, media_id, api_key=api_key)
    crew = details.get("credits", {}).get("crew", [])

    certificates = None
    if media_type == "movie" and "release_dates" in details:
        us = [
            r
            for r in details["release_dates"]["results"]
            if r.get("iso_3166_1") == "US"
        ]
        if us and us[0].get("release_dates"):
            certificates = us[0]["release_dates"][0].get("certification")

    runtime = details.get("runtime")
    if media_type == "movie":
        runtime_display = f"{runtime} min" if runtime else None
    else:
        er = _list_to_str_tmdb(details.get("episode_run_time", []))
        runtime_display = f"{er} min" if er else None

    images_structured = _process_images(details.get("images", {}))
    images_structured["original_language"] = details.get("original_language")

    output_data = {
        "query": query,
        "media_type": media_type,
        "media_id": media_id,
        "title": details.get("title") or details.get("name"),
        "localized_title": details.get("original_title")
        or details.get("original_name"),
        "aka": _list_to_str_tmdb(
            details.get("alternative_titles", {}).get("titles", [])
            or details.get("alternative_titles", {}).get("results", []),
            key="title",
        ),
        "kind": media_type,
        "year": (details.get("release_date") or details.get("first_air_date") or "")[
            :4
        ],
        "release_date": details.get("release_date") or details.get("first_air_date"),
        "imdb_id": details.get("external_ids", {}).get("imdb_id"),
        "tmdb_id": details.get("id"),
        "rating": details.get("vote_average"),
        "votes": details.get("vote_count"),
        "runtime": runtime_display,
        "certificates": certificates,
        "genres": _list_to_str_tmdb(details.get("genres", []), key="name"),
        "languages": _list_to_str_tmdb(
            details.get("spoken_languages", []), key="english_name"
        ),
        "countries": _list_to_str_tmdb(
            details.get("production_countries", []), key="name"
        ),
        "director": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Director"], key="name"
        ),
        "writer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") in ["Screenplay", "Writer", "Story"]],
            key="name",
        ),
        "producer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Producer"], key="name"
        ),
        "composer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Original Music Composer"], key="name"
        ),
        "cinematographer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Director of Photography"], key="name"
        ),
        "cast": _list_to_str_tmdb(
            details.get("credits", {}).get("cast", []), key="name", limit=15
        ),
        "plot": details.get("overview"),
        "tagline": details.get("tagline"),
        "box_office": (
            f"${details.get('revenue'):,}" if details.get("revenue", 0) > 0 else "N/A"
        ),
        "distributors": _list_to_str_tmdb(
            details.get("production_companies", []), key="name"
        ),
        "poster_url": (
            f"{TMDB_IMAGE_BASE_URL}{details.get('poster_path')}"
            if details.get("poster_path")
            else None
        ),
        "url": f"https://www.themoviedb.org/{media_type}/{details.get('id')}",
        "images": images_structured,
    }

    if media_type == "tv":
        output_data.update(
            {
                "seasons": details.get("number_of_seasons"),
                "episodes": details.get("number_of_episodes"),
            }
        )
    return output_data


async def get_movie_details(query, bulk=False, id=False, file=None):
    try:
        data = await _fetch_tmdb_data(
            str(query), api_key=getattr(info, "TMDB_API_KEY", None)
        )
        if data:
            return [data] if bulk else data
    except Exception as e:
        logger.error(f"Fallback get_movie_details failed: {e}")
    return None


async def get_movie_detailsx(query, id=False, file=None):
    q = str(query).strip()
    try:
        data = await _fetch_tmdb_data(q, api_key=getattr(info, "TMDB_API_KEY", None))
        if not data:
            return None
    except Exception as e:
        return None

    details = {}
    details["title"] = data.get("title") or data.get("localized_title")
    details["year"] = data.get("year") if data.get("year") else None
    details["release_date"] = data.get("release_date")
    details["rating"] = (
        round(float(data.get("rating", 0)), 1)
        if data.get("rating") is not None
        else None
    )
    details["votes"] = int(data.get("votes", 0)) if data.get("votes") else 0
    details["runtime"] = data.get("runtime")
    details["certificates"] = data.get("certificates")
    details["tmdb_url"] = data.get("url")

    for key in ("genres", "languages", "countries"):
        raw = data.get(key)
        details[key] = [s.strip() for s in raw.split(",")] if raw else []

    for role in (
        "director",
        "writer",
        "producer",
        "composer",
        "cinematographer",
        "cast",
    ):
        raw = data.get(role)
        details[role] = [s.strip() for s in raw.split(",")] if raw else []

    details["plot"] = data.get("plot") or "No plot description available."
    details["tagline"] = data.get("tagline")
    details["box_office"] = data.get("box_office") if data.get("box_office") else None
    raw_dist = data.get("distributors")
    details["distributors"] = (
        [d.strip() for d in raw_dist.split(",")] if raw_dist else []
    )
    details["imdb_id"] = data.get("imdb_id")
    details["tmdb_id"] = data.get("tmdb_id")

    posters = data.get("images", {}).get("posters", {})
    original_language = data.get("images", {}).get("original_language")
    poster_url = data.get("poster_url")
    if not poster_url:
        for key in ("en", original_language, "xx"):
            if key and posters.get(key):
                poster_url = posters[key][0]
                break
    details["poster_url"] = (
        poster_url.replace("/original/", "/w1280/") if poster_url else None
    )

    backdrops = data.get("images", {}).get("backdrops", {})
    backdrop_url = None
    for key in ("en", original_language, "xx", "no_lang"):
        if key and backdrops.get(key):
            backdrop_url = backdrops[key][0]
            break
    details["backdrop_url"] = (
        backdrop_url.replace("/original/", "/w1280/") if backdrop_url else None
    )
    return details


# ============================================================
# 🚫 USER BANS ENGINE (/ban, /unban, /bannedusers)
# ============================================================
@Client.on_message(filters.command("ban") & admin_filter)
async def ban_user_cmd(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/ban [user_id]`")
    try:
        user_id = int(message.command[1])
        if user_id in get_admin_list():
            return await message.reply_text(
                "❌ **You cannot ban a bot administrator!**"
            )
        if user_id == bot.me.id:
            return await message.reply_text("❌ **I cannot ban myself!**")

        await _plugin_db.ban_user(user_id)
        await message.reply_text(
            f"🚫 **User `{user_id}` has been successfully BANNED.**\nThey can no longer use this bot."
        )
    except ValueError:
        await message.reply_text(
            "❌ **Invalid User ID!** Please provide a valid numerical ID."
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{e}`")


@Client.on_message(filters.command("unban") & admin_filter)
async def unban_user_cmd(bot: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("⚙️ **Usage:** `/unban [user_id]`")
    try:
        user_id = int(message.command[1])
        if not await _plugin_db.is_banned(user_id):
            return await message.reply_text(
                f"⚠️ **User `{user_id}` is not currently banned.**"
            )

        await _plugin_db.unban_user(user_id)
        await message.reply_text(
            f"✅ **User `{user_id}` has been successfully UNBANNED.**\nThey can now use the bot again."
        )
    except ValueError:
        await message.reply_text(
            "❌ **Invalid User ID!** Please provide a valid numerical ID."
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{e}`")


@Client.on_message(filters.command("bannedusers") & admin_filter)
async def check_banned_users(bot: Client, message: Message):
    try:
        await message.reply_text(
            f"📊 **Total Banned Users:** `{await _plugin_db.get_ban_count()}`"
        )
    except Exception as e:
        await message.reply_text(f"❌ **Error:** `{e}`")


# ============================================================
# ❤️ AUTO-REACTION ENGINE (/enablereaction, /disablereaction)
# ============================================================
@Client.on_message(filters.command("enablereaction") & admin_filter)
async def enable_react(bot: Client, message: Message):
    await _plugin_db.set_reaction_status(True)
    await message.reply_text("✅ **Auto-Reaction has been ENABLED globally!**")


@Client.on_message(filters.command("disablereaction") & admin_filter)
async def disable_react(bot: Client, message: Message):
    await _plugin_db.set_reaction_status(False)
    await message.reply_text("🚫 **Auto-Reaction has been DISABLED globally.**")


@Client.on_message((filters.group | filters.channel) & ~filters.bot, group=-5)
async def auto_react_heart(bot: Client, message: Message):
    is_enabled = await _plugin_db.get_reaction_status()
    if not is_enabled or (message.from_user and message.from_user.is_bot):
        return
    if (message.text or message.caption) and str(
        message.text or message.caption
    ).startswith("/"):
        return
    try:
        await bot.send_reaction(
            chat_id=message.chat.id, message_id=message.id, emoji="❤️"
        )
    except Exception:
        pass


# ============================================================
# 🔮 FUZZY AUTO-FILTER ENGINE
# ============================================================
async def get_clean_words():
    if cw_db is None:
        return DEFAULT_CLEAN_WORDS
    try:
        doc = await cw_db.find_one({"id": "words"})
        if doc is None or doc.get("use_default", True):
            return DEFAULT_CLEAN_WORDS
        return doc.get("list", [])
    except Exception:
        return DEFAULT_CLEAN_WORDS


def clean_filename(name: str, clean_words: list) -> str:
    if not name:
        return "File"
    if any(w.lower() in ["mkv", "sandalwood"] for w in clean_words):
        name = re.sub(r"(?i)\[?@?sandalwood[^\]\s]*\]?", "", name)
        name = re.sub(r"(?i)\b(sandalwood|mkv|mp4|avi|webm|zip|rar)\b", "", name)
    name = re.sub(r"[_.-]", " ", name)
    for word in clean_words:
        if word.lower() in ["mkv", "sandalwood", "mp4", "avi", "webm", "zip", "rar"]:
            continue
        name = re.sub(rf"(?i){re.escape(word)}", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:42] + "..") if len(name) > 45 else (name or "File")


class TTLCache:
    def __init__(self, maxsize: int = 2000, ttl: int = 1800):
        self._data: Dict[str, Tuple[float, any]] = {}
        self._maxsize, self._ttl = maxsize, ttl

    def set(self, key: str, value: any):
        self._cleanup()
        if len(self._data) >= self._maxsize:
            oldest_key = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest_key, None)
        self._data[key] = (time.time() + self._ttl, value)

    def get(self, key: str) -> Optional[any]:
        item = self._data.get(key)
        if not item:
            return None
        expire_at, value = item
        if time.time() > expire_at:
            self._data.pop(key, None)
            return None
        return value

    def _cleanup(self):
        now = time.time()
        expired = [k for k, (exp, _) in self._data.items() if now > exp]
        for k in expired:
            self._data.pop(k, None)


GLOBAL_SEARCH_CACHE = TTLCache(maxsize=3000, ttl=600)
BUTTONS_CACHE = TTLCache(maxsize=3000, ttl=1800)
USER_LAST_REQ = TTLCache(maxsize=5000, ttl=60)


def sanitize_search_query(text: str) -> str:
    if not text:
        return ""
    q = re.sub(r"https?://\S+|t\.me/\S+|@\w+", "", text.strip().lower())
    q = re.sub(
        r"(?i)\b(1080p|720p|480p|2160p|4k|mkv|mp4|avi|hdrip|web-?dl|webrip|bluray|brrip|dvdrip|x264|x265|hevc|dual audio|hindi|kannada|telugu|tamil|malayalam|english|subtitles|subs|episodes|season\s*\d+|s\d+e\d+|complete)\b",
        "",
        q,
    )
    q = re.sub(r"[^a-zA-Z0-9]", " ", q)
    active_stops = get_stopwords()
    if active_stops:
        pattern = (
            r"\b(" + "|".join(re.escape(w) for w in active_stops if w.strip()) + r")\b"
        )
        q = re.sub(pattern, " ", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


async def god_tier_search(query: str) -> Tuple[list, int, int, str, bool]:
    clean_query = sanitize_search_query(query)
    if not clean_query:
        return [], 0, 0, query, False

    files, offset, total = await get_search_results(
        clean_query, max_results=10, offset=0, filter=True
    )
    if files:
        return files, offset, total, clean_query, False

    sample_docs = (
        await _Media.collection.find({}, {"file_name": 1})
        .limit(2000)
        .to_list(length=2000)
    )
    db_titles = list(
        set(
            [
                sanitize_search_query(d.get("file_name", ""))
                for d in sample_docs
                if d.get("file_name")
            ]
        )
    )

    matches = difflib.get_close_matches(clean_query, db_titles, n=1, cutoff=0.45)
    if matches:
        corrected_query = matches[0]
        c_files, c_offset, c_total = await get_search_results(
            corrected_query, max_results=10, offset=0, filter=True
        )
        if c_files:
            return c_files, c_offset, c_total, corrected_query, True

    return [], 0, 0, query, False


def get_auto_delete_timer() -> int:
    try:
        return int(getattr(info, "AUTO_DELETE_TIME", 0))
    except Exception:
        return 0


async def silent_auto_delete(msg: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def _send_not_found(msg: Message, img: Optional[str], text: str):
    k_msg = None
    try:
        final_text, final_markup = parse_text_and_markup(text)
        final_text = final_text or "<b>🚫 File not found.</b>"
        if img:
            try:
                k_msg = await msg.reply_photo(
                    photo=img,
                    caption=final_text,
                    reply_markup=final_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                k_msg = await msg.reply_text(
                    text=final_text,
                    reply_markup=final_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
        else:
            k_msg = await msg.reply_text(
                text=final_text,
                reply_markup=final_markup,
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception:
        pass

    if k_msg and get_auto_delete_timer() > 0:
        asyncio.create_task(silent_auto_delete(k_msg, get_auto_delete_timer()))


async def auto_filter(client: Client, msg: Message):
    try:
        b_set = await get_bot_settings()
        auto_img = b_set.get("auto_img")

        if (
            not msg
            or not msg.text
            or msg.text.startswith(("/", "!", "#", ".", ",", "?", "@"))
        ):
            return
        search = re.sub(r"https?://\S+|t\.me/\S+|@\w+", "", msg.text).strip()
        if not search:
            return

        settings = await get_settings(msg.chat.id)
        cache_key = search.lower()
        cached_data = GLOBAL_SEARCH_CACHE.get(cache_key)

        if cached_data:
            files, offset, total_results, search_query, was_corrected = cached_data
        else:
            files, offset, total_results, search_query, was_corrected = (
                await god_tier_search(search)
            )
            if files:
                GLOBAL_SEARCH_CACHE.set(
                    cache_key,
                    (files, offset, total_results, search_query, was_corrected),
                )

        if not files:
            return await _send_not_found(
                msg,
                b_set.get("not_found_img", getattr(info, "NOT_FOUND_IMG", None)),
                b_set.get(
                    "not_found_text",
                    getattr(info, "NOT_FOUND_MSG", "<b>🚫 File not found.</b>"),
                ),
            )

        files.sort(
            key=lambda x: (
                x.get("file_size", 0)
                if isinstance(x, dict)
                else getattr(x, "file_size", 0)
            ),
            reverse=True,
        )

        pre = "filep" if settings.get("file_secure", False) else "file"
        btn = [
            [
                create_btn(
                    text="• Bᴀᴄᴋ Uᴘ Cʜᴀɴɴᴇʟ •",
                    url="https://t.me/KR_Picture",
                    style=BTN_SUCCESS,
                )
            ]
        ]
        bot_username = temp.U_NAME or (await client.get_me()).username or "bot"
        mention = msg.from_user.mention if getattr(msg, "from_user", None) else "User"

        cap = (
            f"<b>Hᴇʏ {mention} 👋🏻\n\n🪄 Aᴜᴛᴏ-Cᴏʀʀᴇᴄᴛᴇᴅ: <del>{search.title()}</del> ➔ <code>{search_query.title()}</code>\n➤ Yᴏᴜʀ Fɪʟᴇꜱ Rᴇᴀᴅʏ Nᴏᴡ 👇</b>"
            if was_corrected
            else f"<b>Hᴇʏ {mention} 👋🏻\n\n➤ Tɪᴛʟᴇ : <code>{search_query.title()}</code>\n➤ Yᴏᴜʀ Fɪʟᴇꜱ Rᴇᴀᴅʏ Nᴏᴡ 👇</b>"
        )

        key = f"{msg.chat.id}-{msg.id}"
        BUTTONS_CACHE.set(key, search_query)
        req = get_uid(msg)
        clean_words = await get_clean_words()

        for file in files:
            file_id = str(
                file.get("file_id", "")
                if isinstance(file, dict)
                else getattr(file, "file_id", "")
            )
            raw_name = str(
                file.get("file_name", "Unknown")
                if isinstance(file, dict)
                else getattr(file, "file_name", "Unknown")
            )
            file_size = int(
                file.get("file_size", 0)
                if isinstance(file, dict)
                else getattr(file, "file_size", 0)
            )

            display_name, size_str, url_payload = (
                clean_filename(raw_name, clean_words),
                get_size(file_size),
                f"https://t.me/{bot_username}?start={pre}_{file_id}",
            )
            if settings.get("button", False):
                btn.append(
                    [create_btn(text=f"{size_str} | {display_name}", url=url_payload)]
                )
            else:
                btn.append(
                    [
                        create_btn(text=f"{display_name}", url=url_payload),
                        create_btn(text=f"{size_str}", url=url_payload),
                    ]
                )

        try:
            total_results = int(total_results)
        except Exception:
            total_results = 0
        total_pages = math.ceil(total_results / 10) if total_results > 0 else 1

        if offset:
            btn.append(
                [
                    create_btn(
                        text=f"1/{total_pages}",
                        callback_data="pages",
                        style=BTN_SUCCESS,
                    ),
                    create_btn(
                        text="NEXT ➡️",
                        callback_data=f"next_{req}_{key}_{offset}",
                        style=BTN_PRIMARY,
                    ),
                ]
            )
        else:
            btn.append(
                [create_btn(text="1/1", callback_data="pages", style=BTN_SUCCESS)]
            )

        final_caption, final_markup = parse_text_and_markup(
            cap, InlineKeyboardMarkup(btn)
        )

        m = None
        try:
            if auto_img and str(auto_img).startswith("http"):
                m = await msg.reply_photo(
                    photo=auto_img,
                    caption=final_caption,
                    reply_markup=final_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
            else:
                m = await msg.reply_text(
                    final_caption,
                    reply_markup=final_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            try:
                m = await msg.reply_text(
                    final_caption,
                    reply_markup=final_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                return
        except Exception:
            return

        if get_auto_delete_timer() > 0 and m:
            asyncio.create_task(silent_auto_delete(m, get_auto_delete_timer()))

    except Exception as e:
        logger.error(f"Fatal error in auto_filter: {e}")


@Client.on_message(filters.group & filters.text & filters.incoming)
async def give_filter(client: Client, message: Message):
    if getattr(info, "REPAIR_MODE", False):
        if get_uid(message) not in get_admin_list():
            return

    user_id = get_uid(message)
    if user_id and await _plugin_db.is_banned(user_id):
        return

    if user_id != 0:
        last_req = USER_LAST_REQ.get(str(user_id))
        if last_req and (time.time() - last_req) < 2.0:
            return
        USER_LAST_REQ.set(str(user_id), time.time())

    try:
        await auto_filter(client, message)
    except Exception as e:
        logger.error(f"give_filter top-level error: {e}")


@Client.on_callback_query(filters.regex(r"^(next_)"))
async def pagination_handler(bot: Client, query: CallbackQuery):
    try:
        _, req, key, offset_str = query.data.split("_", 3)
    except ValueError:
        return await query.answer("Invalid button data!", show_alert=True)

    if int(req) not in [query.from_user.id, 0]:
        return await query.answer(
            "⚠️ That's not for you! Request your own file in the group.",
            show_alert=True,
        )

    offset = int(offset_str) if offset_str.isdigit() else 0
    search = BUTTONS_CACHE.get(key)
    if not search:
        return await query.answer(
            "⌛ This search expired. Please type the movie name again!", show_alert=True
        )

    files, n_offset, total = await get_search_results(
        search, max_results=10, offset=offset, filter=True
    )
    if not files:
        return await query.answer("No more files found.", show_alert=True)

    files.sort(
        key=lambda x: (
            x.get("file_size", 0) if isinstance(x, dict) else getattr(x, "file_size", 0)
        ),
        reverse=True,
    )
    settings = await get_settings(query.message.chat.id)
    btn = [
        [
            create_btn(
                text="• Bᴀᴄᴋ Uᴘ Cʜᴀɴɴᴇʟ •",
                url="https://t.me/KR_Picture",
                style=BTN_SUCCESS,
            )
        ]
    ]
    bot_username = temp.U_NAME or (await bot.get_me()).username or "bot"
    pre = "filep" if settings.get("file_secure", False) else "file"
    clean_words = await get_clean_words()

    for file in files:
        file_id = str(
            file.get("file_id", "")
            if isinstance(file, dict)
            else getattr(file, "file_id", "")
        )
        raw_name = str(
            file.get("file_name", "Unknown")
            if isinstance(file, dict)
            else getattr(file, "file_name", "Unknown")
        )
        file_size = int(
            file.get("file_size", 0)
            if isinstance(file, dict)
            else getattr(file, "file_size", 0)
        )

        display_name, size_str, url_payload = (
            clean_filename(raw_name, clean_words),
            get_size(file_size),
            f"https://t.me/{bot_username}?start={pre}_{file_id}",
        )
        if settings.get("button", False):
            btn.append(
                [create_btn(text=f"{size_str} | {display_name}", url=url_payload)]
            )
        else:
            btn.append(
                [
                    create_btn(text=f"{display_name}", url=url_payload),
                    create_btn(text=f"{size_str}", url=url_payload),
                ]
            )

    off_set = 0 if 0 < offset <= 10 else (None if offset == 0 else offset - 10)
    try:
        total = int(total)
    except Exception:
        total = 0
    total_pages = math.ceil(total / 10) if total > 0 else 1
    current_page = math.ceil(offset / 10) + 1 if offset > 0 else 1

    if not n_offset:
        nav_row = [
            create_btn(
                "⬅️ BACK",
                callback_data=f"next_{req}_{key}_{off_set}",
                style=BTN_PRIMARY,
            ),
            create_btn(
                f"Pages {current_page} / {total_pages}",
                callback_data="pages",
                style=BTN_SUCCESS,
            ),
        ]
    elif off_set is None:
        nav_row = [
            create_btn(
                f"{current_page} / {total_pages}",
                callback_data="pages",
                style=BTN_SUCCESS,
            ),
            create_btn(
                "NEXT ➡️",
                callback_data=f"next_{req}_{key}_{n_offset}",
                style=BTN_PRIMARY,
            ),
        ]
    else:
        nav_row = [
            create_btn(
                "⬅️ BACK",
                callback_data=f"next_{req}_{key}_{off_set}",
                style=BTN_PRIMARY,
            ),
            create_btn(
                f"{current_page} / {total_pages}",
                callback_data="pages",
                style=BTN_SUCCESS,
            ),
            create_btn(
                "NEXT ➡️",
                callback_data=f"next_{req}_{key}_{n_offset}",
                style=BTN_PRIMARY,
            ),
        ]
    btn.append(nav_row)

    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(btn))
    except (MessageNotModified, MessageIdInvalid, ButtonUrlInvalid):
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(btn)
            )
        except Exception:
            pass
    try:
        await query.answer()
    except QueryIdInvalid:
        pass
