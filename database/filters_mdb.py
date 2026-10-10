import logging

from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import enums

from info import DATABASE_NAME, DATABASE_URI

logger = logging.getLogger(__name__)
mydb = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]


async def add_filter(grp_id, text, reply_text, btn, file, alert):
    await mydb[str(grp_id)].update_one(
        {"text": str(text)},
        {
            "$set": {
                "text": str(text),
                "reply": str(reply_text),
                "btn": str(btn),
                "file": str(file),
                "alert": str(alert),
            }
        },
        upsert=True,
    )


async def find_filter(group_id, name):
    async for file in mydb[str(group_id)].find({"text": name}):
        return file.get("reply"), file.get("btn"), file.get("alert"), file.get("file")
    return None, None, None, None


async def get_filters(group_id):
    return [file.get("text") async for file in mydb[str(group_id)].find()]


async def delete_filter(message, text, group_id):
    if (await mydb[str(group_id)].delete_one({"text": text})).deleted_count >= 1:
        await message.reply_text(
            f"'`{text}`' deleted.", quote=True, parse_mode=enums.ParseMode.MARKDOWN
        )
    else:
        await message.reply_text("Couldn't find that filter!", quote=True)


async def del_all(message, group_id, title):
    if str(group_id) not in await mydb.list_collection_names():
        return await message.edit_text(f"Nothing to remove in {title}!")
    try:
        await mydb[str(group_id)].drop()
        await message.edit_text(f"All filters from {title} have been removed")
    except Exception:
        await message.edit_text("Couldn't remove all filters from group!")
