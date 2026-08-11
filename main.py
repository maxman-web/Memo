import os
import time
import asyncio
import aiohttp
import aiofiles
import re
import sqlite3
import mimetypes
from pathlib import Path
from datetime import datetime, timezone
from aiohttp import web
from telethon import TelegramClient, events, Button, functions, types
from telethon.network import ConnectionTcpIntermediate


# ============================================================
# OPTIONAL POSTGRES
# ============================================================
try:
    import psycopg2
    from psycopg2.pool import SimpleConnectionPool

    HAS_POSTGRES_LIB = True

except ImportError:
    psycopg2 = None
    SimpleConnectionPool = None
    HAS_POSTGRES_LIB = False


# ============================================================
# CONFIGURATION
# ============================================================
str_api_id = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

DOOD_KEY = os.environ.get("DOOD_KEY", "").strip()

WEBSITE_HOME = os.environ.get("WEBSITE_HOME", "").strip()
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").strip().rstrip("/")

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

AUTH_USERS_RAW = os.environ.get("AUTH_USERS", "").strip()

AUTH_USERS = [
    int(x.strip())
    for x in AUTH_USERS_RAW.split(",")
    if x.strip().isdigit()
]

FORCE_SUB_CHANNEL = os.environ.get(
    "FORCE_SUB_CHANNEL",
    ""
).strip()


# ============================================================
# OPTIONAL PERFORMANCE CONFIG
# ============================================================
QUEUE_WORKERS = max(
    1,
    int(
        os.environ.get(
            "QUEUE_WORKERS",
            "1"
        )
    )
)

STREAM_CONCURRENCY = max(
    1,
    int(
        os.environ.get(
            "STREAM_CONCURRENCY",
            "8"
        )
    )
)

STREAM_CHUNK_SIZE = max(
    64 * 1024,
    int(
        os.environ.get(
            "STREAM_CHUNK_SIZE",
            str(1024 * 1024)
        )
    )
)

AUTO_DELETE_SECONDS = max(
    30,
    int(
        os.environ.get(
            "AUTO_DELETE_SECONDS",
            "300"
        )
    )
)


if FORCE_SUB_CHANNEL.replace("-", "").isdigit():
    FORCE_SUB_CHANNEL = int(FORCE_SUB_CHANNEL)


# ============================================================
# CHANNEL IDs
#
# DB_CHANNEL_ID:
#   PRIVATE DESTINATION / REAL BOT VAULT
#
# PUBLIC_DB_CHANNEL_ID:
#   PUBLIC MIGRATION SOURCE
# ============================================================
def parse_id(value):
    value = str(value).strip()

    if not value:
        return 0

    if value.startswith("@"):
        return value

    try:
        return int(value)

    except ValueError:
        return 0


DB_CHANNEL_ID = parse_id(
    os.environ.get(
        "DB_CHANNEL_ID",
        "0"
    )
)

PUBLIC_DB_CHANNEL_ID = parse_id(
    os.environ.get(
        "PUBLIC_DB_CHANNEL_ID",
        "0"
    )
)


# ============================================================
# BASIC VALIDATION
# ============================================================
if not str_api_id:
    print("❌ API_ID is missing!")
    raise SystemExit(1)

if not API_HASH:
    print("❌ API_HASH is missing!")
    raise SystemExit(1)

if not BOT_TOKEN:
    print("❌ BOT_TOKEN is missing!")
    raise SystemExit(1)

if not DB_CHANNEL_ID:
    print("❌ DB_CHANNEL_ID is missing!")
    raise SystemExit(1)


try:
    API_ID = int(str_api_id)

except ValueError:
    print("❌ API_ID must be a number!")
    raise SystemExit(1)


# ============================================================
# VIDEO FORMAT POLICY
#
# ONLY THESE EXTENSIONS ARE ACCEPTED.
#
# Images:
#   jpg/png/webp/gif -> rejected
#
# Documents:
#   pdf/zip/doc/txt -> rejected
#
# Subtitles:
#   srt/ass/sub -> rejected
# ============================================================
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".m4v",
    ".ts",
    ".mts",
    ".m2ts",
    ".mpeg",
    ".mpg",
    ".3gp",
    ".3g2",
    ".flv",
    ".wmv",
    ".asf",
    ".ogv",
    ".vob",
    ".rm",
    ".rmvb",
}

VIDEO_MIME_TYPES = {
    "video/mp4",
    "video/x-matroska",
    "video/webm",
    "video/quicktime",
    "video/x-msvideo",
    "video/x-ms-wmv",
    "video/mpeg",
    "video/3gpp",
    "video/x-flv",
    "video/ogg",
    "video/mp2t",
}


# ============================================================
# QUEUES / LOCKS
# ============================================================
WORK_QUEUE = asyncio.Queue()

MIGRATION_LOCK = asyncio.Lock()

STREAM_SEMAPHORE = asyncio.Semaphore(
    STREAM_CONCURRENCY
)


# ============================================================
# TELEGRAM CLIENT
# ============================================================
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


# ============================================================
# HELPERS
# ============================================================
def now_utc():
    return datetime.now(timezone.utc)


def human_size(size):
    if size is None:
        return "Unknown"

    try:
        size = float(size)

    except Exception:
        return "Unknown"

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB"
    ]

    for unit in units:

        if size < 1024:
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size:.2f} PB"


def sanitize_filename(name):
    """
    Make filenames safe for local storage.
    """

    if not name:
        return "video.mp4"

    name = str(name).strip()

    # Remove path traversal
    name = name.replace(
        "\\",
        "_"
    )

    name = name.replace(
        "/",
        "_"
    )

    name = re.sub(
        r"[\x00-\x1f\x7f]",
        "_",
        name
    )

    name = re.sub(
        r'[<>:"|?*]',
        "_",
        name
    )

    name = re.sub(
        r"\s+",
        " ",
        name
    ).strip()

    if not name:
        name = "video.mp4"

    return name[:240]


def get_extension(name):
    if not name:
        return ""

    return Path(
        str(name).strip()
    ).suffix.lower()


def is_video_filename(name):
    """
    Strict video filename validation.
    """

    if not name:
        return False

    return (
        get_extension(name)
        in VIDEO_EXTENSIONS
    )


def is_video_mime(mime):
    if not mime:
        return False

    mime = str(
        mime
    ).lower().split(
        ";",
        1
    )[0].strip()

    return mime.startswith(
        "video/"
    ) or mime in VIDEO_MIME_TYPES


def is_video_message(msg):
    """
    Strict Telegram media validation.

    A Telegram document is accepted only when:
      1. It has a known video extension, OR
      2. Telegram explicitly reports a video MIME type.

    Photos and text are NEVER accepted.
    """

    if not msg:
        return False

    # Telegram native video
    if getattr(msg, "video", None):
        return True

    document = getattr(
        msg,
        "document",
        None
    )

    if not document:
        return False

    mime_type = getattr(
        document,
        "mime_type",
        None
    )

    if is_video_mime(
        mime_type
    ):
        return True

    file_name = get_message_filename(
        msg
    )

    return is_video_filename(
        file_name
    )


# ============================================================
# SMART FILENAME / META PARSER
# ============================================================
def parse_media_meta(file_name):

    if not file_name:

        return (
            "Unknown File",
            "movie",
            None,
            None
        )

    clean_name = re.sub(
        r"[._-]",
        " ",
        str(file_name)
    ).strip()

    clean_name = re.sub(
        r"\s+",
        " ",
        clean_name
    )

    # --------------------------------------------------------
    # S01E02
    # --------------------------------------------------------
    match = re.search(
        r"(.*?)\bS(\d{1,2})\s*E(\d{1,3})\b",
        clean_name,
        re.IGNORECASE
    )

    if match:

        title = match.group(
            1
        ).strip()

        return (
            title,
            "series",
            int(match.group(2)),
            int(match.group(3))
        )

    # --------------------------------------------------------
    # 1x02
    # --------------------------------------------------------
    match = re.search(
        r"(.*?)\b(\d{1,2})x(\d{1,3})\b",
        clean_name,
        re.IGNORECASE
    )

    if match:

        title = match.group(
            1
        ).strip()

        return (
            title,
            "series",
            int(match.group(2)),
            int(match.group(3))
        )

    # --------------------------------------------------------
    # Episode 02 / Ep 02
    # --------------------------------------------------------
    match = re.search(
        r"(.*?)\b(?:Ep|Episode)\s*(\d{1,3})\b",
        clean_name,
        re.IGNORECASE
    )

    if match:

        title = match.group(
            1
        ).strip()

        return (
            title,
            "series",
            1,
            int(match.group(2))
        )

    # --------------------------------------------------------
    # Remove common release metadata
    # --------------------------------------------------------
    movie_title = re.sub(
        r"\b("
        r"1080p|720p|2160p|4320p|4k|"
        r"hdr|hdr10|web\s*dl|webdl|web-rip|webrip|"
        r"bluray|blu-ray|brrip|dvdrip|"
        r"x264|h264|x265|h265|hevc|av1|"
        r"hindi|english|dual\s*audio|multi\s*audio|"
        r"proper|repack|remux"
        r")\b.*",
        "",
        clean_name,
        flags=re.IGNORECASE
    ).strip()

    return (
        movie_title or clean_name,
        "movie",
        None,
        None
    )


# ============================================================
# DATABASE
# ============================================================
USE_POSTGRES = bool(
    DATABASE_URL
    and HAS_POSTGRES_LIB
)


PG_POOL = None


# ============================================================
# POSTGRES
# ============================================================
if USE_POSTGRES:

    print("🐘 Database Mode: PostgreSQL")

    try:

        PG_MIN = max(
            1,
            int(
                os.environ.get(
                    "PG_MIN_CONNECTIONS",
                    "1"
                )
            )
        )

        PG_MAX = max(
            PG_MIN,
            int(
                os.environ.get(
                    "PG_MAX_CONNECTIONS",
                    "5"
                )
            )
        )

        PG_POOL = SimpleConnectionPool(
            PG_MIN,
            PG_MAX,
            DATABASE_URL
        )

        conn = PG_POOL.getconn()

        try:

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        created_at TIMESTAMPTZ
                        DEFAULT NOW()
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault (
                        msg_id BIGINT PRIMARY KEY,
                        file_name TEXT,
                        title TEXT,
                        media_type TEXT,
                        season INTEGER,
                        episode INTEGER,
                        created_at TIMESTAMPTZ
                        DEFAULT NOW()
                    )
                    """
                )

                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vault_map (
                        public_msg_id BIGINT PRIMARY KEY,
                        vault_msg_id BIGINT NOT NULL
                    )
                    """
                )

                cursor.execute(
                    """
                    ALTER TABLE vault
                    ADD COLUMN IF NOT EXISTS
                    title TEXT
                    """
                )

                cursor.execute(
                    """
                    ALTER TABLE vault
                    ADD COLUMN IF NOT EXISTS
                    media_type TEXT
                    """
                )

                cursor.execute(
                    """
                    ALTER TABLE vault
                    ADD COLUMN IF NOT EXISTS
                    season INTEGER
                    """
                )

                cursor.execute(
                    """
                    ALTER TABLE vault
                    ADD COLUMN IF NOT EXISTS
                    episode INTEGER
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_vault_title
                    ON vault(title)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_vault_media_type
                    ON vault(media_type)
                    """
                )

                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_vault_season_episode
                    ON vault(
                        season,
                        episode
                    )
                    """
                )

                conn.commit()

        finally:

            PG_POOL.putconn(
                conn
            )

        print(
            "✅ PostgreSQL tables ready."
        )

    except Exception as e:

        print(
            f"❌ PostgreSQL initialization failed: {e}"
        )

        USE_POSTGRES = False
        PG_POOL = None


# ============================================================
# SQLITE
# ============================================================
if not USE_POSTGRES:

    print(
        "💾 Database Mode: SQLite + Telegram Backup"
    )

    sqlite_conn = sqlite3.connect(
        "bot_users.db",
        check_same_thread=False,
        timeout=30
    )

    sqlite_conn.row_factory = sqlite3.Row

    sqlite_cursor = sqlite_conn.cursor()

    try:

        sqlite_cursor.execute(
            "PRAGMA journal_mode=WAL"
        )

        sqlite_cursor.execute(
            "PRAGMA synchronous=NORMAL"
        )

        sqlite_cursor.execute(
            "PRAGMA busy_timeout=30000"
        )

    except Exception:
        pass

    sqlite_cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT
            DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    sqlite_cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault (
            msg_id INTEGER PRIMARY KEY,
            file_name TEXT,
            title TEXT,
            media_type TEXT,
            season INTEGER,
            episode INTEGER,
            created_at TEXT
            DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    sqlite_cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vault_map (
            public_msg_id INTEGER PRIMARY KEY,
            vault_msg_id INTEGER NOT NULL
        )
        """
    )

    sqlite_cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_vault_title
        ON vault(title)
        """
    )

    sqlite_cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_vault_media_type
        ON vault(media_type)
        """
    )

    sqlite_cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_vault_season_episode
        ON vault(
            season,
            episode
        )
        """
    )

    sqlite_cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_vault_map_vault
        ON vault_map(vault_msg_id)
        """
    )

    sqlite_conn.commit()


