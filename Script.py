class script(object):
    # ==========================================
    # 🏠 MAIN MENUS
    # ==========================================
    START_TXT = """<b>Hᴇʏ {mention} 👋🏻\n\nI ᴀᴍ {bname} 🤖\n\nA ᴘᴏᴡᴇʀꜰᴜʟ ᴀᴜᴛᴏ-ꜰɪʟᴛᴇʀ ʙᴏᴛ ᴛᴏ ꜱᴇᴀʀᴄʜ ᴍᴏᴠɪᴇꜱ ᴀɴᴅ ꜱᴇʀɪᴇꜱ ɪɴꜱᴛᴀɴᴛʟʏ!\n\nSᴇɴᴅ ᴍᴇ ᴀɴʏ ᴍᴏᴠɪᴇ ɴᴀᴍᴇ, ᴀɴᴅ I ᴡɪʟʟ ᴘʀᴏᴠɪᴅᴇ ᴛʜᴇ ꜰɪʟᴇꜱ.</b>"""

    HELP_TXT = """<b>Hᴇʏ {mention} 👋🏻\n\nCʜᴏᴏꜱᴇ ᴛʜᴇ ᴄᴀᴛᴇɢᴏʀʏ ʙᴇʟᴏᴡ ᴛᴏ ꜱᴇᴇ ᴀʟʟ ᴀᴠᴀɪʟᴀʙʟᴇ ᴄᴏᴍᴍᴀɴᴅꜱ ᴀɴᴅ ꜰᴇᴀᴛᴜʀᴇꜱ!</b>"""

    ABOUT_TXT = """<b>🤖 Nᴀᴍᴇ:</b> {bname}\n<b>👨‍💻 Dᴇᴠᴇʟᴏᴘᴇʀ:</b> <a href='https://t.me/KR_Picture'>KR Pɪᴄᴛᴜʀᴇ</a>\n<b>📺 Cʜᴀɴɴᴇʟ:</b> <a href='https://t.me/sandalwood_kannada_moviesz'>Sᴀɴᴅᴀʟᴡᴏᴏᴅ</a>\n<b>💬 Sᴜᴘᴘᴏʀᴛ:</b> <a href='https://t.me/Kannada_Filmy_Group'>Kᴀɴɴᴀᴅᴀ Fɪʟᴍʏ Gʀᴏᴜᴘ</a>"""

    STATUS_TXT = """<b>📊 Bᴏᴛ Dᴀᴛᴀʙᴀsᴇ Sᴛᴀᴛᴜs</b>\n\n<b>📁 Tᴏᴛᴀʟ Fɪʟᴇs:</b> <code>{}</code>\n<b>👤 Tᴏᴛᴀʟ Usᴇʀs:</b> <code>{}</code>\n<b>🏘 Tᴏᴛᴀʟ Cʜᴀᴛs:</b> <code>{}</code>\n<b>💾 Usᴇᴅ Sᴛᴏʀᴀɢᴇ:</b> <code>{}</code>\n<b>💿 Fʀᴇᴇ Sᴛᴏʀᴀɢᴇ:</b> <code>{}</code>"""

    LOG_TEXT_G = """<b>#Nᴇᴡ_Gʀᴏᴜᴘ</b>\n\n<b>Group Name:</b> <code>{}</code>\n<b>Group ID:</b> <code>{}</code>\n<b>Members:</b> <code>{}</code>\n<b>Added By:</b> {}"""

    LOG_TEXT_P = (
        """<b>#Nᴇᴡ_Usᴇʀ</b>\n\n<b>User ID:</b> <code>{}</code>\n<b>Profile:</b> {}"""
    )

    # ==========================================
    # 🎨 UI CUSTOMIZATION MENUS
    # ==========================================
    UISTART_TXT = """<blockquote><b>🎨 Sᴛᴀʀᴛ Mᴇɴᴜ Cᴜsᴛᴏᴍɪᴢᴀᴛɪᴏɴ</b></blockquote>\n
<b>‣ /setstarttext</b> - Set custom text
<b>‣ /defaultstarttext</b> - Revert to default text
<b>‣ /setstartimage</b> - Set image (reply to photo)
<b>‣ /remstartimage</b> - Remove custom image
<b>‣ /addstartbutton</b> - Add a button (e.g. `/addstartbutton "Text" https://link.com`)
<b>‣ /editstartbuttons</b> - Edit layout & color (e.g. `/editstartbuttons 1 sidebyside green`)
<b>‣ /remstartbutton</b> - Clear all custom buttons"""

    UIHELP_TXT = """<blockquote><b>🎨 Hᴇʟᴘ Mᴇɴᴜ Cᴜsᴛᴏᴍɪᴢᴀᴛɪᴏɴ</b></blockquote>\n
<b>‣ /sethelptext</b> - Set custom text
<b>‣ /defaulthelptext</b> - Revert to default text
<b>‣ /sethelpimage</b> - Set image (reply to photo)
<b>‣ /remhelpimage</b> - Remove custom image
<b>‣ /addhelpbutton</b> - Add a button (e.g. `/addhelpbutton "Text" https://link.com`)
<b>‣ /edithelpbuttons</b> - Edit layout & color (e.g. `/edithelpbuttons 1 belowside red`)
<b>‣ /remhelpbutton</b> - Clear all custom buttons"""

    UIABOUT_TXT = """<blockquote><b>🎨 Aʙᴏᴜᴛ Mᴇɴᴜ Cᴜsᴛᴏᴍɪᴢᴀᴛɪᴏɴ</b></blockquote>\n
<b>‣ /setabouttext</b> - Set custom text
<b>‣ /defaultabouttext</b> - Revert to default text
<b>‣ /setaboutimage</b> - Set image (reply to photo)
<b>‣ /remaboutimage</b> - Remove custom image
<b>‣ /addaboutbutton</b> - Add a button (e.g. `/addaboutbutton "Text" https://link.com`)
<b>‣ /editaboutbuttons</b> - Edit layout & color
<b>‣ /remaboutbutton</b> - Clear all custom buttons"""

    # ==========================================
    # 📝 FORMATTING GUIDE (Used in Help)
    # ==========================================
    FORMAT_GUIDE = """
<b>💡 Tᴇxᴛ Fᴏʀᴍᴀᴛᴛɪɴɢ Gᴜɪᴅᴇ:</b>
<i>Supports Markdown and HTML!</i>
• <code>**Bold**</code> ➔ **Bold**
• <code>__Italic__</code> ➔ __Italic__
• <code>~~Strike~~</code> ➔ ~~Strike~~
• <code>--Underline--</code> ➔ --Underline--
• <code>||Spoiler||</code> ➔ ||Spoiler||
• <code>`Mono`</code> ➔ `Mono`
• <code>&gt; Quote</code> ➔ &gt; Quote
• <code>[Link Text](http://url.com)</code> ➔ [Link Text](http://url.com)
"""

    # ==========================================
    # 👋 WELCOME SETTINGS
    # ==========================================
    WELCOME_TXT = f"""<b>👋 Wᴇʟᴄᴏᴍᴇ Mᴇssᴀɢᴇs (Gʀᴏᴜᴘ Aᴅᴍɪɴs)</b>

• <code>/enablewelcome</code> - Turn ON welcome messages.
• <code>/disablewelcome</code> - Turn OFF welcome messages.
• <code>/setwelcometxt &lt;text&gt;</code> - Set custom text.
<i>Variables: {{mention}}, {{title}}, {{count}}</i>
• <code>/setwelcomeimg</code> - Reply to an image to set it.
• <code>/setwelcome</code> - Reply to an image with a caption to set both!
• <code>/delwelcome</code> - Delete custom welcome settings.
{FORMAT_GUIDE}"""

    # ==========================================
    # 🖼️ IMAGE SETTINGS
    # ==========================================
    IMAGES_TXT = """<b>🖼️ Gʟᴏʙᴀʟ Iᴍᴀɢᴇs (Bᴏᴛ Aᴅᴍɪɴ)</b>

<b>Aᴜᴛᴏ-Fɪʟᴛᴇʀ Iᴍᴀɢᴇ</b>
• <code>/setautoimg</code> - Reply to an image to set a global default filter image.
• <code>/remautoimg</code> - Remove global default image.

<b>Fɪʟᴇ Nᴏᴛ Fᴏᴜɴᴅ Iᴍᴀɢᴇ</b>
• <code>/setfilenotfoundimg</code> - Reply to image to set FNF image.
• <code>/remfilenotfoundimg</code> - Remove FNF image.
• <code>/defaultfilenotfoundimg</code> - Reset to repo default.

<b>Fᴏʀᴄᴇ Sᴜʙsᴄʀɪʙᴇ Iᴍᴀɢᴇ</b>
• <code>/setfsubimg</code> - Reply to an image to set it as the Force Subscribe alert photo!"""

    # ==========================================
    # 🔍 SPELL CHECK & NOT FOUND
    # ==========================================
    SPELLCHECK_TXT = """<b>🔍 Sᴘᴇʟʟ Cʜᴇᴄᴋ & Nᴏᴛ Fᴏᴜɴᴅ</b>

<b>Sᴘᴇʟʟ Cʜᴇᴄᴋ & Sᴇᴀʀᴄʜ (Gʀᴏᴜᴘ Aᴅᴍɪɴs)</b>
‣ <code>/enablespellcheck</code> - Turn ON spelling suggestions.
‣ <code>/disablespellcheck</code> - Turn OFF spelling suggestions.

<b>Sᴛᴏᴘᴡᴏʀᴅs & Nᴏᴛ Fᴏᴜɴᴅ Tᴇxᴛ (Bᴏᴛ Aᴅᴍɪɴ)</b>
‣ <code>/addstopwords &lt;words&gt;</code> - Add words the bot should ignore (comma separated).
‣ <code>/stopwords</code> - Show Stop Words 
‣ <code>/remstopwords</code> - Remove Stop Word
‣ <code>/remallstopwords</code> - Remove All Stop Words
‣ <code>/defaultstopwords</code> - Make Default Words into available in repo
‣ <code>/setnotfoundtext &lt;text&gt;</code> - Set FNF text.
‣ <code>/remnotfoundtext</code> - Remove FNF text.
‣ <code>/defaultnotfoundtext</code> - Reset to repo default text."""

    # ==========================================
    # 📝 FILTERS (Auto & Manual)
    # ==========================================
    FILTERS_TXT = """<blockquote><b>Filter Management\nAdd, delete, or view filters to customize responses based on keywords.</b></blockquote>\n
<b>‣ /filter - Add a text filter (Reply `/filter keyword` to a message)
‣ /addfilter - Add a text filter from pre-made buttons
‣ /filterimage - Update only image for a filter (Reply to image with `/filterimage keyword`)
‣ /editfiltercolur - Change button colour - `/editfiltercolur keyword 1 green`
‣ /delfilter - Delete a text filter - `/delfilter filter`
‣ /listfilters - List all filters currently added in the bot\n\nSupports text/photo/video/animation/sticker</b>

<blockquote><b>📝 Auto-Filter & Clean Filename</b></blockquote>
<b>Auto Filter:</b>
Files in connected channels are searched automatically based on user text.
<b>📺 Series Aggregator:</b>
Seasons and Episodes are automatically collapsed into clean "Season X" folders.
<b>🧼 Clean Filename Settings:</b>
Control what words are removed from inline button filenames.
• <code>/setcleanfilename [word1, word2, @channel]</code> - Set words to remove.
• <code>/defaultcleanfilename</code> - Revert to standard repo default (removes mkv, mp4, sandalwood).
• <code>/remcleanfilename</code> - Disable clean filename completely."""

    # ==========================================
    # 📱 FORCE SUBSCRIBE
    # ==========================================
    FORCESUB_TXT = """<blockquote><b>Force Subscription Management\nSet, manage, or clear force subscribe channels.</b></blockquote>\n
<b>‣ /enablefsub - enable force subscribe 
‣ /disablefsub - disable force subscribe 
‣ /setfsubcount - Set maximum Fsub chat count for queue
‣ /setfsub - Set force subscribe channel - `/setfsub channel_id`
‣ /rmfsub - Remove force subscribe channel - `/rmfsub channel_id`
‣ /rmallfsub - Remove all force subscribe channels
‣ /getallfsub - Get all force subscribe channel details
‣ /getactivefsub - Get active force subscribe channels
‣ /getpendingfsub - Get pending force subscribe channels which is in queue
‣ /activatefsub - Activate pending force subscribe channel
‣ /deactivatefsub - Deactivate force subscribe channel
‣ /updatefsubtarget - Update force subscribe channel target
‣ /checkfsubusers - Check force subscribe users count
‣ /clearfsubusers - Clear all force subscribe users from db\n
🚪 JOIN REQUESTS:
‣ /totalrequests - Get total pending join requests
‣ /purgerequests - Purge all join requests from database</b>"""

    # ==========================================
    # 👥 FORCE ADD
    # ==========================================
    FORCEADD_TXT = """<blockquote><b>ForceAdd Management</b></blockquote>\n
<b>‣ /setforceadd - Set a Force Add channel for the group.
‣ /remforceadd - Remove the current Force Add channel.
‣ /getforceadd - View the configured Force Add channel.
‣ /topaddall - Show the all-time top inviters leaderboard.
‣ /topadd24 - Show the top inviters in the last 24 hours.
‣ /topadd7 - Show the top inviters from the last 7 days.
‣ /resetadddaily - Reset today's add statistics.
‣ /resetadd - Reset all Force Add statistics.
‣ /myadds - Check your personal add count and ranking.</b>"""

    # ==========================================
    # 🗑️ DELETE COMMANDS
    # ==========================================
    DELETE_TXT = """<blockquote><b>File/Auto Deletion Management\nDelete files from the database or configure auto-delete settings for files and button messages in groups.</b></blockquote>\n
<b>‣ /delete - Reply to a file to delete it from database
‣ /delmulti - Delete multiple files from database with name - `/delmulti name`
‣ /autodelete - Set file auto delete time in seconds
‣ /buttondel - Set button message in groups auto delete time in seconds
‣ /purgeduplicates - Scan DB, keep highest resolution, and delete redundant clones</b>"""

    # ==========================================
    # 🚫 BANS & RESTRICTIONS
    # ==========================================
    BANS_TXT = """<blockquote><b>User Management\nBan or unban users to control access to the bot.</b></blockquote>\n
<b>‣ /ban - Ban a user from bot - `/ban user_id`
‣ /unban - Unban a user from bot - `/unban user_id`
‣ /bannedusers - Check Banned Users List
‣ /leave - Force bot to leave a chat 
‣ /enable - whitelist a group 
‣ /disable - blacklist a group</b>"""

    # ==========================================
    # 📝 CUSTOM CAPTION
    # ==========================================
    CUSTOMCAPTION_TXT = """<blockquote><b>File Caption Management\nManage or customize captions for files, including additional captions, to enhance file presentation.</b></blockquote>\n
<b>‣ /customcaption - Set custom caption for files (Reply to message to set, or `/customcaption off` to disable)
‣ /captionplus - Set additional caption for files along with main caption (Reply to message to set, or `/captionplus off` to disable)</b>

You can customize file captions by editing your Environment Variables.

<b>Vᴀʀɪᴀʙʟᴇs:</b>
• <code>{file_name}</code> - Name of the file
• <code>{file_size}</code> - Size of the file
• <code>{file_caption}</code> - Original caption"""

    CUSTOMMESSAGES_TXT = """<blockquote><b>Custom Messages & Images\nConfigure custom messages and images for various actions.</b></blockquote>\n
<b>‣ /infomsg - Set info message before sending file
‣ /infoimg - Set info image before sending file
‣ /delmsg - Set delete message after sending file (File auto delete needs to be enabled)
‣ /delimg - Set delete image after sending file
‣ /notfoundmsg - Set message to send when file not found
‣ /notfoundimg - Set image to send when file not found
‣ /fsubmsg - Set force subscribe message
‣ /fsubimg - Set force subscribe image\n
*(Reply to a message/image with the command to set it, or use `off` to remove)*</b>"""

    # ==========================================
    # 📚 INDEXING (File Save)
    # ==========================================
    INDEX_TXT = """<b>📚 Indexing
‣ <code>/index</code> - Save all media to DB
‣ <code>/indexvideo</code> - Save only videos
‣ <code>/indexdoc</code> - Save only documents
‣ <code>/indexaudio</code> - Save only audio
‣ <code>/resumeindex</code> - Resume stopped indexing

⏩ Speed & Skip
‣ <code>/setskip</code> - Skip first X messages
‣ <code>/currentskip</code> - View current skip count
‣ <code>/deleteskip</code> - Reset skip to 0
‣ <code>/setindexspeed</code> - Set delay between fetches

⚖️ Filters
‣ <code>/setminsize</code> - Set minimum file size (MB)
‣ <code>/setblacklist</code> - Add words to ignore
‣ <code>/remblacklist</code> - Remove ignored word
‣ <code>/allblacklist</code> - View ignored words
‣ <code>/setwhitelist</code> - Index ONLY these words
‣ <code>/remwhitelist</code> - Remove whitelist word
‣ <code>/allwhitelist</code> - View whitelist words

🛡️ Auto-Backup & Links
‣ <code>/setbackupchannel</code> - Set private backup chat ID
‣ <code>/autobackup</code> - Toggle Anti-Copyright shield
‣ <code>/link</code> - Get shareable link for a file
‣ <code>/plink</code> - Get protected shareable link
‣ <code>/batch</code> - Index entire channel in bulk
‣ <code>/pbatch</code> - Index entire channel in bulk (Protected Content)

🧹 Database Cleanup
‣ <code>/purgeduplicates</code> - Scan & Delete duplicate DB entries
‣ <code>/cleandeadlinks</code> - Remove broken Telegram links
‣ <code>/total</code> - Count total indexed files
‣ <code>/clearfiles</code> - ⚠️ Nuke entire file database!</b>"""

    # ==========================================
    # 📢 PROMOTIONS & BROADCAST
    # ==========================================
    PROMOTIONS_TXT = """<blockquote><b>Manage Promotional Links\nEasily add, delete, or view promotional links displayed between search results.</b></blockquote>\n
<b>‣ /addpromo - Set promotional links between results - `/addpromo "Button Text" URL`
‣ /delpromo - Delete promotional links between results - `/delpromo URL`
‣ /listpromos - List all promotional links currently added in the DB

💬 Bʀᴏᴀᴅᴄᴀsᴛɪɴɢ (Aᴜᴛᴏ-ᴅᴇʟᴇᴛᴇs ɪɴ 24ʜ)
‣ /broadcast - send to all users 
‣ /group_broadcast - send to all groups</b>"""

    # ==========================================
    # ⚙️ SETTINGS & CONNECTIONS
    # ==========================================
    SETTINGS_TXT = """<blockquote><b>Bot Settings Management</b></blockquote>\n
<b>‣ /repairmode - Enable or disable repair mode - If on, bot will not send any files
‣ /adminsettings - Get current admin settings
‣ /enablereaction - Enable Auto-Heart reactions globally
‣ /disablereaction - Disable Auto-Heart reactions globally</b>"""

    CONNECTIONS_TXT = """<blockquote><b>𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀 𝗮𝗻𝗱 𝗨𝘀𝗮𝗴𝗲\nUsed to connect bot to PM for managing filters, avoiding spamming in groups.</b></blockquote>\n
<b>‣ /connect  - Connect a particular chat to your PM
‣ /disconnect  - Disconnect from a chat
‣ /connections - List all your connections

• <code>/channels</code> - Interactive menu of all connected channels/groups.
• <code>/leavechannel &lt;id&gt;</code> - Force leave and scrub a channel from DB.
• <code>/exportusers</code> - Download .txt list of all users.
• <code>/exportgroups</code> - Download .txt list of all groups.
• <code>/exportchannels</code> - Download .txt list of all channels.</b>"""

    # ==========================================
    # 📊 UTILITIES & BACKUP
    # ==========================================
    UTILITIES_TXT = """<blockquote><b>Utility Commands\nAccess bot logs, server stats, restart the bot, get user and file counts, send broadcasts, and more.</b></blockquote>\n
<b>‣ /channels - List all connected groups and channels (15 per page)
‣ /leavechannel - Leave a channel or group by ID
‣ /logs - Get logs as a file
‣ /server - Get server stats
‣ /restart - Restart the bot
‣ /stats - Database statistics
‣ /analize - View Live Trending Search Dashboard
‣ /exportstats - Export full search history data to .txt
‣ /optimize_db - Optimize Database indexes for speed
‣ /broadcast - Reply to a message to send that to all bot users
‣ /total - Get count of total files in DB
‣ /clearfiles - Clear all files from DB
‣ /users_list - Get list of users in DB
‣ /clearusers - Clear all users from DB
‣ /cleanusers - Ping all users to purge deleted accounts
‣ /clearfsubusers - Clear all force subscribe users from db</b>"""

    BACKUP_TXT = """<blockquote><b>Database Backup Management\nManage database backups, including scheduled backups and manual backups.</b></blockquote>\n
<b>These commands are only available for the ADMINS of the bot.</b>\n
<b>‣ /dbbackup - Generate full JSON backup of your database.
‣ /dbrestore - (reply to .json file) - Restore database from file.
‣ /dbstats - Detailed MongoDB specs.
‣ /dbschedule - Start 24h automated backup cron job.</b>"""

    # ============================================================
    # NEW FEATURE TEXTS (AUTO-POST, PM REPLY, CLEAN FILENAME)
    # ============================================================
    PMAUTOREPLY_TXT = """<b>💬 PM Auto-Reply Engine</b>

Configure what the bot says when someone messages it directly in PM.

<b>Commands:</b>
• <code>/setpmtext [text]</code> - Set PM reply text (HTML bold, italics, links supported).
• <code>/rempmtext</code> - Reset text to default.
• <code>/setpmimage [URL/Reply]</code> - Set PM image.
• <code>/rempmimage</code> - Remove PM image.
• <code>/setpmbutton [Text | URL | Color]</code> - Set colored PM button (green, red, blue).
• <code>/rempmbutton</code> - Remove PM button."""

    AUTOPOST_TXT = """<b>🧬 Auto-Post Engine</b>

‣ <code>/autopost</code> - Toggle auto-post engine (on/off)
‣ <code>/addmovieupdatechannel</code> - Add dynamic movie update channel
‣ <code>/remmovieupdatechannel</code> - Remove movie update channel
‣ <code>/allmovieupdatechannel</code> - View all movie update channels
‣ <code>/addautopostchannel</code> - Add dynamic auto-post channel
‣ <code>/remautopostchannel</code> - Remove auto-post channel
‣ <code>/allautopostchannel</code> - View all auto-post channels
‣ <code>/editautopost</code> - Set main caption template
‣ <code>/setautoposttext</code> - Set main caption template (alias)
‣ <code>/editautoposttitle</code> - Edit title format
‣ <code>/editautopostyear</code> - Edit year format
‣ <code>/editautopostlanguages</code> - Edit languages format
‣ <code>/editautopostresolutions</code> - Edit resolutions format
‣ <code>/editautopostgenres</code> - Edit genres format
‣ <code>/editautopostottplatforms</code> - Edit OTT platforms format
‣ <code>/editautopostdirect</code> - Edit direct search button text
‣ <code>/setapbtn1</code> - Set button 1 (Text | URL)
‣ <code>/setapbtn2</code> - Set button 2 (Text | URL)
‣ <code>/setapbtn3</code> - Set button 3 (Text | URL)
‣ <code>/remapbtn1</code> - Remove button 1
‣ <code>/remapbtn2</code> - Remove button 2
‣ <code>/remapbtn3</code> - Remove button 3
‣ <code>/setautopostimage</code> - Set custom/global poster image
‣ <code>/remautopostimage</code> - Revert back to dynamic TMDB posters
‣ <code>/setautopoststicker</code> - Set sticker sent with post (reply to sticker)
‣ <code>/remautopoststicker</code> - Remove auto-post sticker</b>"""

    POSTHAND_TXT = """<b>📝 POST HANDLER GUIDE

​‣ <code>/post</code> - Start manual post session with interactive search
‣ <code>/editpost</code> - Import and edit an existing channel post via link
‣ <code>/resumepost</code> - Restore lost/interrupted draft from MongoDB
‣ <code>/hydra</code> - Toggle Hydra Anti-Ban engine (on/off)
‣ <code>/updatealllinks</code> - Mass update old inline URLs across channel posts
‣ <code>/editposttitle</code> - Edit movie title in active draft
‣ <code>/edittitle</code> - Edit movie title (alias)
‣ <code>/edittittle</code> - Edit movie title (alias)
‣ <code>/editpostyear</code> - Edit release year in active draft
‣ <code>/edityear</code> - Edit release year (alias)
‣ <code>/editpostbutton</code> - Edit custom inline button (Number Text | URL)
‣ <code>/editpostbuttoncolour</code> - Change button colour (green/red/blue)
‣ <code>/editbuttoncolour</code> - Change button colour (alias)
‣ <code>/editpostdirect</code> - Change Direct Search button URL
‣ <code>/editdirect</code> - Change Direct Search button URL (alias)
‣ <code>/editpostlangs</code> - Edit languages list in active draft
‣ <code>/editlangs</code> - Edit languages list (alias)
‣ <code>/editpostresolutions</code> - Edit resolutions list in active draft
‣ <code>/editresolutions</code> - Edit resolutions list (alias)
‣ <code>/editpostgenres</code> - Edit genres list in active draft
‣ <code>/editgenres</code> - Edit genres list (alias)
‣ <code>/editpostotts</code> - Edit OTT platforms list in active draft
‣ <code>/editotts</code> - Edit OTT platforms list (alias)
‣ <code>/editpostimage</code> - Set preview URL/poster for draft
‣ <code>/editimage</code> - Set preview URL/poster (alias)
‣ <code>/editipostmage</code> - Set preview URL/poster (alias)
‣ <code>/editpostnormalimage</code> - Set normal photo file_id for draft
‣ <code>/editnormalimage</code> - Set normal photo file_id (alias)  </b>"""
