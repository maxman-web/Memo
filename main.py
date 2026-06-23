import os
import time
import asyncio
import aiohttp
import aiofiles
import re
import sqlite3
from aiohttp import web
from telethon import TelegramClient, events, Button, functions, types
from telethon.network import ConnectionTcpIntermediate

# Try importing psycopg2 for PostgreSQL support; fallback to SQLite gracefully if not installed
try:
    import psycopg2
    HAS_POSTGRES_LIB = True
except ImportError:
    HAS_POSTGRES_LIB = False

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
str_api_id = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DOOD_KEY = os.environ.get("DOOD_KEY", "").strip()
WEBSITE_HOME = os.environ.get("WEBSITE_HOME", "").strip()
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip('/')
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# 🔐 SECURITY & FORCE SUB
auth_str = os.environ.get("AUTH_USERS", "").strip()
AUTH_USERS = [int(x) for x in auth_str.split(',') if x.strip().isdigit()]
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "").strip()

if FORCE_SUB_CHANNEL.replace('-', '').isdigit():
    FORCE_SUB_CHANNEL = int(FORCE_SUB_CHANNEL)

# 🚦 GLOBAL QUEUE MANAGER
WORK_QUEUE = asyncio.Queue()

def parse_id(val):
    val = str(val).strip()
    if not val: return 0
    if val.startswith("@"): return val
    try: return int(val)
    except ValueError: return 0

DB_CHANNEL_ID = parse_id(os.environ.get("DB_CHANNEL_ID", "0"))
PUBLIC_CHANNEL_ID = parse_id(os.environ.get("PUBLIC_CHANNEL_ID", "0"))

if not str_api_id:
    print("❌ API_ID is missing!")
    exit(1)

API_ID = int(str_api_id)

# 🔄 CONNECTION (OPTIMIZED FOR RENDER)
bot = TelegramClient(
    'MaxCinema_Render_Session', 
    API_ID, 
    API_HASH, 
    connection=ConnectionTcpIntermediate, 
    timeout=120,          
    request_retries=10, 
    retry_delay=5        
)

print("✅ Bot is Starting...")

# ==========================================
# 🧠 SMART FILENAME & META PARSER
# ==========================================
def parse_media_meta(file_name):
    """
    Analyzes messy file names or captions and extracts a clean title, 
    media type (movie/series), season number, and episode number.
    """
    if not file_name:
        return "Unknown File", "movie", None, None
        
    # Standardize spacing and strip typical extension blocks
    clean_name = re.sub(r'[._-]', ' ', str(file_name)).strip()
    clean_name = re.sub(r'\s+', ' ', clean_name)
    
    # 1. Pattern Matching: S01E02, S1E2, S01 E02
    pattern_se = re.search(r'(.*?)\bS(\d{1,2})\s*E(\d{1,3})\b', clean_name, re.IGNORECASE)
    if pattern_se:
        title = pattern_se.group(1).strip()
        return title, "series", int(pattern_se.group(2)), int(pattern_se.group(3))
        
    # 2. Pattern Matching: 1x02 or 01x02
    pattern_x = re.search(r'(.*?)\b(\d{1,2})x(\d{1,3})\b', clean_name, re.IGNORECASE)
    if pattern_x:
        title = pattern_x.group(1).strip()
        return title, "series", int(pattern_x.group(2)), int(pattern_x.group(3))

    # 3. Pattern Matching: Standalone "Episode 05" or "EP 05" (Defaults to Season 1)
    pattern_ep = re.search(r'(.*?)\b(?:Ep|Episode)\s*(\d{1,3})\b', clean_name, re.IGNORECASE)
    if pattern_ep:
        title = pattern_ep.group(1).strip()
        return title, "series", 1, int(pattern_ep.group(2))

    # 4. Fallback: Treat as Movie and clear common encoding tags to extract the cleanest title
    movie_title = re.sub(r'\b(1080p|720p|4k|hdr|web\s*dl|bluray|x264|h264|x265|hevc|hindi|english|dual\s*audio)\b.*', '', clean_name, flags=re.IGNORECASE).strip()
    return movie_title or clean_name, "movie", None, None

# ==========================================
# 💾 SMART HYBRID DATABASE MANAGEMENT
# ==========================================
USE_POSTGRES = bool(DATABASE_URL and HAS_POSTGRES_LIB)

