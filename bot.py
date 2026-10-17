import asyncio
import builtins
import os
import signal
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple, Union

# ===================================================================
# 🛡️ 0. INJECT TYPING ALIASES INTO BUILTINS (Fixes NameError: Tuple)
# ===================================================================
for _type_name, _type_obj in (
    ("Tuple", Tuple),
    ("List", List),
    ("Dict", Dict),
    ("Optional", Optional),
    ("Union", Union),
    ("Set", Set),
    ("Any", Any),
):
    if not hasattr(builtins, _type_name):
        setattr(builtins, _type_name, _type_obj)

# ===================================================================
# ⚡ 1. CREATE EVENT LOOP FIRST (Prevents RuntimeErrors on Python 3.10+)
# ===================================================================
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# ===================================================================
# 🚀 2. MONGODB CRASH FIX (Monkey Patch for Hosters)
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
from pyrogram import Client, __version__, enums, filters, idle, types
from pyrogram.errors import FloodWait
from pyrogram.raw.all import layer
from pyrogram.types import Message

import info
from database.ia_filterdb import Media
from database.users_chats_db import db as old_db
from info import API_HASH, API_ID, BOT_TOKEN, LOG_STR, PORT, SESSION
from utils import temp

try:
    from database.ia_filterdb import get_all_healthy_read_dbs, mark_db_error
except ImportError:
    get_all_healthy_read_dbs = None
    mark_db_error = None

try:
    from database.plugin_dbs import plugin_db
except ImportError:
    plugin_db = None

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
# 👑 FULL POWER ADMIN CHECKER (GLOBAL + CHAT + ANONYMOUS)
# ============================================================
def get_admin_list() -> List[int]:
    raw_admins = getattr(info, "ADMINS", [])
    if isinstance(raw_admins, (str, int)):
        raw_admins = str(raw_admins).replace(",", " ").split()
    elif isinstance(raw_admins, (list, tuple, set)):
        raw_admins = list(raw_admins)
    else:
        raw_admins = []
    return [int(x) for x in raw_admins if str(x).strip().lstrip("-").isdigit()]


async def is_user_admin(client: Client, message: Message) -> bool:
    if not message:
        return False
    admin_list = get_admin_list()

    if message.from_user and message.from_user.id in admin_list:
        return True

    if message.sender_chat and message.sender_chat.id in admin_list:
        return True

    if message.chat and message.chat.type in [
        enums.ChatType.GROUP,
        enums.ChatType.SUPERGROUP,
    ]:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True

        if message.from_user:
            try:
                member = await client.get_chat_member(
                    message.chat.id, message.from_user.id
                )
                return member.status in [
                    enums.ChatMemberStatus.ADMINISTRATOR,
                    enums.ChatMemberStatus.OWNER,
                ]
            except Exception:
                pass

    return False


async def _find_config_doc_across_dbs(col_name: str, query_doc: dict) -> Optional[dict]:
    """Reads config documents across all healthy DBs (DB 1, DB 2, DB 3) so settings are never lost."""
    if callable(get_all_healthy_read_dbs):
        for idx, target_db in get_all_healthy_read_dbs():
            try:
                doc = await target_db[col_name].find_one(query_doc)
                if doc:
                    return doc
            except Exception as e:
                if callable(mark_db_error):
                    mark_db_error(idx, e)
                continue
    try:
        db_client = motor.motor_asyncio.AsyncIOMotorClient(
            info.DATABASE_URI,
            serverSelectionTimeoutMS=4000,
            connectTimeoutMS=4000,
            socketTimeoutMS=8000,
        )
        bot_db = db_client[info.DATABASE_NAME]
        return await bot_db[col_name].find_one(query_doc)
    except Exception:
        return None


