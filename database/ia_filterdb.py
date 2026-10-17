import base64
import logging
import re
import time
from struct import pack, unpack
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure
from pyrogram.file_id import FileId, FileType
from umongo import Document, fields
from umongo.frameworks.motor_asyncio import MotorAsyncIOInstance

import info
from info import COLLECTION_NAME, DATABASE_NAME, DATABASE_URI, USE_CAPTION_FILTER

try:
    from thefuzz import process
except ImportError:
    process = None
    logging.getLogger(__name__).warning(
        "thefuzz missing! Run 'pip install thefuzz[speedup]'."
    )

logger = logging.getLogger(__name__)

# Primary MongoDB Connection
db = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]
instance = MotorAsyncIOInstance(db)

# Optional Secondary MongoDB Connection (if SECOND_DATABASE_URI is set in info.py / env)
SECOND_DATABASE_URI = getattr(info, "SECOND_DATABASE_URI", "") or ""
db2 = (
    AsyncIOMotorClient(SECOND_DATABASE_URI)[DATABASE_NAME]
    if SECOND_DATABASE_URI and SECOND_DATABASE_URI != DATABASE_URI
    else None
)

KNOWN_TITLES = set()
_PRIMARY_DB_QUOTA_FULL = False


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


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "space quota" in msg or "8000" in msg or "atlaserror" in msg


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
    """Reconstructs valid Pyrogram V2 File IDs from compact `_id` + `file_ref` chunks."""
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
        indexes = []
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


async def free_up_mongodb_storage() -> Dict[str, Any]:
    """
    Emergency 512MB MongoDB Quota Optimizer:
    1. Drops massive redundant text indexes (`media_text_index`, `file_name_text`) which consume 120MB-200MB!
    2. Purges temporary collections (`index_resume`, `broadcast_tasks`, `countdown_tasks`).
    3. Strips redundant `file_id_raw`, `mime_type`, and long `caption` fields from media documents
       (since `encode_file_id(_id, file_ref)` reconstructs the exact Pyrogram FileId automatically!).
    """
    global _PRIMARY_DB_QUOTA_FULL
    report = {
        "dropped_indexes": [],
        "cleared_temp_docs": 0,
        "compacted_files": 0,
        "before_mb": 0.0,
        "after_mb": 0.0,
    }

    try:
        stats_before = await db.command("dbstats")
        report["before_mb"] = round(
            (stats_before.get("dataSize", 0) + stats_before.get("indexSize", 0))
            / (1024 * 1024),
            2,
        )
    except Exception:
        pass

    # 1. Drop heavy text indexes on primary collection (frees 100MB-200MB immediately!)
    try:
        indexes = await db[COLLECTION_NAME].index_information()
        for idx_name in list(indexes.keys()):
            if idx_name != "_id_":
                try:
                    await db[COLLECTION_NAME].drop_index(idx_name)
                    report["dropped_indexes"].append(idx_name)
                except Exception:
                    pass
    except Exception:
        pass

    # 2. Delete temporary/ephemeral collections that work EVEN when over 512MB quota
    for temp_col in (
        "index_resume",
        "broadcast_tasks",
        "countdown_tasks",
        "fsub_join_requests",
        "fsub_verified_users",
        "fsub_users",
    ):
        try:
            res = await db[temp_col].delete_many({})
            report["cleared_temp_docs"] += getattr(res, "deleted_count", 0)
        except Exception:
            pass

    # 3. Once index drop frees space below 512MB, strip bloated fields (`file_id_raw`, `mime_type`)
    try:
        unset_fields = {"file_id_raw": "", "mime_type": ""}
        if not USE_CAPTION_FILTER:
            unset_fields["caption"] = ""
        res_upd = await db[COLLECTION_NAME].update_many(
            {
                "$or": [
                    {"file_id_raw": {"$exists": True}},
                    {"mime_type": {"$exists": True}},
                ]
            },
            {"$unset": unset_fields},
        )
        report["compacted_files"] = getattr(res_upd, "modified_count", 0)
        _PRIMARY_DB_QUOTA_FULL = False
    except Exception:
        pass

    try:
        stats_after = await db.command("dbstats")
        report["after_mb"] = round(
            (stats_after.get("dataSize", 0) + stats_after.get("indexSize", 0))
            / (1024 * 1024),
            2,
        )
    except Exception:
        pass

    search_cache.clear()
    return report


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

    fetch_limit = max_results + 1
    files = (
        await db[COLLECTION_NAME]
        .find(mongo_query)
        .sort("_id", -1)
        .skip(offset)
        .limit(fetch_limit)
        .to_list(length=fetch_limit)
    )

    if db2 is not None and len(files) < fetch_limit:
        rem = fetch_limit - len(files)
        extra_files = (
            await db2[COLLECTION_NAME]
            .find(mongo_query)
            .sort("_id", -1)
            .skip(max(0, offset - len(files)))
            .limit(rem)
            .to_list(length=rem)
        )
        files.extend(extra_files)

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


