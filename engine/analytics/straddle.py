"""ATM straddle: priced-in move, intraday expansion vs decay, realised vs implied range."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from engine.analytics.greeks import time_to_expiry


@dataclass(frozen=True)
class StraddleState:
    open_price: float
    current: float
    expected_now: float        # open straddle decayed by sqrt(time) only
    expansion: float           # current / expected_now - 1 (>0 = expanding)
    implied_move: float        # open straddle (approx. priced-in move to expiry)
    day_implied_move: float    # one-day share of the implied move
    realised_range: float
    range_used: float          # realised range / day implied move
    label: str                 # "expanding" | "decaying" | "steady"


class StraddleTracker:
    def __init__(self, expiry: date, expand_threshold: float = 0.05) -> None:
        self.expiry = expiry
        self.threshold = expand_threshold
        self._open: tuple[datetime, float] | None = None
        self._last: tuple[datetime, float] | None = None
        self._hi = -math.inf
        self._lo = math.inf

    def update(self, ts: datetime, call_mid: float, put_mid: float, spot: float) -> None:
        px = call_mid + put_mid
        if self._open is None:
            self._open = (ts, px)
        self._last = (ts, px)
        self._hi, self._lo = max(self._hi, spot), min(self._lo, spot)

    def state(self) -> StraddleState | None:
        if self._open is None or self._last is None:
            return None
        (t0, p0), (t1, p1) = self._open, self._last
        T0, T1 = time_to_expiry(t0, self.expiry), time_to_expiry(t1, self.expiry)
        expected = p0 * math.sqrt(T1 / T0)
        expansion = p1 / expected - 1 if expected > 0 else 0.0
        days_left = max(T0 * 365, 1.0)
        day_move = p0 / math.sqrt(days_left)
        rng = self._hi - self._lo
        label = "expanding" if expansion > self.threshold else "decaying" if expansion < -self.threshold else "steady"
        return StraddleState(p0, p1, expected, expansion, p0, day_move, rng, rng / day_move if day_move else 0.0, label)