# ============================================================
# DATABASE CONNECTION HELPERS
# ============================================================
def get_pg_conn():

    if not PG_POOL:
        raise RuntimeError(
            "PostgreSQL pool is unavailable."
        )

    return PG_POOL.getconn()


def release_pg_conn(conn):

    if PG_POOL and conn:
        PG_POOL.putconn(
            conn
        )


# ============================================================
# BOT-SAFE MESSAGE LOOKUP
#
# Telegram flatly refuses GetHistoryRequest (history browsing,
# offset_date iteration, search) for BOT accounts, no matter
# what rights the bot has in the channel. It raises:
#
#   "The API access for bot users is restricted..."
#
# What bots CAN do is fetch specific message IDs directly
# (channels.GetMessages). Everything below is built only on
# top of that, so it works from a bot session.
# ============================================================
async def get_message_by_id(chat, msg_id):

    try:
        return await bot.get_messages(
            chat,
            ids=msg_id
        )

    except Exception:
        return None


async def get_messages_by_ids(chat, msg_ids):
    """
    Batched version of get_message_by_id. Telegram accepts a
    list of IDs in one call, which is far cheaper than one
    call per ID when scanning a whole channel.
    """

    if not msg_ids:
        return []

    try:

        messages = await bot.get_messages(
            chat,
            ids=list(msg_ids)
        )

        if not isinstance(messages, list):
            messages = [messages]

        return messages

    except Exception as e:

        print(
            f"Batch fetch failed for "
            f"{msg_ids[0]}-{msg_ids[-1]}: {e}"
        )

        return [None] * len(msg_ids)


async def find_message_near_id(chat, msg_id, max_probe=5):
    """
    get_messages(ids=X) returns None for a deleted/nonexistent
    ID. Probe a few IDs outward to find a real message near the
    one requested, so gaps don't break boundary searches.
    """

    msg = await get_message_by_id(chat, msg_id)

    if msg:
        return msg

    for offset in range(1, max_probe + 1):

        for candidate in (msg_id + offset, msg_id - offset):

            if candidate < 1:
                continue

            msg = await get_message_by_id(chat, candidate)

            if msg:
                return msg

    return None


async def find_latest_message_id(chat, cap=2_000_000):
    """
    Bots can't ask Telegram "what's the newest message" via any
    history API. Instead, probe IDs directly (allowed for bots),
    expanding exponentially until messages stop existing, then
    binary-search the exact boundary.
    """

    lo, hi, last_found = 1, 1, None

    while hi < cap:

        if await find_message_near_id(chat, hi, max_probe=0):

            last_found = hi
            lo = hi
            hi *= 2

            await asyncio.sleep(0.05)

        else:
            break

    if last_found is None:
        return None

    low, high = lo, hi

    while low < high - 1:

        mid = (low + high) // 2

        if await find_message_near_id(chat, mid, max_probe=0):
            low = mid
        else:
            high = mid

        await asyncio.sleep(0.05)

    return low


async def find_id_for_date(chat, target_date, hi_bound):
    """
    Binary search message IDs for the first one whose date is
    >= target_date. Relies on IDs increasing with time, which
    holds for a normal (non-merged) channel.
    """

    lo, hi, result = 1, hi_bound, hi_bound

    while lo <= hi:

        mid = (lo + hi) // 2

        msg = await find_message_near_id(chat, mid)

        if not msg or not msg.date:
            hi = mid - 1
            continue

        msg_date = msg.date

        if msg_date.tzinfo is None:

            msg_date = msg_date.replace(
                tzinfo=timezone.utc
            )

        if msg_date < target_date:
            lo = mid + 1
        else:
            result = mid
            hi = mid - 1

        await asyncio.sleep(0.05)

    return result


async def find_backup_message(
    chat,
    latest_id,
    tag="#USER_BACKUP",
    batch_size=100,
    max_batches=200
):
    """
    Replaces bot.iter_messages(chat, search=tag) which bots
    can't use. Scans backward in ID batches (newest first,
    since backups are re-uploaded periodically) looking for a
    document whose caption contains `tag`.
    """

    if not latest_id:
        return None

    end = latest_id
    batches_checked = 0

    while end >= 1 and batches_checked < max_batches:

        start = max(1, end - batch_size + 1)
        batch_ids = list(range(start, end + 1))

        messages = await get_messages_by_ids(chat, batch_ids)

        for msg in reversed(messages):

            if (
                msg
                and msg.document
                and msg.text
                and tag in msg.text
            ):
                return msg

        end = start - 1
        batches_checked += 1

        await asyncio.sleep(0.2)

    return None


# ============================================================
# TELEGRAM DATABASE BACKUP
# ============================================================
async def sync_database_from_tg():

    if USE_POSTGRES:
        return

    try:

        print(
            "🔄 Looking for SQLite backup..."
        )

        backup_path = (
            "bot_users.restore.db"
        )

        latest_id = await find_latest_message_id(
            DB_CHANNEL_ID
        )

        msg = await find_backup_message(
            DB_CHANNEL_ID,
            latest_id
        )

        if not msg:

            print(
                "ℹ️ No previous database backup found."
            )

            return

        await bot.download_media(
            msg.document,
            file=backup_path
        )

        if not os.path.exists(
            backup_path
        ):
            return

        # Validate SQLite database before replacing
        try:

            test_conn = sqlite3.connect(
                backup_path
            )

            test_conn.execute(
                "PRAGMA integrity_check"
            )

            test_conn.close()

        except Exception as e:

            print(
                f"❌ Backup validation failed: {e}"
            )

            try:
                os.remove(
                    backup_path
                )
            except Exception:
                pass

            return

        # Close current connection first.
        try:
            sqlite_conn.close()
        except Exception:
            pass

        os.replace(
            backup_path,
            "bot_users.db"
        )

        print(
            "✅ Database backup restored."
        )

    except Exception as e:

        print(
            f"❌ Database sync failed: {e}"
        )


async def backup_database_to_tg():

    if USE_POSTGRES:
        return

    try:

        if not os.path.exists(
            "bot_users.db"
        ):
            return

        # Checkpoint WAL so the main DB contains
        # recent changes before uploading it.
        try:

            sqlite_cursor.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            )

            sqlite_conn.commit()

        except Exception:
            pass

        latest_id = await find_latest_message_id(
            DB_CHANNEL_ID
        )

        old_backup = await find_backup_message(
            DB_CHANNEL_ID,
            latest_id
        )

        if old_backup:

            try:
                await old_backup.delete()

            except Exception:
                pass

        await bot.send_file(
            DB_CHANNEL_ID,
            "bot_users.db",
            caption=(
                "💾 #USER_BACKUP\n"
                "DO NOT DELETE."
            )
        )

        print(
            "✅ SQLite backup uploaded."
        )

    except Exception as e:

        print(
            f"❌ Backup upload failed: {e}"
        )


# ============================================================
# USER DATABASE
# ============================================================
def add_user(user_id):

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO users (
                        user_id
                    )
                    VALUES (%s)
                    ON CONFLICT (user_id)
                    DO NOTHING
                    """,
                    (user_id,)
                )

                conn.commit()

        except Exception as e:

            print(
                f"PostgreSQL user error: {e}"
            )

            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass

        finally:

            release_pg_conn(
                conn
            )

        return

    try:

        sqlite_cursor.execute(
            """
            INSERT OR IGNORE INTO users (
                user_id
            )
            VALUES (?)
            """,
            (user_id,)
        )

        changed = (
            sqlite_cursor.rowcount > 0
        )

        sqlite_conn.commit()

        if changed:

            try:

                asyncio.create_task(
                    backup_database_to_tg()
                )

            except Exception:
                pass

    except Exception as e:

        print(
            f"SQLite user error: {e}"
        )


def get_all_users():

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT user_id
                    FROM users
                    ORDER BY user_id
                    """
                )

                return [
                    row[0]
                    for row in cursor.fetchall()
                ]

        except Exception as e:

            print(
                f"PostgreSQL fetch error: {e}"
            )

            return []

        finally:

            release_pg_conn(
                conn
            )

    try:

        sqlite_cursor.execute(
            """
            SELECT user_id
            FROM users
            """
        )

        return [
            row[0]
            for row in sqlite_cursor.fetchall()
        ]

    except Exception as e:

        print(
            f"SQLite fetch error: {e}"
        )

        return []


def get_user_count():

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    "SELECT COUNT(*) FROM users"
                )

                return cursor.fetchone()[0]

        except Exception:
            return 0

        finally:
            release_pg_conn(conn)

    try:

        sqlite_cursor.execute(
            "SELECT COUNT(*) FROM users"
        )

        return sqlite_cursor.fetchone()[0]

    except Exception:
        return 0


# ============================================================
# VAULT INDEX
# ============================================================
def add_vault_item(
    msg_id,
    file_name
):

    if not is_video_filename(
        file_name
    ):

        print(
            f"⚠️ Rejected non-video: {file_name}"
        )

        return False

    title, media_type, season, episode = (
        parse_media_meta(
            file_name
        )
    )

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO vault (
                        msg_id,
                        file_name,
                        title,
                        media_type,
                        season,
                        episode
                    )
                    VALUES (
                        %s, %s, %s,
                        %s, %s, %s
                    )

                    ON CONFLICT (msg_id)
                    DO UPDATE SET
                        file_name =
                            EXCLUDED.file_name,
                        title =
                            EXCLUDED.title,
                        media_type =
                            EXCLUDED.media_type,
                        season =
                            EXCLUDED.season,
                        episode =
                            EXCLUDED.episode
                    """,
                    (
                        msg_id,
                        file_name,
                        title,
                        media_type,
                        season,
                        episode
                    )
                )

                conn.commit()

            return True

        except Exception as e:

            print(
                f"PostgreSQL vault error: {e}"
            )

            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass

            return False

        finally:

            release_pg_conn(
                conn
            )

    try:

        sqlite_cursor.execute(
            """
            INSERT OR REPLACE INTO vault (
                msg_id,
                file_name,
                title,
                media_type,
                season,
                episode
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                msg_id,
                file_name,
                title,
                media_type,
                season,
                episode
            )
        )

        sqlite_conn.commit()

        return True

    except Exception as e:

        print(
            f"SQLite vault error: {e}"
        )

        return False


