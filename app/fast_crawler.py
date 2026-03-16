"""High-speed Toutiao crawler using HTTP APIs instead of Selenium.

Data flow:
  1. PC Feed API  -> article list (multi-channel, paginated)
  2. Mobile Info API -> full article content + metadata
  3. Filter + upsert into the same ``articles`` table
"""

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import httpx
from bs4 import BeautifulSoup
from flask import Flask
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .extensions import db
from .models import Article
from .time_utils import SHANGHAI_TZ, cn_now_naive
from .utils import sha256_hex

logger = logging.getLogger(__name__)

FEED_API_URL = "https://www.toutiao.com/api/pc/feed/"
INFO_API_URL = "https://m.toutiao.com/i{gid}/info/"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


def _is_deadlock_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "deadlock found" in text or "lock wait timeout exceeded" in text


def _commit_with_retry(max_retries: int = 3, sleep_seconds: float = 0.3):
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            db.session.commit()
            return
        except SQLAlchemyError as exc:
            db.session.rollback()
            last_exc = exc
            if not _is_deadlock_error(exc) or attempt >= max_retries:
                raise
            delay = sleep_seconds * attempt
            logger.warning("db commit deadlock retry=%s/%s sleep=%.2fs", attempt, max_retries, delay)
            time.sleep(delay)
    if last_exc:
        raise last_exc


def _normalize_article_url(group_id: str) -> str:
    return f"https://www.toutiao.com/article/{group_id}/"


def _extract_first_image(content_html: str) -> str:
    """Extract the first accessible image URL from article content HTML."""
    if not content_html:
        return ""
    match = re.search(
        r'<img[^>]+(?:src|data-src|original-src)\s*=\s*["\']([^"\']+)["\']',
        content_html,
        re.IGNORECASE,
    )
    if not match:
        return ""
    url = match.group(1).strip()
    if url.startswith("//"):
        url = f"https:{url}"
    return url


