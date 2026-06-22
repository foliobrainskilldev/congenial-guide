# backend/main.py
import os
import httpx
import logging
import traceback
import json
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis.asyncio as redis
from bson import ObjectId

# --- LOCAL IMPORTS ---
from database import DatabaseManager, get_appointments_collection, get_logs_collection, get_users_collection
from ai_service import ai_service
from scheduler import start_scheduler
from security import verify_password, create_access_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ENVIRONMENT VARIABLES ---
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
API_VERSION = "v21.0"

# --- APP INITIALIZATION ---
app = FastAPI(title="Aura Esthetics API", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

redis_client = redis.from_url(REDIS_URL, decode_responses=True)
active_connections = [] # For WebSockets

# --- STARTUP & SHUTDOWN EVENTS ---
@app.on_event("startup")
async def startup_event():
    await DatabaseManager.connect_db()
    start_scheduler()
    logger.info("🚀 Aura Esthetics Backend is running!")

@app.on_event("shutdown")
async def shutdown_event():
    await DatabaseManager.close_db()
    await redis_client.close()

# --- Pydantic Models ---
class LoginRequest(BaseModel):
    username: str
    password: str

class AppointmentUpdate(BaseModel):
    status: str

# ==========================================
# 🔒 AUTHENTICATION & REST ENDPOINTS
# ==========================================

@app.post("/api/auth/login")
async def login(credentials: LoginRequest):
    # Hardcoded fallback (Allows initial login to setup the system)
    if credentials.username == os.getenv("ADMIN_USER", "admin") and credentials.password == os.getenv("ADMIN_PASS", "123456"):
        access_token = create_access_token(data={"sub": credentials.username}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
        return {"token": access_token, "status": "success"}
    
    # Real Database User Check
    user = await get_users_collection().find_one({"username": credentials.username})
    if not user or not verify_password(credentials.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = create_access_token(data={"sub": user["username"]}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"token": access_token, "status": "success"}

@app.get("/api/appointments")
async def list_appointments(current_user: str = Depends(get_current_user)):
    cursor = get_appointments_collection().find().sort("created_at", -1)
    appointments = [ {**doc, "_id": str(doc["_id"])} async for doc in cursor ]
    return {"appointments": appointments}

@app.patch("/api/appointments/{appointment_id}")
async def update_appointment(appointment_id: str, update_data: AppointmentUpdate, current_user: str = Depends(get_current_user)):
    result = await get_appointments_collection().update_one(
        {"_id": ObjectId(appointment_id)}, 
        {"$set": {"status": update_data.status}}
    )
    if result.modified_count == 1:
        return {"message": "Updated successfully"}
    raise HTTPException(status_code=404, detail="Appointment not found")

@app.get("/api/logs")
async def list_logs(current_user: str = Depends(get_current_user)):
    cursor = get_logs_collection().find().sort("timestamp", -1).limit(50)
    logs = [ {**doc, "_id": str(doc["_id"])} async for doc in cursor ]
    return {"logs": logs}


# ==========================================
# 💬 LIVE CHAT WEBSOCKETS (ADMIN DASHBOARD)
# ==========================================

@app.websocket("/ws/admin")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # If admin replies directly from the dashboard to a client via WS:
            msg_data = json.loads(data)
            await send_whatsapp_message(msg_data["phone"], msg_data["text"])
    except WebSocketDisconnect:
        active_connections.remove(websocket)

async def broadcast_to_admins(message: dict):
    for connection in active_connections:
        try: 
            await connection.send_json(message)
        except: 
            pass


# ==========================================
# 📱 WHATSAPP WEBHOOK & UTILS
# ==========================================

async def download_whatsapp_audio(media_id: str) -> bytes:
    url = f"https://graph.facebook.com/{API_VERSION}/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        if res.status_code == 200:
            media_url = res.json().get("url")
            media_res = await client.get(media_url, headers=headers)
            return media_res.content
    return b""

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Verifies the Webhook with Meta (Facebook)"""
    if request.query_params.get("hub.mode") == "subscribe" and request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=request.query_params.get("hub.challenge"), media_type="text/plain")
    raise HTTPException(status_code=403)

@app.post("/webhook")
async def process_whatsapp_message(request: Request):
    """Processes incoming messages from WhatsApp"""
    try:
        body = await request.json()
        entries = body.get("entry", [])
        if not entries or not entries[0].get("changes"): return {"status": "ok"}
            
        messages = entries[0]["changes"][0].get("value", {}).get("messages", [])
        if not messages: return {"status": "ok"}
            
        msg_obj = messages[0]
        sender_phone = msg_obj.get("from")
        message_id = msg_obj.get("id") 

        # 1. RATE LIMITING (Anti-Spam: Max 15 msgs / minute)
        rate_key = f"rate_limit:{sender_phone}"
        requests_count = await redis_client.incr(rate_key)
        if requests_count == 1: 
            await redis_client.expire(rate_key, 60)
        if requests_count > 15: 
            return {"status": "ok"} # Ignore spam quietly

        # 2. EXTRACT TEXT OR TRANSCRIBE AUDIO
        msg_text = ""
        msg_type = msg_obj.get("type")

        if msg_type == "text":
            msg_text = msg_obj.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            msg_text = msg_obj.get("interactive", {}).get("button_reply", {}).get("title", "")
        elif msg_type == "audio":
            media_id = msg_obj.get("audio", {}).get("id")
            audio_bytes = await download_whatsapp_audio(media_id)
            if audio_bytes:
                # Send to Groq Whisper for transcription
                msg_text = ai_service.transcribe_audio(audio_bytes)
            else:
                msg_text = "[Could not download audio]"
            
        if not msg_text:
            await send_whatsapp_message(sender_phone, "Sorry, I can only understand text, buttons, and voice notes!")
            return {"status": "ok"}

        # 3. BROADCAST LIVE MESSAGE TO DASHBOARD (WebSockets)
        await broadcast_to_admins({"phone": sender_phone, "sender": "client", "text": msg_text})

        # 4. HANDOVER CHECK (Is Human Assistant talking?)
        handover_key = f"handover:{sender_phone}"
        if await redis_client.get(handover_key):
            # Do nothing, let the human finish chatting on the dashboard
            return {"status": "ok"}

        if message_id: 
            await mark_as_read_and_typing(message_id)

        redis_key = f"chat_history:{sender_phone}"
        history = await redis_client.get(redis_key) or ""
        
        appointments_col = get_appointments_collection()
        
        # Extract Client Info and Busy Schedule for AI Context
        cursor_client = appointments_col.find({"client_phone": sender_phone}).sort("created_at", -1).limit(2)
        client_info = "\n".join([f"- {ag['service']} ({ag['date_time']}) Status: {ag['status']}" async for ag in cursor_client]) or "None."

        today = datetime.utcnow()
        cursor_schedule = appointments_col.find({
            "status": {"$ne": "Canceled"}, 
            "created_at": {"$gte": today - timedelta(days=30)} 
        }).limit(20)
        busy_schedule = "\n".join([f"- Busy: {ag['date_time']}" async for ag in cursor_schedule]) or "Completely free schedule."
        
        # 5. SEND TO AI
        response_text, bot_action = ai_service.process_message(msg_text, history, client_info, busy_schedule)
        action_type = bot_action.get("action_type")
        data = bot_action.get("data", {})
        
        # 6. EXECUTE AI AUTONOMOUS ACTIONS
        if action_type == "schedule":
            await appointments_col.insert_one({
                "client_phone": sender_phone,
                "client_name": data.get("client_name", "Client"),
                "date_time": data.get("date_time", "To be defined"),
                "service": data.get("service", "Esthetics"),
                "status": "Pending",
                "created_at": datetime.utcnow()
            })
        
        elif action_type == "cancel":
            await appointments_col.update_one(
                {"client_phone": sender_phone, "status": {"$ne": "Canceled"}},
                {"$set": {"status": "Canceled"}}
            )

        elif action_type == "call_human":
            # Pause the AI for this user for 12 hours (43200 seconds)
            await redis_client.set(handover_key, "true", ex=43200) 
            response_text = "I'm transferring you to a human assistant now. They will take over this conversation shortly."

        # 7. SAVE LOG & HISTORY
        new_history = f"{history}\nClient: {msg_text}\nAI: {response_text}"
        await redis_client.set(redis_key, new_history[-1500:], ex=7200) 
        
        await get_logs_collection().insert_one({
            "phone": sender_phone, 
            "client_message": msg_text,
            "ai_response": f"[{action_type.upper()}] {response_text}", 
            "timestamp": datetime.utcnow()
        })
        
        # 8. SEND RESPONSE BACK TO WHATSAPP & DASHBOARD
        buttons = data.get("buttons", [])
        if action_type == "send_buttons" and len(buttons) > 0:
            await send_whatsapp_buttons(sender_phone, response_text, buttons)
        else:
            await send_whatsapp_message(sender_phone, response_text)
            
        await broadcast_to_admins({"phone": sender_phone, "sender": "ai", "text": response_text})

        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        logger.error(traceback.format_exc())
        return {"status": "ok"}

# --- OUTBOUND WHATSAPP API FUNCTIONS ---

async def mark_as_read_and_typing(message_id: str):
    try:
        url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp", 
            "status": "read", 
            "message_id": message_id, 
            "typing_indicator": {"type": "text"}
        }
        async with httpx.AsyncClient() as client:
            await client.post(url, headers=headers, json=payload)
    except: 
        pass

async def send_whatsapp_message(to_phone: str, message: str):
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}}
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload)

async def send_whatsapp_buttons(to_phone: str, text: str, buttons: list):
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    formatted_buttons = []
    for i, btn_text in enumerate(buttons[:3]): 
        formatted_buttons.append({
            "type": "reply",
            "reply": {"id": f"btn_{i}", "title": btn_text[:20]}
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": { "text": text },
            "action": { "buttons": formatted_buttons }
        }
    }
    
    async with httpx.AsyncClient() as client:
        res = await client.post(url, headers=headers, json=payload)
        # Fallback to standard text if button API fails
        if res.status_code != 200:
            await send_whatsapp_message(to_phone, f"{text}\n\nOptions:\n- " + "\n- ".join(buttons))