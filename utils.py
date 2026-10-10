import asyncio
import os
import re
import urllib.parse
from datetime import datetime
from logging import INFO, getLogger
from typing import List, Union

import aiohttp
import requests
from bs4 import BeautifulSoup
from pyrogram import enums, filters
from pyrogram.errors import (
    FloodWait,
    InputUserDeactivated,
    PeerIdInvalid,
    UserIsBlocked,
    UserNotParticipant,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.join_reqs import join_reqs as _db2
from database.users_chats_db import db as _db
import info

logger = getLogger(__name__)
logger.setLevel(INFO)

BTN_URL_REGEX = re.compile(
    r"(\[([^\[]+?)\]\((buttonurl|buttonalert):(?:/{0,2})(.+?)(:same)?\))"
)
SMART_OPEN = "“"
SMART_CLOSE = "”"
START_CHAR = ("'", '"', SMART_OPEN)

# ⚡ EXACT TELEGRAM SUPPORTED COLORS
try:
    from pyrogram.enums import ButtonStyle
    BTN_PRIMARY = getattr(ButtonStyle, "PRIMARY", 1)  # Blue
    BTN_SUCCESS = getattr(ButtonStyle, "SUCCESS", 3)  # Green
    BTN_DANGER = getattr(ButtonStyle, "DANGER", 4)  # Red
except ImportError:
    BTN_PRIMARY = 1
    BTN_SUCCESS = 3
    BTN_DANGER = 4


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


class TMDBWrapper(dict):
    def __getattr__(self, name):
        return self.get(name)


# ============================================================
# 👑 GOD-TIER UNIVERSAL ADMIN ENGINE
# ============================================================
def get_admin_list():
    """Universally fetches and formats the admin list from info.py"""
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, str): 
        return [int(x) for x in raw_admins.replace(",", " ").split() if x.strip().lstrip("-").isdigit()]
    elif isinstance(raw_admins, int): 
        return [raw_admins]
    elif isinstance(raw_admins, list): 
        return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]
    return []

def get_uid(message) -> int:
    """Safely extracts User ID, supporting Anonymous Admins"""
    if getattr(message, "from_user", None): return message.from_user.id
    if getattr(message, "sender_chat", None): return message.sender_chat.id
    return 0

# The master filter that every plugin will import!
admin_filter = filters.create(lambda _, __, msg: bool(get_uid(msg) in get_admin_list()))


# ============================================================
# 🚀 CORE UTILITIES
# ============================================================
def parse_ultra_advanced_query(text):
    text = text.lower().strip()
    text = re.sub(r"[._\-]", " ", text)
    season, episode = None, None
    se_pattern = r"(?:s|season)\s*(\d+)\s*(?:e|ep|episode)\s*(\d+)"
    se_matches = re.search(se_pattern, text)

    if se_matches:
        season, episode = int(se_matches.group(1)), int(se_matches.group(2))
        text = re.sub(se_pattern, "", text)
    else:
        s_match = re.search(r"(?:s|season)\s*(\d+)", text)
        if s_match:
            season = int(s_match.group(1))
            text = re.sub(r"(?:s|season)\s*(\d+)", "", text)
        e_match = re.search(r"(?:e|ep|episode)\s*(\d+)", text)
        if e_match:
            episode = int(e_match.group(1))
            text = re.sub(r"(?:e|ep|episode)\s*(\d+)", "", text)

    qualities = re.findall(
        r"\b(480p|720p|1080p|1440p|2160p|4k|mkv|mp4|hdrip|web\s*dl|bluray)\b", text
    )
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", text)
    languages = re.findall(
        r"\b(hindi|tamil|telugu|malayalam|kannada|english|dual|multi)\b", text
    )

    stopwords = [
        "movie", "full", "watch", "online", "download", "print", "hq", "dubbed",
        "subtitles", "subs", "part", "audio", "video", "kr_picture", "sandalwood",
        "exclusive", "official", "team", "kannada_filmy_group", "telegram", "join", "link",
    ]
    for word in stopwords + qualities + years + languages:
        text = re.sub(rf"\b{word}\b", "", text)
    return {
        "title_words": [w for w in text.split() if w],
        "season": season,
        "episode": episode,
        "qualities": list(set(qualities)),
        "years": list(set(years)),
        "languages": list(set(languages)),
    }


