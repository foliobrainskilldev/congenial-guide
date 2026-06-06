# backend/gemini_service.py
import os
os.environ["ANONYMIZED_TELEMETRY"] = "False"

import json
import logging
import traceback
from typing import Dict, Any, Tuple
import chromadb
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

class GeminiService:
    def __init__(self):
        self.model_name = 'gemini-2.5-flash'
        self.chroma_client = chromadb.Client()
        self.collection_name = "aura_conhecimento"
        
        self.collection = self.chroma_client.get_or_create_collection(
            name=self.collection_name
        )
        self._carregar_base_conhecimento()

    def _carregar_base_conhecimento(self):
        try:
            caminho_arquivo = os.path.join(os.path.dirname(__file__), "conhecimento.json") if '__file__' in globals() else "conhecimento.json"
            if not os.path.exists(caminho_arquivo):
                logger.warning("Ficheiro conhecimento.json não encontrado. RAG vazio.")
                return

            with open(caminho_arquivo, "r", encoding="utf-8") as f:
                dados = json.load(f)
            
            if not dados:
                return

            ids = [item["id"] for item in dados]
            documentos = [item["texto"] for item in dados]
            
            self.collection.add(documents=documentos, ids=ids)
            logger.info("✅ Base de conhecimento RAG carregada no Chroma local.")
        except Exception as e:
            logger.error(f"Erro ao carregar RAG: {e}")

    def _obter_contexto_rag(self, query: str) -> str:
        if self.collection.count() == 0:
            return ""
        try:
            resultados = self.collection.query(
                query_texts=[query],
                n_results=2
            )
            documentos_recuperados = resultados.get("documents", [[]])[0]
            return "\n".join(documentos_recuperados)
        except Exception as e:
            logger.error(f"Erro ao buscar no ChromaDB: {e}")
            return ""

    # NOVO: Recebe 'info_agendamentos' da base de dados
    def processar_mensagem(self, query: str, historico: str, info_agendamentos: str = "") -> Tuple[str, Dict[str, Any]]:
        contexto_rag = self._obter_contexto_rag(query)
        
        tool_agendar = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="agendar_tratamento",
                    description="Agenda um novo tratamento estético.",
                    parameters={
                        "type": "OBJECT",
                        "properties": {
                            "nome_cliente": {"type": "STRING", "description": "Nome completo do cliente."},
                            "data_hora": {"type": "STRING", "description": "Data e hora solicitada."},
                            "servico_estetico_desejado": {"type": "STRING", "description": "O tratamento desejado."}
                        },
                        "required": ["nome_cliente", "data_hora", "servico_estetico_desejado"]
                    }
                )
            ]
        )

        # NOVO: O prompt agora injeta o status real da base de dados
        prompt_sistema = (
            "És a assistente virtual da Clínica de Estética Avançada 'Aura Estética'. "
            "Sê educada, profissional e concisa.\n"
            f"Histórico da conversa:\n{historico}\n\n"
            f"Informações da clínica (RAG):\n{contexto_rag}\n\n"
            f"ESTADO DOS AGENDAMENTOS DO CLIENTE NO SISTEMA:\n{info_agendamentos}\n"
            "ATENÇÃO: Se o utilizador perguntar sobre o seu agendamento, confere o estado acima. "
            "Se o estado for 'Confirmado', diz-lhe que está Confirmado. Se for 'Pendente', diz que ainda está a ser analisado.\n\n"
            f"Mensagem do utilizador: {query}"
        )

        dados_agendamento = None

        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt_sistema,
                config=types.GenerateContentConfig(
                    tools=[tool_agendar],
                    temperature=0.3
                )
            )

            texto_resposta = ""
            chamou_funcao = False
            
            if response.candidates and response.candidates[0].content and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if getattr(part, 'function_call', None):
                        func_call = part.function_call
                        if func_call.name == "agendar_tratamento":
                            chamou_funcao = True
                            args = func_call.args if isinstance(func_call.args, dict) else dict(func_call.args)
                            
                            dados_agendamento = {
                                "nome_cliente": args.get("nome_cliente", "Cliente"),
                                "data_hora": args.get("data_hora", "Horário a definir"),
                                "servico_estetico_desejado": args.get("servico_estetico_desejado", "Serviço")
                            }
                            texto_resposta = (f"Perfeito, {dados_agendamento['nome_cliente']}! "
                                              f"O teu pedido para {dados_agendamento['servico_estetico_desejado']} "
                                              f"para as {dados_agendamento['data_hora']} foi recebido e está como Pendente. "
                                              f"A nossa equipa vai validar e confirmar brevemente.")
                            break

            if not chamou_funcao:
                try:
                    texto_resposta = response.text if response.text else "Não consegui formular uma resposta, pode reformular?"
                except ValueError:
                    texto_resposta = "Anotado! Em que mais posso ajudar?"

            return texto_resposta, dados_agendamento

        except Exception as e:
            logger.error(f"Erro no Gemini: {e}")
            logger.error(traceback.format_exc())
            return "Aguarde um momento, estou a processar a sua informação...", None

gemini_service = GeminiService()