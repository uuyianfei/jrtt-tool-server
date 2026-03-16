"""Aggregate fast crawler metrics from logs.

Usage examples:
  docker compose logs --since=10m fast-crawler | python tools/fast_crawler_metrics.py --window-minutes 10
  python tools/fast_crawler_metrics.py --log-file fast.log --window-minutes 30
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

DONE_RE = re.compile(
    r"fast crawl done: upserted=(?P<upserted>\d+)\s+elapsed=(?P<elapsed>[0-9.]+)s\s+feed_items=(?P<feed>\d+)\s+info_fetched=(?P<info>\d+)"
)
SUMMARY_RE = re.compile(
    r"upsert summary .*?created=(?P<created>\d+)\s+updated=(?P<updated>\d+).*?errors=(?P<errors>\d+)"
)
SUMMARY_SKIP_RE = re.compile(r"(skip_[a-z_]+)=(\d+)")
RATE_LIMIT_RE = re.compile(r"rate limited gid=")


def _iter_lines(log_file: str) -> Iterable[str]:
    if log_file:
        path = Path(log_file)
        if not path.exists():
            raise FileNotFoundError(f"log file not found: {path}")
        with path.open("r", encoding="utf-8", errors="ignore") as fp:
            yield from fp
        return
    yield from sys.stdin


def _safe_div(value: float, divisor: float) -> float:
    if divisor <= 0:
        return 0.0
    return value / divisor


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize fast crawler throughput from logs")
    parser.add_argument("--window-minutes", type=float, required=True, help="Observation window size")
    parser.add_argument("--log-file", type=str, default="", help="Optional log file path; otherwise read stdin")
    args = parser.parse_args()

    totals = Counter()
    skip_totals = Counter()
    rounds = 0
    summary_rounds = 0

    for line in _iter_lines(args.log_file):
        m_done = DONE_RE.search(line)
        if m_done:
            rounds += 1
            totals["upserted"] += int(m_done.group("upserted"))
            totals["feed_items"] += int(m_done.group("feed"))
            totals["info_fetched"] += int(m_done.group("info"))
            totals["elapsed"] += float(m_done.group("elapsed"))
            continue

        m_summary = SUMMARY_RE.search(line)
        if m_summary:
            summary_rounds += 1
            totals["created"] += int(m_summary.group("created"))
            totals["updated"] += int(m_summary.group("updated"))
            totals["errors"] += int(m_summary.group("errors"))
            for key, val in SUMMARY_SKIP_RE.findall(line):
                skip_totals[key] += int(val)
            continue

        if RATE_LIMIT_RE.search(line):
            totals["rate_limited"] += 1

    window_minutes = max(0.0, float(args.window_minutes))
    print("=== fast crawler metrics ===")
    print(f"window_minutes: {window_minutes:.1f}")
    print(f"crawl_rounds: {rounds}")
    print(f"summary_rounds: {summary_rounds}")
    print(f"created: {totals['created']}")
    print(f"updated: {totals['updated']}")
    print(f"upserted: {totals['upserted']}")
    print(f"feed_items: {totals['feed_items']}")
    print(f"info_fetched: {totals['info_fetched']}")
    print(f"errors: {totals['errors']}")
    print(f"rate_limited: {totals['rate_limited']}")
    print(f"created_per_min: {_safe_div(totals['created'], window_minutes):.2f}")
    print(f"upserted_per_min: {_safe_div(totals['upserted'], window_minutes):.2f}")
    print(f"avg_elapsed_seconds: {_safe_div(totals['elapsed'], rounds):.2f}")
    if totals["upserted"] > 0:
        print(f"created_ratio: {totals['created'] / totals['upserted']:.3f}")
    if totals["feed_items"] > 0:
        print(f"info_fetch_ratio: {totals['info_fetched'] / totals['feed_items']:.3f}")

    if skip_totals:
        print("skip_totals:")
        for key in sorted(skip_totals.keys()):
            print(f"  {key}: {skip_totals[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
