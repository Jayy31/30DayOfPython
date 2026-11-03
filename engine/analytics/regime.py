"""Layer A: what kind of day is it? Combines GEX, straddle, opening type and IB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from engine.analytics.gex import GexResult
from engine.analytics.opening import InitialBalance, OpeningRead, OpeningType
from engine.analytics.straddle import StraddleState
from engine.config.schemas import RegimeConfig

RegimeLabel = Literal["trend", "range", "unclear"]


@dataclass(frozen=True)
class RegimeRead:
    label: RegimeLabel
    confidence: float           # 0..1
    trend_points: float
    range_points: float
    drivers: tuple[str, ...]
    allowed_families: tuple[str, ...]


def read_regime(
    cfg: RegimeConfig,
    *,
    gex: GexResult | None = None,
    straddle: StraddleState | None = None,
    opening: OpeningRead | None = None,
    ib: InitialBalance | None = None,
) -> RegimeRead:
    tw, rw = cfg.trend_weights, cfg.range_weights
    trend = rng = 0.0
    drivers: list[str] = []

    def add(side: str, key: str, note: str) -> None:
        nonlocal trend, rng
        w = (tw if side == "trend" else rw).get(key, 0.0)
        if w:
            if side == "trend":
                trend += w
            else:
                rng += w
            drivers.append(f"{side}: {note}")

    if gex is not None:
        add("trend", "negative_gamma", "negative dealer gamma") if gex.regime == "negative" else add("range", "positive_gamma", "positive dealer gamma")
    if straddle is not None:
        if straddle.label == "expanding":
            add("trend", "straddle_expanding", f"straddle expanding {straddle.expansion:+.0%}")
        elif straddle.label == "decaying":
            add("range", "straddle_decaying", f"straddle decaying {straddle.expansion:+.0%}")
    if opening is not None:
        if opening.type in (OpeningType.OPEN_DRIVE, OpeningType.OPEN_TEST_DRIVE):
            add("trend", "opening_drive", opening.type.value)
        elif opening.type is OpeningType.OPEN_AUCTION:
            add("range", "opening_auction", opening.type.value)
    if ib is not None:
        if ib.extension_odds == "high":
            add("trend", "ib_extension_high", f"narrow IB ({ib.ratio_to_implied:.2f}x implied)")
        elif ib.extension_odds == "low":
            add("range", "ib_extension_low", f"wide IB ({ib.ratio_to_implied:.2f}x implied)")

    total_possible = max(sum(tw.values()), sum(rw.values()), 1e-9)
    edge = trend - rng
    label: RegimeLabel = "trend" if edge >= cfg.min_edge else "range" if -edge >= cfg.min_edge else "unclear"
    confidence = min(1.0, abs(edge) / total_possible) if label != "unclear" else 0.0
    return RegimeRead(label, confidence, trend, rng, tuple(drivers), tuple(cfg.families[label]))
