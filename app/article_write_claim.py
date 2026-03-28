"""Per-``articles.id`` distributed lease for cross-worker UPDATE coordination."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import text

from .extensions import db
from .time_utils import cn_now_naive

logger = logging.getLogger(__name__)

_UPSERT = text(
    """
    INSERT INTO article_write_claims (articles_row_id, owner, expires_at, created_at, updated_at)
    VALUES (:articles_row_id, :owner, :expires_at, :created_at, :updated_at)
    ON DUPLICATE KEY UPDATE
      owner = IF(expires_at < NOW(), VALUES(owner), owner),
      expires_at = IF(expires_at < NOW(), VALUES(expires_at), expires_at),
      updated_at = IF(expires_at < NOW(), VALUES(updated_at), updated_at)
    """
)

_VERIFY = text(
    "SELECT owner, expires_at FROM article_write_claims WHERE articles_row_id = :id LIMIT 1"
)

_DELETE = text(
    "DELETE FROM article_write_claims WHERE articles_row_id = :id AND owner = :owner"
)


def try_acquire_article_write(*, articles_row_id: int, owner: str, lease_seconds: int) -> bool:
    """
    Atomically take (or refresh) a lease on ``articles.id``.

    Uses a standalone connection transaction so ORM session state is unaffected.
    """
    now = cn_now_naive()
    sec = max(1, int(lease_seconds))
    expires_at = now + timedelta(seconds=sec)
    own = str(owner or "")[:128]
    aid = int(articles_row_id)
    try:
        with db.engine.begin() as conn:
            conn.execute(
                _UPSERT,
                {
                    "articles_row_id": aid,
                    "owner": own,
                    "expires_at": expires_at,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            row = conn.execute(_VERIFY, {"id": aid}).fetchone()
        if not row:
            return False
        row_owner, row_exp = row[0], row[1]
        if row_exp is not None and getattr(row_exp, "tzinfo", None) is not None:
            row_exp = row_exp.replace(tzinfo=None)
        return str(row_owner or "") == own and row_exp > now
    except Exception:
        logger.exception("article_write_claim acquire failed articles_row_id=%s", aid)
        return False


def release_article_write(*, articles_row_id: int, owner: str) -> None:
    own = str(owner or "")[:128]
    aid = int(articles_row_id)
    try:
        with db.engine.begin() as conn:
            conn.execute(_DELETE, {"id": aid, "owner": own})
    except Exception:
        logger.warning(
            "article_write_claim release failed articles_row_id=%s owner=%s",
            aid,
            own,
            exc_info=True,
        )
