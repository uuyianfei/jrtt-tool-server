"""Apply schema changes for article-author association and metrics status fields."""

from __future__ import annotations

from sqlalchemy import inspect, text

from app import create_app
from app.extensions import db


def _has_column(inspector, table_name: str, col_name: str) -> bool:
    cols = inspector.get_columns(table_name)
    return any(c.get("name") == col_name for c in cols)


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    idx = inspector.get_indexes(table_name)
    return any(i.get("name") == index_name for i in idx)


def main() -> int:
    app = create_app(enable_scheduler=False)
    with app.app_context():
        inspector = inspect(db.engine)
        statements = []

        if not _has_column(inspector, "articles", "author_id"):
            statements.append(
                "ALTER TABLE articles ADD COLUMN author_id INT NULL"
            )
        if not _has_column(inspector, "articles", "metrics_status"):
            statements.append(
                "ALTER TABLE articles ADD COLUMN metrics_status VARCHAR(32) NOT NULL DEFAULT 'pending'"
            )
        if not _has_column(inspector, "articles", "metrics_checked_at"):
            statements.append(
                "ALTER TABLE articles ADD COLUMN metrics_checked_at DATETIME NULL"
            )
        if not _has_column(inspector, "articles", "metrics_error"):
            statements.append(
                "ALTER TABLE articles ADD COLUMN metrics_error VARCHAR(512) NOT NULL DEFAULT ''"
            )

        for stmt in statements:
            app.logger.info("apply ddl: %s", stmt)
            db.session.execute(text(stmt))
        if statements:
            db.session.commit()

        inspector = inspect(db.engine)
        index_statements = []
        if not _has_index(inspector, "articles", "ix_articles_author_id"):
            index_statements.append("CREATE INDEX ix_articles_author_id ON articles (author_id)")
        if not _has_index(inspector, "articles", "ix_articles_metrics_status"):
            index_statements.append("CREATE INDEX ix_articles_metrics_status ON articles (metrics_status)")
        if not _has_index(inspector, "articles", "ix_articles_metrics_checked_at"):
            index_statements.append(
                "CREATE INDEX ix_articles_metrics_checked_at ON articles (metrics_checked_at)"
            )

        for stmt in index_statements:
            app.logger.info("apply ddl: %s", stmt)
            db.session.execute(text(stmt))

        if index_statements:
            db.session.commit()

        fk_exists = False
        for fk in inspector.get_foreign_keys("articles"):
            constrained = fk.get("constrained_columns") or []
            if constrained == ["author_id"]:
                fk_exists = True
                break
        if not fk_exists:
            stmt = (
                "ALTER TABLE articles "
                "ADD CONSTRAINT fk_articles_author_id "
                "FOREIGN KEY (author_id) REFERENCES author_sources(id)"
            )
            app.logger.info("apply ddl: %s", stmt)
            db.session.execute(text(stmt))
            db.session.commit()

        app.logger.info("schema migration finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
