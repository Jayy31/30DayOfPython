"""Read-only dashboard API. No endpoint can change trading state or place anything."""

from __future__ import annotations

import hmac
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from engine.api.readers import Reader
from engine.core.timeutils import now_ist
from engine.metrics.compute import breakdowns, compute

Snapshot = Callable[[], dict[str, Any] | None]


def _range(start: date | None, end: date | None, default_days: int = 30) -> tuple[date, date]:
    end = end or now_ist().date()
    start = start or end - timedelta(days=default_days)
    if start > end:
        raise HTTPException(400, "start after end")
    return start, end


def create_api(reader: Reader, snapshot: Snapshot, *, token: str | None, starting_capital: float, cors_origins: list[str] | None = None, mode: str = "replay") -> FastAPI:
    api = FastAPI(title="papertrade engine API", version="0.1.0", docs_url=None, redoc_url=None)
    api.add_middleware(CORSMiddleware, allow_origins=cors_origins or [], allow_methods=["GET"], allow_headers=["Authorization"])

    def auth(request: Request) -> None:
        if not token:
            return   # local replay without a token configured
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header, f"Bearer {token}"):
            raise HTTPException(401, "unauthorized")

    @api.get("/health")
    def health() -> dict[str, Any]:
        snap = snapshot()
        return {"ok": True, "mode": mode, "live": snap is not None, "last_tick": snap.get("last_tick") if snap else None}

    @api.get("/api/today", dependencies=[Depends(auth)])
    def today() -> dict[str, Any]:
        snap = snapshot()
        d = date.fromisoformat(snap["day"]["date"]) if snap and snap.get("day") else now_ist().date()   # replay days aren't 'today'
        trades = reader.trades(d, d)
        return {"mode": mode, "live": snap, "trades_today": [t.as_dict() for t in trades]}

    @api.get("/api/trades", dependencies=[Depends(auth)])
    def trades(start: date | None = None, end: date | None = None) -> dict[str, Any]:
        s, e = _range(start, end)
        return {"start": s.isoformat(), "end": e.isoformat(), "trades": [t.as_dict() for t in reader.trades(s, e)]}

    @api.get("/api/reports", dependencies=[Depends(auth)])
    def reports(kind: str | None = Query(None, pattern="^(premarket|postmarket|weekly|monthly)$"), limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
        return {"reports": reader.reports(kind, limit)}

    @api.get("/api/metrics", dependencies=[Depends(auth)])
    def metrics(start: date | None = None, end: date | None = None) -> dict[str, Any]:
        s, e = _range(start, end, 3650)
        ts = reader.trades(s, e)
        prior = reader.trades(date(2000, 1, 1), s - timedelta(days=1)) if s > date(2000, 1, 2) else []
        cap0 = starting_capital + sum(t.net_pnl for t in prior)
        return {
            "start": s.isoformat(), "end": e.isoformat(), "starting_capital": cap0,
            "metrics": compute(ts, cap0).as_dict(), "breakdowns": breakdowns(ts, cap0),  # type: ignore[arg-type]
            "sample": {"trades_total": len(prior) + len(ts), "min_to_judge": 100}, "probabilities": "uncalibrated (priors)",
        }

    @api.get("/api/shadow", dependencies=[Depends(auth)])
    def shadow(start: date | None = None, end: date | None = None) -> dict[str, Any]:
        s, e = _range(start, end, 3650)
        ts = reader.trades(s, e)
        actual = round(sum(t.net_pnl for t in ts), 2)
        rows = reader.shadow([t.id for t in ts])
        agg: dict[str, dict[str, float]] = defaultdict(lambda: {"simulated_pnl": 0.0, "changed": 0, "trades": 0})
        for r in rows:
            a = agg[r["rule_id"]]
            a["simulated_pnl"] += r["simulated_pnl"]
            a["changed"] += int(r["active"])
            a["trades"] += 1
        return {
            "start": s.isoformat(), "end": e.isoformat(), "actual_pnl": actual, "trades": len(ts),
            "rules": [{"rule": k, "simulated_pnl": round(v["simulated_pnl"], 2), "difference": round(v["simulated_pnl"] - actual, 2), "trades_changed": int(v["changed"]), "trades_covered": int(v["trades"])} for k, v in sorted(agg.items())],
            "note": "Shadow results are written when the weekly or monthly report runs",
        }

    return api
