import os
import time
import asyncio
import aiohttp
import aiofiles
import re
from aiohttp import web
from telethon import TelegramClient, events, Button, functions, types
from telethon.network import ConnectionTcpIntermediate
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

# 🔐 SECURITY & FORCE SUB
auth_str = os.environ.get("AUTH_USERS", "").strip()
AUTH_USERS = [int(x) for x in auth_str.split(',') if x.strip().isdigit()]
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "").strip()

if FORCE_SUB_CHANNEL.replace('-','').isdigit():
    FORCE_SUB_CHANNEL = int(FORCE_SUB_CHANNEL)

# 🚦 GLOBAL QUEUE MANAGER
WORK_QUEUE = asyncio.Queue()

def parse_id(val):
    val = str(val).strip()
    if not val: return 0
    if val.startswith("@"): return val
    try: return int(val)
    except: return 0

DB_CHANNEL_ID = parse_id(os.environ.get("DB_CHANNEL_ID", "0"))
PUBLIC_CHANNEL_ID = parse_id(os.environ.get("PUBLIC_CHANNEL_ID", "0"))

if not str_api_id:
    print("❌ API_ID is missing!")
    exit(1)

API_ID = int(str_api_id)

# 🔄 CONNECTION (STANDARD FOR RENDER)
bot = TelegramClient(
    'MaxCinema_Mirror_Render',  # 👈 Fresh Session Name
    API_ID, 
    API_HASH, 
    connection=ConnectionTcpIntermediate, # 👈 Best mode for Docker
    timeout=60,          
    request_retries=10, 
    retry_delay=5        
)

print("✅ Bot is Starting...")


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
        participant = await bot(functions.channels.GetParticipantRequest(
            channel=FORCE_SUB_CHANNEL,
            participant=user_id
        ))
        return True
    except:
        return False

# ==========================================
# 🛠️ MEMORY REFRESHER
# ==========================================
async def refresh_cache():
    try:
        async for dialog in bot.iter_dialogs(limit=20): pass 
    except: pass

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
    except: pass

# ==========================================
# 🧠 SMART DOWNLOADER
# ==========================================
async def smart_download(client, message, filename, progress_callback):
    try:
        await client.download_media(message, file=filename, progress_callback=progress_callback)
        return True
    except: pass
    try:
        if hasattr(message, 'media') and hasattr(message.media, 'document'):
            await client.download_media(message.media.document, file=filename, progress_callback=progress_callback)
            return True
    except: pass
    try:
        if isinstance(DB_CHANNEL_ID, int) and DB_CHANNEL_ID != 0:
            vault_msg = await client.send_file(DB_CHANNEL_ID, message.media, caption="🔄 Processing...")
            await client.download_media(vault_msg, file=filename, progress_callback=progress_callback)
            await vault_msg.delete()
            return True
    except: pass
    return False