if USE_POSTGRES:
    print("🐘 Database Mode: Cloud PostgreSQL Connected!")
    def get_pg_conn():
        return psycopg2.connect(DATABASE_URL)
    
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cursor:
                # 1. Ensure core tables exist
                cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY)")
                cursor.execute("CREATE TABLE IF NOT EXISTS vault (msg_id BIGINT PRIMARY KEY, file_name TEXT)")
                
                # 2. Automatically upgrade old tables if columns are missing
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS title TEXT")
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS media_type TEXT")
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS season INTEGER")
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS episode INTEGER")
                conn.commit()
    except Exception as e:
        print(f"❌ PostgreSQL Table Alteration/Init Failed: {e}. Falling back to SQLite.")
        USE_POSTGRES = False
        
if not USE_POSTGRES:
    print("💾 Database Mode: SQLite + Telegram Vault Sync (Render Free Tier Engine)")
    sqlite_conn = sqlite3.connect('bot_users.db', check_same_thread=False)
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)")
    sqlite_cursor.execute("""
        CREATE TABLE IF NOT EXISTS vault (
            msg_id INTEGER PRIMARY KEY, 
            file_name TEXT,
            title TEXT,
            media_type TEXT,
            season INTEGER,
            episode INTEGER
        )
    """)
    sqlite_conn.commit()

async def sync_database_from_tg():
    """Restores the database file from Telegram on startup (SQLite Only)"""
    if USE_POSTGRES: return
    try:
        print("🔄 Syncing user database from Telegram Vault...")
        async for msg in bot.iter_messages(DB_CHANNEL_ID, search="#USER_BACKUP", limit=1):
            if msg.document:
                await bot.download_media(msg.document, file='bot_users.db')
                print("✅ Database synced successfully from Telegram!")
                return
        print("ℹ️ No previous database backup found. Starting fresh.")
    except Exception as e:
        print(f"❌ Backup sync failed: {e}")

async def backup_database_to_tg():
    """Backs up the SQLite database file to your private channel (SQLite Only)"""
    if USE_POSTGRES: return
    try:
        if os.path.exists('bot_users.db'):
            async for msg in bot.iter_messages(DB_CHANNEL_ID, search="#USER_BACKUP"):
                await msg.delete()
            
            await bot.send_file(
                DB_CHANNEL_ID, 
                'bot_users.db', 
                caption="💾 #USER_BACKUP\nDO NOT DELETE. Keeps your user broadcast list alive on Render's free tier."
            )
            print("✅ Database backup pushed to Telegram Vault!")
    except Exception as e:
        print(f"❌ Backup upload failed: {e}")

def add_user(user_id):
    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
                    conn.commit()
        except Exception as e:
            print(f"PostgreSQL Error adding user: {e}")
    else:
        try:
            sqlite_cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            sqlite_conn.commit()
            if sqlite_cursor.rowcount > 0:
                bot.loop.create_task(backup_database_to_tg())
        except Exception as e:
            print(f"SQLite Error adding user: {e}")

def get_all_users():
    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT user_id FROM users")
                    return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"PostgreSQL Error fetching users: {e}")
            return []
    else:
        try:
            sqlite_cursor.execute("SELECT user_id FROM users")
            return [row[0] for row in sqlite_cursor.fetchall()]
        except Exception as e:
            print(f"SQLite Error fetching users: {e}")
            return []

