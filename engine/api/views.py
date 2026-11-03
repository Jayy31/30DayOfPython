"""Read-side views: plain data the API/metrics need, buildable from memory or from SQL rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from engine.core.models import Index
from engine.store.base import TradeRecord


@dataclass(frozen=True)
class ContractView:
    index: Index
    strike: float
    option_type: str
    expiry: date
    symbol: str


@dataclass(frozen=True)
class OutcomeView:
    value: str


@dataclass
class PositionView:
    contract: ContractView
    direction: str
    lots: int
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime | None
    avg_exit_price: float | None
    outcome: OutcomeView | None
    r_multiple: float
    net_pnl: float
    gross_pnl: float
    charges: float
    probability: float | None
    tags: list[str]
    legs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TradeView:
    """Duck-types TradeRecord for metrics.compute / breakdowns."""

    id: str
    day: date
    trade_no: int
    setup: str
    position: PositionView
    reason_log: dict[str, Any]

    @property
    def net_pnl(self) -> float:
        return self.position.net_pnl

    def as_dict(self) -> dict[str, Any]:
        p, c = self.position, self.position.contract
        return {
            "id": self.id, "day": self.day.isoformat(), "trade_no": self.trade_no, "setup": self.setup,
            "index": c.index.value, "strike": c.strike, "option_type": c.option_type, "expiry": c.expiry.isoformat(), "symbol": c.symbol,
            "direction": p.direction, "lots": p.lots, "entry_ts": p.entry_ts.isoformat(), "entry_price": p.entry_price,
            "exit_ts": p.exit_ts.isoformat() if p.exit_ts else None, "avg_exit_price": p.avg_exit_price,
            "outcome": p.outcome.value if p.outcome else None, "r_multiple": p.r_multiple, "net_pnl": p.net_pnl,
            "gross_pnl": p.gross_pnl, "charges": p.charges, "probability": p.probability, "tags": p.tags,
            "legs": p.legs, "reason_log": self.reason_log,
        }


def from_record(t: TradeRecord) -> TradeView:
    p, c = t.position, t.position.contract
    return TradeView(
        id=str(p.id), day=t.day, trade_no=t.trade_no, setup=t.setup, reason_log=t.reason_log,
        position=PositionView(
            contract=ContractView(c.index, c.strike, c.option_type.value, c.expiry, c.key), direction=p.direction.value, lots=p.lots,
            entry_ts=p.entry_ts, entry_price=p.entry_price, exit_ts=p.exit_ts, avg_exit_price=p.avg_exit_price,
            outcome=OutcomeView(p.outcome.value) if p.outcome else None, r_multiple=p.r_multiple, net_pnl=p.net_pnl,
            gross_pnl=p.gross_pnl, charges=p.total_charges, probability=p.probability, tags=list(p.tags),
            legs=[{"ts": leg.ts.isoformat(), "side": leg.side.value, "lots": leg.lots, "price": leg.price, "reason": leg.reason.value, "charges": leg.charges} for leg in p.legs],
        ),
    )
