
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
DOOD_KEY = os.environ.get("DOOD_KEY", "").strip()  # kept for compatibility; unused
WEBSITE_HOME = os.environ.get("WEBSITE_HOME", "").strip()
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

auth_str = os.environ.get("AUTH_USERS", "").strip()
AUTH_USERS = [int(x) for x in auth_str.split(",") if x.strip().isdigit()]
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "").strip()
if FORCE_SUB_CHANNEL.replace("-", "").isdigit():
    FORCE_SUB_CHANNEL = int(FORCE_SUB_CHANNEL)

WORK_QUEUE = asyncio.Queue()

def parse_id(val):
    val = str(val).strip()
    if not val:
        return 0
    if val.startswith("@"):
        return val
    try:
        return int(val)
    except ValueError:
        return 0

DB_CHANNEL_ID = parse_id(os.environ.get("DB_CHANNEL_ID", "0"))
PUBLIC_CHANNEL_ID = parse_id(os.environ.get("PUBLIC_CHANNEL_ID", "0"))

if not str_api_id:
    print("❌ API_ID is missing!")
    raise SystemExit(1)

API_ID = int(str_api_id)

bot = TelegramClient(
    "MaxCinema_Render_Session",
    API_ID,
    API_HASH,
    connection=ConnectionTcpIntermediate,
    timeout=120,
    request_retries=10,
    retry_delay=5
)

print("✅ Bot is Starting...")

# Globals initialized later
USE_POSTGRES = bool(DATABASE_URL and HAS_POSTGRES_LIB)
sqlite_conn = None
sqlite_cursor = None
PG_INITIALIZED = False
SQLITE_INITIALIZED = False

# ==========================================
# 🧠 SMART FILENAME & META PARSER
# ==========================================
def parse_media_meta(file_name):
    if not file_name:
        return "Unknown File", "movie", None, None

    clean_name = re.sub(r"[._-]", " ", str(file_name)).strip()
    clean_name = re.sub(r"\s+", " ", clean_name)

    pattern_se = re.search(r"(.*?)\bS(\d{1,2})\s*E(\d{1,3})\b", clean_name, re.IGNORECASE)
    if pattern_se:
        title = pattern_se.group(1).strip()
        return title, "series", int(pattern_se.group(2)), int(pattern_se.group(3))

    pattern_x = re.search(r"(.*?)\b(\d{1,2})x(\d{1,3})\b", clean_name, re.IGNORECASE)
    if pattern_x:
        title = pattern_x.group(1).strip()
        return title, "series", int(pattern_x.group(2)), int(pattern_x.group(3))

    pattern_ep = re.search(r"(.*?)\b(?:Ep|Episode)\s*(\d{1,3})\b", clean_name, re.IGNORECASE)
    if pattern_ep:
        title = pattern_ep.group(1).strip()
        return title, "series", 1, int(pattern_ep.group(2))

    movie_title = re.sub(
        r"\b(1080p|720p|4k|hdr|web\s*dl|bluray|x264|h264|x265|hevc|hindi|english|dual\s*audio)\b.*",
        "",
        clean_name,
        flags=re.IGNORECASE
    ).strip()
    return movie_title or clean_name, "movie", None, None

def is_media_filename(name: str) -> bool:
    if not name:
        return False
    name = name.lower().strip()
    return bool(re.search(r"\.(mp4|mkv|avi|mov|webm|m4v|srt|ass|sub)$", name))

