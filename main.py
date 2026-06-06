# backend/main.py
import os
import httpx
import logging
import json
from datetime import datetime
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis.asyncio as redis

from database import DatabaseManager, get_agendamentos_collection, get_logs_collection
from gemini_service import gemini_service

# Configuração de Logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variáveis de Ambiente
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
FRONTEND_URL = os.getenv("FRONTEND_URL", "*") # Se *, aceita tudo. Se URL, aceita só do Frontend.

# Instância FastAPI
app = FastAPI(title="Aura Estética API", version="1.0.0")

# Configuração CORS (Requisito Arquitetural Desacoplado)
origins = ["*"] if FRONTEND_URL == "*" else [FRONTEND_URL]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cliente Redis Assíncrono para estado/histórico
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

@app.on_event("startup")
async def startup_db_client():
    await DatabaseManager.connect_db()

@app.on_event("shutdown")
async def shutdown_db_client():
    await DatabaseManager.close_db()
    await redis_client.close()

# --- MODELOS PYDANTIC ---
class LoginRequest(BaseModel):
    username: str
    password: str

class AgendamentoUpdate(BaseModel):
    status: str

# --- ENDPOINTS REST PARA O FRONTEND ---

@app.post("/api/auth/login")
async def login(credentials: LoginRequest):
    # Simulação simples de autenticação exigida pelo requisito. 
    # Em produção, usa hashing (bcrypt) e JWT real.
    ADMIN_USER = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASS = os.getenv("ADMIN_PASS", "123456")
    
    if credentials.username == ADMIN_USER and credentials.password == ADMIN_PASS:
        return {"token": "simulated-jwt-token-7b89a", "status": "success"}
    raise HTTPException(status_code=401, detail="Credenciais inválidas")

@app.get("/api/agendamentos")
async def listar_agendamentos():
    agendamentos_collection = get_agendamentos_collection()
    cursor = agendamentos_collection.find().sort("criado_em", -1)
    agendamentos = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        agendamentos.append(doc)
    return {"agendamentos": agendamentos}

@app.patch("/api/agendamentos/{id_agendamento}")
async def atualizar_agendamento(id_agendamento: str, update_data: AgendamentoUpdate):
    from bson import ObjectId
    agendamentos_collection = get_agendamentos_collection()
    resultado = await agendamentos_collection.update_one(
        {"_id": ObjectId(id_agendamento)}, 
        {"$set": {"status": update_data.status}}
    )
    if resultado.modified_count == 1:
        return {"mensagem": "Status atualizado com sucesso"}
    raise HTTPException(status_code=404, detail="Agendamento não encontrado")

@app.get("/api/logs")
async def listar_logs():
    logs_collection = get_logs_collection()
    cursor = logs_collection.find().sort("timestamp", -1).limit(50)
    logs = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        logs.append(doc)
    return {"logs": logs}


# --- WEBHOOKS META WHATSAPP CLOUD API ---

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Validação do Webhook (GET) exigida pela Meta."""
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook da Meta verificado com sucesso!")
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Falha na verificação")

@app.post("/webhook")
async def process_whatsapp_message(request: Request):
    """Recebe mensagens, processa RAG+Gemini e responde."""
    body = await request.json()
    
    try:
        # Extração defensiva do payload do WhatsApp
        entry = body.get("entry", [])[0]
        changes = entry.get("changes", [])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return {"status": "ok", "msg": "Sem mensagens para processar."}
            
        msg_obj = messages[0]
        sender_phone = msg_obj.get("from")
        msg_text = msg_obj.get("text", {}).get("body", "")
        
        if not msg_text:
            return {"status": "ok"}

        # 1. Recupera histórico do Redis
        redis_key = f"chat_history:{sender_phone}"
        historico = await redis_client.get(redis_key) or ""
        
        # 2. IA gera a resposta e verifica se houve "Function Call"
        resposta_ia, dados_agendamento = gemini_service.processar_mensagem(msg_text, historico)
        
        # 3. Atualiza o histórico no Redis (Expira em 1 hora)
        novo_historico = f"{historico}\nCliente: {msg_text}\nIA: {resposta_ia}"
        await redis_client.set(redis_key, novo_historico[-1000:], ex=3600) 
        
        # 4. Envia resposta de volta via Meta Graph API
        await enviar_mensagem_whatsapp(sender_phone, resposta_ia)
        
        # 5. Salva Log de Interação no MongoDB
        logs_collection = get_logs_collection()
        await logs_collection.insert_one({
            "telefone": sender_phone,
            "mensagem_cliente": msg_text,
            "resposta_ia": resposta_ia,
            "timestamp": datetime.utcnow()
        })
        
        # 6. Se Function Calling capturou um agendamento, salva no MongoDB
        if dados_agendamento:
            agendamentos_collection = get_agendamentos_collection()
            await agendamentos_collection.insert_one({
                "telefone_cliente": sender_phone,
                "nome_cliente": dados_agendamento["nome_cliente"],
                "data_hora": dados_agendamento["data_hora"],
                "servico": dados_agendamento["servico_estetico_desejado"],
                "status": "Pendente",
                "criado_em": datetime.utcnow()
            })

        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Erro no processamento do webhook: {e}")
        return {"status": "error"}

async def enviar_mensagem_whatsapp(to_phone: str, message: str):
    """Envia resposta usando httpx assíncrono."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message}
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload)