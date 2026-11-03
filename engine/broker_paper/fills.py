"""Realistic fill prices: buys at ask + slippage, sells at bid - slippage. Never LTP."""

from __future__ import annotations

import math

from engine.config.schemas import ChargesConfig
from engine.core.models import OptionQuote


def _slip(quote: OptionQuote, cfg: ChargesConfig) -> float:
    return cfg.slippage.fast_move_per_unit if quote.fast_market else cfg.slippage.per_unit


def round_up_tick(price: float, tick: float) -> float:
    return round(math.ceil(round(price / tick, 6)) * tick, 2)


def round_down_tick(price: float, tick: float) -> float:
    return round(math.floor(round(price / tick, 6)) * tick, 2)


def buy_fill_price(quote: OptionQuote, cfg: ChargesConfig) -> float:
    if quote.ask <= 0:
        raise ValueError("no ask: cannot buy")
    return round_up_tick(quote.ask + _slip(quote, cfg), cfg.tick_size)


def sell_fill_price(quote: OptionQuote, cfg: ChargesConfig) -> float:
    return max(cfg.tick_size, round_down_tick(quote.bid - _slip(quote, cfg), cfg.tick_size))
