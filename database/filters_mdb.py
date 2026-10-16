import logging
import re

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import enums

from info import DATABASE_NAME, DATABASE_URI

logger = logging.getLogger(__name__)
mydb = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]


async def add_filter(grp_id, text, reply_text, btn, file, alert="[]"):
    keyword = str(text).lower().strip()
    await mydb[str(grp_id)].update_one(
        {"text": keyword},
        {
            "$set": {
                "text": keyword,
                "reply": str(reply_text or ""),
                "btn": str(btn or "[]"),
                "file": str(file or "None"),
                "alert": str(alert or "[]"),
            }
        },
        upsert=True,
    )


async def find_filter(group_id, name):
    keyword = str(name).lower().strip()
    doc = await mydb[str(group_id)].find_one({"text": keyword})
    if not doc:
        doc = await mydb[str(group_id)].find_one(
            {"text": {"$regex": f"^{re.escape(keyword)}$", "$options": "i"}}
        )
    if doc:
        return doc.get("reply"), doc.get("btn"), doc.get("alert"), doc.get("file")
    return None, None, None, None


async def get_filters(group_id):
    try:
        return [
            doc.get("text")
            async for doc in mydb[str(group_id)].find({}, {"text": 1})
            if doc.get("text")
        ]
    except Exception as e:
        logger.error(f"get_filters error for {group_id}: {e}")
        return []


async def delete_filter(message, text, group_id) -> bool:
    """Deletes a filter from MongoDB without sending duplicate chat replies (handled by pm_filter.py)."""
    keyword = str(text).lower().strip()
    res = await mydb[str(group_id)].delete_many(
        {
            "$or": [
                {"text": keyword},
                {"text": {"$regex": f"^{re.escape(keyword)}$", "$options": "i"}},
            ]
        }
    )
    return res.deleted_count > 0


async def del_all(message, group_id, title):
    if str(group_id) not in await mydb.list_collection_names():
        if message:
            return await message.edit_text(
                f"Nothing to remove in {title}!",
                parse_mode=enums.ParseMode.HTML,
            )
        return
    try:
        await mydb[str(group_id)].drop()
        if message:
            await message.edit_text(
                f"All filters from {title} have been removed.",
                parse_mode=enums.ParseMode.HTML,
            )
    except Exception:
        if message:
            await message.edit_text(
                "Couldn't remove all filters from group!",
                parse_mode=enums.ParseMode.HTML,
            )
