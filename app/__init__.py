import logging

from flask import Flask

from .config import Config
from .extensions import db
from .routes.articles import articles_bp
from .routes.rewrite import rewrite_bp
from .scheduler import scheduler
from .utils import error_response, success_response


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    db.init_app(app)

    with app.app_context():
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

    scheduler.init_app(app)
    scheduler.start()
    app.logger.info("Scheduler started")

    return app
