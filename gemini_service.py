# backend/gemini_service.py
import os
import json
import logging
import traceback
from typing import Dict, Any, Tuple

# --- CORREÇÃO: SILENCIAR O ERRO FALSO DO CHROMADB (POSTHOG) ---
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_TELEMETRY_DISABLED"] = "1"
logging.getLogger('chromadb.telemetry.product.posthog').setLevel(logging.CRITICAL)

import chromadb
from chromadb.config import Settings
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

class GeminiService:
    def __init__(self):
        # --- CORREÇÃO: MUDAR O MODELO PARA O ESTÁVEL E DE ALTO LIMITE ---
        # gemini-2.0-flash é o modelo principal com 1500 requisições diárias gratuitas
        self.model_name = 'gemini-2.0-flash'
        
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
        contexto_rag = self._obter_contexto_rag(query)
        
        tools_clinica = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="acao_sistema",
                    description="Executa ações vitais no sistema (agendar, cancelar, reagendar, chamar humano ou enviar botões).",
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            "tipo_acao": types.Schema(
                                type="STRING", 
                                description="Escolhe: 'agendar', 'cancelar', 'reagendar', 'chamar_humano', 'enviar_botoes' ou 'nenhuma'."
                            ),
                            "nome_cliente": types.Schema(type="STRING", description="Nome do cliente (se aplicável)."),
                            "data_hora": types.Schema(type="STRING", description="Data/Hora desejada (ex: 15/06 às 14:00)."),
                            "servico": types.Schema(type="STRING", description="Serviço estético desejado."),
                            "texto_mensagem": types.Schema(type="STRING", description="Texto que o bot deve dizer ao cliente."),
                            "botao_1": types.Schema(type="STRING", description="Texto do botão 1 (máx 20 letras)."),
                            "botao_2": types.Schema(type="STRING", description="Texto do botão 2 (máx 20 letras)."),
                            "botao_3": types.Schema(type="STRING", description="Texto do botão 3 (máx 20 letras).")
                        },
                        required=["tipo_acao", "texto_mensagem"]
                    )
                )
            ]
        )

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
            f"AGENDA OCUPADA DA CLÍNICA (NÃO MARCAR AQUI):\n{agenda_ocupada}\n\n"
            f"MENSAGEM DO CLIENTE: {query}"
        )

        acao_bot = {
            "tipo_acao": "nenhuma",
            "texto_mensagem": "",
            "dados": {}
        }

        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt_sistema,
                config=types.GenerateContentConfig(
                    tools=[tools_clinica],
                    temperature=0.5 
                )
            )

            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if getattr(part, 'function_call', None):
                        func_call = part.function_call
                        if func_call.name == "acao_sistema":
                            
                            args = func_call.args or {}
                            if not isinstance(args, dict):
                                try:
                                    args = dict(args)
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

            texto_normal = ""
            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if getattr(part, 'text', None):
                        texto_normal += part.text
            
            if not texto_normal:
                try:
                    texto_normal = response.text if response.text else "Não entendi, podes reformular?"
                except Exception:
                    texto_normal = "Não entendi, podes reformular?"

            acao_bot["texto_mensagem"] = texto_normal
            return texto_normal, acao_bot

        except Exception as e:
            erro_str = str(e)
            logger.error(f"Erro no Gemini: {e}")
            logger.error(traceback.format_exc())
            
            if "429" in erro_str or "RESOURCE_EXHAUSTED" in erro_str:
                acao_bot["texto_mensagem"] = "Desculpe, o nosso sistema está a receber muitos pedidos neste momento e atingiu o limite. ⏳ Por favor, aguarde cerca de um minuto e tente novamente."
            else:
                acao_bot["texto_mensagem"] = "Aguarde um momento, ocorreu uma pequena falha ao processar a sua informação."
                
            return acao_bot["texto_mensagem"], acao_bot

gemini_service = GeminiService()