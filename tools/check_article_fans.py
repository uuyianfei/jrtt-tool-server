import argparse
import json
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


INFO_API_URL = "https://m.toutiao.com/i{gid}/info/"
USER_PROFILE_API_URL = "https://www.toutiao.com/api/pc/user/profile?user_id={uid}"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.toutiao.com/",
}


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


def parse_number(text: str) -> int:
    if not text:
        return 0
    raw = str(text).strip()
    try:
        if "\u4e07" in raw:
            return int(float(raw.replace("\u4e07", "")) * 10000)
        if "\u4ebf" in raw:
            return int(float(raw.replace("\u4ebf", "")) * 100000000)
        cleaned = re.sub(r"[^\d.]", "", raw)
        return int(float(cleaned)) if cleaned else 0
    except ValueError:
        return 0


def extract_gid(article_url: str) -> str:
    m = re.search(r"/article/(\d+)/?", article_url or "")
    return m.group(1) if m else ""


def fetch_info_api(gid: str, timeout: int = 15) -> Dict:
    url = INFO_API_URL.format(gid=gid)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.toutiao.com/",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"info api not success: {json.dumps(body, ensure_ascii=False)[:300]}")
    return body.get("data") or {}


def fetch_article_html(article_url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.toutiao.com/",
    }
    resp = requests.get(article_url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.text or ""


def extract_candidate_uids_from_article_html(html_text: str) -> List[str]:
    if not html_text:
        return []
    candidates: List[str] = []
    patterns = [
        r"/c/user/token/(\d+)/",
        r'"creator_uid"\s*:\s*"?(\\?\d+)"?',
        r'"media_user"\s*:\s*\{.{0,600}?"id"\s*:\s*"?(\\?\d+)"?',
        r'"user_info"\s*:\s*\{.{0,600}?"user_id"\s*:\s*"?(\\?\d+)"?',
    ]
    for pattern in patterns:
        for m in re.findall(pattern, html_text, re.IGNORECASE | re.DOTALL):
            uid = str(m).replace("\\", "").strip()
            if uid.isdigit():
                candidates.append(uid)
    deduped: List[str] = []
    seen = set()
    for uid in candidates:
        if uid in seen:
            continue
        seen.add(uid)
        deduped.append(uid)
    return deduped


def extract_real_author_token_from_article_html(html_text: str, gid: str) -> str:
    if not html_text:
        return ""

    if gid:
        patterns = [
            rf"https?://www\.toutiao\.com/c/user/token/([A-Za-z0-9._-]+)/\?[^\"'\s>]*entrance_gid={re.escape(gid)}[^\"'\s>]*",
            rf"https?://www\.toutiao\.com/c/user/token/([A-Za-z0-9._-]+)/\?[^\"'\s>]*source=tuwen_detail[^\"'\s>]*",
            rf"/c/user/token/([A-Za-z0-9._-]+)/\?[^\"'\s>]*entrance_gid={re.escape(gid)}[^\"'\s>]*",
        ]
        for pattern in patterns:
            m = re.search(pattern, html_text, re.IGNORECASE)
            if m:
                return (m.group(1) or "").strip()

    m = re.search(r"/c/user/token/([A-Za-z0-9._-]+)/", html_text, re.IGNORECASE)
    if m:
        return (m.group(1) or "").strip()
    return ""


def fetch_user_profile_api(uid: str, timeout: int = 15) -> Dict:
    """PC user profile API - pure HTTP, no Selenium."""
    url = USER_PROFILE_API_URL.format(uid=uid)
    headers = {**_DEFAULT_HEADERS, "Accept": "application/json, text/plain, */*"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or {}
        return {
            "ok": True,
            "raw": data,
            "followers": int(data.get("follower_count") or data.get("fans_count") or 0),
            "name": str(data.get("name") or data.get("screen_name") or ""),
            "error": "",
        }
    except Exception as exc:
        return {"ok": False, "raw": {}, "followers": 0, "name": "", "error": str(exc)[:300]}


def fetch_followers_from_homepage_html(token: str, timeout: int = 15) -> Dict:
    """Fetch author homepage HTML via HTTP and extract follower count from SSR data."""
    url = f"https://www.toutiao.com/c/user/token/{token}/"
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text or ""

        for pattern in [
            r'<script[^>]*>\s*window\.__INITIAL_STATE__\s*=\s*({.+?})\s*;?\s*</script>',
            r'<script[^>]*>\s*window\._SSR_DATA\s*=\s*({.+?})\s*;?\s*</script>',
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>({.+?})</script>',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    flat = json.dumps(data)
                    fm = re.search(r'"follower_count"\s*:\s*(\d+)', flat)
                    if fm:
                        return {
                            "ok": True,
                            "followers": int(fm.group(1)),
                            "source": "ssr_json",
                            "error": "",
                        }
                except (json.JSONDecodeError, ValueError):
                    pass

        for pattern in [
            r'"follower_count"\s*:\s*(\d+)',
            r'"fans_count"\s*:\s*(\d+)',
        ]:
            m = re.search(pattern, html)
            if m:
                val = int(m.group(1))
                if val > 0:
                    return {"ok": True, "followers": val, "source": "html_json_field", "error": ""}

        soup = BeautifulSoup(html, "html.parser")
        full_text = soup.get_text(" ", strip=True)
        fans = extract_fans_from_text(full_text)
        if fans > 0:
            return {"ok": True, "followers": fans, "source": "html_text", "error": ""}

        return {"ok": False, "followers": 0, "source": "", "error": "no follower data found in homepage HTML"}
    except Exception as exc:
        return {"ok": False, "followers": 0, "source": "", "error": str(exc)[:300]}


def build_author_info(info: Dict) -> Dict:
    media_user = info.get("media_user") or {}
    media_uid = str(media_user.get("id") or "").strip()
    creator_uid = str(info.get("creator_uid") or "").strip()
    uid = media_uid or creator_uid
    author_url = f"https://www.toutiao.com/c/user/token/{uid}/" if uid else ""
    api_followers = int(media_user.get("follower_count") or info.get("follower_count") or 0)
    return {
        "source": str(info.get("source") or "").strip(),
        "media_user_name": str(media_user.get("name") or "").strip(),
        "media_user_id": media_uid,
        "creator_uid": creator_uid,
        "author_url": author_url,
        "api_followers": api_followers,
        "api_followers_from_media_user": int(media_user.get("follower_count") or 0),
        "api_followers_from_info": int(info.get("follower_count") or 0),
    }


def get_author_byline_token(article_url: str, headless: bool = True, wait_seconds: float = 2.0) -> Dict:
    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=zh-CN")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(article_url)
        time.sleep(max(1.0, float(wait_seconds)))
        gid = extract_gid(article_url)

        token = ""
        href = ""
        try:
            href = (
                driver.execute_script(
                    """
                    const cands = Array.from(document.querySelectorAll('a[href*="/c/user/token/"]'));
                    for (const a of cands) {
                      const h = String(a.getAttribute('href') || '');
                      if (!h) continue;
                      if (h.includes('entrance_gid=') || h.includes('source=tuwen_detail')) {
                        return h;
                      }
                    }
                    return cands.length ? String(cands[0].getAttribute('href') || '') : '';
                    """
                )
                or ""
            )
        except Exception:
            href = ""

        if href:
            m = re.search(r"/c/user/token/([A-Za-z0-9._-]+)/", href)
            if m:
                token = (m.group(1) or "").strip()

        if not token:
            html_text = driver.page_source or ""
            token = extract_real_author_token_from_article_html(html_text, gid=gid)

        if token:
            return {
                "ok": True,
                "token": token,
                "author_url": f"https://www.toutiao.com/c/user/token/{token}/",
                "error": "",
            }
        return {"ok": False, "token": "", "author_url": "", "error": "cannot locate author token on article page"}
    except Exception as exc:
        return {"ok": False, "token": "", "author_url": "", "error": str(exc)}
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass


def extract_fans_from_text(text: str) -> int:
    if not text:
        return 0
    cleaned = re.sub(r"\s+", "", text)
    m = re.search(r"(\d+(?:\.\d+)?)([" + "\u4e07\u4ebf" + r"]?)\u7c89\u4e1d", cleaned)
    if m:
        return parse_number((m.group(1) or "") + (m.group(2) or ""))
    m = re.search(r"\u7c89\u4e1d(\d+(?:\.\d+)?)([" + "\u4e07\u4ebf" + r"]?)", cleaned)
    if m:
        return parse_number((m.group(1) or "") + (m.group(2) or ""))
    return 0


def get_fans_from_author_page(author_url: str, headless: bool = True, wait_seconds: float = 1.8) -> Dict:
    if not author_url:
        return {"fans": 0, "raw_text": "", "ok": False, "error": "author_url is empty"}

    options = webdriver.ChromeOptions()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=zh-CN")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get(author_url)
        time.sleep(max(0.8, float(wait_seconds)))
        soup = BeautifulSoup(driver.page_source or "", "html.parser")

        stat_items = soup.select(".relation-stat .stat-item, button.stat-item, .stat-item")
        for item in stat_items:
            aria = item.get("aria-label", "")
            text = item.get_text(strip=True)
            if "\u7c89\u4e1d" in aria or "\u7c89\u4e1d" in text:
                num_span = item.find("span", class_="num")
                if num_span:
                    fans = parse_number(num_span.get_text(strip=True))
                    if fans > 0:
                        return {"fans": fans, "raw_text": text or aria, "ok": True, "error": ""}
                fans = extract_fans_from_text(text or aria)
                if fans > 0:
                    return {"fans": fans, "raw_text": text or aria, "ok": True, "error": ""}

        try:
            stat_texts = driver.execute_script(
                """
                const nodes = Array.from(document.querySelectorAll('.relation-stat .stat-item, button.stat-item, .stat-item'));
                return nodes.map(n => (n.innerText || '').trim()).filter(Boolean);
                """
            ) or []
            for text in stat_texts:
                fans = extract_fans_from_text(str(text))
                if fans > 0:
                    return {"fans": fans, "raw_text": str(text), "ok": True, "error": ""}
        except Exception:
            pass

        full_text = soup.get_text(" ", strip=True)
        fans = extract_fans_from_text(full_text)
        if fans > 0:
            return {"fans": fans, "raw_text": "from_full_text", "ok": True, "error": ""}
        try:
            body_text = driver.execute_script("return (document.body && document.body.innerText) || '';") or ""
            fans2 = extract_fans_from_text(str(body_text))
            if fans2 > 0:
                return {"fans": fans2, "raw_text": "from_live_body_text", "ok": True, "error": ""}
        except Exception:
            pass
        return {"fans": 0, "raw_text": "", "ok": False, "error": "fans not found on author page"}
    except Exception as exc:
        return {"fans": 0, "raw_text": "", "ok": False, "error": str(exc)}
    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Check followers for a Toutiao article.")
    parser.add_argument(
        "--url",
        default="https://www.toutiao.com/article/7618669172603568675/",
        help="Toutiao article URL",
    )
    parser.add_argument("--headless", action="store_true", default=True, help="Run Chrome headless")
    parser.add_argument("--no-headless", action="store_false", dest="headless", help="Run Chrome with UI")
    parser.add_argument("--wait", type=float, default=1.8, help="Wait seconds after loading author page")
    parser.add_argument("--skip-selenium", action="store_true", default=False, help="Skip Selenium, HTTP only")
    args = parser.parse_args()

    article_url = normalize_article_url(args.url)
    gid = extract_gid(article_url)
    if not gid:
        raise SystemExit(f"invalid article url: {article_url}")

    print(f"[1] article_url: {article_url}")
    print(f"[2] group_id: {gid}")

    # --- Info API ---
    info = fetch_info_api(gid)
    author_info = build_author_info(info)
    media_user_raw = info.get("media_user") or {}

    print("[3] info_api (parsed):")
    print(
        json.dumps(
            {
                "title": info.get("title"),
                "source": author_info["source"],
                "media_user_name": author_info["media_user_name"],
                "media_user_id": author_info["media_user_id"],
                "creator_uid": author_info["creator_uid"],
                "author_url": author_info["author_url"],
                "api_followers": author_info["api_followers"],
                "api_followers_from_media_user": author_info["api_followers_from_media_user"],
                "api_followers_from_info": author_info["api_followers_from_info"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    print("[3.1] info_api raw media_user (full):")
    print(json.dumps(media_user_raw, ensure_ascii=False, indent=2))

    root_follower_fields = {
        k: v for k, v in info.items()
        if any(kw in k.lower() for kw in ["follow", "fan", "subscribe", "user"])
        and k != "media_user"
    }
    print("[3.2] info_api root follower-related fields:")
    print(json.dumps(root_follower_fields, ensure_ascii=False, indent=2))

    # --- HTTP-only follower verification ---
    print("\n" + "=" * 50)
    print("=== HTTP-ONLY FOLLOWER VERIFICATION (no Selenium) ===")
    print("=" * 50)

    candidate_tokens = []
    media_uid = author_info["media_user_id"]
    creator_uid = author_info["creator_uid"]
    if media_uid:
        candidate_tokens.append(("media_user_id", media_uid))
    if creator_uid and creator_uid != media_uid:
        candidate_tokens.append(("creator_uid", creator_uid))

    for label, uid in candidate_tokens:
        print(f"\n[4.A] user_profile_api ({label}={uid}):")
        profile_result = fetch_user_profile_api(uid)
        print(json.dumps({
            "ok": profile_result["ok"],
            "followers": profile_result["followers"],
            "name": profile_result["name"],
            "error": profile_result["error"],
        }, ensure_ascii=False, indent=2))
        if profile_result["ok"] and profile_result["raw"]:
            raw_follower_fields = {
                k: v for k, v in profile_result["raw"].items()
                if any(kw in k.lower() for kw in ["follow", "fan", "subscribe", "count"])
            }
            if raw_follower_fields:
                print(f"  profile raw follower fields: {json.dumps(raw_follower_fields, ensure_ascii=False)}")

    for label, token in candidate_tokens:
        print(f"\n[4.B] homepage_html ({label}={token}):")
        html_result = fetch_followers_from_homepage_html(token)
        print(json.dumps(html_result, ensure_ascii=False, indent=2))

    article_html = fetch_article_html(article_url)
    html_uids = extract_candidate_uids_from_article_html(article_html)
    real_token = extract_real_author_token_from_article_html(article_html, gid)
    if real_token and real_token not in [t for _, t in candidate_tokens]:
        print(f"\n[4.C] homepage_html (real_token={real_token}):")
        html_result_c = fetch_followers_from_homepage_html(real_token)
        print(json.dumps(html_result_c, ensure_ascii=False, indent=2))
        if real_token.isdigit():
            print(f"[4.C] user_profile_api (real_token={real_token}):")
            profile_result_c = fetch_user_profile_api(real_token)
            print(json.dumps({
                "ok": profile_result_c["ok"],
                "followers": profile_result_c["followers"],
                "name": profile_result_c["name"],
                "error": profile_result_c["error"],
            }, ensure_ascii=False, indent=2))

    print(f"\n[5] article_html candidate_uids: {json.dumps(html_uids)}")
    print(f"    real_author_token_from_html: {real_token}")

    if args.skip_selenium:
        print("\n[SKIP] Selenium steps skipped (--skip-selenium)")
        api_fans = int(author_info["api_followers"] or 0)
        print(f"\n[SUMMARY] info_api follower_count = {api_fans}")
        print("  Compare [4.A]/[4.B]/[4.C] followers with real profile page fans count")
        return

    # --- Selenium verification ---
    print("\n" + "=" * 50)
    print("=== SELENIUM VERIFICATION ===")
    print("=" * 50)

    real_author = get_author_byline_token(
        article_url,
        headless=bool(args.headless),
        wait_seconds=float(args.wait),
    )
    print("\n[6] real_author_from_detail_page:")
    print(json.dumps(real_author, ensure_ascii=False, indent=2))

    all_candidate_uids: List[str] = []
    for uid in [author_info["media_user_id"], author_info["creator_uid"], *html_uids]:
        uid = str(uid or "").strip()
        if uid and uid.isdigit() and uid not in all_candidate_uids:
            all_candidate_uids.append(uid)

    print(f"[7] all candidate_uids: {json.dumps(all_candidate_uids)}")

    page_results = []
    for uid in all_candidate_uids:
        url = f"https://www.toutiao.com/c/user/token/{uid}/"
        result = get_fans_from_author_page(
            url,
            headless=bool(args.headless),
            wait_seconds=float(args.wait),
        )
        page_results.append({"uid": uid, "author_url": url, **result})

    if real_author.get("author_url"):
        byline_result = get_fans_from_author_page(
            str(real_author.get("author_url")),
            headless=bool(args.headless),
            wait_seconds=float(args.wait),
        )
        page_results.append(
            {
                "uid": str(real_author.get("token") or ""),
                "author_url": str(real_author.get("author_url") or ""),
                "is_real_author_byline": True,
                **byline_result,
            }
        )

    print("\n[8] selenium author_page_results:")
    print(json.dumps(page_results, ensure_ascii=False, indent=2))

    api_fans = int(author_info["api_followers"] or 0)

    db_like_url = author_info["author_url"]
    db_like_fans: Optional[int] = None
    real_author_fans: Optional[int] = None
    for row in page_results:
        if str(row.get("author_url") or "") == str(db_like_url or ""):
            db_like_fans = int(row.get("fans") or 0)
        if bool(row.get("is_real_author_byline")):
            real_author_fans = int(row.get("fans") or 0)

    reason = (
        "same author (API uid == detail page author)"
        if (db_like_url and real_author.get("author_url") and str(db_like_url) == str(real_author.get("author_url")))
        else "MISMATCH (API author != detail page author)"
    )

    verdict = "INFO API RELIABLE"
    if real_author_fans and real_author_fans > 0:
        deviation = abs(api_fans - real_author_fans) / max(real_author_fans, 1)
        if deviation >= 0.1:
            verdict = f"INFO API UNRELIABLE! api={api_fans} vs real={real_author_fans} (deviation={deviation:.1%})"

    print(
        "\n[9] conclusion: "
        + json.dumps(
            {
                "reason": reason,
                "db_like_author_url": db_like_url,
                "db_like_author_page_fans": db_like_fans,
                "real_author_url": real_author.get("author_url"),
                "real_author_page_fans": real_author_fans,
                "api_followers_from_info_api": api_fans,
                "VERDICT": verdict,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
