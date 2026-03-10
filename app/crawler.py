import logging
import os
import re
import time
import json
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup
from flask import current_app
from selenium import webdriver
from selenium.common.exceptions import InvalidSessionIdException, TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError
from webdriver_manager.chrome import ChromeDriverManager

from .extensions import db
from .models import Article, AuthorSource
from .time_utils import SHANGHAI_TZ, cn_now_naive
from .utils import parse_hours_ago, parse_number, parse_publish_datetime, sha256_hex


logger = logging.getLogger(__name__)


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


def _chunked(items: List[Dict], n_chunks: int) -> List[List[Dict]]:
    if not items:
        return []
    n_chunks = max(1, min(n_chunks, len(items)))
    size = (len(items) + n_chunks - 1) // n_chunks
    return [items[i : i + size] for i in range(0, len(items), size)]


def _lease_owner_name() -> str:
    raw = str(current_app.config.get("WORKER_ROLE") or "").strip()
    if raw:
        return raw
    return f"worker-{os.getpid()}"


def normalize_article_url(url: str) -> str:
    if not url:
        return ""
    raw = str(url).strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if raw.startswith("/"):
        raw = f"https://www.toutiao.com{raw}"

    parts = urlsplit(raw)
    scheme = parts.scheme or "https"
    netloc = parts.netloc or "www.toutiao.com"
    path = parts.path or ""

    # 统一文章链接形态，去掉 query/fragment（例如 #comment）
    m = re.search(r"/article/(\d+)/?", path)
    if m:
        path = f"/article/{m.group(1)}/"
    return urlunsplit((scheme, netloc, path, "", ""))


def sanitize_article_url_for_storage(url: str) -> str:
    clean = normalize_article_url(url)
    if not clean:
        return ""
    # 存储层最终保护：非标准 article 链接不入库
    if not re.search(r"/article/\d+/?$", clean):
        return ""
    return clean