# ==========================================
# 💾 DATABASE INITIALIZATION
# ==========================================
def init_postgres():
    global PG_INITIALIZED
    if PG_INITIALIZED or not USE_POSTGRES:
        return

    def get_pg_conn():
        return psycopg2.connect(DATABASE_URL)

    globals()["get_pg_conn"] = get_pg_conn

    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY)")
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS vault (
                        msg_id BIGINT PRIMARY KEY,
                        file_name TEXT,
                        title TEXT,
                        media_type TEXT,
                        season INTEGER,
                        episode INTEGER
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS vault_map (
                        public_msg_id BIGINT PRIMARY KEY,
                        vault_msg_id BIGINT NOT NULL
                    )
                """)
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS title TEXT")
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS media_type TEXT")
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS season INTEGER")
                cursor.execute("ALTER TABLE vault ADD COLUMN IF NOT EXISTS episode INTEGER")
                conn.commit()
        PG_INITIALIZED = True
        print("🐘 Database Mode: Cloud PostgreSQL Connected!")
    except Exception as e:
        print(f"❌ PostgreSQL Table Alteration/Init Failed: {e}. Falling back to SQLite.")
        globals()["USE_POSTGRES"] = False

def init_sqlite():
    global sqlite_conn, sqlite_cursor, SQLITE_INITIALIZED
    if SQLITE_INITIALIZED:
        return

    print("💾 Database Mode: SQLite + Telegram Vault Sync (Render Free Tier Engine)")
    sqlite_conn = sqlite3.connect("bot_users.db", check_same_thread=False)
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
    sqlite_cursor.execute("""
        CREATE TABLE IF NOT EXISTS vault_map (
            public_msg_id INTEGER PRIMARY KEY,
            vault_msg_id INTEGER NOT NULL
        )
    """)
    sqlite_conn.commit()
    SQLITE_INITIALIZED = True

async def sync_database_from_tg():
    """Restores the database file from Telegram on startup (SQLite Only)"""
    if USE_POSTGRES:
        return
    try:
        print("🔄 Syncing user database from Telegram Vault...")
        async for msg in bot.iter_messages(DB_CHANNEL_ID, search="#USER_BACKUP", limit=1):
            if msg and msg.document:
                if os.path.exists("bot_users.db"):
                    try:
                        os.remove("bot_users.db")
                    except Exception:
                        pass
                await bot.download_media(msg.document, file="bot_users.db")
                print("✅ Database synced successfully from Telegram!")
                return
        print("ℹ️ No previous database backup found. Starting fresh.")
    except Exception as e:
        print(f"❌ Backup sync failed: {e}")

async def backup_database_to_tg():
    """Backs up the SQLite database file to your private channel (SQLite Only)"""
    if USE_POSTGRES:
        return
    try:
        if os.path.exists("bot_users.db"):
            async for msg in bot.iter_messages(DB_CHANNEL_ID, search="#USER_BACKUP"):
                await msg.delete()

            await bot.send_file(
                DB_CHANNEL_ID,
                "bot_users.db",
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
                    cursor.execute(
                        "INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                        (user_id,)
                    )
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
                        SET file_name = EXCLUDED.file_name,
                            title = EXCLUDED.title,
                            media_type = EXCLUDED.media_type,
                            season = EXCLUDED.season,
                            episode = EXCLUDED.episode
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
    """Handles multi-word queries for better accuracy"""
    words = query.strip().split()
    if not words:
        return []

    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    conditions = " AND ".join(["(title ILIKE %s OR file_name ILIKE %s)" for _ in words])
                    params = []
                    for word in words:
                        params.extend([f"%{word}%", f"%{word}%"])

                    sql = f"""
                        SELECT msg_id FROM vault
                        WHERE {conditions}
                        ORDER BY media_type DESC, season ASC, episode ASC NULLS LAST
                        LIMIT 8
                    """
                    cursor.execute(sql, tuple(params))
                    return [row[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"PostgreSQL Search Error: {e}")
            return []
    else:
        try:
            conditions = " AND ".join(["(title LIKE ? OR file_name LIKE ?)" for _ in words])
            params = []
            for word in words:
                params.extend([f"%{word}%", f"%{word}%"])

            sql = f"""
                SELECT msg_id FROM vault
                WHERE {conditions}
                ORDER BY media_type DESC, season ASC, episode ASC
                LIMIT 8
            """
            sqlite_cursor.execute(sql, tuple(params))
            return [row[0] for row in sqlite_cursor.fetchall()]
        except Exception as e:
            print(f"SQLite Search Error: {e}")
            return []

async def get_vault_message(vault_id: int):
    try:
        msg = await bot.get_messages(DB_CHANNEL_ID, ids=vault_id)
        if msg and msg.media:
            return msg
    except Exception:
        pass
    return None

def save_public_mapping(public_msg_id: int, vault_msg_id: int):
    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO vault_map (public_msg_id, vault_msg_id)
                        VALUES (%s, %s)
                        ON CONFLICT (public_msg_id)
                        DO UPDATE SET vault_msg_id = EXCLUDED.vault_msg_id
                    """, (public_msg_id, vault_msg_id))
                    conn.commit()
        except Exception as e:
            print(f"PostgreSQL mapping error: {e}")
    else:
        try:
            sqlite_cursor.execute("""
                INSERT OR REPLACE INTO vault_map (public_msg_id, vault_msg_id)
                VALUES (?, ?)
            """, (public_msg_id, vault_msg_id))
            sqlite_conn.commit()
        except Exception as e:
            print(f"SQLite mapping error: {e}")

