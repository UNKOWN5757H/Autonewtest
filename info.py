import re
from os import environ

id_pattern = re.compile(r"^.\d+$")


def is_enabled(value, default):
    if value.lower() in ["true", "yes", "1", "enable", "y"]:
        return True
    elif value.lower() in ["false", "no", "0", "disable", "n"]:
        return False
    else:
        return default


SESSION = environ.get("SESSION", "VersionQ")
API_ID = int(environ.get("API_ID", "2468192"))
API_HASH = environ.get("API_HASH", "4906b3f8f198ec0e24edb2c197677678")
BOT_TOKEN = environ.get("BOT_TOKEN", " ")
PORT = int(environ.get("PORT", "8080"))

CACHE_TIME = int(environ.get("CACHE_TIME", 300))
USE_CAPTION_FILTER = bool(environ.get("USE_CAPTION_FILTER", False))
PICS = (environ.get("PICS", "https://iili.io/COHUHil.jpg")).split()

# ⚡ GLOBAL AUTO-DELETE SETTINGS (in seconds)
FILE_AUTO_DELETE = int(environ.get("FILE_AUTO_DELETE", 1800))
BUTTON_AUTO_DELETE = int(environ.get("BUTTON_AUTO_DELETE", 1800))
AUTO_DELETE_TIME = int(environ.get("AUTO_DELETE_TIME", 1800))
BROADCAST_DELETE_TIME = int(environ.get("BROADCAST_DELETE_TIME", 86400))

REPAIR_MODE = is_enabled(environ.get("REPAIR_MODE", "False"), False)

ADMINS = [
    int(admin) if id_pattern.search(admin) else admin
    for admin in environ.get("ADMINS", "2098589219").split()
]
CHANNELS = [
    int(ch) if id_pattern.search(ch) else ch
    for ch in environ.get("CHANNELS", "-1002055023335").split()
]
auth_users = [
    int(user) if id_pattern.search(user) else user
    for user in environ.get("AUTH_USERS", "2098589219").split()
]
AUTH_USERS = (auth_users + ADMINS) if auth_users else []
auth_grp = environ.get("AUTH_GROUP")
AUTH_GROUPS = [int(ch) for ch in auth_grp.split()] if auth_grp else None

DATABASE_URI = environ.get(
    "DATABASE_URI",
    "mongodb+srv://Rashmika1:Rashmika@cluster0.2rfx8ak.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0",
)
DATABASE_NAME = environ.get("DATABASE_NAME", "ClusterQ")
COLLECTION_NAME = environ.get("COLLECTION_NAME", "VersionQ")

auth_channel = environ.get("AUTH_CHANNEL", "sandalwood_kannada_moviesz")
AUTH_CHANNEL = (
    int(auth_channel) if auth_channel and id_pattern.search(auth_channel) else None
)
REQ_CHANNEL = environ.get("REQ_CHANNEL", "+pCz5eoun5Zk5YzRl")
REQ_CHANNEL = (
    int(REQ_CHANNEL) if REQ_CHANNEL and id_pattern.search(REQ_CHANNEL) else False
)
JOIN_REQS_DB = environ.get("JOIN_REQS_DB", DATABASE_URI)

IS_FSUB_ENABLED = is_enabled(environ.get("IS_FSUB_ENABLED", "True"), True)
FSUB_MAX_COUNT = int(environ.get("FSUB_MAX_COUNT", 0))
FSUB_CHANNELS = {}

LOG_CHANNEL = int(environ.get("LOG_CHANNEL", "-1001693006436"))
SUPPORT_CHAT = environ.get("SUPPORT_CHAT", "Kannada_Filmy_Group")
P_TTI_SHOW_OFF = is_enabled((environ.get("P_TTI_SHOW_OFF", "True")), False)
IMDB = is_enabled((environ.get("IMDB", "False")), True)
SINGLE_BUTTON = is_enabled((environ.get("SINGLE_BUTTON", "True")), False)

CUSTOM_FILE_CAPTION = environ.get(
    "CUSTOM_FILE_CAPTION",
    """<b>{file_name} \n \n𝗝𝗼𝗶𝗻 𝗢𝘂𝗿 𝗖𝗵𝗮𝗻𝗻𝗲𝗹𝘀 ⚡️\n𝐌𝐚𝐢𝐧 𝐂𝐡𝐚𝐧𝐧𝐞𝐥 👇\nhttps://t.me/sandalwood_kannada_moviesz \n𝐑𝐞𝐪𝐮𝐞𝐬𝐭 𝐌𝐨𝐯𝐢𝐞𝐬 👇\nhttps://t.me/Sandalwood_Kannada_Group</b>""",
)
BATCH_FILE_CAPTION = environ.get("BATCH_FILE_CAPTION", CUSTOM_FILE_CAPTION)
CAPTION_PLUS = environ.get("CAPTION_PLUS", None)

NOT_FOUND_MSG = environ.get(
    "NOT_FOUND_MSG",
    "<b>🚫 File not found. Please note👇\n \n✅ Use correct spelling as given in Google.\n \n✅ DO NOT ask for files which are not released in OTT.\n \n✅ Request movies in this format - (Moviename) (Year of release) \nEg. Jai Ganesh 2024 </b>",
)
NOT_FOUND_IMG = environ.get(
    "NOT_FOUND_IMG",
    "https://telegra.ph/file/c4f0458d30f61993aad45-086b84e8363b3c582e.jpg",
)