class FastCrawler:
    """Async HTTP-based Toutiao crawler."""

    def __init__(self, app: Flask):
        self.app = app
        cfg = app.config
        self.channels: List[str] = [
            c.strip() for c in str(cfg.get("FAST_CRAWL_CHANNELS", "__all__")).split(",") if c.strip()
        ]
        self.max_pages: int = int(cfg.get("FAST_CRAWL_MAX_PAGES_PER_CHANNEL", 50))
        self.concurrency: int = int(cfg.get("FAST_CRAWL_CONCURRENCY", 10))
        self.max_hours: float = float(cfg.get("FAST_CRAWL_MAX_HOURS", 24))
        self.request_delay: float = float(cfg.get("FAST_CRAWL_REQUEST_DELAY", 0.3))
        self.interval: int = int(cfg.get("FAST_CRAWL_INTERVAL_SECONDS", 300))
        self.loop_jitter_seconds: int = max(0, int(cfg.get("FAST_CRAWL_LOOP_JITTER_SECONDS", 15)))
        self.startup_jitter_seconds: int = max(
            0, int(cfg.get("FAST_CRAWL_STARTUP_JITTER_SECONDS", 20))
        )
        self.min_content_len: int = int(cfg.get("FAST_CRAWL_MIN_CONTENT_LENGTH", 80))
        self.max_fans: int = int(cfg.get("FAST_CRAWL_MAX_FANS", 0))
        self.block_keywords: List[str] = [
            kw.strip()
            for kw in str(cfg.get("CRAWL_BLOCK_AUTHOR_KEYWORDS", "")).split(",")
            if kw.strip()
        ]

    def _is_blocked_author(self, author: str) -> bool:
        if not author or not self.block_keywords:
            return False
        return any(kw in author for kw in self.block_keywords)

    # ------------------------------------------------------------------
    # Feed API
    # ------------------------------------------------------------------

    async def _fetch_feed_page(
        self, client: httpx.AsyncClient, channel: str, max_behot_time: int = 0
    ) -> Dict:
        params = {
            "category": channel,
            "utm_source": "toutiao",
            "max_behot_time": str(max_behot_time),
        }
        resp = await client.get(FEED_API_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    async def fetch_channel_all_pages(
        self, client: httpx.AsyncClient, channel: str
    ) -> List[Dict]:
        """Paginate through a single channel until max_hours exceeded or no more data."""
        all_items: List[Dict] = []
        seen_gids: Set[str] = set()
        max_behot_time = 0
        now_ts = time.time()
        cutoff_ts = now_ts - self.max_hours * 3600

        for page in range(1, self.max_pages + 1):
            try:
                data = await self._fetch_feed_page(client, channel, max_behot_time)
            except Exception as exc:
                logger.warning("feed page failed channel=%s page=%s err=%s", channel, page, exc)
                break

            items = data.get("data") or []
            has_more = data.get("has_more", False)
            if not items:
                break

            min_behot = None
            page_new = 0
            for item in items:
                gid = str(item.get("group_id", ""))
                if not gid or gid in seen_gids:
                    continue
                if item.get("is_feed_ad"):
                    continue
                seen_gids.add(gid)
                all_items.append(item)
                page_new += 1

                bt = item.get("behot_time", 0)
                if bt and (min_behot is None or bt < min_behot):
                    min_behot = bt

            logger.debug(
                "feed channel=%s page=%s new=%s total=%s min_behot=%s",
                channel, page, page_new, len(all_items), min_behot,
            )

            if min_behot is not None and min_behot < cutoff_ts:
                break
            if not has_more or min_behot is None:
                break

            max_behot_time = min_behot
            await asyncio.sleep(self.request_delay + random.uniform(0, 0.2))

        return all_items

    async def fetch_all_channels(self, client: httpx.AsyncClient) -> List[Dict]:
        """Fetch all channels concurrently and merge/deduplicate."""
        tasks = [self.fetch_channel_all_pages(client, ch) for ch in self.channels]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: Dict[str, Dict] = {}
        for result in results:
            if isinstance(result, Exception):
                logger.warning("channel fetch error: %s", result)
                continue
            for item in result:
                gid = str(item.get("group_id", ""))
                if gid and gid not in merged:
                    merged[gid] = item

        logger.info("feed fetch done channels=%s unique_items=%s", len(self.channels), len(merged))
        return list(merged.values())

    # ------------------------------------------------------------------
    # Mobile Info API
    # ------------------------------------------------------------------

    async def fetch_article_info(
        self, client: httpx.AsyncClient, group_id: str, semaphore: asyncio.Semaphore
    ) -> Optional[Dict]:
        async with semaphore:
            await asyncio.sleep(self.request_delay + random.uniform(0, 0.15))
            url = INFO_API_URL.format(gid=group_id)
            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    if resp.status_code == 429:
                        wait = (attempt + 1) * 2 + random.uniform(0, 1)
                        logger.warning("rate limited gid=%s, retry after %.1fs", group_id, wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    body = resp.json()
                    if not body.get("success"):
                        return None
                    return body.get("data")
                except Exception as exc:
                    if attempt < 2:
                        await asyncio.sleep((attempt + 1) * 1.0)
                        continue
                    logger.warning("info api failed gid=%s err=%s", group_id, exc)
                    return None
        return None

    async def fetch_infos_batch(
        self, client: httpx.AsyncClient, group_ids: List[str]
    ) -> Dict[str, Dict]:
        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = {
            gid: asyncio.create_task(self.fetch_article_info(client, gid, semaphore))
            for gid in group_ids
        }
        results: Dict[str, Dict] = {}
        for gid, task in tasks.items():
            info = await task
            if info:
                results[gid] = info
        return results

    # ------------------------------------------------------------------
    # Filter & Upsert
    # ------------------------------------------------------------------

    def _apply_article_fields(
        self,
        article: Article,
        *,
        article_url: str,
        url_hash: str,
        title: str,
        author: str,
        media_user: Dict,
        info: Dict,
        feed_item: Dict,
        publish_ts,
        published_at: Optional[datetime],
        now: datetime,
        content_html: str,
        followers: int,
        view_count: int,
        like_count: int,
    ) -> None:
        article.title = title[:255]
        article.url = article_url
        article.url_hash = url_hash
        article.cover = _extract_first_image(content_html)
        article.author = author
        author_uid = media_user.get("id") or info.get("creator_uid") or ""
        article.author_url = f"https://www.toutiao.com/c/user/token/{author_uid}/" if author_uid else ""
        article.publish_time_text = str(publish_ts or "")
        article.published_at = published_at or now
        article.published_hours_ago = max(0.0, (now - (published_at or now)).total_seconds() / 3600)
        if content_html and not content_html.strip().startswith("<article"):
            content_html = f"<article>{content_html}</article>"

        article.followers = followers
        article.view_count = view_count
        article.like_count = like_count
        article.comment_count = int(info.get("comment_count") or feed_item.get("comments_count") or 0)
        article.source_html = content_html
        article.last_seen_at = now

    def _filter_and_upsert(self, feed_items: List[Dict], info_map: Dict[str, Dict]) -> int:
        """Synchronous DB operations inside Flask app context."""
        now = cn_now_naive()
        cutoff = now - timedelta(hours=self.max_hours)
        affected = 0
        stats = {
            "candidates": len(feed_items),
            "info_fetched": len(info_map),
            "skip_no_info": 0,
            "skip_micro": 0,
            "skip_video": 0,
            "skip_author": 0,
            "skip_fans": 0,
            "skip_time": 0,
            "skip_content": 0,
            "skip_title": 0,
            "skip_no_engagement": 0,
            "conflict_retry": 0,
            "created": 0,
            "updated": 0,
            "errors": 0,
        }

        for feed_item in feed_items:
            gid = str(feed_item.get("group_id", ""))
            info = info_map.get(gid)
            if not info:
                stats["skip_no_info"] += 1
                continue

            try:
                # Skip micro-headlines (group_source=5, no article content)
                if info.get("group_source") == 5 or (not info.get("content") and info.get("thread")):
                    stats["skip_micro"] += 1
                    continue

                # Skip video content
                if info.get("play_url_list"):
                    stats["skip_video"] += 1
                    continue

                title = (info.get("title") or feed_item.get("title") or "").strip()
                if not title or len(title) < 4:
                    stats["skip_title"] += 1
                    continue
                if any(kw in title.lower() for kw in ["视频", "video", "直播", "live"]):
                    stats["skip_video"] += 1
                    continue

                author = (info.get("source") or feed_item.get("source") or "").strip()
                if self._is_blocked_author(author):
                    stats["skip_author"] += 1
                    continue

                # Fan count filter
                media_user = info.get("media_user") or {}
                followers = int(media_user.get("follower_count") or info.get("follower_count") or 0)
                if self.max_fans > 0 and followers >= self.max_fans:
                    stats["skip_fans"] += 1
                    continue

                # Publish time filter
                publish_ts = info.get("publish_time")
                published_at = None
                if publish_ts:
                    try:
                        ts = int(publish_ts)
                        if ts > 10**11:
                            ts = ts // 1000
                        published_at = datetime.fromtimestamp(ts, SHANGHAI_TZ).replace(tzinfo=None)
                    except Exception:
                        pass
                if published_at and published_at < cutoff:
                    stats["skip_time"] += 1
                    continue

                # Engagement filter: skip articles with 0 reads or 0 likes
                view_count = int(info.get("impression_count") or 0)
                like_count = int(info.get("digg_count") or 0)
                if view_count <= 0 or like_count <= 0:
                    stats["skip_no_engagement"] += 1
                    continue

                # Content filter
                content_html = (info.get("content") or "").strip()
                content_text = ""
                if content_html:
                    content_text = BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)
                if len(content_text) < self.min_content_len:
                    stats["skip_content"] += 1
                    continue

                # Build article URL and hash
                article_url = _normalize_article_url(gid)
                url_hash = sha256_hex(article_url)

                try:
                    with db.session.begin_nested():
                        article = Article.query.filter(
                            (Article.article_id == gid) | (Article.url_hash == url_hash)
                        ).first()
                        is_new = article is None
                        if is_new:
                            article = Article(article_id=gid, url_hash=url_hash, url=article_url)
                            db.session.add(article)

                        self._apply_article_fields(
                            article,
                            article_url=article_url,
                            url_hash=url_hash,
                            title=title,
                            author=author,
                            media_user=media_user,
                            info=info,
                            feed_item=feed_item,
                            publish_ts=publish_ts,
                            published_at=published_at,
                            now=now,
                            content_html=content_html,
                            followers=followers,
                            view_count=view_count,
                            like_count=like_count,
                        )
                        db.session.flush()

                    if is_new:
                        stats["created"] += 1
                    else:
                        stats["updated"] += 1
                    affected += 1
                except IntegrityError:
                    # Another crawler may insert the same row first.
                    stats["conflict_retry"] += 1
                    with db.session.begin_nested():
                        article = Article.query.filter(
                            (Article.article_id == gid) | (Article.url_hash == url_hash)
                        ).first()
                        if not article:
                            raise
                        self._apply_article_fields(
                            article,
                            article_url=article_url,
                            url_hash=url_hash,
                            title=title,
                            author=author,
                            media_user=media_user,
                            info=info,
                            feed_item=feed_item,
                            publish_ts=publish_ts,
                            published_at=published_at,
                            now=now,
                            content_html=content_html,
                            followers=followers,
                            view_count=view_count,
                            like_count=like_count,
                        )
                        db.session.flush()
                    stats["updated"] += 1
                    affected += 1

            except Exception as exc:
                stats["errors"] += 1
                db.session.rollback()
                logger.warning("upsert failed gid=%s err=%s", gid, exc)

        if affected > 0:
            try:
                _commit_with_retry()
            except Exception as exc:
                db.session.rollback()
                stats["errors"] += affected
                affected = 0
                logger.exception("batch commit failed: %s", exc)

        logger.info(
            "upsert summary candidates=%s info_ok=%s created=%s updated=%s "
            "skip_no_info=%s skip_micro=%s skip_video=%s skip_author=%s "
            "skip_fans=%s skip_time=%s skip_engagement=%s skip_content=%s skip_title=%s "
            "conflict_retry=%s errors=%s",
            stats["candidates"], stats["info_fetched"], stats["created"], stats["updated"],
            stats["skip_no_info"], stats["skip_micro"], stats["skip_video"], stats["skip_author"],
            stats["skip_fans"], stats["skip_time"], stats["skip_no_engagement"],
            stats["skip_content"], stats["skip_title"], stats["conflict_retry"], stats["errors"],
        )
        return affected

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    async def run_once(self) -> int:
        started = time.perf_counter()
        headers = {
            "User-Agent": _random_ua(),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.toutiao.com/",
        }

        async with httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
        ) as client:
            feed_items = await self.fetch_all_channels(client)
            if not feed_items:
                logger.info("fast crawl: no feed items")
                return 0

            group_ids = [str(item.get("group_id", "")) for item in feed_items if item.get("group_id")]
            logger.info("fast crawl: fetching info for %s articles", len(group_ids))

            # Filter out already-known articles that were seen recently to save API calls
            with self.app.app_context():
                recent_cutoff = cn_now_naive() - timedelta(minutes=30)
                existing_ids = set()
                if group_ids:
                    for batch_start in range(0, len(group_ids), 500):
                        batch = group_ids[batch_start:batch_start + 500]
                        rows = db.session.query(Article.article_id).filter(
                            Article.article_id.in_(batch),
                            Article.last_seen_at >= recent_cutoff,
                        ).all()
                        existing_ids.update(r[0] for r in rows)

            new_gids = [gid for gid in group_ids if gid not in existing_ids]
            logger.info(
                "fast crawl: %s new + %s recently seen = %s total",
                len(new_gids), len(existing_ids), len(group_ids),
            )

            info_map = await self.fetch_infos_batch(client, new_gids)

        with self.app.app_context():
            affected = self._filter_and_upsert(feed_items, info_map)

        elapsed = time.perf_counter() - started
        logger.info(
            "fast crawl done: upserted=%s elapsed=%.1fs feed_items=%s info_fetched=%s",
            affected, elapsed, len(feed_items), len(info_map),
        )
        return affected

    async def run_loop(self):
        logger.info(
            "fast crawler loop started interval=%ss channels=%s concurrency=%s max_pages=%s",
            self.interval, self.channels, self.concurrency, self.max_pages,
        )
        if self.startup_jitter_seconds > 0:
            startup_sleep = random.uniform(0, float(self.startup_jitter_seconds))
            logger.info("fast crawler startup jitter sleep %.1fs", startup_sleep)
            await asyncio.sleep(startup_sleep)
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                logger.exception("fast crawl loop error: %s", exc)
            loop_sleep = self.interval + random.uniform(0, float(self.loop_jitter_seconds))
            logger.info("fast crawl sleeping %.1fs", loop_sleep)
            await asyncio.sleep(loop_sleep)