def get_vault_id_from_public(public_msg_id: int):
    if USE_POSTGRES:
        try:
            with get_pg_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT vault_msg_id FROM vault_map WHERE public_msg_id = %s", (public_msg_id,))
                    row = cursor.fetchone()
                    return row[0] if row else None
        except Exception:
            return None
    else:
        try:
            sqlite_cursor.execute("SELECT vault_msg_id FROM vault_map WHERE public_msg_id = ?", (public_msg_id,))
            row = sqlite_cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None

async def send_public_card(caption: str, buttons: list, poster_path: str | None = None):
    if poster_path:
        try:
            public_msg = await bot.send_file(PUBLIC_CHANNEL_ID, poster_path, caption=caption, buttons=buttons)
        finally:
            try:
                if os.path.exists(poster_path):
                    os.remove(poster_path)
            except Exception:
                pass
    else:
        public_msg = await bot.send_message(PUBLIC_CHANNEL_ID, caption, buttons=buttons)
    return public_msg

# ==========================================
# 🛠️ HELPER: CHECK SUBSCRIPTION
# ==========================================
async def check_subscription(user_id):
    if not FORCE_SUB_CHANNEL:
        return True
    if user_id in AUTH_USERS:
        return True
    try:
        await bot(functions.channels.GetParticipantRequest(
            channel=FORCE_SUB_CHANNEL,
            participant=user_id
        ))
        return True
    except Exception:
        return False

# ==========================================
# 🚦 QUEUE WORKER
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
            except Exception:
                pass
        finally:
            WORK_QUEUE.task_done()
            await asyncio.sleep(2)

# ==========================================
# 📊 PROGRESS BAR
# ==========================================
async def progress_bar(current, total, status_msg, action_name, last_time_ref):
    if total <= 0:
        total = 1
    now = time.time()
    if now - last_time_ref[0] < 5:
        return
    percentage = current * 100 / total
    filled = int(percentage / 10)
    bar = "▓" * filled + "░" * (10 - filled)
    try:
        await status_msg.edit(f"{action_name}\n{bar} **{percentage:.1f}%**")
        last_time_ref[0] = now
    except Exception:
        pass

# ==========================================
# 🧠 SMART DOWNLOADER
# ==========================================
async def smart_download(client, message, filename, progress_callback):
    try:
        await client.download_media(message, file=filename, progress_callback=progress_callback)
        return True
    except Exception:
        pass
    try:
        if hasattr(message, "media") and hasattr(message.media, "document"):
            await client.download_media(message.media.document, file=filename, progress_callback=progress_callback)
            return True
    except Exception:
        pass
    return False

# ==========================================
# 🌐 WEB SERVER
# ==========================================
async def stream_handler(request):
    try:
        msg_id = int(request.match_info["msg_id"])
        message = await bot.get_messages(DB_CHANNEL_ID, ids=msg_id)
        if not message or not message.file:
            return web.Response(status=404, text="File not found")

        file_name = "video.mp4"
        if message.document:
            for attr in message.document.attributes:
                if isinstance(attr, types.DocumentAttributeFilename):
                    file_name = attr.file_name
                    break

        file_size = message.document.size if message.document else 0
        mime_type = (message.document.mime_type if message.document else None) or "application/octet-stream"

        range_header = request.headers.get("Range")
        start = 0
        end = file_size - 1 if file_size else 0

        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                if match.group(2):
                    end = int(match.group(2))
                else:
                    end = file_size - 1 if file_size else 0

        if file_size and start >= file_size:
            return web.Response(status=416, text="Requested Range Not Satisfiable")

        response = web.StreamResponse(status=206 if range_header else 200)
        response.headers["Content-Type"] = mime_type
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Content-Disposition"] = f'inline; filename="{file_name}"'

        remaining = (end - start) + 1 if file_size else 0
        if remaining < 0:
            remaining = 0
        response.content_length = remaining if remaining else None

        if range_header and file_size:
            response.headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

        await response.prepare(request)

        async for chunk in bot.iter_download(message.media, offset=start):
            if file_size and remaining <= 0:
                break
            if file_size and len(chunk) > remaining:
                chunk = chunk[:remaining]
            await response.write(chunk)
            if file_size:
                remaining -= len(chunk)

        return response
    except Exception as e:
        return web.Response(status=500, text=str(e))

