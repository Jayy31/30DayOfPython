"""Live engine: ticks -> market state -> decision -> paper broker, with locked risk rules.

One instance runs one trading day. Works identically on the mock replay provider
and (Phase 10) the Kotak feed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime

from engine.alerts.notifier import Notifier
from engine.broker_paper.broker import InsufficientCapital, PaperBroker
from engine.broker_paper.position import Leg, Position
from engine.config.schemas import AppConfig
from engine.core.models import Index, OptionQuote
from engine.core.timeutils import floor_to_minutes, to_ist
from engine.data.broker.base import MarketDataProvider
from engine.data.feed_monitor import FeedMonitor
from engine.data.models import Instrument, Tick
from engine.decision.engine import DecisionEngine, IndexInputs, TradePlan
from engine.decision.reasons import add_exit, render_text
from engine.risk.engine import RiskEngine
from engine.state.market_state import IndexState
from engine.store.base import Store, TradeRecord

log = logging.getLogger(__name__)
ExternalBias = Callable[[Index, datetime], Mapping[str, float | None]]
EventsFn = Callable[[datetime], list[str]]


@dataclass
class DayResult:
    day: date
    trades: list[TradeRecord] = field(default_factory=list)
    signals: int = 0
    feed_gaps: int = 0
    ticks: int = 0


class LiveEngine:
    def __init__(
        self,
        cfg: AppConfig,
        provider: MarketDataProvider,
        states: dict[Index, IndexState],
        decision: DecisionEngine,
        risk: RiskEngine,
        broker: PaperBroker,
        notifier: Notifier,
        store: Store,
        *,
        external_bias: ExternalBias | None = None,
        events: EventsFn | None = None,
        feed_gap_seconds: float = 10.0,
        size_fraction: float = 1.0,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.states = states
        self.decision = decision
        self.risk = risk
        self.broker = broker
        self.notifier = notifier
        self.store = store
        self.external_bias = external_bias or (lambda idx, now: {})
        self.events = events or (lambda now: [])
        self.monitor = FeedMonitor(feed_gap_seconds)
        self.size_fraction = size_fraction
        self._token_state = {tok: st for st in states.values() for tok in st.tokens()}
        self._minute: datetime | None = None
        self._plan: TradePlan | None = None
        self._last_quote: OptionQuote | None = None
        self._result: DayResult | None = None
        self._feed_down_alerted = False
        self.recorded: list[Tick] | None = None   # set to [] to keep the day's ticks for Parquet
        self._closed_today: list[Position] = []   # keep extending their quote path until square-off
        self.last_ctx: dict[Index, object] = {}
        self.recent_signals: deque = deque(maxlen=50)   # type: ignore[type-arg]
        self.last_tick_ts: datetime | None = None

    # ------------------------------------------------------------ main loop
    async def run_day(self, day: date, instruments: list[Instrument]) -> DayResult:
        self._result = DayResult(day=day)
        await self.provider.connect()
        await self.provider.subscribe([i for i in instruments if i.token in self._token_state])
        try:
            async for tick in self.provider.ticks():
                if tick.ts.date() != day:
                    continue
                await self.on_tick(tick)
        finally:
            await self._final_square_off()
            await self.provider.close()
        return self._result

    async def on_tick(self, tick: Tick) -> None:
        assert self._result is not None
        self._result.ticks += 1
        self.last_tick_ts = tick.ts
        if self.recorded is not None:
            self.recorded.append(tick)
        gap = self.monitor.on_tick(tick.ts)
        if gap is not None:
            self._result.feed_gaps += 1
            self.store.save_event(tick.ts, "feed_gap", {"start": gap.start.isoformat(), "seconds": gap.seconds})
            if self._feed_down_alerted:
                await self.notifier.send(f"Feed restored after {gap.seconds:.0f}s")
                self._feed_down_alerted = False
        st = self._token_state.get(tick.token)
        if st is not None:
            st.on_tick(tick)
        for done in self._closed_today:
            if tick.token == done.contract.symbol and tick.ts.time() <= self.cfg.rules.square_off:
                done.path.append(tick.to_quote())
        pos = self.broker.position
        if pos is not None and tick.token == pos.contract.symbol:
            self._last_quote = tick.to_quote()
            await self._handle_legs(self.broker.on_quote(self._last_quote), pos)
        minute = floor_to_minutes(tick.ts, 1)
        if self._minute is None or minute > self._minute:
            self._minute = minute
            await self.on_minute(tick.ts)

    async def on_minute(self, now: datetime) -> None:
        for st in self.states.values():
            st.refresh(now)
        if self.risk.must_square_off(now):
            await self._final_square_off()
            return
        if self.broker.has_open_position or not self.risk.in_entry_window(now) or self.risk.day_closed(now):
            return
        inputs = []
        events = self.events(now)
        nifty = self.states.get(Index.NIFTY)
        proxy = float(nifty.futures_class().bias) if nifty else None
        for idx, st in self.states.items():
            ctx = st.context(now, self.external_bias(idx, now), events, proxy if idx is Index.SENSEX else None)
            if ctx is not None:
                self.last_ctx[idx] = ctx
            chain = st.chain(now)
            if ctx is None or chain is None:
                continue
            inputs.append(IndexInputs(ctx=ctx, chain=chain, lot_size=st.lot_size, reference_iv=st.reference_iv))
        if not inputs:
            return
        res = self.decision.evaluate(now, inputs, self.broker.capital * self.size_fraction)
        if res.signals:
            assert self._result is not None
            self._result.signals += len(res.signals)
            self.store.save_signals(res.signals)
            self.recent_signals.extend(res.signals)
        if res.plan is not None:
            await self._enter(res.plan, now)

    async def _enter(self, plan: TradePlan, now: datetime) -> None:
        st = self.states[plan.index]
        tick = st.options.get(plan.contract.symbol)
        if tick is None:
            return
        quote = tick.to_quote()
        try:
            pos = self.broker.open(plan.contract, quote.model_copy(update={"ts": now}), plan.direction, setup=plan.setup, probability=plan.probability.p, tags=list(plan.tags), size_fraction=self.size_fraction)
        except InsufficientCapital as e:
            self.store.save_event(now, "entry_skipped", {"reason": str(e)})
            return
        self.risk.register_open(now)
        self._plan = plan
        self._last_quote = quote
        await self.notifier.send(f"ENTRY {plan.index.value} {plan.contract.strike:.0f}{plan.contract.option_type.value} x{pos.lots} lots @ {pos.entry_price}\n" + render_text(plan.reason_log))

    async def _handle_legs(self, legs: list[Leg], pos: Position) -> None:
        for leg in legs:
            if pos.is_open:
                await self.notifier.send(f"PARTIAL {pos.contract.key}: sold {leg.lots} lot(s) @ {leg.price} ({leg.reason.value}); SL now {pos.sl}")
        if legs and not pos.is_open:
            await self._close(pos)

    async def _close(self, pos: Position) -> None:
        assert pos.outcome is not None and pos.exit_ts is not None
        if self._result is None:
            self._result = DayResult(day=pos.entry_ts.date())
        self.risk.register_close(pos.exit_ts, pos.contract.index, pos.direction, pos.outcome)
        plan = self._plan
        log_ = add_exit(
            plan.reason_log if plan else {},
            outcome=pos.outcome.value,
            legs=[{"ts": leg.ts.isoformat(), "side": leg.side.value, "lots": leg.lots, "price": leg.price, "reason": leg.reason.value} for leg in pos.legs],
            net_pnl=pos.net_pnl,
            r_multiple=pos.r_multiple,
        )
        rec = TradeRecord(position=pos, day=self._result.day, trade_no=len(self._result.trades) + 1, setup=pos.setup, reason_log=log_, capital_after=self.broker.capital)
        self._result.trades.append(rec)
        self._closed_today.append(pos)
        self.store.save_trade(rec)
        self._plan = None
        await self.notifier.send(f"EXIT {pos.contract.key} {pos.outcome.value}: net {pos.net_pnl:+.2f} ({pos.r_multiple:+.2f}R). Capital {self.broker.capital:.2f}")

    async def _final_square_off(self) -> None:
        pos = self.broker.position
        if pos is None or not pos.is_open:
            return
        quote = self._last_quote or pos.last_quote
        if quote is None:
            return
        if self._minute is not None:
            quote = quote.model_copy(update={"ts": max(quote.ts, to_ist(self._minute))})
        await self._handle_legs(self.broker.square_off(quote), pos)

    # ---------------------------------------------------------- feed watchdog
    async def watchdog(self, now: datetime) -> None:
        """Call every few seconds. If the feed is silent while a trade is open, alert and
        manage the trade on REST quotes until ticks return."""
        pos = self.broker.position
        if not self.monitor.is_stale(now):
            return
        if not self._feed_down_alerted:
            self._feed_down_alerted = True
            self.store.save_event(now, "feed_drop", {"last_tick": self.monitor.last_tick.isoformat() if self.monitor.last_tick else None})
            await self.notifier.send("ALERT: market feed silent" + (" - managing open trade on REST quotes" if pos else ""))
        if pos is None or not pos.is_open:
            return
        st = self.states[pos.contract.index]
        inst = next((i for i in st.instruments if i.token == pos.contract.symbol), None)
        if inst is None:
            return
        tick = await self.provider.quote(inst)
        q = tick.to_quote().model_copy(update={"ts": max(tick.ts, to_ist(now))})
        self._last_quote = q
        legs = self.broker.square_off(q) if self.risk.must_square_off(now) else self.broker.on_quote(q)
        await self._handle_legs(legs, pos)


    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict:  # type: ignore[type-arg]
        """Read-only view of the live state for the dashboard API."""
        pos = self.broker.position
        indices = {}
        for idx, st in self.states.items():
            ctx = self.last_ctx.get(idx)
            sst = st.straddle.state() if st.straddle else None
            indices[idx.value] = {
                "spot": st.spot,
                "vwap": st.vwap.value,
                "or_high": st.opening_range.high,
                "or_low": st.opening_range.low,
                "regime": {"label": st.regime.label, "confidence": round(st.regime.confidence, 3), "drivers": list(st.regime.drivers)} if st.regime else None,
                "bias": {"label": ctx.bias.label, "score": round(ctx.bias.score, 3)} if ctx is not None else None,  # type: ignore[attr-defined]
                "trigger": {"direction": ctx.trigger.direction, "strength": round(ctx.trigger.strength, 3)} if ctx is not None else None,  # type: ignore[attr-defined]
                "zones": [{"price": round(z.price, 2), "sources": sorted(set(z.sources))} for z in (ctx.zones if ctx is not None else ())],  # type: ignore[attr-defined]
                "call_wall": st.oi.call_wall if st.oi else None,
                "put_wall": st.oi.put_wall if st.oi else None,
                "pcr": round(st.oi.pcr, 3) if st.oi else None,
                "gex": {"regime": st.gex.regime, "flip": round(st.gex.flip, 2) if st.gex.flip else None} if st.gex else None,
                "straddle": {"label": sst.label, "expansion": round(sst.expansion, 4), "day_implied_move": round(sst.day_implied_move, 2), "range_used": round(sst.range_used, 3)} if sst else None,
                "opening": st.opening.type.value if st.opening else None,
                "futures_oi": st.futures_class().value,
            }
        open_trade = None
        if pos is not None and pos.is_open:
            q = pos.last_quote
            open_trade = {
                "symbol": pos.contract.key, "index": pos.contract.index.value, "strike": pos.contract.strike,
                "option_type": pos.contract.option_type.value, "direction": pos.direction.value, "setup": pos.setup,
                "lots": pos.lots, "open_lots": pos.open_lots, "entry_price": pos.entry_price, "entry_ts": pos.entry_ts.isoformat(),
                "sl": pos.sl, "target_1": pos.target_1, "target_2": pos.target_2, "stage": pos.stage, "trailed": pos.trailed,
                "bid": q.bid if q else None, "mtm": self.broker.mark_to_market(), "probability": pos.probability,
            }
        res = self._result
        return {
            "last_tick": self.last_tick_ts.isoformat() if self.last_tick_ts else None,
            "feed_stale": self._feed_down_alerted,
            "capital": self.broker.capital,
            "open_trade": open_trade,
            "day": {"date": res.day.isoformat(), "trades": len(res.trades), "signals": res.signals, "feed_gaps": res.feed_gaps, "net_pnl": round(sum(t.net_pnl for t in res.trades), 2)} if res else None,
            "indices": indices,
            "signals": [
                {"ts": s.ts.isoformat(), "index": s.index.value, "setup": s.setup, "direction": s.direction.value, "score": s.score,
                 "probability": s.probability.p if s.probability else None, "probability_label": s.probability.label if s.probability else None,
                 "taken": s.taken, "reasons": list(s.reject_reasons)}
                for s in reversed(self.recent_signals)
            ],
        }
