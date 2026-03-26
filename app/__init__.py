import logging
import time
from datetime import datetime

from flask import Flask

from .config import Config
from .extensions import db
from .routes.articles import articles_bp
from .routes.rewrite import rewrite_bp
from .scheduler import scheduler
from .time_utils import SHANGHAI_TZ
from .utils import error_response, success_response

from sqlalchemy import text as sql_text


class ShanghaiFormatter(logging.Formatter):
    """Force log timestamps to Asia/Shanghai regardless of host timezone."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, SHANGHAI_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def create_app(enable_scheduler: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    log_format = "%(asctime)s %(levelname)s %(name)s - %(message)s"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(ShanghaiFormatter(log_format))
        root_logger.addHandler(handler)
    else:
        for handler in root_logger.handlers:
            handler.setFormatter(ShanghaiFormatter(log_format))

    db.init_app(app)

    with app.app_context():
        # Flask-SQLAlchemy `db.create_all()` may run concurrently across multiple containers.
        # On MySQL this can trigger error 1684 due to concurrent DDL.
        # Use MySQL GET_LOCK/RELEASE_LOCK to serialize schema initialization.
        if app.config.get("DB_CREATE_ALL_LOCK_ENABLED", True) and db.engine.dialect.name == "mysql":
            lock_name = str(app.config.get("DB_CREATE_ALL_LOCK_NAME") or "jrtt_db_create_all")
            lock_timeout_seconds = int(app.config.get("DB_CREATE_ALL_LOCK_TIMEOUT_SECONDS", 20))
            max_tries = int(app.config.get("DB_CREATE_ALL_LOCK_MAX_TRIES", 6))
            sleep_seconds = float(app.config.get("DB_CREATE_ALL_LOCK_SLEEP_SECONDS", 2.0))

            last_got = 0
            for attempt in range(1, max_tries + 1):
                # Keep GET_LOCK/RELEASE_LOCK/create_all on the same connection.
                with db.engine.connect() as conn:
                    got = (
                        conn.execute(
                            sql_text("SELECT GET_LOCK(:name, :timeout) AS got"),
                            {"name": lock_name, "timeout": lock_timeout_seconds},
                        ).scalar()
                        or 0
                    )
                    last_got = int(got)
                    if last_got == 1:
                        try:
                            db.metadata.create_all(bind=conn)
                        finally:
                            conn.execute(sql_text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                        break
                app.logger.info(
                    "db.create_all waiting for lock lock_name=%s attempt=%s/%s got=%s",
                    lock_name,
                    attempt,
                    max_tries,
                    last_got,
                )
                time.sleep(sleep_seconds * attempt)

            if last_got != 1:
                # Fallback: shouldn't happen often, but avoid blocking forever.
                app.logger.warning("db.create_all lock not acquired, fallback create_all (may hit MySQL 1684)")
                db.create_all()
        else:
            db.create_all()

    app.register_blueprint(articles_bp)
    app.register_blueprint(rewrite_bp)

    @app.get("/health")
    def health() -> tuple:
        return success_response({"status": "ok"})

    @app.errorhandler(404)
    def not_found(_):
        return error_response(4004, "资源不存在")

    @app.errorhandler(Exception)
    def internal_error(err):
        logging.exception("Unhandled error: %s", err)
        return error_response(5000, "服务异常")

    if enable_scheduler:
        scheduler.init_app(app)
        scheduler.start()
        app.logger.info("Scheduler started")
    else:
        app.logger.info("Scheduler disabled for this process")

    return app
