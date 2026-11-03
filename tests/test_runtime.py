from __future__ import annotations

from datetime import date, datetime, time, timedelta

import httpx
import pytest

from engine.alerts.notifier import MemoryNotifier, TelegramNotifier, split_message
from engine.app import App
from engine.broker_paper.position import Outcome
from engine.config.settings import Settings
from engine.core.models import Direction, Index, OptionContract
from engine.core.timeutils import IST
from engine.data.broker.mock import MockMarketDataProvider
from engine.data.broker.synthetic import default_market
from engine.data.models import InstrumentKind
from engine.llm.mock import MockProvider
from engine.llm.service import LLMService
from engine.llm.usage import UsageLedger
from engine.reports.builder import build_report, trade_summary
from engine.runtime.live import LiveEngine
from engine.scheduler.jobs import JOBS, Job, JobRunner, build_scheduler, next_runs
from engine.state.market_state import IndexState
from engine.store.base import MemoryStore
from tests.conftest import DAY, at


def make_app(cfg, **kw) -> App:
    app = App(Settings(_env_file=None), cfg, store=MemoryStore(), notifier=MemoryNotifier(), replay_step_seconds=60, **kw)
    app.tick_store = None
    return app


@pytest.fixture(scope="module")
def replayed(cfg):
    import asyncio

    app = make_app(cfg)
    res = asyncio.run(app.run_live_day(DAY))
    return app, res


# ------------------------------------------------------------------ state
def test_index_state_builds_context(cfg):
    m = default_market(DAY, step_seconds=60)
    insts = [i for i in m.instruments if i.underlying is Index.NIFTY]
    exp = next(i.expiry for i in insts if i.kind is InstrumentKind.OPTION)
    st = IndexState(cfg, Index.NIFTY, insts, exp)
    for t in m.ticks():
        if t.ts > at(10, 30):
            break
        st.on_tick(t)
        if t.ts.second == 0 and t.token == "NIFTY-IDX":
            st.refresh(t.ts)
    ctx = st.context(at(10, 30), {"news": None})
    assert ctx is not None and ctx.or_complete and ctx.or_high >= ctx.or_low
    assert st.opening is not None and st.ib is not None and st.gex is not None and st.oi is not None
    assert st.vwap.value is not None and ctx.regime.label in ("trend", "range", "unclear")
    assert all(z.source_count >= cfg.decision.location.min_sources for z in ctx.zones)
    chain = st.chain(at(10, 30))
    assert chain is not None and len(chain.rows) == 42 and st.lot_size == 65


# ------------------------------------------------------------------ live engine
def test_replay_day_respects_locked_rules(cfg, replayed):
    app, res = replayed
    assert res.ticks > 30000 and res.signals > 0
    assert len(res.trades) <= cfg.rules.max_trades_per_day
    windows = cfg.rules.entry_windows
    for t in res.trades:
        p = t.position
        assert any(s <= p.entry_ts.time() < e for s, e in windows)
        assert p.exit_ts is not None and p.exit_ts.time() <= time(15, 15) and not p.is_open
        assert p.legs[0].price > 0 and p.probability is not None
        assert t.reason_log["probability"]["label"] == "uncalibrated (priors)"
        assert "exit" in t.reason_log
    if len(res.trades) == 2:
        first, second = res.trades
        assert first.position.outcome is not Outcome.TARGET
        assert second.position.entry_ts >= first.position.exit_ts
        if first.position.outcome is Outcome.SL:
            assert second.position.probability >= cfg.rules.min_probability_after_sl
    assert app.broker.capital == pytest.approx(15000 + sum(t.net_pnl for t in res.trades), abs=0.05)
    msgs = app.notifier.messages
    assert sum(m.startswith("ENTRY") for m in msgs) == len(res.trades) == sum(m.startswith("EXIT") for m in msgs)
    assert len(app.store.signals) == res.signals and len(app.store.trades) == len(res.trades)


async def test_watchdog_manages_trade_on_rest_quotes(cfg):
    m = default_market(DAY, step_seconds=60)
    insts = m.instruments
    ticks = [t for t in m.ticks() if t.ts <= at(10, 0)]
    provider = MockMarketDataProvider(ticks, insts)
    await provider.connect()
    app = make_app(cfg)
    exp = next(i.expiry for i in insts if i.kind is InstrumentKind.OPTION and i.underlying is Index.NIFTY)
    st = IndexState(cfg, Index.NIFTY, [i for i in insts if i.underlying is Index.NIFTY], exp)
    eng = LiveEngine(cfg, provider, {Index.NIFTY: st}, app.decision, app.risk, app.broker, app.notifier, app.store)  # type: ignore[arg-type]
    await provider.subscribe(insts)
    async for _ in provider.ticks():
        pass   # provider REST view is now at 10:00
    ces = [i for i in insts if i.kind is InstrumentKind.OPTION and i.underlying is Index.NIFTY and i.option_type.value == "CE"]
    opt = None
    for i in sorted(ces, key=lambda i: i.strike):
        if (await provider.quote(i)).ask * 65 < 13000:
            opt = i
            break
    assert opt is not None
    last = await provider.quote(opt)
    contract = OptionContract(index=Index.NIFTY, expiry=opt.expiry, strike=opt.strike, option_type=opt.option_type, lot_size=opt.lot_size, symbol=opt.token)
    app.broker.open(contract, last.to_quote(), Direction.LONG)
    app.risk.register_open(last.ts)
    eng.monitor.on_tick(at(10, 0))
    await eng.watchdog(at(10, 0, 5))          # not stale yet
    assert not app.notifier.messages
    await eng.watchdog(at(10, 1))             # 60s silence -> alert + REST management
    assert any("feed silent" in m for m in app.notifier.messages)
    assert any(e[1] == "feed_drop" for e in app.store.events)
    assert eng._last_quote is not None
    await eng.watchdog(at(15, 15))            # still silent at square-off -> closes on REST quote
    assert not app.broker.has_open_position


