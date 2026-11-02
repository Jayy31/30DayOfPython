"""Shared domain models."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from engine.core.timeutils import to_ist


class Index(StrEnum):
    NIFTY = "NIFTY"
    SENSEX = "SENSEX"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class Direction(StrEnum):
    LONG = "LONG"    # bullish -> buy CE
    SHORT = "SHORT"  # bearish -> buy PE

    @property
    def option_type(self) -> OptionType:
        return OptionType.CE if self is Direction.LONG else OptionType.PE


class OptionContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: Index
    expiry: date
    strike: float
    option_type: OptionType
    lot_size: int
    symbol: str = ""

    @property
    def key(self) -> str:
        return self.symbol or f"{self.index}{self.expiry:%y%m%d}{int(self.strike)}{self.option_type}"


class OptionQuote(BaseModel):
    """Top-of-book snapshot for one option. Timestamps are IST-aware."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    bid: float
    ask: float
    ltp: float
    volume: int = 0
    oi: int = 0
    fast_market: bool = False

    @field_validator("ts")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return to_ist(v)

    @property
    def spread(self) -> float:
        return self.ask - self.bid