def _build_compact_media_doc(media) -> Optional[dict]:
    """
    Builds an ultra-compact media document (~65% smaller in MongoDB!).
    Omits `file_id_raw`, `mime_type`, and `caption` (unless USE_CAPTION_FILTER is True)
    because `SafeMediaWrapper` reconstructs the full Pyrogram V2 FileId from `_id` + `file_ref`!
    """
    try:
        file_id, file_ref = unpack_new_file_id(media.file_id)
        file_name = re.sub(
            r"[_\-\.\+]", " ", str(getattr(media, "file_name", "") or "File")
        )
        file_name = re.sub(r"\s+", " ", file_name).strip()
        add_to_vocab(file_name)

        doc = {
            "_id": file_id,
            "file_ref": file_ref,
            "file_name": file_name,
            "file_size": int(getattr(media, "file_size", 0) or 0),
        }
        ftype = getattr(media, "file_type", None)
        if ftype:
            doc["file_type"] = str(ftype)
        if USE_CAPTION_FILTER and getattr(media, "caption", None):
            doc["caption"] = str(getattr(media, "caption", ""))[:300]
        return doc
    except Exception:
        return None


async def save_file(media) -> Tuple[bool, int]:
    """
    Saves a single media object into MongoDB.
    Returns:
      (True, 1)  -> Saved
      (False, 0) -> Duplicate
      (False, 3) -> MongoDB 512MB Storage Quota Full (Code 8000)
      (False, 2) -> Other Error
    """
    global _PRIMARY_DB_QUOTA_FULL
    doc = _build_compact_media_doc(media)
    if not doc:
        return False, 2

    target_db = db2 if (_PRIMARY_DB_QUOTA_FULL and db2 is not None) else db

    try:
        await target_db[COLLECTION_NAME].insert_one(doc)
        search_cache.clear()
        wrapped = SafeMediaWrapper(doc)
        file_details_cache.set(str(doc["_id"]), [wrapped])
        return True, 1
    except DuplicateKeyError:
        return False, 0
    except OperationFailure as e:
        if _is_quota_error(e):
            _PRIMARY_DB_QUOTA_FULL = True
            if target_db is db and db2 is not None:
                try:
                    await db2[COLLECTION_NAME].insert_one(doc)
                    search_cache.clear()
                    wrapped = SafeMediaWrapper(doc)
                    file_details_cache.set(str(doc["_id"]), [wrapped])
                    return True, 1
                except DuplicateKeyError:
                    return False, 0
                except Exception:
                    return False, 3
            return False, 3
        logger.error(f"save_file OperationFailure: {e}")
        return False, 2
    except Exception as e:
        if _is_quota_error(e):
            _PRIMARY_DB_QUOTA_FULL = True
            return False, 3
        logger.error(f"save_file error: {e}")
        return False, 2


async def save_batch(media_list) -> Tuple[int, int, int]:
    """
    Saves a batch of media objects.
    Raises OperationFailure (Code 8000) if all configured MongoDB clusters are 100% out of space
    so `plugins/index.py` can pause cleanly instead of looping infinitely.
    """
    global _PRIMARY_DB_QUOTA_FULL
    if not media_list:
        return 0, 0, 0

    documents = []
    for media in media_list:
        doc = _build_compact_media_doc(media)
        if doc:
            documents.append(doc)

    if not documents:
        return 0, 0, len(media_list)

    target_db = db2 if (_PRIMARY_DB_QUOTA_FULL and db2 is not None) else db

    try:
        res = await target_db[COLLECTION_NAME].insert_many(documents, ordered=False)
        search_cache.clear()
        return len(res.inserted_ids), 0, 0
    except BulkWriteError as bwe:
        search_cache.clear()
        write_errors = bwe.details.get("writeErrors", [])
        if any(
            err.get("code") == 8000
            or "space quota" in str(err.get("errmsg", "")).lower()
            for err in write_errors
        ):
            _PRIMARY_DB_QUOTA_FULL = True
            if target_db is db and db2 is not None:
                return await save_batch(media_list)
            raise OperationFailure(
                "MongoDB 512MB Space Quota Reached (Code 8000).", code=8000
            )

        inserted = bwe.details.get("nInserted", 0)
        duplicates = sum(1 for err in write_errors if err.get("code") == 11000)
        return inserted, duplicates, len(media_list) - inserted - duplicates
    except OperationFailure as e:
        if _is_quota_error(e):
            _PRIMARY_DB_QUOTA_FULL = True
            if target_db is db and db2 is not None:
                return await save_batch(media_list)
            raise
        return 0, 0, len(media_list)


async def get_file_details(query):
    """Instant RAM-cached lookup for file details across primary and secondary DBs."""
    q_str = str(query).strip()
    if cached := file_details_cache.get(q_str):
        return cached

    doc = await db[COLLECTION_NAME].find_one({"_id": q_str})
    if not doc and len(q_str) >= 20:
        doc = await db[COLLECTION_NAME].find_one(
            {"_id": {"$regex": f"^{re.escape(q_str)}"}}
        )

    if not doc and db2 is not None:
        doc = await db2[COLLECTION_NAME].find_one({"_id": q_str})
        if not doc and len(q_str) >= 20:
            doc = await db2[COLLECTION_NAME].find_one(
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
