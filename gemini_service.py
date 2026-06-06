# backend/gemini_service.py
import os
import json
import logging
from typing import Dict, Any, Tuple
import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Configuração da API do Google Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

class GeminiEmbeddingFunction(EmbeddingFunction):
    """Função customizada para usar o Google Embeddings no ChromaDB."""
    def __call__(self, input: Documents) -> Embeddings:
        embeddings = []
        for doc in input:
            response = client.models.embed_content(
                model='text-embedding-004',
                contents=doc
            )
            embeddings.append(response.embeddings[0].values)
        return embeddings

class GeminiService:
    def __init__(self):
        self.model_name = 'gemini-2.5-flash'
        self.chroma_client = chromadb.Client()
        self.collection_name = "aura_conhecimento"
        self.embedding_fn = GeminiEmbeddingFunction()
        
        self.collection = self.chroma_client.get_or_create_collection(
            name=self.collection_name, 
            embedding_function=self.embedding_fn
        )
        self._carregar_base_conhecimento()

    def _carregar_base_conhecimento(self):
        """Lê o conhecimento.json e indexa no ChromaDB (RAG)."""
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
            logger.info("✅ Base de conhecimento RAG carregada no ChromaDB.")
        except Exception as e:
            logger.error(f"Erro ao carregar RAG: {e}")

    def _obter_contexto_rag(self, query: str) -> str:
        """Busca as informações mais relevantes na base de dados vetorial."""
        if self.collection.count() == 0:
            return ""
        
        resultados = self.collection.query(
            query_texts=[query],
            n_results=2
        )
        documentos_recuperados = resultados.get("documents", [[]])[0]
        return "\n".join(documentos_recuperados)

    def processar_mensagem(self, query: str, historico: str) -> Tuple[str, Dict[str, Any]]:
        """Gera a resposta usando o Gemini e verifica extração de agendamento."""
        contexto_rag = self._obter_contexto_rag(query)
        
        # Tool Schema simplificado usando dicts para evitar "OBJECT" crash do SDK
        tool_agendar = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="agendar_tratamento",
                    description="Agenda um tratamento estético na clínica capturando os dados essenciais do cliente.",
                    parameters={
                        "type": "OBJECT",
                        "properties": {
                            "nome_cliente": {
                                "type": "STRING", 
                                "description": "Nome completo do cliente."
                            },
                            "data_hora": {
                                "type": "STRING", 
                                "description": "Data e hora solicitada (ex: 2024-05-20 15:00)."
                            },
                            "servico_estetico_desejado": {
                                "type": "STRING", 
                                "description": "O tratamento que o cliente deseja realizar (ex: Botox)."
                            }
                        },
                        "required": ["nome_cliente", "data_hora", "servico_estetico_desejado"]
                    }
                )
            ]
        )

        prompt_sistema = (
            "És a assistente virtual da Clínica de Estética Avançada 'Aura Estética'. "
            "Sejas educada, profissional e concisa.\n"
            f"Histórico da conversa:\n{historico}\n\n"
            f"Informações relevantes da clínica (RAG):\n{contexto_rag}\n\n"
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
            
            # Verifica Function Calling
            if response.function_calls:
                for func_call in response.function_calls:
                    if func_call.name == "agendar_tratamento":
                        # Extrai de forma segura para dict
                        args = func_call.args if isinstance(func_call.args, dict) else dict(func_call.args)
                        
                        dados_agendamento = {
                            "nome_cliente": args.get("nome_cliente", "Cliente"),
                            "data_hora": args.get("data_hora", "Horário a definir"),
                            "servico_estetico_desejado": args.get("servico_estetico_desejado", "Serviço")
                        }
                        texto_resposta = (f"Perfeito, {dados_agendamento['nome_cliente']}! "
                                          f"O teu pedido para {dados_agendamento['servico_estetico_desejado']} "
                                          f"para as {dados_agendamento['data_hora']} foi recebido. "
                                          f"Vamos analisar e confirmamos o agendamento em breve.")
            else:
                texto_resposta = response.text

            return texto_resposta, dados_agendamento

        except Exception as e:
            logger.error(f"Erro no processamento do Gemini: {e}")
            logger.error(traceback.format_exc())
            return "Aguarde um momento, estou a processar a sua informação...", None

# Instância Singleton
gemini_service = GeminiService()