import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import pymysql
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymysql.connections import Connection
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import authors into author_sources with crawl enrichment.")
    parser.add_argument("--file", default="good_authors.json", help="Path to source JSON file.")
    parser.add_argument("--batch-size", type=int, default=100, help="DB commit batch size.")
    parser.add_argument("--dry-run", action="store_true", help="Only parse/crawl, do not write DB.")
    parser.add_argument("--table", default="author_sources", help="Target table name.")
    parser.add_argument("--headless", action="store_true", default=True, help="Run Chrome in headless mode.")
    parser.add_argument("--no-headless", action="store_false", dest="headless", help="Run Chrome with UI.")
    parser.add_argument(
        "--crawl-delay",
        type=float,
        default=1.2,
        help="Sleep seconds after opening each author page.",
    )
    return parser.parse_args()


def parse_number(text: str) -> int:
    if not text:
        return 0
    raw = str(text).strip()
    try:
        if "万" in raw:
            return int(float(raw.replace("万", "")) * 10000)
        if "亿" in raw:
            return int(float(raw.replace("亿", "")) * 100000000)
        cleaned = re.sub(r"[^\d.]", "", raw)
        return int(float(cleaned)) if cleaned else 0
    except ValueError:
        return 0


def clean_author_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    for suffix in ["- 今日头条", "_今日头条", " - 今日头条", " 的头条主页", "的头条主页"]:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text[:128]


def load_author_urls(file_path: Path) -> List[str]:
    if not file_path.exists():
        raise FileNotFoundError(f"JSON file not found: {file_path}")
    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict):
        candidates = payload.get("good_authors")
    elif isinstance(payload, list):
        candidates = payload
    else:
        raise ValueError("Unsupported JSON format. Expected object or list.")

    if not isinstance(candidates, list):
        raise ValueError("Unsupported JSON format: good_authors must be a list.")

    urls: List[str] = []
    for item in candidates:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = str(item.get("author_url", "")).strip()
        else:
            url = ""
        if url:
            urls.append(url)

    seen = set()
    deduped: List[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def get_mysql_config() -> Dict[str, object]:
    load_dotenv()
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        "database": os.getenv("MYSQL_DB", "jrtt_tool"),
    }


def validate_table_name(table: str) -> str:
    value = (table or "").strip()
    if not value:
        raise ValueError("Table name is empty.")
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Invalid table name: {table}")
    return value


def chunked(items: Sequence, size: int) -> List[Sequence]:
    step = max(1, int(size))
    return [items[i : i + step] for i in range(0, len(items), step)]


class AuthorMetaCrawler:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver = self._init_driver()

    def _resolve_driver_path(self) -> str:
        configured = (os.getenv("CHROMEDRIVER_PATH", "") or "").strip()
        if configured and os.path.exists(configured):
            return configured
        cache_root = Path.home() / ".wdm" / "drivers" / "chromedriver"
        if cache_root.exists():
            candidates = list(cache_root.rglob("chromedriver.exe"))
            if candidates:
                candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return str(candidates[0])
        return ""

    def _init_driver(self):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "eager"
        chrome_binary = (os.getenv("CHROME_BINARY_PATH", "") or "").strip()
        if chrome_binary:
            options.binary_location = chrome_binary
        if self.headless:
            options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=zh-CN")
        options.add_argument("--disable-blink-features=AutomationControlled")
        ua = (os.getenv("CRAWL_USER_AGENT", "") or "").strip()
        if ua:
            options.add_argument(f"user-agent={ua}")

        driver_path = self._resolve_driver_path()
        if driver_path:
            service = Service(driver_path)
        else:
            service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver

    def _extract_fans(self, soup: BeautifulSoup) -> int:
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
                m = re.search(r"(\d+(?:\.\d+)?)([万亿]?)", item_text)
                if m:
                    fans = parse_number((m.group(1) or "") + (m.group(2) or ""))
                    if fans > 0:
                        return fans

        full_text = soup.get_text(" ", strip=True)
        m = re.search(r"粉丝\s*(\d+(?:\.\d+)?)([万亿]?)", full_text)
        if m:
            return parse_number((m.group(1) or "") + (m.group(2) or ""))
        return 0

    def _extract_name(self, soup: BeautifulSoup) -> str:
        selectors = [
            ".user-info-name",
            ".user-name",
            ".name",
            "h1",
            "title",
        ]
        for sel in selectors:
            node = soup.select_one(sel)
            if not node:
                continue
            name = clean_author_name(node.get_text(strip=True))
            if name:
                return name

        og_title = soup.select_one('meta[property="og:title"]')
        if og_title:
            name = clean_author_name(og_title.get("content", ""))
            if name:
                return name
        return ""

    def crawl_one(self, author_url: str, crawl_delay: float) -> Dict[str, object]:
        result: Dict[str, object] = {
            "author_url": author_url,
            "author_name": "",
            "followers": 0,
            "error": "",
        }
        try:
            self.driver.get(author_url)
            time.sleep(max(0.2, float(crawl_delay)))
            soup = BeautifulSoup(self.driver.page_source or "", "html.parser")
            result["author_name"] = self._extract_name(soup)
            result["followers"] = int(self._extract_fans(soup))
        except Exception as exc:
            result["error"] = str(exc)[:500]
        return result

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


