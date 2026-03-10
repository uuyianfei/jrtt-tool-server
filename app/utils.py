import hashlib
import re
from datetime import datetime, timedelta

from flask import jsonify

from .time_utils import SHANGHAI_TZ, cn_now_naive

def success_response(data=None, message="ok"):
    if data is None:
        data = {}
    return jsonify({"code": 0, "message": message, "data": data}), 200


def error_response(code, message, data=None):
    if data is None:
        data = {}
    return jsonify({"code": code, "message": message, "data": data}), 200


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


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def format_compact_number(num: int) -> str:
    if num >= 10000:
        value = num / 10000.0
        text = f"{value:.1f}".rstrip("0").rstrip(".")
        return f"{text}w"
    return str(num)


def parse_hours_ago(time_str: str):
    if not time_str or time_str == "未知时间":
        return None
    text = time_str.strip()
    now = cn_now_naive()
    try:
        if "小时前" in text:
            m = re.search(r"(\d+)\s*小时前", text)
            return float(m.group(1)) if m else None
        if "分钟前" in text:
            m = re.search(r"(\d+)\s*分钟前", text)
            return float(m.group(1)) / 60 if m else None
        if "天前" in text:
            m = re.search(r"(\d+)\s*天前", text)
            return float(m.group(1)) * 24 if m else None
        if "今天" in text:
            m = re.search(r"今天\s*(\d+):(\d+)", text)
            if m:
                h, mm = map(int, m.groups())
                dt = now.replace(hour=h, minute=mm, second=0, microsecond=0)
                return max(0.0, (now - dt).total_seconds() / 3600)
            return 0.0
        if "昨天" in text:
            m = re.search(r"昨天\s*(\d+):(\d+)", text)
            if m:
                h, mm = map(int, m.groups())
                dt = now.replace(hour=h, minute=mm, second=0, microsecond=0) - timedelta(days=1)
                return (now - dt).total_seconds() / 3600
            return 24.0
        m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
        if m:
            month, day = map(int, m.groups())
            year = now.year if month <= now.month else now.year - 1
            dt = datetime(year, month, day)
            return (now - dt).total_seconds() / 3600
    except Exception:
        return None
    return None


def parse_publish_datetime(time_text: str):
    """Parse publish datetime text into naive datetime (Asia/Shanghai local clock)."""
    if not time_text:
        return None
    raw = str(time_text).strip()
    if not raw:
        return None

    # ISO8601: 2026-03-10T12:34:56+08:00 / 2026-03-10T04:34:56Z
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        if "T" in iso_candidate and ("+" in iso_candidate or iso_candidate.endswith("00:00")):
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is not None:
                dt = dt.astimezone(SHANGHAI_TZ).replace(tzinfo=None)
            return dt
    except Exception:
        pass

    patterns = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in patterns:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", raw)
    if m:
        year, month, day, hour, minute, second = m.groups()
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second or 0),
        )

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        year, month, day = m.groups()
        return datetime(int(year), int(month), int(day))
    return None