# ==========================================
# 🌐 WEB SERVER
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

        response = web.StreamResponse()
        response.headers['Content-Type'] = mime_type
        response.headers['Content-Disposition'] = f'attachment; filename="{file_name}"'
        response.content_length = file_size
        
        await response.prepare(request)
        async for chunk in bot.iter_download(message.media):
            await response.write(chunk)    
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
    args = event.text.split()
    is_joined = await check_subscription(sender.id)
    
    if len(args) > 1:
        param = args[1]
        if not is_joined:
            msg = "⛔ **Access Denied!**\n\nYou must join our main channel to download this file."
            btn = [[Button.url("📢 Join Channel", url=CHANNEL_LINK or "https://t.me/MaxCinemaOfficial")],
                   [Button.url("🔄 Try Again", url=f"https://t.me/{event.client.me.username}?start={param}")] ]
            return await event.reply(msg, buttons=btn)
            
        await event.reply("📂 **Fetching your files...**")
        if "pack_" in param:
            try:
                _, start_id, end_id = param.split('_')
                start_id = int(start_id)
                end_id = int(end_id)
                ids_to_fetch = list(range(start_id, end_id + 1))
                chunk_size = 20
                for i in range(0, len(ids_to_fetch), chunk_size):
                    chunk = ids_to_fetch[i : i + chunk_size]
                    await bot.forward_messages(event.chat_id, chunk, DB_CHANNEL_ID)
                    await asyncio.sleep(1)
            except: await event.reply("❌ Pack not found.")
        else:
            try:
                msg_id = int(param)
                await bot.forward_messages(event.chat_id, msg_id, DB_CHANNEL_ID)
            except: await event.reply("❌ File not found.")
        return

    if AUTH_USERS and sender.id in AUTH_USERS:
        admin_guide = (
            "**👑 MAXCINEMA ADMIN GUIDE**\n\n"
            "**1️⃣ MIRROR (Full)**\nReply `/mirror name.mp4`\n"
            "• Adds to Vault + Doodstream\n\n"
            "**2️⃣ ADD (Instant)**\nReply `/add` to a video.\n"
            "• Adds to Vault ONLY.\n\n"
            "**3️⃣ POST (Auto)**\nReply Photo + `/post` to a bot message.\n\n"
            "**4️⃣ POST ID (Manual)**\nPhoto Caption: `/postid 1234 Caption...`\n"
            "• Posts ID 1234 directly to channel."
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
    if not query: return await event.reply("❌ Usage: `/request Name`")
    if not AUTH_USERS: return await event.reply("❌ No Admins.")
    try:
        for admin_id in AUTH_USERS:
            try:
                await bot.send_message(admin_id, f"📩 **NEW REQUEST!**\n👤 {sender.first_name}\n📝 `{query}`")
            except: continue
        await event.reply("✅ **Request Sent!**")
    except Exception as e:
        await event.reply(f"❌ Error sending request: {e}")

# 🆕 COMMAND 1: /add (Instant Save)
@bot.on(events.NewMessage(pattern='/add'))
async def add_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return
    reply = await event.get_reply_message()
    if not reply or not reply.media: return await event.reply("❌ Please reply to a video or file.")

    try:
        original_caption = reply.text or ""
        vault_msg = await bot.send_file(DB_CHANNEL_ID, reply.media, caption=original_caption)
        msg = (
            f"✅ **File Added to DB!**\n\n"
            f"📂 **Vault ID:** {vault_msg.id}\n"
            f"🔗 **Doodstream:** N/A\n\n"
            f"👇 Reply with Photo + `/post` to publish."
        )
        await event.reply(msg)
    except Exception as e:
        await event.reply(f"❌ Error Adding: {e}")

# 🆕 COMMAND 2: /postid (Manual Post by ID)
@bot.on(events.NewMessage(pattern='/postid'))
async def postid_handler(event):
    sender = await event.get_sender()
    if not AUTH_USERS or sender.id not in AUTH_USERS: return
    
    # Expected Format: /postid 1055 This is the caption
    args = event.text.split(' ', 2)
    
    if len(args) < 3: 
        return await event.reply("❌ **Usage:** Send Photo with caption:\n`/postid 1234 Your Movie Caption`")
    
    try:
        vault_id = int(args[1])
        caption = args[2]
    except:
        return await event.reply("❌ **Error:** ID must be a number.\nEx: `/postid 1055 Caption`")

    # Generate Buttons
    me = await bot.get_me()
    deep_link = f"https://t.me/{me.username}?start={vault_id}"
    
    buttons = []
    if WEBSITE_HOME: buttons.append([Button.url("🌍 Visit Website", url=WEBSITE_HOME)])
    buttons.append([Button.url("📂 Get File", url=deep_link)])

    # Get Poster from the message itself or reply
    poster = await event.download_media() if event.photo else None
    
    if not poster:
        reply = await event.get_reply_message()
        if reply and reply.photo:
            poster = await reply.download_media()

    try:
        await bot.send_file(PUBLIC_CHANNEL_ID, poster, caption=caption, buttons=buttons)
        if poster: os.remove(poster)
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
    except: return await event.reply("❌ Invalid Format. Use `100-107`")

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
        await event.reply(f"✅ Pack Published!\n🔗 **Link:** {pack_link}")
    except Exception as e: await event.reply(f"❌ Error: {e}")

@bot.on(events.NewMessage(pattern='/post'))
async def post_handler(event):
    if "/postpack" in event.text: return 
    if "/postid" in event.text: return
    
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
        await event.reply(f"✅ Published!\nButtons added: {len(buttons[0]) if buttons else 0}")
    except Exception as e: await event.reply(f"❌ Error: {e}")

# ==========================================
# 🧠 CORE PROCESSOR (Called by Queue Worker)
# ==========================================
async def process_task(event, source, name, thumb_path):
    status_msg = await event.reply(f"⏳ **Initializing:** `{name}`...")
    last_time = [0]
    try:
        # 1. DOWNLOAD
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
                            async for chunk in resp.content.iter_chunked(4 * 1024 * 1024): 
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

        # 2. THUMBNAIL
        current_thumb = thumb_path
        if not current_thumb:
            generated_thumb = f"{name}_thumb.jpg"
            cmd = ["ffmpeg", "-i", name, "-ss", "00:00:05", "-vframes", "1", generated_thumb, "-y"]
            process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await process.wait()
            if os.path.exists(generated_thumb): current_thumb = generated_thumb

        # 3. UPLOAD TO VAULT
        await status_msg.edit("⚡ **Uploading to Vault...**")
        last_time = [0]
        async def up_callback(c, t): await progress_bar(c, t, status_msg, "☁️ **Uploading to Vault...**", last_time)
        try:
            vault_msg = await bot.send_file(DB_CHANNEL_ID, file=name, caption=f"🔒 {name}", thumb=current_thumb, supports_streaming=True, progress_callback=up_callback)
        except Exception as e: return False

        # 4. UPLOAD TO DOODSTREAM
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

        # 5. FINISH (Clean)
        final_msg = (
            f"✅ **Mirror Complete!**\n\n"
            f"📂 **Vault ID:** {vault_msg.id}\n"
            f"🔗 **Doodstream:** {dood_link}\n\n"
            f"👇 Reply with Photo + `/post` to publish."
        )
        await status_msg.edit(final_msg)
        
        # Cleanup
        if os.path.exists(name): os.remove(name)
        return True
        
    except Exception as e:
        await status_msg.edit(f"❌ Error: {str(e)}")
        return False

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
    
    if batch_thumb: pass

async def start_web_server():
    app = web.Application()
    app.add_routes([
        web.get("/", root_handler),
        web.get("/stream/{msg_id}", stream_handler)
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    
    # 👇 CHANGE THIS LINE
    # Hugging Face expects Port 7860
    port = int(os.environ.get("PORT", 7860)) 
    
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web Server Started on Port {port}")


if __name__ == '__main__':
    bot.start(bot_token=BOT_TOKEN)
    bot.loop.create_task(start_web_server())
    bot.loop.create_task(worker())
    bot.loop.create_task(refresh_cache())
    bot.run_until_disconnected()








