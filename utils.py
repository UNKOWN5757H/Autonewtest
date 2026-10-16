import asyncio
import inspect
import os
import re
import urllib.parse
from logging import INFO, getLogger
from typing import Dict, List, Optional, Union

import aiohttp
import requests
from bs4 import BeautifulSoup
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    FloodWait,
    InputUserDeactivated,
    PeerIdInvalid,
    UserDeactivated,
    UserDeactivatedBan,
    UserIsBlocked,
    UserNotParticipant,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from database.users_chats_db import db as _db
from info import (
    ADMINS,
    AUTH_CHANNEL,
    LONG_IMDB_DESCRIPTION,
    MAX_LIST_ELM,
    REQ_CHANNEL,
    TMDB_API_KEY,
)

# Safe optional import for join_reqs
try:
    from database.join_reqs import join_reqs as _db2
except ImportError:
    _db2 = None

logger = getLogger(__name__)
logger.setLevel(INFO)

BTN_URL_REGEX = re.compile(
    r"(\[([^\[]+?)\]\((buttonurl|buttonalert):(?:/{0,2})(.+?)(:same)?\))"
)
SMART_OPEN, SMART_CLOSE, START_CHAR = "“", "”", ("'", '"', "“")

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


class temp(object):
    BANNED_USERS = []
    BANNED_CHATS = []
    ME = None
    CURRENT = int(os.environ.get("SKIP", 2))
    CANCEL = False
    MELCOW = {}
    U_NAME = None
    B_NAME = None
    SETTINGS = {}
    INDEX_CANCEL = {}
    INDEX_SPEED = 0


class TMDBWrapper(dict):
    def __getattr__(self, name):
        return self.get(name)


# Fallback TMDB Read Token if TMDB_API_KEY is not set in info.py
_TMDB_BEARER_FALLBACK = (
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI2ZGU3YTIyZGU1YjE5YTFjNmUyZGU5ZWEyMzE2ZmQxMCIs"
    "Im5iZiI6MTc0NTMyMjQ2Mi41MzMsInN1YiI6IjY4MDc4MWRlYzVjODAzNWZiMDhhNjExNCIsInNjb3"
    "BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.rMMJ2-PBIv8Y7ybxPIEpIlzTEXzuwrm9ruKxAUCAsbw"
)
_http_session: Optional[aiohttp.ClientSession] = None


async def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
    return _http_session


# ============================================================
# 👑 FULL POWER ADMIN CHECKER (GLOBAL + CHAT + ANONYMOUS)
# ============================================================
def get_admin_list() -> List[int]:
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

    if message.from_user and message.from_user.id in admin_list:
        return True

    if message.sender_chat and message.sender_chat.id in admin_list:
        return True

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


async def cb_user_admin(client: Client, query: CallbackQuery) -> bool:
    if not query:
        return False
    if isinstance(query, Message):
        return await is_user_admin(client, query)

    admin_list = get_admin_list()
    if query.from_user and query.from_user.id in admin_list:
        return True

    if (
        query.message
        and query.message.chat
        and query.message.chat.type in [enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]
    ):
        if (
            query.message.sender_chat
            and query.message.sender_chat.id == query.message.chat.id
        ):
            return True
        if query.from_user:
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


async def _admin_check_async(_, client: Client, message: Message) -> bool:
    return await is_user_admin(client, message)


async def _cb_admin_check_async(_, client: Client, query: CallbackQuery) -> bool:
    return await cb_user_admin(client, query)


admin_filter = filters.create(_admin_check_async)
cb_admin_filter = filters.create(_cb_admin_check_async)


