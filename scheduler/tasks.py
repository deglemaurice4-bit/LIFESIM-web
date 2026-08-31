import asyncio
import logging
from handlers.economy import process_daily_tax, process_lottery_draw
from handlers.bank import process_bank_interests
from handlers.competitions import start_new_competition, end_competition_and_reward
from database import init_db

logger = logging.getLogger(__name__)

async def start_scheduler():
    await init_db()
    logger.info("Scheduler démarré")
    while True:
        try:
            # Tâches quotidiennes
            await process_daily_tax()
            await process_lottery_draw()
            await process_bank_interests()
            await start_new_competition()
            await end_competition_and_reward()
            # Ajoute d'autres tâches ici (social, maintenance, etc.)
        except Exception as e:
            logger.error(f"Erreur dans le scheduler: {e}")
        await asyncio.sleep(3600)  # toutes les heures