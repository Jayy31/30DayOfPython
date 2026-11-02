from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text

from engine.config.settings import Settings
from engine.db import models as m
from engine.db.session import make_engine, session_scope
from engine.store.base import ReportRecord
from engine.store.sql import SqlStore
from tests.conftest import DAY, at

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def engine():
    eng = make_engine(Settings().database_url)
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    with eng.begin() as c:
        c.execute(text("TRUNCATE trades, trade_legs, trade_reasons, capital_ledger, signals, reports, system_events CASCADE"))
    return eng


def test_sql_store_persists_replay(cfg, engine):
    from engine.alerts.notifier import MemoryNotifier
    from engine.app import App

    store = SqlStore(engine)
    app = App(Settings(_env_file=None), cfg, store=store, notifier=MemoryNotifier(), replay_step_seconds=60)
    app.tick_store = None
    res = asyncio.run(app.run_live_day(DAY))
    with session_scope(engine) as s:
        assert s.scalar(select(func.count()).select_from(m.Signal)) == res.signals
        assert s.scalar(select(func.count()).select_from(m.Trade)) == len(res.trades)
        if res.trades:
            t = s.scalar(select(m.Trade).order_by(m.Trade.trade_no))
            assert t.prior_version == "priors-v1" and t.outcome is not None and len(t.legs) >= 2
            assert s.scalar(select(func.count()).select_from(m.CapitalLedger)) == len(res.trades)
    if res.trades:
        store.save_trade(res.trades[0])   # rerun: replaces, no duplicate
        with session_scope(engine) as s:
            assert s.scalar(select(func.count()).select_from(m.Trade)) == len(res.trades)


def test_report_upsert_and_job_marks(engine):
    store = SqlStore(engine)
    store.save_report(ReportRecord("weekly", DAY, "v1", {"a": 1}))
    store.save_report(ReportRecord("weekly", DAY, "v2", {"a": 2}))
    with session_scope(engine) as s:
        rows = s.scalars(select(m.Report).where(m.Report.kind == "weekly")).all()
        assert len(rows) == 1 and rows[0].body == "v2"
    assert not store.job_done("premarket", DAY)
    store.mark_job("premarket", DAY)
    assert store.job_done("premarket", DAY)
    store.save_event(at(10, 0), "feed_drop", {"x": 1})