async def _restore_persistent_settings():
    """Restores saved custom captions, UI settings, timers, and global images into `info` on bot startup."""
    try:
        # 1. Restore custom_captions collection
        cap_doc = await _find_config_doc_across_dbs(
            "custom_captions", {"_id": "settings"}
        )
        if cap_doc:
            for k, v in cap_doc.items():
                if k != "_id" and v is not None:
                    setattr(info, k, v)

        # 2. Restore global_bot_settings collection
        global_doc = await _find_config_doc_across_dbs(
            "global_bot_settings", {"_id": "bot_config"}
        )
        if global_doc:
            attr_map = {
                "fsub_img": "FSUB_IMG",
                "fsub_msg": "FSUB_MSG",
                "auto_img": "AUTO_IMG",
                "not_found_img": "NOT_FOUND_IMG",
                "not_found_text": "NOT_FOUND_MSG",
                "info_msg": "INFO_MSG",
                "info_img": "INFO_IMG",
                "del_msg": "DEL_MSG",
                "del_img": "DEL_IMG",
            }
            for gk, attr in attr_map.items():
                if gk in global_doc and global_doc[gk] is not None:
                    setattr(info, attr, global_doc[gk])

        # 3. Restore deletion_settings timers (Both PM & Group Button Auto-Delete)
        del_doc = await _find_config_doc_across_dbs(
            "deletion_settings", {"_id": "timers"}
        )
        if del_doc:
            if (
                "FILE_AUTO_DELETE" in del_doc
                and del_doc["FILE_AUTO_DELETE"] is not None
            ):
                val = int(del_doc["FILE_AUTO_DELETE"])
                setattr(info, "FILE_AUTO_DELETE", val)
                setattr(info, "AUTO_DELETE_TIME", val)
                setattr(info, "AUTO_DELETE", val > 0)
                setattr(temp, "FILE_AUTO_DELETE", val)
            if (
                "BUTTON_AUTO_DELETE" in del_doc
                and del_doc["BUTTON_AUTO_DELETE"] is not None
            ):
                b_val = int(del_doc["BUTTON_AUTO_DELETE"])
                setattr(info, "BUTTON_AUTO_DELETE", b_val)
                setattr(info, "BUTTON_DEL", b_val)
                setattr(temp, "BUTTON_AUTO_DELETE", b_val)
                setattr(temp, "BUTTON_DEL", b_val)

        # 4. Restore reaction_settings
        react_doc = await _find_config_doc_across_dbs(
            "reaction_settings", {"_id": "config"}
        )
        if react_doc and "enabled" in react_doc:
            setattr(info, "ENABLE_REACTION", bool(react_doc["enabled"]))

        # 5. Restore UI customizations from editable.py
        try:
            from plugins.editable import get_ui

            await get_ui(force_refresh=True)
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Failed to restore persistent settings on boot: {e}")


async def _boot_multibot_workers():
    """Loads saved Multi-Bot workers across all DBs and starts them in the background on boot."""
    try:
        from plugins.multibot import ensure_all_workers_started, load_multibot_config

        cfg = await load_multibot_config(force_refresh=True)
        bots_count = len(cfg.get("bots", []))
        if cfg.get("enabled", True) and bots_count > 0:
            started = await ensure_all_workers_started()
            logger.info(
                f"🤖 Multi-Bot Auto-Boot: {len(started)}/{bots_count} Worker Bot(s) Online and Ready!"
            )
    except Exception as e:
        logger.error(f"Multi-Bot auto-boot warning: {e}")


