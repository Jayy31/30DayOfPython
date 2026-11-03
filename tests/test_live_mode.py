"""Live-data wiring with fakes: jobs, option window re-centring, breadth/VIX, no network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.alerts.notifier import MemoryNotifier
from engine.app import App
from engine.config.settings import AppMode, Settings
from engine.core.models import Index
from engine.data.broker.kotak import KotakDataClient, KotakDataProvider
from engine.data.broker.mock import MockMarketDataProvider
from engine.data.broker.synthetic import default_market
from engine.data.models import InstrumentKind, Tick
from engine.runtime.live import LiveEngine
from engine.scheduler.jobs import JOBS, JobRunner
from engine.state.market_state import IndexState
from engine.store.base import MemoryStore
from tests.conftest import DAY, at
from tests.test_kotak import CREDS, FakeNeo, FakeWs


def live_app(cfg, tmp_path) -> App:
    app = App(Settings(_env_file=None, app_mode=AppMode.LIVE_DATA), cfg, store=MemoryStore(), notifier=MemoryNotifier(), data_dir=tmp_path)
    app._kotak = KotakDataProvider(cfg, KotakDataClient("ck", neo=FakeNeo()), credentials=CREDS, now=lambda: at(10, 0))
    return app


async def test_broker_login_job_builds_universe(cfg, tmp_path):
    app = live_app(cfg, tmp_path)
    assert await JobRunner(app).run(next(j for j in JOBS if j.name == "broker_login"), DAY) == "ok"
    ev = next(e for e in app.store.events if e[1] == "broker_login")
    assert ev[2]["lot_sizes"] == {"NIFTY": 65, "SENSEX": 20} and ev[2]["instruments"] > 20


async def test_premarket_live_has_watchlist(cfg, tmp_path):
    app = live_app(cfg, tmp_path)
    app.overnight = {"score": 0.3}
    await JobRunner(app).run(next(j for j in JOBS if j.name == "premarket"), DAY)
    rep = app.store.reports[("premarket", DAY)]
    n = rep.data["indices"]["NIFTY"]
    assert n["atm"] == 24000 and len(n["watchlist"]) == 5 and n["expected_range"][0] < 24000 < n["expected_range"][1]
    assert rep.data["bias"]["overnight"] == 0.3 and "weight 0" in rep.data["gift_nifty"]


async def test_replay_jobs_stay_offline(cfg, tmp_path):
    app = App(Settings(_env_file=None), cfg, store=MemoryStore(), notifier=MemoryNotifier(), data_dir=tmp_path)
    for name in ("overnight_data", "broker_login", "nse_data", "fii_evening"):
        assert await JobRunner(app).run(next(j for j in JOBS if j.name == name), DAY) == "ok"
    assert sum(e[1] == "job_not_configured" for e in app.store.events) == 4


async def test_option_window_recenters_and_unsubscribes(cfg, tmp_path):
    m = default_market(DAY, step_seconds=60, strikes_each_side=10)
    insts = m.instruments
    exp = next(i.expiry for i in insts if i.kind is InstrumentKind.OPTION and i.underlying is Index.NIFTY)
    st = IndexState(cfg, Index.NIFTY, [i for i in insts if i.underlying is Index.NIFTY], exp)

    class Rec(MockMarketDataProvider):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.sub_log, self.unsub_log = [], []

        async def subscribe(self, xs):
            xs = list(xs)
            self.sub_log.append([x.token for x in xs])
            await super().subscribe(xs)

        async def unsubscribe(self, xs):
            xs = list(xs)
            self.unsub_log.append([x.token for x in xs])
            await super().unsubscribe(xs)

    prov = Rec([], insts)
    app = App(Settings(_env_file=None), cfg, store=MemoryStore(), notifier=MemoryNotifier(), data_dir=tmp_path)
    eng = LiveEngine(cfg, prov, {Index.NIFTY: st}, app.decision, app.risk, app.broker, app.notifier, app.store,  # type: ignore[arg-type]
                     option_window=2, initial_centers={Index.NIFTY: 24000})
    first = eng._initial_subscriptions(insts)
    opt_strikes = sorted({i.strike for i in first if i.kind is InstrumentKind.OPTION})
    assert opt_strikes == [23900, 23950, 24000, 24050, 24100]
    assert any(i.kind is InstrumentKind.INDEX for i in first) and any(i.kind is InstrumentKind.FUTURE for i in first)
    st.spot = 24070   # < 3 strikes away: no change
    await eng._rewindow()
    assert prov.sub_log == []
    st.spot = 24160   # >= 3 strikes: recentre on 24150
    st.options["NIFTY-" + exp.strftime("%y%m%d") + "-23900-CE"] = Tick(ts=at(10, 0), token="x", ltp=1)
    await eng._rewindow()
    added = {t.split("-")[2] for t in prov.sub_log[0]}
    removed = {t.split("-")[2] for t in prov.unsub_log[0]}
    assert added == {"24150", "24200", "24250"} and removed == {"23900", "23950", "24000"}
    assert not any("-23900-" in k for k in st.options)


def test_breadth_and_vix_feed_into_context(cfg, tmp_path):
    m = default_market(DAY, step_seconds=60, strikes_each_side=2)
    insts = m.instruments
    exp = next(i.expiry for i in insts if i.kind is InstrumentKind.OPTION and i.underlying is Index.NIFTY)
    st = IndexState(cfg, Index.NIFTY, [i for i in insts if i.underlying is Index.NIFTY], exp)
    st.spot, st.prev_close = 24120.0, 24000.0
    app = App(Settings(_env_file=None), cfg, store=MemoryStore(), notifier=MemoryNotifier(), data_dir=tmp_path)
    eng = LiveEngine(cfg, MockMarketDataProvider([], insts), {Index.NIFTY: st}, app.decision, app.risk, app.broker, app.notifier, app.store,  # type: ignore[arg-type]
                     heavyweights={"nse_cm|1333": ("HDFCBANK", 13.0), "nse_cm|2885": ("RELIANCE", 8.5)}, vix_token="nse_cm|INDIA VIX")
    assert eng._breadth(st) is None
    eng.equity_px = {"nse_cm|1333": (1010.0, 1000.0), "nse_cm|2885": (2970.0, 3000.0)}
    b = eng._breadth(st)
    assert b is not None and b.index_change_pct == pytest.approx(0.5)
    assert b.weighted_change_pct == pytest.approx((13 * 1.0 + 8.5 * -1.0) / 21.5)