def acquire_author_leases(limit: int) -> List[AuthorSource]:
    if limit <= 0:
        return []
    now = cn_now_naive()
    owner = _lease_owner_name()
    lease_seconds = int(current_app.config.get("AUTHOR_LEASE_SECONDS", 240))
    lease_until = now + timedelta(seconds=lease_seconds)

    # 先多取一些候选，允许在竞争中部分领取失败
    candidate_rows = (
        AuthorSource.query.filter(
            AuthorSource.status == "active",
            or_(AuthorSource.lease_until.is_(None), AuthorSource.lease_until < now),
        )
        .order_by(AuthorSource.last_crawled_at.isnot(None), AuthorSource.last_crawled_at.asc(), AuthorSource.id.asc())
        .limit(limit * 3)
        .all()
    )
    leased_ids: List[int] = []
    for row in candidate_rows:
        updated = (
            AuthorSource.query.filter(
                AuthorSource.id == row.id,
                AuthorSource.status == "active",
                or_(AuthorSource.lease_until.is_(None), AuthorSource.lease_until < now),
            )
            .update(
                {
                    AuthorSource.lease_owner: owner,
                    AuthorSource.lease_until: lease_until,
                },
                synchronize_session=False,
            )
        )
        if updated == 1:
            leased_ids.append(int(row.id))
        if len(leased_ids) >= limit:
            break
    if leased_ids:
        _commit_with_retry()
        return AuthorSource.query.filter(AuthorSource.id.in_(leased_ids)).all()
    db.session.rollback()
    return []


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
            article_url = normalize_article_url(article_url)

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

    def _extract_article_container(self, soup: BeautifulSoup):
        selectors = [
            "article.syl-article-base",
            ".syl-article-base",
            ".syl-page-article",
            ".article-main",
            ".article-content-wrap",
            ".article-content",
            ".a-con",
            ".tt-article-content",
            ".tt-post-content",
            ".pgc-article",
            ".wtt-content",
            ".wtt-details-content",
            "[data-testid='article-content']",
            ".content",
            "article",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                return node
        return None

    def _is_meaningful_article_html(self, article_html: str) -> bool:
        if not article_html:
            return False
        text = BeautifulSoup(article_html, "html.parser").get_text(" ", strip=True)
        return len(text) >= 80

    def _wait_article_ready(self, timeout_seconds: int = 6):
        selectors = [
            "article.syl-article-base",
            ".syl-article-base",
            ".syl-page-article",
            ".article-main",
            ".article-content-wrap",
            ".article-content",
            ".a-con",
            ".tt-article-content",
            ".tt-post-content",
            ".pgc-article",
            "article",
        ]
        selector_js = ",".join([f'"{s}"' for s in selectors])
        js = f"""
        const selectors = [{selector_js}];
        for (const s of selectors) {{
          const el = document.querySelector(s);
          if (!el) continue;
          const t = (el.innerText || "").trim();
          if (t.length >= 80) return true;
        }}
        return false;
        """
        WebDriverWait(self.driver, timeout_seconds).until(lambda d: bool(d.execute_script(js)))

    def _extract_article_html_from_scripts(self, soup: BeautifulSoup, html_text: str) -> str:
        # JSON-LD 正文
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            text = (script.string or script.get_text() or "").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            blocks = data if isinstance(data, list) else [data]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                body = block.get("articleBody")
                if isinstance(body, str) and len(body.strip()) >= 50:
                    content = html.escape(body.strip()).replace("\n", "</p><p>")
                    return f"<article><p>{content}</p></article>"

        # 兜底：脚本中的 content/articleBody/body 字段
        patterns = [
            r'"articleBody"\s*:\s*"(.{80,}?)"',
            r'"content"\s*:\s*"(.{80,}?)"',
            r'"body"\s*:\s*"(.{80,}?)"',
        ]
        for pattern in patterns:
            m = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            raw = m.group(1)
            cleaned = raw.encode("utf-8", "ignore").decode("unicode_escape", errors="ignore")
            cleaned = cleaned.replace("\\n", "\n").replace("\\t", " ").strip()
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
            if len(cleaned) >= 80:
                content = html.escape(cleaned).replace("\n", "</p><p>")
                return f"<article><p>{content}</p></article>"
        return ""

    def _get_article_details(self, article_url: str) -> Dict:
        detail = {
            "like_count": 0,
            "comment_count": 0,
            "article_html": "",
            "title": "",
            "publish_time_text": "",
            "published_at": None,
        }
        try:
            article_url = normalize_article_url(article_url)
            self._safe_get(article_url)
            self._safe_execute_script("window.scrollTo(0, 0);")
            try:
                self._wait_article_ready(timeout_seconds=6)
            except TimeoutException:
                # 页面结构不稳定时继续走兜底解析，不中断流程
                time.sleep(1.0)
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

            container = self._extract_article_container(soup)
            if container:
                detail["article_html"] = str(container)
            else:
                fallback_html = self._extract_article_html_from_scripts(soup, html)
                if fallback_html:
                    detail["article_html"] = fallback_html
                    logger.info("article html extracted via script fallback url=%s", article_url)
                else:
                    logger.info("empty article html extracted url=%s", article_url)

            # 二次兜底：首轮正文为空或正文过短时，等待并重抓一次
            if not self._is_meaningful_article_html(detail.get("article_html", "")):
                try:
                    self._safe_execute_script("window.scrollTo(0, 0);")
                    self._wait_article_ready(timeout_seconds=5)
                except Exception:
                    time.sleep(0.8)
                html_retry = self._safe_page_source()
                soup_retry = BeautifulSoup(html_retry, "html.parser")
                container_retry = self._extract_article_container(soup_retry)
                if container_retry:
                    retry_html = str(container_retry)
                    if self._is_meaningful_article_html(retry_html):
                        detail["article_html"] = retry_html
                        logger.info("article html extracted via retry url=%s", article_url)
                if not self._is_meaningful_article_html(detail.get("article_html", "")):
                    fallback_retry = self._extract_article_html_from_scripts(soup_retry, html_retry)
                    if fallback_retry:
                        detail["article_html"] = fallback_retry
                        logger.info("article html extracted via retry script fallback url=%s", article_url)

            title_elem = soup.select_one("h1") or soup.select_one("title")
            if title_elem:
                detail["title"] = title_elem.get_text(strip=True)[:200]
            published_at, publish_text = self._extract_published_at_from_html(soup, html)
            if published_at:
                detail["published_at"] = published_at
            if publish_text:
                detail["publish_time_text"] = publish_text
        except Exception as exc:
            logger.warning("get detail failed: %s", exc)
        return detail

    def _extract_published_at_from_html(self, soup: BeautifulSoup, html: str):
        candidates = []
        meta_selectors = [
            ('meta[property="article:published_time"]', "content"),
            ('meta[property="og:published_time"]', "content"),
            ('meta[name="publish_time"]', "content"),
            ('meta[name="publishdate"]', "content"),
            ('meta[itemprop="datePublished"]', "content"),
        ]
        for selector, attr in meta_selectors:
            node = soup.select_one(selector)
            if node and node.get(attr):
                candidates.append(node.get(attr, "").strip())

        # JSON-LD / inline script 常见字段
        string_patterns = [
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'"publish_time"\s*:\s*"([^"]+)"',
            r'"publishTime"\s*:\s*"([^"]+)"',
            r'"published_at"\s*:\s*"([^"]+)"',
        ]
        for pattern in string_patterns:
            m = re.search(pattern, html, re.IGNORECASE)
            if m:
                candidates.append((m.group(1) or "").strip())

        for text in candidates:
            dt = parse_publish_datetime(text)
            if dt:
                return dt, text

        # 时间戳兜底（秒或毫秒）
        ts_patterns = [
            r'"publish_time"\s*:\s*(\d{10,13})',
            r'"publishTime"\s*:\s*(\d{10,13})',
            r'"create_time"\s*:\s*(\d{10,13})',
            r'"created_time"\s*:\s*(\d{10,13})',
        ]
        for pattern in ts_patterns:
            m = re.search(pattern, html, re.IGNORECASE)
            if not m:
                continue
            raw = m.group(1)
            try:
                ts = int(raw)
                if ts > 10**11:
                    ts = ts // 1000
                dt = datetime.fromtimestamp(ts, SHANGHAI_TZ).replace(tzinfo=None)
                return dt, raw
            except Exception:
                continue
        return None, ""

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

    def crawl_author_recent_articles(self, author_url: str, author_name: str = "", max_items: int = 30) -> List[Dict]:
        if not author_url:
            return []
        scroll_rounds = int(current_app.config.get("AUTHOR_ARTICLE_SCROLL_ROUNDS", 4))
        found_map = {}
        self._safe_get(author_url)
        time.sleep(1.2)
        for _ in range(scroll_rounds):
            html = self._safe_page_source()
            soup = BeautifulSoup(html, "html.parser")
            links = soup.find_all("a", href=True)
            for link in links:
                href = (link.get("href") or "").strip()
                if "/article/" not in href:
                    continue
                article_url = href if href.startswith("http") else f"https://www.toutiao.com{href}"
                article_url = normalize_article_url(article_url)
                m = re.search(r"/article/(\d+)/", article_url)
                if not m:
                    continue
                article_id = m.group(1)
                title = (link.get_text(strip=True) or link.get("title") or "").strip()
                if not title:
                    continue
                publish_time = ""
                parent = link
                for _ in range(8):
                    if parent is None:
                        break
                    text = parent.get_text(" ", strip=True)
                    tm = re.search(
                        r"(今天\s*\d{1,2}:\d{1,2}|昨天\s*\d{1,2}:\d{1,2}|\d+\s*小时前|\d+\s*分钟前|\d+\s*天前|"
                        r"\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{1,2})?|"
                        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*\d{1,2}:\d{1,2}(?::\d{1,2})?)",
                        text,
                    )
                    if tm:
                        publish_time = (tm.group(1) or "").strip()
                        break
                    parent = parent.parent
                found_map[article_id] = {
                    "article_id": article_id,
                    "url": article_url,
                    "title": title[:200],
                    "author": author_name or "",
                    "author_url": author_url,
                    "publish_time": publish_time,
                    "comment_count": 0,
                    "cover": "",
                }
                if len(found_map) >= max_items:
                    break
            if len(found_map) >= max_items:
                break
            self._safe_execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.0)
        return list(found_map.values())[:max_items]


