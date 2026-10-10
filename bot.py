import asyncio
import glob
import os
import signal
import sys
from typing import AsyncGenerator, Union

# ===================================================================
# ⚡ 1. CREATE EVENT LOOP FIRST
# ===================================================================
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# ===================================================================
# 🚀 2. MONGODB CRASH FIX (Monkey Patch)
# ===================================================================
import motor.motor_asyncio
import pymongo

motor.motor_asyncio.AsyncIOMotorClient.handlers = []
motor.motor_asyncio.AsyncIOMotorDatabase.handlers = []
motor.motor_asyncio.AsyncIOMotorCollection.handlers = []
pymongo.mongo_client.MongoClient.handlers = []
pymongo.database.Database.handlers = []
pymongo.collection.Collection.handlers = []

# ===================================================================
from logging import ERROR, INFO, basicConfig, getLogger
from logging.config import fileConfig

from aiohttp import web
from pyrogram import Client, __version__, filters, idle, types
from pyrogram.raw.all import layer
from pyrogram.types import Message

from database.ia_filterdb import Media
from database.plugin_dbs import plugin_db
from database.users_chats_db import db as old_db
from info import API_HASH, API_ID, BOT_TOKEN, LOG_STR, PORT, SESSION
from utils import temp

# ============================================================
# ⚙️ SAFE LOGGING SETUP
# ============================================================
if os.path.exists("logging.conf"):
    fileConfig("logging.conf")
else:
    basicConfig(
        level=INFO,
        format="[%(asctime)s - %(levelname)s] - %(name)s - %(message)s",
        datefmt="%d-%b-%y %H:%M:%S",
    )

getLogger("pyrogram").setLevel(ERROR)
getLogger("pyrogram.session.session").setLevel(ERROR)
getLogger("imdbpy").setLevel(ERROR)
logger = getLogger(__name__)


# ============================================================
# 🤖 BOT CLASS
# ============================================================
class Bot(Client):
    def __init__(self):
        super().__init__(
            name=SESSION,
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=500,
            plugins={"root": "plugins"},
            sleep_threshold=5,
            in_memory=True,
        )

    async def start(self, *args, **kwargs):
        await super().start(*args, **kwargs)

        b_users = []
        b_chats = []
        try:

            async def fetch_bans():
                if hasattr(old_db, "grp"):
                    async for chat in old_db.grp.find(
                        {"chat_status.is_disabled": True}
                    ):
                        if chat.get("id"):
                            b_chats.append(chat["id"])

                async for user in plugin_db.ban_col.find({}):
                    if user.get("_id"):
                        b_users.append(user["_id"])

                if hasattr(old_db, "col"):
                    async for user in old_db.col.find({"ban_status.is_banned": True}):
                        u_id = user.get("id")
                        if u_id and u_id not in b_users:
                            b_users.append(u_id)

            await asyncio.wait_for(fetch_bans(), timeout=10.0)
        except Exception as e:
            logger.error(f"Failed to load bans (Timeout/Error): {e}")

        temp.BANNED_USERS = b_users
        temp.BANNED_CHATS = b_chats

        try:
            await Media.collection.drop_index("file_name_text")
        except Exception:
            pass

        try:
            await Media.ensure_indexes()
        except Exception as e:
            logger.error(f"Error ensuring DB indexes: {e}")

        me = await self.get_me()
        temp.ME = me.id
        temp.U_NAME = me.username
        temp.B_NAME = me.first_name
        self.username = f"@{me.username}"

        logger.info(
            f"{me.first_name} with Pyrogram v{__version__} (Layer {layer}) started on {me.username}."
        )
        logger.info(LOG_STR)

        if os.path.exists("restart.txt"):
            try:
                with open("restart.txt", "r") as f:
                    chat_id_str, msg_id_str = f.read().strip().split("\n")
                await self.edit_message_text(
                    chat_id=int(chat_id_str),
                    message_id=int(msg_id_str),
                    text="✅ **Bot Restarted Successfully!**",
                )
            except Exception:
                pass
            finally:
                if os.path.exists("restart.txt"):
                    os.remove("restart.txt")

    async def stop(self, *args, **kwargs):
        await super().stop(*args, **kwargs)
        logger.info("Bot stopped. Bye.")

    async def iter_messages(
        self, chat_id: Union[int, str], limit: int, offset: int = 0
    ) -> AsyncGenerator[types.Message, None]:
        current = offset
        while True:
            new_diff = min(200, limit - current)
            if new_diff <= 0:
                return
            messages = await self.get_messages(
                chat_id, list(range(current, current + new_diff + 1))
            )
            for message in messages:
                if not getattr(message, "empty", False):
                    yield message
                current += 1


app = Bot()

# ============================================================
# 🗑️ AUTO DELETE PM MEDIA
# ============================================================
AUTO_DELETE_TASKS = set()


async def delete_media_task(message: Message, delay: int):
    await asyncio.sleep(delay)
    try:
        if message:
            await message.delete()
    except Exception:
        pass


@app.on_message(
    filters.private
    & (
        filters.document
        | filters.video
        | filters.audio
        | filters.photo
        | filters.voice
        | filters.video_note
    ),
    group=2,
)
async def auto_delete_user_media_pm(client: Client, message: Message):
    user = message.from_user
    if not user or message.outgoing:
        return
    task = asyncio.create_task(delete_media_task(message, delay=1800))
    AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(AUTO_DELETE_TASKS.discard)


# ============================================================
# 🌐 HEALTH CHECKS & STARTUP
# ============================================================
async def health_check(request):
    return web.Response(text="Bot is running and healthy!")


async def start_services():
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()

    bind_port = int(PORT) if PORT else 8080
    site = web.TCPSite(runner, "0.0.0.0", bind_port)
    await site.start()
    logger.info(f"🌐 Web server listening on port {bind_port} for health checks.")

    await app.start()
    await idle()
    await app.stop()
    await runner.cleanup()


def force_shutdown(signum, frame):
    logger.info("🛑 Received shutdown signal. Killing instance immediately!")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, force_shutdown)
    signal.signal(signal.SIGINT, force_shutdown)
    try:
        loop.run_until_complete(start_services())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process interrupted. Shutting down...")
