# backend/database.py
import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

class DatabaseManager:
    client: AsyncIOMotorClient = None
    db = None

    @classmethod
    async def connect_db(cls):
        """Estabelece a conexão assíncrona com o MongoDB."""
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        try:
            cls.client = AsyncIOMotorClient(mongo_uri)
            # Utiliza o banco de dados 'aura_estetica'
            cls.db = cls.client.aura_estetica
            logger.info("✅ Conectado com sucesso ao MongoDB (Motor).")
        except Exception as e:
            logger.error(f"❌ Erro ao conectar ao MongoDB: {e}")
            raise e

    @classmethod
    async def close_db(cls):
        """Encerra a conexão com o MongoDB."""
        if cls.client:
            cls.client.close()
            logger.info("🔌 Conexão com o MongoDB encerrada.")

# Utilitários para acesso rápido às coleções
def get_agendamentos_collection():
    return DatabaseManager.db["agendamentos"]

def get_logs_collection():
    return DatabaseManager.db["logs"]