"""Dealer gamma exposure (GEX) and the gamma flip level.

Convention: dealers are long calls / short puts sold by clients ("classic"
SpotGamma convention: call GEX +, put GEX -). Participant-wise OI can tilt this
via `call_sign` / `put_sign` in [-1, 1] (e.g. when clients are net option buyers,
writers/dealers are short both and signs move toward -1).

GEX per strike = sign * gamma * OI_units * S^2 * 0.01  (Rs change in dealer delta per 1% move).
Positive total -> dealers dampen moves (pinning); negative -> they amplify moves.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from engine.analytics.greeks import greeks, implied_vol, time_to_expiry
from engine.core.models import OptionType
from engine.data.models import OptionChain


@dataclass(frozen=True)
class GexResult:
    total: float
    by_strike: dict[float, float]
    flip: float | None          # spot where total GEX crosses zero (nearest to current spot)
    regime: str                 # "positive" | "negative"


def _row_iv(row, spot: float, t: float, fallback_iv: float) -> float:  # type: ignore[no-untyped-def]
    if row.iv:
        return row.iv / 100 if row.iv > 3 else row.iv   # accept percent or decimal
    mid = (row.bid + row.ask) / 2 if row.bid > 0 and row.ask > 0 else row.ltp
    return implied_vol(mid, spot, row.strike, t, row.option_type) or fallback_iv


def _total_gex(chain: OptionChain, spot: float, t: float, ivs: list[float], oi_mult: int, call_sign: float, put_sign: float) -> tuple[float, dict[float, float]]:
    per: dict[float, float] = defaultdict(float)
    for row, iv in zip(chain.rows, ivs, strict=True):
        if row.oi <= 0:
            continue
        g = greeks(spot, row.strike, t, iv, row.option_type).gamma
        sign = call_sign if row.option_type is OptionType.CE else put_sign
        per[row.strike] += sign * g * row.oi * oi_mult * spot * spot * 0.01
    return sum(per.values()), dict(per)


def compute_gex(
    chain: OptionChain,
    now: datetime,
    *,
    oi_multiplier: int = 1,          # set to lot size when OI is reported in lots
    call_sign: float = 1.0,
    put_sign: float = -1.0,
    fallback_iv: float = 0.14,
    flip_range_pct: float = 3.0,
    flip_steps: int = 121,
) -> GexResult:
    t = time_to_expiry(now, chain.expiry)
    ivs = [_row_iv(r, chain.spot, t, fallback_iv) for r in chain.rows]
    total, by_strike = _total_gex(chain, chain.spot, t, ivs, oi_multiplier, call_sign, put_sign)

    grid = np.linspace(chain.spot * (1 - flip_range_pct / 100), chain.spot * (1 + flip_range_pct / 100), flip_steps)
    totals = np.array([_total_gex(chain, float(s), t, ivs, oi_multiplier, call_sign, put_sign)[0] for s in grid])
    flip = None
    crossings = np.where(np.sign(totals[:-1]) * np.sign(totals[1:]) < 0)[0]
    if len(crossings):
        cands = []
        for i in crossings:
            s0, s1, g0, g1 = grid[i], grid[i + 1], totals[i], totals[i + 1]
            cands.append(float(s0 - g0 * (s1 - s0) / (g1 - g0)))   # linear interpolation
        flip = min(cands, key=lambda s: abs(s - chain.spot))
    return GexResult(total=total, by_strike=by_strike, flip=flip, regime="positive" if total >= 0 else "negative")