def add_vault_item(msg_id, file_name):
    """Indexes structural file property details into the database"""
    title, media_type, season, episode = parse_media_meta(file_name)
    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO vault (msg_id, file_name, title, media_type, season, episode) 
                        VALUES (%s, %s, %s, %s, %s, %s) 
                        ON CONFLICT (msg_id) DO UPDATE 
                        SET file_name = EXCLUDED.file_name, title = EXCLUDED.title, media_type = EXCLUDED.media_type, season = EXCLUDED.season, episode = EXCLUDED.episode
                    """, (msg_id, file_name, title, media_type, season, episode))
                    conn.commit()
        except Exception as e:
            print(f"PostgreSQL Vault Indexing Error: {e}")
    else:
        try:
            sqlite_cursor.execute("""
                INSERT OR REPLACE INTO vault (msg_id, file_name, title, media_type, season, episode) 
                VALUES (?, ?, ?, ?, ?, ?)
            """, (msg_id, file_name, title, media_type, season, episode))
            sqlite_conn.commit()
            bot.loop.create_task(backup_database_to_tg())
        except Exception as e:
            print(f"SQLite Vault Indexing Error: {e}")

def search_vault(query):
    """Queries the database search index sorted chronologically by season and episode"""
    clean_query = f"%{query.strip()}%"
    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT msg_id FROM vault 
                        WHERE title ILIKE %s OR file_name ILIKE %s 
                        ORDER BY media_type DESC, season ASC, episode ASC NULLS LAST LIMIT 5
                    """, (clean_query, clean_query))
                    return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"PostgreSQL Search Error: {e}")
            return []
    else:
        try:
            sqlite_cursor.execute("""
                SELECT msg_id FROM vault 
                WHERE title LIKE ? OR file_name LIKE ? 
                ORDER BY media_type DESC, season ASC, episode ASC LIMIT 5
            """, (clean_query, clean_query))
            return [row[0] for row in sqlite_cursor.fetchall()]
        except Exception as e:
            print(f"SQLite Search Error: {e}")
            return []

# ==========================================
# 🛠️ HELPER: FORCE CORRECT DOMAIN
# ==========================================
def fix_dood_link(link):
    if not link: return None
    bad_domains = ["dsvplay.com", "dood.re", "dood.wf", "dood.cx", "dood.sh", "dood.pm", "dood.to", "dood.so", "dood.la"]
    clean_link = link
    for domain in bad_domains:
        if domain in clean_link:
            clean_link = clean_link.replace(domain, "myvidplay.com")
            break
    if "myvidplay.com" not in clean_link and "http" in clean_link:
        clean_link = re.sub(r'https?://[^/]+', 'https://myvidplay.com', clean_link)
    return clean_link

# ==========================================
# 🛠️ HELPER: CHECK SUBSCRIPTION
# ==========================================
async def check_subscription(user_id):
    if not FORCE_SUB_CHANNEL: return True
    if user_id in AUTH_USERS: return True
    try:
        await bot(functions.channels.GetParticipantRequest(
            channel=FORCE_SUB_CHANNEL,
            participant=user_id
        ))
        return True
    except Exception:
        return False

# ==========================================
# 🚦 QUEUE WORKER (WITH MEMORY LEAK FIX)
# ==========================================
async def worker():
    print("👷 Queue Worker Started")
    while True:
        task_data = await WORK_QUEUE.get()
        event, source, name, thumb_path = task_data
        try:
            await process_task(event, source, name, thumb_path)
        except Exception as e:
            print(f"Task Failed: {e}")
            try:
                await event.reply(f"❌ Task Failed: {str(e)}")
            except Exception: pass
        finally:
            WORK_QUEUE.task_done()
            await asyncio.sleep(2)

# ==========================================
# 📊 PROGRESS BAR
# ==========================================
async def progress_bar(current, total, status_msg, action_name, last_time_ref):
    now = time.time()
    if now - last_time_ref[0] < 5: return 
    percentage = current * 100 / total
    filled = int(percentage / 10)
    bar = '▓' * filled + '░' * (10 - filled)
    try:
        await status_msg.edit(f"{action_name}\n{bar} **{percentage:.1f}%**")
        last_time_ref[0] = now
    except Exception: pass

# ==========================================
# 🧠 SMART DOWNLOADER
# ==========================================
async def smart_download(client, message, filename, progress_callback):
    try:
        await client.download_media(message, file=filename, progress_callback=progress_callback)
        return True
    except Exception: pass
    try:
        if hasattr(message, 'media') and hasattr(message.media, 'document'):
            await client.download_media(message.media.document, file=filename, progress_callback=progress_callback)
            return True
    except Exception: pass
    return False

