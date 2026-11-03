"""Opening-type classification and initial-balance analysis (Market Profile terms).

open_drive            : opens and moves one way, barely revisits the open
open_test_drive       : probes one way first, then drives the other way through the open
open_rejection_reverse: moves one way, gets rejected and closes back across the open
open_auction          : rotates around the open, no conviction
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from engine.data.models import Candle


class OpeningType(StrEnum):
    OPEN_DRIVE = "open_drive"
    OPEN_TEST_DRIVE = "open_test_drive"
    OPEN_REJECTION_REVERSE = "open_rejection_reverse"
    OPEN_AUCTION = "open_auction"


@dataclass(frozen=True)
class OpeningRead:
    type: OpeningType
    direction: int      # +1 up, -1 down, 0 none
    confidence: float   # 0..1


def classify_opening(candles: Sequence[Candle], *, revisit_frac: float = 0.15, drive_frac: float = 0.6) -> OpeningRead:
    """candles: 1-min candles for the first 15-60 minutes."""
    if len(candles) < 3:
        raise ValueError("need at least 3 candles")
    o = candles[0].open
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    close = candles[-1].close
    rng = hi - lo
    if rng <= 0:
        return OpeningRead(OpeningType.OPEN_AUCTION, 0, 0.5)
    up, down = hi - o, o - lo
    net = close - o
    direction = (net > 0) - (net < 0)
    # index of extremes tells which side was tested first
    hi_i = next(i for i, c in enumerate(candles) if c.high == hi)
    lo_i = next(i for i, c in enumerate(candles) if c.low == lo)

    if abs(net) >= drive_frac * rng:
        against = down if direction > 0 else up
        if against <= revisit_frac * rng:
            return OpeningRead(OpeningType.OPEN_DRIVE, direction, min(1.0, abs(net) / rng))
        tested_first = lo_i < hi_i if direction > 0 else hi_i < lo_i
        if tested_first:
            return OpeningRead(OpeningType.OPEN_TEST_DRIVE, direction, min(1.0, abs(net) / rng) * 0.8)
    first_ext_up = hi_i < lo_i
    first_move = up if first_ext_up else down
    reversed_ = (first_ext_up and close < o) or (not first_ext_up and close > o)
    if hi_i != lo_i and first_move >= 0.5 * rng and reversed_ and abs(net) >= 0.25 * rng:
        return OpeningRead(OpeningType.OPEN_REJECTION_REVERSE, -1 if first_ext_up else 1, 0.6)
    return OpeningRead(OpeningType.OPEN_AUCTION, 0, 1 - abs(net) / rng)


@dataclass(frozen=True)
class InitialBalance:
    high: float
    low: float
    range: float
    ratio_to_implied: float   # IB range / day implied move
    extension_odds: str       # "high" | "normal" | "low" (narrow IB -> more likely to extend)


def initial_balance(first_hour: Sequence[Candle], day_implied_move: float, narrow: float = 0.5, wide: float = 1.0) -> InitialBalance:
    hi = max(c.high for c in first_hour)
    lo = min(c.low for c in first_hour)
    ratio = (hi - lo) / day_implied_move if day_implied_move > 0 else 0.0
    odds = "high" if ratio < narrow else "low" if ratio > wide else "normal"
    return InitialBalance(hi, lo, hi - lo, ratio, odds)
