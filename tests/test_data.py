from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.broker_paper.broker import PaperBroker
from engine.config.settings import AppMode, Settings
from engine.core.models import Direction, Index, OptionContract, OptionType
from engine.data.broker.base import FeedError, MarketDataProvider, assert_data_only
from engine.data.broker.factory import create_provider
from engine.data.broker.mock import MockMarketDataProvider
from engine.data.broker.synthetic import bs_price, default_market
from engine.data.feed_monitor import FeedMonitor
from engine.data.models import InstrumentKind
from engine.data.storage import ParquetTickStore
from tests.conftest import DAY, at


@pytest.fixture(scope="module")
def market():
    return default_market(DAY, step_seconds=60, strikes_each_side=2)


@pytest.fixture(scope="module")
def ticks(market):
    return list(market.ticks())


def test_bs_put_call_parity():
    s, k, t, r = 24000, 24000, 7 / 365, 0.065
    import math
    c = bs_price(s, k, t, 0.14, OptionType.CE, r)
    p = bs_price(s, k, t, 0.14, OptionType.PE, r)
    assert c - p == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)


def test_synthetic_is_deterministic_and_sane(market, ticks):
    assert ticks == list(default_market(DAY, step_seconds=60, strikes_each_side=2).ticks())
    opts = [t for t in ticks if "-IDX" not in t.token and "-FUT" not in t.token]
    assert all(t.bid < t.ask and t.bid >= 0.05 for t in opts)
    assert all(t.ts.date() == DAY for t in ticks)
    assert ticks[0].ts == at(9, 15) and ticks[-1].ts == at(15, 30)
    # 2 indices x (spot + fut + 5 strikes x 2) per minute
    assert len(market.instruments) == 2 * (2 + 10)


def test_lot_sizes_and_expiries(market):
    nifty_opts = [i for i in market.instruments if i.underlying is Index.NIFTY and i.kind is InstrumentKind.OPTION]
    assert {i.lot_size for i in nifty_opts} == {65}
    assert {i.expiry for i in nifty_opts} == {DAY + timedelta(days=7)}


async def test_subscription_filters_stream(market, ticks):
    p = MockMarketDataProvider(ticks, market.instruments)
    await p.connect()
    spot = next(i for i in market.instruments if i.token == "NIFTY-IDX")
    await p.subscribe([spot])
    got = [t async for t in p.ticks()]
    assert {t.token for t in got} == {"NIFTY-IDX"} and len(got) == 376


async def test_not_connected_raises(market, ticks):
    p = MockMarketDataProvider(ticks, market.instruments)
    with pytest.raises(FeedError):
        async for _ in p.ticks():
            pass


async def test_feed_drop_keeps_rest_quotes(market, ticks):
    p = MockMarketDataProvider(ticks, market.instruments, feed_drops=[(at(10, 0), at(10, 10))])
    await p.connect()
    spot = next(i for i in market.instruments if i.token == "NIFTY-IDX")
    await p.subscribe([spot])
    mon = FeedMonitor(threshold_seconds=120)
    seen = []
    async for t in p.ticks():
        mon.on_tick(t.ts)
        seen.append(t.ts)
        if t.ts == at(10, 10):
            break
    assert not any(at(10, 0) <= ts < at(10, 10) for ts in seen)
    assert mon.gaps and mon.gaps[0].start == at(9, 59) and mon.gaps[0].end == at(10, 10)


async def test_option_chain_and_candles(market, ticks):
    p = MockMarketDataProvider(ticks, market.instruments)
    await p.connect()
    async for _ in p.ticks():
        pass
    exp = (await p.expiries(Index.NIFTY, DAY))[0]
    chain = await p.option_chain(Index.NIFTY, exp)
    assert len(chain.rows) == 10 and chain.row(chain.atm_strike(), OptionType.CE) is not None
    spot = next(i for i in market.instruments if i.token == "NIFTY-IDX")
    candles = await p.candles(spot, at(9, 15), at(10, 15), 15)
    assert len(candles) == 4 and all(c.low <= c.open <= c.high for c in candles)


def test_parquet_roundtrip_and_idempotent(tmp_path, ticks):
    store = ParquetTickStore(tmp_path)
    n = store.write_day(DAY, ticks)
    assert store.write_day(DAY, ticks) == n             # rerun replaces, no duplicates
    back = store.read_day(DAY)
    assert len(back) == n and back[0].ts.tzinfo is not None
    assert {(t.ts, t.token, t.ltp) for t in back} == {(t.ts, t.token, t.ltp) for t in ticks}
    assert {t.token for t in store.read_day(DAY, {"NIFTY-IDX"})} == {"NIFTY-IDX"}
    assert store.days() == [DAY]
    assert store.read_day(date(2026, 11, 4)) == []


def test_parquet_rejects_wrong_day(tmp_path, ticks):
    with pytest.raises(ValueError):
        ParquetTickStore(tmp_path).write_day(date(2026, 11, 4), ticks[:5])


def test_data_only_guard():
    class Bad:
        def place_order(self) -> None: ...

    with pytest.raises(RuntimeError):
        assert_data_only(Bad())
    assert not hasattr(MarketDataProvider, "place_order")


def test_factory_modes(tmp_path):
    assert isinstance(create_provider(Settings(_env_file=None), DAY, step_seconds=900), MockMarketDataProvider)
    with pytest.raises(NotImplementedError):
        create_provider(Settings(_env_file=None, app_mode=AppMode.LIVE_DATA), DAY)


async def test_end_to_end_replay_drives_paper_broker(cfg, market, ticks):
    """Replay a day: enter an ATM CE at 09:45, manage per tick, square off by 15:15."""
    p = MockMarketDataProvider(ticks, market.instruments)
    await p.connect()
    opt = next(i for i in market.instruments if i.underlying is Index.NIFTY and i.kind is InstrumentKind.OPTION and i.option_type is OptionType.CE and i.strike == 24000)
    await p.subscribe([opt])
    contract = OptionContract(index=Index.NIFTY, expiry=opt.expiry, strike=opt.strike, option_type=OptionType.CE, lot_size=opt.lot_size, symbol=opt.symbol)
    broker = PaperBroker(cfg)
    pos = None
    async for t in p.ticks():
        if pos is None and t.ts >= at(9, 45):
            pos = broker.open(contract, t.to_quote(), Direction.LONG, setup="replay_test")
        elif pos is not None:
            broker.on_quote(t.to_quote())
            if not broker.has_open_position:
                break
    assert pos is not None and not pos.is_open and pos.outcome is not None
    assert pos.exit_ts is not None and pos.exit_ts <= at(15, 15)
    assert broker.capital == pytest.approx(15000 + pos.net_pnl)
