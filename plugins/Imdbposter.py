import asyncio
import difflib
import html
import inspect
import re
import time
import warnings
from io import BytesIO
from logging import ERROR, getLogger
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from PIL import Image
from pyrogram import Client, enums, filters
from pyrogram.errors import (
    ChatWriteForbidden,
    Forbidden,
    MediaCaptionTooLong,
    MediaEmpty,
    MessageIdInvalid,
    MessageNotModified,
    PeerIdInvalid,
    PhotoInvalidDimensions,
    QueryIdInvalid,
    UserIsBlocked,
    WebpageMediaEmpty,
)
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import info
from info import IMAGE_FETCH, TMDB_API_KEY

logger = getLogger(__name__)
logger.setLevel(ERROR)

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
) -> Optional[Message]:
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
) -> Optional[Message]:
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


DEFAULT_IMDB_TEMPLATE = """🎬 <b>Title:</b> <a href="{url}">{title}</a> ({year})
⭐️ <b>Rating:</b> {rating} / 10
🎭 <b>Genres:</b> {genres}
🗣 <b>Languages:</b> {languages}
⏰ <b>Runtime:</b> {runtime}
📜 <b>Plot:</b> {plot}"""

IMDB_TEMPLATE = (
    getattr(info, "IMDB_TEMPLATE", DEFAULT_IMDB_TEMPLATE) or DEFAULT_IMDB_TEMPLATE
)


class SafeDict(dict):
    def __missing__(self, key):
        return "N/A"


Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore", Image.DecompressionBombWarning)

# --- TMDB Configuration & Fast In-Memory Cache ---
TMDB_BEARER_TOKEN = (
    "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiI2ZGU3YTIyZGU1YjE5YTFjNmUyZGU5ZWEyMzE2ZmQxMCIs"
    "Im5iZiI6MTc0NTMyMjQ2Mi41MzMsInN1YiI6IjY4MDc4MWRlYzVjODAzNWZiMDhhNjExNCIsInNjb3"
    "BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.rMMJ2-PBIv8Y7ybxPIEpIlzTEXzuwrm9ruKxAUCAsbw"
)
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/original"

_session: Optional[aiohttp.ClientSession] = None
_TMDB_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cache_get(key: str) -> Optional[Any]:
    item = _TMDB_CACHE.get(key)
    if not item:
        return None
    if time.time() > item[0]:
        _TMDB_CACHE.pop(key, None)
        return None
    return item[1]


def _cache_set(key: str, val: Any, ttl: int = 1800):
    if len(_TMDB_CACHE) >= 1000:
        _TMDB_CACHE.clear()
    _TMDB_CACHE[key] = (time.time() + ttl, val)


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


def _resize_image_sync(data: bytes, size=(860, 1200)) -> BytesIO:
    img = Image.open(BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    resample_filter = getattr(Image, "Resampling", Image).LANCZOS
    img = img.resize(size, resample_filter)
    out = BytesIO()
    out.name = "poster.jpg"
    img.save(out, format="JPEG", quality=90)
    out.seek(0)
    return out


async def fetch_image(url: str, size=(860, 1200)):
    if not IMAGE_FETCH or not url:
        return url
    try:
        session = await get_session()
        async with session.get(url) as response:
            if response.status != 200:
                return None
            data = await response.read()
            return await asyncio.to_thread(_resize_image_sync, data, size)
    except Exception as e:
        logger.error(f"Error in fetch_image: {e}")
    return None


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
    if not query or not query.from_user:
        return False
    admin_list = get_admin_list()
    if query.from_user.id in admin_list:
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


async def _admin_filter_func(_, client: Client, message: Message) -> bool:
    return await is_user_admin(client, message)


async def _cb_admin_filter_func(_, client: Client, query: CallbackQuery) -> bool:
    return await cb_user_admin(client, query)


admin_filter = filters.create(_admin_filter_func)
cb_admin_filter = filters.create(_cb_admin_filter_func)


# ============================================================
# ⚡ TMDB INTERNAL API CALLS
# ============================================================
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
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None
    if year:
        q = q.replace(year_match.group(1), "")
    q = re.sub(
        r"(?i)\b(hdrip|web-dl|webrip|bluray|brrip|dvdrip|dvdscr|tsrip|camrip|hdtc|hevc|x264|x265|1080p|720p|480p|2160p|4k|hindi|kannada|telugu|tamil|malayalam|english|movie|series|season\s*\d+|ep\s*\d+)\b",
        "",
        q,
    )
    q = re.sub(r"[\(\)\[\]\{\}\-_.:]", " ", q)
    return re.sub(r"\s+", " ", q).strip() or str(query).strip(), year


async def _tmdb_get(path, params=None, api_key=None):
    url = f"{TMDB_BASE_URL}/{path.lstrip('/')}"
    _params = params.copy() if params else {}
    _headers = {}
    key_to_use = api_key or TMDB_API_KEY
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
    cache_key = f"det:{media_type}:{media_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    params = {
        "append_to_response": "credits,external_ids,alternative_titles,release_dates,images"
    }
    res = await _tmdb_get(f"{media_type}/{media_id}", params=params, api_key=api_key)
    if res:
        _cache_set(cache_key, res, ttl=1800)
    return res


