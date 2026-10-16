import logging
from typing import List, Optional, Union

from motor.motor_asyncio import AsyncIOMotorClient

from info import DATABASE_NAME, DATABASE_URI

logger = logging.getLogger(__name__)

_db = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]
mycol = _db["CONNECTION"]
active_col = _db["active_connections"]


async def _find_user_doc(user_id: Union[int, str]) -> Optional[dict]:
    uid_str = str(user_id).strip()
    doc = await mycol.find_one({"_id": uid_str})
    if not doc and uid_str.lstrip("-").isdigit():
        doc = await mycol.find_one({"_id": int(uid_str)})
    return doc


async def _sync_active_col(user_id: str, group_id: Optional[str]):
    try:
        if group_id:
            await active_col.update_one(
                {"user_id": str(user_id)},
                {"$set": {"group_id": str(group_id), "active_group": str(group_id)}},
                upsert=True,
            )
        else:
            await active_col.delete_one({"user_id": str(user_id)})
    except Exception as e:
        logger.error(f"active_connections sync error: {e}")


async def add_connection(group_id: Union[int, str], user_id: Union[int, str]) -> bool:
    uid_str = str(user_id).strip()
    gid_str = str(group_id).strip()

    query = await _find_user_doc(uid_str)
    doc_id = query["_id"] if query else uid_str

    existing_groups = [
        str(x.get("group_id"))
        for x in (query.get("group_details", []) if query else [])
        if isinstance(x, dict) and x.get("group_id") is not None
    ]

    if query and gid_str in existing_groups:
        await mycol.update_one({"_id": doc_id}, {"$set": {"active_group": gid_str}})
        await _sync_active_col(uid_str, gid_str)
        return True

    if not query:
        await mycol.insert_one(
            {
                "_id": uid_str,
                "group_details": [{"group_id": gid_str}],
                "active_group": gid_str,
            }
        )
    else:
        await mycol.update_one(
            {"_id": doc_id},
            {
                "$push": {"group_details": {"group_id": gid_str}},
                "$set": {"active_group": gid_str},
            },
        )

    await _sync_active_col(uid_str, gid_str)
    return True


async def active_connection(user_id: Union[int, str]) -> Optional[int]:
    uid_str = str(user_id).strip()
    query = await _find_user_doc(uid_str)
    if query and query.get("active_group"):
        try:
            return int(str(query["active_group"]).strip())
        except (ValueError, TypeError):
            pass

    try:
        act_doc = await active_col.find_one({"user_id": uid_str})
        if act_doc and act_doc.get("group_id"):
            return int(str(act_doc["group_id"]).strip())
    except Exception:
        pass
    return None


async def all_connections(user_id: Union[int, str]) -> Optional[List[str]]:
    uid_str = str(user_id).strip()
    query = await _find_user_doc(uid_str)
    if not query:
        return None
    details = query.get("group_details", [])
    groups = [
        str(x["group_id"])
        for x in details
        if isinstance(x, dict) and x.get("group_id") is not None
    ]
    return list(dict.fromkeys(groups)) if groups else None


async def if_active(user_id: Union[int, str], group_id: Union[int, str]) -> bool:
    uid_str = str(user_id).strip()
    gid_str = str(group_id).strip()
    query = await _find_user_doc(uid_str)
    if query and str(query.get("active_group", "")).strip() == gid_str:
        return True
    try:
        act_doc = await active_col.find_one({"user_id": uid_str})
        if act_doc and str(act_doc.get("group_id", "")).strip() == gid_str:
            return True
    except Exception:
        pass
    return False


async def make_active(user_id: Union[int, str], group_id: Union[int, str]) -> bool:
    uid_str = str(user_id).strip()
    gid_str = str(group_id).strip()
    query = await _find_user_doc(uid_str)
    doc_id = query["_id"] if query else uid_str

    await mycol.update_one(
        {"_id": doc_id}, {"$set": {"active_group": gid_str}}, upsert=True
    )
    await _sync_active_col(uid_str, gid_str)
    return True


async def make_inactive(user_id: Union[int, str]) -> bool:
    uid_str = str(user_id).strip()
    query = await _find_user_doc(uid_str)
    doc_id = query["_id"] if query else uid_str

    res = await mycol.update_one({"_id": doc_id}, {"$set": {"active_group": None}})
    await _sync_active_col(uid_str, None)
    return res.modified_count != 0


async def delete_connection(
    user_id: Union[int, str], group_id: Union[int, str]
) -> bool:
    uid_str = str(user_id).strip()
    gid_str = str(group_id).strip()
    query = await _find_user_doc(uid_str)
    if not query:
        return False

    doc_id = query["_id"]
    pull_targets = [{"group_id": gid_str}]
    if gid_str.lstrip("-").isdigit():
        pull_targets.append({"group_id": int(gid_str)})

    update = await mycol.update_one(
        {"_id": doc_id},
        {"$pull": {"group_details": {"$in": pull_targets}}},
    )

    if update.modified_count == 0:
        return False

    updated_doc = await mycol.find_one({"_id": doc_id})
    details = updated_doc.get("group_details", []) if updated_doc else []

    if details:
        if str(updated_doc.get("active_group", "")).strip() == gid_str:
            new_active = str(details[-1]["group_id"])
            await mycol.update_one(
                {"_id": doc_id}, {"$set": {"active_group": new_active}}
            )
            await _sync_active_col(uid_str, new_active)
    else:
        await mycol.update_one({"_id": doc_id}, {"$set": {"active_group": None}})
        await _sync_active_col(uid_str, None)

    return True