def search_vault(query):

    words = query.strip().split()

    if not words:
        return []

    # Don't allow non-video results.
    if USE_POSTGRES:

        conn = None

        try:

            conditions = " AND ".join(
                [
                    """
                    (
                        title ILIKE %s
                        OR file_name ILIKE %s
                    )
                    """
                    for _ in words
                ]
            )

            params = []

            for word in words:

                params.extend(
                    [
                        f"%{word}%",
                        f"%{word}%"
                    ]
                )

            sql = f"""
                SELECT msg_id
                FROM vault
                WHERE media_type IN (
                    'movie',
                    'series'
                )
                AND {conditions}
                ORDER BY
                    media_type DESC,
                    title ASC,
                    season ASC,
                    episode ASC NULLS LAST
                LIMIT 8
            """

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    sql,
                    tuple(params)
                )

                return [
                    row[0]
                    for row in cursor.fetchall()
                ]

        except Exception as e:

            print(
                f"PostgreSQL search error: {e}"
            )

            return []

        finally:

            release_pg_conn(
                conn
            )

    try:

        conditions = " AND ".join(
            [
                """
                (
                    title LIKE ?
                    OR file_name LIKE ?
                )
                """
                for _ in words
            ]
        )

        params = []

        for word in words:

            params.extend(
                [
                    f"%{word}%",
                    f"%{word}%"
                ]
            )

        sql = f"""
            SELECT msg_id
            FROM vault
            WHERE media_type IN (
                'movie',
                'series'
            )
            AND {conditions}
            ORDER BY
                media_type DESC,
                title ASC,
                season ASC,
                episode ASC
            LIMIT 8
        """

        sqlite_cursor.execute(
            sql,
            tuple(params)
        )

        return [
            row[0]
            for row in sqlite_cursor.fetchall()
        ]

    except Exception as e:

        print(
            f"SQLite search error: {e}"
        )

        return []


def get_vault_record(msg_id):

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT
                        msg_id,
                        file_name,
                        title,
                        media_type,
                        season,
                        episode
                    FROM vault
                    WHERE msg_id = %s
                    """,
                    (msg_id,)
                )

                row = cursor.fetchone()

                return row

        except Exception:
            return None

        finally:
            release_pg_conn(conn)

    try:

        sqlite_cursor.execute(
            """
            SELECT
                msg_id,
                file_name,
                title,
                media_type,
                season,
                episode
            FROM vault
            WHERE msg_id = ?
            """,
            (msg_id,)
        )

        return sqlite_cursor.fetchone()

    except Exception:
        return None


def get_vault_count():

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM vault
                    WHERE media_type IN (
                        'movie',
                        'series'
                    )
                    """
                )

                return cursor.fetchone()[0]

        except Exception:
            return 0

        finally:
            release_pg_conn(conn)

    try:

        sqlite_cursor.execute(
            """
            SELECT COUNT(*)
            FROM vault
            WHERE media_type IN (
                'movie',
                'series'
            )
            """
        )

        return sqlite_cursor.fetchone()[0]

    except Exception:
        return 0


# ============================================================
# FILE HELPERS
# ============================================================
def get_message_filename(msg):

    try:

        if getattr(
            msg,
            "file",
            None
        ):

            name = getattr(
                msg.file,
                "name",
                None
            )

            if name:
                return str(name)

    except Exception:
        pass

    try:

        document = getattr(
            msg,
            "document",
            None
        )

        if document:

            for attr in document.attributes:

                if isinstance(
                    attr,
                    types.DocumentAttributeFilename
                ):

                    return attr.file_name

    except Exception:
        pass

    return ""


def get_message_filename_or_fallback(msg):

    name = get_message_filename(
        msg
    )

    if name:

        return sanitize_filename(
            name
        )

    # Native Telegram video may not have
    # a filename. Give it a safe MP4 name.
    if getattr(
        msg,
        "video",
        None
    ):

        return (
            f"Video_{msg.id}.mp4"
        )

    return ""


# ============================================================
# VAULT MESSAGE
# ============================================================
async def get_vault_message(
    vault_id
):

    try:

        msg = await bot.get_messages(
            DB_CHANNEL_ID,
            ids=vault_id
        )

        if (
            msg
            and msg.media
            and is_video_message(msg)
        ):

            return msg

    except Exception as e:

        print(
            f"Vault lookup failed "
            f"for {vault_id}: {e}"
        )

    return None


