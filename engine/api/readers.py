"""History readers for the API: from the in-process store (replay) or from Postgres/Supabase."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import selectinload

from engine.api.views import ContractView, OutcomeView, PositionView, TradeView, from_record
from engine.core.models import Index
from engine.db import models as m
from engine.db.session import session_scope
from engine.store.base import MemoryStore


class Reader(ABC):
    @abstractmethod
    def trades(self, start: date, end: date) -> list[TradeView]: ...

    @abstractmethod
    def reports(self, kind: str | None, limit: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    def shadow(self, trade_ids: list[str]) -> list[dict[str, Any]]: ...


class MemoryReader(Reader):
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def trades(self, start: date, end: date) -> list[TradeView]:
        return [from_record(t) for t in self.store.trades_between(start, end)]

    def reports(self, kind: str | None, limit: int) -> list[dict[str, Any]]:
        reps = sorted((r for r in self.store.reports.values() if kind is None or r.kind == kind), key=lambda r: r.period, reverse=True)
        return [{"kind": r.kind, "period": r.period.isoformat(), "body": r.body, "data": r.data} for r in reps[:limit]]

    def shadow(self, trade_ids: list[str]) -> list[dict[str, Any]]:
        ids = set(trade_ids)
        return [{"rule_id": rid, "trade_id": tid, "active": a, "simulated_pnl": pnl, "state": st} for (rid, tid), (a, pnl, st) in self.store.shadow.items() if tid in ids]


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


class SqlReader(Reader):
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def trades(self, start: date, end: date) -> list[TradeView]:
        with session_scope(self.engine) as s:
            rows = s.scalars(select(m.Trade).where(m.Trade.day >= start, m.Trade.day <= end).options(selectinload(m.Trade.legs)).order_by(m.Trade.day, m.Trade.trade_no)).all()
            reasons = {r.trade_id: r.reasons for r in s.scalars(select(m.TradeReason).where(m.TradeReason.trade_id.in_([t.id for t in rows])))}
            out = []
            for t in rows:
                pv = PositionView(
                    contract=ContractView(Index(t.index), float(t.strike), t.option_type, t.expiry, t.symbol), direction=t.direction, lots=t.lots,
                    entry_ts=t.entry_ts, entry_price=float(t.entry_price), exit_ts=t.exit_ts, avg_exit_price=_f(t.avg_exit_price),
                    outcome=OutcomeView(t.outcome) if t.outcome else None, r_multiple=t.r_multiple or 0.0, net_pnl=_f(t.net_pnl) or 0.0,
                    gross_pnl=_f(t.gross_pnl) or 0.0, charges=_f(t.charges) or 0.0, probability=t.probability, tags=list(t.tags or []),
                    legs=[{"ts": lg.ts.isoformat(), "side": lg.side, "lots": lg.lots, "price": float(lg.price), "reason": lg.reason, "charges": float(lg.charges)} for lg in sorted(t.legs, key=lambda x: x.ts)],
                )
                out.append(TradeView(id=str(t.id), day=t.day, trade_no=t.trade_no, setup=t.setup, position=pv, reason_log=reasons.get(t.id, {})))
            return out

    def reports(self, kind: str | None, limit: int) -> list[dict[str, Any]]:
        with session_scope(self.engine) as s:
            q = select(m.Report).order_by(m.Report.period.desc(), m.Report.kind).limit(limit)
            if kind:
                q = q.where(m.Report.kind == kind)
            return [{"kind": r.kind, "period": r.period.isoformat(), "body": r.body, "data": r.data} for r in s.scalars(q)]

    def shadow(self, trade_ids: list[str]) -> list[dict[str, Any]]:
        if not trade_ids:
            return []
        import uuid

        with session_scope(self.engine) as s:
            rows = s.scalars(select(m.ShadowResult).where(m.ShadowResult.trade_id.in_([uuid.UUID(t) for t in trade_ids])))
            return [{"rule_id": r.rule_id, "trade_id": str(r.trade_id), "active": r.active, "simulated_pnl": float(r.simulated_pnl), "state": r.state} for r in rows]
