import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from typing import Dict, List

from bs4 import BeautifulSoup
from flask import current_app
from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from .extensions import db
from .models import Article
from .time_utils import cn_now_naive
from .utils import parse_hours_ago, parse_number, sha256_hex


logger = logging.getLogger(__name__)


class ToutiaoCrawler:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = self._init_browser()

    def _init_browser(self):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "eager"
        chrome_binary = (current_app.config.get("CHROME_BINARY_PATH") or "").strip()
        if chrome_binary:
            options.binary_location = chrome_binary
        if self.headless:
            # Windows 某些版本在 --headless=new 下更容易崩溃，回退到经典无头模式
            options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=zh-CN")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        driver_path = self._resolve_driver_path()
        if driver_path:
            logger.info("use local chromedriver: %s", driver_path)
            service = Service(driver_path)
        else:
            logger.info("local chromedriver not found, fallback to webdriver-manager download")
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
                """
            },
        )
        return driver

    def _resolve_driver_path(self) -> str:
        configured = (current_app.config.get("CHROMEDRIVER_PATH") or "").strip()
        if configured and os.path.exists(configured):
            return configured

        # 优先复用 webdriver-manager 的本地缓存，避免每次联网探测版本
        home = Path.home()
        cache_root = home / ".wdm" / "drivers" / "chromedriver"
        if cache_root.exists():
            candidates = list(cache_root.rglob("chromedriver.exe"))
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return str(candidates[0])
        return ""

    def _recreate_driver(self):
        logger.warning("webdriver session invalid, recreating chrome driver")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        self.driver = self._init_browser()

    def _safe_get(self, url: str):
        try:
            self.driver.get(url)
            return
        except (InvalidSessionIdException, WebDriverException) as exc:
            logger.warning("webdriver get failed, retry once: %s", exc)
            self._recreate_driver()
            self.driver.get(url)

    def _safe_refresh(self):
        try:
            self.driver.refresh()
            return
        except (InvalidSessionIdException, WebDriverException) as exc:
            logger.warning("webdriver refresh failed, retry once: %s", exc)
            self._recreate_driver()
            # 刷新失败时由调用方重新 get 页面即可

    def _safe_execute_script(self, script: str):
        try:
            self.driver.execute_script(script)
            return
        except (InvalidSessionIdException, WebDriverException) as exc:
            logger.warning("webdriver execute_script failed, retry once: %s", exc)
            self._recreate_driver()

    def _safe_page_source(self) -> str:
        try:
            return self.driver.page_source
        except (InvalidSessionIdException, WebDriverException) as exc:
            logger.warning("webdriver page_source failed, retry once: %s", exc)
            self._recreate_driver()
            return self.driver.page_source

    def close(self):
        if self.driver:
            self.driver.quit()

    def _find_article_cards(self, soup: BeautifulSoup):
        cards = []
        for a in soup.find_all("a", class_="title", href=True):
            parent = a
            found = False
            for _ in range(5):
                if parent and "feed-card-article" in parent.get("class", []):
                    cards.append(parent)
                    found = True
                    break
                parent = parent.parent
                if parent is None:
                    break
            if not found:
                cards.append(a)
        return cards

    def _is_blocked_author(self, author: str) -> bool:
        if not author:
            return False
        raw = current_app.config.get("CRAWL_BLOCK_AUTHOR_KEYWORDS", "")
        keywords = [k.strip() for k in str(raw).split(",") if k.strip()]
        author_text = author.strip()
        return any(k in author_text for k in keywords)

    def _extract_fans_from_text(self, text: str) -> int:
        if not text:
            return 0
        cleaned = re.sub(r"\s+", "", text)
        # 兼容：179.0万粉丝
        m = re.search(r"(\d+(?:\.\d+)?)([万亿]?)粉丝", cleaned)
        if m:
            return parse_number((m.group(1) or "") + (m.group(2) or ""))
        # 兼容：粉丝179.0万
        m = re.search(r"粉丝(\d+(?:\.\d+)?)([万亿]?)", cleaned)
        if m:
            return parse_number((m.group(1) or "") + (m.group(2) or ""))
        return 0

    def _extract_count_by_keyword(self, text: str, keyword: str) -> int:
        if not text:
            return 0
        cleaned = re.sub(r"\s+", "", text)
        m = re.search(rf"{keyword}[:：]?(\d+(?:\.\d+)?)([万亿]?)", cleaned)
        if m:
            return parse_number((m.group(1) or "") + (m.group(2) or ""))
        m = re.search(rf"(\d+(?:\.\d+)?)([万亿]?).{{0,2}}{keyword}", cleaned)
        if m:
            return parse_number((m.group(1) or "") + (m.group(2) or ""))
        return 0

    def _extract_article_info(self, card):
        try:
            title_elem = card if card.name == "a" else card.find("a", class_="title")
            if not title_elem:
                return None

            article_url = title_elem.get("href", "")
            if not article_url.startswith("http"):
                article_url = f"https://www.toutiao.com{article_url}"

            article_id_match = re.search(r"/article/(\d+)/", article_url)
            if not article_id_match:
                return None

            title = title_elem.get_text(strip=True)
            # 推荐流里无标题的通常是微头条，直接过滤
            if not title:
                logger.info("skip card: empty title (likely micro-headline)")
                return None
            if any(kw in title.lower() for kw in ["视频", "video", "直播", "live"]):
                logger.info("skip card: video/live content title=%s", title[:40])
                return None

            author, author_url = "", ""
            author_elem = card.find("div", class_="feed-card-footer-cmp-author") or card.find(
                "div", class_="author-info"
            )
            if author_elem:
                author_link = author_elem.find("a", href=re.compile(r"/c/user/"))
                if author_link:
                    author = author_link.get_text(strip=True) or ""
                    author_url = author_link.get("href", "")
            if author_url and not author_url.startswith("http"):
                author_url = f"https://www.toutiao.com{author_url}"
            if self._is_blocked_author(author):
                logger.info("skip blocked author: %s", author)
                return None

            publish_time = ""
            time_elem = card.find("div", class_="feed-card-footer-time-cmp") or card.find("div", class_="time")
            if time_elem:
                publish_time = time_elem.get_text(strip=True)

            comment_count = 0
            comment_elem = card.find("div", class_="feed-card-footer-comment-cmp") or card.find(
                "div", class_="comment"
            )
            if comment_elem:
                comment_link = comment_elem.find("a") or comment_elem
                comment_text = (
                    comment_link.get("aria-label", "")
                    or comment_link.get_text(strip=True)
                    or comment_elem.get_text(strip=True)
                )
                if comment_text:
                    m = re.search(r"评论数?[:：]?\s*(\d+(?:\.\d+)?)([万亿]?)", comment_text)
                    if not m:
                        m = re.search(r"(\d+(?:\.\d+)?)([万亿]?)\s*评论", comment_text)
                    if m:
                        comment_count = parse_number((m.group(1) or "") + (m.group(2) or ""))

            cover = ""
            img = card.find("img")
            if img:
                cover = img.get("src", "") or img.get("data-src", "")
                if cover.startswith("//"):
                    cover = f"https:{cover}"

            return {
                "article_id": article_id_match.group(1),
                "url": article_url,
                "title": title[:200],
                "author": author,
                "author_url": author_url,
                "publish_time": publish_time,
                "comment_count": comment_count,
                "cover": cover,
            }
        except Exception as exc:
            logger.warning("extract article info failed: %s", exc)
            return None

    def _get_author_fans_count(self, author_url: str) -> int:
        if not author_url:
            return 0
        try:
            self._safe_get(author_url)
            time.sleep(1.2)
            soup = BeautifulSoup(self._safe_page_source(), "html.parser")
            stat_items = soup.select(".relation-stat .stat-item, button.stat-item, .stat-item")
            for item in stat_items:
                aria = item.get("aria-label", "")
                item_text = item.get_text(strip=True)
                if "粉丝" in aria or "粉丝" in item_text:
                    num_span = item.find("span", class_="num")
                    if num_span:
                        fans = parse_number(num_span.get_text(strip=True))
                        if fans > 0:
                            return fans
                    fans = self._extract_fans_from_text(item_text)
                    if fans > 0:
                        return fans

            # 等待一小段时间再读一次，兼容动态渲染稍慢
            time.sleep(0.8)
            soup = BeautifulSoup(self._safe_page_source(), "html.parser")
            full_text = soup.get_text(" ", strip=True)
            fans = self._extract_fans_from_text(full_text)
            if fans > 0:
                return fans

            match = re.search(r"粉丝\s*(\d+(?:\.\d+)?)([万亿]?)", full_text)
            if match:
                return parse_number(match.group(1) + match.group(2))
        except Exception as exc:
            logger.warning("get fans failed: %s", exc)
        return 0

    def _get_article_details(self, article_url: str) -> Dict:
        detail = {
            "like_count": 0,
            "comment_count": 0,
            "article_html": "",
            "title": "",
        }
        try:
            self._safe_get(article_url)
            time.sleep(1.5)
            html = self._safe_page_source()
            soup = BeautifulSoup(html, "html.parser")

            # 优先按新版详情页交互区结构解析（detail-like / detail-interaction-comment）
            like_btn = soup.select_one(".detail-side-interaction .detail-like")
            if like_btn:
                # 点赞优先取按钮内数字，避免 aria-label 与 span 数字拼接导致 1 -> 11
                span = like_btn.find("span")
                if span:
                    span_num = parse_number(span.get_text(strip=True))
                    if span_num > 0:
                        detail["like_count"] = span_num
                if detail["like_count"] <= 0:
                    aria = like_btn.get("aria-label", "")
                    detail["like_count"] = self._extract_count_by_keyword(aria, "点赞")

            comment_btn = soup.select_one(".detail-side-interaction .detail-interaction-comment")
            if comment_btn:
                comment_text = " ".join(
                    [
                        comment_btn.get("aria-label", ""),
                        comment_btn.get_text(" ", strip=True),
                    ]
                )
                detail["comment_count"] = self._extract_count_by_keyword(comment_text, "评论")

            like_patterns = [r"点赞\s*(\d+)", r'"digg_count"\s*:\s*(\d+)']
            if detail["like_count"] <= 0:
                for pattern in like_patterns:
                    m = re.search(pattern, html, re.IGNORECASE)
                    if m:
                        detail["like_count"] = int(m.group(1))
                        break

            comment_patterns = [
                r"评论\s*(\d+(?:\.\d+)?)([万亿]?)",
                r"(\d+(?:\.\d+)?)([万亿]?)\s*评论",
                r'"comment_count"\s*:\s*(\d+)',
                r'commentCount["\']?\s*:\s*["\']?(\d+)',
            ]
            if detail["comment_count"] <= 0:
                for pattern in comment_patterns:
                    m = re.search(pattern, html, re.IGNORECASE)
                    if m:
                        if len(m.groups()) >= 2 and (m.group(2) is not None):
                            detail["comment_count"] = parse_number((m.group(1) or "") + (m.group(2) or ""))
                        else:
                            detail["comment_count"] = int(m.group(1))
                        break

            container = (
                soup.select_one("article.syl-article-base")
                or soup.select_one(".syl-article-base")
                or soup.select_one("article")
            )
            if container:
                detail["article_html"] = str(container)

            title_elem = soup.select_one("h1") or soup.select_one("title")
            if title_elem:
                detail["title"] = title_elem.get_text(strip=True)[:200]
        except Exception as exc:
            logger.warning("get detail failed: %s", exc)
        return detail

    def _get_article_read_count_from_author(
        self,
        author_url: str,
        article_id: str,
        article_title: str,
        article_url: str,
    ) -> int:
        if not author_url:
            return 0
        try:
            self._safe_get(author_url)
            time.sleep(1.5)

            article_path = ""
            if article_url and article_url.startswith("https://www.toutiao.com"):
                article_path = article_url.replace("https://www.toutiao.com", "")

            for _ in range(6):
                html = self._safe_page_source()
                soup = BeautifulSoup(html, "html.parser")

                links = []
                if article_id:
                    links = soup.find_all("a", href=lambda x: x and f"/article/{article_id}/" in str(x))
                if not links and article_path:
                    links = soup.find_all("a", href=lambda x: x and article_path in str(x))
                if not links and article_title:
                    links = soup.find_all(
                        "a",
                        class_="title",
                        string=lambda s: s and article_title[:18] in s,
                    )

                for link in links:
                    parent = link
                    for _ in range(12):
                        if parent is None:
                            break
                        read_div = parent.find("div", class_="profile-feed-card-tools-text")
                        if read_div:
                            read_text = read_div.get_text(strip=True)
                            m = re.search(r"(\d+(?:\.\d+)?)([万亿]?)\s*阅读", read_text)
                            if m:
                                return parse_number((m.group(1) or "") + (m.group(2) or ""))
                        parent = parent.parent

                # 没找到就继续下拉加载更多
                self._safe_execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1.0)
        except Exception as exc:
            logger.warning("get read count from author failed: %s", exc)
        return 0

    def crawl_recommend_page(self, target_count: int) -> List[Dict]:
        url = current_app.config["TOUTIAO_URL"]
        scroll_rounds = int(current_app.config.get("CRAWL_LIST_SCROLL_ROUNDS", 6))
        for attempt in range(2):
            try:
                self._safe_get(url)
                time.sleep(2)
                self._safe_refresh()
                # refresh 失败后，至少保证重新打开一次页面
                self._safe_get(url)
                time.sleep(2)
                found_map = {}
                last_card_count = 0
                for round_idx in range(1, scroll_rounds + 1):
                    self._safe_execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(1.2)
                    soup = BeautifulSoup(self._safe_page_source(), "html.parser")
                    cards = self._find_article_cards(soup)
                    current_card_count = len(cards)
                    new_card_delta = max(0, current_card_count - last_card_count)
                    last_card_count = current_card_count

                    for card in cards:
                        info = self._extract_article_info(card)
                        if not info:
                            continue
                        found_map[info["article_id"]] = info
                        if len(found_map) >= target_count:
                            break

                    logger.info(
                        "crawl list round=%s/%s cards=%s new_cards=%s selected=%s",
                        round_idx,
                        scroll_rounds,
                        current_card_count,
                        new_card_delta,
                        len(found_map),
                    )
                    if len(found_map) >= target_count:
                        break

                found = list(found_map.values())[:target_count]
                logger.info("crawl list parsed selected=%s target=%s", len(found), target_count)
                return found
            except (InvalidSessionIdException, WebDriverException) as exc:
                logger.warning("crawl recommend failed attempt=%s err=%s", attempt + 1, exc)
                self._recreate_driver()
                time.sleep(1)
        logger.warning("crawl recommend failed after retries")
        return []


def upsert_articles(items: List[Dict]):
    now = cn_now_naive()
    app = current_app._get_current_object()
    max_hours = float(current_app.config["CRAWL_MAX_HOURS"])
    max_fans = int(current_app.config["CRAWL_MAX_FANS"])
    detail_workers = max(1, int(current_app.config.get("CRAWL_DETAIL_WORKERS", 3)))
    crawl_headless = bool(current_app.config["CRAWL_HEADLESS"])
    affected = 0
    stats = {
        "total_candidates": len(items),
        "skip_time": 0,
        "skip_fans": 0,
        "created": 0,
        "updated": 0,
        "errors": 0,
    }
    logger.info("detail workers=%s", detail_workers)

    def enrich_one(base: Dict):
        article_id = base.get("article_id", "")
        title = (base.get("title") or "")[:40]
        with app.app_context():
            crawler = ToutiaoCrawler(headless=crawl_headless)
            try:
                logger.info("detail processing article_id=%s title=%s", article_id, title)
                hours_ago = parse_hours_ago(base.get("publish_time", ""))
                if hours_ago is None or hours_ago > max_hours:
                    return {
                        "status": "skip_time",
                        "article_id": article_id,
                        "hours_ago": hours_ago,
                    }

                fans = crawler._get_author_fans_count(base.get("author_url", ""))
                if fans >= max_fans:
                    return {
                        "status": "skip_fans",
                        "article_id": article_id,
                        "fans": fans,
                    }

                details = crawler._get_article_details(base["url"])
                read_count = crawler._get_article_read_count_from_author(
                    author_url=base.get("author_url", ""),
                    article_id=base.get("article_id", ""),
                    article_title=base.get("title", ""),
                    article_url=base.get("url", ""),
                )
                return {
                    "status": "ok",
                    "article_id": article_id,
                    "base": base,
                    "hours_ago": float(hours_ago),
                    "fans": int(fans),
                    "details": details,
                    "read_count": int(read_count or 0),
                }
            finally:
                crawler.close()

    enrich_results = []
    with ThreadPoolExecutor(max_workers=detail_workers) as executor:
        future_map = {executor.submit(enrich_one, base): base for base in items}
        for future in as_completed(future_map):
            base = future_map[future]
            article_id = base.get("article_id", "")
            try:
                result = future.result()
                status = result.get("status")
                if status == "skip_time":
                    stats["skip_time"] += 1
                    logger.info(
                        "skip article_id=%s reason=time hours_ago=%s max_hours=%s",
                        article_id,
                        result.get("hours_ago"),
                        max_hours,
                    )
                    continue
                if status == "skip_fans":
                    stats["skip_fans"] += 1
                    logger.info(
                        "skip article_id=%s reason=fans fans=%s max_fans=%s",
                        article_id,
                        result.get("fans"),
                        max_fans,
                    )
                    continue
                enrich_results.append(result)
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("detail enrich failed article_id=%s err=%s", article_id, exc)

    for idx, item in enumerate(enrich_results, start=1):
        base = item["base"]
        article_id = item["article_id"]
        try:
            logger.info(
                "upsert processing [%s/%s] article_id=%s",
                idx,
                len(enrich_results),
                article_id,
            )
            hours_ago = float(item["hours_ago"])
            fans = int(item["fans"])
            details = item["details"]
            read_count = int(item["read_count"])
            published_at = now - timedelta(hours=hours_ago)
            url_hash = sha256_hex(base["url"])
            article = Article.query.filter(
                (Article.article_id == base["article_id"]) | (Article.url_hash == url_hash)
            ).first()
            is_new = article is None
            if is_new:
                article = Article(article_id=base["article_id"], url_hash=url_hash, url=base["url"])
                db.session.add(article)
                stats["created"] += 1
            else:
                stats["updated"] += 1

            article.title = details.get("title") or base.get("title") or "无标题"
            article.url_hash = url_hash
            article.cover = base.get("cover", "")
            article.author = base.get("author", "")
            article.author_url = base.get("author_url", "")
            article.publish_time_text = base.get("publish_time", "")
            article.published_at = published_at
            article.published_hours_ago = float(hours_ago)
            article.followers = int(fans)
            # 阅读量只采用作者主页作品卡片值（详情页无可靠阅读量）
            article.view_count = int(read_count or 0)
            article.like_count = int(details.get("like_count") or 0)
            article.comment_count = int(
                max(
                    int(details.get("comment_count") or 0),
                    int(base.get("comment_count") or 0),
                )
            )
            article.source_html = details.get("article_html") or ""
            article.last_seen_at = now
            affected += 1
            logger.info(
                "upsert success article_id=%s mode=%s fans=%s views=%s likes=%s comments=%s (read_from_author=%s)",
                article_id,
                "create" if is_new else "update",
                article.followers,
                article.view_count,
                article.like_count,
                article.comment_count,
                read_count,
            )
        except Exception as exc:
            stats["errors"] += 1
            logger.exception("upsert failed article_id=%s err=%s", article_id, exc)
    db.session.commit()
    logger.info(
        "upsert summary total=%s affected=%s created=%s updated=%s skip_time=%s skip_fans=%s errors=%s",
        stats["total_candidates"],
        affected,
        stats["created"],
        stats["updated"],
        stats["skip_time"],
        stats["skip_fans"],
        stats["errors"],
    )
    return affected


def run_crawl_job():
    logger.info("crawl job started")
    list_crawler = ToutiaoCrawler(headless=current_app.config["CRAWL_HEADLESS"])
    try:
        items = list_crawler.crawl_recommend_page(current_app.config["CRAWL_TARGET_COUNT"])
    finally:
        list_crawler.close()
    logger.info("crawl list collected=%s", len(items))
    changed = upsert_articles(items)
    logger.info("crawl job finished, upserted=%s", changed)


def cleanup_expired_articles():
    expire_before = cn_now_naive() - timedelta(hours=24)
    deleted = (
        Article.query.filter(
            ((Article.published_at.isnot(None)) & (Article.published_at < expire_before))
            | ((Article.published_at.is_(None)) & (Article.created_at < expire_before))
        )
        .delete(synchronize_session=False)
    )
    db.session.commit()
    logger.info("cleanup job finished, deleted=%s", deleted)
