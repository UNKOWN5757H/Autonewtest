import logging
import time

from motor.motor_asyncio import AsyncIOMotorClient

from info import DATABASE_NAME, DATABASE_URI

logger = logging.getLogger(__name__)


class PluginDatabase:
    def __init__(self):
        self.client = AsyncIOMotorClient(DATABASE_URI)
        self.db = self.client[DATABASE_NAME]
        self.promo_col = self.db["promotions"]
        self.ban_col = self.db["banned_users"]
        self.fsub_col = self.db["fsub_users"]
        self.react_col = self.db["reaction_settings"]
        self.fa_col = self.db["forceadd_data"]

    async def add_promo(self, text: str, url: str):
        await self.promo_col.update_one(
            {"url": url}, {"$set": {"text": text, "url": url}}, upsert=True
        )

    async def del_promo(self, url: str) -> bool:
        return (await self.promo_col.delete_one({"url": url})).deleted_count > 0

    async def get_all_promos(self) -> list:
        return await self.promo_col.find({}).to_list(length=None)

    async def ban_user(self, user_id: int):
        await self.ban_col.update_one(
            {"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True
        )

    async def unban_user(self, user_id: int):
        await self.ban_col.delete_one({"_id": user_id})

    async def is_banned(self, user_id: int) -> bool:
        return bool(await self.ban_col.find_one({"_id": user_id}))

    async def get_ban_count(self) -> int:
        return await self.ban_col.count_documents({})

    async def add_fsub_user(self, user_id: int):
        await self.fsub_col.update_one(
            {"_id": user_id}, {"$set": {"_id": user_id}}, upsert=True
        )

    async def get_fsub_count(self) -> int:
        return await self.fsub_col.count_documents({})

    async def clear_fsub_users(self):
        await self.fsub_col.delete_many({})

    async def get_reaction_status(self) -> bool:
        doc = await self.react_col.find_one({"_id": "global_status"})
        return doc.get("is_enabled", True) if doc else True

    async def set_reaction_status(self, status: bool):
        await self.react_col.update_one(
            {"_id": "global_status"}, {"$set": {"is_enabled": status}}, upsert=True
        )

    async def set_fa_settings(self, chat_id: int, limit: int, mode: str):
        await self.fa_col.update_one(
            {"_id": f"settings_{chat_id}"},
            {"$set": {"limit": limit, "mode": mode}},
            upsert=True,
        )

    async def get_fa_settings(self, chat_id: int) -> dict:
        doc = await self.fa_col.find_one({"_id": f"settings_{chat_id}"})
        return doc if doc else {"limit": 0, "mode": "all"}

    async def track_fa_new_user(self, chat_id: int, user_id: int):
        await self.fa_col.update_one(
            {"_id": f"new_users_{chat_id}"},
            {"$addToSet": {"users": user_id}},
            upsert=True,
        )

    async def is_fa_new_user(self, chat_id: int, user_id: int) -> bool:
        return bool(
            await self.fa_col.find_one(
                {"_id": f"new_users_{chat_id}", "users": user_id}
            )
        )

    async def increment_fa_adds(self, chat_id: int, user_id: int, count: int):
        now = int(time.time())
        await self.fa_col.update_one(
            {"_id": f"adds_{chat_id}_{user_id}"},
            {"$inc": {"total": count}, "$push": {"history": {"$each": [now] * count}}},
            upsert=True,
        )
        await self.fa_col.update_one(
            {"_id": f"adds_{chat_id}_{user_id}"},
            {"$pull": {"history": {"$lt": now - 604800}}},
        )

    async def get_fa_user_adds(
        self, chat_id: int, user_id: int, time_limit: int = None
    ) -> int:
        doc = await self.fa_col.find_one({"_id": f"adds_{chat_id}_{user_id}"})
        if not doc:
            return 0
        if time_limit is None:
            return doc.get("total", 0)
        return len(
            [
                ts
                for ts in doc.get("history", [])
                if ts >= (int(time.time()) - time_limit)
            ]
        )

    async def reset_fa_daily_adds(self, chat_id: int):
        await self.fa_col.update_many(
            {"_id": {"$regex": f"^adds_{chat_id}_"}}, {"$set": {"history": []}}
        )

    async def reset_fa_all_adds(self, chat_id: int):
        await self.fa_col.delete_many({"_id": {"$regex": f"^adds_{chat_id}_"}})

    async def get_fa_top_adds(self, chat_id: int, time_limit: int = None) -> list:
        cursor = self.fa_col.find({"_id": {"$regex": f"^adds_{chat_id}_"}})
        scores, now = [], int(time.time())
        async for doc in cursor:
            uid = int(doc["_id"].split("_")[-1])
            score = (
                len([ts for ts in doc.get("history", []) if ts >= (now - time_limit)])
                if time_limit
                else doc.get("total", 0)
            )
            if score > 0:
                scores.append((uid, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:10]


plugin_db = PluginDatabase()
