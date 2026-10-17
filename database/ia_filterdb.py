import base64
import logging
import re
import time
from struct import pack, unpack

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import BulkWriteError, DuplicateKeyError
from pyrogram.file_id import FileId, FileType
from umongo import Document, fields
from umongo.frameworks.motor_asyncio import MotorAsyncIOInstance

from info import COLLECTION_NAME, DATABASE_NAME, DATABASE_URI, USE_CAPTION_FILTER

try:
    from thefuzz import process
except ImportError:
    process = None
    logging.getLogger(__name__).warning(
        "thefuzz missing! Run 'pip install thefuzz[speedup]'."
    )

logger = getLogger(__name__)
db = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]
instance = MotorAsyncIOInstance(db)

KNOWN_TITLES = set()


class SimpleCache:
    def __init__(self, ttl=900, maxsize=5000):
        self.cache = {}
        self.ttl = ttl
        self.maxsize = maxsize

    def get(self, key):
        item = self.cache.get(key)
        if not item:
            return None
        if time.time() - item["time"] < self.ttl:
            return item["data"]
        self.cache.pop(key, None)
        return None

    def set(self, key, data):
        if len(self.cache) >= self.maxsize:
            oldest_key = next(iter(self.cache))
            self.cache.pop(oldest_key, None)
        self.cache[key] = {"time": time.time(), "data": data}

    def clear(self):
        self.cache.clear()


search_cache = SimpleCache(ttl=900, maxsize=5000)
file_details_cache = SimpleCache(ttl=3600, maxsize=10000)


def add_to_vocab(file_name):
    if not process:
        return
    clean = re.sub(
        r"\b(mkv|mp4|avi|1080p|720p|480p|2160p|bluray|x264|hevc|web-dl|hdrip)\b",
        "",
        file_name,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"[_\-\.\+\[\]\(\)]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean:
        KNOWN_TITLES.add(clean)


def encode_file_id(s_pack, file_ref):
    """Reconstructs valid Pyrogram V2 File IDs from truncated database chunks."""
    try:
        s_unpack = unpack(
            "<iiqq", base64.urlsafe_b64decode(s_pack + "=" * (-len(s_pack) % 4))
        )
        file_id_obj = FileId(
            file_type=FileType(s_unpack[0]),
            dc_id=s_unpack[1],
            media_id=s_unpack[2],
            access_hash=s_unpack[3],
            file_reference=(
                base64.urlsafe_b64decode(file_ref + "=" * (-len(file_ref) % 4))
                if file_ref
                else b""
            ),
        )
        return file_id_obj.encode()
    except Exception as e:
        logger.error(f"Failed to encode file_id: {e}")
        return s_pack


@instance.register
class Media(Document):
    file_id = fields.StrField(attribute="_id")
    file_ref = fields.StrField(allow_none=True)
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)
    caption = fields.StrField(allow_none=True)

    class Meta:
        indexes = [
            {
                "key": [("file_name", "text"), ("caption", "text")],
                "name": "media_text_index",
                "weights": {"file_name": 10, "caption": 2},
            }
        ]
        collection_name = COLLECTION_NAME


class SafeMediaWrapper:
    """Impervious wrapper supporting both Object attribute and Dictionary access."""

    def __init__(self, data):
        self._id = data.get("_id")
        self.file_id = self._id
        self.file_ref = data.get("file_ref", "")
        self.file_name = data.get("file_name", "")
        self.file_size = data.get("file_size", 0)
        self.file_type = data.get("file_type")
        self.mime_type = data.get("mime_type")
        self.caption = data.get("caption")

        raw = data.get("file_id_raw")
        if raw:
            self.full_file_id = raw
        else:
            self.full_file_id = (
                encode_file_id(self._id, self.file_ref) if self.file_ref else self._id
            )

    def get(self, key, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key):
        return getattr(self, key)


