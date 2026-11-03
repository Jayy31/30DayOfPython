from __future__ import annotations

import pytest

from engine.broker_paper.ladder import booking_plan
from engine.broker_paper.swing import SwingTrail
from engine.config.schemas import TrailConfig
from tests.conftest import at


@pytest.mark.parametrize(
    ("lots", "plan"),
    [
        (1, (0, 0, 1)),
        (2, (1, 0, 1)),
        (3, (1, 1, 1)),
        (4, (2, 1, 1)),
        (5, (2, 1, 2)),
        (6, (3, 1, 2)),
        (7, (3, 1, 3)),
        (8, (4, 2, 2)),
        (9, (4, 2, 3)),
        (12, (6, 3, 3)),
    ],
)
def test_booking_plan(lots, plan):
    p = booking_plan(lots)
    assert (p.at_first, p.at_second, p.trail) == plan
    assert p.at_first + p.at_second + p.trail == lots and p.trail >= 1


def test_booking_plan_rejects_zero():
    with pytest.raises(ValueError):
        booking_plan(0)


def _feed(trail: SwingTrail, lows: list[float], tf: int = 3) -> None:
    """One candle per value: open/close at value, a single tick each."""
    for i, v in enumerate(lows):
        m = 15 + i * tf
        trail.update(at(9 + m // 60, m % 60), v)


def test_pivot_low_confirmed_after_k_closed_candles():
    t = SwingTrail(TrailConfig(timeframe_min=3, pivot_bars=2, buffer_pct=2))
    _feed(t, [110, 105, 100, 104, 108])
    assert t.last_swing_low is None          # 108 candle still forming -> only 1 right bar closed
    _feed_more = 15 + 5 * 3
    t.update(at(9 + _feed_more // 60, _feed_more % 60), 112)
    assert t.last_swing_low == 100
    assert t.stop_level() == pytest.approx(98.0)


def test_no_pivot_when_right_side_lower():
    t = SwingTrail(TrailConfig(timeframe_min=3, pivot_bars=2, buffer_pct=2))
    _feed(t, [110, 105, 100, 99, 108, 112, 115])
    assert t.last_swing_low == 99


def test_candle_aggregates_intrabar_ticks():
    t = SwingTrail(TrailConfig(timeframe_min=3, pivot_bars=1, buffer_pct=0))
    t.update(at(9, 15, 0), 100)
    t.update(at(9, 16, 0), 95)    # low inside same 3-min candle
    t.update(at(9, 17, 59), 101)
    assert t.current is not None and t.current.low == 95 and t.current.close == 101
