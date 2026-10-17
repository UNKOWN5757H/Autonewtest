import base64
import logging
import re
import time
from struct import pack, unpack
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
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

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

# ============================================================
# 🗄️ 3-DATABASE FAULT-TOLERANT SETUP (FAST TIMEOUTS & FAILOVER)
# ============================================================
_MONGO_KWARGS = {
    "serverSelectionTimeoutMS": 4000,
    "connectTimeoutMS": 4000,
    "socketTimeoutMS": 8000,
    "retryWrites": True,
}

SECOND_DATABASE_URI = getattr(info, "SECOND_DATABASE_URI", "") or ""
THIRD_DATABASE_URI = getattr(info, "THIRD_DATABASE_URI", "") or ""

SECOND_DATABASE_NAME = getattr(info, "SECOND_DATABASE_NAME", DATABASE_NAME) or DATABASE_NAME
THIRD_DATABASE_NAME = getattr(info, "THIRD_DATABASE_NAME", DATABASE_NAME) or DATABASE_NAME

db = AsyncIOMotorClient(DATABASE_URI, **_MONGO_KWARGS)[DATABASE_NAME]
instance = MotorAsyncIOInstance(db)

db2 = (
    AsyncIOMotorClient(SECOND_DATABASE_URI, **_MONGO_KWARGS)[SECOND_DATABASE_NAME]
    if SECOND_DATABASE_URI and SECOND_DATABASE_URI != DATABASE_URI
    else None
)

db3 = (
    AsyncIOMotorClient(THIRD_DATABASE_URI, **_MONGO_KWARGS)[THIRD_DATABASE_NAME]
    if THIRD_DATABASE_URI
    and THIRD_DATABASE_URI not in [DATABASE_URI, SECOND_DATABASE_URI]
    else None
)

# Circuit-breaker trackers so a broken/full DB never freezes the bot or spams logs
_DEAD_READ_DBS: Dict[int, float] = {}   # db_idx -> cooldown_until_ts (for bad auth / paused pool)
_QUOTA_FULL_DBS: Dict[int, float] = {}  # db_idx -> cooldown_until_ts (for 512MB quota full)

ACTIVE_DB_INDEX = 1  # 1=db, 2=db2, 3=db3
KNOWN_TITLES = set()


def get_writable_System_db():
    """
    Returns the first healthy, non-full database handle for plugins (fsub, multibot, etc.)
    when the primary 512MB cluster is full!
    """
    now = time.time()
    for idx, target in ((1, db), (2, db2), (3, db3)):
        if target is None:
            continue
        if now < _DEAD_READ_DBS.get(idx, 0.0):
            continue
        if now < _QUOTA_FULL_DBS.get(idx, 0.0):
            continue
        return target
    # Fallback to db2 or db3 if db1 is full
    for idx, target in ((2, db2), (3, db3), (1, db)):
        if target is not None and now >= _DEAD_READ_DBS.get(idx, 0.0):
            return target
    return db


def get_all_healthy_read_dbs() -> List[Tuple[int, Any]]:
    now = time.time()
    healthy = []
    for idx, target in ((1, db), (2, db2), (3, db3)):
        if target is None:
            continue
        if now < _DEAD_READ_DBS.get(idx, 0.0):
            continue
        healthy.append((idx, target))
    return healthy


def _is_auth_or_conn_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "bad auth" in msg
        or "authentication failed" in msg
        or "connection pool paused" in msg
        or "serverselectiontimeout" in msg
        or "timed out" in msg
        or "dnspython" in msg
        or "configurationerror" in msg
    )


def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "space quota" in msg or "over your space" in msg


def mark_db_error(idx: int, exc: Exception):
    now = time.time()
    if _is_quota_error(exc):
        # Mute writes to this full DB for 30 minutes
        _QUOTA_FULL_DBS[idx] = now + 1800
    elif _is_auth_or_conn_error(exc):
        # Mute reads & writes to this broken-auth DB for 30 minutes so searches take 0ms
        _DEAD_READ_DBS[idx] = now + 1800


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