def upsert_articles(items: List[Dict], max_hours: Optional[float] = None):
    now = cn_now_naive()
    app = current_app._get_current_object()
    max_hours = float(max_hours if max_hours is not None else current_app.config["CRAWL_MAX_HOURS"])
    max_fans = int(current_app.config["CRAWL_MAX_FANS"])
    detail_workers = max(1, int(current_app.config.get("CRAWL_DETAIL_WORKERS", 3)))
    crawl_headless = bool(current_app.config["CRAWL_HEADLESS"])
    affected = 0
    stats = {
        "total_candidates": len(items),
        "skip_time": 0,
        "skip_fans": 0,
        "empty_html": 0,
        "skip_empty_html_create": 0,
        "created": 0,
        "updated": 0,
        "errors": 0,
    }
    logger.info("detail workers=%s", detail_workers)

    def enrich_chunk(chunk: List[Dict]) -> List[Dict]:
        chunk_results: List[Dict] = []
        with app.app_context():
            crawler = ToutiaoCrawler(headless=crawl_headless)
            try:
                for base in chunk:
                    article_id = base.get("article_id", "")
                    title = (base.get("title") or "")[:40]
                    try:
                        logger.info("detail processing article_id=%s title=%s", article_id, title)
                        list_hours_ago = parse_hours_ago(base.get("publish_time", ""))
                        fans = int(base.get("followers") or 0)
                        if fans <= 0:
                            fans = crawler._get_author_fans_count(base.get("author_url", ""))
                        if fans >= max_fans:
                            chunk_results.append(
                                {"status": "skip_fans", "article_id": article_id, "fans": fans}
                            )
                            continue

                        details = crawler._get_article_details(base["url"])
                        detail_published_at = details.get("published_at")
                        hours_ago = list_hours_ago
                        if detail_published_at:
                            hours_ago = max(0.0, (now - detail_published_at).total_seconds() / 3600)
                        if hours_ago is None or hours_ago > max_hours:
                            chunk_results.append(
                                {
                                    "status": "skip_time",
                                    "article_id": article_id,
                                    "hours_ago": hours_ago,
                                }
                            )
                            continue
                        read_count = crawler._get_article_read_count_from_author(
                            author_url=base.get("author_url", ""),
                            article_id=base.get("article_id", ""),
                            article_title=base.get("title", ""),
                            article_url=base.get("url", ""),
                        )
                        chunk_results.append(
                            {
                                "status": "ok",
                                "article_id": article_id,
                                "base": base,
                                "hours_ago": float(hours_ago),
                                "fans": int(fans),
                                "details": details,
                                "read_count": int(read_count or 0),
                                "published_at": detail_published_at,
                                "publish_time_text": details.get("publish_time_text")
                                or base.get("publish_time", ""),
                            }
                        )
                    except Exception as exc:
                        chunk_results.append(
                            {"status": "error", "article_id": article_id, "error": str(exc)[:300]}
                        )
            finally:
                crawler.close()
        return chunk_results

    enrich_results = []
    chunks = _chunked(items, detail_workers)
    with ThreadPoolExecutor(max_workers=max(1, len(chunks))) as executor:
        future_map = {executor.submit(enrich_chunk, chunk): chunk for chunk in chunks}
        for future in as_completed(future_map):
            try:
                results = future.result()
                for result in results:
                    article_id = result.get("article_id", "")
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
                    if status == "error":
                        stats["errors"] += 1
                        logger.warning(
                            "detail enrich failed article_id=%s err=%s",
                            article_id,
                            result.get("error", "unknown"),
                        )
                        continue
                    enrich_results.append(result)
            except Exception as exc:
                stats["errors"] += 1
                logger.exception("detail enrich chunk failed err=%s", exc)

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
            base["url"] = sanitize_article_url_for_storage(base.get("url", ""))
            if not base["url"]:
                stats["errors"] += 1
                logger.warning("skip article_id=%s reason=invalid_storage_url", article_id)
                continue
            published_at = item.get("published_at") or (now - timedelta(hours=hours_ago))
            url_hash = sha256_hex(base["url"])
            article = Article.query.filter(
                (Article.article_id == base["article_id"]) | (Article.url_hash == url_hash)
            ).first()
            is_new = article is None
            article_html = (details.get("article_html") or "").strip()
            if not article_html:
                stats["empty_html"] += 1
                if is_new:
                    stats["skip_empty_html_create"] += 1
                    logger.info("skip create article_id=%s reason=empty_source_html", article_id)
                    continue
            if is_new:
                article = Article(article_id=base["article_id"], url_hash=url_hash, url=base["url"])
                db.session.add(article)
                stats["created"] += 1
            else:
                stats["updated"] += 1

            article.title = details.get("title") or base.get("title") or "无标题"
            article.url = base["url"]
            article.url_hash = url_hash
            article.cover = base.get("cover", "")
            article.author = base.get("author", "")
            article.author_url = base.get("author_url", "")
            article.publish_time_text = item.get("publish_time_text") or base.get("publish_time", "")
            article.published_at = published_at
            article.published_hours_ago = max(0.0, (now - published_at).total_seconds() / 3600)
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
            if article_html:
                article.source_html = article_html
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
    _commit_with_retry()
    logger.info(
        "upsert summary total=%s affected=%s created=%s updated=%s skip_time=%s skip_fans=%s empty_html=%s skip_empty_create=%s errors=%s",
        stats["total_candidates"],
        affected,
        stats["created"],
        stats["updated"],
        stats["skip_time"],
        stats["skip_fans"],
        stats["empty_html"],
        stats["skip_empty_html_create"],
        stats["errors"],
    )
    return affected


