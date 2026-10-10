import logging

import motor.motor_asyncio
from pymongo.errors import DuplicateKeyError

from info import JOIN_REQS_DB, REQ_CHANNEL

logger = logging.getLogger(__name__)


class JoinReqs:
    def __init__(self):
        self.client, self.db, self.col = None, None, None
        if JOIN_REQS_DB:
            try:
                self.client = motor.motor_asyncio.AsyncIOMotorClient(
                    JOIN_REQS_DB, serverSelectionTimeoutMS=5000
                )
                self.db = self.client["JoinReqs"]
                self.col = self.db[
                    str(REQ_CHANNEL) if REQ_CHANNEL else "join_reqs_default"
                ]
            except Exception as e:
                logger.error(f"MongoDB Error: {e}")

    def isActive(self):
        return self.col is not None

    async def add_user(self, user_id, first_name=None, username=None, date=None):
        if not self.isActive():
            return False
        try:
            await self.col.insert_one(
                {
                    "_id": int(user_id),
                    "user_id": int(user_id),
                    "first_name": first_name or "",
                    "username": username or "",
                    "date": date,
                }
            )
            return True
        except DuplicateKeyError:
            return False

    async def get_user(self, user_id):
        return (
            await self.col.find_one({"user_id": int(user_id)})
            if self.isActive()
            else None
        )

    async def delete_user(self, user_id):
        if not self.isActive():
            return False
        return (await self.col.delete_one({"user_id": int(user_id)})).deleted_count > 0

    async def clear_all(self):
        return (await self.col.delete_many({})).deleted_count if self.isActive() else 0

    async def total_requests(self):
        return await self.col.count_documents({}) if self.isActive() else 0


join_reqs = JoinReqs()
