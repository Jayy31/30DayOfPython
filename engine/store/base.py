"""Persistence interface for trades, signals, reports, events and job runs.

MemoryStore is used in replay/tests; SqlStore writes the Postgres/Supabase tables.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from engine.broker_paper.position import Position
from engine.decision.engine import SignalRecord


@dataclass
class TradeRecord:
    position: Position
    day: date
    trade_no: int
    setup: str
    reason_log: dict[str, Any]
    capital_after: float
    signal_index: int | None = None

    @property
    def net_pnl(self) -> float:
        return self.position.net_pnl


@dataclass
class ReportRecord:
    kind: str
    period: date
    body: str
    data: dict[str, Any] = field(default_factory=dict)


class Store(ABC):
    @abstractmethod
    def save_signals(self, signals: list[SignalRecord]) -> None: ...

    @abstractmethod
    def save_trade(self, rec: TradeRecord) -> None: ...

    @abstractmethod
    def trades_between(self, start: date, end: date) -> list[TradeRecord]: ...

    @abstractmethod
    def save_report(self, rep: ReportRecord) -> None: ...

    @abstractmethod
    def save_event(self, ts: datetime, kind: str, detail: dict[str, Any]) -> None: ...

    @abstractmethod
    def job_done(self, job: str, day: date) -> bool: ...

    @abstractmethod
    def mark_job(self, job: str, day: date) -> None: ...


class MemoryStore(Store):
    def __init__(self) -> None:
        self.signals: list[SignalRecord] = []
        self.trades: list[TradeRecord] = []
        self.reports: dict[tuple[str, date], ReportRecord] = {}
        self.events: list[tuple[datetime, str, dict[str, Any]]] = []
        self.jobs: set[tuple[str, date]] = set()

    def save_signals(self, signals: list[SignalRecord]) -> None:
        self.signals.extend(signals)

    def save_trade(self, rec: TradeRecord) -> None:
        self.trades = [t for t in self.trades if (t.day, t.trade_no) != (rec.day, rec.trade_no)] + [rec]

    def trades_between(self, start: date, end: date) -> list[TradeRecord]:
        return sorted((t for t in self.trades if start <= t.day <= end), key=lambda t: (t.day, t.trade_no))

    def save_report(self, rep: ReportRecord) -> None:
        self.reports[(rep.kind, rep.period)] = rep   # idempotent upsert

    def save_event(self, ts: datetime, kind: str, detail: dict[str, Any]) -> None:
        self.events.append((ts, kind, detail))

    def job_done(self, job: str, day: date) -> bool:
        return (job, day) in self.jobs

    def mark_job(self, job: str, day: date) -> None:
        self.jobs.add((job, day))
