import motor.motor_asyncio

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


class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self.grp = self.db.groups
        self.bot_settings = self.db.bot_settings

    def new_user(self, id, name):
        return dict(id=id, name=name, ban_status=dict(is_banned=False, ban_reason=""))

    def new_group(self, id, title):
        return dict(id=id, title=title, chat_status=dict(is_disabled=False, reason=""))

    async def add_user(self, id, name):
        await self.col.insert_one(self.new_user(id, name))

    async def is_user_exist(self, id):
        return bool(await self.col.find_one({"id": int(id)}))

    async def total_users_count(self):
        return await self.col.count_documents({})

    async def get_all_users(self):
        return await self.col.find({}).to_list(length=None)

    async def delete_user(self, user_id):
        await self.col.delete_many({"id": int(user_id)})

    async def add_chat(self, chat, title):
        await self.grp.insert_one(self.new_group(chat, title))

    async def get_chat(self, chat):
        chat_doc = await self.grp.find_one({"id": int(chat)})
        return chat_doc.get("chat_status") if chat_doc else False

    async def re_enable_chat(self, id):
        await self.grp.update_one(
            {"id": int(id)},
            {"$set": {"chat_status": dict(is_disabled=False, reason="")}},
        )

    async def disable_chat(self, chat, reason="No Reason"):
        await self.grp.update_one(
            {"id": int(chat)},
            {"$set": {"chat_status": dict(is_disabled=True, reason=reason)}},
        )

    async def total_chat_count(self):
        return await self.grp.count_documents({})

    async def get_all_chats(self):
        return await self.grp.find({}).to_list(length=None)

    async def update_settings(self, id, settings):
        await self.grp.update_one({"id": int(id)}, {"$set": {"settings": settings}})

    async def get_settings(self, id):
        default = {
            "button": SINGLE_BUTTON,
            "botpm": P_TTI_SHOW_OFF,
            "file_secure": PROTECT_CONTENT,
            "imdb": IMDB,
            "spell_check": SPELL_CHECK_REPLY,
            "welcome": MELCOW_NEW_USERS,
            "template": IMDB_TEMPLATE,
        }
        chat = await self.grp.find_one({"id": int(id)})
        return chat.get("settings", default) if chat else default

    async def get_db_size(self):
        return (await self.db.command("dbstats")).get("dataSize", 0)

    async def get_bot_setting(self, bot_id, setting_name, default):
        bot_doc = await self.bot_settings.find_one({"id": int(bot_id)})
        return bot_doc.get(setting_name, default) if bot_doc else default

    async def update_bot_setting(self, bot_id, setting_name, value):
        await self.bot_settings.update_one(
            {"id": int(bot_id)}, {"$set": {setting_name: value}}, upsert=True
        )

    async def update_movie_update_status(self, bot_id, enable):
        await self.update_bot_setting(bot_id, "MOVIE_UPDATE_NOTIFICATION", enable)


db = Database(DATABASE_URI, DATABASE_NAME)
