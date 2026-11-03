"""Exit-ladder scenarios. Entry price = 101 (ask 100.5 + 0.5 slip) unless noted.

R = 20% of 101 = 20.2 -> SL 80.8, 1R 121.2, 1.5R 131.3, BE 106.05, lock 121.2.
"""

from __future__ import annotations

import pytest

from engine.broker_paper.charges import Side
from engine.broker_paper.position import LegReason, Outcome, Position
from engine.core.models import Direction
from tests.conftest import at
from tests.helpers import NIFTY_CE, q


def make(cfg, lots: int, entry: float = 101.0) -> Position:
    return Position(
        contract=NIFTY_CE,
        exchange="NSE",
        direction=Direction.LONG,
        lots=lots,
        entry_price=entry,
        entry_ts=at(9, 45),
        rules=cfg.rules,
        charges=cfg.charges,
    )


def reasons(p: Position) -> list[tuple[LegReason, int]]:
    return [(leg.reason, leg.lots) for leg in p.legs if leg.side is Side.SELL]


def test_levels(cfg):
    p = make(cfg, 1)
    assert (p.initial_sl, p.target_1, p.target_2) == (80.8, 121.2, 131.3)


def test_initial_sl_hit(cfg):
    p = make(cfg, 2)
    assert p.on_quote(q(at(9, 50), 90)) == []
    legs = p.on_quote(q(at(9, 55), 80.5))
    assert [leg.reason for leg in legs] == [LegReason.SL]
    assert legs[0].price == 80.0  # bid - slippage
    assert p.outcome is Outcome.SL and not p.is_open
    assert p.net_pnl < 0 and p.r_multiple < -1  # slippage + charges make it worse than -1R


def test_one_lot_1r_then_breakeven_counts_as_target(cfg):
    p = make(cfg, 1)
    assert p.on_quote(q(at(10, 0), 122)) == []          # 1 lot: nothing booked at 1R
    assert p.stage == 1 and p.reached_1r and p.sl == pytest.approx(106.05)
    p.on_quote(q(at(10, 10), 106))
    assert reasons(p) == [(LegReason.BREAKEVEN_SL, 1)]
    assert p.outcome is Outcome.TARGET


def test_one_lot_1_5r_locks_plus_20(cfg):
    p = make(cfg, 1)
    p.on_quote(q(at(10, 0), 122))
    p.on_quote(q(at(10, 5), 132))
    assert p.stage == 2 and p.sl == pytest.approx(121.2)
    p.on_quote(q(at(10, 6), 121))
    assert reasons(p) == [(LegReason.LOCK_SL, 1)]


def test_two_lots(cfg):
    p = make(cfg, 2)
    p.on_quote(q(at(10, 0), 122))
    assert reasons(p) == [(LegReason.TARGET_1R, 1)]
    p.on_quote(q(at(10, 5), 132))
    assert len(reasons(p)) == 1 and p.sl == pytest.approx(121.2)   # nothing booked at 1.5R for 2 lots
    p.on_quote(q(at(10, 7), 120))
    assert reasons(p) == [(LegReason.TARGET_1R, 1), (LegReason.LOCK_SL, 1)]
    assert p.outcome is Outcome.TARGET


def test_three_lots(cfg):
    p = make(cfg, 3)
    p.on_quote(q(at(10, 0), 122))
    p.on_quote(q(at(10, 5), 132))
    assert reasons(p) == [(LegReason.TARGET_1R, 1), (LegReason.TARGET_1_5R, 1)]
    assert p.open_lots == 1


def test_five_lots_2_1_2(cfg):
    p = make(cfg, 5)
    p.on_quote(q(at(10, 0), 122))
    p.on_quote(q(at(10, 5), 132))
    assert reasons(p) == [(LegReason.TARGET_1R, 2), (LegReason.TARGET_1_5R, 1)]
    assert p.open_lots == 2


def test_gap_through_both_targets_in_one_tick(cfg):
    p = make(cfg, 4)
    legs = p.on_quote(q(at(10, 0), 140))
    assert [(leg.reason, leg.lots) for leg in legs] == [(LegReason.TARGET_1R, 2), (LegReason.TARGET_1_5R, 1)]
    assert p.stage == 2 and p.sl == pytest.approx(121.2)


def test_trail_follows_swing_lows_and_never_moves_down(cfg):
    p = make(cfg, 1)
    p.on_quote(q(at(9, 48), 122))
    p.on_quote(q(at(9, 51), 135))                 # stage 2, SL 121.2
    # 3-min candles: 140, 145, 138 (pivot), 146, 150 -> pivot 138 confirmed at the 7th candle open
    prices = [140, 145, 138, 146, 150, 152]
    for i, px in enumerate(prices):
        m = 54 + i * 3
        p.on_quote(q(at(9 + m // 60, m % 60), px))
    assert p.trailed and p.sl == pytest.approx(135.2)   # 138 * 0.98 = 135.24 -> tick down
    # a later, lower pivot must not lower the SL
    for i, px in enumerate([149, 136, 150, 151, 153]):
        m = 72 + i * 3
        p.on_quote(q(at(9 + m // 60, m % 60), px if px != 136 else 136.5))
    assert p.sl >= 135.2
    p.on_quote(q(at(10, 40), 135))
    assert reasons(p) == [(LegReason.TRAIL_SL, 1)]
    assert p.outcome is Outcome.TARGET


def test_trail_inactive_before_1_5r(cfg):
    p = make(cfg, 1)
    for i, px in enumerate([110, 105, 100, 104, 108, 112]):
        m = 48 + i * 3
        p.on_quote(q(at(9 + m // 60, m % 60), px))
    assert p.sl == 80.8 and not p.trailed


def test_square_off_before_1r_is_time_exit(cfg):
    p = make(cfg, 2)
    p.on_quote(q(at(14, 0), 110))
    p.square_off(q(at(15, 15), 112))
    assert reasons(p) == [(LegReason.SQUARE_OFF, 2)]
    assert p.outcome is Outcome.TIME


def test_square_off_after_1r_is_target(cfg):
    p = make(cfg, 2)
    p.on_quote(q(at(14, 0), 122))
    p.square_off(q(at(15, 15), 115))
    assert p.outcome is Outcome.TARGET


def test_pnl_accounting(cfg):
    p = make(cfg, 2)
    p.on_quote(q(at(10, 0), 122))      # sell 1 lot @121.5
    p.on_quote(q(at(10, 5), 106))      # sell 1 lot @105.5 (BE stop)
    assert p.gross_pnl == pytest.approx((121.5 - 101) * 65 + (105.5 - 101) * 65)
    assert p.net_pnl == pytest.approx(p.gross_pnl - p.total_charges)
    assert p.avg_exit_price == pytest.approx(113.5)
    assert p.on_quote(q(at(10, 6), 200)) == []  # closed positions ignore ticks
