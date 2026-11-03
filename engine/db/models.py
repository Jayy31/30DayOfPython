"""Core Postgres tables (Supabase in production). Raw ticks go to Parquet, not here.

All timestamps are timezone-aware (TIMESTAMPTZ). Natural unique keys make
daily jobs idempotent (re-running a job upserts instead of duplicating).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(14, 2)
Price = Numeric(12, 2)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class TradingDay(Base):
    __tablename__ = "trading_days"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    is_holiday: Mapped[bool] = mapped_column(Boolean, default=False)
    premarket_report: Mapped[str | None] = mapped_column(Text)
    bias: Mapped[dict[str, Any]] = mapped_column(default=dict)       # per index
    events: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[uuid.UUID] = _uuid()
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    index: Mapped[str] = mapped_column(String(10))
    setup: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(5))
    layers: Mapped[dict[str, Any]] = mapped_column(default=dict)
    score: Mapped[float | None]
    probability: Mapped[float | None]
    probability_calibrated: Mapped[bool] = mapped_column(Boolean, default=False)
    prior_version: Mapped[str | None] = mapped_column(String(32))
    taken: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[uuid.UUID] = _uuid()
    day: Mapped[date] = mapped_column(Date, index=True)
    trade_no: Mapped[int] = mapped_column(Integer)   # 1 or 2 within the day
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    index: Mapped[str] = mapped_column(String(10))
    symbol: Mapped[str] = mapped_column(String(64))
    strike: Mapped[Decimal] = mapped_column(Price)
    option_type: Mapped[str] = mapped_column(String(2))
    expiry: Mapped[date] = mapped_column(Date)
    lots: Mapped[int] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(5))
    setup: Mapped[str] = mapped_column(String(64))
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[Decimal] = mapped_column(Price)
    initial_sl: Mapped[Decimal] = mapped_column(Price)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    avg_exit_price: Mapped[Decimal | None] = mapped_column(Price)
    outcome: Mapped[str | None] = mapped_column(String(16))   # target | sl | time
    reached_1r: Mapped[bool] = mapped_column(Boolean, default=False)
    gross_pnl: Mapped[Decimal | None] = mapped_column(Money)
    charges: Mapped[Decimal | None] = mapped_column(Money)
    net_pnl: Mapped[Decimal | None] = mapped_column(Money)
    r_multiple: Mapped[float | None]
    probability: Mapped[float | None]
    prior_version: Mapped[str | None] = mapped_column(String(32))
    tags: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()

    legs: Mapped[list[TradeLeg]] = relationship(back_populates="trade", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint("day", "trade_no"),)


class TradeLeg(Base):
    __tablename__ = "trade_legs"
    id: Mapped[uuid.UUID] = _uuid()
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    side: Mapped[str] = mapped_column(String(4))   # BUY | SELL
    lots: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Price)
    reason: Mapped[str] = mapped_column(String(24))  # entry | target_1r | target_1_5r | sl | trail | square_off
    charges: Mapped[Decimal] = mapped_column(Money)
    trade: Mapped[Trade] = relationship(back_populates="legs")


class TradeReason(Base):
    __tablename__ = "trade_reasons"
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"), primary_key=True)
    reasons: Mapped[dict[str, Any]] = mapped_column(default=dict)   # layer-by-layer log
    confidence: Mapped[int | None] = mapped_column(Integer)
    review: Mapped[str | None] = mapped_column(Text)


class CapitalLedger(Base):
    __tablename__ = "capital_ledger"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    trade_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("trades.id"), unique=True)
    change: Mapped[Decimal] = mapped_column(Money)
    balance: Mapped[Decimal] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(Text)


class ShadowResult(Base):
    __tablename__ = "shadow_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    trade_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trades.id", ondelete="CASCADE"))
    active: Mapped[bool] = mapped_column(Boolean)   # would this rule have blocked/changed the trade
    simulated_pnl: Mapped[Decimal] = mapped_column(Money)
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    __table_args__ = (UniqueConstraint("rule_id", "trade_id"),)


class NewsItem(Base):
    __tablename__ = "news_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    text_hash: Mapped[str] = mapped_column(String(64), unique=True)
    source: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[str | None] = mapped_column(String(10))
    surprise: Mapped[float | None]
    impact: Mapped[float | None]
    affected: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = _created()


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    index: Mapped[str] = mapped_column(String(10))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)  # GEX, flip, straddle, walls, VIX, breadth
    __table_args__ = (UniqueConstraint("ts", "index"),)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    task: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))   # premarket | postmarket | weekly | monthly
    period: Mapped[date] = mapped_column(Date)
    body: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _created()
    __table_args__ = (UniqueConstraint("kind", "period"),)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32))   # login | feed_drop | reconnect | error ...
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
