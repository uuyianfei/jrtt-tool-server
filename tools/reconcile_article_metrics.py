"""Reconcile article metrics and author followers for newly ingested rows.

This worker is designed for eventual-consistency:
1) fast crawler inserts/updates articles with metrics_status=pending
2) this job recalculates followers and article metrics
3) rows are marked as checked/failed
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import timedelta
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from flask import current_app

from app import create_app
from app.crawler import ToutiaoCrawler
from app.extensions import db
from app.models import Article, AuthorSource
from app.time_utils import cn_now_naive

INFO_API_URL = "https://m.toutiao.com/i{gid}/info/"
CHECKED_REFRESH_CLAIM_ERROR = "checked-refreshing"
PENDING_REFRESH_CLAIM_ERROR = "pending-refreshing"


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
        db.session.commit()
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
        db.session.commit()
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

        db.session.commit()
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
        db.session.commit()
        return rows


def reconcile_checked_once(
    batch_size: int,
    max_hours: float,
    request_delay: float,
) -> Dict[str, int]:
    rows = claim_checked_articles_for_refresh(batch_size=batch_size, max_hours=max_hours)
    if not rows:
        return {"picked": 0, "checked_refreshed": 0, "failed": 0}

    now = cn_now_naive()
    checked_refreshed = 0
    failed = 0
    failed_reasons: Dict[str, int] = {}
    try:
        for row in rows:
            gid = str(row.article_id or "").strip()
            if not gid:
                failed += 1
                row.metrics_status = "failed"
                row.metrics_error = "missing article_id"
                row.metrics_checked_at = now
                failed_reasons["missing_article_id"] = int(failed_reasons.get("missing_article_id", 0)) + 1
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
                    continue

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
                time.sleep(max(0.0, float(request_delay)))

        db.session.commit()
        return {
            "picked": len(rows),
            "checked_refreshed": checked_refreshed,
            "failed": failed,
            "failed_reasons": failed_reasons,
        }
    except Exception:
        db.session.rollback()
        raise


def reconcile_once(
    batch_size: int,
    max_hours: float,
    request_delay: float,
    headless: bool | None = None,
) -> Dict[str, int]:
    rows = claim_pending_articles_for_refresh(batch_size=batch_size, max_hours=max_hours)
    if not rows:
        return {"picked": 0, "checked": 0, "failed": 0, "authors_updated": 0}

    now = cn_now_naive()
    checked = 0
    failed = 0
    pending_author_followers_unavailable = 0
    authors_updated = 0
    authors_mapped = 0

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
                if (row.author_url or "").strip():
                    row.author_url = ""

            need_extract_rows = [
                r for r in need_map_rows if (r.url or "").strip()
            ]
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
                existing = AuthorSource.query.filter(AuthorSource.author_url.in_(author_urls)).all()
                existing_map = {a.author_url: a for a in existing}
            else:
                existing_map = {}

            for row in need_map_rows:
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

        # 1) Update author followers in batch (dedup by author_id)
        author_ids = sorted({int(r.author_id) for r in rows if r.author_id})
        for author_id in author_ids:
            author = AuthorSource.query.filter(AuthorSource.id == author_id).first()
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

        # 2) Refresh per-article metrics and status
        for row in rows:
            gid = str(row.article_id or "").strip()
            if not gid:
                row.metrics_status = "failed"
                row.metrics_error = "missing article_id"
                failed += 1
                continue

            try:
                info = fetch_info_api(gid)
                if not info:
                    row.metrics_status = "failed"
                    row.metrics_error = "info api empty"
                    failed += 1
                    continue

                row.view_count = int(info.get("impression_count") or row.view_count or 0)
                row.like_count = int(info.get("digg_count") or row.like_count or 0)
                row.comment_count = int(
                    max(
                        int(info.get("comment_count") or 0),
                        int(row.comment_count or 0),
                    )
                )

                author_ok = False
                if row.author_id:
                    author = AuthorSource.query.filter(AuthorSource.id == row.author_id).first()
                    author_ok = bool(author and int(author.followers or 0) > 0)
                if author_ok:
                    row.metrics_status = "checked"
                    row.metrics_checked_at = now
                    row.metrics_error = ""
                    checked += 1
                else:
                    # 作者粉丝获取失败时：保持可重试状态 pending，而不是把整批文章直接打成 failed。
                    row.metrics_status = "pending"
                    row.metrics_checked_at = None
                    row.metrics_error = "author followers unavailable"
                    pending_author_followers_unavailable += 1
            except Exception as exc:
                row.metrics_status = "failed"
                row.metrics_error = str(exc)[:500]
                failed += 1
            finally:
                time.sleep(max(0.0, float(request_delay)))

        db.session.commit()
        return {
            "picked": len(rows),
            "checked": checked,
            "failed": failed,
            "authors_updated": authors_updated,
            "pending_author_followers_unavailable": pending_author_followers_unavailable,
            "authors_mapped": authors_mapped,
        }
    except Exception:
        db.session.rollback()
        raise
    finally:
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
                    "reconcile checked-refresh done picked=%s checked_refreshed=%s failed=%s failed_reasons=%s",
                    stats["picked"],
                    stats["checked_refreshed"],
                    stats["failed"],
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
                    "reconcile pending done picked=%s checked=%s failed=%s authors_updated=%s pending_author_followers_unavailable=%s authors_mapped=%s",
                    stats["picked"],
                    stats["checked"],
                    stats["failed"],
                    stats["authors_updated"],
                    stats.get("pending_author_followers_unavailable", 0),
                    stats.get("authors_mapped", 0),
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
