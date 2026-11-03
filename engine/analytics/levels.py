"""Layer C primitives: reference levels, VWAP, opening range, volume profile,
confluence clustering, and failed-break detection (Layer D input)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import numpy as np

from engine.core.timeutils import to_ist
from engine.data.models import Candle


@dataclass(frozen=True)
class Level:
    price: float
    source: str          # e.g. "PDH", "VWAP", "OR_HIGH", "CALL_WALL", "POC"
    weight: float = 1.0


# ------------------------------------------------------------------ VWAP
class Vwap:
    """Session VWAP from cumulative volume (use index futures: spot has no volume)."""

    def __init__(self) -> None:
        self._pv = 0.0
        self._v = 0
        self._last_cum: int | None = None

    def update(self, price: float, cum_volume: int) -> float | None:
        if self._last_cum is None:
            self._last_cum = cum_volume
            return self.value
        dv = max(cum_volume - self._last_cum, 0)
        self._last_cum = cum_volume
        self._pv += price * dv
        self._v += dv
        return self.value

    @property
    def value(self) -> float | None:
        return self._pv / self._v if self._v else None


# ---------------------------------------------------------- opening range
class OpeningRange:
    def __init__(self, start: time = time(9, 15), end: time = time(9, 30)) -> None:
        self.start, self.end = start, end
        self.high: float | None = None
        self.low: float | None = None
        self.complete = False

    def update(self, ts: datetime, px: float) -> None:
        t = to_ist(ts).time()
        if t >= self.end:
            self.complete = self.high is not None
            return
        if t < self.start:
            return
        self.high = px if self.high is None else max(self.high, px)
        self.low = px if self.low is None else min(self.low, px)


# ---------------------------------------------------------- volume profile
@dataclass(frozen=True)
class VolumeProfile:
    poc: float
    vah: float
    val: float
    low_volume_nodes: tuple[float, ...]


def volume_profile(candles: Sequence[Candle], bin_size: float, value_area: float = 0.70, lvn_ratio: float = 0.25) -> VolumeProfile:
    """Distribute each candle's volume evenly over its high-low range in `bin_size` bins."""
    if not candles:
        raise ValueError("no candles")
    hist: dict[float, float] = defaultdict(float)
    for c in candles:
        lo, hi = np.floor(c.low / bin_size), np.floor(c.high / bin_size)
        bins = np.arange(lo, hi + 1) * bin_size
        vol = max(c.volume, 1)
        for b in bins:
            hist[float(b)] += vol / len(bins)
    prices = sorted(hist)
    vols = np.array([hist[p] for p in prices])
    poc_i = int(vols.argmax())
    total, acc = vols.sum(), vols[poc_i]
    lo_i = hi_i = poc_i
    while acc < value_area * total and (lo_i > 0 or hi_i < len(prices) - 1):
        up = vols[hi_i + 1] if hi_i < len(prices) - 1 else -1
        dn = vols[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += dn
    peak = vols.max()
    lvns = tuple(
        prices[i]
        for i in range(1, len(prices) - 1)
        if vols[i] < lvn_ratio * peak and vols[i] < vols[i - 1] and vols[i] < vols[i + 1]
    )
    return VolumeProfile(poc=prices[poc_i], vah=prices[hi_i] + bin_size, val=prices[lo_i], low_volume_nodes=lvns)


# ------------------------------------------------------------ reference
def previous_day_levels(day_candles: Sequence[Candle]) -> list[Level]:
    return [
        Level(max(c.high for c in day_candles), "PDH"),
        Level(min(c.low for c in day_candles), "PDL"),
        Level(day_candles[-1].close, "PDC"),
    ]


def profile_levels(vp: VolumeProfile) -> list[Level]:
    return [Level(vp.poc, "POC"), Level(vp.vah, "VAH"), Level(vp.val, "VAL")] + [Level(p, "LVN", 0.5) for p in vp.low_volume_nodes]


# ------------------------------------------------------------ confluence
@dataclass
class Zone:
    price: float
    sources: list[str] = field(default_factory=list)
    weight: float = 0.0
    low: float = 0.0
    high: float = 0.0

    @property
    def source_count(self) -> int:
        return len(set(self.sources))


def confluence_zones(levels: Iterable[Level], tolerance_pct: float = 0.1, min_sources: int = 2) -> list[Zone]:
    """Cluster nearby levels; a zone is valid only when >= min_sources distinct sources agree."""
    lv = sorted(levels, key=lambda x: x.price)
    zones: list[list[Level]] = []
    for level in lv:
        if zones and level.price - zones[-1][0].price <= zones[-1][0].price * tolerance_pct / 100:
            zones[-1].append(level)
        else:
            zones.append([level])
    out = []
    for group in zones:
        w = sum(x.weight for x in group)
        z = Zone(
            price=sum(x.price * x.weight for x in group) / w,
            sources=[x.source for x in group],
            weight=w,
            low=group[0].price,
            high=group[-1].price,
        )
        if z.source_count >= min_sources:
            out.append(z)
    return out


def nearest_zone(zones: Sequence[Zone], px: float, max_distance_pct: float) -> Zone | None:
    best = min(zones, key=lambda z: abs(z.price - px), default=None)
    if best is None or abs(best.price - px) > px * max_distance_pct / 100:
        return None
    return best


# ---------------------------------------------------------- failed breaks
@dataclass(frozen=True)
class FailedBreak:
    level: Level
    side: str               # "above" (failed breakout up -> bearish) | "below" (bullish)
    broke_at: datetime
    rejected_at: datetime
    extreme: float          # furthest price beyond the level (stop for the reversal)


class FailedBreakDetector:
    """A break beyond `level` by >= min_break_pct that returns inside within `within`."""

    def __init__(self, level: Level, within: timedelta = timedelta(minutes=15), min_break_pct: float = 0.03) -> None:
        self.level = level
        self.within = within
        self.min_break = level.price * min_break_pct / 100
        self._broke_at: datetime | None = None
        self._side: str | None = None
        self._extreme = level.price

    def update(self, ts: datetime, px: float) -> FailedBreak | None:
        lvl = self.level.price
        if self._broke_at is None:
            if px >= lvl + self.min_break:
                self._broke_at, self._side, self._extreme = ts, "above", px
            elif px <= lvl - self.min_break:
                self._broke_at, self._side, self._extreme = ts, "below", px
            return None
        if ts - self._broke_at > self.within:
            # break held: it's a real breakout, stop watching until price returns
            inside = (px < lvl) if self._side == "above" else (px > lvl)
            if inside:
                self._broke_at = None
            return None
        if self._side == "above":
            self._extreme = max(self._extreme, px)
            if px < lvl:
                return self._fire(ts)
        else:
            self._extreme = min(self._extreme, px)
            if px > lvl:
                return self._fire(ts)
        return None

    def _fire(self, ts: datetime) -> FailedBreak:
        assert self._broke_at is not None and self._side is not None
        fb = FailedBreak(self.level, self._side, self._broke_at, ts, self._extreme)
        self._broke_at = None
        return fb
