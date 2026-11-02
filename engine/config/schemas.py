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

    @model_validator(mode="after")
    def _instruments_cover_indices(self) -> AppConfig:
        missing = set(self.rules.indices) - set(self.instruments)
        if missing:
            raise ValueError(f"instruments.yaml missing: {sorted(missing)}")
        for idx in self.rules.indices:
            if self.instruments[idx].exchange not in self.charges.exchange_txn_pct:
                raise ValueError(f"charges.yaml has no exchange_txn_pct for {self.instruments[idx].exchange}")
        return self
