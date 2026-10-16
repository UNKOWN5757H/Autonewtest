import logging
from typing import Any, Dict, List, Optional, Union

import motor.motor_asyncio

from info import DATABASE_URI, JOIN_REQS_DB, REQ_CHANNEL

logger = logging.getLogger(__name__)


class JoinReqs:
    def __init__(self):
        self.client = None
        self.db = None
        self.col = None
        uri = JOIN_REQS_DB or DATABASE_URI
        if uri:
            try:
                self.client = motor.motor_asyncio.AsyncIOMotorClient(
                    uri, serverSelectionTimeoutMS=5000
                )
                self.db = self.client["JoinReqs"]
                col_name = str(REQ_CHANNEL).strip() if REQ_CHANNEL else "join_reqs_default"
                self.col = self.db[col_name]
            except Exception as e:
                logger.error(f"JoinReqs MongoDB Error: {e}")

    def isActive(self) -> bool:
        return self.col is not None

    async def add_user(
        self,
        user_id: Union[int, str],
        first_name: Optional[str] = None,
        username: Optional[str] = None,
        date: Any = None,
    ) -> bool:
        if not self.isActive():
            return False
        try:
            uid = int(user_id)
            await self.col.update_one(
                {"_id": uid},
                {
                    "$set": {
                        "_id": uid,
                        "user_id": uid,
                        "first_name": str(first_name or ""),
                        "username": str(username or ""),
                        "date": date,
                    }
                },
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"JoinReqs add_user error: {e}")
            return False

    async def get_user(self, user_id: Union[int, str]) -> Optional[Dict[str, Any]]:
        if not self.isActive():
            return None
        try:
            uid = int(user_id)
            return await self.col.find_one(
                {"$or": [{"user_id": uid}, {"_id": uid}]}
            )
        except Exception:
            return None

    async def get_all_users(self) -> List[Dict[str, Any]]:
        if not self.isActive():
            return []
        try:
            return await self.col.find({}).to_list(length=None)
        except Exception:
            return []

    async def delete_user(self, user_id: Union[int, str]) -> bool:
        if not self.isActive():
            return False
        try:
            uid = int(user_id)
            res = await self.col.delete_many(
                {"$or": [{"user_id": uid}, {"_id": uid}]}
            )
            return res.deleted_count > 0
        except Exception:
            return False

    async def clear_all(self) -> int:
        if not self.isActive():
            return 0
        try:
            res = await self.col.delete_many({})
            return res.deleted_count
        except Exception:
            return 0

    async def delete_all_users(self) -> int:
        """Alias used by plugins/fsub.py for backward compatibility."""
        return await self.clear_all()

    async def total_requests(self) -> int:
        if not self.isActive():
            return 0
        try:
            return await self.col.count_documents({})
        except Exception:
            return 0

    async def get_all_users_count(self) -> int:
        """Alias used by plugins/fsub.py for backward compatibility."""
        return await self.total_requests()


join_reqs = JoinReqs()