async def _search_candidates(query: str, api_key=None, limit: int = 10):
    """Searches TMDB and returns a ranked list of candidate dicts."""
    clean_title, year = _sanitize_and_extract(query)
    cache_key = f"cand:{clean_title.lower()}:{year}:{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    queries_to_try = list(
        dict.fromkeys(
            [
                clean_title,
                (
                    " ".join(clean_title.split()[:2])
                    if len(clean_title.split()) > 2
                    else ""
                ),
                clean_title.split()[0] if len(clean_title.split()) > 1 else "",
            ]
        )
    )

    multi_results = []
    for target_query in [q for q in queries_to_try if q]:
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
            params = {
                "query": clean_title,
                "language": "en-US",
                "page": 1,
                "include_adult": "false",
            }
            multi_results = (
                await _tmdb_get("search/multi", params=params, api_key=api_key)
            ).get("results", [])
        except Exception:
            pass

    if not multi_results:
        return []

    scored_candidates = []
    for r in multi_results:
        mtype = r.get("media_type")
        if mtype not in ("movie", "tv"):
            continue

        title_str = str(r.get("title") or r.get("name") or "Unknown")
        ratio = difflib.SequenceMatcher(
            None, title_str.lower(), clean_title.lower()
        ).ratio()
        rd_str = r.get("release_date") or r.get("first_air_date") or ""
        match_year = (
            int(rd_str[:4])
            if rd_str and len(rd_str) >= 4 and rd_str[:4].isdigit()
            else None
        )

        year_score = 1.0
        if year and match_year:
            diff = abs(match_year - year)
            year_score = 1.2 if diff == 0 else 0.9 if diff == 1 else 0.5

        final_score = (ratio * 100) * year_score + (r.get("popularity", 0) / 10)
        mid = r["id"]
        scored_candidates.append(
            {
                "media_type": mtype,
                "media_id": mid,
                "id": f"{mtype}:{mid}",
                "movieID": f"{mtype}:{mid}",
                "title": title_str,
                "year": str(match_year) if match_year else "",
                "score": final_score,
            }
        )

    scored_candidates.sort(key=lambda x: x["score"], reverse=True)
    final_list = scored_candidates[:limit]
    _cache_set(cache_key, final_list, ttl=1800)
    return final_list


async def _search_media_id(query: str, api_key=None):
    candidates = await _search_candidates(query, api_key=api_key, limit=1)
    if not candidates:
        return None, None
    return candidates[0]["media_type"], candidates[0]["media_id"]