# ============================================================
# 🤖 BOT CLASS INITIALIZATION
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
            sleep_threshold=10,
            in_memory=True,
        )

    async def start(self, *args, **kwargs):
        await super().start(*args, **kwargs)

        b_users = []
        b_chats = []
        try:

            async def fetch_bans_and_settings():
                await _restore_persistent_settings()
                async for chat in old_db.grp.find({"chat_status.is_disabled": True}):
                    if chat.get("id"):
                        b_chats.append(int(chat["id"]))
                if plugin_db is not None and hasattr(plugin_db, "ban_col"):
                    async for user in plugin_db.ban_col.find({}):
                        if user.get("_id"):
                            b_users.append(int(user["_id"]))
                async for user in old_db.col.find({"ban_status.is_banned": True}):
                    u_id = user.get("id")
                    if u_id and int(u_id) not in b_users:
                        b_users.append(int(u_id))

            await asyncio.wait_for(fetch_bans_and_settings(), timeout=15.0)
        except Exception as e:
            logger.error(f"Failed to load startup bans/settings (Timeout/Error): {e}")

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

        # Automatically load & start all saved Multi-Bot workers in the background
        asyncio.create_task(_boot_multibot_workers())

        logger.info(
            f"{me.first_name} with Pyrogram v{__version__} (Layer {layer}) started on @{me.username}."
        )
        logger.info(LOG_STR)

        if os.path.exists("restart.txt"):
            try:
                with open("restart.txt", "r") as f:
                    lines = f.read().strip().splitlines()
                if len(lines) >= 2:
                    chat_id_str, msg_id_str = lines[0], lines[1]
                    await self.edit_message_text(
                        chat_id=int(chat_id_str),
                        message_id=int(msg_id_str),
                        text="✅ <b>Bot Restarted Successfully!</b>",
                        parse_mode=enums.ParseMode.HTML,
                    )
            except Exception as e:
                logger.error(f"Failed to edit restart message: {e}")
            finally:
                try:
                    os.remove("restart.txt")
                except Exception:
                    pass

    async def stop(self, *args, **kwargs):
        try:
            from plugins.multibot import stop_all_workers

            await stop_all_workers()
        except Exception:
            pass
        await super().stop(*args, **kwargs)
        logger.info("Bot stopped. Bye.")

    async def iter_messages(
        self, chat_id: Union[int, str], limit: int, offset: int = 0
    ) -> AsyncGenerator[types.Message, None]:
        """
        Safely iterates through chat messages in batches of up to 200 (Telegram API limit)
        with automatic FloodWait recovery.
        """
        current = max(1, offset)
        while current <= limit:
            batch_size = min(200, (limit - current) + 1)
            if batch_size <= 0:
                return
            message_ids = list(range(current, current + batch_size))
            try:
                messages = await self.get_messages(chat_id, message_ids)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue
            except Exception as e:
                logger.error(f"iter_messages error at offset {current}: {e}")
                current += batch_size
                continue

            if not isinstance(messages, list):
                messages = [messages]

            for message in messages:
                if message and not getattr(message, "empty", False):
                    yield message
            current += batch_size


app = Bot()


# ============================================================
# 🗑️ AUTO DELETE PM MEDIA (WITH ADMIN IMMUNITY)
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
    & filters.incoming
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
    if not message.from_user or message.outgoing:
        return

    # 👑 FOOLPROOF IMMUNITY: Admins will NOT have their PM files deleted automatically
    if await is_user_admin(client, message):
        return

    delay = int(getattr(info, "FILE_AUTO_DELETE", 1800))
    if delay <= 0:
        return

    task = asyncio.create_task(delete_media_task(message, delay=delay))
    AUTO_DELETE_TASKS.add(task)
    task.add_done_callback(AUTO_DELETE_TASKS.discard)


# ============================================================
# 🌐 HEALTH CHECKS & STARTUP (SUPPORTS / AND /health)
# ============================================================
async def health_check(request):
    return web.Response(text="Bot is running and healthy!", status=200)


async def start_services():
    web_app = web.Application()
    web_app.router.add_get("/", health_check)
    web_app.router.add_get("/health", health_check)
    runner = web.AppRunner(web_app)
    await runner.setup()

    bind_port = int(PORT) if PORT else 8080
    try:
        site = web.TCPSite(runner, "0.0.0.0", bind_port)
        await site.start()
        logger.info(
            f"🌐 Web server listening on port {bind_port} (/ and /health) for health checks."
        )
    except OSError as e:
        logger.warning(
            f"Port {bind_port} is already in use (external web server active): {e}"
        )

    await app.start()
    await idle()
    await app.stop()
    await runner.cleanup()


def force_shutdown(signum, frame):
    logger.info("🛑 Received shutdown signal. Exiting gracefully...")
    for task in asyncio.all_tasks(loop):
        task.cancel()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, force_shutdown)
    signal.signal(signal.SIGINT, force_shutdown)
    try:
        loop.run_until_complete(start_services())
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        logger.info("Process interrupted. Shutting down...")
