"""Periodic cleanup: expired articles and stale distributed-claim rows."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Dict, Type

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .extensions import db
from .models import Article, ArticleWriteClaim, AuthorFansClaim, FastCrawlClaim
from .time_utils import cn_now_naive

# rewrite_tasks 不与 articles 级联删除；见 cleanup_expired_articles 文档

logger = logging.getLogger(__name__)


def _is_lock_retryable(exc: Exception) -> bool:
    """InnoDB 1205 (lock wait timeout) / 1213 (deadlock)."""
    if isinstance(exc, OperationalError):
        orig = getattr(exc, "orig", None)
        args = getattr(orig, "args", None)
        errno = args[0] if args else None
        if int(errno or 0) in (1205, 1213):
            return True
    err_text = str(exc).lower()
    return "lock wait timeout exceeded" in err_text or "deadlock found" in err_text


def _delete_in_batches(
    model: Type[Any],
    filter_expr,
    batch_size: int,
    max_retries: int = 3,
    batch_sleep: float = 0.5,
) -> int:
    """Delete matching rows by primary key in chunks.

    Improvements over the original implementation:
    - Per-batch retry with exponential backoff on 1205/1213
    - Sleep between batches to yield lock window to other transactions
    - Commit after each chunk to shorten lock duration
    """
    total = 0
    bs = max(1, batch_size)
    retries = max(1, int(max_retries))
    sleep_seconds = max(0.0, float(batch_sleep))

    while True:
        ids = [
            row[0]
            for row in db.session.query(model.id)
            .filter(filter_expr)
            .order_by(model.id)
            .limit(bs)
            .all()
        ]
        if not ids:
            break

        deleted = False
        for attempt in range(1, retries + 1):
            try:
                n = db.session.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
                db.session.commit()
                total += int(n or 0)
                deleted = True
                break
            except OperationalError as exc:
                db.session.rollback()
                if not _is_lock_retryable(exc) or attempt >= retries:
                    logger.warning(
                        "cleanup batch delete failed after %s retries model=%s err=%s",
                        attempt,
                        model.__tablename__,
                        exc,
                    )
                    # Re-raise only if non-retryable; for retryable, give up this batch
                    if not _is_lock_retryable(exc):
                        raise
                    break
                delay = 0.5 * attempt
                logger.info(
                    "cleanup batch delete retry=%s/%s model=%s sleep=%.2fs",
                    attempt,
                    retries,
                    model.__tablename__,
                    delay,
                )
                time.sleep(delay)
            except Exception:
                db.session.rollback()
                raise

        if not deleted:
            # All retries exhausted for this batch; skip to next batch
            # to avoid infinite loop on the same locked rows
            logger.warning(
                "cleanup skipping batch of %s rows for %s after all retries exhausted",
                len(ids),
                model.__tablename__,
            )
            # Still break to avoid re-selecting the same rows in an infinite loop
            break

        # Sleep between batches to yield lock window
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return total


def _try_advisory_lock(lock_name: str, timeout: int = 0) -> bool:
    """Attempt to acquire a MySQL advisory lock (non-blocking by default)."""
    try:
        result = db.session.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": lock_name, "timeout": timeout},
        )
        row = result.fetchone()
        return row is not None and int(row[0] or 0) == 1
    except Exception:
        logger.warning("advisory lock acquire failed lock=%s", lock_name, exc_info=True)
        return False


def _release_advisory_lock(lock_name: str) -> None:
    """Release a MySQL advisory lock."""
    try:
        db.session.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
    except Exception:
        logger.warning("advisory lock release failed lock=%s", lock_name, exc_info=True)


def cleanup_expired_articles() -> Dict[str, int]:
    """
    Delete articles older than ``CLEANUP_ARTICLE_MAX_AGE_HOURS`` and claim rows
    whose ``expires_at`` is older than ``CLAIM_CLEANUP_RETENTION_HOURS`` beyond now.

    Does **not** delete ``rewrite_tasks``：改写记录独立保留，后期可用 ``fromTaskId`` 或
    任务内已缓存的 ``source_html`` 在无 ``articles`` 行时再次发起改写。

    Uses a MySQL advisory lock to prevent concurrent cleanup runs from
    different processes/containers from amplifying lock contention.
    """
    lock_name = "jrtt_article_cleanup"
    if not _try_advisory_lock(lock_name, timeout=0):
        logger.info("cleanup skipped: another cleanup process holds the advisory lock")
        return {
            "deleted_articles": 0,
            "deleted_fast_crawl_claims": 0,
            "deleted_author_fans_claims": 0,
            "deleted_article_write_claims": 0,
            "skipped": True,
        }

    try:
        expire_hours = int(current_app.config.get("CLEANUP_ARTICLE_MAX_AGE_HOURS", 24))
        expire_before = cn_now_naive() - timedelta(hours=max(1, expire_hours))
        batch_size = int(current_app.config.get("CLEANUP_DELETE_BATCH_SIZE", 200))
        max_retries = int(current_app.config.get("CLEANUP_BATCH_MAX_RETRIES", 3))
        batch_sleep = float(current_app.config.get("CLEANUP_BATCH_SLEEP_SECONDS", 0.5))

        article_filter = (
            ((Article.published_at.isnot(None)) & (Article.published_at < expire_before))
            | ((Article.published_at.is_(None)) & (Article.created_at < expire_before))
        )
        deleted_articles = _delete_in_batches(
            Article, article_filter, batch_size,
            max_retries=max_retries, batch_sleep=batch_sleep,
        )

        claim_retention_hours = int(current_app.config.get("CLAIM_CLEANUP_RETENTION_HOURS", 24))
        claim_expire_before = cn_now_naive() - timedelta(hours=max(1, claim_retention_hours))

        claim_filter = FastCrawlClaim.expires_at < claim_expire_before
        deleted_fast_claims = _delete_in_batches(
            FastCrawlClaim, claim_filter, batch_size,
            max_retries=max_retries, batch_sleep=batch_sleep,
        )

        deleted_author_claims = _delete_in_batches(
            AuthorFansClaim, AuthorFansClaim.expires_at < claim_expire_before, batch_size,
            max_retries=max_retries, batch_sleep=batch_sleep,
        )

        deleted_article_write_claims = _delete_in_batches(
            ArticleWriteClaim, ArticleWriteClaim.expires_at < claim_expire_before, batch_size,
            max_retries=max_retries, batch_sleep=batch_sleep,
        )

        logger.info(
            "cleanup job finished, deleted_articles=%s deleted_fast_crawl_claims=%s "
            "deleted_author_fans_claims=%s deleted_article_write_claims=%s "
            "article_max_age_hours=%s claim_retention_hours=%s",
            deleted_articles,
            deleted_fast_claims,
            deleted_author_claims,
            deleted_article_write_claims,
            expire_hours,
            claim_retention_hours,
        )
        return {
            "deleted_articles": int(deleted_articles or 0),
            "deleted_fast_crawl_claims": int(deleted_fast_claims or 0),
            "deleted_author_fans_claims": int(deleted_author_claims or 0),
            "deleted_article_write_claims": int(deleted_article_write_claims or 0),
        }
    finally:
        _release_advisory_lock(lock_name)