# ==========================================
# 🌐 WEB SERVER (WITH VIDEO SEEKING FIX)
# ==========================================
async def stream_handler(request):
    try:
        msg_id = int(request.match_info['msg_id'])
        message = await bot.get_messages(DB_CHANNEL_ID, ids=msg_id)
        if not message or not message.file:
            return web.Response(status=404, text="File not found")

        file_name = "video.mp4"
        for attr in message.document.attributes:
            if isinstance(attr, types.DocumentAttributeFilename):
                file_name = attr.file_name
        
        file_size = message.document.size
        mime_type = message.document.mime_type or "application/octet-stream"

        range_header = request.headers.get('Range')
        start = 0
        end = file_size - 1

        if range_header:
            match = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if match:
                start = int(match.group(1))
                if match.group(2):
                    end = int(match.group(2))

        response = web.StreamResponse(status=206 if range_header else 200)
        response.headers['Content-Type'] = mime_type
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response.headers['Content-Disposition'] = f'inline; filename="{file_name}"'
        response.content_length = (end - start) + 1
        
        await response.prepare(request)

        offset_limit = 1048576 
        chunk_start = start - (start % offset_limit)
        
        async for chunk in bot.iter_download(message.media, offset=chunk_start):
            if chunk_start < start:
                slice_start = start - chunk_start
                chunk = chunk[slice_start:]
                chunk_start = start
            
            if len(chunk) > response.content_length:
                chunk = chunk[:response.content_length]

            await response.write(chunk)
            response.content_length -= len(chunk)
            if response.content_length <= 0:
                break

        return response
    except Exception as e:
        return web.Response(status=500, text=str(e))

async def root_handler(request):
    return web.Response(text="🚀 MaxCinema Bot is Running")

# ==========================================
# 🌟 HANDLERS
# ==========================================
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    sender = await event.get_sender()
    if sender:
        add_user(sender.id)
    args = event.text.split()
    is_joined = await check_subscription(sender.id)
    
    if len(args) > 1:
        param = args[1]
        if not is_joined:
            msg = "⛔ **Access Denied!**\n\nYou must join our main channel to download this file."
            btn = [[Button.url("📢 Join Channel", url=CHANNEL_LINK or "https://t.me/MaxCinemaOfficial")],
                   [Button.url("🔄 Try Again", url=f"https://t.me/{event.client.me.username}?start={param}")] ]
            return await event.reply(msg, buttons=btn)
            
        status = await event.reply("📂 **Fetching your files...**")
        if "pack_" in param:
            try:
                _, start_id, end_id = param.split('_')
                ids_to_fetch = list(range(int(start_id), int(end_id) + 1))
                messages = await bot.get_messages(DB_CHANNEL_ID, ids=ids_to_fetch)
                for msg in messages:
                    if msg and msg.media:
                        await bot.send_file(event.chat_id, msg.media, caption=msg.text)
                await status.delete()
            except Exception: await status.edit("❌ Pack not found.")
        else:
            try:
                msg_id = int(param)
                msg = await bot.get_messages(DB_CHANNEL_ID, ids=msg_id)
                if msg and msg.media:
                    await bot.send_file(event.chat_id, msg.media, caption=msg.text)
                    await status.delete()
                else:
                    await status.edit("❌ File not found.")
            except Exception: await status.edit("❌ Error processing file.")
        return

    if AUTH_USERS and sender.id in AUTH_USERS:
        admin_guide = (
            "**👑 MAXCINEMA ADMIN GUIDE**\n\n"
            "**1️⃣ MIRROR (Full)**\nReply `/mirror name.mp4`\n"
            "**2️⃣ ADD (Instant)**\nReply `/add` to a video.\n"
            "**3️⃣ POST (Auto)**\nReply Photo + `/post` to a bot message.\n"
            "**4️⃣ POST ID (Manual)**\nPhoto Caption: `/postid 1234 Caption...`\n"
            "**5️⃣ TMDB**\n`/tmdb Inception` to get movie details.\n"
            "**6️⃣ BROADCAST**\n`/broadcast Message` to message all users.\n"
            "**7️⃣ STATS**\n`/stats` to view DB users.\n"
            "**8️⃣ INDEX ALL**\n`/indexvault` to backfill structured items."
        )
        await event.reply(admin_guide)
    else:
        welcome_text = "**👋 Welcome to MaxCinema Bot!**\n\nI store files for the main channel."
        buttons = []
        if CHANNEL_LINK: buttons.append([Button.url("📢 Join Main Channel", url=CHANNEL_LINK)])
        buttons.append([Button.inline("📝 Request a Movie", data="help_request")])
        await event.reply(welcome_text, buttons=buttons)

