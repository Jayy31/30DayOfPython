from __future__ import annotations

import pytest

from engine.broker_paper.broker import InsufficientCapital, PaperBroker
from engine.broker_paper.position import LegReason, Outcome
from engine.core.models import Direction
from tests.conftest import at
from tests.helpers import NIFTY_CE, NIFTY_PE, SENSEX_CE, q


def test_open_sizes_and_settles_capital(cfg):
    b = PaperBroker(cfg)
    pos = b.open(NIFTY_CE, q(at(9, 45), 100, 100.5), Direction.LONG, setup="orb")
    assert pos.lots == 2 and pos.entry_price == 101.0 and pos.entry_ts == at(9, 45)
    assert pos.capital_required() <= 15000
    b.on_quote(q(at(9, 50), 80))
    assert not b.has_open_position and pos.outcome is Outcome.SL
    assert b.capital == pytest.approx(15000 + pos.net_pnl)


def test_one_position_at_a_time(cfg):
    b = PaperBroker(cfg)
    b.open(NIFTY_CE, q(at(9, 45), 100), Direction.LONG)
    with pytest.raises(RuntimeError):
        b.open(SENSEX_CE, q(at(9, 46), 300), Direction.LONG)


def test_direction_must_match_option_type(cfg):
    with pytest.raises(ValueError):
        PaperBroker(cfg).open(NIFTY_PE, q(at(9, 45), 100), Direction.LONG)


def test_insufficient_capital(cfg):
    with pytest.raises(InsufficientCapital):
        PaperBroker(cfg).open(NIFTY_CE, q(at(9, 45), 400), Direction.LONG)


def test_forced_square_off_at_1515(cfg):
    b = PaperBroker(cfg)
    pos = b.open(NIFTY_CE, q(at(14, 0), 100), Direction.LONG)
    b.on_quote(q(at(15, 14, 59), 105))
    assert b.has_open_position
    legs = b.on_quote(q(at(15, 15), 104))
    assert [leg.reason for leg in legs] == [LegReason.SQUARE_OFF]
    assert legs[0].price == 103.5 and pos.outcome is Outcome.TIME


def test_half_size_fraction(cfg):
    b = PaperBroker(cfg)
    pos = b.open(NIFTY_CE, q(at(9, 45), 50, 50.5), Direction.LONG, size_fraction=0.5)
    assert pos.lots == 2   # 4 lots at full size


def test_no_order_api_in_paper_broker():
    import engine.broker_paper as pkg
    from pathlib import Path

    src = " ".join(p.read_text() for p in Path(pkg.__file__).parent.glob("*.py")).lower()
    for banned in ("place_order", "modify_order", "cancel_order", "neo_api", "requests.", "httpx"):
        assert banned not in src
