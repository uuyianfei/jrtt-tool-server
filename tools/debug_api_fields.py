"""
Diagnostic: dump all user-related fields from Feed API and Info API
to find hidden follower data or MS4w tokens.
"""
import json
import re
import sys

import requests

FEED_API_URL = "https://www.toutiao.com/api/pc/feed/"
INFO_API_URL = "https://m.toutiao.com/i{gid}/info/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.toutiao.com/",
}

INTERESTING_KEYWORDS = [
    "follow", "fan", "subscribe", "user", "author", "creator",
    "media", "token", "sec_uid", "uid", "avatar", "screen_name",
    "source", "name", "MS4w",
]


def is_interesting_key(key: str) -> bool:
    k = key.lower()
    return any(kw in k for kw in INTERESTING_KEYWORDS)


def extract_interesting(obj, prefix=""):
    """Recursively extract fields whose key matches interesting keywords."""
    results = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if is_interesting_key(k):
                results[full_key] = v
            if isinstance(v, (dict, list)):
                results.update(extract_interesting(v, full_key))
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:3]):
            results.update(extract_interesting(item, f"{prefix}[{i}]"))
    return results


def check_ms4w_in_values(obj) -> list:
    """Search for any MS4w-style tokens in all string values."""
    found = []
    raw = json.dumps(obj, ensure_ascii=False)
    for m in re.finditer(r"MS4w[A-Za-z0-9._=-]{10,}", raw):
        found.append(m.group(0))
    return list(set(found))


def dump_feed_item(gid: str) -> list:
    """Fetch one page of feed and find the item matching gid. Returns media_urls."""
    resp = requests.get(
        FEED_API_URL,
        params={"category": "__all__", "utm_source": "toutiao", "max_behot_time": "0"},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data") or []
    print(f"[Feed API] fetched {len(items)} items")

    media_urls = []
    for item in items[:3]:
        item_gid = str(item.get("group_id", ""))
        print(f"\n--- Feed item gid={item_gid} title={str(item.get('title',''))[:40]} ---")
        interesting = extract_interesting(item)
        print(json.dumps(interesting, ensure_ascii=False, indent=2))
        mu = item.get("media_url", "")
        if mu:
            media_urls.append(mu)

    target = None
    for item in items:
        if str(item.get("group_id", "")) == gid:
            target = item
            break
    if target:
        print(f"\n\n=== TARGET Feed item gid={gid} ===")
        interesting = extract_interesting(target)
        print(json.dumps(interesting, ensure_ascii=False, indent=2))
        mu = target.get("media_url", "")
        if mu and mu not in media_urls:
            media_urls.append(mu)
    else:
        print(f"\n(target gid={gid} not found in current feed page)")

    return media_urls


def dump_info_api(gid: str):
    """Fetch info API and dump all user-related fields."""
    url = INFO_API_URL.format(gid=gid)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    info = body.get("data") or {}

    print(f"\n=== Info API gid={gid} ===")
    interesting = extract_interesting(info)
    print(json.dumps(interesting, ensure_ascii=False, indent=2))

    ms4w = check_ms4w_in_values(info)
    print(f"MS4w tokens: {ms4w if ms4w else 'NONE'}")

    all_keys = sorted(info.keys())
    print(f"\nAll top-level keys ({len(all_keys)}): {all_keys}")

    media_user = info.get("media_user") or {}
    if media_user:
        mu_keys = sorted(media_user.keys())
        print(f"media_user keys ({len(mu_keys)}): {mu_keys}")


def test_profile_http(media_url_path: str):
    """Fetch an author profile page via HTTP and try to extract follower count."""
    if not media_url_path:
        return
    url = f"https://www.toutiao.com{media_url_path}" if media_url_path.startswith("/") else media_url_path
    print(f"\n{'='*60}")
    print(f"HTTP profile test: {url[:100]}...")
    print(f"{'='*60}")
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "text/html,*/*"}, timeout=15, allow_redirects=True)
        print(f"  status: {resp.status_code}")
        print(f"  final_url: {resp.url[:120]}")
        html = resp.text or ""
        print(f"  html_length: {len(html)}")

        # Check for follower_count in embedded JSON
        for pattern_name, pattern in [
            ("follower_count", r'"follower_count"\s*:\s*"?(\d+)"?'),
            ("fans_count", r'"fans_count"\s*:\s*"?(\d+)"?'),
        ]:
            m = re.search(pattern, html)
            if m:
                print(f"  FOUND {pattern_name} = {m.group(1)}")

        # Check for Chinese fans text
        cleaned = re.sub(r"\s+", "", html)
        m = re.search(r'(\d+(?:\.\d+)?)[\u4e07\u4ebf]?\u7c89\u4e1d', cleaned)
        if m:
            print(f"  FOUND fans text: {m.group(0)}")

        # Check for SSR data
        for ssr_name, ssr_pat in [
            ("__INITIAL_STATE__", r'window\.__INITIAL_STATE__'),
            ("__NEXT_DATA__", r'__NEXT_DATA__'),
            ("_SSR_DATA", r'_SSR_DATA'),
        ]:
            if re.search(ssr_pat, html):
                print(f"  SSR data present: {ssr_name}")

        # Dump all follower/fan values found
        all_follower_matches = re.findall(r'"(?:follower_count|fans_count|followers_count)"\s*:\s*"?(\d+)"?', html)
        if all_follower_matches:
            print(f"  All follower values in HTML: {all_follower_matches}")
        else:
            print("  NO follower values found in HTML")

        # Check redirect to MS4w token
        if "MS4w" in resp.url or "MS4w" in html[:5000]:
            ms4w_match = re.search(r'MS4w[A-Za-z0-9._=-]{10,}', resp.url + html[:5000])
            if ms4w_match:
                print(f"  MS4w token: {ms4w_match.group(0)}")

    except Exception as exc:
        print(f"  ERROR: {exc}")


def main():
    gids = sys.argv[1:] if len(sys.argv) > 1 else [
        "7618782645354283572",
        "7618669172603568675",
    ]
    for gid in gids:
        print(f"\n{'='*60}")
        print(f"Checking gid={gid}")
        print(f"{'='*60}")
        dump_info_api(gid)

    print(f"\n\n{'='*60}")
    print("Feed API sample (first 3 items)")
    print(f"{'='*60}")
    media_urls = dump_feed_item(gids[0] if gids else "")

    # Test HTTP fetch of author profile pages from feed media_url
    if media_urls:
        for mu in media_urls[:2]:
            test_profile_http(mu)


if __name__ == "__main__":
    main()
