"""Reconcile article metrics and author followers for newly ingested rows.

This worker is designed for eventual-consistency:
1) fast crawler inserts/updates articles with metrics_status=pending
2) this job recalculates followers and article metrics
3) rows are marked as checked/failed
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from datetime import timedelta
from typing import Dict, List, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from flask import current_app
from sqlalchemy import text as sql_text
from sqlalchemy.exc import OperationalError

from app import create_app
from app.article_write_claim import release_article_write, try_acquire_article_write
from app.crawler import ToutiaoCrawler
from app.extensions import db
from app.models import Article, AuthorFansClaim, AuthorSource
from app.time_utils import cn_now_naive

INFO_API_URL = "https://m.toutiao.com/i{gid}/info/"
CHECKED_REFRESH_CLAIM_ERROR = "checked-refreshing"
PENDING_REFRESH_CLAIM_ERROR = "pending-refreshing"


def _article_write_claim_owner() -> str:
    role = str(current_app.config.get("WORKER_ROLE") or "").strip()
    if role:
        return f"{role}-aw"
    return f"reconcile-aw-{socket.gethostname()}-{os.getpid()}"


def _bulk_try_acquire_article_writes(row_ids: List[int], owner: str, lease_seconds: int) -> Set[int]:
    acquired: Set[int] = set()
    for rid in row_ids:
        if try_acquire_article_write(
            articles_row_id=int(rid),
            owner=owner,
            lease_seconds=lease_seconds,
        ):
            acquired.add(int(rid))
    return acquired


def _release_article_write_claims(acquired: Set[int], owner: str) -> None:
    for aid in list(acquired):
        try:
            release_article_write(articles_row_id=int(aid), owner=owner)
        except Exception:
            current_app.logger.warning(
                "article_write_claim release failed articles_row_id=%s", aid, exc_info=True
            )


def _is_mysql_retryable_lock_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", None)
    errno = args[0] if args else None
    return int(errno or 0) in (1205, 1213)


def _commit_with_retry(*, retries: int = 3, retry_sleep_seconds: float = 0.2) -> None:
    for attempt in range(max(1, int(retries))):
        try:
            db.session.commit()
            return
        except Exception as exc:
            db.session.rollback()
            if _is_mysql_retryable_lock_error(exc) and attempt < int(retries) - 1:
                time.sleep(max(0.0, float(retry_sleep_seconds)) * (attempt + 1))
                continue
            raise


def _reconcile_author_fans_claim_owner() -> str:
    role = str(current_app.config.get("WORKER_ROLE") or "").strip()
    if role:
        return role
    hostname = socket.gethostname()
    pid = os.getpid()
    return f"reconcile-author-fans-{hostname}-{pid}"


def claim_author_ids(author_ids: List[int]) -> List[int]:
    """
    Distributed lease claim for reconcile pending "author followers" update.

    Return: only author_ids successfully claimed by this worker (still valid lease).
    """

    # Always keep behavior safe by default: when disabled, treat all as claimed.
    if not bool(current_app.config.get("AUTHOR_FANS_CLAIM_ENABLED", True)):
        return sorted({int(a) for a in author_ids if a is not None})

    uniq_author_ids = sorted({int(a) for a in author_ids if a is not None})
    if not uniq_author_ids:
        return []

    lease_seconds = int(current_app.config.get("AUTHOR_FANS_CLAIM_LEASE_SECONDS", 240))
    owner = _reconcile_author_fans_claim_owner()
    now = cn_now_naive()
    expires_at = now + timedelta(seconds=max(1, int(lease_seconds)))

    # Keep each MySQL transaction small to reduce lock duration.
    claim_batch_size = 200
    sql = sql_text(
        """
        INSERT INTO author_fans_claims (author_id, owner, expires_at, created_at, updated_at)
        VALUES (:author_id, :owner, :expires_at, :created_at, :updated_at)
        ON DUPLICATE KEY UPDATE
          owner = IF(expires_at < NOW(), VALUES(owner), owner),
          expires_at = IF(expires_at < NOW(), VALUES(expires_at), expires_at),
          updated_at = IF(expires_at < NOW(), VALUES(updated_at), updated_at)
        """
    )

    for i in range(0, len(uniq_author_ids), claim_batch_size):
        batch = uniq_author_ids[i : i + claim_batch_size]
        try:
            for author_id in batch:
                db.session.execute(
                    sql,
                    {
                        "author_id": int(author_id),
                        "owner": owner,
                        "expires_at": expires_at,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
            db.session.commit()
        except Exception:
            db.session.rollback()
            # Best-effort: keep processing other batches.
            # (The final "claimed" filtering below will ensure we only touch valid leases.)
            continue

    claimed_rows = (
        db.session.query(AuthorFansClaim.author_id)
        .filter(
            AuthorFansClaim.author_id.in_(uniq_author_ids),
            AuthorFansClaim.owner == owner,
            AuthorFansClaim.expires_at > now,
        )
        .all()
    )
    return [int(r[0]) for r in claimed_rows]


def fetch_info_api(gid: str, timeout: int = 15) -> Dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.toutiao.com/",
    }
    resp = requests.get(INFO_API_URL.format(gid=gid), headers=headers, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success"):
        return {}
    return payload.get("data") or {}


def pick_pending_articles(batch_size: int, max_hours: float) -> List[Article]:
    now = cn_now_naive()
    cutoff = now - timedelta(hours=max(0.0, float(max_hours)))
    return (
        Article.query.filter(
            Article.metrics_status != "checked",
            Article.published_at >= cutoff,
        )
        # 优先最新文章先校准
        .order_by(Article.published_at.desc(), Article.id.desc())
        .limit(max(1, int(batch_size)))
        .all()
    )


def claim_pending_articles_for_refresh(
    batch_size: int,
    max_hours: float,
    stale_seconds: int = 300,
) -> List[Article]:
    """
    Strict non-duplicate across multiple workers (pending mode).
    Claim rows before doing HTTP calls:
    - eligible: metrics_status != checked, published_at within window
    - exclude claimed rows unless their updated_at is stale
    - set metrics_error to PENDING_REFRESH_CLAIM_ERROR
    """
    now = cn_now_naive()
    cutoff = now - timedelta(hours=max(0.0, float(max_hours)))
    stale_cutoff = now - timedelta(seconds=max(0, int(stale_seconds)))
    limit = max(1, int(batch_size))

    # eligibility predicate (stale claim rows can be re-claimed)
    eligible = (
        (Article.metrics_status != "checked")
        & (Article.published_at >= cutoff)
        & (
            (Article.metrics_error != PENDING_REFRESH_CLAIM_ERROR)
            | (Article.updated_at <= stale_cutoff)
        )
    )

    # Preferred path: SELECT ... FOR UPDATE SKIP LOCKED
    try:
        q = (
            db.session.query(Article)
            .filter(eligible)
            .order_by(Article.published_at.desc(), Article.id.desc())
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        rows: List[Article] = q.all()
        if not rows:
            return []
        for row in rows:
            row.metrics_error = PENDING_REFRESH_CLAIM_ERROR
        _commit_with_retry()
        return rows
    except Exception:
        # Fallback: conditional UPDATE to ensure exclusivity
        candidate_ids = (
            db.session.query(Article.id)
            .filter(eligible)
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(limit * 5)
            .all()
        )
        claimed_ids: List[int] = []
        for (row_id,) in candidate_ids:
            updated = (
                db.session.query(Article)
                .filter(Article.id == row_id)
                .filter(
                    (Article.metrics_error != PENDING_REFRESH_CLAIM_ERROR)
                    | (Article.updated_at <= stale_cutoff)
                )
                .update({"metrics_error": PENDING_REFRESH_CLAIM_ERROR}, synchronize_session=False)
            )
            if int(updated or 0) == 1:
                claimed_ids.append(int(row_id))
                if len(claimed_ids) >= limit:
                    break
        if not claimed_ids:
            return []
        rows = db.session.query(Article).filter(Article.id.in_(claimed_ids)).all()
        _commit_with_retry()
        return rows


def pick_checked_articles_for_refresh(batch_size: int, max_hours: float) -> List[Article]:
    now = cn_now_naive()
    cutoff = now - timedelta(hours=max(0.0, float(max_hours)))
    return (
        Article.query.filter(
            Article.metrics_status == "checked",
            Article.metrics_error != CHECKED_REFRESH_CLAIM_ERROR,
            Article.published_at >= cutoff,
        )
        .order_by(
            # 优先“最新发布的文章”；同一发布时间里，优先最久未更新（或从未更新）
            Article.published_at.desc(),
            Article.metrics_checked_at.is_(None).desc(),
            Article.metrics_checked_at.asc(),
            Article.id.asc(),
        )
        .limit(max(1, int(batch_size)))
        .all()
    )


def claim_checked_articles_for_refresh(batch_size: int, max_hours: float) -> List[Article]:
    """
    Strict non-duplicate across multiple workers:
    - Atomically claim rows before doing HTTP calls.
    - Claim uses `metrics_error` sentinel and `SELECT ... FOR UPDATE SKIP LOCKED`.
    """
    now = cn_now_naive()
    cutoff = now - timedelta(hours=max(0.0, float(max_hours)))
    limit = max(1, int(batch_size))

    base_q = (
        db.session.query(Article.id)
        .filter(
            Article.metrics_status == "checked",
            Article.metrics_error != CHECKED_REFRESH_CLAIM_ERROR,
            Article.published_at >= cutoff,
        )
        .order_by(
            Article.published_at.desc(),
            Article.metrics_checked_at.is_(None).desc(),
            Article.metrics_checked_at.asc(),
            Article.id.asc(),
        )
    )

    # Preferred path: SELECT ... FOR UPDATE SKIP LOCKED
    try:
        q = (
            db.session.query(Article)
            .filter(
                Article.metrics_status == "checked",
                Article.metrics_error != CHECKED_REFRESH_CLAIM_ERROR,
                Article.published_at >= cutoff,
            )
            .order_by(
                Article.published_at.desc(),
                Article.metrics_checked_at.is_(None).desc(),
                Article.metrics_checked_at.asc(),
                Article.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )

        rows: List[Article] = q.all()
        if not rows:
            return []

        for row in rows:
            row.metrics_error = CHECKED_REFRESH_CLAIM_ERROR

        _commit_with_retry()
        return rows
    except Exception:
        # Fallback path: conditional UPDATE to guarantee exclusivity even without SKIP LOCKED
        # (May be slower, but keeps strict non-duplicate.)
        candidate_ids = [row_id for (row_id,) in base_q.limit(limit * 5).all()]
        if not candidate_ids:
            return []

        claimed_ids: List[int] = []
        for row_id in candidate_ids:
            updated = (
                db.session.query(Article)
                .filter(Article.id == row_id, Article.metrics_error != CHECKED_REFRESH_CLAIM_ERROR)
                .update({"metrics_error": CHECKED_REFRESH_CLAIM_ERROR}, synchronize_session=False)
            )
            if int(updated or 0) == 1:
                claimed_ids.append(int(row_id))
                if len(claimed_ids) >= limit:
                    break

        if not claimed_ids:
            return []

        rows = db.session.query(Article).filter(Article.id.in_(claimed_ids)).all()
        _commit_with_retry()
        return rows


def reconcile_checked_once(
    batch_size: int,
    max_hours: float,
    request_delay: float,
) -> Dict[str, int]:
    rows = claim_checked_articles_for_refresh(batch_size=batch_size, max_hours=max_hours)
    if not rows:
        return {
            "picked": 0,
            "checked_refreshed": 0,
            "failed": 0,
            "skipped_article_write_claim": 0,
        }

    aw_owner = _article_write_claim_owner()
    aw_enabled = bool(current_app.config.get("ARTICLE_WRITE_CLAIM_ENABLED", True))
    aw_lease = int(current_app.config.get("ARTICLE_WRITE_CLAIM_LEASE_SECONDS", 900))
    write_claim_acquired: Set[int] = set()
    skipped_article_write_claim = 0
    if aw_enabled:
        write_claim_acquired = _bulk_try_acquire_article_writes(
            [int(r.id) for r in rows],
            aw_owner,
            aw_lease,
        )
        skipped_article_write_claim = len(rows) - len(write_claim_acquired)
        picked_before_filter = len(rows)
        # 未抢到 article 写租约的行：撤掉 checked-refresh 哨兵，避免永久卡在 CHECKED_REFRESH_CLAIM_ERROR
        for row in rows:
            if row.id not in write_claim_acquired:
                row.metrics_error = ""
        if skipped_article_write_claim:
            _commit_with_retry()
        rows = [r for r in rows if r.id in write_claim_acquired]
        if not rows:
            return {
                "picked": picked_before_filter,
                "checked_refreshed": 0,
                "failed": 0,
                "skipped_article_write_claim": skipped_article_write_claim,
            }

    now = cn_now_naive()
    checked_refreshed = 0
    failed = 0
    failed_reasons: Dict[str, int] = {}
    picked = len(rows)
    try:
        for row in rows:
            gid = str(row.article_id or "").strip()
            if not gid:
                failed += 1
                row.metrics_status = "failed"
                row.metrics_error = "missing article_id"
                row.metrics_checked_at = now
                failed_reasons["missing_article_id"] = int(failed_reasons.get("missing_article_id", 0)) + 1
                # Per-row commit: release lock immediately
                try:
                    _commit_with_retry()
                except Exception as commit_exc:
                    db.session.rollback()
                    current_app.logger.warning("checked-refresh per-row commit failed gid=%s err=%s", gid, commit_exc)
                continue
            try:
                info = fetch_info_api(gid)
                if not info:
                    failed += 1
                    # Backoff: keep status 可重试，但避免 metrics_checked_at 仍为 NULL 导致下一轮永远最优先
                    row.metrics_status = "checked"
                    row.metrics_error = "info api empty"
                    row.metrics_checked_at = now
                    failed_reasons["info_api_empty"] = int(failed_reasons.get("info_api_empty", 0)) + 1
                else:
                    row.view_count = int(info.get("impression_count") or row.view_count or 0)
                    row.like_count = int(info.get("digg_count") or row.like_count or 0)
                    row.comment_count = int(
                        max(
                            int(info.get("comment_count") or 0),
                            int(row.comment_count or 0),
                        )
                    )
                    # checked 刷新模式：保持 checked，只更新校准时间
                    row.metrics_status = "checked"
                    row.metrics_checked_at = now
                    row.metrics_error = ""
                    checked_refreshed += 1
            except Exception as exc:
                failed += 1
                # Backoff: 写入失败原因，避免该行在排序里持续被命中
                row.metrics_status = "checked"
                reason = exc.__class__.__name__ or "Exception"
                failed_reasons[reason] = int(failed_reasons.get(reason, 0)) + 1
                err_msg = " ".join(str(exc).split())[:300]
                row.metrics_error = f"checked-refresh exception: {reason}: {err_msg}"[:500]
                row.metrics_checked_at = now
            finally:
                # Per-row commit: each row's UPDATE is committed immediately,
                # so locks are held for ms instead of the entire batch duration.
                try:
                    _commit_with_retry()
                except Exception as commit_exc:
                    db.session.rollback()
                    current_app.logger.warning("checked-refresh per-row commit failed gid=%s err=%s", gid, commit_exc)
                time.sleep(max(0.0, float(request_delay)))

        return {
            "picked": picked,
            "checked_refreshed": checked_refreshed,
            "failed": failed,
            "failed_reasons": failed_reasons,
            "skipped_article_write_claim": skipped_article_write_claim,
        }
    except Exception:
        db.session.rollback()
        raise
    finally:
        if aw_enabled and write_claim_acquired:
            _release_article_write_claims(write_claim_acquired, aw_owner)


def reconcile_once(
    batch_size: int,
    max_hours: float,
    request_delay: float,
    headless: bool | None = None,
) -> Dict[str, int]:
    rows = claim_pending_articles_for_refresh(batch_size=batch_size, max_hours=max_hours)
    if not rows:
        return {
            "picked": 0,
            "checked": 0,
            "failed": 0,
            "authors_updated": 0,
            "skipped_article_write_claim": 0,
        }

    now = cn_now_naive()
    checked = 0
    failed = 0
    pending_author_followers_unavailable = 0
    authors_updated = 0
    authors_mapped = 0
    skipped_article_write_claim = 0

    aw_owner = _article_write_claim_owner()
    aw_enabled = bool(current_app.config.get("ARTICLE_WRITE_CLAIM_ENABLED", True))
    aw_lease = int(current_app.config.get("ARTICLE_WRITE_CLAIM_LEASE_SECONDS", 900))
    write_claim_acquired: Set[int] = set()
    if aw_enabled:
        write_claim_acquired = _bulk_try_acquire_article_writes(
            [int(r.id) for r in rows],
            aw_owner,
            aw_lease,
        )
        skipped_article_write_claim = len(rows) - len(write_claim_acquired)

    if headless is None:
        headless = bool(current_app.config.get("CRAWL_HEADLESS", True))
    crawler: ToutiaoCrawler | None = None
    try:
        # 0) Ensure Article.author_id/author_url mapping
        #    Some rows may exist without author_id, so metrics reconciliation would otherwise stay in pending forever.
        need_map_rows = [r for r in rows if not r.author_id]
        if need_map_rows:
            # 0.1) Force parse from article page when author_id is missing.
            #     User explicitly requested: do NOT use "news list" author_url; only trust article detail page.
            for row in need_map_rows:
                if aw_enabled and row.id not in write_claim_acquired:
                    continue
                if (row.author_url or "").strip():
                    row.author_url = ""

            need_extract_rows = [
                r for r in need_map_rows if (r.url or "").strip()
            ]
            if aw_enabled:
                need_extract_rows = [r for r in need_extract_rows if r.id in write_claim_acquired]
            if need_extract_rows:
                if crawler is None:
                    crawler = ToutiaoCrawler(headless=headless)
                for row in need_extract_rows:
                    info = crawler._extract_author_info_from_article_page(row.url)  # noqa: SLF001
                    extracted_author_url = (info.get("author_url") or "").strip()
                    if extracted_author_url:
                        row.author_url = extracted_author_url
                        extracted_author_name = (info.get("author_name") or "").strip()
                        if extracted_author_name:
                            # prefer detail-page name; only fill when Article has none
                            if not (row.author or "").strip():
                                row.author = extracted_author_name
                    time.sleep(max(0.0, float(request_delay)))

            # 0.2) Create/find AuthorSource, then assign author_id back to Article.
            author_urls = sorted({(r.author_url or "").strip() for r in need_map_rows if (r.author_url or "").strip()})
            if author_urls:
                with db.session.no_autoflush:
                    existing = AuthorSource.query.filter(AuthorSource.author_url.in_(author_urls)).all()
                existing_map = {a.author_url: a for a in existing}
            else:
                existing_map = {}

            for row in need_map_rows:
                if aw_enabled and row.id not in write_claim_acquired:
                    continue
                if row.author_id:
                    continue
                author_url = (row.author_url or "").strip()
                if not author_url:
                    continue
                author = existing_map.get(author_url)
                if author is None:
                    author = AuthorSource(
                        author_url=author_url,
                        author_name=(row.author or "").strip(),
                        followers=0,
                    )
                    db.session.add(author)
                    db.session.flush()
                    existing_map[author_url] = author

                # Best-effort: refresh name if Article has it.
                if (row.author or "").strip() and not (author.author_name or "").strip():
                    author.author_name = (row.author or "").strip()[:128]
                author.last_seen_at = now
                row.author_id = author.id
                authors_mapped += 1

        # Step 0) Snapshot minimal per-article fields so step boundaries don't
        # trigger SQLAlchemy attribute reloads while we are doing HTTP calls.
        article_meta_by_id: Dict[int, Dict] = {}
        for r in rows:
            article_meta_by_id[int(r.id)] = {
                "gid": str(r.article_id or "").strip(),
                "author_id": (int(r.author_id) if r.author_id else None),
                "prev_view": int(r.view_count or 0),
                "prev_like": int(r.like_count or 0),
                "prev_comment": int(r.comment_count or 0),
            }

        # Step 0.2) Commit immediately after author mapping to release locks early.
        if need_map_rows:
            _commit_with_retry()

        # Step 1) Update author followers in batch, only for successfully claimed authors.
        author_ids = sorted(
            {m["author_id"] for m in article_meta_by_id.values() if m.get("author_id") is not None}
        )
        claimed_author_ids = claim_author_ids(author_ids)
        claimed_author_ids_set = set(claimed_author_ids)

        authors_map: Dict[int, AuthorSource] = {}
        author_followers_map: Dict[int, int] = {}
        if claimed_author_ids:
            authors = AuthorSource.query.filter(AuthorSource.id.in_(claimed_author_ids)).all()
            authors_map = {a.id: a for a in authors}

            for author_id in claimed_author_ids:
                author = authors_map.get(author_id)
                if not author or not author.author_url:
                    continue

                # 延迟启动浏览器：只有确实要抓作者粉丝时才创建 Selenium driver
                if crawler is None:
                    crawler = ToutiaoCrawler(headless=headless)

                fans = int(crawler._get_author_fans_count(author.author_url) or 0)  # noqa: SLF001
                if fans > 0:
                    author.followers = fans
                    author.last_seen_at = now
                    author.last_error = ""
                    authors_updated += 1
                else:
                    # 抓不到粉丝时，不要让文章立刻进入“失败终态”，
                    # 只记录失败原因以便后续重试/排查。
                    author.fail_count = int(author.fail_count or 0) + 1
                    author.last_error = "get fans returned 0"
                author.last_crawled_at = now
                time.sleep(max(0.0, float(request_delay)))

            # Step 1. end: commit before HTTP calls so we don't hold locks longer than needed.
            _commit_with_retry()
            author_followers_map = {aid: int(a.followers or 0) for aid, a in authors_map.items()}

        # 2) Refresh per-article metrics and status (per-row commit)
        for row in rows:
            if aw_enabled and row.id not in write_claim_acquired:
                continue
            meta = article_meta_by_id.get(int(row.id)) or {}
            gid = str(meta.get("gid") or "").strip()
            author_id = meta.get("author_id")
            prev_view = int(meta.get("prev_view") or 0)
            prev_like = int(meta.get("prev_like") or 0)
            prev_comment = int(meta.get("prev_comment") or 0)

            if not gid:
                row.metrics_status = "failed"
                row.metrics_error = "missing article_id"
                failed += 1
                try:
                    _commit_with_retry()
                except Exception as commit_exc:
                    db.session.rollback()
                    current_app.logger.warning("reconcile per-row commit failed gid=%s err=%s", gid, commit_exc)
                continue

            try:
                info = fetch_info_api(gid)
                if not info:
                    row.metrics_status = "failed"
                    row.metrics_error = "info api empty"
                    failed += 1
                else:
                    row.view_count = int(info.get("impression_count") or prev_view or 0)
                    row.like_count = int(info.get("digg_count") or prev_like or 0)
                    row.comment_count = int(
                        max(
                            int(info.get("comment_count") or 0),
                            int(prev_comment or 0),
                        )
                    )

                    author_ok = (
                        author_id is not None
                        and int(author_id) in claimed_author_ids_set
                        and int(author_followers_map.get(int(author_id)) or 0) > 0
                    )
                    if author_ok:
                        row.metrics_status = "checked"
                        row.metrics_checked_at = now
                        row.metrics_error = ""
                        checked += 1
                    else:
                        # 作者粉丝获取失败（或本轮没拿到 claim）：保持可重试状态 pending。
                        row.metrics_status = "pending"
                        row.metrics_checked_at = None
                        if author_id is not None and int(author_id) not in claimed_author_ids_set:
                            row.metrics_error = "author followers not claimed"
                        else:
                            row.metrics_error = "author followers unavailable"
                        pending_author_followers_unavailable += 1
            except Exception as exc:
                row.metrics_status = "failed"
                row.metrics_error = str(exc)[:500]
                failed += 1
            finally:
                # Per-row commit: each row's UPDATE is committed immediately,
                # so locks are held for ms instead of the entire batch duration.
                try:
                    _commit_with_retry()
                except Exception as commit_exc:
                    db.session.rollback()
                    current_app.logger.warning("reconcile per-row commit failed gid=%s err=%s", gid, commit_exc)
                time.sleep(max(0.0, float(request_delay)))

        return {
            "picked": len(rows),
            "checked": checked,
            "failed": failed,
            "authors_updated": authors_updated,
            "pending_author_followers_unavailable": pending_author_followers_unavailable,
            "authors_mapped": authors_mapped,
            "skipped_article_write_claim": skipped_article_write_claim,
        }
    except Exception:
        db.session.rollback()
        raise
    finally:
        if aw_enabled and write_claim_acquired:
            _release_article_write_claims(write_claim_acquired, aw_owner)
        if crawler is not None:
            crawler.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile article metrics and author followers")
    parser.add_argument(
        "--mode",
        choices=["pending", "checked-refresh"],
        default="pending",
        help="pending: reconcile pending rows; checked-refresh: continuously refresh checked rows",
    )
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--max-hours", type=float, default=24)
    parser.add_argument("--request-delay", type=float, default=0.25)
    parser.add_argument("--loop", action="store_true", default=False)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument(
        "--empty-threshold",
        type=int,
        default=2,
        help="连续轮空次数达到阈值后退避",
    )
    parser.add_argument(
        "--empty-backoff-seconds",
        type=int,
        default=600,
        help="连续轮空退避时长（秒）",
    )
    args = parser.parse_args()

    app = create_app(enable_scheduler=False)
    with app.app_context():
        headless = bool(app.config.get("CRAWL_HEADLESS", True))
        app.logger.info(
            "reconcile worker started mode=%s loop=%s batch_size=%s max_hours=%s headless=%s",
            str(args.mode),
            bool(args.loop),
            int(args.batch_size),
            float(args.max_hours),
            bool(headless),
        )
        empty_rounds = 0
        while True:
            if str(args.mode) == "checked-refresh":
                stats = reconcile_checked_once(
                    batch_size=int(args.batch_size),
                    max_hours=float(args.max_hours),
                    request_delay=float(args.request_delay),
                )
                app.logger.info(
                    "reconcile checked-refresh done picked=%s checked_refreshed=%s failed=%s "
                    "skipped_article_write_claim=%s failed_reasons=%s",
                    stats["picked"],
                    stats["checked_refreshed"],
                    stats["failed"],
                    stats.get("skipped_article_write_claim", 0),
                    stats.get("failed_reasons", {}),
                )
                # 连续轮空：本轮没有真正刷新任何条
                if int(stats.get("checked_refreshed", 0)) <= 0:
                    empty_rounds += 1
                else:
                    empty_rounds = 0
            else:
                stats = reconcile_once(
                    batch_size=int(args.batch_size),
                    max_hours=float(args.max_hours),
                    request_delay=float(args.request_delay),
                    headless=headless,
                )
                app.logger.info(
                    "reconcile pending done picked=%s checked=%s failed=%s authors_updated=%s "
                    "pending_author_followers_unavailable=%s authors_mapped=%s skipped_article_write_claim=%s",
                    stats["picked"],
                    stats["checked"],
                    stats["failed"],
                    stats["authors_updated"],
                    stats.get("pending_author_followers_unavailable", 0),
                    stats.get("authors_mapped", 0),
                    stats.get("skipped_article_write_claim", 0),
                )
                # 连续轮空：本轮没有挑到任何 pending
                if int(stats.get("picked", 0)) <= 0:
                    empty_rounds += 1
                else:
                    empty_rounds = 0
            if not args.loop:
                break
            if empty_rounds >= int(args.empty_threshold):
                app.logger.info(
                    "reconcile empty rounds threshold reached (empty_rounds=%s), backoff %s seconds",
                    empty_rounds,
                    int(args.empty_backoff_seconds),
                )
                time.sleep(int(args.empty_backoff_seconds))
                empty_rounds = 0
            time.sleep(max(5, int(args.interval_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