@bot.on(events.CallbackQuery(data="help_request"))
async def callback_handler(event):
    await event.answer("💡 TYPE:\n/request Name", alert=True)

@bot.on(events.NewMessage(pattern='/request'))
async def request_handler(event):
    query = event.text.replace("/request", "").strip()
    sender = await event.get_sender()
    
    if not query: 
        return await event.reply("❌ Usage: `/request Movie Name`")
    
    status = await event.reply(f"🔍 Searching Structured Index for `{query}`...")
    
    try:
        msg_ids = search_vault(query)
        
        if msg_ids:
            messages = await bot.get_messages(DB_CHANNEL_ID, ids=msg_ids)
            if not isinstance(messages, list):
                messages = [messages]
                
            found = False
            for msg in messages:
                if msg and msg.media:
                    await bot.send_file(event.chat_id, msg.media, caption=msg.text)
                    found = True
            
            if found:
                return await status.edit("✅ **Here is what I found sorted for you!**")

        await status.edit("⚠️ Not found in database index. Forwarding request to admins...")
        if AUTH_USERS:
            for admin_id in AUTH_USERS:
                try:
                    await bot.send_message(admin_id, f"📩 **NEW REQUEST!**\n👤 {sender.first_name} (`{sender.id}`)\n📝 `{query}`")
                except Exception: continue
            await event.reply("✅ **Request Sent to Admins!**")
    except Exception as e:
        await status.edit(f"❌ Error searching: {str(e)}")

@bot.on(events.NewMessage(pattern='/indexvault'))
async def index_vault_handler(event):
    """Admin command to bypass GetHistoryRequest restrictions by crawling message IDs directly."""
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return
    
    status = await event.reply("🔄 **Starting Restricted-Bypass Indexing...**\nFetching latest message checkpoint...")
    
    try:
        # 1. Find the highest message ID in the channel by sending a temporary message or checking latest
        latest_msg = await bot.send_message(DB_CHANNEL_ID, "🔄 Checking channel sync point...")
        max_id = latest_msg.id
        await latest_msg.delete() # Clean it up immediately
        
        await status.edit(f"🔍 **Checkpoint found at ID: {max_id}**\nScanning files backwards to bypass Telegram restrictions...")
        
        count = 0
        # 2. Walk backwards through every possible message ID
        for current_id in range(max_id, 0, -1):
            try:
                msg = await bot.get_messages(DB_CHANNEL_ID, ids=current_id)
                if msg and msg.document:
                    file_name = ""
                    for attr in msg.document.attributes:
                        if isinstance(attr, types.DocumentAttributeFilename):
                            file_name = attr.file_name
                            break
                    if not file_name and msg.text:
                        file_name = msg.text.split('\n')[0][:80]
                    if not file_name:
                        file_name = f"Media_File_{msg.id}"
                    
                    add_vault_item(msg.id, file_name)
                    count += 1
                    
                    if count % 20 == 0:  # Frequent small updates so you know it's working
                        await status.edit(f"🔄 **Bypass Indexing Progress...**\nParsed **{count}** files.\nChecking ID: {current_id}...")
                
                # Tiny rest to stay completely clear of FloodWaits
                if current_id % 30 == 0:
                    await asyncio.sleep(0.5)
                    
            except Exception as e:
                # If a single message ID fails or is deleted, keep moving!
                continue
                
        await status.edit(f"✅ **Restricted-Bypass Indexing Complete!**\nSuccessfully indexed **{count}** files without using history APIs.")
        if not USE_POSTGRES:
            bot.loop.create_task(backup_database_to_tg())
            
    except Exception as e:
        await status.edit(f"❌ Indexing Bypass Failed: {str(e)}")


@bot.on(events.NewMessage(pattern='/stats'))
async def stats_handler(event):
    if event.sender_id not in AUTH_USERS: return
    users = get_all_users()
    await event.reply(f"📊 **Bot Statistics**\n\n👥 Total Users: **{len(users)}**")

