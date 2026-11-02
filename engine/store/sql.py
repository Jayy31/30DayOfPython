"""SQL store: writes the core tables. Upserts on natural keys keep jobs idempotent."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from engine.broker_paper.position import Position
from engine.db import models as m
from engine.db.session import session_scope
from engine.decision.engine import SignalRecord
from engine.store.base import MemoryStore, ReportRecord, Store, TradeRecord


def _d(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, 2)))


def _json(x: Any) -> Any:
    return json.loads(json.dumps(x, default=str))


class SqlStore(Store):
    """Persists to Postgres. Reads of trades for reports come back from an in-process cache
    of this run plus DB rows summarised (positions are not rehydrated)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._mem = MemoryStore()

    def save_signals(self, signals: list[SignalRecord]) -> None:
        self._mem.save_signals(signals)
        with session_scope(self.engine) as s:
            for sig in signals:
                p = sig.probability
                s.add(m.Signal(
                    ts=sig.ts, index=sig.index.value, setup=sig.setup, direction=sig.direction.value,
                    layers=_json(sig.layers), score=sig.score, probability=p.p if p else None,
                    probability_calibrated=bool(p and p.calibrated), prior_version=p.model_version if p else None,
                    taken=sig.taken, reject_reason="; ".join(sig.reject_reasons) or None,
                ))

    def save_trade(self, rec: TradeRecord) -> None:
        self._mem.save_trade(rec)
        pos: Position = rec.position
        c = pos.contract
        with session_scope(self.engine) as s:
            existing = s.scalar(select(m.Trade).where(m.Trade.day == rec.day, m.Trade.trade_no == rec.trade_no))
            if existing is not None:
                s.execute(m.CapitalLedger.__table__.delete().where(m.CapitalLedger.trade_id == existing.id))
                s.delete(existing)
                s.flush()
            t = m.Trade(
                id=pos.id, day=rec.day, trade_no=rec.trade_no, index=c.index.value, symbol=c.key, strike=_d(c.strike),
                option_type=c.option_type.value, expiry=c.expiry, lots=pos.lots, lot_size=c.lot_size,
                direction=pos.direction.value, setup=rec.setup, entry_ts=pos.entry_ts, entry_price=_d(pos.entry_price),
                initial_sl=_d(pos.initial_sl), exit_ts=pos.exit_ts, avg_exit_price=_d(pos.avg_exit_price),
                outcome=pos.outcome.value if pos.outcome else None, reached_1r=pos.reached_1r,
                gross_pnl=_d(pos.gross_pnl), charges=_d(pos.total_charges), net_pnl=_d(pos.net_pnl),
                r_multiple=pos.r_multiple, probability=pos.probability,
                prior_version=(rec.reason_log.get("probability") or {}).get("model_version"), tags=list(pos.tags),
            )
            t.legs = [m.TradeLeg(ts=leg.ts, side=leg.side.value, lots=leg.lots, price=_d(leg.price), reason=leg.reason.value, charges=_d(leg.charges)) for leg in pos.legs]
            s.add(t)
            s.add(m.TradeReason(trade_id=pos.id, reasons=_json(rec.reason_log), confidence=rec.reason_log.get("confidence"), review=rec.reason_log.get("review")))
            s.flush()
            s.execute(insert(m.CapitalLedger).values(ts=pos.exit_ts or pos.entry_ts, trade_id=pos.id, change=_d(pos.net_pnl), balance=_d(rec.capital_after)).on_conflict_do_nothing(index_elements=["trade_id"]))

    def trades_between(self, start: date, end: date) -> list[TradeRecord]:
        return self._mem.trades_between(start, end)

    def save_report(self, rep: ReportRecord) -> None:
        self._mem.save_report(rep)
        with session_scope(self.engine) as s:
            stmt = insert(m.Report).values(kind=rep.kind, period=rep.period, body=rep.body, data=_json(rep.data))
            s.execute(stmt.on_conflict_do_update(constraint="uq_reports_kind", set_={"body": stmt.excluded.body, "data": stmt.excluded.data}))

    def save_event(self, ts: datetime, kind: str, detail: dict[str, Any]) -> None:
        self._mem.save_event(ts, kind, detail)
        with session_scope(self.engine) as s:
            s.add(m.SystemEvent(ts=ts, kind=kind, detail=_json(detail)))

    def job_done(self, job: str, day: date) -> bool:
        with session_scope(self.engine) as s:
            return s.scalar(select(m.SystemEvent.id).where(m.SystemEvent.kind == "job_done", m.SystemEvent.detail["job"].astext == job, m.SystemEvent.detail["day"].astext == day.isoformat())) is not None

    def mark_job(self, job: str, day: date) -> None:
        with session_scope(self.engine) as s:
            s.add(m.SystemEvent(ts=datetime.now().astimezone(), kind="job_done", detail={"job": job, "day": day.isoformat()}))
