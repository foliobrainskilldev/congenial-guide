import os
import json
import logging
import traceback
from typing import Dict, Any, Tuple

# --- SILENCIAR O ERRO FALSO DO CHROMADB (POSTHOG) ---
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_TELEMETRY_DISABLED"] = "1"
logging.getLogger('chromadb.telemetry.product.posthog').setLevel(logging.CRITICAL)

import chromadb
from chromadb.config import Settings
from groq import Groq

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
# Inicia o cliente Groq
client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

class AIService:
    def __init__(self):
        # Llama 3.3 70B Versatile é o modelo de ponta recomendado na Groq. 
        # Extremamente inteligente, rápido e com limites altos.
        self.model_name = 'llama-3.3-70b-versatile'
        
        self.chroma_client = chromadb.Client(Settings(anonymized_telemetry=False))
        self.collection_name = "aura_conhecimento"
        self.collection = self.chroma_client.get_or_create_collection(name=self.collection_name)
        self._carregar_base_conhecimento()

    def _carregar_base_conhecimento(self):
        try:
            caminho_arquivo = os.path.join(os.path.dirname(__file__), "conhecimento.json") if '__file__' in globals() else "conhecimento.json"
            if not os.path.exists(caminho_arquivo):
                return
            with open(caminho_arquivo, "r", encoding="utf-8") as f:
                dados = json.load(f)
            if not dados:
                return
            ids = [item["id"] for item in dados]
            documentos = [item["texto"] for item in dados]
            self.collection.add(documents=documentos, ids=ids)
            logger.info("✅ Base de conhecimento RAG carregada.")
        except Exception as e:
            logger.error(f"Erro ao carregar RAG: {e}")

    def _obter_contexto_rag(self, query: str) -> str:
        try:
            if self.collection.count() == 0:
                return ""
            resultados = self.collection.query(query_texts=[query], n_results=2)
            return "\n".join(resultados.get("documents", [[]])[0])
        except Exception:
            return ""

    def processar_mensagem(self, query: str, historico: str, info_cliente: str, agenda_ocupada: str) -> Tuple[str, Dict[str, Any]]:
        if not client:
            return "Erro: Chave da Groq não configurada (GROQ_API_KEY).", {"tipo_acao": "nenhuma"}

        contexto_rag = self._obter_contexto_rag(query)
        
        # Ferramenta traduzida para o formato que a Groq/OpenAI usa
        tools_clinica = [
            {
                "type": "function",
                "function": {
                    "name": "acao_sistema",
                    "description": "Executa ações vitais no sistema (agendar, cancelar, reagendar, chamar humano ou enviar botões).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tipo_acao": {
                                "type": "string",
                                "description": "Escolhe: 'agendar', 'cancelar', 'reagendar', 'chamar_humano', 'enviar_botoes' ou 'nenhuma'."
                            },
                            "nome_cliente": {"type": "string", "description": "Nome do cliente (se aplicável)."},
                            "data_hora": {"type": "string", "description": "Data/Hora desejada (ex: 15/06 às 14:00)."},
                            "servico": {"type": "string", "description": "Serviço estético desejado."},
                            "texto_mensagem": {"type": "string", "description": "Texto que o bot deve dizer ao cliente."},
                            "botao_1": {"type": "string", "description": "Texto do botão 1 (máx 20 letras)."},
                            "botao_2": {"type": "string", "description": "Texto do botão 2 (máx 20 letras)."},
                            "botao_3": {"type": "string", "description": "Texto do botão 3 (máx 20 letras)."}
                        },
                        "required": ["tipo_acao", "texto_mensagem"]
                    }
                }
            }
        ]

        prompt_sistema = (
            "És a rececionista virtual avançada da 'Aura Estética'. "
            "REGRAS DE OURO:\n"
            "1. ADAPTA-TE AO HUMOR: Analisa se o cliente está feliz, apressado ou irritado. Responde com a emoção correspondente. Não sejas robótica. Usa linguagem natural e variada.\n"
            "2. CALENDÁRIO: NUNCA agendes para um horário que esteja na lista de 'Agenda Ocupada'. Se pedirem, sugere outro horário livre próximo.\n"
            "3. BOTÕES DO WHATSAPP: Se precisares que o cliente confirme algo ou escolha entre 2 opções, usa a 'tipo_acao': 'enviar_botoes' e preenche o botao_1 e botao_2.\n"
            "4. PASSAR A HUMANO: Se o cliente estiver chateado, confuso, ou pedir para falar com uma pessoa, usa a 'tipo_acao': 'chamar_humano'.\n"
            "5. CANCELAR/REAGENDAR: Se o cliente quiser cancelar ou mudar a hora, usa 'cancelar' ou 'reagendar'.\n\n"
            f"HISTÓRICO DO CHAT:\n{historico}\n\n"
            f"BASE DE DADOS RAG:\n{contexto_rag}\n\n"
            f"AGENDAMENTOS ATUAIS DESTE CLIENTE:\n{info_cliente}\n\n"
            f"AGENDA OCUPADA DA CLÍNICA (NÃO MARCAR AQUI):\n{agenda_ocupada}\n"
        )

        acao_bot = {
            "tipo_acao": "nenhuma",
            "texto_mensagem": "",
            "dados": {}
        }

        messages = [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": query}
        ]

        try:
            response = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=tools_clinica,
                tool_choice="auto",
                temperature=0.5,
                max_completion_tokens=1024
            )

            response_message = response.choices[0].message
            
            # Se a IA decidiu chamar a nossa ferramenta
            if response_message.tool_calls:
                for tool_call in response_message.tool_calls:
                    if tool_call.function.name == "acao_sistema":
                        try:
                            args = json.loads(tool_call.function.arguments)
                        except Exception:
                            args = {}
                        
                        acao_bot["tipo_acao"] = args.get("tipo_acao", "nenhuma")
                        acao_bot["texto_mensagem"] = args.get("texto_mensagem", "Anotado!")
                        
                        acao_bot["dados"] = {
                            "nome_cliente": args.get("nome_cliente", ""),
                            "data_hora": args.get("data_hora", ""),
                            "servico": args.get("servico", ""),
                            "botoes": [b for b in [args.get("botao_1"), args.get("botao_2"), args.get("botao_3")] if b]
                        }
                        return acao_bot["texto_mensagem"], acao_bot

            # Se não chamou ferramenta, devolve o texto normal da resposta
            texto_normal = response_message.content
            if not texto_normal:
                texto_normal = "Não entendi, podes reformular?"
                
            acao_bot["texto_mensagem"] = texto_normal
            return texto_normal, acao_bot

        except Exception as e:
            logger.error(f"Erro no Groq: {e}")
            logger.error(traceback.format_exc())
            acao_bot["texto_mensagem"] = "Aguarde um momento, ocorreu uma pequena falha técnica. Voltarei a tentar num instante!"
            return acao_bot["texto_mensagem"], acao_bot

ai_service = AIService()