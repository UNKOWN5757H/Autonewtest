from motor.motor_asyncio import AsyncIOMotorClient

from info import DATABASE_NAME, DATABASE_URI

mycol = AsyncIOMotorClient(DATABASE_URI)[DATABASE_NAME]["CONNECTION"]


async def add_connection(group_id, user_id):
    query = await mycol.find_one({"_id": user_id})
    if query and group_id in [x["group_id"] for x in query.get("group_details", [])]:
        return False
    if not query:
        await mycol.insert_one(
            {
                "_id": user_id,
                "group_details": [{"group_id": group_id}],
                "active_group": group_id,
            }
        )
    else:
        await mycol.update_one(
            {"_id": user_id},
            {
                "$push": {"group_details": {"group_id": group_id}},
                "$set": {"active_group": group_id},
            },
        )
    return True


async def active_connection(user_id):
    query = await mycol.find_one({"_id": user_id})
    return int(query["active_group"]) if query and query.get("active_group") else None


async def all_connections(user_id):
    query = await mycol.find_one({"_id": user_id})
    return [x["group_id"] for x in query.get("group_details", [])] if query else None


async def if_active(user_id, group_id):
    query = await mycol.find_one({"_id": user_id})
    return query is not None and query.get("active_group") == group_id


async def make_active(user_id, group_id):
    return (
        await mycol.update_one({"_id": user_id}, {"$set": {"active_group": group_id}})
    ).modified_count != 0


async def make_inactive(user_id):
    return (
        await mycol.update_one({"_id": user_id}, {"$set": {"active_group": None}})
    ).modified_count != 0


async def delete_connection(user_id, group_id):
    update = await mycol.update_one(
        {"_id": user_id}, {"$pull": {"group_details": {"group_id": group_id}}}
    )
    if update.modified_count == 0:
        return False
    query = await mycol.find_one({"_id": user_id})
    if query and query.get("group_details"):
        if query.get("active_group") == group_id:
            await mycol.update_one(
                {"_id": user_id},
                {"$set": {"active_group": query["group_details"][-1]["group_id"]}},
            )
    else:
        await mycol.update_one({"_id": user_id}, {"$set": {"active_group": None}})
    return True
