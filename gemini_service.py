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

# Configuração da API do Google Gemini (Novo SDK)
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
        # Inicializa o ChromaDB em memória (ou persistente)
        self.chroma_client = chromadb.Client()
        self.collection_name = "aura_conhecimento"
        self.embedding_fn = GeminiEmbeddingFunction()
        
        # Cria ou obtém a coleção
        self.collection = self.chroma_client.get_or_create_collection(
            name=self.collection_name, 
            embedding_function=self.embedding_fn
        )
        self._carregar_base_conhecimento()

    def _carregar_base_conhecimento(self):
        """Lê o conhecimento.json e indexa no ChromaDB (RAG)."""
        try:
            # Estrutura esperada do conhecimento.json: [{"id": "1", "texto": "Tratamento X..."}]
            caminho_arquivo = os.path.join(os.path.dirname(__els__file__), "conhecimento.json") if '__file__' in globals() else "conhecimento.json"
            if not os.path.exists(caminho_arquivo):
                logger.warning("Ficheiro conhecimento.json não encontrado. RAG vazio.")
                return

            with open(caminho_arquivo, "r", encoding="utf-8") as f:
                dados = json.load(f)
            
            if not dados:
                return

            ids = [item["id"] for item in dados]
            documentos = [item["texto"] for item in dados]
            
            # Adiciona ao ChromaDB
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
        """
        Gera a resposta usando o Gemini 2.5 Flash, passando o contexto do RAG 
        e disponibilizando a ferramenta (Function Calling) de agendamento.
        Retorna (texto_da_resposta, dados_agendamento_extraidos_se_houver)
        """
        contexto_rag = self._obter_contexto_rag(query)
        
        # Define a Ferramenta (Function Calling) para agendamento
        tool_agendar = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="agendar_tratamento",
                    description="Agenda um tratamento estético na clínica capturando os dados essenciais do cliente.",
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "nome_cliente": types.Schema(type=types.Type.STRING, description="Nome completo do cliente."),
                            "data_hora": types.Schema(type=types.Type.STRING, description="Data e hora solicitada (ex: 2024-05-20 15:00)."),
                            "servico_estetico_desejado": types.Schema(type=types.Type.STRING, description="O tratamento que o cliente deseja realizar (ex: Botox, Limpeza de Pele).")
                        },
                        required=["nome_cliente", "data_hora", "servico_estetico_desejado"]
                    )
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
            
            # Verifica se o Gemini decidiu chamar a função
            if response.function_calls:
                for func_call in response.function_calls:
                    if func_call.name == "agendar_tratamento":
                        # Extrai os argumentos devolvidos pela IA
                        args = func_call.args
                        dados_agendamento = {
                            "nome_cliente": args.get("nome_cliente", ""),
                            "data_hora": args.get("data_hora", ""),
                            "servico_estetico_desejado": args.get("servico_estetico_desejado", "")
                        }
                        texto_resposta = (f"Perfeito, {dados_agendamento['nome_cliente']}! "
                                          f"O teu pedido para {dados_agendamento['servico_estetico_desejado']} "
                                          f"para {dados_agendamento['data_hora']} foi registado. A clínica irá confirmar em breve.")
            else:
                texto_resposta = response.text

            return texto_resposta, dados_agendamento

        except Exception as e:
            logger.error(f"Erro no Gemini: {e}")
            return "Peço desculpa, mas estou a enfrentar problemas técnicos neste momento. Tente novamente mais tarde.", None

# Instância Singleton do Serviço
gemini_service = GeminiService()