# ============================================================
# PUBLIC -> PRIVATE MAPPING
# ============================================================
def save_public_mapping(
    public_msg_id,
    vault_msg_id
):

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    INSERT INTO vault_map (
                        public_msg_id,
                        vault_msg_id
                    )
                    VALUES (%s, %s)

                    ON CONFLICT (
                        public_msg_id
                    )
                    DO UPDATE SET
                        vault_msg_id =
                        EXCLUDED.vault_msg_id
                    """,
                    (
                        public_msg_id,
                        vault_msg_id
                    )
                )

                conn.commit()

        except Exception as e:

            print(
                f"Mapping error: {e}"
            )

            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass

        finally:

            release_pg_conn(conn)

        return

    try:

        sqlite_cursor.execute(
            """
            INSERT OR REPLACE INTO
            vault_map (
                public_msg_id,
                vault_msg_id
            )
            VALUES (?, ?)
            """,
            (
                public_msg_id,
                vault_msg_id
            )
        )

        sqlite_conn.commit()

    except Exception as e:

        print(
            f"Mapping error: {e}"
        )


def get_vault_id_from_public(
    public_msg_id
):

    if USE_POSTGRES:

        conn = None

        try:

            conn = get_pg_conn()

            with conn.cursor() as cursor:

                cursor.execute(
                    """
                    SELECT vault_msg_id
                    FROM vault_map
                    WHERE public_msg_id = %s
                    """,
                    (public_msg_id,)
                )

                row = cursor.fetchone()

                return (
                    row[0]
                    if row
                    else None
                )

        except Exception:
            return None

        finally:
            release_pg_conn(conn)

    try:

        sqlite_cursor.execute(
            """
            SELECT vault_msg_id
            FROM vault_map
            WHERE public_msg_id = ?
            """,
            (public_msg_id,)
        )

        row = sqlite_cursor.fetchone()

        return (
            row[0]
            if row
            else None
        )

    except Exception:
        return None


def is_public_message_migrated(
    public_msg_id
):

    return (
        get_vault_id_from_public(
            public_msg_id
        )
        is not None
    )


# ============================================================
# PUBLIC CARD
# ============================================================
async def send_public_card(
    caption,
    buttons,
    poster_path=None
):

    if not FORCE_SUB_CHANNEL:

        raise RuntimeError(
            "FORCE_SUB_CHANNEL is not configured, "
            "so there is no public channel to post "
            "cards to."
        )

    if poster_path:

        try:

            public_msg = await bot.send_file(
                FORCE_SUB_CHANNEL,
                poster_path,
                caption=caption,
                buttons=buttons
            )

        finally:

            try:

                if os.path.exists(
                    poster_path
                ):

                    os.remove(
                        poster_path
                    )

            except Exception:
                pass

    else:

        public_msg = await bot.send_message(
            FORCE_SUB_CHANNEL,
            caption,
            buttons=buttons
        )

    return public_msg


# ============================================================
# DOOD LINK
# ============================================================
def fix_dood_link(link):

    if not link:
        return None

    bad_domains = [
        "dsvplay.com",
        "dood.re",
        "dood.wf",
        "dood.cx",
        "dood.sh",
        "dood.pm",
        "dood.to",
        "dood.so",
        "dood.la"
    ]

    clean_link = link

    for domain in bad_domains:

        if domain in clean_link:

            clean_link = clean_link.replace(
                domain,
                "myvidplay.com"
            )

            break

    if (
        "myvidplay.com"
        not in clean_link
        and "http" in clean_link
    ):

        clean_link = re.sub(
            r"https?://[^/]+",
            "https://myvidplay.com",
            clean_link
        )

    return clean_link


# ============================================================
# AUTO DELETE
# ============================================================
async def auto_delete_task(
    event,
    messages,
    delay=AUTO_DELETE_SECONDS
):

    await asyncio.sleep(
        delay
    )

    try:

        for msg in messages:

            try:
                await msg.delete()

            except Exception:
                pass

        try:

            await event.respond(
                "⏱️ *Files auto-deleted for "
                "security. Request them again "
                "if needed!*"
            )

        except Exception:
            pass

    except Exception:
        pass


def get_file_size(message):
    """
    Best-effort file size lookup from a sent/received message.
    """

    try:

        if getattr(message, "file", None):

            size = getattr(
                message.file,
                "size",
                None
            )

            if size:
                return size

    except Exception:
        pass

    try:

        document = getattr(
            message,
            "document",
            None
        )

        if document:

            return getattr(
                document,
                "size",
                None
            )

    except Exception:
        pass

    return None


def auto_delete_delay_for_size(
    total_bytes,
    min_delay=AUTO_DELETE_SECONDS,
    max_delay=7200,
    assumed_speed_mbps=2,
    buffer_seconds=120
):
    """
    Scales the auto-delete window to the file size instead of
    using one fixed delay for everything.

    - assumed_speed_mbps is intentionally conservative (2 MB/s,
      ~16 Mbps) to cover slower/mobile connections rather than
      best-case wifi.
    - buffer_seconds adds slack on top of the raw download-time
      estimate so people aren't racing the clock.
    - min_delay is a floor: small files never get an
      uncomfortably short window (defaults to AUTO_DELETE_SECONDS,
      so it stays configurable via the existing env var).
    - max_delay is a ceiling so a huge file can't leave content
      sitting in a chat indefinitely, defeating the point of
      auto-delete.
    """

    if not total_bytes:
        return min_delay

    estimated_seconds = (
        total_bytes
        / (assumed_speed_mbps * 1024 * 1024)
    ) + buffer_seconds

    return max(
        min_delay,
        min(
            int(estimated_seconds),
            max_delay
        )
    )


# ============================================================
# FORCE SUB
# ============================================================
async def check_subscription(
    user_id
):

    if not FORCE_SUB_CHANNEL:
        return True

    if user_id in AUTH_USERS:
        return True

    try:

        await bot(
            functions.channels.GetParticipantRequest(
                channel=FORCE_SUB_CHANNEL,
                participant=user_id
            )
        )

        return True

    except Exception:

        return False


# ============================================================
# PROGRESS BAR
# ============================================================
async def progress_bar(
    current,
    total,
    status_msg,
    action_name,
    last_time_ref
):

    now = time.time()

    if (
        now - last_time_ref[0]
        < 5
    ):

        return

    if total:

        percentage = (
            current
            * 100
            / total
        )

    else:

        percentage = 0

    filled = int(
        percentage / 10
    )

    filled = min(
        max(
            filled,
            0
        ),
        10
    )

    bar = (
        "▓" * filled
        +
        "░" * (
            10 - filled
        )
    )

    try:

        await status_msg.edit(
            f"{action_name}\n"
            f"{bar} "
            f"**{percentage:.1f}%**\n"
            f"`{human_size(current)}`"
            + (
                f" / `{human_size(total)}`"
                if total
                else ""
            )
        )

        last_time_ref[0] = now

    except Exception:
        pass


# ============================================================
# SMART DOWNLOAD
# ============================================================
async def smart_download(
    client,
    message,
    filename,
    progress_callback
):

    if not is_video_message(
        message
    ):

        return False

    filename = sanitize_filename(
        filename
    )

    if not is_video_filename(
        filename
    ):

        return False

    try:

        await client.download_media(
            message,
            file=filename,
            progress_callback=progress_callback
        )

        if os.path.exists(
            filename
        ):

            return True

    except Exception as e:

        print(
            f"Primary Telegram download "
            f"failed: {e}"
        )

    try:

        if (
            hasattr(
                message,
                "media"
            )
            and hasattr(
                message.media,
                "document"
            )
        ):

            await client.download_media(
                message.media.document,
                file=filename,
                progress_callback=progress_callback
            )

            if os.path.exists(
                filename
            ):

                return True

    except Exception as e:

        print(
            f"Fallback Telegram download "
            f"failed: {e}"
        )

    return False


# ============================================================
# MIGRATION
# ============================================================
async def migrate_message(
    public_msg,
    status_msg=None
):

    if not public_msg:
        return False, None

    # STRICT VIDEO CHECK
    if not is_video_message(
        public_msg
    ):

        return False, None

    public_id = public_msg.id

    existing_id = (
        get_vault_id_from_public(
            public_id
        )
    )

    if existing_id:

        return (
            False,
            existing_id
        )

    file_name = (
        get_message_filename_or_fallback(
            public_msg
        )
    )

    if not is_video_filename(
        file_name
    ):

        return False, None

    try:

        forwarded = await bot.forward_messages(
            DB_CHANNEL_ID,
            public_id,
            from_peer=PUBLIC_DB_CHANNEL_ID
        )

        if isinstance(
            forwarded,
            list
        ):

            if not forwarded:
                return False, None

            vault_msg = forwarded[0]

        else:

            vault_msg = forwarded

        if not vault_msg:
            return False, None

        # Confirm the forwarded item is a video.
        if not is_video_message(
            vault_msg
        ):

            try:
                await vault_msg.delete()
            except Exception:
                pass

            return False, None

        if not add_vault_item(
            vault_msg.id,
            file_name
        ):

            return False, None

        save_public_mapping(
            public_id,
            vault_msg.id
        )

        return (
            True,
            vault_msg.id
        )

    except Exception as e:

        print(
            f"Migration failed for "
            f"{public_id}: {e}"
        )

        return (
            False,
            None
        )


def parse_migration_date(
    value
):

    try:

        return datetime.strptime(
            value,
            "%Y-%m-%d"
        ).replace(
            tzinfo=timezone.utc
        )

    except Exception:

        return None


async def migrate_by_range(
    start_id,
    end_id,
    status_msg
):

    if MIGRATION_LOCK.locked():

        await status_msg.edit(
            "⚠️ A migration is already running."
        )

        return

    async with MIGRATION_LOCK:

        migrated = 0
        skipped = 0
        failed = 0
        checked = 0

        try:

            if start_id < end_id:

                step_range = range(
                    start_id,
                    end_id + 1
                )

            else:

                step_range = range(
                    start_id,
                    end_id - 1,
                    -1
                )

            total = len(
                step_range
            )

            for msg_id in step_range:

                checked += 1

                if is_public_message_migrated(
                    msg_id
                ):

                    skipped += 1
                    continue

                try:

                    public_msg = await bot.get_messages(
                        PUBLIC_DB_CHANNEL_ID,
                        ids=msg_id
                    )

                except Exception as e:

                    print(
                        f"Could not read public "
                        f"ID {msg_id}: {e}"
                    )

                    failed += 1
                    continue

                if (
                    not public_msg
                    or not public_msg.media
                ):

                    skipped += 1
                    continue

                # Only migrate videos.
                if not is_video_message(
                    public_msg
                ):

                    skipped += 1
                    continue

                ok, private_id = (
                    await migrate_message(
                        public_msg,
                        status_msg
                    )
                )

                if ok:

                    migrated += 1

                elif private_id:

                    skipped += 1

                else:

                    failed += 1

                if (
                    checked % 10 == 0
                    or checked == total
                ):

                    await status_msg.edit(
                        "🔄 **Migration Running**\n\n"
                        f"📊 Checked: "
                        f"`{checked}/{total}`\n"
                        f"🎬 Videos: "
                        f"`{migrated}`\n"
                        f"⏭️ Skipped: "
                        f"`{skipped}`\n"
                        f"❌ Failed: "
                        f"`{failed}`"
                    )

                await asyncio.sleep(
                    0.15
                )

            await status_msg.edit(
                "✅ **Migration Complete**\n\n"
                f"📊 Checked: `{checked}`\n"
                f"🎬 Videos migrated: "
                f"`{migrated}`\n"
                f"⏭️ Skipped: `{skipped}`\n"
                f"❌ Failed: `{failed}`"
            )

        except Exception as e:

            await status_msg.edit(
                f"❌ Migration stopped:\n`{e}`"
            )


# ============================================================
# DUPLICATE UPDATE GUARD
#
# Telegram / Telethon's own retry logic can occasionally
# redeliver the same update more than once - after a reconnect,
# on a flaky connection, or if more than one instance of this
# bot is accidentally running against the same bot token (very
# common right after a redeploy on some hosts). This blocks a
# message from being handled twice.
#
# Registered as the very FIRST NewMessage handler: Telethon
# calls handlers in registration order, and raising
# StopPropagation here prevents every handler below it from
# running again for a message we've already processed.
# ============================================================
_SEEN_EVENTS = {}
_SEEN_EVENTS_LOCK = asyncio.Lock()
_SEEN_TTL_SECONDS = 60
_SEEN_MAX_ENTRIES = 2000


async def _is_duplicate_event(chat_id, msg_id):

    key = (chat_id, msg_id)
    now = time.time()

    async with _SEEN_EVENTS_LOCK:

        if len(_SEEN_EVENTS) > _SEEN_MAX_ENTRIES:

            cutoff = now - _SEEN_TTL_SECONDS

            for old_key, seen_at in list(
                _SEEN_EVENTS.items()
            ):

                if seen_at < cutoff:
                    del _SEEN_EVENTS[old_key]

        seen_at = _SEEN_EVENTS.get(key)

        if (
            seen_at is not None
            and (now - seen_at) < _SEEN_TTL_SECONDS
        ):
            return True

        _SEEN_EVENTS[key] = now

        return False


@bot.on(events.NewMessage())
async def duplicate_guard_handler(event):

    if await _is_duplicate_event(
        event.chat_id,
        event.id
    ):

        print(
            f"⚠️ Ignored duplicate update: "
            f"chat={event.chat_id} msg={event.id}"
        )

        raise events.StopPropagation


# ============================================================
# MIGRATION COMMAND
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/migrate(?:\s|$)"
    )
)
async def migrate_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    if not PUBLIC_DB_CHANNEL_ID:

        return await event.reply(
            "❌ `PUBLIC_DB_CHANNEL_ID` "
            "is not configured."
        )

    args = event.text.split()

    if len(args) < 2:

        return await event.reply(
            "📦 **Migration Usage**\n\n"
            "`/migrate 100-500`\n"
            "Migrate IDs 100 → 500.\n\n"
            "`/migrate 19417`\n"
            "Start at 19417 and migrate downward.\n\n"
            "`/migrate date 2026-01-01`\n"
            "Migrate from that date until now.\n\n"
            "`/migrate date 2026-01-01 2026-08-10`\n"
            "Migrate between two dates.\n\n"
            "🎬 Only video files are migrated."
        )

    # --------------------------------------------------------
    # DATE MIGRATION
    # --------------------------------------------------------
    if args[1].lower() == "date":

        if len(args) < 3:

            return await event.reply(
                "❌ Example:\n"
                "`/migrate date 2026-01-01`"
            )

        start_date = parse_migration_date(
            args[2]
        )

        if not start_date:

            return await event.reply(
                "❌ Invalid start date.\n"
                "Use `YYYY-MM-DD`."
            )

        end_date = None

        if len(args) >= 4:

            end_date = parse_migration_date(
                args[3]
            )

            if not end_date:

                return await event.reply(
                    "❌ Invalid end date.\n"
                    "Use `YYYY-MM-DD`."
                )

            end_date = end_date.replace(
                hour=23,
                minute=59,
                second=59
            )

        status = await event.reply(
            "🔍 **Locating message range "
            "for that date...**"
        )

        latest_id = await find_latest_message_id(
            PUBLIC_DB_CHANNEL_ID
        )

        if not latest_id:

            return await status.edit(
                "❌ Could not resolve any messages "
                "in the public DB."
            )

        start_id = await find_id_for_date(
            PUBLIC_DB_CHANNEL_ID,
            start_date,
            latest_id
        )

        end_id = (
            await find_id_for_date(
                PUBLIC_DB_CHANNEL_ID,
                end_date,
                latest_id
            )
            if end_date
            else latest_id
        )

        await status.edit(
            "🚀 **Starting migration**\n\n"
            f"📅 Resolved to ID range:\n"
            f"From: `{start_id}`\n"
            f"To: `{end_id}`"
        )

        asyncio.create_task(
            migrate_by_range(
                start_id,
                end_id,
                status
            )
        )

        return

    # --------------------------------------------------------
    # RANGE MIGRATION
    # --------------------------------------------------------
    value = args[1]

    if "-" in value:

        try:

            first, second = value.split(
                "-",
                1
            )

            start_id = int(first)
            end_id = int(second)

        except Exception:

            return await event.reply(
                "❌ Invalid range.\n"
                "Example: `/migrate 100-500`"
            )

    else:

        try:

            start_id = int(value)

        except ValueError:

            return await event.reply(
                "❌ Invalid ID."
            )

        status = await event.reply(
            "🔍 Finding oldest message "
            "in public DB..."
        )

        oldest_msg = await find_message_near_id(
            PUBLIC_DB_CHANNEL_ID,
            1,
            max_probe=50
        )

        if not oldest_msg:

            return await status.edit(
                "❌ Public DB is empty "
                "or unreachable."
            )

        end_id = oldest_msg.id

        await status.edit(
            f"🚀 **Starting downward migration**\n\n"
            f"From: `{start_id}`\n"
            f"To: `{end_id}`\n\n"
            "🎬 Video files only."
        )

        asyncio.create_task(
            migrate_by_range(
                start_id,
                end_id,
                status
            )
        )

        return

    status = await event.reply(
        f"🚀 **Starting migration**\n\n"
        f"From: `{start_id}`\n"
        f"To: `{end_id}`"
    )

    asyncio.create_task(
        migrate_by_range(
            start_id,
            end_id,
            status
        )
    )


# ============================================================
# CHECK PUBLIC DB
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/checkpublicdb$"
    )
)
async def check_public_db_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    if not PUBLIC_DB_CHANNEL_ID:

        return await event.reply(
            "❌ `PUBLIC_DB_CHANNEL_ID` is missing."
        )

    try:

        channel = await bot.get_entity(
            PUBLIC_DB_CHANNEL_ID
        )

        await event.reply(
            "✅ **Public Migration Source Verified**\n\n"
            f"🏷️ **Name:** "
            f"{getattr(channel, 'title', 'Unknown')}\n"
            f"🆔 **ID:** `{PUBLIC_DB_CHANNEL_ID}`\n\n"
            "🎬 Migration accepts videos only."
        )

    except Exception as e:

        await event.reply(
            "❌ Could not access public DB.\n\n"
            f"`{e}`"
        )


# ============================================================
# CHECK PRIVATE DB
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/checkchannel$"
    )
)
async def check_channel_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    try:

        channel = await bot.get_entity(
            DB_CHANNEL_ID
        )

        await event.reply(
            "✅ **Private Vault Verified**\n\n"
            f"🏷️ **Name:** "
            f"{getattr(channel, 'title', 'Unknown')}\n"
            f"🆔 **ID:** `{DB_CHANNEL_ID}`"
        )

    except Exception as e:

        await event.reply(
            "❌ Could not access private vault.\n\n"
            f"`{e}`"
        )


# ============================================================
# CHECK DATABASE
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/checkdb$"
    )
)
async def check_db_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    try:

        count = get_vault_count()

        if USE_POSTGRES:

            await event.reply(
                f"🐘 **PostgreSQL**\n\n"
                f"🎬 Video files: `{count}`"
            )

        else:

            await event.reply(
                f"💾 **SQLite**\n\n"
                f"🎬 Video files: `{count}`"
            )

    except Exception as e:

        await event.reply(
            f"❌ Database error:\n`{e}`"
        )


# ============================================================
# HEALTH
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/health$"
    )
)
async def health_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    try:

        me = await bot.get_me()

        vault_count = (
            get_vault_count()
        )

        user_count = (
            get_user_count()
        )

        await event.reply(
            "🟢 **MAXCINEMA HEALTH**\n\n"
            f"🤖 Bot: `@{me.username}`\n"
            f"💾 Database: "
            f"`{'PostgreSQL' if USE_POSTGRES else 'SQLite'}`\n"
            f"🎬 Videos: `{vault_count}`\n"
            f"👥 Users: `{user_count}`\n"
            f"📥 Queue: `{WORK_QUEUE.qsize()}`\n"
            f"🔄 Migration: "
            f"`{'RUNNING' if MIGRATION_LOCK.locked() else 'IDLE'}`\n"
            f"🌐 Stream limit: `{STREAM_CONCURRENCY}`"
        )

    except Exception as e:

        await event.reply(
            f"🔴 **Health Check Failed**\n\n"
            f"`{e}`"
        )


# ============================================================
# VIDEO STATS
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/videostats$"
    )
)
async def video_stats_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    try:

        if USE_POSTGRES:

            conn = None

            try:

                conn = get_pg_conn()

                with conn.cursor() as cursor:

                    cursor.execute(
                        """
                        SELECT
                            media_type,
                            COUNT(*)
                        FROM vault
                        GROUP BY media_type
                        ORDER BY media_type
                        """
                    )

                    rows = cursor.fetchall()

            finally:

                release_pg_conn(
                    conn
                )

        else:

            sqlite_cursor.execute(
                """
                SELECT
                    media_type,
                    COUNT(*)
                FROM vault
                GROUP BY media_type
                ORDER BY media_type
                """
            )

            rows = sqlite_cursor.fetchall()

        lines = [
            "📊 **VIDEO VAULT STATS**",
            ""
        ]

        total = 0

        for row in rows:

            media_type = row[0]
            count = row[1]

            total += count

            lines.append(
                f"🎬 {media_type}: `{count}`"
            )

        lines.extend(
            [
                "",
                f"📦 Total: `{total}`"
            ]
        )

        await event.reply(
            "\n".join(lines)
        )

    except Exception as e:

        await event.reply(
            f"❌ Stats error:\n`{e}`"
        )


# ============================================================
# VAULT LOOKUP
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/vault\s+\d+$"
    )
)
async def vault_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    try:

        msg_id = int(
            event.text.split()[1]
        )

        record = get_vault_record(
            msg_id
        )

        if not record:

            return await event.reply(
                "❌ Vault record not found."
            )

        await event.reply(
            "🎬 **VAULT ITEM**\n\n"
            f"🆔 ID: `{record[0]}`\n"
            f"📁 File: `{record[1]}`\n"
            f"🎞️ Title: `{record[2]}`\n"
            f"📌 Type: `{record[3]}`\n"
            f"📺 Season: `{record[4] or '-'}`\n"
            f"🎬 Episode: `{record[5] or '-'}`"
        )

    except Exception as e:

        await event.reply(
            f"❌ Vault lookup failed:\n`{e}`"
        )


# ============================================================
# ADMIN SEARCH
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/find(?:\s|$)"
    )
)
async def find_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    query = (
        event.text
        .replace(
            "/find",
            "",
            1
        )
        .strip()
    )

    if not query:

        return await event.reply(
            "❌ Usage:\n"
            "`/find Stranger Things`"
        )

    ids = search_vault(
        query
    )

    if not ids:

        return await event.reply(
            "❌ No videos found."
        )

    lines = [
        "🔎 **VIDEO SEARCH RESULTS**",
        ""
    ]

    for msg_id in ids:

        record = get_vault_record(
            msg_id
        )

        if record:

            lines.append(
                f"🎬 `{record[0]}` — "
                f"{record[1]}"
            )

    await event.reply(
        "\n".join(lines)
    )


# ============================================================
# START
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/start"
    )
)
async def start_handler(event):

    sender = await event.get_sender()

    if not sender:
        return

    add_user(
        sender.id
    )

    args = event.text.split()

    is_joined = await check_subscription(
        sender.id
    )

    # ========================================================
    # DEEP LINK
    # ========================================================
    if len(args) > 1:

        param = args[1]

        if not is_joined:

            me = await bot.get_me()

            msg = (
                "⛔ **Access Denied!**\n\n"
                "You must join our main channel "
                "to download this file."
            )

            btn = [
                [
                    Button.url(
                        "📢 Join Channel",
                        url=(
                            CHANNEL_LINK
                            or
                            "https://t.me/MaxCinemaOfficial"
                        )
                    )
                ],
                [
                    Button.url(
                        "🔄 Try Again",
                        url=(
                            f"https://t.me/"
                            f"{me.username}"
                            f"?start={param}"
                        )
                    )
                ]
            ]

            return await event.reply(
                msg,
                buttons=btn
            )

        status = await event.reply(
            "📂 **Fetching your video...**"
        )

        # ====================================================
        # PACK
        # ====================================================
        if param.startswith(
            "pack_"
        ):

            try:

                _, start_id, end_id = (
                    param.split("_")
                )

                ids_to_fetch = range(
                    int(start_id),
                    int(end_id) + 1
                )

                found_any = False

                sent_messages = []

                for msg_id in ids_to_fetch:

                    msg = await get_vault_message(
                        msg_id
                    )

                    if not msg:
                        continue

                    sent_file = (
                        await bot.send_file(
                            event.chat_id,
                            msg.media,
                            caption=msg.text
                        )
                    )

                    sent_messages.append(
                        sent_file
                    )

                    found_any = True

                if found_any:

                    total_size = sum(
                        (get_file_size(m) or 0)
                        for m in sent_messages
                    )

                    delay = auto_delete_delay_for_size(
                        total_size
                    )

                    warning = await event.reply(
                        "⏳ **SECURITY:** "
                        f"*Videos will auto-delete "
                        f"in {delay // 60} "
                        "minutes.*\n\n"
                        "👉 Forward them to your "
                        "**Saved Messages** now to "
                        "keep them."
                    )

                    sent_messages.append(
                        warning
                    )

                    asyncio.create_task(
                        auto_delete_task(
                            event,
                            sent_messages,
                            delay=delay
                        )
                    )

                    await status.delete()

                else:

                    await status.edit(
                        "❌ Video pack not found."
                    )

            except Exception as e:

                await status.edit(
                    f"❌ Pack error:\n`{e}`"
                )

        # ====================================================
        # SINGLE VIDEO
        # ====================================================
        else:

            try:

                msg_id = int(
                    param
                )

                msg = await get_vault_message(
                    msg_id
                )

                # Try public -> private mapping.
                if not msg:

                    mapped_id = (
                        get_vault_id_from_public(
                            msg_id
                        )
                    )

                    if mapped_id:

                        msg = (
                            await get_vault_message(
                                mapped_id
                            )
                        )

                if (
                    msg
                    and msg.media
                    and is_video_message(msg)
                ):

                    sent_file = (
                        await bot.send_file(
                            event.chat_id,
                            msg.media,
                            caption=msg.text
                        )
                    )

                    delay = auto_delete_delay_for_size(
                        get_file_size(sent_file)
                    )

                    warning = await event.reply(
                        "⏳ **SECURITY:** "
                        f"*This video will auto-delete "
                        f"in {delay // 60} "
                        "minutes.*\n\n"
                        "👉 Forward it to your "
                        "**Saved Messages** now to "
                        "keep it."
                    )

                    asyncio.create_task(
                        auto_delete_task(
                            event,
                            [
                                sent_file,
                                warning
                            ],
                            delay=delay
                        )
                    )

                    await status.delete()

                else:

                    await status.edit(
                        "❌ Video not found."
                    )

            except Exception as e:

                await status.edit(
                    f"❌ Error processing video:\n"
                    f"`{e}`"
                )

        return

    # ========================================================
    # ADMIN START
    # ========================================================
    if sender.id in AUTH_USERS:

        admin_guide = (
            "**👑 MAXCINEMA ADMIN GUIDE**\n\n"

            "**1️⃣ MIRROR**\n"
            "`/mirror name.mp4`\n\n"

            "**2️⃣ ADD VIDEO**\n"
            "Reply `/add` to a video.\n\n"

            "**3️⃣ POST**\n"
            "Reply photo + `/post`.\n\n"

            "**4️⃣ POST ID**\n"
            "`/postid 1234 Caption`\n\n"

            "**5️⃣ POST PACK**\n"
            "`/postpack 100-107 Caption`\n\n"

            "**6️⃣ TMDB**\n"
            "`/tmdb Inception`\n\n"

            "**7️⃣ BROADCAST**\n"
            "`/broadcast Message`\n\n"

            "**8️⃣ STATS**\n"
            "`/stats`\n\n"

            "**9️⃣ VIDEO STATS**\n"
            "`/videostats`\n\n"

            "**🔟 INDEX VAULT**\n"
            "`/indexvault`\n\n"

            "**1️⃣1️⃣ FIND VIDEO**\n"
            "`/find Movie Name`\n\n"

            "**1️⃣2️⃣ VAULT ITEM**\n"
            "`/vault 1234`\n\n"

            "**1️⃣3️⃣ MIGRATION**\n"
            "`/migrate 100-500`\n"
            "`/migrate 19417`\n"
            "`/migrate date 2026-01-01`\n\n"

            "**1️⃣4️⃣ CHECK PUBLIC DB**\n"
            "`/checkpublicdb`\n\n"

            "**1️⃣5️⃣ CHECK PRIVATE DB**\n"
            "`/checkchannel`\n\n"

            "**1️⃣6️⃣ HEALTH**\n"
            "`/health`\n\n"

            "🎬 **VIDEO ONLY:**\n"
            "MP4 • MKV • AVI • MOV • WEBM • "
            "M4V • TS • MPEG • MPG • 3GP • "
            "FLV • WMV • OGV • and more."
        )

        await event.reply(
            admin_guide
        )

    else:

        welcome_text = (
            "**👋 Welcome to MaxCinema Bot!**\n\n"
            "🎬 I store and deliver video files "
            "for the main channel.\n\n"
            "Supported videos include:\n"
            "MP4 • MKV • AVI • MOV • WEBM • M4V"
        )

        buttons = []

        if CHANNEL_LINK:

            buttons.append(
                [
                    Button.url(
                        "📢 Join Main Channel",
                        url=CHANNEL_LINK
                    )
                ]
            )

        buttons.append(
            [
                Button.inline(
                    "📝 Request a Movie",
                    data="help_request"
                )
            ]
        )

        await event.reply(
            welcome_text,
            buttons=buttons
        )


# ============================================================
# REQUEST CALLBACK
# ============================================================
@bot.on(
    events.CallbackQuery(
        data="help_request"
    )
)
async def callback_handler(event):

    await event.answer(
        "💡 TYPE:\n/request Movie Name",
        alert=True
    )


# ============================================================
# REQUEST
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/request"
    )
)
async def request_handler(event):

    query = (
        event.text
        .replace(
            "/request",
            "",
            1
        )
        .strip()
    )

    sender = await event.get_sender()

    if not query:

        return await event.reply(
            "❌ Usage:\n"
            "`/request Movie Name`"
        )

    status = await event.reply(
        f"🔍 Searching for `{query}`..."
    )

    try:

        msg_ids = search_vault(
            query
        )

        if msg_ids:

            messages = await bot.get_messages(
                DB_CHANNEL_ID,
                ids=msg_ids
            )

            if not isinstance(
                messages,
                list
            ):

                messages = [
                    messages
                ]

            found = False
            sent_messages = []

            for msg in messages:

                if (
                    msg
                    and msg.media
                    and is_video_message(msg)
                ):

                    sent_file = (
                        await bot.send_file(
                            event.chat_id,
                            msg.media,
                            caption=msg.text
                        )
                    )

                    sent_messages.append(
                        sent_file
                    )

                    found = True

            if found:

                total_size = sum(
                    (get_file_size(m) or 0)
                    for m in sent_messages
                )

                delay = auto_delete_delay_for_size(
                    total_size
                )

                warning = await event.reply(
                    "⏳ **SECURITY:** "
                    f"*Videos auto-delete in "
                    f"{delay // 60} minutes.*\n\n"
                    "👉 Forward them to your "
                    "**Saved Messages** now to "
                    "keep them."
                )

                sent_messages.append(
                    warning
                )

                asyncio.create_task(
                    auto_delete_task(
                        event,
                        sent_messages,
                        delay=delay
                    )
                )

                return await status.edit(
                    "✅ **Here is what I found!**"
                )

        await status.edit(
            "⚠️ Not found. "
            "Forwarding request to admins..."
        )

        if AUTH_USERS:

            first_name = (
                getattr(
                    sender,
                    "first_name",
                    "Unknown"
                )
                if sender
                else "Unknown"
            )

            sender_id = (
                sender.id
                if sender
                else "Unknown"
            )

            for admin_id in AUTH_USERS:

                try:

                    await bot.send_message(
                        admin_id,
                        (
                            "📩 **NEW REQUEST!**\n\n"
                            f"👤 {first_name}\n"
                            f"🆔 `{sender_id}`\n"
                            f"📝 `{query}`"
                        )
                    )

                except Exception:
                    pass

            await event.reply(
                "✅ **Request Sent to Admins!**"
            )

    except Exception as e:

        await status.edit(
            f"❌ Search error:\n`{e}`"
        )


# ============================================================
# INDEX PRIVATE VAULT
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/indexvault$"
    )
)
async def index_vault_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    status = await event.reply(
        "🔍 **Locating newest message in vault...**"
    )

    count = 0
    skipped = 0
    checked = 0

    try:

        latest_id = await find_latest_message_id(
            DB_CHANNEL_ID
        )

        if not latest_id:

            return await status.edit(
                "❌ Could not resolve any messages "
                "in the vault."
            )

        await status.edit(
            "🔄 **Starting video vault indexing...**\n\n"
            f"📈 Scanning IDs `1` → `{latest_id}`"
        )

        batch_size = 100

        for batch_start in range(
            1,
            latest_id + 1,
            batch_size
        ):

            batch_ids = list(
                range(
                    batch_start,
                    min(
                        batch_start + batch_size,
                        latest_id + 1
                    )
                )
            )

            messages = await get_messages_by_ids(
                DB_CHANNEL_ID,
                batch_ids
            )

            for msg in messages:

                checked += 1

                if not msg or not msg.media:

                    skipped += 1
                    continue

                # STRICT VIDEO CHECK
                if not is_video_message(
                    msg
                ):

                    skipped += 1
                    continue

                file_name = (
                    get_message_filename_or_fallback(
                        msg
                    )
                )

                if not file_name:

                    skipped += 1
                    continue

                if not is_video_filename(
                    file_name
                ):

                    skipped += 1
                    continue

                if add_vault_item(
                    msg.id,
                    file_name
                ):

                    count += 1

            try:

                await status.edit(
                    f"🔄 Checked `{checked}/{latest_id}`\n"
                    f"🎬 Indexed: `{count}`\n"
                    f"⏭️ Skipped: `{skipped}`"
                )

            except Exception:
                pass

            await asyncio.sleep(0.3)

        await status.edit(
            f"✅ **Video Indexing Complete**\n\n"
            f"🎬 Indexed: `{count}` videos\n"
            f"⏭️ Skipped: `{skipped}` non-videos"
        )

        if not USE_POSTGRES:

            asyncio.create_task(
                backup_database_to_tg()
            )

    except Exception as e:

        await status.edit(
            f"❌ Indexing failed:\n`{e}`"
        )


# ============================================================
# STATS
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/stats$"
    )
)
async def stats_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    users = get_user_count()
    videos = get_vault_count()

    await event.reply(
        "📊 **Bot Statistics**\n\n"
        f"👥 Users: **{users}**\n"
        f"🎬 Videos: **{videos}**\n"
        f"📥 Queue: **{WORK_QUEUE.qsize()}**"
    )


# ============================================================
# BROADCAST
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/broadcast"
    )
)
async def broadcast_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    msg = (
        event.text
        .replace(
            "/broadcast",
            "",
            1
        )
        .strip()
    )

    if not msg:

        return await event.reply(
            "❌ Usage:\n"
            "`/broadcast Hello everyone!`"
        )

    users = get_all_users()

    status = await event.reply(
        f"🚀 Broadcasting to `{len(users)}` users..."
    )

    sent = 0

    for user in users:

        try:

            await bot.send_message(
                user,
                msg
            )

            sent += 1

            await asyncio.sleep(
                0.1
            )

        except Exception:
            pass

    await status.edit(
        "✅ **Broadcast Complete!**\n\n"
        f"Delivered: `{sent}/{len(users)}`"
    )


# ============================================================
# TMDB
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/tmdb"
    )
)
async def tmdb_handler(event):

    if event.sender_id not in AUTH_USERS:
        return

    query = (
        event.text
        .replace(
            "/tmdb",
            "",
            1
        )
        .strip()
    )

    if not query:

        return await event.reply(
            "❌ Usage:\n"
            "`/tmdb Inception`"
        )

    if not TMDB_API_KEY:

        return await event.reply(
            "❌ TMDB_API_KEY is missing."
        )

    status = await event.reply(
        "🔍 Fetching TMDB..."
    )

    try:

        async with aiohttp.ClientSession() as session:

            url = (
                "https://api.themoviedb.org/3/"
                "search/movie"
            )

            params = {
                "api_key": TMDB_API_KEY,
                "query": query
            }

            async with session.get(
                url,
                params=params
            ) as response:

                data = await response.json()

        if not data.get(
            "results"
        ):

            return await status.edit(
                "❌ Movie not found."
            )

        movie = data[
            "results"
        ][0]

        title = movie.get(
            "title",
            "Unknown"
        )

        year = (
            movie.get(
                "release_date",
                ""
            )
            .split("-")[0]
        )

        rating = movie.get(
            "vote_average",
            "N/A"
        )

        overview = movie.get(
            "overview",
            "No summary."
        )

        poster_path = movie.get(
            "poster_path"
        )

        poster_url = (
            "https://image.tmdb.org/t/p/w500"
            f"{poster_path}"
            if poster_path
            else None
        )

        caption = (
            f"🎬 **{title} ({year})**\n\n"
            f"⭐ **Rating:** {rating}/10\n\n"
            f"📖 **Plot:** {overview}\n\n"
            f"👇 **Download Below**"
        )

        if poster_url:

            await bot.send_file(
                event.chat_id,
                poster_url,
                caption=caption
            )

            await status.delete()

        else:

            await status.edit(
                caption
            )

    except Exception as e:

        await status.edit(
            f"❌ TMDB error:\n`{e}`"
        )


# ============================================================
# ADD VIDEO
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/add$"
    )
)
async def add_handler(event):

    sender = await event.get_sender()

    if (
        not AUTH_USERS
        or sender.id not in AUTH_USERS
    ):

        return

    reply = await event.get_reply_message()

    if not reply or not reply.media:

        return await event.reply(
            "❌ Reply to a video file.\n\n"
            "🎬 Supported:\n"
            "MP4 • MKV • AVI • MOV • WEBM • M4V"
        )

    # STRICT VIDEO CHECK
    if not is_video_message(
        reply
    ):

        return await event.reply(
            "❌ This is not a supported video.\n\n"
            "Photos, subtitles, PDFs, ZIPs, "
            "and other documents are rejected.\n\n"
            "🎬 Supported video formats:\n"
            "MP4 • MKV • AVI • MOV • WEBM • M4V • "
            "TS • MPEG • MPG • 3GP • FLV • WMV"
        )

    try:

        original_caption = (
            reply.text or ""
        )

        original_name = (
            get_message_filename_or_fallback(
                reply
            )
        )

        if not original_name:

            original_name = (
                f"Video_{reply.id}.mp4"
            )

        if not is_video_filename(
            original_name
        ):

            return await event.reply(
                "❌ Video filename/format "
                "could not be identified."
            )

        vault_msg = await bot.send_file(
            DB_CHANNEL_ID,
            reply.media,
            caption=original_caption,
            supports_streaming=True
        )

        if not is_video_message(
            vault_msg
        ):

            try:
                await vault_msg.delete()
            except Exception:
                pass

            return await event.reply(
                "❌ Vault rejected this media "
                "because it is not a supported video."
            )

        index_title = (
            f"{original_name} "
            f"{original_caption}"
        ).strip()

        if not index_title:

            index_title = (
                original_name
            )

        if add_vault_item(
            vault_msg.id,
            original_name
        ):

            await event.reply(
                "✅ **Video Added & Indexed!**\n\n"
                f"📂 **Vault ID:** `{vault_msg.id}`\n"
                f"🎬 **File:** `{original_name}`\n\n"
                "👇 Reply with Photo + `/post` "
                "to publish."
            )

        else:

            await event.reply(
                "❌ Video uploaded but database "
                "indexing failed."
            )

    except Exception as e:

        await event.reply(
            f"❌ Error adding video:\n`{e}`"
        )


# ============================================================
# POST ID
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/postid"
    )
)
async def postid_handler(event):

    sender = await event.get_sender()

    if (
        not AUTH_USERS
        or sender.id not in AUTH_USERS
    ):

        return

    args = event.text.split(
        " ",
        2
    )

    if len(args) < 3:

        return await event.reply(
            "❌ Usage:\n"
            "`/postid 1234 Your Movie Caption`"
        )

    try:

        vault_id = int(
            args[1]
        )

    except ValueError:

        return await event.reply(
            "❌ ID must be a number."
        )

    caption = args[2].strip()

    vault_msg = await get_vault_message(
        vault_id
    )

    if not vault_msg:

        return await event.reply(
            "❌ That ID is not a valid "
            "video in the PRIVATE storage channel."
        )

    me = await bot.get_me()

    deep_link = (
        f"https://t.me/"
        f"{me.username}"
        f"?start={vault_id}"
    )

    buttons = []

    if WEBSITE_HOME:

        buttons.append(
            [
                Button.url(
                    "🌍 Visit Website",
                    url=WEBSITE_HOME
                )
            ]
        )

    buttons.append(
        [
            Button.url(
                "📂 Get File",
                url=deep_link
            )
        ]
    )

    poster = (
        await event.download_media()
        if event.photo
        else None
    )

    if not poster:

        reply = (
            await event.get_reply_message()
        )

        if reply and reply.photo:

            poster = (
                await reply.download_media()
            )

    try:

        public_msg = (
            await send_public_card(
                caption,
                buttons,
                poster
            )
        )

        save_public_mapping(
            public_msg.id,
            vault_id
        )

        await event.reply(
            "✅ **Published!**\n\n"
            f"🆔 Vault ID: `{vault_id}`"
        )

    except Exception as e:

        await event.reply(
            f"❌ Error:\n`{e}`"
        )


# ============================================================
# POST PACK
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/postpack"
    )
)
async def postpack_handler(event):

    sender = await event.get_sender()

    if (
        not AUTH_USERS
        or sender.id not in AUTH_USERS
    ):

        return

    args = event.text.split()

    if len(args) < 2:

        return await event.reply(
            "❌ Usage:\n"
            "`/postpack 100-107 Caption`"
        )

    range_str = args[1]

    try:

        start_id, end_id = (
            range_str.split("-")
        )

        start_id = int(
            start_id
        )

        end_id = int(
            end_id
        )

    except Exception:

        return await event.reply(
            "❌ Invalid format.\n"
            "Use `100-107`."
        )

    caption = (
        event.text
        .replace(
            "/postpack",
            "",
            1
        )
        .replace(
            range_str,
            "",
            1
        )
        .strip()
        or
        "🎬 **New Season Pack!**"
    )

    me = await bot.get_me()

    pack_link = (
        f"https://t.me/"
        f"{me.username}"
        f"?start=pack_"
        f"{start_id}_"
        f"{end_id}"
    )

    buttons = [
        [
            Button.url(
                "📂 Get Full Season",
                url=pack_link
            )
        ]
    ]

    if WEBSITE_HOME:

        buttons.insert(
            0,
            [
                Button.url(
                    "🌍 Visit Website",
                    url=WEBSITE_HOME
                )
            ]
        )

    poster = (
        await event.download_media()
        if event.photo
        else None
    )

    try:

        ok_count = 0

        for item_id in range(
            start_id,
            end_id + 1
        ):

            if await get_vault_message(
                item_id
            ):

                ok_count += 1

        if ok_count == 0:

            return await event.reply(
                "❌ None of those IDs "
                "contain videos."
            )

        public_msg = (
            await send_public_card(
                caption,
                buttons,
                poster
            )
        )

        save_public_mapping(
            public_msg.id,
            start_id
        )

        await event.reply(
            "✅ **Pack Published!**\n\n"
            f"🎬 Videos available: `{ok_count}`\n"
            f"🔗 {pack_link}"
        )

    except Exception as e:

        await event.reply(
            f"❌ Error:\n`{e}`"
        )


# ============================================================
# POST
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/post(?!id|pack)"
    )
)
async def post_handler(event):

    sender = await event.get_sender()

    if (
        not AUTH_USERS
        or sender.id not in AUTH_USERS
    ):

        return

    reply = await event.get_reply_message()

    if not reply:

        return await event.reply(
            "⚠️ Reply to a Vault ID message."
        )

    vault_id = None

    for line in (
        reply.text or ""
    ).split("\n"):

        if "Vault ID:" in line:

            vault_id = re.sub(
                r"[^0-9]",
                "",
                line.split(
                    "Vault ID:",
                    1
                )[1]
            )

    if not vault_id:

        return await event.reply(
            "❌ No Vault ID found."
        )

    vault_msg = await get_vault_message(
        int(vault_id)
    )

    if not vault_msg:

        return await event.reply(
            "❌ Vault ID does not exist "
            "or is not a video."
        )

    caption = (
        event.text
        .replace(
            "/post",
            "",
            1
        )
        .strip()
        or
        "🎬 **New Movie Uploaded!**"
    )

    me = await bot.get_me()

    deep_link = (
        f"https://t.me/"
        f"{me.username}"
        f"?start={vault_id}"
    )

    buttons = []

    if WEBSITE_HOME:

        buttons.append(
            [
                Button.url(
                    "🌍 Visit Website",
                    url=WEBSITE_HOME
                )
            ]
        )

    buttons.append(
        [
            Button.url(
                "📂 Get File",
                url=deep_link
            )
        ]
    )

    poster = (
        await event.download_media()
        if event.photo
        else None
    )

    try:

        public_msg = (
            await send_public_card(
                caption,
                buttons,
                poster
            )
        )

        save_public_mapping(
            public_msg.id,
            int(vault_id)
        )

        await event.reply(
            "✅ **Published!**\n\n"
            f"🆔 Vault ID: `{vault_id}`"
        )

    except Exception as e:

        await event.reply(
            f"❌ Error:\n`{e}`"
        )


# ============================================================
# CORE MIRROR PROCESSOR
# ============================================================
async def process_task(
    event,
    source,
    name,
    thumb_path
):

    name = sanitize_filename(
        name
    )

    if not is_video_filename(
        name
    ):

        await event.reply(
            f"❌ Rejected `{name}`\n\n"
            "Only supported video formats "
            "can be mirrored."
        )

        return False

    status_msg = await event.reply(
        f"⏳ **Initializing:** `{name}`..."
    )

    last_time = [0]

    current_thumb = thumb_path

    try:

        # ----------------------------------------------------
        # URL SOURCE
        # ----------------------------------------------------
        if isinstance(
            source,
            str
        ):

            await status_msg.edit(
                "🚀 **Downloading video URL...**"
            )

            headers = {
                "User-Agent":
                "Mozilla/5.0"
            }

            timeout = aiohttp.ClientTimeout(
                total=3600
            )

            async with aiohttp.ClientSession(
                timeout=timeout
            ) as session:

                async with session.get(
                    source,
                    headers=headers,
                    allow_redirects=True
                ) as resp:

                    if resp.status != 200:

                        await status_msg.edit(
                            f"❌ Server returned "
                            f"`{resp.status}`"
                        )

                        return False

                    content_type = (
                        resp.headers.get(
                            "Content-Type",
                            ""
                        ).lower()
                    )

                    # If the server explicitly says
                    # image/pdf/text, reject it.
                    if (
                        content_type.startswith(
                            "image/"
                        )
                        or
                        content_type.startswith(
                            "text/"
                        )
                        or
                        "application/pdf"
                        in content_type
                    ):

                        await status_msg.edit(
                            "❌ URL is not a video."
                        )

                        return False

                    total = int(
                        resp.headers.get(
                            "content-length",
                            0
                        )
                    )

                    current = 0

                    async with aiofiles.open(
                        name,
                        "wb"
                    ) as f:

                        async for chunk in (
                            resp.content.iter_chunked(
                                10 * 1024 * 1024
                            )
                        ):

                            await f.write(
                                chunk
                            )

                            current += len(
                                chunk
                            )

                            await progress_bar(
                                current,
                                total,
                                status_msg,
                                "⬇️ **Downloading...**",
                                last_time
                            )

            if not os.path.exists(
                name
            ):

                await status_msg.edit(
                    "❌ Download produced no file."
                )

                return False

            if os.path.getsize(
                name
            ) <= 0:

                await status_msg.edit(
                    "❌ Downloaded file is empty."
                )

                return False

        # ----------------------------------------------------
        # TELEGRAM SOURCE
        # ----------------------------------------------------
        else:

            if not is_video_message(
                source
            ):

                await status_msg.edit(
                    "❌ Telegram source is not "
                    "a supported video."
                )

                return False

            await status_msg.edit(
                "📥 **Downloading video "
                "from Telegram...**"
            )

            async def dl_callback(
                current,
                total
            ):

                await progress_bar(
                    current,
                    total,
                    status_msg,
                    "📥 **Downloading...**",
                    last_time
                )

            success = await smart_download(
                bot,
                source,
                name,
                dl_callback
            )

            if not success:

                await status_msg.edit(
                    "❌ Telegram video "
                    "download failed."
                )

                return False

        # ----------------------------------------------------
        # GENERATE THUMBNAIL
        # ----------------------------------------------------
        if not current_thumb:

            generated_thumb = (
                f"{name}_thumb.jpg"
            )

            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "00:00:05",
                "-i",
                name,
                "-frames:v",
                "1",
                "-y",
                generated_thumb
            ]

            process = (
                await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
            )

            try:

                await asyncio.wait_for(
                    process.wait(),
                    timeout=30
                )

            except asyncio.TimeoutError:

                try:
                    process.kill()
                except Exception:
                    pass

            if os.path.exists(
                generated_thumb
            ):

                current_thumb = (
                    generated_thumb
                )

        # ----------------------------------------------------
        # UPLOAD TO PRIVATE VAULT
        # ----------------------------------------------------
        await status_msg.edit(
            "⚡ **Uploading video to Vault...**"
        )

        last_time = [0]

        async def up_callback(
            current,
            total
        ):

            await progress_bar(
                current,
                total,
                status_msg,
                "☁️ **Uploading to Vault...**",
                last_time
            )

        try:

            vault_msg = await bot.send_file(
                DB_CHANNEL_ID,
                file=name,
                caption=f"🔒 {name}",
                thumb=current_thumb,
                supports_streaming=True,
                progress_callback=up_callback
            )

        except Exception as e:

            await status_msg.edit(
                f"❌ Vault upload failed:\n`{e}`"
            )

            return False

        if not is_video_message(
            vault_msg
        ):

            try:
                await vault_msg.delete()
            except Exception:
                pass

            await status_msg.edit(
                "❌ Uploaded item was not "
                "recognized as a video."
            )

            return False

        add_vault_item(
            vault_msg.id,
            name
        )

        # ----------------------------------------------------
        # OPTIONAL DOOD UPLOAD
        # ----------------------------------------------------
        dood_link = None

        if DOOD_KEY:

            await status_msg.edit(
                "⚡ **Uploading to Doodstream...**"
            )

            try:

                async with aiohttp.ClientSession() as session:

                    api_url = (
                        "https://doodapi.co/api/"
                        "upload/server"
                    )

                    async with session.get(
                        api_url,
                        params={
                            "key": DOOD_KEY
                        }
                    ) as response:

                        data = await response.json()

                    if data.get(
                        "status"
                    ) == 200:

                        upload_url = (
                            data.get(
                                "result"
                            )
                        )

                        if upload_url:

                            form = aiohttp.FormData()

                            with open(
                                name,
                                "rb"
                            ) as file_handle:

                                form.add_field(
                                    "api_key",
                                    DOOD_KEY
                                )

                                form.add_field(
                                    "file",
                                    file_handle,
                                    filename=name
                                )

                                async with session.post(
                                    upload_url,
                                    data=form,
                                    timeout=7200
                                ) as response:

                                    dood_data = (
                                        await response.json()
                                    )

                            if (
                                dood_data.get(
                                    "status"
                                )
                                == 200
                            ):

                                result = (
                                    dood_data.get(
                                        "result",
                                        []
                                    )
                                )

                                if result:

                                    raw_link = (
                                        result[0].get(
                                            "download_url"
                                        )
                                    )

                                    dood_link = (
                                        fix_dood_link(
                                            raw_link
                                        )
                                    )

            except Exception as e:

                print(
                    f"Dood upload failed: {e}"
                )

        # ----------------------------------------------------
        # FINAL
        # ----------------------------------------------------
        stream_url = (
            f"{BASE_URL}/stream/"
            f"{vault_msg.id}"
            if BASE_URL
            else "N/A"
        )

        final_msg = (
            "✅ **Mirror Complete!**\n\n"
            f"📂 **Vault ID:** "
            f"`{vault_msg.id}`\n"
            f"🎬 **File:** `{name}`\n"
            f"📦 **Size:** "
            f"`{human_size(os.path.getsize(name))}`\n"
            f"🌐 **Stream:** {stream_url}\n"
        )

        if dood_link:

            final_msg += (
                f"🔗 **Dood:** {dood_link}\n"
            )

        final_msg += (
            "\n👇 Reply with Photo + `/post` "
            "to publish."
        )

        await status_msg.edit(
            final_msg
        )

        return True

    except Exception as e:

        try:

            await status_msg.edit(
                f"❌ Error:\n`{e}`"
            )

        except Exception:
            pass

        return False

    finally:

        if os.path.exists(
            name
        ):

            try:
                os.remove(
                    name
                )
            except Exception:
                pass

        if (
            current_thumb
            and current_thumb != thumb_path
            and os.path.exists(
                current_thumb
            )
        ):

            try:
                os.remove(
                    current_thumb
                )
            except Exception:
                pass


# ============================================================
# QUEUE WORKER
# ============================================================
async def worker(
    worker_id=1
):

    print(
        f"👷 Queue Worker {worker_id} Started"
    )

    while True:

        task_data = (
            await WORK_QUEUE.get()
        )

        event, source, name, thumb_path = (
            task_data
        )

        try:

            await process_task(
                event,
                source,
                name,
                thumb_path
            )

        except Exception as e:

            print(
                f"Worker {worker_id} failed: {e}"
            )

            try:

                await event.reply(
                    f"❌ Task failed:\n`{e}`"
                )

            except Exception:
                pass

        finally:

            WORK_QUEUE.task_done()

            await asyncio.sleep(
                2
            )


# ============================================================
# MIRROR COMMAND
# ============================================================
@bot.on(
    events.NewMessage(
        pattern=r"^/mirror"
    )
)
async def handler(event):

    sender = await event.get_sender()

    if (
        not AUTH_USERS
        or sender.id not in AUTH_USERS
    ):

        return

    reply = await event.get_reply_message()

    batch_thumb = (
        await event.download_media()
        if event.photo
        else (
            await reply.download_media()
            if reply and reply.photo
            else None
        )
    )

    tasks = []

    # --------------------------------------------------------
    # Reply to Telegram video
    # --------------------------------------------------------
    if (
        reply
        and is_video_message(reply)
    ):

        parts = event.text.split(
            " ",
            1
        )

        requested_name = (
            parts[1].strip()
            if len(parts) > 1
            else get_message_filename_or_fallback(
                reply
            )
        )

        requested_name = sanitize_filename(
            requested_name
        )

        if not is_video_filename(
            requested_name
        ):

            # If user did not explicitly provide
            # an extension, use Telegram's extension.
            original_name = (
                get_message_filename_or_fallback(
                    reply
                )
            )

            if is_video_filename(
                original_name
            ):

                requested_name = (
                    original_name
                )

        if not is_video_filename(
            requested_name
        ):

            return await event.reply(
                "❌ Please provide a valid "
                "video filename.\n\n"
                "Example:\n"
                "`/mirror Movie.mkv`"
            )

        tasks.append(
            (
                reply,
                requested_name
            )
        )

    # --------------------------------------------------------
    # URL pairs
    #
    # /mirror url1 name1.mp4 url2 name2.mkv
    # --------------------------------------------------------
    else:

        parts = event.text.split()

        if (
            len(parts) > 1
            and (
                len(parts[1:])
                % 2 != 0
            )
        ):

            return await event.reply(
                "❌ Each URL must have "
                "a filename.\n\n"
                "Example:\n"
                "`/mirror URL1 Movie.mp4`"
            )

        for i in range(
            1,
            len(parts),
            2
        ):

            if i + 1 >= len(parts):
                break

            source = parts[i]
            name = sanitize_filename(
                parts[i + 1]
            )

            if not is_video_filename(
                name
            ):

                continue

            tasks.append(
                (
                    source,
                    name
                )
            )

    if not tasks:

        return await event.reply(
            "❌ No valid videos found.\n\n"
            "Usage:\n"
            "`/mirror link1 movie.mp4`\n\n"
            "Or reply to a video:\n"
            "`/mirror Movie.mkv`\n\n"
            "Supported:\n"
            "MP4 • MKV • AVI • MOV • WEBM • "
            "M4V • TS • MPEG • MPG • 3GP • "
            "FLV • WMV • OGV"
        )

    position = (
        WORK_QUEUE.qsize()
        + 1
    )

    await event.reply(
        f"📥 **Added to Queue**\n"
        f"🎬 Videos: `{len(tasks)}`\n"
        f"📍 Position: `{position}`"
    )

    for source, name in tasks:

        await WORK_QUEUE.put(
            (
                event,
                source,
                name,
                batch_thumb
            )
        )


# ============================================================
# WEB STREAMING
# ============================================================
async def stream_handler(
    request
):

    async with STREAM_SEMAPHORE:

        try:

            msg_id = int(
                request.match_info[
                    "msg_id"
                ]
            )

            message = await bot.get_messages(
                DB_CHANNEL_ID,
                ids=msg_id
            )

            if (
                not message
                or not message.file
                or not is_video_message(message)
            ):

                return web.Response(
                    status=404,
                    text="Video not found"
                )

            file_name = (
                get_message_filename(
                    message
                )
                or
                "video.mp4"
            )

            file_name = sanitize_filename(
                file_name
            )

            file_size = (
                getattr(
                    message.document,
                    "size",
                    None
                )
            )

            if not file_size:

                return web.Response(
                    status=404,
                    text="Video size unavailable"
                )

            mime_type = (
                getattr(
                    message.document,
                    "mime_type",
                    None
                )
                or
                mimetypes.guess_type(
                    file_name
                )[0]
                or
                "video/mp4"
            )

            range_header = (
                request.headers.get(
                    "Range"
                )
            )

            start = 0
            end = file_size - 1

            if range_header:

                match = re.match(
                    r"bytes=(\d*)-(\d*)",
                    range_header
                )

                if not match:

                    return web.Response(
                        status=416
                    )

                start_raw = (
                    match.group(1)
                )

                end_raw = (
                    match.group(2)
                )

                if (
                    not start_raw
                    and not end_raw
                ):

                    return web.Response(
                        status=416
                    )

                # bytes=-500000
                if not start_raw:

                    suffix_length = int(
                        end_raw
                    )

                    if suffix_length <= 0:

                        return web.Response(
                            status=416
                        )

                    start = max(
                        file_size
                        - suffix_length,
                        0
                    )

                else:

                    start = int(
                        start_raw
                    )

                    if end_raw:

                        end = min(
                            int(end_raw),
                            file_size - 1
                        )

            if start >= file_size:

                response = web.Response(
                    status=416
                )

                response.headers[
                    "Content-Range"
                ] = (
                    f"bytes */{file_size}"
                )

                return response

            end = min(
                end,
                file_size - 1
            )

            if end < start:

                response = web.Response(
                    status=416
                )

                response.headers[
                    "Content-Range"
                ] = (
                    f"bytes */{file_size}"
                )

                return response

            content_length = (
                end
                - start
                + 1
            )

            status_code = (
                206
                if range_header
                else 200
            )

            response = web.StreamResponse(
                status=status_code
            )

            response.headers[
                "Content-Type"
            ] = mime_type

            response.headers[
                "Accept-Ranges"
            ] = "bytes"

            response.headers[
                "Content-Length"
            ] = str(
                content_length
            )

            if range_header:

                response.headers[
                    "Content-Range"
                ] = (
                    f"bytes {start}-{end}/"
                    f"{file_size}"
                )

            response.headers[
                "Content-Disposition"
            ] = (
                'inline; filename="'
                f'{file_name}"'
            )

            response.headers[
                "Cache-Control"
            ] = (
                "public, max-age=3600"
            )

            await response.prepare(
                request
            )

            # HEAD requests should return
            # headers but no body.
            if request.method == "HEAD":

                await response.write_eof()

                return response

            # Align Telegram download offset
            # to the 1MB boundary.
            offset_limit = (
                STREAM_CHUNK_SIZE
            )

            chunk_start = (
                start
                - (
                    start
                    % offset_limit
                )
            )

            remaining = (
                content_length
            )

            async for chunk in bot.iter_download(
                message.media,
                offset=chunk_start,
                request_size=offset_limit
            ):

                if chunk_start < start:

                    slice_start = (
                        start
                        - chunk_start
                    )

                    chunk = chunk[
                        slice_start:
                    ]

                    chunk_start = start

                if len(chunk) > remaining:

                    chunk = chunk[
                        :remaining
                    ]

                if not chunk:
                    break

                await response.write(
                    chunk
                )

                remaining -= len(
                    chunk
                )

                chunk_start += len(
                    chunk
                )

                if remaining <= 0:
                    break

            await response.write_eof()

            return response

        except asyncio.CancelledError:

            raise

        except Exception as e:

            print(
                f"Streaming error: {e}"
            )

            return web.Response(
                status=500,
                text="Streaming error"
            )


# ============================================================
# WEB ROOT
# ============================================================
async def root_handler(
    request
):

    return web.Response(
        text=(
            "🚀 MaxCinema Bot is Running\n"
            "🎬 Video Streaming Service"
        )
    )


# ============================================================
# WEB HEALTH
# ============================================================
async def web_health_handler(
    request
):

    return web.json_response(
        {
            "status": "ok",
            "service": "MaxCinema",
            "database": (
                "postgresql"
                if USE_POSTGRES
                else "sqlite"
            ),
            "videos": get_vault_count(),
            "queue": WORK_QUEUE.qsize(),
        }
    )


# ============================================================
# WEB SERVER
# ============================================================
async def start_web_server():

    app = web.Application(
        client_max_size=1024 ** 4
    )

    app.add_routes(
        [
            web.get(
                "/",
                root_handler
            ),
            web.get(
                "/health",
                web_health_handler
            ),
            web.get(
                "/stream/{msg_id}",
                stream_handler,
                allow_head=False
            ),
            web.head(
                "/stream/{msg_id}",
                stream_handler
            )
        ]
    )

    runner = web.AppRunner(
        app,
        access_log=None
    )

    await runner.setup()

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"✅ Web Server Started "
        f"on Port {port}"
    )


# ============================================================
# STARTUP
# ============================================================
async def startup_tasks():

    await sync_database_from_tg()

    asyncio.create_task(
        start_web_server()
    )

    for i in range(
        QUEUE_WORKERS
    ):

        asyncio.create_task(
            worker(
                i + 1
            )
        )


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":

    print(
        "=========================================="
    )

    print(
        "🎬 MAXCINEMA VIDEO BOT"
    )

    print(
        "=========================================="
    )

    print(
        f"🔒 Private Vault: "
        f"{DB_CHANNEL_ID}"
    )

    print(
        f"📦 Public Migration Source: "
        f"{PUBLIC_DB_CHANNEL_ID}"
    )

    print(
        f"🐘 PostgreSQL: "
        f"{USE_POSTGRES}"
    )

    print(
        f"👷 Queue Workers: "
        f"{QUEUE_WORKERS}"
    )

    print(
        f"🌐 Stream Concurrency: "
        f"{STREAM_CONCURRENCY}"
    )

    print(
        "🎬 Video-only mode: ENABLED"
    )

    print(
        "=========================================="
    )

    bot.start(
        bot_token=BOT_TOKEN
    )

    bot.loop.run_until_complete(
        startup_tasks()
    )

    bot.run_until_disconnected()