def add_to_vocab(file_name: str):
    if not process or not file_name:
        return
    clean = re.sub(
        r"\b(mkv|mp4|avi|1080p|720p|480p|2160p|bluray|x264|hevc|web-dl|hdrip)\b",
        "",
        file_name,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"[_\-\.\+\[\]\(\)]", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    if clean and len(KNOWN_TITLES) < 5000:
        KNOWN_TITLES.add(clean)


def encode_file_id(s_pack: str, file_ref: str) -> str:
    """Reconstructs valid Pyrogram V2 File IDs from ultra-compact `_id` + `file_ref` chunks."""
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
    except Exception:
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

    def __init__(self, data: dict):
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
    Extreme 512MB MongoDB Quota Optimizer:
    1. Drops heavy text indexes.
    2. Deletes temporary cache collections (join requests, resume checkpoints, etc.).
    3. Unsets unused keys to shrink DB by ~60%.
    """
    global ACTIVE_DB_INDEX
    report = {
        "dropped_indexes": [],
        "cleared_temp_docs": 0,
        "compacted_files": 0,
    }

    for idx, target_db in ((1, db), (2, db2), (3, db3)):
        if target_db is None or time.time() < _DEAD_READ_DBS.get(idx, 0.0):
            continue

        try:
            indexes = await target_db[COLLECTION_NAME].index_information()
            for idx_name in list(indexes.keys()):
                if idx_name != "_id_":
                    try:
                        await target_db[COLLECTION_NAME].drop_index(idx_name)
                        report["dropped_indexes"].append(f"DB{idx}:{idx_name}")
                    except Exception:
                        pass
        except Exception as e:
            mark_db_error(idx, e)

        for temp_col in (
            "index_resume",
            "index_checkpoints",
            "broadcast_tasks",
            "countdown_tasks",
            "fsub_join_requests",
            "fsub_verified_users",
            "fsub_users",
        ):
            try:
                res = await target_db[temp_col].delete_many({})
                report["cleared_temp_docs"] += getattr(res, "deleted_count", 0)
            except Exception:
                pass

        try:
            unset_fields = {"file_id_raw": "", "mime_type": ""}
            if not USE_CAPTION_FILTER:
                unset_fields["caption"] = ""
            res_upd = await target_db[COLLECTION_NAME].update_many(
                {
                    "$or": [
                        {"file_id_raw": {"$exists": True}},
                        {"mime_type": {"$exists": True}},
                    ]
                },
                {"$unset": unset_fields},
            )
            report["compacted_files"] += getattr(res_upd, "modified_count", 0)
        except Exception:
            pass

    _QUOTA_FULL_DBS.clear()
    ACTIVE_DB_INDEX = 1
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
    """
    Searches seamlessly across all healthy databases.
    If DB2 or DB3 has `bad auth` or `connection pool paused`, it is automatically
    skipped so DB1 results are returned in <50ms without any error!
    """
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
    files = []

    for idx, target_db in get_all_healthy_read_dbs():
        if len(files) >= fetch_limit:
            break
        rem = fetch_limit - len(files)
        skip_amt = max(0, offset - len(files)) if idx > 1 else offset
        try:
            docs = (
                await target_db[COLLECTION_NAME]
                .find(mongo_query)
                .sort("_id", -1)
                .skip(skip_amt)
                .limit(rem)
                .to_list(length=rem)
            )
            if docs:
                files.extend(docs)
        except Exception as e:
            mark_db_error(idx, e)
            continue

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
    """Builds ultra-compact media document (~65% smaller) to save quota."""
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
    """Saves a single file, automatically jumping to DB2 or DB3 if quota is full or auth fails."""
    global ACTIVE_DB_INDEX
    doc = _build_compact_media_doc(media)
    if not doc:
        return False, 2

    now = time.time()
    for i in (1, 2, 3):
        if i < ACTIVE_DB_INDEX:
            continue
        target_db = db if i == 1 else (db2 if i == 2 else db3)
        if target_db is None:
            continue
        if now < _DEAD_READ_DBS.get(i, 0.0) or now < _QUOTA_FULL_DBS.get(i, 0.0):
            continue

        try:
            await target_db[COLLECTION_NAME].insert_one(doc)
            ACTIVE_DB_INDEX = i
            search_cache.clear()
            wrapped = SafeMediaWrapper(doc)
            file_details_cache.set(str(doc["_id"]), [wrapped])
            return True, 1
        except DuplicateKeyError:
            return False, 0
        except Exception as e:
            mark_db_error(i, e)
            if _is_quota_error(e) or _is_auth_or_conn_error(e):
                ACTIVE_DB_INDEX = i + 1
                continue
            return False, 2

    return False, 8000  # Code 8000 = All available Databases are Full or Unreachable!


async def save_batch(media_list) -> Tuple[int, int, int]:
    """Saves a batch of media, automatically jumping to DB2 or DB3 if quota is full."""
    global ACTIVE_DB_INDEX
    if not media_list:
        return 0, 0, 0

    documents = []
    for media in media_list:
        doc = _build_compact_media_doc(media)
        if doc:
            documents.append(doc)

    if not documents:
        return 0, 0, len(media_list)

    now = time.time()
    for i in (1, 2, 3):
        if i < ACTIVE_DB_INDEX:
            continue
        target_db = db if i == 1 else (db2 if i == 2 else db3)
        if target_db is None:
            continue
        if now < _DEAD_READ_DBS.get(i, 0.0) or now < _QUOTA_FULL_DBS.get(i, 0.0):
            continue

        try:
            res = await target_db[COLLECTION_NAME].insert_many(documents, ordered=False)
            ACTIVE_DB_INDEX = i
            search_cache.clear()
            return len(res.inserted_ids), 0, 0
        except BulkWriteError as bwe:
            search_cache.clear()
            write_errors = bwe.details.get("writeErrors", [])

            is_quota = any(
                "space quota" in str(err.get("errmsg", "")).lower()
                or "over your space" in str(err.get("errmsg", "")).lower()
                for err in write_errors
            )

            if is_quota:
                _QUOTA_FULL_DBS[i] = now + 1800
                ACTIVE_DB_INDEX = i + 1
                continue

            inserted = bwe.details.get("nInserted", 0)
            duplicates = sum(1 for err in write_errors if err.get("code") == 11000)
            return inserted, duplicates, len(media_list) - inserted - duplicates
        except Exception as e:
            mark_db_error(i, e)
            if _is_quota_error(e) or _is_auth_or_conn_error(e):
                ACTIVE_DB_INDEX = i + 1
                continue
            return 0, 0, len(media_list)

    raise OperationFailure("ALL AVAILABLE MONGODB CLUSTERS ARE FULL (Code 8000).", code=8000)


async def get_file_details(query):
    """Instant RAM-cached lookup across all healthy databases."""
    q_str = str(query).strip()
    if cached := file_details_cache.get(q_str):
        return cached

    for idx, target_db in get_all_healthy_read_dbs():
        try:
            doc = await target_db[COLLECTION_NAME].find_one({"_id": q_str})
            if not doc and len(q_str) >= 20:
                doc = await target_db[COLLECTION_NAME].find_one(
                    {"_id": {"$regex": f"^{re.escape(q_str)}"}}
                )
            if doc:
                wrapped = [SafeMediaWrapper(doc)]
                file_details_cache.set(q_str, wrapped)
                return wrapped
        except Exception as e:
            mark_db_error(idx, e)
            continue

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
