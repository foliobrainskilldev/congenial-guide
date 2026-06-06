# backend/main.py
import os
import httpx
import logging
import traceback
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis.asyncio as redis
from bson import ObjectId

from database import DatabaseManager, get_agendamentos_collection, get_logs_collection

# --- ALTERAÇÃO AQUI: Importar o novo AI Service em vez do Gemini ---
from ai_service import ai_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
VERIFY_TOKEN = os.getenv("WEBHOOK_VERIFY_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
FRONTEND_URL = os.getenv("FRONTEND_URL", "*")
API_VERSION = "v21.0" 

app = FastAPI(title="Aura Estética API", version="2.0.0")

origins = ["*"] if FRONTEND_URL == "*" else [FRONTEND_URL]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

@app.on_event("startup")
async def startup_db_client():
    await DatabaseManager.connect_db()

@app.on_event("shutdown")
async def shutdown_db_client():
    await DatabaseManager.close_db()
    await redis_client.close()

class LoginRequest(BaseModel):
    username: str
    password: str

class AgendamentoUpdate(BaseModel):
    status: str

# --- ENDPOINTS REST ---
@app.post("/api/auth/login")
async def login(credentials: LoginRequest):
    if credentials.username == os.getenv("ADMIN_USER", "admin") and credentials.password == os.getenv("ADMIN_PASS", "123456"):
        return {"token": "simulated-jwt", "status": "success"}
    raise HTTPException(status_code=401, detail="Credenciais inválidas")

@app.get("/api/agendamentos")
async def listar_agendamentos():
    cursor = get_agendamentos_collection().find().sort("criado_em", -1)
    agendamentos = [ {**doc, "_id": str(doc["_id"])} async for doc in cursor ]
    return {"agendamentos": agendamentos}

@app.patch("/api/agendamentos/{id_agendamento}")
async def atualizar_agendamento(id_agendamento: str, update_data: AgendamentoUpdate):
    resultado = await get_agendamentos_collection().update_one({"_id": ObjectId(id_agendamento)}, {"$set": {"status": update_data.status}})
    if resultado.modified_count == 1:
        return {"mensagem": "Atualizado"}
    raise HTTPException(status_code=404)

@app.get("/api/logs")
async def listar_logs():
    cursor = get_logs_collection().find().sort("timestamp", -1).limit(50)
    logs = [ {**doc, "_id": str(doc["_id"])} async for doc in cursor ]
    return {"logs": logs}

# --- WEBHOOK WHATSAPP ---
@app.get("/webhook")
async def verify_webhook(request: Request):
    if request.query_params.get("hub.mode") == "subscribe" and request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=request.query_params.get("hub.challenge"), media_type="text/plain")
    raise HTTPException(status_code=403)