@bot.on(events.NewMessage(pattern='/broadcast'))
async def broadcast_handler(event):
    if event.sender_id not in AUTH_USERS: return
    msg = event.text.replace("/broadcast", "").strip()
    if not msg: return await event.reply("❌ Usage: `/broadcast Hello everyone!`")
    
    users = get_all_users()
    sent = 0
    status = await event.reply(f"🚀 Broadcasting to {len(users)} users...")
    
    for user in users:
        try:
            await bot.send_message(user, msg)
            sent += 1
            await asyncio.sleep(0.1) 
        except Exception: pass
        
    await status.edit(f"✅ **Broadcast Complete!**\nDelivered to: {sent}/{len(users)}")

@bot.on(events.NewMessage(pattern='/tmdb'))
async def tmdb_handler(event):
    if event.sender_id not in AUTH_USERS: return
    query = event.text.replace("/tmdb", "").strip()
    if not query: return await event.reply("❌ Usage: `/tmdb Inception`")
    if not TMDB_API_KEY: return await event.reply("❌ TMDB_API_KEY is missing in ENV.")
    
    status = await event.reply("🔍 Fetching from TMDB...")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={query}"
            async with session.get(url) as r:
                data = await r.json()
                
            if not data.get('results'):
                return await status.edit("❌ Movie not found.")
                
            movie = data['results'][0]
            title = movie.get('title')
            year = movie.get('release_date', '').split('-')[0]
            rating = movie.get('vote_average', 'N/A')
            overview = movie.get('overview', 'No summary.')
            poster_path = movie.get('poster_path')
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
            
            caption = (
                f"🎬 **{title} ({year})**\n\n"
                f"⭐ **IMDb Rating:** {rating}/10\n"
                f"📖 **Plot:** {overview}\n\n"
                f"👇 **Download Below**"
            )
            
            if poster_url:
                await bot.send_file(event.chat_id, poster_url, caption=f"`{caption}`\n\n*(Copy the text above)*")
                await status.delete()
            else:
                await status.edit(f"`{caption}`")
    except Exception as e:
        await status.edit(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern='/add'))
async def add_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return
    reply = await event.get_reply_message()
    if not reply or not reply.media: return await event.reply("❌ Please reply to a video or file.")

    try:
        original_caption = reply.text or ""
        vault_msg = await bot.send_file(DB_CHANNEL_ID, reply.media, caption=original_caption)
        
        index_title = original_caption
        if reply.file and hasattr(reply.file, 'name') and reply.file.name:
            index_title = f"{reply.file.name} {original_caption}"
        if not index_title.strip():
            index_title = f"Movie_File_{vault_msg.id}"
            
        add_vault_item(vault_msg.id, index_title)
        
        msg = (
            f"✅ **File Added & Structured to DB!**\n\n"
            f"📂 **Vault ID:** {vault_msg.id}\n"
            f"🔗 **Doodstream:** N/A\n\n"
            f"👇 Reply with Photo + `/post` to publish."
        )
        await event.reply(msg)
    except Exception as e:
        await event.reply(f"❌ Error Adding: {e}")

@bot.on(events.NewMessage(pattern='/postid'))
async def postid_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return
    args = event.text.split(' ', 2)
    if len(args) < 3: 
        return await event.reply("❌ **Usage:** Send Photo with caption:\n`/postid 1234 Your Movie Caption`")
    
    try:
        vault_id = int(args[1])
        caption = args[2]
    except ValueError:
        return await event.reply("❌ **Error:** ID must be a number.\nEx: `/postid 1055 Caption`")

    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={vault_id}"
    
    buttons = []
    if WEBSITE_HOME: buttons.append([Button.url("🌍 Visit Website", url=WEBSITE_HOME)])
    buttons.append([Button.url("📂 Get File", url=deep_link)])

    poster = await event.download_media() if event.photo else None
    if not poster:
        reply = await event.get_reply_message()
        if reply and reply.photo:
            poster = await reply.download_media()

    try:
        await bot.send_file(PUBLIC_CHANNEL_ID, poster, caption=caption, buttons=buttons)
        if poster: os.remove(poster)
        
        add_vault_item(vault_id, caption)
        await event.reply(f"✅ **Published Manually!**\n🆔 Vault ID: {vault_id}")
    except Exception as e:
        await event.reply(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern='/postpack'))
