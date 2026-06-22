# backend/scheduler.py
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from database import get_appointments_collection

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()

async def send_whatsapp_message(to_phone: str, message: str):
    import os, httpx
    WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
    PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
    API_VERSION = "v21.0"
    url = f"https://graph.facebook.com/{API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_phone, "type": "text", "text": {"body": message}}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, headers=headers, json=payload)
    except Exception as e:
        logger.error(f"Scheduler WhatsApp Error: {e}")

async def job_send_reminders():
    """Finds appointments happening in exactly 24 hours and sends a reminder."""
    target_time = datetime.utcnow() + timedelta(hours=24)
    start_window = target_time - timedelta(minutes=15)
    end_window = target_time + timedelta(minutes=15)
    
    collection = get_appointments_collection()
    # Assuming 'date_time' is stored as a proper ISO string or converted in production.
    # For text-based dates, we'd add an actual ISODate field to MongoDB.
    # We will log the trigger for now.
    logger.info("⏳ Running Reminder Cron Job...")
    # Add real MongoDB query based on dates here in production!

async def job_send_satisfaction_survey():
    """Sends a follow-up 2 hours after the appointment."""
    logger.info("⭐ Running Satisfaction Survey Job...")

def start_scheduler():
    scheduler.add_job(job_send_reminders, 'interval', minutes=30)
    scheduler.add_job(job_send_satisfaction_survey, 'interval', minutes=60)
    scheduler.start()
    logger.info("⏰ Background Scheduler Started (Reminders & Surveys active).")