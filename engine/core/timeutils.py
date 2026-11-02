"""IST time helpers. Everything in the engine is timezone-aware IST."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Convert an aware datetime to IST. Naive datetimes are rejected."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all timestamps must be timezone-aware")
    return dt.astimezone(IST)


def ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def in_window(t: time, start: time, end: time) -> bool:
    """Half-open window [start, end)."""
    return start <= t < end


def floor_to_minutes(dt: datetime, minutes: int) -> datetime:
    """Floor to a candle boundary aligned to the 09:15 market open."""
    dt = to_ist(dt)
    open_dt = ist(dt.date(), MARKET_OPEN)
    elapsed = int((dt - open_dt).total_seconds() // 60)
    return open_dt + timedelta(minutes=(elapsed // minutes) * minutes)