async def root_handler(request):
    return web.Response(text="🚀 MaxCinema Bot is Running")

# ==========================================
# 🌟 HANDLERS
# ==========================================
@bot.on(events.NewMessage(pattern="/start"))
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
            btn = [
                [Button.url("📢 Join Channel", url=CHANNEL_LINK or "https://t.me/MaxCinemaOfficial")],
                [Button.url("🔄 Try Again", url=f"https://t.me/{event.client.me.username}?start={param}")]
            ]
            return await event.reply(msg, buttons=btn)

        status = await event.reply("📂 **Fetching your files...**")

        if "pack_" in param:
            try:
                _, start_id, end_id = param.split("_")
                ids_to_fetch = list(range(int(start_id), int(end_id) + 1))
                found_any = False

                for msg_id in ids_to_fetch:
                    msg = await get_vault_message(msg_id)
                    if msg:
                        sent_file = await bot.send_file(event.chat_id, msg.media, caption=msg.text)
                        warning = await event.reply("⏳ **SECURITY:** *This file will auto-delete in 5 minutes to protect the bot.*")
                        bot.loop.create_task(auto_delete_task(event, [sent_file, warning], delay=300))
                        found_any = True

                if found_any:
                    await status.delete()
                else:
                    await status.edit("❌ Pack not found in the storage channel.")
            except Exception:
                await status.edit("❌ Pack not found.")
        else:
            try:
                msg_id = int(param)
                msg = await get_vault_message(msg_id)
                if not msg:
                    mapped_vault_id = get_vault_id_from_public(msg_id)
                    if mapped_vault_id:
                        msg = await get_vault_message(mapped_vault_id)

                if msg and msg.media:
                    sent_file = await bot.send_file(event.chat_id, msg.media, caption=msg.text)
                    warning = await event.reply("⏳ **SECURITY:** *This file will auto-delete in 5 minutes to protect the bot.*")
                    bot.loop.create_task(auto_delete_task(event, [sent_file, warning], delay=300))
                    await status.delete()
                else:
                    await status.edit("❌ File not found in the storage channel.")
            except Exception:
                await status.edit("❌ Error processing file.")
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
        if CHANNEL_LINK:
            buttons.append([Button.url("📢 Join Main Channel", url=CHANNEL_LINK)])
        buttons.append([Button.inline("📝 Request a Movie", data="help_request")])
        await event.reply(welcome_text, buttons=buttons)

@bot.on(events.CallbackQuery(data="help_request"))
async def callback_handler(event):
    await event.answer("💡 TYPE:\n/request Name", alert=True)

@bot.on(events.NewMessage(pattern="/request"))
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
                    sent_file = await bot.send_file(event.chat_id, msg.media, caption=msg.text)
                    warning = await event.reply("⏳ **SECURITY:** *This file will auto-delete in 5 minutes to protect the bot.*")
                    bot.loop.create_task(auto_delete_task(event, [sent_file, warning], delay=300))
                    found = True

            if found:
                return await status.edit("✅ **Here is what I found sorted for you!**")

        await status.edit("⚠️ Not found in database index. Forwarding request to admins...")
        if AUTH_USERS:
            for admin_id in AUTH_USERS:
                try:
                    await bot.send_message(admin_id, f"📩 **NEW REQUEST!**\n👤 {sender.first_name} (`{sender.id}`)\n📝 `{query}`")
                except Exception:
                    continue
            await event.reply("✅ **Request Sent to Admins!**")
    except Exception as e:
        await status.edit(f"❌ Error searching: {str(e)}")

