"""Drop FOREIGN KEY on articles.author_id (optional, one-time).

When present, InnoDB enforces referential checks on INSERT/UPDATE of articles rows,
which can serialize with concurrent updates to the same author_sources row (1205).

This tool removes only the FK constraint; ``ix_articles_author_id`` and the column remain.
Application code should still resolve author_id via ``author_sources`` (as fast_crawler does).

To re-add the FK later, run ``tools/migrate_article_author_metrics_schema.py`` (idempotent
for missing FK only).
"""

from __future__ import annotations

import argparse

from sqlalchemy import inspect, text

from app import create_app
from app.extensions import db


def main() -> int:
    parser = argparse.ArgumentParser(description="Drop articles.author_id foreign key (optional)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required: confirm intent to drop FK",
    )
    args = parser.parse_args()
    if not args.yes:
        parser.error("refusing to run without --yes")

    app = create_app(enable_scheduler=False)
    with app.app_context():
        inspector = inspect(db.engine)
        for fk in inspector.get_foreign_keys("articles") or []:
            constrained = fk.get("constrained_columns") or []
            if constrained != ["author_id"]:
                continue
            name = fk.get("name")
            if not name:
                continue
            stmt = text(f"ALTER TABLE articles DROP FOREIGN KEY `{name}`")
            app.logger.info("apply ddl: %s", stmt)
            db.session.execute(stmt)
            db.session.commit()
            app.logger.info("dropped FK name=%s", name)
            return 0
        app.logger.info("no author_id foreign key on articles; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
