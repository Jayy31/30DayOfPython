"""Pydantic schemas for the YAML config files. Strict: unknown keys are errors."""

from __future__ import annotations

from datetime import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IndexName = Literal["NIFTY", "SENSEX"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _parse_hhmm(v: Any) -> time:
    if isinstance(v, time):
        return v
    hh, mm = str(v).split(":")
    return time(int(hh), int(mm))


class TrailConfig(_Strict):
    timeframe_min: int = Field(3, ge=1, le=15)
    pivot_bars: int = Field(2, ge=1, le=5)
    buffer_pct: float = Field(2.0, ge=0, le=10)


class RulesConfig(_Strict):
    capital: float = Field(gt=0)
    max_trades_per_day: int = Field(ge=1, le=2)
    max_open_trades: Literal[1]
    indices: list[IndexName] = Field(min_length=1)

    sl_pct: float = Field(ge=18, le=20)
    first_target_rr: float = Field(1.0, gt=0)
    second_target_rr: float = Field(1.5, gt=0)
    breakeven_buffer_pct: float = Field(5, ge=0, le=15)
    lock_pct_at_second_target: float = Field(20, ge=0)
    trail: TrailConfig = TrailConfig()

    observe_until: time = time(9, 30)
    entry_windows: list[tuple[time, time]] = Field(min_length=1)
    square_off: time = time(15, 15)

    strike_range: int = Field(2, ge=0, le=2)
    min_delta: float = Field(0.35, gt=0, lt=1)

    min_probability: float = Field(0.60, gt=0, le=1)
    min_probability_after_sl: float = Field(0.70, gt=0, le=1)
    stop_after_target: bool = True

    @field_validator("observe_until", "square_off", mode="before")
    @classmethod
    def _t(cls, v: Any) -> time:
        return _parse_hhmm(v)

    @field_validator("entry_windows", mode="before")
    @classmethod
    def _windows(cls, v: Any) -> list[tuple[time, time]]:
        return [(_parse_hhmm(a), _parse_hhmm(b)) for a, b in v]

    @model_validator(mode="after")
    def _check(self) -> RulesConfig:
        if self.second_target_rr <= self.first_target_rr:
            raise ValueError("second_target_rr must be greater than first_target_rr")
        if self.min_probability_after_sl < self.min_probability:
            raise ValueError("min_probability_after_sl must be >= min_probability")
        if self.lock_pct_at_second_target <= self.breakeven_buffer_pct:
            raise ValueError("lock_pct_at_second_target must exceed breakeven_buffer_pct")
        prev_end = self.observe_until
        for start, end in self.entry_windows:
            if not start < end:
                raise ValueError(f"entry window {start}-{end} is empty")
            if start < prev_end:
                raise ValueError("entry windows must be ordered, non-overlapping and after observe_until")
            prev_end = end
        if prev_end > self.square_off:
            raise ValueError("entry windows must end before square_off")
        if self.square_off > time(15, 30):
            raise ValueError("square_off must be before market close (15:30)")
        if len(set(self.indices)) != len(self.indices):
            raise ValueError("duplicate indices")
        return self


class SlippageConfig(_Strict):
    per_unit: float = Field(ge=0)
    fast_move_per_unit: float = Field(ge=0)


class ChargesConfig(_Strict):
    brokerage_per_order: float = Field(ge=0)
    gst_pct: float = Field(ge=0)
    sebi_fee_per_crore: float = Field(ge=0)
    stt_sell_pct: float = Field(ge=0)
    stamp_duty_buy_pct: float = Field(ge=0)
    exchange_txn_pct: dict[Literal["NSE", "BSE"], float]
    slippage: SlippageConfig
    tick_size: float = Field(gt=0)


class InstrumentConfig(_Strict):
    exchange: Literal["NSE", "BSE"]
    segment: str
    strike_step: int = Field(gt=0)
    fallback_lot_size: int = Field(gt=0)


class ShadowRuleConfig(_Strict):
    id: str
    type: str
    params: dict[str, Any] = {}


class ShadowRulesConfig(_Strict):
    rules: list[ShadowRuleConfig] = []

    @model_validator(mode="after")
    def _unique(self) -> ShadowRulesConfig:
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("shadow rule ids must be unique")
        return self


class AppConfig(_Strict):
    rules: RulesConfig
    charges: ChargesConfig
    instruments: dict[IndexName, InstrumentConfig]
    shadow: ShadowRulesConfig
    holidays: dict[int, list[str]]
    decision: "DecisionConfig"
    llm: "LlmConfig"
    news: "NewsConfig"

    @model_validator(mode="after")
    def _instruments_cover_indices(self) -> AppConfig:
        missing = set(self.rules.indices) - set(self.instruments)
        if missing:
            raise ValueError(f"instruments.yaml missing: {sorted(missing)}")
        for idx in self.rules.indices:
            if self.instruments[idx].exchange not in self.charges.exchange_txn_pct:
                raise ValueError(f"charges.yaml has no exchange_txn_pct for {self.instruments[idx].exchange}")
        return self


# ---------------------------------------------------------------- decision
class RegimeConfig(_Strict):
    trend_weights: dict[str, float]
    range_weights: dict[str, float]
    min_edge: float = Field(gt=0)
    families: dict[Literal["trend", "range", "unclear"], list[str]]


class BiasConfig(_Strict):
    weights: dict[str, float]
    sensex_futures_proxy_weight: float = Field(ge=0, le=1)
    neutral_band: float = Field(ge=0, lt=1)
    max_counter_bias: float = Field(ge=0, le=1)


class LocationConfig(_Strict):
    zone_tolerance_pct: float = Field(gt=0)
    min_sources: int = Field(ge=2)
    max_distance_pct: float = Field(gt=0)


class TriggerConfig(_Strict):
    min_strength: float = Field(ge=0, le=1)


class StrikeConfig(_Strict):
    max_spread_pct: float = Field(gt=0)
    min_volume: int = Field(ge=0)
    min_oi: int = Field(ge=0)
    max_iv_jump_pct: float = Field(gt=0)
    hold_minutes: int = Field(gt=0)
    min_gain_to_theta: float = Field(gt=0)
    expiry_day_switch: time

    @field_validator("expiry_day_switch", mode="before")
    @classmethod
    def _t(cls, v: Any) -> time:
        return _parse_hhmm(v)


class SetupsConfig(_Strict):
    enabled: list[str] = Field(min_length=1)


class PriorsConfig(_Strict):
    version: str
    base: dict[str, float]
    weights: dict[str, float]
    clip: tuple[float, float]
    calibrated_after_trades: int = Field(ge=1)

    @model_validator(mode="after")
    def _check(self) -> PriorsConfig:
        lo, hi = self.clip
        if not 0 < lo < hi < 1:
            raise ValueError("priors.clip must satisfy 0 < lo < hi < 1")
        if any(not 0 < p < 1 for p in self.base.values()):
            raise ValueError("prior base rates must be in (0, 1)")
        return self


class DecisionConfig(_Strict):
    regime: RegimeConfig
    bias: BiasConfig
    location: LocationConfig
    trigger: TriggerConfig
    strike: StrikeConfig
    setups: SetupsConfig
    priors: PriorsConfig
    score_weights: dict[str, float]

    @model_validator(mode="after")
    def _priors_cover_setups(self) -> DecisionConfig:
        missing = set(self.setups.enabled) - set(self.priors.base)
        if missing:
            raise ValueError(f"priors.base missing setups: {sorted(missing)}")
        if set(self.regime.families) != {"trend", "range", "unclear"}:
            raise ValueError("regime.families needs trend, range and unclear")
        return self


# --------------------------------------------------------------------- llm
class LlmTaskConfig(_Strict):
    tier: Literal["fast", "smart"]
    max_tokens: int = Field(gt=0, le=64000)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"


class ModelPricing(_Strict):
    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    long_context_threshold: int | None = None
    long_context_multiplier: float = 1.0


class LlmConfig(_Strict):
    max_prompt_tokens: int = Field(gt=0, le=100000)
    tasks: dict[str, LlmTaskConfig]
    pricing: dict[str, ModelPricing]


# -------------------------------------------------------------------- news
class NewsSourceConfig(_Strict):
    id: str
    kind: Literal["rss", "gdelt", "finnhub"]
    url: str = ""
    query: str = ""
    official: bool = False
    enabled: bool = True


class NewsConfig(_Strict):
    poll_market_seconds: int = Field(gt=0)
    poll_overnight_seconds: int = Field(gt=0)
    dedupe_similarity: float = Field(gt=0, le=1)
    dedupe_window_minutes: int = Field(gt=0)
    spike_min_sources: int = Field(ge=2)
    spike_window_minutes: int = Field(gt=0)
    llm_batch_size: int = Field(gt=0, le=50)
    confirm_moves_pct: dict[str, float]
    topics: dict[str, list[str]]
    index_keywords: dict[str, list[str]]
    sources: list[NewsSourceConfig]


AppConfig.model_rebuild()