@bot.on(events.NewMessage(pattern="/indexvault"))
async def index_vault_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS:
        return

    status = await event.reply("🔄 Starting vault indexing...")
    count = 0

    try:
        async for msg in bot.iter_messages(DB_CHANNEL_ID, limit=None):
            if not msg or not msg.media:
                continue

            file_name = ""
            if getattr(msg, "file", None) and getattr(msg.file, "name", None):
                file_name = msg.file.name
            elif msg.document:
                for attr in msg.document.attributes:
                    if isinstance(attr, types.DocumentAttributeFilename):
                        file_name = attr.file_name
                        break
            elif msg.text:
                file_name = msg.text.split("\n")[0][:80]

            if not file_name:
                file_name = f"Media_File_{msg.id}"

            if not is_media_filename(file_name):
                continue

            add_vault_item(msg.id, file_name)
            count += 1

            if count % 50 == 0:
                await status.edit(f"🔄 Indexed {count} files...")

        await status.edit(f"✅ Indexing complete. Indexed {count} files.")
        if not USE_POSTGRES:
            bot.loop.create_task(backup_database_to_tg())
    except Exception as e:
        await status.edit(f"❌ Indexing failed: {e}")

@bot.on(events.NewMessage(pattern="/stats"))
async def stats_handler(event):
    if event.sender_id not in AUTH_USERS:
        return
    users = get_all_users()
    await event.reply(f"📊 **Bot Statistics**\n\n👥 Total Users: **{len(users)}**")

@bot.on(events.NewMessage(pattern="/broadcast"))
async def broadcast_handler(event):
    if event.sender_id not in AUTH_USERS:
        return
    msg = event.text.replace("/broadcast", "").strip()
    if not msg:
        return await event.reply("❌ Usage: `/broadcast Hello everyone!`")

    users = get_all_users()
    sent = 0
    status = await event.reply(f"🚀 Broadcasting to {len(users)} users...")

    for user in users:
        try:
            await bot.send_message(user, msg)
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass

    await status.edit(f"✅ **Broadcast Complete!**\nDelivered to: {sent}/{len(users)}")

@bot.on(events.NewMessage(pattern="/tmdb"))
async def tmdb_handler(event):
    if event.sender_id not in AUTH_USERS:
        return
    query = event.text.replace("/tmdb", "").strip()
    if not query:
        return await event.reply("❌ Usage: `/tmdb Inception`")
    if not TMDB_API_KEY:
        return await event.reply("❌ TMDB_API_KEY is missing in ENV.")

    status = await event.reply("🔍 Fetching from TMDB...")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={query}"
            async with session.get(url) as r:
                data = await r.json()

            if not data.get("results"):
                return await status.edit("❌ Movie not found.")

            movie = data["results"][0]
            title = movie.get("title")
            year = movie.get("release_date", "").split("-")[0]
            rating = movie.get("vote_average", "N/A")
            overview = movie.get("overview", "No summary.")
            poster_path = movie.get("poster_path")
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

@bot.on(events.NewMessage(pattern="/add"))
async def add_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS:
        return

    reply = await event.get_reply_message()
    if not reply or not reply.media:
        return await event.reply("❌ Please reply to a video or file.")

    try:
        original_caption = reply.text or ""
        vault_msg = await bot.send_file(DB_CHANNEL_ID, reply.media, caption=original_caption)

        index_title = original_caption
        if reply.file and hasattr(reply.file, "name") and reply.file.name:
            index_title = f"{reply.file.name} {original_caption}"
        if not index_title.strip():
            index_title = f"Movie_File_{vault_msg.id}"

        add_vault_item(vault_msg.id, index_title)

        msg = (
            f"✅ **File Added & Indexed!**\n\n"
            f"📂 **Vault ID:** {vault_msg.id}\n"
            f"👇 Reply with Photo + `/post` to publish."
        )
        await event.reply(msg)
    except Exception as e:
        await event.reply(f"❌ Error Adding: {e}")

