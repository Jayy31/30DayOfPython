"""Heavyweight breadth: weighted move of the top index constituents vs the index itself."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Breadth:
    weighted_change_pct: float
    index_change_pct: float
    advancing_weight: float      # share of weight that is up (0..1)
    divergence: bool
    direction: int               # sign of the heavyweights' weighted move


def heavyweight_breadth(
    weights: Mapping[str, float],
    change_pct: Mapping[str, float],
    index_change_pct: float,
    divergence_gap_pct: float = 0.15,
) -> Breadth:
    """weights: symbol -> index weight; change_pct: symbol -> % change from prev close."""
    present = {s: w for s, w in weights.items() if s in change_pct}
    if not present:
        raise ValueError("no heavyweight prices")
    tw = sum(present.values())
    wchg = sum(w * change_pct[s] for s, w in present.items()) / tw
    adv = sum(w for s, w in present.items() if change_pct[s] > 0) / tw
    opposite = wchg * index_change_pct < 0 and max(abs(wchg), abs(index_change_pct)) >= divergence_gap_pct / 2
    divergence = opposite or abs(wchg - index_change_pct) >= divergence_gap_pct
    return Breadth(wchg, index_change_pct, adv, divergence, (wchg > 0) - (wchg < 0))
