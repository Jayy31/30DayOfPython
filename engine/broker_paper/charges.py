"""Statutory + broker charges per executed leg. All rates come from charges.yaml."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from engine.config.schemas import ChargesConfig


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ChargeBreakdown(BaseModel):
    brokerage: float
    exchange_txn: float
    sebi: float
    gst: float
    stt: float
    stamp_duty: float

    @property
    def total(self) -> float:
        return round(self.brokerage + self.exchange_txn + self.sebi + self.gst + self.stt + self.stamp_duty, 2)


def leg_charges(side: Side, price: float, qty: int, exchange: str, cfg: ChargesConfig) -> ChargeBreakdown:
    turnover = price * qty
    brokerage = cfg.brokerage_per_order
    exch = turnover * cfg.exchange_txn_pct[exchange] / 100  # type: ignore[index]
    sebi = turnover * cfg.sebi_fee_per_crore / 1e7
    gst = (brokerage + exch + sebi) * cfg.gst_pct / 100
    stt = turnover * cfg.stt_sell_pct / 100 if side is Side.SELL else 0.0
    stamp = turnover * cfg.stamp_duty_buy_pct / 100 if side is Side.BUY else 0.0
    return ChargeBreakdown(
        brokerage=round(brokerage, 4),
        exchange_txn=round(exch, 4),
        sebi=round(sebi, 4),
        gst=round(gst, 4),
        stt=round(stt, 4),
        stamp_duty=round(stamp, 4),
    )


def round_trip_estimate(price: float, qty: int, exchange: str, cfg: ChargesConfig) -> float:
    """Charges for buying and selling `qty` at `price` (used for sizing)."""
    return leg_charges(Side.BUY, price, qty, exchange, cfg).total + leg_charges(Side.SELL, price, qty, exchange, cfg).total
