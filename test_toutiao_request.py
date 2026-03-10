import json
import re
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


def normalize_article_url(url: str) -> str:
    raw = (url or "").strip()
    parts = urlsplit(raw)
    scheme = parts.scheme or "https"
    netloc = parts.netloc or "www.toutiao.com"
    path = parts.path or ""
    m = re.search(r"/article/(\d+)/?", path)
    if m:
        path = f"/article/{m.group(1)}/"
    return urlunsplit((scheme, netloc, path, "", ""))


def parse_from_html(html_text: str):
    soup = BeautifulSoup(html_text, "html.parser")

    title = ""
    h1 = soup.select_one("h1")
    if h1:
        title = h1.get_text(strip=True)
    if not title and soup.title:
        title = soup.title.get_text(strip=True)

    article_html = ""
    selectors = [
        "article.syl-article-base",
        ".syl-article-base",
        ".syl-page-article",
        ".article-content",
        ".tt-article-content",
        "article",
    ]
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            article_html = str(node)
            break

    article_text = ""
    if article_html:
        article_text = BeautifulSoup(article_html, "html.parser").get_text("\n", strip=True)

    like_count = 0
    comment_count = 0

    m = re.search(r'"digg_count"\s*:\s*(\d+)', html_text, re.IGNORECASE)
    if m:
        like_count = int(m.group(1))
    m = re.search(r'"comment_count"\s*:\s*(\d+)', html_text, re.IGNORECASE)
    if m:
        comment_count = int(m.group(1))

    # JSON-LD 兜底正文
    if not article_text:
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
                if isinstance(block, dict) and isinstance(block.get("articleBody"), str):
                    article_text = block["articleBody"].strip()
                    break
            if article_text:
                break

    return {
        "title": title,
        "like_count": like_count,
        "comment_count": comment_count,
        "article_text_len": len(article_text),
        "article_text_preview": article_text[:300],
        "has_article_html": bool(article_html),
    }


def main():
    url = "https://www.toutiao.com/article/7613289863035306534/"
    url = normalize_article_url(url)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    parsed = parse_from_html(resp.text)
    result = {
        "url": url,
        "status_code": resp.status_code,
        "content_type": resp.headers.get("Content-Type", ""),
        **parsed,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
