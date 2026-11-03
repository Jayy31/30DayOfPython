"""Layer D: does the order flow confirm a direction right now?"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.analytics.breadth import Breadth
from engine.analytics.levels import FailedBreak
from engine.analytics.orderflow import Absorption, FuturesOiClass


@dataclass(frozen=True)
class TriggerInputs:
    window_delta: float = 0.0            # normalised cumulative delta over the window, [-1, 1]
    depth_imbalance: float = 0.0         # [-1, 1]
    futures_oi: FuturesOiClass = FuturesOiClass.NEUTRAL
    breadth: Breadth | None = None
    absorption: Absorption | None = None
    failed_break: FailedBreak | None = None
    premium_flow: float | None = None    # Sensex: CE-PE premium flow at ATM +/- 2


@dataclass(frozen=True)
class TriggerRead:
    direction: int
    strength: float                      # 0..1 support for `direction`
    evidence: tuple[str, ...] = field(default_factory=tuple)

    def supports(self, direction: int, min_strength: float) -> bool:
        return self.direction == direction and self.strength >= min_strength


# each component votes in [-1, 1] with these weights
_W = {"delta": 1.0, "depth": 0.5, "futures_oi": 0.8, "breadth": 0.6, "absorption": 1.0, "failed_break": 1.2, "premium_flow": 0.6}


def read_trigger(x: TriggerInputs) -> TriggerRead:
    votes: list[tuple[str, float, str]] = [
        ("delta", x.window_delta, f"cum delta {x.window_delta:+.2f}"),
        ("depth", x.depth_imbalance, f"depth imbalance {x.depth_imbalance:+.2f}"),
    ]
    if x.futures_oi is not FuturesOiClass.NEUTRAL:
        votes.append(("futures_oi", float(x.futures_oi.bias), f"futures {x.futures_oi.value}"))
    if x.breadth is not None:
        v = float(x.breadth.direction) * (0.5 if x.breadth.divergence else 1.0)
        votes.append(("breadth", v, f"heavyweights {x.breadth.weighted_change_pct:+.2f}%{' (divergent)' if x.breadth.divergence else ''}"))
    if x.absorption is not None:
        votes.append(("absorption", float(x.absorption.side), f"absorption at {x.absorption.level:.0f}"))
    if x.failed_break is not None:
        side = -1.0 if x.failed_break.side == "above" else 1.0
        votes.append(("failed_break", side, f"failed break {x.failed_break.side} {x.failed_break.level.source}"))
    if x.premium_flow is not None:
        votes.append(("premium_flow", x.premium_flow, f"premium flow {x.premium_flow:+.2f}"))

    num = sum(_W[k] * max(-1.0, min(1.0, v)) for k, v, _ in votes)
    den = sum(_W[k] for k, _, _ in votes)
    net = num / den if den else 0.0
    direction = (net > 0) - (net < 0)
    evidence = tuple(note for k, v, note in votes if v * direction > 0)
    return TriggerRead(direction, abs(net), evidence)