# ============================================================
# 🛠️ UNIVERSAL UTILITIES
# ============================================================
def parse_ultra_advanced_query(text: str) -> dict:
    text = re.sub(r"[._\-]", " ", str(text or "").lower().strip())
    season, episode = None, None
    se_pattern = r"(?:s|season)\s*(\d+)\s*(?:e|ep|episode)\s*(\d+)"
    if se_matches := re.search(se_pattern, text):
        season, episode = int(se_matches.group(1)), int(se_matches.group(2))
        text = re.sub(se_pattern, "", text)
    else:
        if s_match := re.search(r"(?:s|season)\s*(\d+)", text):
            season = int(s_match.group(1))
            text = re.sub(r"(?:s|season)\s*(\d+)", "", text)
        if e_match := re.search(r"(?:e|ep|episode)\s*(\d+)", text):
            episode = int(e_match.group(1))
            text = re.sub(r"(?:e|ep|episode)\s*(\d+)", "", text)

    qualities = re.findall(
        r"\b(480p|720p|1080p|1440p|2160p|4k|mkv|mp4|hdrip|web\s*dl|bluray)\b", text
    )
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", text)
    languages = re.findall(
        r"\b(hindi|tamil|telugu|malayalam|kannada|english|dual|multi)\b", text
    )

    default_stopwords = [
        "movie",
        "full",
        "watch",
        "online",
        "download",
        "print",
        "hq",
        "dubbed",
        "subtitles",
        "subs",
        "part",
        "audio",
        "kr_picture",
        "sandalwood",
        "exclusive",
        "official",
        "team",
        "kannada_filmy_group",
        "telegram",
        "join",
        "link",
    ]

    try:
        from plugins.custom_settings import get_stopwords

        stopwords = list(set(default_stopwords + get_stopwords()))
    except Exception:
        stopwords = default_stopwords

    for word in stopwords + qualities + years + languages:
        text = re.sub(rf"\b{re.escape(word)}\b", "", text)

    return {
        "title_words": [w for w in text.split() if w],
        "season": season,
        "episode": episode,
        "qualities": list(set(qualities)),
        "years": list(set(years)),
        "languages": list(set(languages)),
    }


async def is_subscribed(bot: Client, query: Union[CallbackQuery, Message]) -> bool:
    if not getattr(info, "IS_FSUB_ENABLED", True):
        return True

    if isinstance(query, Message):
        if await is_user_admin(bot, query):
            return True
        user_id = query.from_user.id if query.from_user else None
    else:
        if await cb_user_admin(bot, query):
            return True
        user_id = query.from_user.id if query.from_user else None

    if not user_id:
        return True

    # 1. Check dynamic ForceSub channels configured via /setfsub in plugins/fsub.py
    try:
        from plugins.fsub import _get_missing_channel_buttons, get_fsub_config

        f_cfg = await get_fsub_config()
        if f_cfg.get("is_enabled", True) and f_cfg.get("channels"):
            missing = await _get_missing_channel_buttons(bot, user_id, f_cfg)
            return len(missing) == 0
    except Exception:
        pass

    auth_ch = getattr(info, "AUTH_CHANNEL", AUTH_CHANNEL)
    req_ch = getattr(info, "REQ_CHANNEL", REQ_CHANNEL)

    if not auth_ch and not req_ch:
        return True

    if _db2 is not None:
        try:
            if _db2.isActive() and await _db2.get_user(user_id):
                return True
        except Exception:
            pass

    if not auth_ch:
        return True

    try:
        user = await bot.get_chat_member(int(auth_ch), int(user_id))
        return user.status not in [
            enums.ChatMemberStatus.BANNED,
            enums.ChatMemberStatus.LEFT,
        ]
    except UserNotParticipant:
        return False
    except Exception as e:
        logger.error(f"Subscription check error: {e}")
        return True


