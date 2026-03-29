"""Standalone cleanup worker: expired articles + claim table GC.

Examples:
  python tools/run_cleanup.py
  python tools/run_cleanup.py --loop --interval-minutes 30
  docker compose run --rm cleanup
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.cleanup_job import cleanup_expired_articles


def main() -> int:
    parser = argparse.ArgumentParser(description="Cleanup expired articles and stale claim rows")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run repeatedly at --interval-minutes (default: from CLEANUP_INTERVAL_MINUTES)",
    )
    parser.add_argument(
        "--interval-minutes",
        type=float,
        default=None,
        help="Minutes between runs when --loop (overrides CLEANUP_INTERVAL_MINUTES)",
    )
    args = parser.parse_args()

    app = create_app(enable_scheduler=False)
    with app.app_context():
        while True:
            try:
                cleanup_expired_articles()
            except Exception:
                app.logger.exception("cleanup run failed")
            if not args.loop:
                break
            interval = args.interval_minutes
            if interval is None:
                interval = float(app.config.get("CLEANUP_INTERVAL_MINUTES", 30))
            time.sleep(max(5.0, float(interval) * 60.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
