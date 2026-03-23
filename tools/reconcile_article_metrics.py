"""Reconcile article metrics and author followers for newly ingested rows.

This worker is designed for eventual-consistency:
1) fast crawler inserts/updates articles with metrics_status=pending
2) this job recalculates followers and article metrics
3) rows are marked as checked/failed
"""

from __future__ import annotations

import argparse
import time
from datetime import timedelta
from typing import Dict, List

import requests

from app import create_app
from app.crawler import ToutiaoCrawler
from app.extensions import db
from app.models import Article, AuthorSource
from app.time_utils import cn_now_naive

INFO_API_URL = "https://m.toutiao.com/i{gid}/info/"


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
        .order_by(Article.id.asc())
        .limit(max(1, int(batch_size)))
        .all()
    )


def reconcile_once(batch_size: int, max_hours: float, request_delay: float) -> Dict[str, int]:
    rows = pick_pending_articles(batch_size=batch_size, max_hours=max_hours)
    if not rows:
        return {"picked": 0, "checked": 0, "failed": 0, "authors_updated": 0}

    now = cn_now_naive()
    checked = 0
    failed = 0
    authors_updated = 0

    crawler = ToutiaoCrawler(headless=True)
    try:
        # 1) Update author followers in batch (dedup by author_id)
        author_ids = sorted({int(r.author_id) for r in rows if r.author_id})
        for author_id in author_ids:
            author = AuthorSource.query.filter(AuthorSource.id == author_id).first()
            if not author or not author.author_url:
                continue
            fans = int(crawler._get_author_fans_count(author.author_url) or 0)  # noqa: SLF001
            if fans > 0:
                author.followers = fans
                author.last_seen_at = now
                author.last_error = ""
                authors_updated += 1
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
                    row.metrics_status = "failed"
                    row.metrics_error = "author followers unavailable"
                    failed += 1
            except Exception as exc:
                row.metrics_status = "failed"
                row.metrics_error = str(exc)[:500]
                failed += 1
            finally:
                time.sleep(max(0.0, float(request_delay)))

        db.session.commit()
        return {"picked": len(rows), "checked": checked, "failed": failed, "authors_updated": authors_updated}
    except Exception:
        db.session.rollback()
        raise
    finally:
        crawler.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile article metrics and author followers")
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--max-hours", type=float, default=24)
    parser.add_argument("--request-delay", type=float, default=0.25)
    parser.add_argument("--loop", action="store_true", default=False)
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()

    app = create_app(enable_scheduler=False)
    with app.app_context():
        app.logger.info(
            "reconcile worker started loop=%s batch_size=%s max_hours=%s",
            bool(args.loop),
            int(args.batch_size),
            float(args.max_hours),
        )
        while True:
            stats = reconcile_once(
                batch_size=int(args.batch_size),
                max_hours=float(args.max_hours),
                request_delay=float(args.request_delay),
            )
            app.logger.info(
                "reconcile done picked=%s checked=%s failed=%s authors_updated=%s",
                stats["picked"],
                stats["checked"],
                stats["failed"],
                stats["authors_updated"],
            )
            if not args.loop:
                break
            time.sleep(max(5, int(args.interval_seconds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