def collect_authors_from_recommend():
    logger.info("author collect job started")
    now = cn_now_naive()
    max_fans = int(current_app.config["CRAWL_MAX_FANS"])
    target_count = int(current_app.config.get("AUTHOR_COLLECT_TARGET_COUNT", current_app.config["CRAWL_TARGET_COUNT"]))
    commit_batch_size = max(1, int(current_app.config.get("AUTHOR_COLLECT_COMMIT_BATCH_SIZE", 50)))
    crawler = ToutiaoCrawler(headless=current_app.config["CRAWL_HEADLESS"])
    created = 0
    updated = 0
    skipped = 0
    errors = 0
    try:
        items = crawler.crawl_recommend_page(target_count)
        author_map = {}
        for item in items:
            author_url = (item.get("author_url") or "").strip()
            author_name = (item.get("author") or "").strip()
            if not author_url:
                continue
            if crawler._is_blocked_author(author_name):
                skipped += 1
                continue
            author_map[author_url] = {"author_url": author_url, "author_name": author_name}

        author_urls = list(author_map.keys())
        existing_rows = []
        if author_urls:
            existing_rows = AuthorSource.query.filter(AuthorSource.author_url.in_(author_urls)).all()
        existing_map = {row.author_url: row for row in existing_rows}

        pending_changes = 0
        for base in author_map.values():
            try:
                fans = crawler._get_author_fans_count(base["author_url"])
                if fans >= max_fans:
                    skipped += 1
                    continue
                row = existing_map.get(base["author_url"])
                if row is None:
                    row = AuthorSource(
                        author_url=base["author_url"],
                        author_name=base.get("author_name", ""),
                        first_seen_at=now,
                    )
                    db.session.add(row)
                    existing_map[base["author_url"]] = row
                    created += 1
                else:
                    updated += 1
                row.author_name = base.get("author_name", "") or row.author_name
                row.followers = int(fans)
                if row.status != "invalid":
                    row.status = "active"
                row.last_seen_at = now
                row.last_error = ""
                pending_changes += 1
                if pending_changes >= commit_batch_size:
                    _commit_with_retry()
                    pending_changes = 0
            except Exception as exc:
                db.session.rollback()
                errors += 1
                logger.warning("author collect failed author=%s err=%s", base.get("author_url"), exc)
        if pending_changes > 0:
            _commit_with_retry()
        logger.info(
            "author collect summary candidates=%s distinct=%s created=%s updated=%s skipped=%s errors=%s",
            len(items),
            len(author_map),
            created,
            updated,
            skipped,
            errors,
        )
    finally:
        crawler.close()
    return created + updated