@bot.on(events.NewMessage(pattern="/postid"))
async def postid_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS:
        return

    args = event.text.split(" ", 2)
    if len(args) < 3:
        return await event.reply("❌ Usage:\n`/postid 1234 Your Movie Caption`")

    try:
        vault_id = int(args[1])
        caption = args[2].strip()
    except ValueError:
        return await event.reply("❌ ID must be a number.")

    vault_msg = await get_vault_message(vault_id)
    if not vault_msg:
        return await event.reply("❌ That ID is not a valid file in the storage channel.")

    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={vault_id}"

    buttons = []
    if WEBSITE_HOME:
        buttons.append([Button.url("🌍 Visit Website", url=WEBSITE_HOME)])
    buttons.append([Button.url("📂 Get File", url=deep_link)])

    poster = await event.download_media() if event.photo else None
    if not poster:
        reply = await event.get_reply_message()
        if reply and reply.photo:
            poster = await reply.download_media()

    try:
        public_msg = await send_public_card(caption, buttons, poster)
        save_public_mapping(public_msg.id, vault_id)
        await event.reply(f"✅ Published.\n🆔 Vault ID: `{vault_id}`")
    except Exception as e:
        await event.reply(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern="/postpack"))
async def postpack_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS:
        return

    args = event.text.split()
    if len(args) < 2:
        return await event.reply("❌ Usage: `/postpack 100-107 Caption`")

    range_str = args[1]
    try:
        start_id, end_id = range_str.split("-")
    except Exception:
        return await event.reply("❌ Invalid Format. Use `100-107`")

    caption = event.text.replace("/postpack", "").replace(range_str, "").strip() or "🎬 **New Season Pack!**"

    me = await bot.get_me()
    pack_link = f"https://t.me/{me.username}?start=pack_{start_id}_{end_id}"

    buttons = [[Button.url("📂 Get Full Season", url=pack_link)]]
    if WEBSITE_HOME:
        buttons.insert(0, [Button.url("🌍 Visit Website", url=WEBSITE_HOME)])

    poster = await event.download_media() if event.photo else None

    try:
        ok_count = 0
        for item_id in range(int(start_id), int(end_id) + 1):
            if await get_vault_message(item_id):
                ok_count += 1

        if ok_count == 0:
            return await event.reply("❌ None of those IDs exist in the storage channel.")

        public_msg = await send_public_card(caption, buttons, poster)
        save_public_mapping(public_msg.id, int(start_id))
        await event.reply(f"✅ Pack Published!\n🔗 **Link:** {pack_link}")
    except Exception as e:
        await event.reply(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern="/post"))
async def post_handler(event):
    if "/postpack" in event.text or "/postid" in event.text:
        return

    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS:
        return

    reply = await event.get_reply_message()
    if not reply:
        return await event.reply("⚠️ Reply to a Vault ID message.")

    vault_id = None
    for line in (reply.text or "").split("\n"):
        if "Vault ID:" in line:
            vault_id = re.sub(r"[^0-9]", "", line.split("Vault ID:")[1])

    if not vault_id:
        return await event.reply("❌ Invalid: No Vault ID found.")

    vault_msg = await get_vault_message(int(vault_id))
    if not vault_msg:
        return await event.reply("❌ That Vault ID does not exist in the storage channel.")

    caption = event.text.replace("/post", "").strip() or "🎬 **New Movie Uploaded!**"

    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={vault_id}"

    buttons = []
    if WEBSITE_HOME:
        buttons.append([Button.url("🌍 Visit Website", url=WEBSITE_HOME)])
    buttons.append([Button.url("📂 Get File", url=deep_link)])

    poster = await event.download_media() if event.photo else None

    try:
        public_msg = await send_public_card(caption, buttons, poster)
        save_public_mapping(public_msg.id, int(vault_id))
        await event.reply(f"✅ Published!\n🆔 Vault ID: `{vault_id}`")
    except Exception as e:
        await event.reply(f"❌ Error: {e}")

