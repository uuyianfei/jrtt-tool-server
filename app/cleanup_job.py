"""Periodic cleanup: expired articles and stale distributed-claim rows."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Dict

from flask import current_app

from .extensions import db
from .models import Article, ArticleWriteClaim, AuthorFansClaim, FastCrawlClaim
from .time_utils import cn_now_naive

# rewrite_tasks 不与 articles 级联删除；见 cleanup_expired_articles 文档

logger = logging.getLogger(__name__)


def cleanup_expired_articles() -> Dict[str, int]:
    """
    Delete articles older than ``CLEANUP_ARTICLE_MAX_AGE_HOURS`` and claim rows
    whose ``expires_at`` is older than ``CLAIM_CLEANUP_RETENTION_HOURS`` beyond now.

    Does **not** delete ``rewrite_tasks``：改写记录独立保留，后期可用 ``fromTaskId`` 或
    任务内已缓存的 ``source_html`` 在无 ``articles`` 行时再次发起改写。
    """
    expire_hours = int(current_app.config.get("CLEANUP_ARTICLE_MAX_AGE_HOURS", 24))
    expire_before = cn_now_naive() - timedelta(hours=max(1, expire_hours))
    deleted_articles = (
        Article.query.filter(
            ((Article.published_at.isnot(None)) & (Article.published_at < expire_before))
            | ((Article.published_at.is_(None)) & (Article.created_at < expire_before))
        )
        .delete(synchronize_session=False)
    )
    claim_retention_hours = int(current_app.config.get("CLAIM_CLEANUP_RETENTION_HOURS", 24))
    claim_expire_before = cn_now_naive() - timedelta(hours=max(1, claim_retention_hours))
    deleted_fast_claims = (
        FastCrawlClaim.query.filter(FastCrawlClaim.expires_at < claim_expire_before).delete(
            synchronize_session=False
        )
    )
    deleted_author_claims = (
        AuthorFansClaim.query.filter(AuthorFansClaim.expires_at < claim_expire_before).delete(
            synchronize_session=False
        )
    )
    deleted_article_write_claims = (
        ArticleWriteClaim.query.filter(ArticleWriteClaim.expires_at < claim_expire_before).delete(
            synchronize_session=False
        )
    )
    db.session.commit()
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