def crawl_from_author_pool():
    logger.info("author articles job started")
    now = cn_now_naive()
    max_hours = float(current_app.config.get("AUTHOR_ARTICLE_MAX_HOURS", 24))
    batch_size = int(current_app.config.get("AUTHOR_CRAWL_BATCH_SIZE", 20))
    per_author_limit = int(current_app.config.get("AUTHOR_PER_AUTHOR_TARGET_COUNT", 20))
    max_fails = int(current_app.config.get("AUTHOR_MAX_FAILS", 5))
    owner = _lease_owner_name()
    authors = acquire_author_leases(batch_size)
    if not authors:
        logger.info("author articles job skipped: no leasable authors")
        return 0

    crawler = ToutiaoCrawler(headless=current_app.config["CRAWL_HEADLESS"])
    total_changed = 0
    try:
        for author in authors:
            author_id = int(author.id)
            author_url = author.author_url
            author_name = author.author_name
            author_followers = int(author.followers or 0)
            try:
                items = crawler.crawl_author_recent_articles(
                    author_url=author_url,
                    author_name=author_name,
                    max_items=per_author_limit,
                )
                if items:
                    for item in items:
                        item["followers"] = author_followers
                    changed = upsert_articles(items, max_hours=max_hours)
                    total_changed += int(changed)

                # 单作者状态更新独立提交，减少长事务锁竞争
                now_local = cn_now_naive()
                AuthorSource.query.filter(
                    AuthorSource.id == author_id, AuthorSource.lease_owner == owner
                ).update(
                    {
                        AuthorSource.last_crawled_at: now_local,
                        AuthorSource.fail_count: 0,
                        AuthorSource.last_error: "",
                        AuthorSource.lease_owner: "",
                        AuthorSource.lease_until: None,
                    },
                    synchronize_session=False,
                )
                _commit_with_retry()
            except Exception as exc:
                # 先回滚坏事务，避免 PendingRollbackError 连锁
                db.session.rollback()
                try:
                    row = AuthorSource.query.filter(AuthorSource.id == author_id).first()
                    if row:
                        row.fail_count = int(row.fail_count or 0) + 1
                        row.last_error = str(exc)[:500]
                        if row.fail_count >= max_fails:
                            row.status = "invalid"
                        row.lease_owner = ""
                        row.lease_until = None
                        _commit_with_retry()
                except Exception as update_exc:
                    db.session.rollback()
                    logger.warning(
                        "author crawl status update failed author=%s err=%s",
                        author_url,
                        update_exc,
                    )
                logger.warning(
                    "author crawl failed author=%s fail_count=%s err=%s",
                    author_url,
                    (row.fail_count if "row" in locals() and row else "n/a"),
                    exc,
                )
    finally:
        crawler.close()
    logger.info(
        "author articles summary owner=%s processed_authors=%s changed_articles=%s max_hours=%s elapsed_seconds=%.2f",
        owner,
        len(authors),
        total_changed,
        max_hours,
        (cn_now_naive() - now).total_seconds(),
    )
    return total_changed


def run_crawl_job():
    # backward compatible wrapper
    logger.info("crawl job started (legacy wrapper)")
    collect_authors_from_recommend()
    changed = crawl_from_author_pool()
    logger.info("crawl job finished, upserted=%s", changed)


def run_author_collect_job():
    collect_authors_from_recommend()


def run_author_articles_job():
    crawl_from_author_pool()


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