async def postpack_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return 
    
    args = event.text.split()
    if len(args) < 2: return await event.reply("❌ Usage: `/postpack 100-107 Caption`")
    
    range_str = args[1]
    try: start_id, end_id = range_str.split('-')
    except Exception: return await event.reply("❌ Invalid Format. Use `100-107`")

    dood_link = None
    for arg in args:
        if "http" in arg: 
            dood_link = arg
            break
            
    caption = event.text.replace("/postpack", "").replace(range_str, "")
    if dood_link: caption = caption.replace(dood_link, "")
    caption = caption.strip() or "🎬 **New Season Pack!**"
    
    me = await bot.get_me()
    pack_link = f"https://t.me/{me.username}?start=pack_{start_id}_{end_id}"
    
    buttons = [[Button.url("📂 Get Full Season", url=pack_link)]]
    web_url = dood_link if dood_link else WEBSITE_HOME
    if web_url: buttons.insert(0, [Button.url("🌍 Stream / Web", url=web_url)])

    poster = await event.download_media() if event.photo else None
    try:
        await bot.send_file(PUBLIC_CHANNEL_ID, poster, caption=caption, buttons=buttons)
        if poster: os.remove(poster)
        
        for item_id in range(int(start_id), int(end_id) + 1):
            add_vault_item(item_id, f"{caption} Episode {item_id}")
            
        await event.reply(f"✅ Pack Published!\n🔗 **Link:** {pack_link}")
    except Exception as e: await event.reply(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern='/post'))
async def post_handler(event):
    if "/postpack" in event.text or "/postid" in event.text: return 
    
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return 
    reply = await event.get_reply_message()
    if not reply: return await event.reply("⚠️ Reply to a Vault ID message.")
    
    vault_id = None
    for line in reply.text.split('\n'):
        if "Vault ID:" in line: vault_id = re.sub(r'[^0-9]', '', line.split("Vault ID:")[1])

    if not vault_id: return await event.reply("❌ **Invalid:** No Vault ID found.")

    dood_link = None
    args = event.text.split()
    for arg in args:
        if "http" in arg and "dood" in arg:
            dood_link = arg
            break
            
    if not dood_link:
        for line in reply.text.split('\n'):
            if "Doodstream:" in line: 
                raw_link = line.split("Doodstream:")[1]
                if "http" in raw_link: dood_link = raw_link.replace('*', '').replace(' ', '').strip()

    caption = event.text.replace("/post", "")
    if dood_link: caption = caption.replace(dood_link, "")
    caption = caption.strip() or "🎬 **New Movie Uploaded!**"

    web_url = fix_dood_link(dood_link) if dood_link else None
    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={vault_id}"
    
    buttons = []
    if web_url:
        buttons.append([Button.url("☁️ Stream (Now)", url=web_url)])
        buttons.append([Button.url("📂 Get File", url=deep_link)])
        if WEBSITE_HOME: buttons.append([Button.url("🌍 Visit Website", url=WEBSITE_HOME)])
    else:
        if WEBSITE_HOME: buttons.append([Button.url("🌍 Visit Website", url=WEBSITE_HOME)])
        buttons.append([Button.url("📂 Get File", url=deep_link)])

    poster = await event.download_media() if event.photo else None
    try:
        await bot.send_file(PUBLIC_CHANNEL_ID, poster, caption=caption, buttons=buttons)
        if poster: os.remove(poster)
        
        add_vault_item(int(vault_id), caption)
        await event.reply(f"✅ Published!\nButtons added: {len(buttons[0]) if buttons else 0}")
    except Exception as e: await event.reply(f"❌ Error: {e}")

