import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.config import settings

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()
_sync_job = None


def register_sync_job(sync_func):
    global _sync_job
    interval_hours = settings.SCHEDULER_INTERVAL_HOURS

    if _sync_job:
        scheduler.remove_job("auto_sync")

    _sync_job = scheduler.add_job(
        sync_func,
        trigger=IntervalTrigger(hours=interval_hours),
        id="auto_sync",
        name="Auto sync data from web to Google Sheets",
        replace_existing=True,
    )
    logger.info(f"Auto sync scheduled every {interval_hours} hour(s)")


def start():
    if not scheduler.running:
        scheduler.start()
        logger.info("Scheduler started")


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