async def get_poster(query, bulk=False, id=False, file=None):
    """Fetches movie/TV metadata from TMDB (delegates to plugins.Imdbposter if loaded)."""
    try:
        from plugins.Imdbposter import get_poster as _imdb_get_poster

        return await _imdb_get_poster(query, bulk=bulk, id=id, file=file)
    except Exception:
        pass

    api_key = getattr(info, "TMDB_API_KEY", TMDB_API_KEY)
    headers = {}
    if not api_key:
        headers = {
            "Authorization": f"Bearer {_TMDB_BEARER_FALLBACK}",
            "Content-Type": "application/json;charset=utf-8",
        }

    session = await _get_http_session()

    async def _tmdb_fetch_json(url: str):
        async with session.get(url, headers=headers, ssl=False) as resp:
            if resp.status != 200:
                return None
            return await resp.json()

    try:
        if id:
            query_str = str(query).strip()
            media_type = "movie"

            if ":" in query_str:
                mtype, mid = query_str.split(":", 1)
                if mtype in ("movie", "tv"):
                    media_type, query_str = mtype, mid
            elif query_str.startswith("tt"):
                key_param = f"api_key={api_key}&" if api_key else ""
                find_url = f"https://api.themoviedb.org/3/find/{query_str}?{key_param}external_source=imdb_id"
                find_data = await _tmdb_fetch_json(find_url)
                if not find_data:
                    return None
                results = find_data.get("movie_results", []) + find_data.get(
                    "tv_results", []
                )
                if not results:
                    return None
                query_str = str(results[0]["id"])
                media_type = "tv" if "name" in results[0] else "movie"

            key_param = f"api_key={api_key}&" if api_key else ""
            detail_url = f"https://api.themoviedb.org/3/{media_type}/{query_str}?{key_param}append_to_response=credits,external_ids"
            movie = await _tmdb_fetch_json(detail_url)

            if not movie and media_type == "movie" and query_str.isdigit():
                media_type = "tv"
                detail_url = f"https://api.themoviedb.org/3/tv/{query_str}?{key_param}append_to_response=credits,external_ids"
                movie = await _tmdb_fetch_json(detail_url)

            if not movie:
                return None

            title = movie.get("title") or movie.get("name") or "Unknown"
            year = (movie.get("release_date") or movie.get("first_air_date") or "")[:4]
            poster_path = movie.get("poster_path")
            crew = movie.get("credits", {}).get("crew", [])
            cast_data = movie.get("credits", {}).get("cast", [])
            plot = movie.get("overview") or "N/A"
            if not LONG_IMDB_DESCRIPTION and len(plot) > 800:
                plot = plot[:800] + "..."

            imdb_id = (
                movie.get("imdb_id")
                or movie.get("external_ids", {}).get("imdb_id")
                or query_str
            )
            poster_full = (
                f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
            )

            return {
                "id": f"{media_type}:{query_str}",
                "movieID": f"{media_type}:{query_str}",
                "title": title,
                "votes": movie.get("vote_count", 0),
                "aka": movie.get("original_title") or movie.get("original_name", "N/A"),
                "seasons": movie.get("number_of_seasons", "N/A"),
                "box_office": (
                    f"${movie.get('revenue', 0):,}" if movie.get("revenue") else "N/A"
                ),
                "localized_title": title,
                "kind": media_type,
                "imdb_id": imdb_id,
                "cast": ", ".join([c["name"] for c in cast_data[:10] if c.get("name")])
                or "N/A",
                "runtime": (
                    f"{movie.get('runtime')} min" if movie.get("runtime") else "N/A"
                ),
                "countries": ", ".join(
                    [
                        c["name"]
                        for c in movie.get("production_countries", [])
                        if c.get("name")
                    ]
                )
                or "N/A",
                "certificates": "N/A",
                "languages": ", ".join(
                    [
                        l.get("english_name", "")
                        for l in movie.get("spoken_languages", [])
                        if l.get("english_name")
                    ]
                )
                or "N/A",
                "director": ", ".join(
                    [
                        c["name"]
                        for c in crew
                        if c.get("job") == "Director" and c.get("name")
                    ]
                )
                or "N/A",
                "writer": ", ".join(
                    [
                        c["name"]
                        for c in crew
                        if c.get("department") == "Writing" and c.get("name")
                    ]
                )
                or "N/A",
                "producer": "N/A",
                "composer": "N/A",
                "cinematographer": "N/A",
                "music_team": "N/A",
                "distributors": ", ".join(
                    [
                        c["name"]
                        for c in movie.get("production_companies", [])
                        if c.get("name")
                    ]
                )
                or "N/A",
                "release_date": movie.get("release_date")
                or movie.get("first_air_date")
                or "N/A",
                "year": year or "N/A",
                "genres": ", ".join(
                    [g["name"] for g in movie.get("genres", []) if g.get("name")]
                )
                or "N/A",
                "poster": poster_full,
                "poster_url": poster_full,
                "plot": plot,
                "rating": str(round(float(movie.get("vote_average") or 0), 1)),
                "url": f"https://www.themoviedb.org/{media_type}/{query_str}",
            }
        else:
            clean_query, year = str(query).strip(), None
            if year_match := re.search(r"\b(19\d{2}|20\d{2})\b", clean_query):
                year = year_match.group(1)
                clean_query = clean_query.replace(year, "").strip()

            clean_query = re.sub(
                r"(?i)\b(hdrip|web-dl|webrip|bluray|brrip|dvdrip|dvdscr|tsrip|camrip|hdtc|hevc|x264|x265|1080p|720p|480p|2160p|4k|hindi|kannada|telugu|tamil|malayalam|english|movie|series)\b",
                "",
                clean_query,
            )
            clean_query = (
                re.sub(r"[\(\)\[\]\{\}\-_.:]", " ", clean_query).strip()
                or str(query).strip()
            )

            key_param = f"api_key={api_key}&" if api_key else ""
            url = f"https://api.themoviedb.org/3/search/multi?{key_param}query={urllib.parse.quote(clean_query)}&include_adult=false"
            if year:
                url += f"&year={year}"

            search_data = await _tmdb_fetch_json(url)
            results = (search_data or {}).get("results", [])

            if not results and year:
                retry_url = f"https://api.themoviedb.org/3/search/multi?{key_param}query={urllib.parse.quote(clean_query)}&include_adult=false"
                retry_data = await _tmdb_fetch_json(retry_url)
                results = (retry_data or {}).get("results", [])

            movies = [
                TMDBWrapper(
                    {
                        "id": f"{item.get('media_type')}:{item.get('id')}",
                        "movieID": f"{item.get('media_type')}:{item.get('id')}",
                        "title": item.get("title") or item.get("name"),
                        "year": (
                            item.get("release_date") or item.get("first_air_date") or ""
                        )[:4],
                        "kind": item.get("media_type"),
                        "media_type": item.get("media_type"),
                    }
                )
                for item in results
                if item.get("media_type") in ["movie", "tv"]
            ]
            if bulk:
                return movies[: int(MAX_LIST_ELM)] if MAX_LIST_ELM else movies
            return await get_poster(movies[0]["movieID"], id=True) if movies else None
    except Exception as e:
        logger.error(f"TMDB Fetch Error: {e}")
        return None


