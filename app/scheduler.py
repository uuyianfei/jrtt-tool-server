import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .crawler import (
    run_author_articles_job,
    run_author_articles_loop,
    run_author_collect_job,
    run_crawl_job,
    run_recommend_news_job,
)

logger = logging.getLogger(__name__)


class AppScheduler:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.started = False

    def init_app(self, app):
        crawl_seconds = app.config["CRAWL_INTERVAL_SECONDS"]
        author_collect_seconds = app.config["AUTHOR_COLLECT_INTERVAL_SECONDS"]
        author_articles_seconds = app.config["AUTHOR_CRAWL_INTERVAL_SECONDS"]
        author_articles_continuous = bool(app.config.get("AUTHOR_ARTICLES_CONTINUOUS_ENABLED", True))
        job_jitter = int(app.config.get("JOB_JITTER_SECONDS", 0))
        crawl_enabled = bool(app.config.get("CRAWL_JOB_ENABLED", True))
        crawl_direct_recommend_enabled = bool(app.config.get("CRAWL_DIRECT_RECOMMEND_ENABLED", False))
        author_collect_enabled = bool(app.config.get("AUTHOR_COLLECT_JOB_ENABLED", True))
        author_articles_enabled = bool(app.config.get("AUTHOR_ARTICLES_JOB_ENABLED", True))

        def crawl_wrapper():
            with app.app_context():
                try:
                    if crawl_direct_recommend_enabled:
                        run_recommend_news_job()
                    else:
                        run_crawl_job()
                except Exception as exc:
                    logger.exception("crawl job error: %s", exc)

        def author_collect_wrapper():
            with app.app_context():
                try:
                    run_author_collect_job()
                except Exception as exc:
                    logger.exception("author collect job error: %s", exc)

        def author_articles_wrapper():
            with app.app_context():
                try:
                    if author_articles_continuous:
                        run_author_articles_loop()
                    else:
                        run_author_articles_job()
                except Exception as exc:
                    logger.exception("author articles job error: %s", exc)

        if author_collect_enabled:
            self.scheduler.add_job(
                author_collect_wrapper,
                trigger="interval",
                seconds=author_collect_seconds,
                id="author_collect_job",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now(),
                jitter=job_jitter,
            )
        else:
            logger.info("author collect job disabled by AUTHOR_COLLECT_JOB_ENABLED=false")

        if author_articles_enabled:
            if author_articles_continuous:
                self.scheduler.add_job(
                    author_articles_wrapper,
                    trigger="date",
                    id="author_articles_job",
                    replace_existing=True,
                    max_instances=1,
                    # 启动后以常驻循环运行，不再走固定间隔调度
                    next_run_time=datetime.now() + timedelta(seconds=10),
                )
                logger.info("author articles job running in continuous loop mode")
            else:
                self.scheduler.add_job(
                    author_articles_wrapper,
                    trigger="interval",
                    seconds=author_articles_seconds,
                    id="author_articles_job",
                    replace_existing=True,
                    max_instances=1,
                    # 与作者采集任务错峰启动，减少 author_sources 行锁争用
                    next_run_time=datetime.now() + timedelta(seconds=10),
                    jitter=job_jitter,
                )
        else:
            logger.info("author articles job disabled by AUTHOR_ARTICLES_JOB_ENABLED=false")

        # legacy entry, default off to avoid duplicate execution
        if crawl_enabled:
            self.scheduler.add_job(
                crawl_wrapper,
                trigger="interval",
                seconds=crawl_seconds,
                id="crawl_job",
                replace_existing=True,
                max_instances=1,
                next_run_time=datetime.now(),
                jitter=job_jitter,
            )
            if crawl_direct_recommend_enabled:
                logger.info("crawl job enabled in direct recommend mode")
            else:
                logger.warning("legacy crawl job enabled; may overlap with dual-line jobs")
        else:
            logger.info("legacy crawl job disabled by CRAWL_JOB_ENABLED=false")

    def start(self):
        if not self.started:
            self.scheduler.start()
            self.started = True


scheduler = AppScheduler()
