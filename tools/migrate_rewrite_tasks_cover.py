"""Add rewrite_tasks.cover for rewrite history list thumbnails."""

from __future__ import annotations

from sqlalchemy import inspect, text

from app import create_app
from app.extensions import db


def _has_column(inspector, table_name: str, col_name: str) -> bool:
    cols = inspector.get_columns(table_name)
    return any(c.get("name") == col_name for c in cols)


def main() -> int:
    app = create_app(enable_scheduler=False)
    with app.app_context():
        inspector = inspect(db.engine)
        if _has_column(inspector, "rewrite_tasks", "cover"):
            app.logger.info("rewrite_tasks.cover already exists, skip")
            return 0
        stmt = "ALTER TABLE rewrite_tasks ADD COLUMN cover VARCHAR(1024) NOT NULL DEFAULT ''"
        app.logger.info("apply ddl: %s", stmt)
        db.session.execute(text(stmt))
        db.session.commit()
        app.logger.info("rewrite_tasks.cover migration finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