async def broadcast_messages(user_id: int, message: Message):
    try:
        await message.copy(chat_id=int(user_id))
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await broadcast_messages(user_id, message)
    except (InputUserDeactivated, UserDeactivated, UserDeactivatedBan):
        await _db.delete_user(int(user_id))
        return False, "Deleted"
    except UserIsBlocked:
        await _db.delete_user(int(user_id))
        return False, "Blocked"
    except PeerIdInvalid:
        await _db.delete_user(int(user_id))
        return False, "Error"
    except Exception:
        return False, "Error"


def _fetch_gagala(text: str) -> List[str]:
    try:
        response = requests.get(
            f"https://www.google.com/search?q={urllib.parse.quote_plus(text)}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        response.raise_for_status()
        return [
            title.getText()
            for title in BeautifulSoup(response.text, "html.parser").find_all("h3")
            if title.getText()
        ]
    except Exception as e:
        logger.error(f"Google Scrape Error: {e}")
        return []


async def search_gagala(text: str) -> List[str]:
    return await asyncio.to_thread(_fetch_gagala, text)


async def get_settings(group_id: int) -> dict:
    gid = int(group_id)
    settings = temp.SETTINGS.get(gid)
    if not settings:
        settings = await _db.get_settings(gid) or {}
        temp.SETTINGS[gid] = settings
    return settings


async def save_group_settings(group_id: int, key: str, value):
    gid = int(group_id)
    current = await get_settings(gid)
    if not isinstance(current, dict):
        current = {}
    current[key] = value
    temp.SETTINGS[gid] = current
    await _db.update_settings(gid, current)


def get_size(size) -> str:
    if not size or size < 0:
        return "0 Bytes"
    units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
    size = float(size)
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return "%.2f %s" % (size, units[i])


def split_list(l: list, n: int):
    for i in range(0, len(l), n):
        yield l[i : i + n]


def get_file_id(msg: Message):
    if not msg or not msg.media:
        return None, None, None
    media_type = getattr(msg.media, "value", str(msg.media))
    if media_type == "caption":
        return None, None, None
    obj = getattr(msg, media_type, None)
    if not obj:
        return None, None, None
    return (
        getattr(obj, "file_id", None),
        getattr(obj, "file_ref", None),
        media_type,
    )


async def extract_user(message: Message, text: str = None):
    client = message._client
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    elif text:
        try:
            return await client.get_users(
                int(text) if str(text).lstrip("-").isdigit() else text
            )
        except Exception:
            pass
    elif message.command and len(message.command) > 1:
        arg = message.command[1]
        try:
            return await client.get_users(
                int(arg) if str(arg).lstrip("-").isdigit() else arg
            )
        except Exception:
            pass
    return message.from_user


def list_to_str(k) -> str:
    if not k:
        return "N/A"
    if isinstance(k, str):
        return k
    limit = int(MAX_LIST_ELM) if MAX_LIST_ELM else len(k)
    return ", ".join(str(elem) for elem in k[:limit] if elem)


def last_online(from_user) -> str:
    if not from_user:
        return "Unknown"
    if from_user.is_bot:
        return "🤖 Bot :("
    if from_user.status == enums.UserStatus.RECENTLY:
        return "Recently"
    if from_user.status == enums.UserStatus.LAST_WEEK:
        return "Within the last week"
    if from_user.status == enums.UserStatus.LAST_MONTH:
        return "Within the last month"
    if from_user.status == enums.UserStatus.LONG_AGO:
        return "A long time ago :("
    if from_user.status == enums.UserStatus.ONLINE:
        return "Currently Online"
    if from_user.status == enums.UserStatus.OFFLINE:
        return (
            getattr(from_user, "last_online_date").strftime("%a, %d %b %Y, %H:%M:%S")
            if getattr(from_user, "last_online_date", None)
            else "Offline"
        )
    return ""


def parser(text: str, keyword: str):
    if not text:
        return "", [], None
    if "buttonalert" in text:
        text = text.replace("\n", "\\n").replace("\t", "\\t")
    buttons, alerts, note_data, prev = [], [], "", 0
    try:
        for i, match in enumerate(BTN_URL_REGEX.finditer(text)):
            n_escapes, to_check = 0, match.start(1) - 1
            while to_check > 0 and text[to_check] == "\\":
                n_escapes += 1
                to_check -= 1
            if n_escapes % 2 == 0:
                note_data += text[prev : match.start(1)]
                prev = match.end(1)
                if match.group(3) == "buttonalert":
                    btn = InlineKeyboardButton(
                        text=match.group(2),
                        callback_data=f"alertmessage:{i}:{keyword}"[:64],
                    )
                    alerts.append(match.group(4))
                else:
                    raw_url = match.group(4).replace(" ", "")
                    if raw_url.startswith("t.me/"):
                        raw_url = f"https://{raw_url}"
                    btn = InlineKeyboardButton(text=match.group(2), url=raw_url)
                if bool(match.group(5)) and buttons:
                    buttons[-1].append(btn)
                else:
                    buttons.append([btn])
            else:
                note_data += text[prev:to_check]
                prev = match.start(1) - 1
        return note_data + text[prev:], buttons, alerts if alerts else None
    except Exception as e:
        logger.error(f"Parser Error: {e}")
        return note_data or text, buttons, None


def remove_escapes(text: str) -> str:
    res, is_escaped = "", False
    for char in text:
        if is_escaped:
            res += char
            is_escaped = False
        elif char == "\\":
            is_escaped = True
        else:
            res += char
    return res


def humanbytes(size) -> str:
    if not size:
        return "0 B"
    power, n, dic = 2**10, 0, {0: " ", 1: "Ki", 2: "Mi", 3: "Gi", 4: "Ti"}
    while size > power and n < 4:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + dic[n] + "B"


def get_readable_time(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    result = []
    for p_name, p_seconds in [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]:
        if seconds >= p_seconds:
            p_val, seconds = divmod(seconds, p_seconds)
            result.append(f"{int(p_val)}{p_name}")
    return " ".join(result)


def parse_text_and_markup(raw_text: str, static_keyboard: InlineKeyboardMarkup = None):
    if not raw_text:
        return "", static_keyboard
    button_pattern = re.compile(r"\[([^\[\]\|]+)\|([^\[\]\|]+)(?:\|([^\[\]\|]+))?\]")
    parsed_lines, dynamic_keyboard = [], []

    for line in raw_text.split("\n"):
        if matches := button_pattern.findall(line):
            row = []
            for match in matches:
                btn_text = match[0].strip()
                btn_target = match[1].strip()
                if btn_target.startswith("t.me/"):
                    btn_target = f"https://{btn_target}"

                kwargs = {"text": btn_text}
                if btn_target.startswith("callback:"):
                    kwargs["callback_data"] = btn_target.split("callback:", 1)[
                        1
                    ].strip()[:64]
                else:
                    kwargs["url"] = btn_target

                color_key = match[2].strip().lower() if match[2] else "normal"
                btn_style = {
                    "blue": BTN_PRIMARY,
                    "green": BTN_SUCCESS,
                    "red": BTN_DANGER,
                }.get(color_key, None)

                if _SUPPORTS_BTN_STYLE and btn_style is not None:
                    kwargs["style"] = btn_style

                try:
                    row.append(InlineKeyboardButton(**kwargs))
                except Exception:
                    kwargs.pop("style", None)
                    row.append(InlineKeyboardButton(**kwargs))
            if row:
                dynamic_keyboard.append(row)
            if clean_line := button_pattern.sub("", line).strip():
                parsed_lines.append(clean_line)
        else:
            parsed_lines.append(line)

    if static_keyboard and getattr(static_keyboard, "inline_keyboard", None):
        for r in static_keyboard.inline_keyboard:
            dynamic_keyboard.append(list(r))

    return "\n".join(parsed_lines), (
        InlineKeyboardMarkup(dynamic_keyboard) if dynamic_keyboard else None
    )