INFO_MSG = environ.get("INFO_MSG", None)
INFO_IMG = environ.get("INFO_IMG", None)
DEL_MSG = environ.get("DEL_MSG", None)
DEL_IMG = environ.get("DEL_IMG", None)
FSUB_MSG = environ.get("FSUB_MSG", None)
FSUB_IMG = environ.get("FSUB_IMG", None)

IMDB_TEMPLATE = environ.get(
    "IMDB_TEMPLATE",
    "<b>Query: {query}</b> \nIMDb Data:\n\n🏷 Title: <a href={url}>{title}</a>\n🎭 Genres: {genres}\n📆 Year: <a href={url}/releaseinfo>{year}</a>\n🌟 Rating: <a href={url}/ratings>{rating}</a> / 10",
)
LONG_IMDB_DESCRIPTION = is_enabled(environ.get("LONG_IMDB_DESCRIPTION", "False"), False)
SPELL_CHECK_REPLY = is_enabled(environ.get("SPELL_CHECK_REPLY", "True"), True)
MAX_LIST_ELM = environ.get("MAX_LIST_ELM", None)
INDEX_REQ_CHANNEL = int(environ.get("INDEX_REQ_CHANNEL", LOG_CHANNEL))
FILE_STORE_CHANNEL = [
    int(ch) for ch in (environ.get("FILE_STORE_CHANNEL", "-1002961955349")).split()
]
MELCOW_NEW_USERS = is_enabled((environ.get("MELCOW_NEW_USERS", "False")), True)
PROTECT_CONTENT = is_enabled((environ.get("PROTECT_CONTENT", "False")), False)
PUBLIC_FILE_STORE = is_enabled((environ.get("PUBLIC_FILE_STORE", "True")), True)

MOVIE_UPDATE_NOTIFICATION = is_enabled(
    environ.get("MOVIE_UPDATE_NOTIFICATION", "True"), False
)

# ⚡ DEDICATED CHANNELS
MOVIE_UPDATE_CHANNEL = [
    int(ch)
    for ch in environ.get(
        "MOVIE_UPDATE_CHANNEL",
        "-1001923564465 -1002391396205 -1002102723064 -1002032840255 -1002438454087 -1002436948244 -1002459789086 -1002309583611",
    ).split()
]

AUTOPOSTCHANNEL = [
    int(ch) for ch in environ.get("AUTOPOSTCHANNEL", "-1001923564465").split()
]  # Change to your auto-post channel ID

IMAGE_FETCH = is_enabled(environ.get("IMAGE_FETCH", "True"), True)
LINK_PREVIEW = is_enabled(environ.get("LINK_PREVIEW", "False"), False)
ABOVE_PREVIEW = is_enabled(environ.get("ABOVE_PREVIEW", "True"), True)
TMDB_API_KEY = environ.get("TMDB_API_KEY", "")
TMDB_POSTER = is_enabled(environ.get("TMDB_POSTER", "False"), False)
LANDSCAPE_POSTER = is_enabled(environ.get("LANDSCAPE_POSTER", "True"), True)

LOG_STR = "Current Cusomized Configurations are:-\n"
LOG_STR += (
    "IMDB Results are enabled, Bot will be showing imdb details for you queries.\n"
    if IMDB
    else "IMBD Results are disabled.\n"
)
LOG_STR += (
    "P_TTI_SHOW_OFF found , Users will be redirected to send /start to Bot PM instead of sending file file directly\n"
    if P_TTI_SHOW_OFF
    else "P_TTI_SHOW_OFF is disabled files will be send in PM, instead of sending start.\n"
)
LOG_STR += (
    "SINGLE_BUTTON is Found, filename and files size will be shown in a single button instead of two separate buttons\n"
    if SINGLE_BUTTON
    else "SINGLE_BUTTON is disabled , filename and file_sixe will be shown as different buttons\n"
)
LOG_STR += (
    f"CUSTOM_FILE_CAPTION enabled with value {CUSTOM_FILE_CAPTION}, your files will be send along with this customized caption.\n"
    if CUSTOM_FILE_CAPTION
    else "No CUSTOM_FILE_CAPTION Found, Default captions of file will be used.\n"
)
LOG_STR += (
    "Long IMDB storyline enabled."
    if LONG_IMDB_DESCRIPTION
    else "LONG_IMDB_DESCRIPTION is disabled , Plot will be shorter.\n"
)
LOG_STR += (
    "Spell Check Mode Is Enabled, bot will be suggesting related movies if movie not found\n"
    if SPELL_CHECK_REPLY
    else "SPELL_CHECK_REPLY Mode disabled\n"
)
LOG_STR += (
    f"MAX_LIST_ELM Found, long list will be shortened to first {MAX_LIST_ELM} elements\n"
    if MAX_LIST_ELM
    else "Full List of casts and crew will be shown in imdb template, restrict them by adding a value to MAX_LIST_ELM\n"
)
LOG_STR += f"Your current IMDB template is {IMDB_TEMPLATE}"