def count_existing(cur, table: str, urls: Sequence[str]) -> int:
    if not urls:
        return 0
    total = 0
    for batch in chunked(urls, 1000):
        placeholders = ", ".join(["%s"] * len(batch))
        sql = f"SELECT COUNT(*) FROM {table} WHERE author_url IN ({placeholders})"
        cur.execute(sql, tuple(batch))
        total += int(cur.fetchone()[0])
    return total


def upsert_author_rows(conn: Connection, table: str, rows: List[Tuple], batch_size: int):
    sql = (
        f"INSERT INTO {table} "
        "(author_url, author_name, followers, status, lease_owner, fail_count, last_error, first_seen_at, last_seen_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE "
        "author_name = CASE WHEN VALUES(author_name) <> '' THEN VALUES(author_name) ELSE author_name END, "
        "followers = CASE WHEN VALUES(followers) > 0 THEN VALUES(followers) ELSE followers END, "
        "status = CASE WHEN status = 'invalid' THEN status ELSE 'active' END, "
        "last_error = '', "
        "last_seen_at = NOW(), "
        "updated_at = NOW()"
    )
    with conn.cursor() as cur:
        for batch in chunked(rows, batch_size):
            cur.executemany(sql, batch)
        conn.commit()


def import_author_urls(
    conn: Connection,
    table: str,
    crawled_rows: List[Dict[str, object]],
    batch_size: int,
    dry_run: bool,
) -> Dict[str, int]:
    urls = [str(x.get("author_url") or "") for x in crawled_rows if str(x.get("author_url") or "")]
    if not urls:
        return {"total": 0, "existing": 0, "created_or_updated": 0, "with_followers": 0, "crawl_failed": 0}

    with conn.cursor() as cur:
        existing_count = count_existing(cur, table, urls)

    with_followers = sum(1 for x in crawled_rows if int(x.get("followers") or 0) > 0)
    crawl_failed = sum(1 for x in crawled_rows if str(x.get("error") or ""))

    if dry_run:
        return {
            "total": len(urls),
            "existing": existing_count,
            "created_or_updated": 0,
            "to_create": len(urls) - existing_count,
            "with_followers": with_followers,
            "crawl_failed": crawl_failed,
        }

    db_rows = []
    for row in crawled_rows:
        db_rows.append(
            (
                str(row.get("author_url") or ""),
                clean_author_name(str(row.get("author_name") or "")),
                int(row.get("followers") or 0),
                "active",
                "",
                0,
                "",
            )
        )
    upsert_author_rows(conn=conn, table=table, rows=db_rows, batch_size=batch_size)

    return {
        "total": len(urls),
        "existing": existing_count,
        "created_or_updated": len(db_rows),
        "with_followers": with_followers,
        "crawl_failed": crawl_failed,
    }


def main() -> int:
    args = parse_args()
    source_file = Path(args.file).resolve()
    try:
        table = validate_table_name(args.table)
    except Exception as exc:
        print(f"[失败] 表名不合法: {exc}")
        return 1

    try:
        urls = load_author_urls(source_file)
    except Exception as exc:
        print(f"[失败] 读取 JSON 失败: {exc}")
        return 1
    if not urls:
        print("[完成] 无可导入作者链接")
        return 0

    crawler = None
    crawled_rows: List[Dict[str, object]] = []
    try:
        crawler = AuthorMetaCrawler(headless=args.headless)
        total = len(urls)
        for idx, url in enumerate(urls, start=1):
            row = crawler.crawl_one(author_url=url, crawl_delay=args.crawl_delay)
            crawled_rows.append(row)
            if idx % 20 == 0 or idx == total:
                print(
                    "[爬取进度] {cur}/{total} followers_ok={ok} failed={failed}".format(
                        cur=idx,
                        total=total,
                        ok=sum(1 for x in crawled_rows if int(x.get("followers") or 0) > 0),
                        failed=sum(1 for x in crawled_rows if str(x.get("error") or "")),
                    )
                )
    except Exception as exc:
        print(f"[失败] 爬取作者信息失败: {exc}")
        return 2
    finally:
        if crawler:
            crawler.close()

    cfg = get_mysql_config()
    try:
        conn = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            connect_timeout=10,
            autocommit=False,
        )
    except Exception as exc:
        print(f"[失败] 连接数据库失败: {exc}")
        return 3

    try:
        stats = import_author_urls(
            conn=conn,
            table=table,
            crawled_rows=crawled_rows,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
        print(
            "[完成] total={total}, existing={existing}, created_or_updated={created_or_updated}, "
            "with_followers={with_followers}, crawl_failed={crawl_failed}{to_create}".format(
                total=stats.get("total", 0),
                existing=stats.get("existing", 0),
                created_or_updated=stats.get("created_or_updated", 0),
                with_followers=stats.get("with_followers", 0),
                crawl_failed=stats.get("crawl_failed", 0),
                to_create=f", to_create={stats.get('to_create', 0)}" if args.dry_run else "",
            )
        )
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"[失败] 导入数据库失败: {exc}")
        return 4
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
