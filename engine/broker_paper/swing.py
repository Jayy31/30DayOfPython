"""Swing-low trail on option-premium candles.

Candles are built from premium ticks (LTP) on `timeframe_min` buckets aligned to 09:15.
A candle is a confirmed pivot low when its low is <= the lows of the `pivot_bars` candles
before it and strictly < the lows of the `pivot_bars` closed candles after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from engine.config.schemas import TrailConfig
from engine.core.timeutils import floor_to_minutes


@dataclass
class Candle:
    start: datetime
    open: float
    high: float
    low: float
    close: float


class SwingTrail:
    def __init__(self, cfg: TrailConfig) -> None:
        self.cfg = cfg
        self.closed: list[Candle] = []
        self.current: Candle | None = None
        self.last_swing_low: float | None = None

    def update(self, ts: datetime, price: float) -> None:
        bucket = floor_to_minutes(ts, self.cfg.timeframe_min)
        c = self.current
        if c is None or bucket > c.start:
            if c is not None:
                self.closed.append(c)
                self._detect()
            self.current = Candle(bucket, price, price, price, price)
            return
        c.high = max(c.high, price)
        c.low = min(c.low, price)
        c.close = price

    def _detect(self) -> None:
        k = self.cfg.pivot_bars
        if len(self.closed) < 2 * k + 1:
            return
        i = len(self.closed) - 1 - k
        low = self.closed[i].low
        left = self.closed[i - k : i]
        right = self.closed[i + 1 : i + 1 + k]
        if all(low <= c.low for c in left) and all(low < c.low for c in right):
            self.last_swing_low = low

    def stop_level(self) -> float | None:
        if self.last_swing_low is None:
            return None
        return self.last_swing_low * (1 - self.cfg.buffer_pct / 100)