async def get_search_results(
    query_text,
    file_type=None,
    max_results=10,
    offset=0,
    filter=False,
    is_autocorrect=False,
):
    cache_key = f"{query_text}_{file_type}_{max_results}_{offset}_{filter}"
    if cached := search_cache.get(cache_key):
        return cached

    try:
        from utils import parse_ultra_advanced_query

        parsed = parse_ultra_advanced_query(query_text)
    except Exception:
        parsed = {}

    conditions = []
    if parsed.get("title_words"):
        ordered_regex = ".*".join([re.escape(w) for w in parsed["title_words"]])
        conditions.append({"file_name": {"$regex": ordered_regex, "$options": "i"}})

    if parsed.get("season") is not None:
        conditions.append(
            {
                "file_name": {
                    "$regex": rf"(s0?{parsed['season']}\b|season\s*0?{parsed['season']}\b)",
                    "$options": "i",
                }
            }
        )
    if parsed.get("episode") is not None:
        conditions.append(
            {
                "file_name": {
                    "$regex": rf"(e0?{parsed['episode']}\b|ep\s*0?{parsed['episode']}\b|episode\s*0?{parsed['episode']}\b)",
                    "$options": "i",
                }
            }
        )

    for y in parsed.get("years", []):
        conditions.append({"file_name": {"$regex": rf"\b{y}\b", "$options": "i"}})
    for q in parsed.get("qualities", []):
        conditions.append({"file_name": {"$regex": rf"\b{q}\b", "$options": "i"}})
    for l in parsed.get("languages", []):
        conditions.append(
            {
                "$or": [
                    {"file_name": {"$regex": rf"\b{l}\b", "$options": "i"}},
                    {"caption": {"$regex": rf"\b{l}\b", "$options": "i"}},
                ]
            }
            if USE_CAPTION_FILTER
            else {"file_name": {"$regex": rf"\b{l}\b", "$options": "i"}}
        )

    if not conditions:
        conditions.append(
            {
                "file_name": {
                    "$regex": re.escape(query_text).replace(r"\ ", r".*"),
                    "$options": "i",
                }
            }
        )

    mongo_query = {"$and": conditions} if len(conditions) > 1 else conditions[0]
    if file_type:
        mongo_query = {"$and": [mongo_query, {"file_type": file_type}]}

    # ⚡ SINGLE-QUERY FETCH: Avoids the slow `count_documents` full-collection scan!
    fetch_limit = max_results + 1
    files = (
        await db[COLLECTION_NAME]
        .find(mongo_query)
        .sort("_id", -1)
        .skip(offset)
        .limit(fetch_limit)
        .to_list(length=fetch_limit)
    )

    has_more = len(files) > max_results
    if has_more:
        files = files[:max_results]
        total_results = offset + max_results + 1
        next_offset = offset + max_results
    else:
        total_results = offset + len(files)
        next_offset = 0

    if total_results == 0 and not is_autocorrect and process and KNOWN_TITLES:
        if best_match := process.extractOne(query_text, KNOWN_TITLES):
            if 75 <= best_match[1] <= 100:
                corrected = await get_search_results(
                    best_match[0],
                    file_type,
                    max_results,
                    offset,
                    filter,
                    is_autocorrect=True,
                )
                if corrected[2] > 0:
                    search_cache.set(cache_key, corrected)
                return corrected

    wrapped_files = []
    for f in files:
        wrapped = SafeMediaWrapper(f)
        if wrapped.file_id:
            file_details_cache.set(str(wrapped.file_id), [wrapped])
        wrapped_files.append(wrapped)

    result = (wrapped_files, next_offset, total_results)
    if total_results > 0:
        search_cache.set(cache_key, result)
    return result


async def save_file(media):
    """Saves a single media object into MongoDB and returns (saved_bool, status_code)."""
    try:
        file_id, file_ref = unpack_new_file_id(media.file_id)
        file_name = re.sub(
            r"[_\-\.\+]", " ", str(getattr(media, "file_name", "") or "File")
        )
        add_to_vocab(file_name)
        doc = {
            "_id": file_id,
            "file_ref": file_ref,
            "file_name": file_name,
            "file_size": getattr(media, "file_size", 0) or 0,
            "file_type": getattr(media, "file_type", None),
            "mime_type": getattr(media, "mime_type", None),
            "caption": (
                str(getattr(media, "caption", None))
                if getattr(media, "caption", None)
                else None
            ),
            "file_id_raw": getattr(media, "file_id", None),
        }
        await db[COLLECTION_NAME].insert_one(doc)
        search_cache.clear()
        wrapped = SafeMediaWrapper(doc)
        file_details_cache.set(str(file_id), [wrapped])
        return True, 1
    except DuplicateKeyError:
        return False, 0
    except Exception as e:
        logger.error(f"save_file error: {e}")
        return False, 2


async def save_batch(media_list):
    if not media_list:
        return 0, 0, 0
    documents = []
    for media in media_list:
        try:
            file_id, file_ref = unpack_new_file_id(media.file_id)
            file_name = re.sub(
                r"[_\-\.\+]", " ", str(getattr(media, "file_name", "") or "")
            )
            add_to_vocab(file_name)
            documents.append(
                {
                    "_id": file_id,
                    "file_ref": file_ref,
                    "file_name": file_name,
                    "file_size": getattr(media, "file_size", 0),
                    "file_type": getattr(media, "file_type", None),
                    "mime_type": getattr(media, "mime_type", None),
                    "caption": (
                        str(getattr(media, "caption", None))
                        if getattr(media, "caption", None)
                        else None
                    ),
                    "file_id_raw": getattr(media, "file_id", None),
                }
            )
        except Exception:
            pass

    if not documents:
        return 0, 0, len(media_list)
    try:
        res = await db[COLLECTION_NAME].insert_many(documents, ordered=False)
        search_cache.clear()
        return len(res.inserted_ids), 0, 0
    except BulkWriteError as bwe:
        search_cache.clear()
        inserted = bwe.details.get("nInserted", 0)
        duplicates = sum(
            1 for err in bwe.details.get("writeErrors", []) if err["code"] == 11000
        )
        return inserted, duplicates, len(media_list) - inserted - duplicates


async def get_file_details(query):
    """Instant RAM-cached lookup for file details when a user clicks a file button."""
    q_str = str(query).strip()
    if cached := file_details_cache.get(q_str):
        return cached

    doc = await db[COLLECTION_NAME].find_one({"_id": q_str})
    if not doc and len(q_str) >= 20:
        doc = await db[COLLECTION_NAME].find_one(
            {"_id": {"$regex": f"^{re.escape(q_str)}"}}
        )

    if doc:
        wrapped = [SafeMediaWrapper(doc)]
        file_details_cache.set(q_str, wrapped)
        return wrapped
    return []


def unpack_new_file_id(new_file_id):
    decoded = FileId.decode(new_file_id)
    file_id = (
        base64.urlsafe_b64encode(
            pack(
                "<iiqq",
                int(getattr(decoded.file_type, "value", decoded.file_type)),
                getattr(decoded, "dc_id", 0),
                getattr(decoded, "media_id", None) or getattr(decoded, "id", 0),
                getattr(decoded, "access_hash", 0),
            )
        )
        .decode()
        .rstrip("=")
    )
    return file_id, base64.urlsafe_b64encode(
        getattr(decoded, "file_reference", b"")
    ).decode().rstrip("=")
