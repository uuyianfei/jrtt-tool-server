import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List

from bs4 import BeautifulSoup
from flask import current_app
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from .extensions import db
from .models import Article
from .utils import parse_hours_ago, parse_number


logger = logging.getLogger(__name__)


class ToutiaoCrawler:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = self._init_browser()

    def _init_browser(self):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "eager"
        if self.headless:
            options.add_argument("--headless=new")
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

            title = title_elem.get_text(strip=True) or f"文章_{article_id_match.group(1)}"
            if any(kw in title.lower() for kw in ["视频", "video", "直播", "live"]):
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

            publish_time = ""
            time_elem = card.find("div", class_="feed-card-footer-time-cmp") or card.find("div", class_="time")
            if time_elem:
                publish_time = time_elem.get_text(strip=True)

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
                "cover": cover,
            }
        except Exception as exc:
            logger.warning("extract article info failed: %s", exc)
            return None

    def _get_author_fans_count(self, author_url: str) -> int:
        if not author_url:
            return 0
        try:
            self.driver.get(author_url)
            time.sleep(1.2)
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
            stat_items = soup.find_all("button", class_="stat-item")
            for item in stat_items:
                aria = item.get("aria-label", "")
                if "粉丝" in aria or "粉丝" in item.get_text(strip=True):
                    num_span = item.find("span", class_="num")
                    if num_span:
                        return parse_number(num_span.get_text(strip=True))
            match = re.search(r"粉丝\s*(\d+(?:\.\d+)?)([万亿]?)", soup.get_text())
            if match:
                return parse_number(match.group(1) + match.group(2))
        except Exception as exc:
            logger.warning("get fans failed: %s", exc)
        return 0

    def _get_article_details(self, article_url: str) -> Dict:
        detail = {
            "like_count": 0,
            "comment_count": 0,
            "view_count": 0,
            "article_html": "",
            "title": "",
        }
        try:
            self.driver.get(article_url)
            time.sleep(1.5)
            html = self.driver.page_source
            soup = BeautifulSoup(html, "html.parser")

            like_patterns = [r"点赞\s*(\d+)", r'"digg_count"\s*:\s*(\d+)']
            for pattern in like_patterns:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    detail["like_count"] = int(m.group(1))
                    break

            comment_patterns = [r"评论\s*(\d+)", r'"comment_count"\s*:\s*(\d+)']
            for pattern in comment_patterns:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    detail["comment_count"] = int(m.group(1))
                    break

            view_patterns = [r"(\d+(?:\.\d+)?)([万]?)\s*阅读", r'"read_count"\s*:\s*(\d+)']
            for pattern in view_patterns:
                m = re.search(pattern, html, re.IGNORECASE)
                if m:
                    if len(m.groups()) == 2:
                        detail["view_count"] = parse_number((m.group(1) or "") + (m.group(2) or ""))
                    else:
                        detail["view_count"] = int(m.group(1))
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

    def crawl_recommend_page(self, target_count: int) -> List[Dict]:
        url = current_app.config["TOUTIAO_URL"]
        self.driver.get(url)
        time.sleep(2)
        self.driver.refresh()
        time.sleep(2)
        self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.2)
        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        cards = self._find_article_cards(soup)

        found = []
        for card in cards:
            info = self._extract_article_info(card)
            if not info:
                continue
            found.append(info)
            if len(found) >= target_count:
                break
        return found


def upsert_articles(items: List[Dict]):
    now = datetime.utcnow()
    max_hours = float(current_app.config["CRAWL_MAX_HOURS"])
    max_fans = int(current_app.config["CRAWL_MAX_FANS"])
    affected = 0

    crawler = ToutiaoCrawler(headless=current_app.config["CRAWL_HEADLESS"])
    try:
        for base in items:
            hours_ago = parse_hours_ago(base.get("publish_time", ""))
            if hours_ago is None or hours_ago > max_hours:
                continue

            fans = crawler._get_author_fans_count(base.get("author_url", ""))
            if fans >= max_fans:
                continue

            details = crawler._get_article_details(base["url"])
            published_at = now - timedelta(hours=float(hours_ago))
            article = Article.query.filter(
                (Article.article_id == base["article_id"]) | (Article.url == base["url"])
            ).first()
            if not article:
                article = Article(article_id=base["article_id"], url=base["url"])
                db.session.add(article)

            article.title = details.get("title") or base.get("title") or "无标题"
            article.cover = base.get("cover", "")
            article.author = base.get("author", "")
            article.author_url = base.get("author_url", "")
            article.publish_time_text = base.get("publish_time", "")
            article.published_at = published_at
            article.published_hours_ago = float(hours_ago)
            article.followers = int(fans)
            article.view_count = int(details.get("view_count") or 0)
            article.like_count = int(details.get("like_count") or 0)
            article.comment_count = int(details.get("comment_count") or 0)
            article.source_html = details.get("article_html") or ""
            article.last_seen_at = now
            affected += 1
        db.session.commit()
    finally:
        crawler.close()
    return affected


def run_crawl_job():
    logger.info("crawl job started")
    list_crawler = ToutiaoCrawler(headless=current_app.config["CRAWL_HEADLESS"])
    try:
        items = list_crawler.crawl_recommend_page(current_app.config["CRAWL_TARGET_COUNT"])
    finally:
        list_crawler.close()
    changed = upsert_articles(items)
    logger.info("crawl job finished, upserted=%s", changed)


def cleanup_expired_articles():
    expire_before = datetime.utcnow() - timedelta(hours=24)
    deleted = (
        Article.query.filter(
            ((Article.published_at.isnot(None)) & (Article.published_at < expire_before))
            | ((Article.published_at.is_(None)) & (Article.created_at < expire_before))
        )
        .delete(synchronize_session=False)
    )
    db.session.commit()
    logger.info("cleanup job finished, deleted=%s", deleted)
