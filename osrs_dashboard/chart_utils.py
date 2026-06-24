"""
Shared time-bucketing for chart endpoints: keeps recent data points at full
resolution and groups older ones into coarser buckets, so charts stay legible
as snapshot tables (scanned every few minutes) grow indefinitely.
"""
from datetime import datetime, timedelta
from typing import Optional


def bucket_key(dt: datetime, idx: int, now: Optional[datetime] = None):
    """
    Return a grouping key for a timestamp:
      - last 24h: unique per point (raw, no averaging)
      - 24h-7d ago: one bucket per hour
      - older than 7d: one bucket per day
    `idx` makes "raw" keys unique (the row's position in the source list).
    """
    now = now or datetime.now()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)
    if dt >= day_ago:
        return ("raw", idx)
    if dt >= week_ago:
        return ("bucket", dt.strftime("%Y-%m-%d %H:00:00"))
    return ("bucket", dt.strftime("%Y-%m-%d 00:00:00"))


def fmt_gp(v: Optional[int]) -> str:
    """51300000 -> '51.30M', 104000 -> '104.0K', matches the JS fmtGp() in account.html."""
    if v is None:
        return "—"
    v = float(v)
    if abs(v) >= 1e9:
        return f"{v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.0f}"