def _process_images(images_data):
    posters_by_lang, backdrops_by_lang = {}, {}
    for img in images_data.get("posters", []):
        posters_by_lang.setdefault(img.get("iso_639_1") or "no_lang", []).append(
            f"{TMDB_IMAGE_BASE_URL}{img['file_path']}"
        )
    for img in images_data.get("backdrops", []):
        backdrops_by_lang.setdefault(img.get("iso_639_1") or "no_lang", []).append(
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


def _format_details_output(query: str, media_type: str, media_id: int, details: dict):
    crew = details.get("credits", {}).get("crew", [])

    certificates = None
    if media_type == "movie" and "release_dates" in details:
        us = [
            r
            for r in details["release_dates"].get("results", [])
            if r.get("iso_3166_1") == "US"
        ]
        if us and us[0].get("release_dates"):
            certificates = us[0]["release_dates"][0].get("certification")

    runtime_display = (
        f"{details.get('runtime')} min"
        if media_type == "movie" and details.get("runtime")
        else (
            f"{_list_to_str_tmdb(details.get('episode_run_time', []))} min"
            if media_type != "movie" and details.get("episode_run_time")
            else "N/A"
        )
    )
    images_structured = _process_images(details.get("images", {}))
    images_structured["original_language"] = details.get("original_language")

    raw_rating = details.get("vote_average")
    rating_val = round(float(raw_rating), 1) if raw_rating is not None else "N/A"

    output_data = {
        "query": query,
        "media_type": media_type,
        "media_id": media_id,
        "id": f"{media_type}:{media_id}",
        "movieID": f"{media_type}:{media_id}",
        "title": details.get("title") or details.get("name") or "Unknown",
        "localized_title": details.get("original_title")
        or details.get("original_name")
        or "N/A",
        "aka": _list_to_str_tmdb(
            details.get("alternative_titles", {}).get("titles", [])
            or details.get("alternative_titles", {}).get("results", []),
            key="title",
        )
        or "N/A",
        "kind": media_type,
        "year": (details.get("release_date") or details.get("first_air_date") or "")[:4]
        or "N/A",
        "release_date": details.get("release_date")
        or details.get("first_air_date")
        or "N/A",
        "imdb_id": details.get("external_ids", {}).get("imdb_id") or "N/A",
        "tmdb_id": details.get("id"),
        "rating": rating_val,
        "votes": details.get("vote_count") or 0,
        "runtime": runtime_display,
        "certificates": certificates or "N/A",
        "genres": _list_to_str_tmdb(details.get("genres", []), key="name") or "N/A",
        "languages": _list_to_str_tmdb(
            details.get("spoken_languages", []), key="english_name"
        )
        or "N/A",
        "countries": _list_to_str_tmdb(
            details.get("production_countries", []), key="name"
        )
        or "N/A",
        "director": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Director"], key="name"
        )
        or "N/A",
        "writer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") in ["Screenplay", "Writer", "Story"]],
            key="name",
        )
        or "N/A",
        "producer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Producer"], key="name"
        )
        or "N/A",
        "composer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Original Music Composer"], key="name"
        )
        or "N/A",
        "cinematographer": _list_to_str_tmdb(
            [p for p in crew if p.get("job") == "Director of Photography"], key="name"
        )
        or "N/A",
        "cast": _list_to_str_tmdb(
            details.get("credits", {}).get("cast", []), key="name", limit=15
        )
        or "N/A",
        "plot": details.get("overview") or "No plot available.",
        "tagline": details.get("tagline") or "N/A",
        "box_office": (
            f"${details.get('revenue'):,}" if details.get("revenue", 0) > 0 else "N/A"
        ),
        "distributors": _list_to_str_tmdb(
            details.get("production_companies", []), key="name"
        )
        or "N/A",
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
                "seasons": details.get("number_of_seasons") or "N/A",
                "episodes": details.get("number_of_episodes") or "N/A",
            }
        )
    return output_data


