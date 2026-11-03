"""Layer F: probability that the premium hits +R before -R.

Until enough trades + a replay backtest exist, this is a rule-based prior model.
Every estimate is labelled uncalibrated and stamped with the prior version.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.config.schemas import PriorsConfig

UNCALIBRATED_LABEL = "uncalibrated (priors)"


@dataclass(frozen=True)
class ProbabilityInputs:
    setup: str
    regime_confidence: float     # 0..1 (0 if regime unclear)
    bias_alignment: float        # bias score * direction, -1..1
    zone_sources: int            # distinct sources at the confluence zone
    trigger_strength: float      # 0..1
    strike_score: float          # expected net gain / premium


@dataclass(frozen=True)
class ProbabilityEstimate:
    p: float
    calibrated: bool
    model_version: str
    n_samples: int
    components: dict[str, float]

    @property
    def label(self) -> str:
        return "calibrated" if self.calibrated else UNCALIBRATED_LABEL


class PriorModel:
    def __init__(self, cfg: PriorsConfig, trades_recorded: int = 0) -> None:
        self.cfg = cfg
        self.trades_recorded = trades_recorded

    def estimate(self, x: ProbabilityInputs) -> ProbabilityEstimate:
        w = self.cfg.weights
        comps = {
            "base": self.cfg.base[x.setup],
            "regime": w.get("regime", 0) * x.regime_confidence,
            "bias": w.get("bias", 0) * x.bias_alignment,
            "zone": w.get("zone", 0) * max(0, x.zone_sources - 2),
            "trigger": w.get("trigger", 0) * x.trigger_strength,
            "strike": w.get("strike", 0) * max(-1.0, min(1.0, x.strike_score)),
        }
        lo, hi = self.cfg.clip
        p = max(lo, min(hi, sum(comps.values())))
        # The prior model never claims calibration; an empirical model (fit on
        # >= calibrated_after_trades trades + replay backtest) will replace it.
        return ProbabilityEstimate(round(p, 4), False, self.cfg.version, self.trades_recorded, comps)