# ==========================================
# 🧠 CORE PROCESSOR (WITH CLEANUP FIX)
# ==========================================
async def process_task(event, source, name, thumb_path):
    status_msg = await event.reply(f"⏳ **Initializing:** `{name}`...")
    last_time = [0]
    current_thumb = thumb_path
    
    try:
        if isinstance(source, str): 
            await status_msg.edit(f"🚀 **Downloading URL...**")
            headers = {'User-Agent': 'Mozilla/5.0'}
            timeout = aiohttp.ClientTimeout(total=3600)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source, headers=headers) as resp:
                    if resp.status == 200:
                        total = int(resp.headers.get('content-length', 0))
                        current = 0
                        async with aiofiles.open(name, mode='wb') as f:
                            async for chunk in resp.content.iter_chunked(10 * 1024 * 1024): 
                                await f.write(chunk)
                                current += len(chunk)
                                await progress_bar(current, total, status_msg, "⬇️ **Downloading...**", last_time)
                    else: 
                        await status_msg.edit(f"❌ Error: Server returned {resp.status}")
                        return False
        else:
            await status_msg.edit(f"📥 **Downloading from Telegram...**")
            async def dl_callback(c, t): await progress_bar(c, t, status_msg, "📥 **Downloading...**", last_time)
            success = await smart_download(bot, source, name, dl_callback)
            if not success: return False

        if not current_thumb:
            generated_thumb = f"{name}_thumb.jpg"
            cmd = ["ffmpeg", "-i", name, "-ss", "00:00:05", "-vframes", "1", generated_thumb, "-y"]
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                process.kill()
            if os.path.exists(generated_thumb): current_thumb = generated_thumb

        await status_msg.edit("⚡ **Uploading to Vault...**")
        last_time = [0]
        async def up_callback(c, t): await progress_bar(c, t, status_msg, "☁️ **Uploading to Vault...**", last_time)
        try:
            vault_msg = await bot.send_file(DB_CHANNEL_ID, file=name, caption=f"🔒 {name}", thumb=current_thumb, supports_streaming=True, progress_callback=up_callback)
            add_vault_item(vault_msg.id, name)
        except Exception as e: return False

        await status_msg.edit("⚡ **Uploading to Doodstream...**")
        dood_link = "N/A"
        try:
             if DOOD_KEY:
                 async with aiohttp.ClientSession() as session:
                    url_api = f"https://doodapi.co/api/upload/server?key={DOOD_KEY}"
                    async with session.get(url_api) as r:
                        data = await r.json()
                    if data['status'] == 200:
                        upload_url = data['result']
                        post_data = aiohttp.FormData()
                        post_data.add_field('api_key', DOOD_KEY)
                        post_data.add_field('file', open(name, 'rb'), filename=name)
                        async with session.post(upload_url, data=post_data, timeout=7200) as r:
                            resp = await r.json()
                            if resp['status'] == 200:
                                raw_link = resp['result'][0]['download_url']
                                dood_link = fix_dood_link(raw_link)
        except Exception as e: dood_link = f"Error: {e}"

        final_msg = (
            f"✅ **Mirror Complete!**\n\n"
            f"📂 **Vault ID:** {vault_msg.id}\n"
            f"🔗 **Doodstream:** {dood_link}\n\n"
            f"👇 Reply with Photo + `/post` to publish."
        )
        await status_msg.edit(final_msg)
        return True
        
    except Exception as e:
        await status_msg.edit(f"❌ Error: {str(e)}")
        return False
        
    finally:
        if os.path.exists(name): 
            try: os.remove(name)
            except Exception: pass
        if current_thumb and current_thumb != thumb_path and os.path.exists(current_thumb):
            try: os.remove(current_thumb)
            except Exception: pass

@bot.on(events.NewMessage(pattern='/mirror'))
async def handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return 
    reply = await event.get_reply_message()
    batch_thumb = await event.download_media() if event.photo else (await reply.download_media() if reply and reply.photo else None)

    tasks = []
    if reply and (reply.video or reply.document) and not reply.photo:
        parts = event.text.split(' ', 1)
        new_name = parts[1] if len(parts) > 1 else f"Video_{int(time.time())}.mp4"
        tasks.append((reply, new_name))
    else:
        parts = event.text.split()
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                tasks.append((parts[i], parts[i+1]))

    if not tasks: return await event.reply("Usage: `/mirror link1 name1 link2 name2...`")

    q_size = WORK_QUEUE.qsize()
    await event.reply(f"📥 **Added to Queue**\nPosition: {q_size + 1}")
    
    for source, name in tasks:
        await WORK_QUEUE.put((event, source, name, batch_thumb))

async def start_web_server():
    app = web.Application()
    app.add_routes([
        web.get("/", root_handler),
        web.get("/stream/{msg_id}", stream_handler)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get("PORT", 10000)) 
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web Server Started on Port {port}")


if __name__ == '__main__':
    bot.start(bot_token=BOT_TOKEN)
    
    bot.loop.run_until_complete(sync_database_from_tg())
    
    bot.loop.create_task(start_web_server())
    bot.loop.create_task(worker())
    bot.run_until_disconnected()
