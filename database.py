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
        """Establishes an asynchronous connection with MongoDB."""
        mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
        try:
            cls.client = AsyncIOMotorClient(mongo_uri)
            cls.db = cls.client.aura_esthetics
            logger.info("✅ Successfully connected to MongoDB (Motor).")
        except Exception as e:
            logger.error(f"❌ Error connecting to MongoDB: {e}")
            raise e

    @classmethod
    async def close_db(cls):
        """Closes the connection with MongoDB."""
        if cls.client:
            cls.client.close()
            logger.info("🔌 Connection with MongoDB closed.")

# Utilities for quick access to collections
def get_appointments_collection():
    return DatabaseManager.db["appointments"]

def get_logs_collection():
    return DatabaseManager.db["logs"]

def get_users_collection():
    return DatabaseManager.db["users"]

def get_metrics_collection():
    return DatabaseManager.db["metrics"]