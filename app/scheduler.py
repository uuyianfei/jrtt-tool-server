import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from .crawler import cleanup_expired_articles, run_crawl_job

logger = logging.getLogger(__name__)


class AppScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.started = False

    def init_app(self, app):
        crawl_seconds = app.config["CRAWL_INTERVAL_SECONDS"]
        cleanup_minutes = app.config["CLEANUP_INTERVAL_MINUTES"]
        crawl_enabled = bool(app.config.get("CRAWL_JOB_ENABLED", True))
        cleanup_enabled = bool(app.config.get("CLEANUP_JOB_ENABLED", True))

        def crawl_wrapper():
            with app.app_context():
                try:
                    run_crawl_job()
                except Exception as exc:
                    logger.exception("crawl job error: %s", exc)

        def cleanup_wrapper():
            with app.app_context():
                try:
                    cleanup_expired_articles()
                except Exception as exc:
                    logger.exception("cleanup job error: %s", exc)

        if crawl_enabled:
            self.scheduler.add_job(
                crawl_wrapper,
                trigger="interval",
                seconds=crawl_seconds,
                id="crawl_job",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now(),
            )
        else:
            logger.info("crawl job disabled by CRAWL_JOB_ENABLED=false")

        if cleanup_enabled:
            self.scheduler.add_job(
                cleanup_wrapper,
                trigger="interval",
                minutes=cleanup_minutes,
                id="cleanup_job",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now(),
            )
        else:
            logger.info("cleanup job disabled by CLEANUP_JOB_ENABLED=false")

    def start(self):
        if not self.started:
            self.scheduler.start()
            self.started = True


scheduler = AppScheduler()