@app.post("/webhook")
async def process_whatsapp_message(request: Request):
    try:
        body = await request.json()
        entries = body.get("entry", [])
        if not entries or not entries[0].get("changes"): return {"status": "ok"}
            
        messages = entries[0]["changes"][0].get("value", {}).get("messages", [])
        if not messages: return {"status": "ok"}
            
        msg_obj = messages[0]
        sender_phone = msg_obj.get("from")
        message_id = msg_obj.get("id") 

        # 1. IDENTIFICAR SE É TEXTO OU UM CLIQUE NUM BOTÃO INTERATIVO
        msg_text = ""
        if msg_obj.get("type") == "text":
            msg_text = msg_obj.get("text", {}).get("body", "")
        elif msg_obj.get("type") == "interactive":
            msg_text = msg_obj.get("interactive", {}).get("button_reply", {}).get("title", "")
            
        if not msg_text:
            await enviar_mensagem_whatsapp(sender_phone, "Desculpa, por enquanto só consigo entender texto ou cliques nos botões!")
            return {"status": "ok"}

        # 2. VERIFICAR HANDOVER (PAUSA PARA ATENDIMENTO HUMANO)
        handover_key = f"handover:{sender_phone}"
        is_handover = await redis_client.get(handover_key)
        if is_handover:
            await get_logs_collection().insert_one({
                "telefone": sender_phone,
                "mensagem_cliente": f"[MODO HUMANO] {msg_text}",
                "resposta_ia": "(Bot Pausado)",
                "timestamp": datetime.utcnow()
            })
            return {"status": "ok"}

        if message_id:
            await marcar_como_lida_e_a_escrever(message_id)

        redis_key = f"chat_history:{sender_phone}"
        historico = await redis_client.get(redis_key) or ""
        
        agendamentos_collection = get_agendamentos_collection()
        
        # 3. EXTRAIR INFORMAÇÃO DO CLIENTE E AGENDA GERAL (CALENDÁRIO)
        cursor_cliente = agendamentos_collection.find({"telefone_cliente": sender_phone}).sort("criado_em", -1).limit(2)
        info_cliente = "\n".join([f"- {ag['servico']} ({ag['data_hora']}) Status: {ag['status']}" async for ag in cursor_cliente]) or "Nenhum."

        hoje = datetime.utcnow()
        cursor_agenda = agendamentos_collection.find({
            "status": {"$ne": "Cancelado"}, 
            "criado_em": {"$gte": hoje - timedelta(days=30)} 
        }).limit(20)
        agenda_ocupada = "\n".join([f"- Ocupado: {ag['data_hora']}" async for ag in cursor_agenda]) or "Agenda totalmente livre."
        
        # 4. ENVIAR PARA A IA (Agora usando Groq)
        texto_resposta, acao_bot = ai_service.processar_mensagem(msg_text, historico, info_cliente, agenda_ocupada)
        
        # 5. EXECUTAR AÇÕES AUTÓNOMAS DA IA
        tipo_acao = acao_bot.get("tipo_acao")
        dados = acao_bot.get("dados", {})

        if tipo_acao == "agendar":
            await agendamentos_collection.insert_one({
                "telefone_cliente": sender_phone,
                "nome_cliente": dados.get("nome_cliente", "Cliente"),
                "data_hora": dados.get("data_hora", "A Definir"),
                "servico": dados.get("servico", "Estética"),
                "status": "Pendente",
                "criado_em": datetime.utcnow()
            })
        
        elif tipo_acao == "cancelar":
            await agendamentos_collection.update_one(
                {"telefone_cliente": sender_phone, "status": {"$ne": "Cancelado"}},
                {"$set": {"status": "Cancelado"}}
            )
            
        elif tipo_acao == "chamar_humano":
            await redis_client.set(handover_key, "true", ex=43200)
            texto_resposta = "Compreendo perfeitamente. Vou transferir-te para a nossa equipa humana. Eles vão assumir esta conversa em breve!"

        # 6. ATUALIZAR HISTÓRICO E LOG
        novo_historico = f"{historico}\nCliente: {msg_text}\nIA: {texto_resposta}"
        await redis_client.set(redis_key, novo_historico[-1500:], ex=7200) 
        
        await get_logs_collection().insert_one({
            "telefone": sender_phone,
            "mensagem_cliente": msg_text,
            "resposta_ia": f"[{tipo_acao.upper()}] {texto_resposta}",
            "timestamp": datetime.utcnow()
        })
        
        # 7. ENVIAR RESPOSTA PARA O WHATSAPP
        botoes = dados.get("botoes", [])
        if tipo_acao == "enviar_botoes" and len(botoes) > 0:
            await enviar_botoes_whatsapp(sender_phone, texto_resposta, botoes)
        else:
            await enviar_mensagem_whatsapp(sender_phone, texto_resposta)

        return {"status": "ok"}
        
    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        logger.error(traceback.format_exc()) 
        return {"status": "ok"} 

# --- FUNÇÕES META API ---

async def marcar_como_lida_e_a_escrever(message_id: str):
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, headers=headers, json=payload)
    except: pass

async def enviar_mensagem_whatsapp(to_phone: str, message: str):
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}}
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload)

async def enviar_botoes_whatsapp(to_phone: str, texto: str, botoes: list):
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    botoes_formatados = []
    for i, btn_text in enumerate(botoes[:3]): 
        botoes_formatados.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{i}",
                "title": btn_text[:20] 
            }
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": { "text": texto },
            "action": { "buttons": botoes_formatados }
        }
    }
    
    async with httpx.AsyncClient() as client:
        res = await client.post(url, headers=headers, json=payload)
        if res.status_code != 200:
            await enviar_mensagem_whatsapp(to_phone, f"{texto}\n\nOpções:\n- " + "\n- ".join(botoes))