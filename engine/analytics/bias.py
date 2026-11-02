"""Layer B: which direction is favoured? Weighted blend of normalised [-1, 1] inputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from engine.config.schemas import BiasConfig
from engine.core.models import Index


@dataclass(frozen=True)
class BiasDriver:
    name: str
    value: float
    weight: float


@dataclass(frozen=True)
class BiasRead:
    index: Index
    score: float                       # -1..1
    label: Literal["bullish", "bearish", "neutral"]
    drivers: tuple[BiasDriver, ...]

    @property
    def direction(self) -> int:
        return 1 if self.label == "bullish" else -1 if self.label == "bearish" else 0


def read_bias(cfg: BiasConfig, index: Index, inputs: Mapping[str, float | None], nifty_futures_proxy: float | None = None) -> BiasRead:
    """inputs: driver name -> value in [-1, 1] (None = unavailable; excluded, not zero).

    For Sensex the 'futures_oi' driver is blended with Nifty futures flow by
    `sensex_futures_proxy_weight` because Sensex futures are thin."""
    vals = dict(inputs)
    if index is Index.SENSEX and nifty_futures_proxy is not None:
        w = cfg.sensex_futures_proxy_weight
        own = vals.get("futures_oi")
        vals["futures_oi"] = nifty_futures_proxy if own is None else (1 - w) * own + w * nifty_futures_proxy
    drivers = []
    num = den = 0.0
    for name, v in vals.items():
        w = cfg.weights.get(name, 0.0)
        if v is None or w == 0:
            continue
        v = max(-1.0, min(1.0, float(v)))
        drivers.append(BiasDriver(name, v, w))
        num += w * v
        den += w
    score = num / den if den else 0.0
    label: Literal["bullish", "bearish", "neutral"] = (
        "bullish" if score > cfg.neutral_band else "bearish" if score < -cfg.neutral_band else "neutral"
    )
    return BiasRead(index, score, label, tuple(sorted(drivers, key=lambda d: -abs(d.value * d.weight))))
