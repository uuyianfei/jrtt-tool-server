import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

import pymysql
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymysql.connections import Connection
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


def log(message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import authors into author_sources with crawl enrichment.")
    parser.add_argument("--file", default="good_authors.json", help="Path to source JSON file.")
    parser.add_argument("--batch-size", type=int, default=100, help="DB commit batch size.")
    parser.add_argument("--dry-run", action="store_true", help="Only parse/crawl, do not write DB.")
    parser.add_argument("--table", default="author_sources", help="Target table name.")
    parser.add_argument("--headless", action="store_true", default=False, help="Run Chrome in headless mode.")
    parser.add_argument("--no-headless", action="store_false", dest="headless", help="Run Chrome with UI.")
    parser.add_argument(
        "--crawl-delay",
        type=float,
        default=1.2,
        help="Sleep seconds after opening each author page.",
    )
    parser.add_argument("--log-every", type=int, default=1, help="Print crawl progress every N authors.")
    parser.add_argument("--db-flush-size", type=int, default=10, help="Flush to DB every N crawled authors.")
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


def normalize_author_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = parts.scheme or "https"
    netloc = parts.netloc or "www.toutiao.com"
    path = parts.path or ""
    # token 链接保留路径，去掉 query/fragment，避免 source=feed 干扰跳转行为
    return urlunsplit((scheme, netloc, path, "", ""))


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
    def __init__(self, headless: bool = False):
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
        log(f"[启动] 初始化浏览器驱动 headless={self.headless}")
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
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        ua = (os.getenv("CRAWL_USER_AGENT", "") or "").strip()
        if ua:
            options.add_argument(f"user-agent={ua}")

        driver_path = self._resolve_driver_path()
        if driver_path:
            log(f"[驱动] 使用本地 chromedriver: {driver_path}")
            service = Service(driver_path)
        else:
            log("[驱动] 未找到本地 chromedriver，尝试 webdriver-manager 下载（首次可能较慢）")
            service = Service(ChromeDriverManager().install())
            log(f"[驱动] webdriver-manager 下载完成: {service.path}")
        driver = webdriver.Chrome(service=service, options=options)
        page_load_timeout = max(10, int(os.getenv("IMPORT_PAGE_LOAD_TIMEOUT_SECONDS", "25")))
        driver.set_page_load_timeout(page_load_timeout)
        log(f"[启动] 浏览器驱动就绪 page_load_timeout={page_load_timeout}s")
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": """
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh'] });
                    window.chrome = window.chrome || { runtime: {} };
                    """
                },
            )
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd(
                "Network.setExtraHTTPHeaders",
                {
                    "headers": {
                        "Referer": "https://www.toutiao.com/",
                    }
                },
            )
        except Exception:
            # 非关键增强，失败不影响主流程
            pass
        return driver

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
                fans = self._extract_fans_from_text(item_text)
                if fans > 0:
                    return fans
                m = re.search(r"(\d+(?:\.\d+)?)([万亿]?)", item_text)
                if m:
                    fans = parse_number((m.group(1) or "") + (m.group(2) or ""))
                    if fans > 0:
                        return fans

        full_text = soup.get_text(" ", strip=True)
        fans = self._extract_fans_from_text(full_text)
        if fans > 0:
            return fans
        m = re.search(r"粉丝\s*(\d+(?:\.\d+)?)([万亿]?)", full_text)
        if m:
            return parse_number((m.group(1) or "") + (m.group(2) or ""))
        json_patterns = [
            r'"followers(?:_count)?"\s*:\s*(\d+)',
            r'"fans(?:_count)?"\s*:\s*(\d+)',
        ]
        html = str(soup)
        for pattern in json_patterns:
            mm = re.search(pattern, html, re.IGNORECASE)
            if mm:
                return int(mm.group(1))
        return 0

    def _extract_fans_from_live_dom(self) -> int:
        """
        兜底：直接从浏览器实时 DOM 提取粉丝文案，避免 page_source 时机过早导致解析不到。
        """
        try:
            texts = self.driver.execute_script(
                """
                const nodes = Array.from(document.querySelectorAll('.relation-stat .stat-item, button.stat-item, .stat-item'));
                return nodes.map(n => (n.innerText || '').trim()).filter(Boolean);
                """
            ) or []
            for text in texts:
                fans = self._extract_fans_from_text(str(text))
                if fans > 0:
                    return fans
            return 0
        except Exception:
            return 0

    def _extract_fans_from_body_text(self) -> int:
        """
        终极兜底：从 document.body.innerText 全文匹配“粉丝”相关数字。
        """
        try:
            text = self.driver.execute_script("return (document.body && document.body.innerText) || '';") or ""
            text = str(text)
            fans = self._extract_fans_from_text(text)
            if fans > 0:
                return fans
            m = re.search(r"粉丝\s*(\d+(?:\.\d+)?)([万亿]?)", re.sub(r"\s+", "", text))
            if m:
                return parse_number((m.group(1) or "") + (m.group(2) or ""))
        except Exception:
            return 0
        return 0

    def _wait_profile_ready(self, timeout_seconds: int = 8):
        """
        等待作者主页统计区渲染，减少首屏未就绪时粉丝数解析为 0 的概率。
        """
        timeout_seconds = max(3, int(timeout_seconds))
        WebDriverWait(self.driver, timeout_seconds).until(
            lambda d: bool(
                d.execute_script(
                    """
                    const nodes = document.querySelectorAll('.relation-stat .stat-item, button.stat-item, .stat-item');
                    if (!nodes || nodes.length === 0) return false;
                    for (const n of nodes) {
                      const t = (n.innerText || '').trim();
                      if (t.includes('粉丝')) return true;
                    }
                    return false;
                    """
                )
            )
        )

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
        normalized_url = normalize_author_url(author_url)
        result: Dict[str, object] = {
            "author_url": normalized_url or author_url,
            "author_name": "",
            "followers": 0,
            "error": "",
        }
        try:
            self.driver.get(normalized_url or author_url)
            try:
                self._wait_profile_ready(timeout_seconds=int(os.getenv("IMPORT_PROFILE_READY_TIMEOUT_SECONDS", "8")))
            except Exception:
                # 首轮等待失败不直接中断，继续走后续解析与重试兜底
                pass
            time.sleep(max(0.2, float(crawl_delay)))
            soup = BeautifulSoup(self.driver.page_source or "", "html.parser")
            result["author_name"] = self._extract_name(soup)
            result["followers"] = int(self._extract_fans(soup))
            if int(result["followers"] or 0) <= 0:
                live_dom_fans = int(self._extract_fans_from_live_dom() or 0)
                if live_dom_fans > 0:
                    result["followers"] = live_dom_fans
            if int(result["followers"] or 0) <= 0:
                body_text_fans = int(self._extract_fans_from_body_text() or 0)
                if body_text_fans > 0:
                    result["followers"] = body_text_fans
            if int(result["followers"] or 0) <= 0:
                # 兼容动态渲染慢或首屏未就绪，做一次轻量重试
                try:
                    self.driver.refresh()
                    self._wait_profile_ready(timeout_seconds=int(os.getenv("IMPORT_PROFILE_READY_TIMEOUT_SECONDS", "8")))
                except Exception:
                    pass
                time.sleep(max(0.6, float(crawl_delay)))
                soup_retry = BeautifulSoup(self.driver.page_source or "", "html.parser")
                retry_name = self._extract_name(soup_retry)
                retry_followers = int(self._extract_fans(soup_retry))
                if retry_followers <= 0:
                    retry_followers = int(self._extract_fans_from_live_dom() or 0)
                if retry_followers <= 0:
                    retry_followers = int(self._extract_fans_from_body_text() or 0)
                if retry_name:
                    result["author_name"] = retry_name
                if retry_followers > 0:
                    result["followers"] = retry_followers
            if int(result["followers"] or 0) <= 0:
                current_url = ""
                page_title = ""
                try:
                    current_url = str(self.driver.current_url or "")
                except Exception:
                    pass
                try:
                    page_title = str(self.driver.title or "")
                except Exception:
                    pass
                result["error"] = (
                    f"followers_not_found current_url={current_url[:180]} title={page_title[:80]}"
                )
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
    log(
        "[启动] import_author_sources.py file={file} headless={headless} dry_run={dry_run} batch_size={batch}".format(
            file=source_file,
            headless=args.headless,
            dry_run=args.dry_run,
            batch=args.batch_size,
        )
    )
    try:
        table = validate_table_name(args.table)
    except Exception as exc:
        log(f"[失败] 表名不合法: {exc}")
        return 1

    try:
        log(f"[读取] 开始读取作者链接: {source_file}")
        urls = load_author_urls(source_file)
    except Exception as exc:
        log(f"[失败] 读取 JSON 失败: {exc}")
        return 1
    if not urls:
        log("[完成] 无可导入作者链接")
        return 0
    log(f"[读取] 作者链接加载完成 total={len(urls)}")

    cfg = get_mysql_config()
    log(
        "[数据库] 开始连接 MySQL host={host} port={port} db={db} user={user}".format(
            host=cfg["host"],
            port=cfg["port"],
            db=cfg["database"],
            user=cfg["user"],
        )
    )
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
        log(f"[失败] 连接数据库失败: {exc}")
        return 3

    try:
        with conn.cursor() as cur:
            existing_count = count_existing(cur, table, urls)
        log(f"[数据库] 预检查完成 existing={existing_count}")
    except Exception as exc:
        conn.close()
        log(f"[失败] 数据库预检查失败: {exc}")
        return 4

    crawler = None
    with_followers = 0
    crawl_failed = 0
    created_or_updated = 0
    pending_db_rows: List[Tuple] = []
    flush_size = max(1, int(args.db_flush_size))
    try:
        log("[爬取] 开始初始化爬虫")
        crawler = AuthorMetaCrawler(headless=args.headless)
        total = len(urls)
        log_every = max(1, int(args.log_every))
        log(
            f"[爬取] 开始抓取作者元信息 total={total} crawl_delay={args.crawl_delay}s db_flush_size={flush_size}"
        )
        for idx, url in enumerate(urls, start=1):
            row = crawler.crawl_one(author_url=url, crawl_delay=args.crawl_delay)
            if int(row.get("followers") or 0) > 0:
                with_followers += 1
            if str(row.get("error") or ""):
                crawl_failed += 1

            if not args.dry_run:
                pending_db_rows.append(
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
                if len(pending_db_rows) >= flush_size:
                    upsert_author_rows(
                        conn=conn,
                        table=table,
                        rows=pending_db_rows,
                        batch_size=max(1, min(flush_size, int(args.batch_size))),
                    )
                    created_or_updated += len(pending_db_rows)
                    log(
                        "[入库进度] flushed={flushed} total={total}".format(
                            flushed=created_or_updated,
                            total=total,
                        )
                    )
                    pending_db_rows = []

            if idx == 1 or idx % log_every == 0 or idx == total:
                log(
                    "[爬取进度] {cur}/{total} followers_ok={ok} failed={failed}".format(
                        cur=idx,
                        total=total,
                        ok=with_followers,
                        failed=crawl_failed,
                    )
                )

        if (not args.dry_run) and pending_db_rows:
            upsert_author_rows(
                conn=conn,
                table=table,
                rows=pending_db_rows,
                batch_size=max(1, min(flush_size, int(args.batch_size))),
            )
            created_or_updated += len(pending_db_rows)
            log(
                "[入库进度] flushed={flushed} total={total}".format(
                    flushed=created_or_updated,
                    total=total,
                )
            )
    except Exception as exc:
        log(f"[失败] 爬取作者信息失败: {exc}")
        return 2
    finally:
        if crawler:
            crawler.close()
            log("[爬取] 浏览器已关闭")

    try:
        stats = {
            "total": len(urls),
            "existing": existing_count,
            "created_or_updated": created_or_updated if not args.dry_run else 0,
            "with_followers": with_followers,
            "crawl_failed": crawl_failed,
        }
        if args.dry_run:
            stats["to_create"] = max(0, int(stats["total"]) - int(stats["existing"]))
        log(
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
        log(f"[失败] 导入数据库失败: {exc}")
        return 4
    finally:
        conn.close()
        log("[数据库] 连接已关闭")


if __name__ == "__main__":
    raise SystemExit(main())
