"""Layer D primitives: approximate order flow from ticks + depth, and futures price/OI classes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from engine.data.models import Tick


def classify_aggressor(tick: Tick, prev_ltp: float | None) -> int:
    """+1 buyer-initiated, -1 seller-initiated, 0 unknown. Quote rule first, tick rule fallback."""
    if tick.ask > 0 and tick.ltp >= tick.ask:
        return 1
    if tick.bid > 0 and tick.ltp <= tick.bid:
        return -1
    if prev_ltp is not None:
        return (tick.ltp > prev_ltp) - (tick.ltp < prev_ltp)
    return 0


class CumulativeDelta:
    def __init__(self, window: timedelta = timedelta(minutes=5)) -> None:
        self.cum = 0
        self.window = window
        self._recent: deque[tuple[datetime, int, float]] = deque()
        self._prev_ltp: float | None = None
        self._prev_vol: int | None = None

    def update(self, tick: Tick) -> int:
        qty = tick.last_qty
        if self._prev_vol is not None and tick.volume >= self._prev_vol:
            qty = tick.volume - self._prev_vol or qty
        self._prev_vol = tick.volume
        signed = classify_aggressor(tick, self._prev_ltp) * qty
        self._prev_ltp = tick.ltp
        self.cum += signed
        self._recent.append((tick.ts, signed, tick.ltp))
        while self._recent and tick.ts - self._recent[0][0] > self.window:
            self._recent.popleft()
        return signed

    @property
    def window_delta(self) -> int:
        return sum(s for _, s, _ in self._recent)

    def window_prices(self) -> list[float]:
        return [p for _, _, p in self._recent]


def depth_imbalance(tick: Tick, levels: int = 5) -> float:
    """(bid qty - ask qty) / total over top `levels`; falls back to touchline. Range [-1, 1]."""
    if tick.bids or tick.asks:
        b = sum(x.qty for x in tick.bids[:levels])
        a = sum(x.qty for x in tick.asks[:levels])
    else:
        b, a = tick.bid_qty, tick.ask_qty
    return (b - a) / (b + a) if (a + b) else 0.0


@dataclass(frozen=True)
class Absorption:
    side: int        # +1 = buyers absorbed (sellers hitting, price holds -> bullish), -1 opposite
    delta: int
    level: float


def detect_absorption(cd: CumulativeDelta, level: float, tolerance: float, min_delta: int) -> Absorption | None:
    """Heavy one-sided aggression into a level that fails to push price through it."""
    prices = cd.window_prices()
    if not prices:
        return None
    d = cd.window_delta
    if d <= -min_delta and min(prices) >= level - tolerance:
        return Absorption(side=1, delta=d, level=level)       # selling absorbed at support
    if d >= min_delta and max(prices) <= level + tolerance:
        return Absorption(side=-1, delta=d, level=level)      # buying absorbed at resistance
    return None


class FuturesOiClass(StrEnum):
    LONG_BUILDUP = "long_buildup"
    SHORT_BUILDUP = "short_buildup"
    SHORT_COVERING = "short_covering"
    LONG_UNWINDING = "long_unwinding"
    NEUTRAL = "neutral"

    @property
    def bias(self) -> int:
        return {"long_buildup": 1, "short_covering": 1, "short_buildup": -1, "long_unwinding": -1}.get(self.value, 0)


def classify_futures_oi(price_chg_pct: float, oi_chg_pct: float, min_price_pct: float = 0.05, min_oi_pct: float = 0.2) -> FuturesOiClass:
    if abs(price_chg_pct) < min_price_pct or abs(oi_chg_pct) < min_oi_pct:
        return FuturesOiClass.NEUTRAL
    if price_chg_pct > 0:
        return FuturesOiClass.LONG_BUILDUP if oi_chg_pct > 0 else FuturesOiClass.SHORT_COVERING
    return FuturesOiClass.SHORT_BUILDUP if oi_chg_pct > 0 else FuturesOiClass.LONG_UNWINDING


def option_premium_flow(ce_ticks: list[Tick], pe_ticks: list[Tick]) -> float:
    """Sensex proxy for missing futures flow: net buyer-initiated premium (CE - PE) at ATM +/- 2.
    Returns a value in [-1, 1]."""

    def flow(ticks: list[Tick]) -> float:
        prev = None
        total = 0.0
        for t in ticks:
            total += classify_aggressor(t, prev) * t.last_qty * t.ltp
            prev = t.ltp
        return total

    ce, pe = flow(ce_ticks), flow(pe_ticks)
    denom = abs(ce) + abs(pe)
    return (ce - pe) / denom if denom else 0.0
