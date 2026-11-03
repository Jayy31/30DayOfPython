from __future__ import annotations

from datetime import date

import pytest

from engine.broker_paper.position import Outcome
from engine.core.models import Direction, Index
from engine.core.trading_calendar import TradingCalendar
from engine.risk.engine import RiskEngine
from tests.conftest import at

L, S = Direction.LONG, Direction.SHORT
N, X = Index.NIFTY, Index.SENSEX


@pytest.fixture
def risk(cfg) -> RiskEngine:
    return RiskEngine(cfg.rules, TradingCalendar(cfg.holidays))


def close(risk: RiskEngine, t, idx, d, outcome):
    risk.register_open(t)
    risk.register_close(t, idx, d, outcome)


@pytest.mark.parametrize(
    ("hh", "mm", "ok"),
    [
        (9, 15, False), (9, 29, False), (9, 30, True), (11, 29, True), (11, 30, False),
        (12, 30, False), (13, 30, True), (14, 29, True), (14, 30, False), (15, 0, False),
    ],
)
def test_entry_windows(risk, hh, mm, ok):
    assert risk.check_entry(at(hh, mm), N, L, 0.9).allowed is ok


def test_probability_threshold(risk):
    assert not risk.check_entry(at(10, 0), N, L, 0.59).allowed
    assert risk.check_entry(at(10, 0), N, L, 0.60).allowed


def test_weekend_and_holiday_blocked(cfg):
    r = RiskEngine(cfg.rules, TradingCalendar({2026: ["2026-11-03"]}))
    assert "not a trading day" in r.check_entry(at(10, 0), N, L, 0.9).reasons
    assert not r.check_entry(at(10, 0, d=date(2026, 11, 7)), N, L, 0.9).allowed


def test_open_trade_blocks_new_entry(risk):
    risk.register_open(at(9, 40))
    d = risk.check_entry(at(10, 0), X, L, 0.9)
    assert not d.allowed and "a trade is already open" in d.reasons


def test_target_stops_the_day(risk):
    close(risk, at(10, 0), N, L, Outcome.TARGET)
    d = risk.check_entry(at(13, 45), X, S, 0.99)
    assert not d.allowed and any("target hit" in r for r in d.reasons)


def test_after_sl_needs_070_any_direction_any_index(risk):
    close(risk, at(10, 0), N, L, Outcome.SL)
    for idx, d in [(N, L), (N, S), (X, L), (X, S)]:
        assert not risk.check_entry(at(10, 30), idx, d, 0.69).allowed
        dec = risk.check_entry(at(10, 30), idx, d, 0.70)
        assert dec.allowed and dec.required_probability == 0.70


def test_index_switch_same_direction_tagged(risk):
    close(risk, at(10, 0), N, L, Outcome.SL)
    tags = risk.check_entry(at(10, 30), X, L, 0.8).tags
    assert {"after_sl", "same_direction_after_sl", "index_switch_same_bet", "trade_2"} <= set(tags)
    assert "index_switch_same_bet" not in risk.check_entry(at(10, 30), X, S, 0.8).tags


def test_max_two_trades(risk):
    close(risk, at(9, 45), N, L, Outcome.SL)
    close(risk, at(10, 15), X, S, Outcome.SL)
    d = risk.check_entry(at(13, 45), N, L, 0.99)
    assert not d.allowed and "daily trade limit reached" in d.reasons


def test_time_exit_keeps_normal_threshold(risk):
    close(risk, at(10, 0), N, L, Outcome.TIME)
    assert risk.required_probability(at(10, 5)) == 0.60


def test_new_day_resets(risk):
    close(risk, at(10, 0), N, L, Outcome.TARGET)
    assert risk.check_entry(at(10, 0, d=date(2026, 11, 4)), N, L, 0.6).allowed


def test_overnight_open_trade_refused(risk):
    risk.register_open(at(14, 0))
    with pytest.raises(RuntimeError, match="overnight"):
        risk.check_entry(at(9, 45, d=date(2026, 11, 4)), N, L, 0.9)


def test_must_square_off(risk):
    assert not risk.must_square_off(at(15, 14, 59))
    assert risk.must_square_off(at(15, 15))


def test_day_closed(risk):
    assert not risk.day_closed(at(10, 0))
    close(risk, at(10, 0), N, L, Outcome.SL)
    assert not risk.day_closed(at(10, 5))
    close(risk, at(10, 30), X, S, Outcome.TARGET)
    assert risk.day_closed(at(10, 35))
