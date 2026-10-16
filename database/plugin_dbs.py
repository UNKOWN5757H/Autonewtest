import logging
import time
from typing import Optional, Union

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
        self.del_col = self.db["deletion_settings"]

    # ============================================================
    # 📢 PROMOTIONS HELPERS
    # ============================================================
    async def add_promo(self, text: str, url: str):
        await self.promo_col.update_one(
            {"url": str(url).strip()},
            {"$set": {"text": str(text).strip(), "url": str(url).strip()}},
            upsert=True,
        )

    async def del_promo(self, url: str) -> bool:
        target = str(url).strip()
        res = await self.promo_col.delete_many(
            {"$or": [{"url": target}, {"text": target}]}
        )
        return res.deleted_count > 0

    async def get_all_promos(self) -> list:
        return await self.promo_col.find({}).to_list(length=None)

    async def get_promos(self) -> list:
        """Alias used by plugins/pm_filter.py and plugins/broadcast.py."""
        return await self.get_all_promos()

    # ============================================================
    # 🚫 BAN MANAGEMENT HELPERS
    # ============================================================
    async def ban_user(self, user_id: Union[int, str]):
        uid = int(user_id)
        await self.ban_col.update_one(
            {"_id": uid},
            {"$set": {"_id": uid, "banned_at": int(time.time())}},
            upsert=True,
        )

    async def unban_user(self, user_id: Union[int, str]):
        uid = int(user_id)
        await self.ban_col.delete_one({"_id": uid})

    async def is_banned(self, user_id: Union[int, str]) -> bool:
        return bool(await self.ban_col.find_one({"_id": int(user_id)}))

    async def get_ban_count(self) -> int:
        return await self.ban_col.count_documents({})

    # ============================================================
    # 📱 FORCE SUBSCRIBE USER HELPERS
    # ============================================================
    async def add_fsub_user(self, user_id: Union[int, str]):
        uid = int(user_id)
        await self.fsub_col.update_one(
            {"_id": uid}, {"$set": {"_id": uid}}, upsert=True
        )

    async def get_fsub_users_count(self) -> int:
        return await self.fsub_col.count_documents({})

    async def get_fsub_count(self) -> int:
        """Alias for backward compatibility."""
        return await self.get_fsub_users_count()

    async def clear_fsub_users(self):
        await self.fsub_col.delete_many({})

    # ============================================================
    # ❤️ AUTO-REACTION STATUS HELPERS
    # ============================================================
    async def get_reaction_status(self) -> bool:
        doc = await self.react_col.find_one({"_id": "global_status"})
        if doc and "is_enabled" in doc:
            return bool(doc["is_enabled"])
        cfg = await self.react_col.find_one({"_id": "config"})
        if cfg and "enabled" in cfg:
            return bool(cfg["enabled"])
        return True

    async def set_reaction_status(self, status: bool):
        val = bool(status)
        await self.react_col.update_one(
            {"_id": "global_status"}, {"$set": {"is_enabled": val}}, upsert=True
        )
        await self.react_col.update_one(
            {"_id": "config"}, {"$set": {"enabled": val}}, upsert=True
        )

    # ============================================================
    # ⏳ AUTO-DELETE TIMER HELPERS (SYNCED WITH delete.py & pm_filter.py)
    # ============================================================
    async def set_buttondel(self, seconds: int):
        await self.del_col.update_one(
            {"_id": "timers"},
            {"$set": {"BUTTON_AUTO_DELETE": int(seconds)}},
            upsert=True,
        )

    async def get_buttondel(self) -> Optional[int]:
        doc = await self.del_col.find_one({"_id": "timers"})
        if doc and doc.get("BUTTON_AUTO_DELETE") is not None:
            return int(doc["BUTTON_AUTO_DELETE"])
        return None

    async def set_autodelete(self, seconds: int):
        await self.del_col.update_one(
            {"_id": "timers"},
            {"$set": {"FILE_AUTO_DELETE": int(seconds)}},
            upsert=True,
        )

    async def get_autodelete(self) -> Optional[int]:
        doc = await self.del_col.find_one({"_id": "timers"})
        if doc and doc.get("FILE_AUTO_DELETE") is not None:
            return int(doc["FILE_AUTO_DELETE"])
        return None

    # ============================================================
    # 👥 FORCE ADD HELPERS
    # ============================================================
    async def set_fa_settings(
        self, chat_id: Union[int, str], limit: int, mode: str
    ):
        await self.fa_col.update_one(
            {"_id": f"settings_{int(chat_id)}"},
            {"$set": {"limit": int(limit), "mode": str(mode)}},
            upsert=True,
        )

    async def get_fa_settings(self, chat_id: Union[int, str]) -> dict:
        doc = await self.fa_col.find_one({"_id": f"settings_{int(chat_id)}"})
        return doc if doc else {"limit": 0, "mode": "all"}

    async def track_fa_new_user(
        self, chat_id: Union[int, str], user_id: Union[int, str]
    ):
        await self.fa_col.update_one(
            {"_id": f"new_users_{int(chat_id)}"},
            {"$addToSet": {"users": int(user_id)}},
            upsert=True,
        )

    async def is_fa_new_user(
        self, chat_id: Union[int, str], user_id: Union[int, str]
    ) -> bool:
        return bool(
            await self.fa_col.find_one(
                {"_id": f"new_users_{int(chat_id)}", "users": int(user_id)}
            )
        )

    async def increment_fa_adds(
        self, chat_id: Union[int, str], user_id: Union[int, str], count: int
    ):
        now = int(time.time())
        cnt = max(0, int(count))
        if cnt <= 0:
            return
        await self.fa_col.update_one(
            {"_id": f"adds_{int(chat_id)}_{int(user_id)}"},
            {
                "$inc": {"total": cnt},
                "$push": {"history": {"$each": [now] * cnt}},
            },
            upsert=True,
        )
        await self.fa_col.update_one(
            {"_id": f"adds_{int(chat_id)}_{int(user_id)}"},
            {"$pull": {"history": {"$lt": now - 604800}}},
        )

    async def get_fa_user_adds(
        self,
        chat_id: Union[int, str],
        user_id: Union[int, str],
        time_limit: Optional[int] = None,
    ) -> int:
        doc = await self.fa_col.find_one(
            {"_id": f"adds_{int(chat_id)}_{int(user_id)}"}
        )
        if not doc:
            return 0
        if time_limit is None:
            return int(doc.get("total", 0))
        cutoff = int(time.time()) - int(time_limit)
        return len([ts for ts in doc.get("history", []) if ts >= cutoff])

    async def reset_fa_daily_adds(self, chat_id: Union[int, str]):
        await self.fa_col.update_many(
            {"_id": {"$regex": f"^adds_{int(chat_id)}_"}},
            {"$set": {"history": []}},
        )

    async def reset_fa_all_adds(self, chat_id: Union[int, str]):
        await self.fa_col.delete_many(
            {"_id": {"$regex": f"^adds_{int(chat_id)}_"}}
        )

    async def get_fa_top_adds(
        self, chat_id: Union[int, str], time_limit: Optional[int] = None
    ) -> list:
        cursor = self.fa_col.find({"_id": {"$regex": f"^adds_{int(chat_id)}_"}})
        scores, now = [], int(time.time())
        async for doc in cursor:
            try:
                uid = int(str(doc["_id"]).split("_")[-1])
            except (ValueError, IndexError):
                continue
            score = (
                len(
                    [
                        ts
                        for ts in doc.get("history", [])
                        if ts >= (now - int(time_limit))
                    ]
                )
                if time_limit
                else int(doc.get("total", 0))
            )
            if score > 0:
                scores.append((uid, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:10]


plugin_db = PluginDatabase()
