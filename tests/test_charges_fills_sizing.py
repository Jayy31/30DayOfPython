from __future__ import annotations

import pytest

from engine.broker_paper.charges import Side, leg_charges, round_trip_estimate
from engine.broker_paper.fills import buy_fill_price, sell_fill_price
from engine.broker_paper.sizing import lots_for_capital
from tests.conftest import at
from tests.helpers import q


def test_buy_leg_charges_nse(cfg):
    c = leg_charges(Side.BUY, 100.0, 130, "NSE", cfg.charges)  # turnover 13,000
    assert c.stt == 0
    assert c.stamp_duty == pytest.approx(0.39)            # 0.003%
    assert c.exchange_txn == pytest.approx(4.5539)        # 0.03503%
    assert c.sebi == pytest.approx(0.013)
    assert c.gst == pytest.approx((4.5539 + 0.013) * 0.18, abs=1e-3)
    assert c.total == pytest.approx(5.78, abs=0.01)


def test_sell_leg_has_stt_not_stamp(cfg):
    c = leg_charges(Side.SELL, 100.0, 130, "NSE", cfg.charges)
    assert c.stt == pytest.approx(13.0) and c.stamp_duty == 0


def test_bse_rate_used(cfg):
    nse = leg_charges(Side.BUY, 100, 100, "NSE", cfg.charges).exchange_txn
    bse = leg_charges(Side.BUY, 100, 100, "BSE", cfg.charges).exchange_txn
    assert bse < nse


def test_fills_never_ltp(cfg):
    quote = q(at(10, 0), bid=100.0, ask=100.5, ltp=100.25)
    assert buy_fill_price(quote, cfg.charges) == 101.0     # ask + 0.5
    assert sell_fill_price(quote, cfg.charges) == 99.5     # bid - 0.5
    fast = q(at(10, 0), bid=100.0, ask=100.5, fast=True)
    assert buy_fill_price(fast, cfg.charges) == 102.5
    assert sell_fill_price(fast, cfg.charges) == 98.0


def test_fill_rounds_to_tick_against_us(cfg):
    quote = q(at(10, 0), bid=100.02, ask=100.52)
    assert buy_fill_price(quote, cfg.charges) == 101.05
    assert sell_fill_price(quote, cfg.charges) == 99.5


def test_sell_never_below_tick(cfg):
    assert sell_fill_price(q(at(10, 0), bid=0.1), cfg.charges) == 0.05


@pytest.mark.parametrize(
    ("price", "lot", "expected"),
    [
        (100.0, 65, 2),    # 13,000 + charges
        (115.0, 65, 2),    # 14,950 + ~29 charges fits
            (115.2, 65, 1),    # 14,976 + ~29 charges > 15,000
        (230.0, 65, 1),
        (231.0, 65, 0),    # 15,015 > capital
        (50.0, 65, 4),
        (300.0, 20, 2),    # Sensex
    ],
)
def test_lot_sizing(cfg, price, lot, expected):
    exch = "NSE" if lot == 65 else "BSE"
    lots = lots_for_capital(15000, price, lot, exch, cfg.charges)
    assert lots == expected
    if lots:
        qty = lots * lot
        assert qty * price + round_trip_estimate(price, qty, exch, cfg.charges) <= 15000


def test_sizing_edge_inputs(cfg):
    assert lots_for_capital(0, 100, 65, "NSE", cfg.charges) == 0
    assert lots_for_capital(15000, 0, 65, "NSE", cfg.charges) == 0