async def _fetch_tmdb_by_id(raw_id: str, api_key=None):
    """Fetches TMDB media details directly by 'movie:123', 'tv:123', 'tt1234567', or numeric ID."""
    s_id = str(raw_id).strip()

    if ":" in s_id:
        mtype, mid = s_id.split(":", 1)
        if mtype in ("movie", "tv") and mid.isdigit():
            details = await _fetch_media_details(mtype, int(mid), api_key=api_key)
            return _format_details_output(s_id, mtype, int(mid), details)

    if s_id.startswith("tt"):
        find_res = await _tmdb_get(
            f"find/{s_id}",
            params={"external_source": "imdb_id"},
            api_key=api_key,
        )
        for mtype, key in [("movie", "movie_results"), ("tv", "tv_results")]:
            results = find_res.get(key, [])
            if results:
                mid = results[0]["id"]
                details = await _fetch_media_details(mtype, mid, api_key=api_key)
                return _format_details_output(s_id, mtype, mid, details)

    if s_id.isdigit():
        mid = int(s_id)
        for mtype in ("movie", "tv"):
            try:
                details = await _fetch_media_details(mtype, mid, api_key=api_key)
                if details and details.get("id"):
                    return _format_details_output(s_id, mtype, mid, details)
            except Exception:
                continue

    return None


async def _fetch_tmdb_data(query: str, api_key=None):
    media_type, media_id = await _search_media_id(query, api_key=api_key)
    if not media_id:
        return None
    details = await _fetch_media_details(media_type, media_id, api_key=api_key)
    return _format_details_output(query, media_type, media_id, details)


async def get_poster(query, bulk=False, id=False, file=None):
    """
    Universal poster & metadata fetcher:
    - id=True: Fetches exact media details by ID ('movie:123', 'tv:123', 'tt1234567', or numeric ID).
    - bulk=True: Returns up to 10 matching search candidates for interactive selection.
    - default: Returns full details dict for the top match.
    """
    try:
        api_key = TMDB_API_KEY or None
        if id:
            return await _fetch_tmdb_by_id(str(query), api_key=api_key)
        if bulk:
            return await _search_candidates(str(query), api_key=api_key, limit=10)
        return await _fetch_tmdb_data(str(query), api_key=api_key)
    except Exception as e:
        logger.error(f"get_poster failed for query={query} (bulk={bulk}, id={id}): {e}")
        return None


async def get_movie_detailsx(query, id=False, file=None):
    q = str(query).strip()
    try:
        data = (
            await _fetch_tmdb_by_id(q, api_key=TMDB_API_KEY or None)
            if id
            else await _fetch_tmdb_data(q, api_key=TMDB_API_KEY or None)
        )
        if not data:
            return None
    except Exception:
        return None

    raw_rating = data.get("rating")
    try:
        parsed_rating = (
            round(float(raw_rating), 1) if raw_rating not in (None, "N/A") else None
        )
    except (ValueError, TypeError):
        parsed_rating = None

    details = {
        "title": data.get("title") or data.get("localized_title"),
        "year": data.get("year") if data.get("year") != "N/A" else None,
        "release_date": (
            data.get("release_date") if data.get("release_date") != "N/A" else None
        ),
        "rating": parsed_rating,
        "votes": int(data.get("votes", 0)) if data.get("votes") else 0,
        "runtime": data.get("runtime") if data.get("runtime") != "N/A" else None,
        "certificates": (
            data.get("certificates") if data.get("certificates") != "N/A" else None
        ),
        "tmdb_url": data.get("url"),
        "plot": data.get("plot") or "No plot description available.",
        "tagline": data.get("tagline") if data.get("tagline") != "N/A" else None,
        "box_office": data.get("box_office"),
        "imdb_id": data.get("imdb_id") if data.get("imdb_id") != "N/A" else None,
        "tmdb_id": data.get("tmdb_id"),
    }

    for key in ("genres", "languages", "countries"):
        val = data.get(key)
        details[key] = (
            [s.strip() for s in val.split(",") if s.strip()]
            if val and val != "N/A"
            else []
        )
    for role in (
        "director",
        "writer",
        "producer",
        "composer",
        "cinematographer",
        "cast",
    ):
        val = data.get(role)
        details[role] = (
            [s.strip() for s in val.split(",") if s.strip()]
            if val and val != "N/A"
            else []
        )
    dist = data.get("distributors")
    details["distributors"] = (
        [d.strip() for d in dist.split(",") if d.strip()]
        if dist and dist != "N/A"
        else []
    )

    posters, original_language, poster_url = (
        data.get("images", {}).get("posters", {}),
        data.get("images", {}).get("original_language"),
        data.get("poster_url"),
    )
    if not poster_url:
        for key in ("en", original_language, "xx"):
            if posters.get(key):
                poster_url = posters[key][0]
                break
    details["poster_url"] = (
        poster_url.replace("/original/", "/w1280/") if poster_url else None
    )

    backdrops, backdrop_url = data.get("images", {}).get("backdrops", {}), None
    for key in ("en", original_language, "xx", "no_lang"):
        if backdrops.get(key):
            backdrop_url = backdrops[key][0]
            break
    details["backdrop_url"] = (
        backdrop_url.replace("/original/", "/w1280/") if backdrop_url else None
    )

    return details


