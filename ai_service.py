# backend/ai_service.py
import os
import io
import json
import logging
import traceback
from typing import Dict, Any, Tuple

os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_TELEMETRY_DISABLED"] = "1"
logging.getLogger('chromadb.telemetry.product.posthog').setLevel(logging.CRITICAL)

import chromadb
from chromadb.config import Settings
from groq import Groq

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class AIService:
    def __init__(self):
        self.model_name = 'llama-3.3-70b-versatile'
        self.audio_model = 'whisper-large-v3'
        self.chroma_client = chromadb.Client(Settings(anonymized_telemetry=False))
        self.collection = self.chroma_client.get_or_create_collection(name="aura_knowledge")
        self._load_knowledge_base()

    def _load_knowledge_base(self):
        try:
            file_path = os.path.join(os.path.dirname(__file__), "conhecimento.json") if '__file__' in globals() else "conhecimento.json"
            if not os.path.exists(file_path): return
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data:
                self.collection.add(documents=[item["texto"] for item in data], ids=[item["id"] for item in data])
                logger.info("✅ RAG knowledge base loaded.")
        except Exception as e:
            logger.error(f"Error loading RAG: {e}")

    def _get_rag_context(self, query: str) -> str:
        try:
            if self.collection.count() == 0: return ""
            results = self.collection.query(query_texts=[query], n_results=2)
            return "\n".join(results.get("documents", [[]])[0])
        except Exception:
            return ""

    # NEW: Audio Transcription using Groq Whisper!
    def transcribe_audio(self, audio_bytes: bytes) -> str:
        if not client: return ""
        try:
            file_tuple = ("audio.ogg", audio_bytes, "audio/ogg")
            transcription = client.audio.transcriptions.create(
                file=file_tuple,
                model=self.audio_model
            )
            return transcription.text
        except Exception as e:
            logger.error(f"❌ Error transcribing audio: {e}")
            return "Audio message could not be transcribed."

    def process_message(self, query: str, chat_history: str, client_info: str, busy_schedule: str) -> Tuple[str, Dict[str, Any]]:
        if not client: return "Error: GROQ_API_KEY not configured.", {"action_type": "none"}

        rag_context = self._get_rag_context(query)
        
        clinic_tools = [{
            "type": "function",
            "function": {
                "name": "system_action",
                "description": "Executes vital system actions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action_type": { "type": "string", "enum": ["schedule", "cancel", "reschedule", "call_human", "send_buttons", "none"] },
                        "client_name": {"type": "string"},
                        "date_time": {"type": "string"},
                        "service": {"type": "string"},
                        "message_text": {"type": "string", "description": "Response text for the client."},
                        "buttons": { "type": "array", "items": {"type": "string"}, "description": "Max 3 buttons (max 20 chars each)." }
                    },
                    "required": ["action_type", "message_text"]
                }
            }
        }]

        system_prompt = (
            "You are the virtual receptionist of 'Aura Esthetics'.\n"
            "1. BE EMPATHETIC: Match the client's mood.\n"
            "2. CALENDAR: NEVER schedule on the 'Busy Schedule' list. Check it strictly.\n"
            "3. BUTTONS: If offering choices, use 'send_buttons' with up to 3 options.\n"
            "4. HUMAN TRANSFER: If the client is upset or demands a person, use 'call_human'.\n\n"
            f"CHAT HISTORY:\n{chat_history}\n\n"
            f"RAG DATABASE:\n{rag_context}\n\n"
            f"CLIENT APPOINTMENTS:\n{client_info}\n\n"
            f"BUSY SCHEDULE:\n{busy_schedule}\n"
        )

        bot_action = { "action_type": "none", "message_text": "", "data": {} }

        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": query}],
                tools=clinic_tools,
                tool_choice="auto",
                temperature=0.4,
                max_tokens=800
            )

            msg = response.choices[0].message
            if msg.tool_calls:
                for tool in msg.tool_calls:
                    if tool.function.name == "system_action":
                        args = json.loads(tool.function.arguments)
                        bot_action["action_type"] = args.get("action_type", "none")
                        bot_action["message_text"] = args.get("message_text", "Noted!")
                        bot_action["data"] = {
                            "client_name": args.get("client_name", ""),
                            "date_time": args.get("date_time", ""),
                            "service": args.get("service", ""),
                            "buttons": args.get("buttons", [])
                        }
                        return bot_action["message_text"], bot_action

            bot_action["message_text"] = msg.content or "I didn't quite catch that."
            return bot_action["message_text"], bot_action

        except Exception as e:
            logger.error(f"Groq API Error: {traceback.format_exc()}")
            return "System is busy, please wait.", bot_action

ai_service = AIService()