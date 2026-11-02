"""Lot sizing: whole lots only, premium + estimated round-trip charges within capital."""

from __future__ import annotations

from engine.broker_paper.charges import round_trip_estimate
from engine.config.schemas import ChargesConfig


def lots_for_capital(capital: float, buy_price: float, lot_size: int, exchange: str, cfg: ChargesConfig) -> int:
    if buy_price <= 0 or lot_size <= 0 or capital <= 0:
        return 0
    lots = int(capital // (buy_price * lot_size))
    while lots > 0:
        qty = lots * lot_size
        if qty * buy_price + round_trip_estimate(buy_price, qty, exchange, cfg) <= capital:
            return lots
        lots -= 1
    return 0
