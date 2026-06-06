# backend/main.py
import os
import httpx
import logging
import traceback
from datetime import datetime
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis.asyncio as redis
from bson import ObjectId

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
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")

# Instância FastAPI
app = FastAPI(title="Aura Estética API", version="1.0.0")

# Configuração CORS
origins = ["*"] if FRONTEND_URL == "*" else [FRONTEND_URL]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cliente Redis Assíncrono
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
    hub_mode = request.query_params.get("hub.mode")
    hub_challenge = request.query_params.get("hub.challenge")
    hub_verify_token = request.query_params.get("hub.verify_token")

    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook da Meta verificado com sucesso!")
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Falha na verificação")

@app.post("/webhook")
async def process_whatsapp_message(request: Request):
    try:
        body = await request.json()
        
        entries = body.get("entry", [])
        if not entries:
            return {"status": "ok"}
            
        changes = entries[0].get("changes", [])
        if not changes:
            return {"status": "ok"}
            
        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        
        if not messages:
            return {"status": "ok", "msg": "Evento ignorado"}
            
        msg_obj = messages[0]
        sender_phone = msg_obj.get("from")
        message_id = msg_obj.get("id") 
        
        msg_text = msg_obj.get("text", {}).get("body", "")
        if not msg_text:
            await enviar_mensagem_whatsapp(sender_phone, "Ainda não consigo ouvir áudios ou ver imagens. Por favor, escreve a tua mensagem em texto!")
            return {"status": "ok"}

        # 1. MARCA COMO LIDA IMEDIATAMENTE (PONTINHOS AZUIS)
        if message_id:
            await marcar_como_lida(message_id)

        # 2. TENTA ATIVAR O ESTADO "A ESCREVER..."
        await mostrar_a_escrever(sender_phone)

        # 3. Recupera histórico do Redis
        redis_key = f"chat_history:{sender_phone}"
        historico = await redis_client.get(redis_key) or ""
        
        # 4. Busca os agendamentos reais
        agendamentos_collection = get_agendamentos_collection()
        cursor = agendamentos_collection.find({"telefone_cliente": sender_phone}).sort("criado_em", -1).limit(3)
        agendamentos_db = []
        async for doc in cursor:
            agendamentos_db.append(doc)
            
        texto_agendamentos = ""
        if agendamentos_db:
            texto_agendamentos = "Agendamentos atuais deste cliente na base de dados:\n"
            for ag in agendamentos_db:
                texto_agendamentos += f"- Serviço: {ag.get('servico')}, Data: {ag.get('data_hora')}, Status: {ag.get('status')}\n"
        else:
            texto_agendamentos = "O cliente ainda não tem agendamentos registados no sistema."
        
        # 5. IA gera a resposta
        resposta_ia, dados_agendamento = gemini_service.processar_mensagem(msg_text, historico, texto_agendamentos)
        
        # 6. Atualiza o histórico
        novo_historico = f"{historico}\nCliente: {msg_text}\nIA: {resposta_ia}"
        await redis_client.set(redis_key, novo_historico[-1000:], ex=3600) 
        
        # 7. Envia a resposta final
        await enviar_mensagem_whatsapp(sender_phone, resposta_ia)
        
        # 8. Logs e BD
        logs_collection = get_logs_collection()
        await logs_collection.insert_one({
            "telefone": sender_phone,
            "mensagem_cliente": msg_text,
            "resposta_ia": resposta_ia,
            "timestamp": datetime.utcnow()
        })
        
        if dados_agendamento:
            await agendamentos_collection.insert_one({
                "telefone_cliente": sender_phone,
                "nome_cliente": dados_agendamento.get("nome_cliente", "Desconhecido"),
                "data_hora": dados_agendamento.get("data_hora", "A Combinar"),
                "servico": dados_agendamento.get("servico_estetico_desejado", "Serviço Geral"),
                "status": "Pendente",
                "criado_em": datetime.utcnow()
            })

        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Erro no processamento do webhook: {e}")
        logger.error(traceback.format_exc()) 
        return {"status": "ok"} 

# --- FUNÇÕES META API ---

async def marcar_como_lida(message_id: str):
    """Avisa a Meta que a mensagem foi lida, gerando os pontinhos azuis no cliente."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id
    }
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, headers=headers, json=payload)
    except Exception as e:
        logger.error(f"Erro ao marcar mensagem como lida: {e}")

async def mostrar_a_escrever(to_phone: str):
    """Testa a funcionalidade 'A escrever...' baseada na sugestão."""
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    # Aqui colocamos exatamente o parâmetro sugerido, adaptado com o "to" (destinatário) obrigatório da Meta
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "typing_indicator": {
            "type": "text"
        }
    }
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            # Se a Meta não aceitar isto, devolve um erro. Escrevemos no log para descobrir!
            if response.status_code != 200:
                logger.warning(f"Resposta da Meta sobre o 'A escrever': {response.text}")
    except Exception as e:
        logger.error(f"Erro ao tentar mostrar 'A escrever': {e}")

async def enviar_mensagem_whatsapp(to_phone: str, message: str):
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