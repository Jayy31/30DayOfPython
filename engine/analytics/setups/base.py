"""Setup plug-ins. Each named setup is a class registered by name; config enables them.

A setup only proposes a direction + reference levels. Location, trigger, strike,
probability and risk checks are applied afterwards by the decision engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from engine.analytics.bias import BiasRead
from engine.analytics.gex import GexResult
from engine.analytics.levels import FailedBreak, Zone
from engine.analytics.oi import WallShift
from engine.analytics.regime import RegimeRead
from engine.analytics.triggers import TriggerRead
from engine.core.models import Direction, Index


@dataclass(frozen=True)
class MarketContext:
    """Everything a setup may look at for one index at one moment."""

    ts: datetime
    index: Index
    spot: float
    regime: RegimeRead
    bias: BiasRead
    trigger: TriggerRead
    zones: tuple[Zone, ...] = ()
    closes_1m: tuple[float, ...] = ()        # recent 1-min closes, oldest first
    vwap: float | None = None
    or_high: float | None = None
    or_low: float | None = None
    or_complete: bool = False
    failed_break: FailedBreak | None = None
    wall_shift: WallShift | None = None
    gex: GexResult | None = None
    day_implied_move: float | None = None
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class SetupCandidate:
    setup: str
    family: str
    direction: Direction
    target_level: float          # where the underlying should go (for Layer E)
    invalidation: float          # underlying level that kills the idea
    notes: tuple[str, ...] = field(default_factory=tuple)

    def target_points(self, spot: float) -> float:
        return abs(self.target_level - spot)


class Setup(ABC):
    name: str
    family: str

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None: ...


REGISTRY: dict[str, type[Setup]] = {}


def register(cls: type[Setup]) -> type[Setup]:
    if cls.name in REGISTRY:
        raise ValueError(f"duplicate setup {cls.name}")
    REGISTRY[cls.name] = cls
    return cls


def load_setups(enabled: list[str]) -> list[Setup]:
    from engine.analytics.setups import builtin  # noqa: F401  (registers built-ins)

    unknown = set(enabled) - set(REGISTRY)
    if unknown:
        raise ValueError(f"unknown setups in config: {sorted(unknown)}")
    return [REGISTRY[n]() for n in enabled]


# ----------------------------------------------------------------- helpers
def default_target(ctx: MarketContext, direction: Direction) -> float:
    """Next confluence zone in the trade direction, else half the day's implied move."""
    above = sorted(z.price for z in ctx.zones if z.price > ctx.spot * 1.0005)
    below = sorted((z.price for z in ctx.zones if z.price < ctx.spot * 0.9995), reverse=True)
    pool = above if direction is Direction.LONG else below
    if pool:
        return pool[0]
    step = (ctx.day_implied_move or ctx.spot * 0.004) * 0.5
    return ctx.spot + step if direction is Direction.LONG else ctx.spot - step


def direction_of(sign: int) -> Direction | None:
    return Direction.LONG if sign > 0 else Direction.SHORT if sign < 0 else None