# ------------------------------------------------------------------ alerts
def test_split_message():
    parts = split_message("a" * 9000, 4000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]
    assert split_message("x\ny\n", 4000) == ["x\ny\n"]


async def test_telegram_sends_and_swallows_errors():
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200 if len(sent) == 1 else 500, json={"ok": True})

    tg = TelegramNotifier("TOKEN", "42", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await tg.send("hello") is True
    assert "botTOKEN/sendMessage" in str(sent[0].url) and b'"chat_id":"42"' in sent[0].content.replace(b" ", b"")
    assert await tg.send("again") is False   # no exception


# ------------------------------------------------------------------ reports
async def test_report_llm_and_template(cfg, replayed):
    app, res = replayed
    data = {"summary": trade_summary(res.trades), "capital": app.broker.capital}
    tmpl = await build_report("postmarket", DAY, data, None)
    assert tmpl.data["narrative_by"] == "template" and "Trades" in tmpl.body
    llm = LLMService(MockProvider(queue=[{"title": "Post-market", "body": "Quiet day."}]), cfg.llm, UsageLedger(cfg.llm.pricing, 40), "claude-haiku-5-5", "claude-sonnet-5-5")
    rep = await build_report("postmarket", DAY, data, llm)
    assert rep.data["narrative_by"] == "llm" and rep.body.startswith("Post-market")
    assert llm.provider.calls[0]["model"] == "claude-sonnet-5-5"


# ------------------------------------------------------------------ scheduler
async def test_job_runner_holiday_idempotency_and_failure(cfg):
    app = make_app(cfg)
    calls = []

    async def body(a, d):
        calls.append(d)

    job = Job("t", {"hour": "8"}, body)
    runner = JobRunner(app)
    assert await runner.run(job, date(2026, 11, 7)) == "skipped_holiday"      # Saturday
    assert await runner.run(job, DAY) == "ok"
    assert await runner.run(job, DAY) == "skipped_done"
    assert await runner.run(job, DAY, force=True) == "ok" and len(calls) == 2

    async def boom(a, d):
        raise RuntimeError("x")

    assert await runner.run(Job("b", {"hour": "8"}, boom), DAY) == "failed"
    assert any("job b failed" in m for m in app.notifier.messages)
    weekend_job = next(j for j in JOBS if j.name == "weekly")
    assert await runner.run(weekend_job, date(2026, 11, 7)) == "ok"


def test_scheduler_times_in_ist(cfg):
    sched = build_scheduler(make_app(cfg))
    now = datetime(2026, 11, 2, 20, 0, tzinfo=IST)   # Monday evening
    runs = next_runs(sched, now)
    assert runs["premarket"] == datetime(2026, 11, 3, 8, 45, tzinfo=IST)
    assert runs["square_off"].time() == time(15, 15) and runs["postmarket"].time() == time(16, 0)
    assert runs["weekly"].weekday() == 5 and runs["monthly"].day == 1
    assert {j.name for j in JOBS} == set(runs)


async def test_postmarket_job_saves_report(cfg, replayed):
    app, _ = replayed
    await JobRunner(app).run(next(j for j in JOBS if j.name == "postmarket"), DAY, force=True)
    rep = app.store.reports[("postmarket", DAY)]
    assert rep.data["summary"]["trades"] == len(app.store.trades)


async def test_weekly_job_includes_metrics_and_shadow(cfg):
    from tests.trade_factory import LOSS, WIN, make_trade

    app = make_app(cfg)
    for d, no, path in ((date(2026, 11, 2), 1, LOSS), (date(2026, 11, 3), 1, WIN)):
        app.store.save_trade(make_trade(cfg, d, no, path))
    status = await JobRunner(app).run(next(j for j in JOBS if j.name == "weekly"), date(2026, 11, 7))
    assert status == "ok"
    rep = app.store.reports[("weekly", date(2026, 11, 2))]
    assert rep.data["metrics"]["trades"] == 2 and rep.data["shadow_rules"]
    assert "Shadow half_sizing" in rep.body and "too few to judge" in rep.body
    assert "llm_usage" in rep.data