async def is_subscribed(bot, query):
    user_id = get_uid(query.message) if getattr(query, "message", None) else (query.from_user.id if getattr(query, "from_user", None) else 0)
    
    # Universal Admin Bypass
    if user_id in get_admin_list():
        return True
        
    auth_channel = getattr(info, "AUTH_CHANNEL", None)
    req_channel = getattr(info, "REQ_CHANNEL", None)
    
    if not auth_channel and not req_channel:
        return True
        
    if _db2.isActive() and await _db2.get_user(user_id):
        return True
        
    if not auth_channel:
        return True
        
    try:
        user = await bot.get_chat_member(auth_channel, user_id)
        return user.status not in [
            enums.ChatMemberStatus.BANNED,
            enums.ChatMemberStatus.LEFT,
        ]
    except UserNotParticipant:
        return False
    except Exception as e:
        logger.exception(f"Subscription Error: {e}")
        return False


async def get_poster(query, bulk=False, id=False, file=None):
    tmdb_key = getattr(info, "TMDB_API_KEY", None)
    if not tmdb_key:
        return None
        
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10)
    ) as session:
        try:
            if id:
                query_str = str(query)
                media_type = "movie"
                if query_str.startswith("tt"):
                    find_url = f"https://api.themoviedb.org/3/find/{query_str}?api_key={tmdb_key}&external_source=imdb_id"
                    async with session.get(find_url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            results = data.get("movie_results", []) + data.get(
                                "tv_results", []
                            )
                            if not results:
                                return None
                            query_str = str(results[0]["id"])
                            media_type = "tv" if "name" in results[0] else "movie"
                        else:
                            return None

                details_url = f"https://api.themoviedb.org/3/{media_type}/{query_str}?api_key={tmdb_key}&append_to_response=credits"
                async with session.get(details_url) as resp:
                    if resp.status != 200:
                        return None
                    movie = await resp.json()
                    title = movie.get("title") or movie.get("name")
                    year = (
                        movie.get("release_date") or movie.get("first_air_date") or ""
                    )[:4]
                    poster_path = movie.get("poster_path")
                    poster = (
                        f"https://image.tmdb.org/t/p/w500{poster_path}"
                        if poster_path
                        else None
                    )
                    crew = movie.get("credits", {}).get("crew", [])
                    cast_data = movie.get("credits", {}).get("cast", [])
                    director = ", ".join(
                        [c["name"] for c in crew if c.get("job") == "Director"]
                    )
                    writer = ", ".join(
                        [c["name"] for c in crew if c.get("department") == "Writing"]
                    )
                    cast = ", ".join([c["name"] for c in cast_data[:10]])
                    genres = ", ".join([g["name"] for g in movie.get("genres", [])])
                    plot = movie.get("overview", "N/A")
                    if not getattr(info, "LONG_IMDB_DESCRIPTION", False) and len(plot) > 800:
                        plot = plot[:800] + "..."

                    return {
                        "title": title,
                        "votes": movie.get("vote_count", 0),
                        "aka": movie.get("original_title")
                        or movie.get("original_name", "N/A"),
                        "seasons": movie.get("number_of_seasons", "N/A"),
                        "box_office": (
                            f"${movie.get('revenue', 0):,}"
                            if movie.get("revenue")
                            else "N/A"
                        ),
                        "localized_title": title,
                        "kind": media_type,
                        "imdb_id": movie.get("imdb_id", query_str),
                        "cast": cast or "N/A",
                        "runtime": (
                            f"{movie.get('runtime', 'N/A')} min"
                            if movie.get("runtime")
                            else "N/A"
                        ),
                        "countries": ", ".join(
                            [c["name"] for c in movie.get("production_countries", [])]
                        )
                        or "N/A",
                        "certificates": "N/A",
                        "languages": ", ".join(
                            [
                                l.get("english_name", "")
                                for l in movie.get("spoken_languages", [])
                            ]
                        )
                        or "N/A",
                        "director": director or "N/A",
                        "writer": writer or "N/A",
                        "producer": "N/A",
                        "composer": "N/A",
                        "cinematographer": "N/A",
                        "music_team": "N/A",
                        "distributors": ", ".join(
                            [c["name"] for c in movie.get("production_companies", [])]
                        )
                        or "N/A",
                        "release_date": movie.get("release_date")
                        or movie.get("first_air_date")
                        or "N/A",
                        "year": year or "N/A",
                        "genres": genres or "N/A",
                        "poster": poster,
                        "plot": plot,
                        "rating": str(round(movie.get("vote_average", 0), 1)),
                        "url": f"https://www.themoviedb.org/{media_type}/{query_str}",
                    }
            else:
                clean_query = str(query).strip()
                year = None
                year_match = re.search(r"\b(19\d{2}|20\d{2})\b", clean_query)
                if year_match:
                    year = year_match.group(1)
                    clean_query = clean_query.replace(year, "").strip()
                clean_query = re.sub(
                    r"(?i)\b(hdrip|web-dl|webrip|bluray|brrip|dvdrip|dvdscr|tsrip|camrip|hdtc|hevc|x264|x265|1080p|720p|480p|2160p|4k|hindi|kannada|telugu|tamil|malayalam|english|movie|series)\b",
                    "",
                    clean_query,
                )
                clean_query = re.sub(r"[\(\)\[\]\{\}\-_.:]", " ", clean_query)
                clean_query = re.sub(r"\s+", " ", clean_query).strip()
                if not clean_query:
                    clean_query = str(query)

                url = f"https://api.themoviedb.org/3/search/multi?api_key={tmdb_key}&query={urllib.parse.quote(clean_query)}&include_adult=true"
                if year:
                    url += f"&year={year}"

                async with session.get(url) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    results = data.get("results", [])
                    movies = []
                    for item in results:
                        if item.get("media_type") not in ["movie", "tv"]:
                            continue
                        movies.append(
                            TMDBWrapper(
                                {
                                    "movieID": str(item.get("id")),
                                    "title": item.get("title") or item.get("name"),
                                    "year": (
                                        item.get("release_date")
                                        or item.get("first_air_date")
                                        or ""
                                    )[:4],
                                    "kind": item.get("media_type"),
                                }
                            )
                        )
                    max_list_elm = getattr(info, "MAX_LIST_ELM", None)
                    if bulk:
                        return movies[: int(max_list_elm)] if max_list_elm else movies
                    if not movies:
                        return None
                    return await get_poster(movies[0]["movieID"], id=True)
        except Exception as e:
            logger.error(f"TMDB Fetch Error: {e}")
            return None


async def broadcast_messages(user_id, message):
    try:
        await message.copy(chat_id=user_id)
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await broadcast_messages(user_id, message)
    except InputUserDeactivated:
        await _db.delete_user(int(user_id))
        return False, "Deleted"
    except UserIsBlocked:
        return False, "Blocked"
    except PeerIdInvalid:
        await _db.delete_user(int(user_id))
        return False, "Error"
    except Exception:
        return False, "Error"


def _fetch_gagala(text):
    usr_agent = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/61.0.3163.100 Safari/537.36"
    }
    try:
        response = requests.get(
            f"https://www.google.com/search?q={text.replace(' ', '+')}",
            headers=usr_agent,
            timeout=5,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        return [title.getText() for title in soup.find_all("h3") if title.getText()]
    except Exception as e:
        logger.error(f"Google Scrape Error: {e}")
        return []


async def search_gagala(text):
    return await asyncio.to_thread(_fetch_gagala, text)


async def get_settings(group_id):
    settings = temp.SETTINGS.get(group_id)
    if not settings:
        settings = await _db.get_settings(group_id)
        temp.SETTINGS[group_id] = settings
    return settings


async def save_group_settings(group_id, key, value):
    current = await get_settings(group_id)
    current[key] = value
    temp.SETTINGS[group_id] = current
    await _db.update_settings(group_id, current)


def get_size(size):
    units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
    size = float(size)
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        i += 1
        size /= 1024.0
    return "%.2f %s" % (size, units[i])


def split_list(l, n):
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
    return getattr(obj, "file_id", None), getattr(obj, "file_ref", None), media_type


async def extract_user(message: Message, text: str = None):
    client = message._client
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    elif text:
        try:
            return await client.get_users(text)
        except Exception:
            pass
    elif len(message.command) > 1:
        target = message.command[1]
        try:
            return await client.get_users(target)
        except Exception:
            pass
    return message.from_user


def list_to_str(k):
    if not k:
        return "N/A"
    if isinstance(k, str):
        return k
    max_list_elm = getattr(info, "MAX_LIST_ELM", None)
    if max_list_elm:
        k = k[: int(max_list_elm)]
    return ", ".join(str(elem) for elem in k)


def last_online(from_user):
    time_str = ""
    if from_user.is_bot:
        time_str += "🤖 Bot :("
    elif from_user.status == enums.UserStatus.RECENTLY:
        time_str += "Recently"
    elif from_user.status == enums.UserStatus.LAST_WEEK:
        time_str += "Within the last week"
    elif from_user.status == enums.UserStatus.LAST_MONTH:
        time_str += "Within the last month"
    elif from_user.status == enums.UserStatus.LONG_AGO:
        time_str += "A long time ago :("
    elif from_user.status == enums.UserStatus.ONLINE:
        time_str += "Currently Online"
    elif from_user.status == enums.UserStatus.OFFLINE:
        if hasattr(from_user, "last_online_date") and from_user.last_online_date:
            time_str += from_user.last_online_date.strftime("%a, %d %b %Y, %H:%M:%S")
        else:
            time_str += "Offline"
    return time_str


def parser(text, keyword):
    if "buttonalert" in text:
        text = text.replace("\n", "\\n").replace("\t", "\\t")
    buttons, alerts, note_data = [], [], ""
    prev = 0
    try:
        for i, match in enumerate(BTN_URL_REGEX.finditer(text)):
            n_escapes, to_check = 0, match.start(1) - 1
            while to_check > 0 and text[to_check] == "\\":
                n_escapes += 1
                to_check -= 1
            if n_escapes % 2 == 0:
                note_data += text[prev : match.start(1)]
                prev = match.end(1)
                btn = (
                    InlineKeyboardButton(
                        text=match.group(2), callback_data=f"alertmessage:{i}:{keyword}"
                    )
                    if match.group(3) == "buttonalert"
                    else InlineKeyboardButton(
                        text=match.group(2), url=match.group(4).replace(" ", "")
                    )
                )
                if bool(match.group(5)) and buttons:
                    buttons[-1].append(btn)
                else:
                    buttons.append([btn])
                if match.group(3) == "buttonalert":
                    alerts.append(match.group(4))
            else:
                note_data += text[prev:to_check]
                prev = match.start(1) - 1
        note_data += text[prev:]
        return note_data, buttons, alerts if alerts else None
    except Exception as e:
        logger.error(f"Parser Error: {e}")
        return note_data, buttons, None


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


def humanbytes(size):
    if not size:
        return ""
    power, n = 2**10, 0
    dic = {0: " ", 1: "Ki", 2: "Mi", 3: "Gi", 4: "Ti"}
    while size > power and n < 4:
        size /= power
        n += 1
    return str(round(size, 2)) + " " + dic[n] + "B"


def get_readable_time(seconds):
    periods, result = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)], []
    for p_name, p_seconds in periods:
        if seconds >= p_seconds:
            p_val, seconds = divmod(seconds, p_seconds)
            result.append(f"{int(p_val)}{p_name}")
    return " ".join(result)


