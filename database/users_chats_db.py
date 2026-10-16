import logging
from typing import Any, Dict, List, Optional, Union

import motor.motor_asyncio

import info
from info import (
    DATABASE_NAME,
    DATABASE_URI,
    IMDB,
    IMDB_TEMPLATE,
    MELCOW_NEW_USERS,
    P_TTI_SHOW_OFF,
    PROTECT_CONTENT,
    SINGLE_BUTTON,
    SPELL_CHECK_REPLY,
)

try:
    from info import MOVIE_UPDATE_NOTIFICATION
except ImportError:
    MOVIE_UPDATE_NOTIFICATION = True

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, uri: str, database_name: str):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.grp = self.db.groups
        self.bot_settings = self.db.bot_settings

    def new_user(self, id: int, name: str) -> Dict[str, Any]:
        return dict(
            id=int(id),
            name=str(name or "User"),
            ban_status=dict(is_banned=False, ban_reason=""),
        )

    def new_group(self, id: int, title: str) -> Dict[str, Any]:
        return dict(
            id=int(id),
            title=str(title or "Group"),
            chat_status=dict(is_disabled=False, reason=""),
        )

    async def add_user(self, id: Union[int, str], name: str = "User"):
        uid = int(id)
        await self.col.update_one(
            {"id": uid},
            {
                "$set": {"name": str(name or "User")},
                "$setOnInsert": {
                    "id": uid,
                    "ban_status": dict(is_banned=False, ban_reason=""),
                },
            },
            upsert=True,
        )

    async def is_user_exist(self, id: Union[int, str]) -> bool:
        return bool(await self.col.find_one({"id": int(id)}))

    async def total_users_count(self) -> int:
        return await self.col.count_documents({})

    async def get_all_users(self) -> List[Dict[str, Any]]:
        return await self.col.find({}).to_list(length=None)

    async def delete_user(self, user_id: Union[int, str]):
        uid = int(user_id)
        await self.col.delete_many({"$or": [{"id": uid}, {"user_id": uid}]})

    async def remove_ban(self, id: Union[int, str]):
        uid = int(id)
        await self.col.update_one(
            {"id": uid},
            {"$set": {"ban_status": dict(is_banned=False, ban_reason="")}},
        )

    async def ban_user(self, user_id: Union[int, str], ban_reason: str = "No Reason"):
        uid = int(user_id)
        await self.col.update_one(
            {"id": uid},
            {
                "$set": {
                    "ban_status": dict(is_banned=True, ban_reason=str(ban_reason))
                }
            },
            upsert=True,
        )

    async def get_ban_status(self, id: Union[int, str]) -> Dict[str, Any]:
        default = dict(is_banned=False, ban_reason="")
        user = await self.col.find_one({"id": int(id)})
        return user.get("ban_status", default) if user else default

    async def get_all_banned_users(self) -> List[Dict[str, Any]]:
        return await self.col.find({"ban_status.is_banned": True}).to_list(length=None)

    async def add_chat(self, chat: Union[int, str], title: str = "Group"):
        cid = int(chat)
        await self.grp.update_one(
            {"id": cid},
            {
                "$set": {"title": str(title or "Group")},
                "$setOnInsert": {
                    "id": cid,
                    "chat_status": dict(is_disabled=False, reason=""),
                },
            },
            upsert=True,
        )

    async def get_chat(self, chat: Union[int, str]) -> Union[Dict[str, Any], bool]:
        chat_doc = await self.grp.find_one({"id": int(chat)})
        if not chat_doc:
            return False
        status = dict(chat_doc.get("chat_status") or {})
        if "title" not in status and chat_doc.get("title"):
            status["title"] = chat_doc["title"]
        if "id" not in status:
            status["id"] = chat_doc.get("id", int(chat))
        return status

    async def re_enable_chat(self, id: Union[int, str]):
        cid = int(id)
        await self.grp.update_one(
            {"id": cid},
            {"$set": {"chat_status": dict(is_disabled=False, reason="")}},
            upsert=True,
        )

    async def disable_chat(
        self, chat: Union[int, str], reason: str = "No Reason"
    ):
        cid = int(chat)
        await self.grp.update_one(
            {"id": cid},
            {"$set": {"chat_status": dict(is_disabled=True, reason=str(reason))}},
            upsert=True,
        )

    async def delete_chat(self, chat_id: Union[int, str]) -> bool:
        cid = int(chat_id)
        res = await self.grp.delete_many(
            {"$or": [{"id": cid}, {"chat_id": cid}, {"_id": cid}]}
        )
        return res.deleted_count > 0

    async def total_chat_count(self) -> int:
        return await self.grp.count_documents({})

    async def get_all_chats(self) -> List[Dict[str, Any]]:
        chats = await self.grp.find({}).to_list(length=None)
        for c in chats:
            chat_status = c.get("chat_status") or {}
            if "is_disabled" not in c and "is_disabled" in chat_status:
                c["is_disabled"] = chat_status["is_disabled"]
            if "reason" not in c and "reason" in chat_status:
                c["reason"] = chat_status["reason"]
        return chats

    async def update_settings(
        self, id: Union[int, str], settings: Dict[str, Any]
    ):
        cid = int(id)
        await self.grp.update_one(
            {"id": cid},
            {"$set": {"settings": settings}},
            upsert=True,
        )

    async def get_settings(self, id: Union[int, str]) -> Dict[str, Any]:
        default = {
            "button": SINGLE_BUTTON,
            "botpm": P_TTI_SHOW_OFF,
            "file_secure": PROTECT_CONTENT,
            "imdb": IMDB,
            "spell_check": SPELL_CHECK_REPLY,
            "welcome": MELCOW_NEW_USERS,
            "template": IMDB_TEMPLATE,
            "auto_delete": getattr(info, "AUTO_DELETE", True),
            "button_del": int(getattr(info, "BUTTON_AUTO_DELETE", 1800)),
        }
        chat = await self.grp.find_one({"id": int(id)})
        if chat and isinstance(chat.get("settings"), dict):
            return {**default, **chat["settings"]}
        return default

    async def get_db_size(self) -> int:
        try:
            return (await self.db.command("dbstats")).get("dataSize", 0)
        except Exception:
            return 0

    async def get_bot_setting(
        self, bot_id: Union[int, str], setting_name: str, default: Any = None
    ) -> Any:
        bot_doc = await self.bot_settings.find_one({"id": int(bot_id)})
        return bot_doc.get(setting_name, default) if bot_doc else default

    async def update_bot_setting(
        self, bot_id: Union[int, str], setting_name: str, value: Any
    ):
        await self.bot_settings.update_one(
            {"id": int(bot_id)}, {"$set": {setting_name: value}}, upsert=True
        )

    async def update_movie_update_status(
        self, bot_id: Union[int, str], enable: bool
    ):
        await self.update_bot_setting(bot_id, "MOVIE_UPDATE_NOTIFICATION", enable)


db = Database(DATABASE_URI, DATABASE_NAME)