# ==========================================
# 🧠 CORE PROCESSOR
# ==========================================
async def process_task(event, source, name, thumb_path):
    status_msg = await event.reply(f"⏳ **Initializing:** `{name}`...")
    last_time = [0]
    current_thumb = thumb_path

    try:
        if isinstance(source, str):
            await status_msg.edit("🚀 **Downloading URL...**")
            headers = {"User-Agent": "Mozilla/5.0"}
            timeout = aiohttp.ClientTimeout(total=3600)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(source, headers=headers) as resp:
                    if resp.status == 200:
                        total = int(resp.headers.get("content-length", 0))
                        current = 0
                        async with aiofiles.open(name, mode="wb") as f:
                            async for chunk in resp.content.iter_chunked(10 * 1024 * 1024):
                                await f.write(chunk)
                                current += len(chunk)
                                await progress_bar(current, total, status_msg, "⬇️ **Downloading...**", last_time)
                    else:
                        await status_msg.edit(f"❌ Error: Server returned {resp.status}")
                        return False
        else:
            await status_msg.edit("📥 **Downloading from Telegram...**")

            async def dl_callback(c, t):
                await progress_bar(c, t, status_msg, "📥 **Downloading...**", last_time)

            success = await smart_download(bot, source, name, dl_callback)
            if not success:
                return False

        if not current_thumb:
            generated_thumb = f"{name}_thumb.jpg"
            cmd = ["ffmpeg", "-i", name, "-ss", "00:00:05", "-vframes", "1", generated_thumb, "-y"]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                process.kill()

            if os.path.exists(generated_thumb):
                current_thumb = generated_thumb

        await status_msg.edit("⚡ **Uploading to Vault...**")
        last_time = [0]

        async def up_callback(c, t):
            await progress_bar(c, t, status_msg, "☁️ **Uploading to Vault...**", last_time)

        try:
            vault_msg = await bot.send_file(
                DB_CHANNEL_ID,
                file=name,
                caption=f"🔒 {name}",
                thumb=current_thumb,
                supports_streaming=True,
                progress_callback=up_callback
            )
            add_vault_item(vault_msg.id, name)
        except Exception:
            return False

        stream_url = f"{BASE_URL}/stream/{vault_msg.id}" if BASE_URL else "N/A"
        final_msg = (
            f"✅ **Mirror Complete!**\n\n"
            f"📂 **Vault ID:** {vault_msg.id}\n"
            f"🌐 **Stream:** {stream_url}\n\n"
            f"👇 Reply with Photo + `/post` to publish."
        )
        await status_msg.edit(final_msg)
        return True

    except Exception as e:
        await status_msg.edit(f"❌ Error: {str(e)}")
        return False

    finally:
        if os.path.exists(name):
            try:
                os.remove(name)
            except Exception:
                pass
        if current_thumb and current_thumb != thumb_path and os.path.exists(current_thumb):
            try:
                os.remove(current_thumb)
            except Exception:
                pass

@bot.on(events.NewMessage(pattern="/mirror"))
async def handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS:
        return

    reply = await event.get_reply_message()
    batch_thumb = await event.download_media() if event.photo else (await reply.download_media() if reply and reply.photo else None)

    tasks = []
    if reply and (reply.video or reply.document) and not reply.photo:
        parts = event.text.split(" ", 1)
        new_name = parts[1] if len(parts) > 1 else f"Video_{int(time.time())}.mp4"
        tasks.append((reply, new_name))
    else:
        parts = event.text.split()
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                tasks.append((parts[i], parts[i + 1]))

    if not tasks:
        return await event.reply("Usage: `/mirror link1 name1 link2 name2...`")

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

@bot.on(events.NewMessage(pattern="/checkchannel"))
async def check_channel_handler(event):
    if event.sender_id not in AUTH_USERS:
        return
    try:
        channel = await bot.get_entity(DB_CHANNEL_ID)
        await event.reply(
            f"✅ **Storage Channel Verified!**\n\n"
            f"🏷️ **Name:** {channel.title}\n"
            f"🆔 **ID:** `{DB_CHANNEL_ID}`"
        )
    except Exception as e:
        await event.reply(f"❌ Could not find channel! Check your DB_CHANNEL_ID.\nError: {e}")

@bot.on(events.NewMessage(pattern="/checkdb"))
async def check_db_handler(event):
    if USE_POSTGRES:
        conn = get_pg_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM vault")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        await event.reply(f"🐘 Postgres Database contains {count} items in 'vault'.")
    else:
        sqlite_cursor.execute("SELECT COUNT(*) FROM vault")
        count = sqlite_cursor.fetchone()[0]
        await event.reply(f"💾 SQLite Database contains {count} items in 'vault'.")

async def startup_sequence():
    await sync_database_from_tg()
    if USE_POSTGRES:
        init_postgres()
    else:
        init_sqlite()

if __name__ == "__main__":
    bot.start(bot_token=BOT_TOKEN)
    bot.loop.run_until_complete(startup_sequence())
    bot.loop.create_task(start_web_server())
    bot.loop.create_task(worker())
    bot.run_until_disconnected()