# ============================================================
# 🎯 IMDB SEARCH COMMANDS & BUTTON CALLBACKS
# ============================================================
@Client.on_message(filters.command(["imdb", "search"]) & admin_filter, group=-2)
async def imdb_search(client: Client, message: Message):
    if len(message.command) < 2:
        return await safe_reply_text(
            message,
            "🎬 <b>Usage:</b> <code>/imdb Movie or Series Name</code>",
            parse_mode=enums.ParseMode.HTML,
        )

    query = message.text.split(None, 1)[1].strip()
    wait_msg = await safe_reply_text(
        message,
        f"🔎 <b>Searching TMDB/IMDb for:</b> <code>{html.escape(query)}</code>...",
        parse_mode=enums.ParseMode.HTML,
    )

    try:
        movies = await get_poster(query, bulk=True)
        if not movies:
            if wait_msg:
                return await safe_edit_text(
                    wait_msg,
                    "❌ <b>No results found for your query.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        buttons = []
        for movie in movies[:10]:
            m_id = (
                getattr(movie, "movieID", None)
                or (movie.get("movieID") if isinstance(movie, dict) else None)
                or (movie.get("id") if isinstance(movie, dict) else None)
            )
            title = getattr(movie, "title", None) or (
                movie.get("title") if isinstance(movie, dict) else "Unknown"
            )
            year = getattr(movie, "year", None) or (
                movie.get("year") if isinstance(movie, dict) else ""
            )
            mtype = (
                movie.get("media_type", "movie") if isinstance(movie, dict) else "movie"
            )
            type_icon = "📺" if mtype == "tv" else "🎬"

            if m_id:
                label = (
                    f"{type_icon} {title} ({year})" if year else f"{type_icon} {title}"
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            label[:60],
                            callback_data=f"imdb#{m_id}",
                        )
                    ]
                )

        if not buttons:
            if wait_msg:
                return await safe_edit_text(
                    wait_msg,
                    "❌ <b>Could not parse search results.</b>",
                    parse_mode=enums.ParseMode.HTML,
                )
            return

        buttons.append([InlineKeyboardButton("🔐 Close", callback_data="imdb#close")])
        if wait_msg:
            await safe_edit_text(
                wait_msg,
                f"🎥 <b>Found {len(buttons) - 1} result(s) for:</b> <code>{html.escape(query)}</code>\n"
                f"👇 <i>Select a title below to generate its poster & details:</i>",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception as e:
        logger.error(f"imdb_search error: {e}")
        if wait_msg:
            await safe_edit_text(
                wait_msg,
                f"⚠️ <b>Search error:</b> <code>{html.escape(str(e))}</code>",
                parse_mode=enums.ParseMode.HTML,
            )


@Client.on_callback_query(filters.regex(r"^imdb#"), group=-2)
async def imdb_callback(client: Client, query: CallbackQuery):
    if not await cb_user_admin(client, query):
        return await safe_answer(
            query, "❌ Unauthorized: Admins only!", show_alert=True
        )

    try:
        _, payload = query.data.split("#", 1)

        if payload == "close":
            await safe_answer(query, "Closed!")
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        await safe_answer(query, "⏳ Fetching movie details...", show_alert=False)

        imdb = await get_poster(query=payload, id=True)
        if not imdb:
            return await safe_edit_text(
                query.message,
                "❌ <b>Could not load details for this title.</b>",
                parse_mode=enums.ParseMode.HTML,
            )

        imdb_id = imdb.get("imdb_id")
        if imdb_id and imdb_id != "N/A":
            url = f"https://www.imdb.com/title/{imdb_id}/"
        else:
            url = imdb.get("url", "https://www.themoviedb.org/")

        buttons = [
            [InlineKeyboardButton(f"🔗 {imdb.get('title', 'View Link')}", url=url)],
            [InlineKeyboardButton("🔐 Close", callback_data="imdb#close")],
        ]

        plot_text = str(imdb.get("plot") or "N/A")
        if len(plot_text) > 450:
            plot_text = plot_text[:447] + "..."

        format_args = SafeDict(
            query=html.escape(str(imdb.get("title", "N/A"))),
            title=html.escape(str(imdb.get("title", "N/A"))),
            votes=imdb.get("votes", "N/A"),
            aka=html.escape(str(imdb.get("aka", "N/A"))),
            seasons=imdb.get("seasons", "N/A"),
            box_office=html.escape(str(imdb.get("box_office", "N/A"))),
            localized_title=html.escape(str(imdb.get("localized_title", "N/A"))),
            kind=html.escape(str(imdb.get("kind", "N/A")).upper()),
            imdb_id=imdb.get("imdb_id", payload),
            cast=html.escape(str(imdb.get("cast", "N/A"))),
            runtime=html.escape(str(imdb.get("runtime", "N/A"))),
            countries=html.escape(str(imdb.get("countries", "N/A"))),
            certificates=html.escape(str(imdb.get("certificates", "N/A"))),
            languages=html.escape(str(imdb.get("languages", "N/A"))),
            director=html.escape(str(imdb.get("director", "N/A"))),
            writer=html.escape(str(imdb.get("writer", "N/A"))),
            producer=html.escape(str(imdb.get("producer", "N/A"))),
            composer=html.escape(str(imdb.get("composer", "N/A"))),
            cinematographer=html.escape(str(imdb.get("cinematographer", "N/A"))),
            music_team=html.escape(str(imdb.get("composer", "N/A"))),
            distributors=html.escape(str(imdb.get("distributors", "N/A"))),
            release_date=html.escape(str(imdb.get("release_date", "N/A"))),
            year=html.escape(str(imdb.get("year", "N/A"))),
            genres=html.escape(str(imdb.get("genres", "N/A"))),
            poster=imdb.get("poster_url", "N/A"),
            plot=html.escape(plot_text),
            rating=imdb.get("rating", "N/A"),
            url=url,
        )

        # Convert any legacy Markdown bold (**text**) in custom IMDB_TEMPLATE to HTML
        raw_caption = IMDB_TEMPLATE.format_map(format_args)
        caption = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", raw_caption)
        if len(caption) > 1024:
            caption = caption[:1020] + "..."

        poster = imdb.get("poster_url")
        reply_markup = InlineKeyboardMarkup(buttons)

        if poster:
            photo_obj = await fetch_image(poster) if IMAGE_FETCH else poster
            photo_to_send = photo_obj or poster
            try:
                await query.message.reply_photo(
                    photo=photo_to_send,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=enums.ParseMode.HTML,
                )
                await query.message.delete()
                return
            except (
                MediaEmpty,
                PhotoInvalidDimensions,
                WebpageMediaEmpty,
                MediaCaptionTooLong,
            ):
                try:
                    fallback_poster = poster.replace("/original/", "/w780/")
                    await query.message.reply_photo(
                        photo=fallback_poster,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode=enums.ParseMode.HTML,
                    )
                    await query.message.delete()
                    return
                except Exception:
                    pass

        await safe_edit_text(
            query.message,
            caption,
            reply_markup=reply_markup,
            disable_web_page_preview=False,
            parse_mode=enums.ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"IMDb Callback Error: {e}")
        await safe_edit_text(
            query.message,
            f"⚠️ <b>Error loading details:</b> <code>{html.escape(str(e))}</code>",
            parse_mode=enums.ParseMode.HTML,
        )
