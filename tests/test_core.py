from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timezone

import pytest

from engine.core.logs import JsonFormatter
from engine.core.models import Direction, OptionQuote, OptionType
from engine.core.timeutils import IST, floor_to_minutes, in_window, to_ist
from engine.core.trading_calendar import TradingCalendar
from tests.conftest import at


def test_to_ist_converts_and_rejects_naive():
    utc = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)
    assert to_ist(utc).time() == time(9, 30)
    with pytest.raises(ValueError):
        to_ist(datetime(2026, 11, 3, 9, 30))


def test_window_half_open():
    assert in_window(time(9, 30), time(9, 30), time(11, 30))
    assert not in_window(time(11, 30), time(9, 30), time(11, 30))


def test_floor_to_candle_aligned_to_open():
    assert floor_to_minutes(at(9, 17, 59), 3) == at(9, 15)
    assert floor_to_minutes(at(9, 18), 3) == at(9, 18)
    assert floor_to_minutes(at(10, 2, 30), 5) == at(10, 0)


def test_calendar():
    cal = TradingCalendar({2026: ["2026-11-04"]})
    assert cal.is_trading_day(date(2026, 11, 3))
    assert not cal.is_trading_day(date(2026, 11, 4))        # holiday
    assert not cal.is_trading_day(date(2026, 11, 7))        # Saturday
    assert cal.next_trading_day(date(2026, 11, 3)) == date(2026, 11, 5)
    assert cal.previous_trading_day(date(2026, 11, 9)) == date(2026, 11, 6)


def test_quote_requires_aware_ts():
    with pytest.raises(ValueError):
        OptionQuote(ts=datetime(2026, 11, 3, 10, 0), bid=1, ask=1.1, ltp=1)
    q = OptionQuote(ts=datetime(2026, 11, 3, 4, 30, tzinfo=timezone.utc), bid=100, ask=101, ltp=100.5)
    assert q.ts.tzinfo == IST and q.spread == 1


def test_direction_maps_to_option_type():
    assert Direction.LONG.option_type is OptionType.CE
    assert Direction.SHORT.option_type is OptionType.PE


def test_json_log_line():
    rec = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "hi", "trade_id": "x"})
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "hi" and out["trade_id"] == "x" and out["ts"].endswith("+05:30")
