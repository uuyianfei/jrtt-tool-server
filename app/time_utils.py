from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def cn_now() -> datetime:
    """Return timezone-aware current time in Asia/Shanghai."""
    return datetime.now(SHANGHAI_TZ)


def cn_now_naive() -> datetime:
    """Return naive datetime in Asia/Shanghai local clock, suitable for DB naive DateTime."""
    return cn_now().replace(tzinfo=None)