def parse_text_and_markup(raw_text, static_keyboard=None):
    if not raw_text:
        return "", static_keyboard

    lines = raw_text.split("\n")
    parsed_lines = []
    dynamic_keyboard = []

    # Regex matches custom buttons [Text | URL] or [Text | URL | Color]
    button_pattern = re.compile(r"\[([^\[\]\|]+)\|([^\[\]\|]+)(?:\|([^\[\]\|]+))?\]")

    for line in lines:
        matches = button_pattern.findall(line)
        if matches:
            row = []
            for match in matches:
                btn_text = match[0].strip()
                btn_url = match[1].strip()
                color_str = match[2].strip().lower() if match[2] else "normal"

                style_map = {
                    "blue": BTN_PRIMARY,
                    "green": BTN_SUCCESS,
                    "red": BTN_DANGER,
                    "normal": None,
                }
                btn_style = style_map.get(color_str, None)

                kwargs = {"text": btn_text, "url": btn_url}
                if btn_style is not None:
                    kwargs["style"] = btn_style

                try:
                    row.append(InlineKeyboardButton(**kwargs))
                except TypeError:
                    kwargs.pop("style", None)
                    row.append(InlineKeyboardButton(**kwargs))

            if row:
                dynamic_keyboard.append(row)

            clean_line = button_pattern.sub("", line).strip()
            if clean_line:
                parsed_lines.append(clean_line)
        else:
            parsed_lines.append(line)

    final_text = "\n".join(parsed_lines)

    if static_keyboard and hasattr(static_keyboard, "inline_keyboard"):
        dynamic_keyboard.extend(static_keyboard.inline_keyboard)

    reply_markup = InlineKeyboardMarkup(dynamic_keyboard) if dynamic_keyboard else None
    return final_text, reply_markup
