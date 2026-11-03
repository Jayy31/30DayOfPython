from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from engine.api.readers import MemoryReader
from engine.api.server import create_api
from engine.api.views import from_record
from engine.metrics.report_data import period_report_data
from engine.store.base import MemoryStore
from tests.trade_factory import LOSS, WIN, make_trade

D1, D2 = date(2026, 11, 2), date(2026, 11, 3)


@pytest.fixture
def store(cfg):
    st = MemoryStore()
    ts = [make_trade(cfg, D1, 1, LOSS), make_trade(cfg, D2, 1, WIN, tags=["event:RBI_POLICY"])]
    for t in ts:
        st.save_trade(t)
    period_report_data(cfg, st, ts, 15000, (D1, D2))
    from engine.store.base import ReportRecord

    st.save_report(ReportRecord("weekly", D1, "Weekly body", {"x": 1}))
    st.save_report(ReportRecord("postmarket", D2, "PM body", {}))
    return st


def client(store, token="secret", snap=None):
    api = create_api(MemoryReader(store), lambda: snap, token=token, starting_capital=15000, cors_origins=["https://dash.example"])
    return TestClient(api)


H = {"Authorization": "Bearer secret"}


def test_health_is_public_and_others_need_token(store):
    c = client(store)
    assert c.get("/health").json()["ok"] is True
    assert c.get("/api/trades").status_code == 401
    assert c.get("/api/trades", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/trades", headers=H, params={"start": "2026-11-01", "end": "2026-11-05"}).status_code == 200


def test_api_is_read_only(store):
    c = client(store)
    for path in ("/api/trades", "/api/today", "/api/metrics", "/health"):
        assert c.post(path, headers=H).status_code == 405


def test_trades_and_reason_logs(store):
    body = client(store).get("/api/trades", headers=H, params={"start": "2026-11-01", "end": "2026-11-05"}).json()
    ts = body["trades"]
    assert [t["day"] for t in ts] == ["2026-11-02", "2026-11-03"]
    assert ts[0]["outcome"] == "sl" and ts[1]["legs"][0]["reason"] == "entry"
    assert ts[1]["reason_log"]["probability"]["label"] == "uncalibrated (priors)"


def test_metrics_and_breakdowns(store):
    b = client(store).get("/api/metrics", headers=H, params={"start": "2026-11-01", "end": "2026-11-05"}).json()
    assert b["metrics"]["trades"] == 2 and "setup" in b["breakdowns"] and b["probabilities"] == "uncalibrated (priors)"
    assert len(b["metrics"]["capital_curve"]) == 2
    later = client(store).get("/api/metrics", headers=H, params={"start": "2026-11-03", "end": "2026-11-05"}).json()
    assert later["starting_capital"] == pytest.approx(15000 + from_record(store.trades[0]).net_pnl)


def test_shadow_comparison(cfg, store):
    b = client(store).get("/api/shadow", headers=H, params={"start": "2026-11-01", "end": "2026-11-05"}).json()
    assert {r["rule"] for r in b["rules"]} == {r.id for r in cfg.shadow.rules}
    assert all(r["trades_covered"] == 2 for r in b["rules"])


def test_reports_filter(store):
    c = client(store)
    assert [r["kind"] for r in c.get("/api/reports", headers=H).json()["reports"]] == ["postmarket", "weekly"]
    assert c.get("/api/reports", headers=H, params={"kind": "weekly"}).json()["reports"][0]["body"] == "Weekly body"
    assert c.get("/api/reports", headers=H, params={"kind": "bogus"}).status_code == 422


def test_bad_range(store):
    assert client(store).get("/api/trades", headers=H, params={"start": "2026-11-05", "end": "2026-11-01"}).status_code == 400


def test_cors_only_for_configured_origin(store):
    c = client(store)
    ok = c.get("/health", headers={"Origin": "https://dash.example"})
    assert ok.headers.get("access-control-allow-origin") == "https://dash.example"
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "https://evil.example"}).headers


def test_today_uses_live_snapshot_day(store):
    snap = {"day": {"date": "2026-11-03"}, "capital": 15100, "open_trade": None, "indices": {}, "signals": [], "last_tick": "x"}
    b = client(store, snap=snap).get("/api/today", headers=H).json()
    assert b["live"]["capital"] == 15100 and [t["day"] for t in b["trades_today"]] == ["2026-11-03"]


def test_live_snapshot_from_replay(cfg):
    import asyncio

    from tests.test_runtime import make_app

    app = make_app(cfg)
    asyncio.run(app.run_live_day(D2))
    snap = app.live.snapshot()
    assert snap["day"]["date"] == "2026-11-03" and set(snap["indices"]) == {"NIFTY", "SENSEX"}
    n = snap["indices"]["NIFTY"]
    assert n["spot"] and n["regime"]["label"] in ("trend", "range", "unclear") and n["gex"] is not None
    assert snap["signals"] and "probability_label" in snap["signals"][0]
    import json

    json.dumps(snap)   # must be JSON-serialisable


@pytest.mark.db
def test_sql_reader_matches_memory(cfg):
    from engine.api.readers import SqlReader
    from engine.config.settings import Settings
    from engine.db.session import make_engine
    from engine.store.sql import SqlStore

    eng = make_engine(Settings().database_url)
    try:
        eng.connect().close()
    except Exception:
        pytest.skip("Postgres not reachable")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    st = SqlStore(eng)
    t = make_trade(cfg, date(2027, 1, 4), 1, WIN, tags=["event:CPI"])
    st.save_trade(t)
    got = SqlReader(eng).trades(date(2027, 1, 4), date(2027, 1, 4))
    assert len(got) == 1
    g, mem = got[0].as_dict(), from_record(t).as_dict()
    for k in ("day", "trade_no", "index", "strike", "lots", "outcome", "net_pnl", "tags"):
        assert g[k] == mem[k], k
    assert len(g["legs"]) == len(mem["legs"])
