"""
Fetch feed articles and print auth_type vs follower_count for each,
to verify whether auth_type != "5" always has accurate follower counts.
"""
import json
import re
import time
import random
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

CHANNELS = [
    "__all__", "news_hot", "news_society", "news_entertainment", "news_tech",
    "news_finance", "news_military", "news_sports", "news_car", "news_health",
    "news_house", "news_education", "news_science", "news_travel", "news_food",
    "news_history", "news_baby", "news_regimen", "news_story", "news_game",
]


def fetch_feed(channel="__all__", max_behot_time=0):
    resp = requests.get(
        FEED_API_URL,
        params={"category": channel, "utm_source": "toutiao", "max_behot_time": str(max_behot_time)},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_info(gid):
    url = INFO_API_URL.format(gid=gid)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        return None
    return body.get("data") or {}


def main():
    seen_gids = set()
    all_items = []

    pages_per_channel = 3
    for channel in CHANNELS:
        max_behot_time = 0
        for page in range(pages_per_channel):
            try:
                data = fetch_feed(channel, max_behot_time)
                items = data.get("data") or []
                min_bt = None
                for item in items:
                    gid = str(item.get("group_id", ""))
                    if gid and gid not in seen_gids and not item.get("is_feed_ad"):
                        seen_gids.add(gid)
                        all_items.append(item)
                    bt = item.get("behot_time", 0)
                    if bt and (min_bt is None or bt < min_bt):
                        min_bt = bt
                if min_bt:
                    max_behot_time = min_bt
                if not data.get("has_more"):
                    break
                time.sleep(0.3)
            except Exception as exc:
                print(f"[WARN] feed channel={channel} page={page} failed: {exc}")
                break

    import os
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_type_fans_report.txt")
    outf = open(out_path, "w", encoding="utf-8")

    def log(line=""):
        print(line)
        outf.write(line + "\n")

    log(f"Collected {len(all_items)} unique feed items\n")
    log(f"{'auth_type':>10} | {'follower_count':>15} | {'source':^20} | {'title':^40} | article_url")
    log("-" * 140)

    stats = {"total": 0, "type_5": 0, "type_other": 0, "type_none": 0, "no_info": 0}

    for item in all_items:
        gid = str(item.get("group_id", ""))
        title = str(item.get("title") or "")[:38]
        source = str(item.get("source") or "")[:18]
        article_url = f"https://www.toutiao.com/article/{gid}/"

        try:
            info = fetch_info(gid)
            if not info:
                stats["no_info"] += 1
                continue
            time.sleep(random.uniform(0.15, 0.35))
        except Exception:
            stats["no_info"] += 1
            continue

        if info.get("group_source") == 5 or (not info.get("content") and info.get("thread")):
            continue
        if info.get("play_url_list"):
            continue
        full_title = (info.get("title") or item.get("title") or "").strip()
        if any(kw in full_title for kw in ["视频", "video", "直播", "live"]):
            continue
        if info.get("video_play_info") or info.get("video_id"):
            continue

        media_user = info.get("media_user") or {}
        follower_count = str(media_user.get("follower_count") or info.get("follower_count") or "0")
        auth_info_obj = media_user.get("user_auth_info") or {}
        auth_type = str(auth_info_obj.get("auth_type", "")) if auth_info_obj else ""
        auth_info_text = str(auth_info_obj.get("auth_info", ""))[:30] if auth_info_obj else ""

        stats["total"] += 1
        if auth_type == "5":
            stats["type_5"] += 1
        elif auth_type:
            stats["type_other"] += 1
        else:
            stats["type_none"] += 1

        tag = ""
        if auth_type == "5":
            tag = "[MEDIA]"
        elif auth_type:
            tag = f"[AUTH-{auth_type}]"
        else:
            tag = "[PERSONAL]"

        log(f"{tag:>10} | {follower_count:>15} | {source:^20} | {title:^40} | {article_url}")

    log(f"\n{'='*60}")
    log(f"Summary: total={stats['total']}, auth_type=5: {stats['type_5']}, "
        f"auth_type=other: {stats['type_other']}, no_auth(personal): {stats['type_none']}, "
        f"no_info: {stats['no_info']}")
    outf.close()
    print(f"\nReport saved to: {out_path}")


if __name__ == "__main__":
    main()
