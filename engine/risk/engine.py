"""Risk engine: locked trading rules. Neither the analysis nor the LLM can override these."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from engine.broker_paper.position import Outcome
from engine.config.schemas import RulesConfig
from engine.core.models import Direction, Index
from engine.core.timeutils import in_window, to_ist
from engine.core.trading_calendar import TradingCalendar


@dataclass(frozen=True)
class ClosedTrade:
    index: Index
    direction: Direction
    outcome: Outcome


@dataclass
class DayState:
    day: date
    closed: list[ClosedTrade] = field(default_factory=list)
    open_trades: int = 0

    @property
    def trades_taken(self) -> int:
        return len(self.closed) + self.open_trades


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    required_probability: float
    reasons: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()


class RiskEngine:
    def __init__(self, rules: RulesConfig, calendar: TradingCalendar) -> None:
        self.rules = rules
        self.calendar = calendar
        self.state: DayState | None = None

    def _day(self, now: datetime) -> DayState:
        d = to_ist(now).date()
        if self.state is None or self.state.day != d:
            if self.state is not None and self.state.open_trades:
                raise RuntimeError("new day with an open trade: overnight positions are not allowed")
            self.state = DayState(day=d)
        return self.state

    def in_entry_window(self, now: datetime) -> bool:
        t = to_ist(now).time()
        return any(in_window(t, s, e) for s, e in self.rules.entry_windows)

    def must_square_off(self, now: datetime) -> bool:
        return to_ist(now).time() >= self.rules.square_off

    def day_closed(self, now: datetime) -> bool:
        """No further entry is possible today (limit reached or target hit), so skip evaluation."""
        st = self._day(now)
        if st.trades_taken >= self.rules.max_trades_per_day:
            return True
        return self.rules.stop_after_target and any(t.outcome is Outcome.TARGET for t in st.closed)

    def required_probability(self, now: datetime) -> float:
        st = self._day(now)
        if any(t.outcome is Outcome.SL for t in st.closed):
            return self.rules.min_probability_after_sl
        return self.rules.min_probability

    def check_entry(self, now: datetime, index: Index, direction: Direction, probability: float) -> RiskDecision:
        st = self._day(now)
        reasons: list[str] = []
        tags: list[str] = []
        required = self.required_probability(now)

        if not self.calendar.is_trading_day(st.day):
            reasons.append("not a trading day")
        if index.value not in self.rules.indices:
            reasons.append(f"{index} not enabled")
        if not self.in_entry_window(now):
            reasons.append("outside entry windows")
        if st.open_trades >= self.rules.max_open_trades:
            reasons.append("a trade is already open")
        if st.trades_taken >= self.rules.max_trades_per_day:
            reasons.append("daily trade limit reached")
        if self.rules.stop_after_target and any(t.outcome is Outcome.TARGET for t in st.closed):
            reasons.append("target hit earlier today: done for the day")

        last = st.closed[-1] if st.closed else None
        if st.closed:
            tags.append(f"trade_{st.trades_taken + 1}")
        if last is not None and last.outcome is Outcome.SL:
            tags.append("after_sl")
            if last.direction is direction:
                tags.append("same_direction_after_sl")
                if last.index is not index:
                    tags.append("index_switch_same_bet")
        if probability < required:
            reasons.append(f"probability {probability:.3f} < required {required:.2f}")

        return RiskDecision(allowed=not reasons, required_probability=required, reasons=tuple(reasons), tags=tuple(tags))

    def register_open(self, now: datetime) -> None:
        st = self._day(now)
        if st.open_trades >= self.rules.max_open_trades:
            raise RuntimeError("cannot open: limit reached")
        st.open_trades += 1

    def register_close(self, now: datetime, index: Index, direction: Direction, outcome: Outcome) -> None:
        st = self._day(now)
        if st.open_trades < 1:
            raise RuntimeError("no open trade to close")
        st.open_trades -= 1
        st.closed.append(ClosedTrade(index=index, direction=direction, outcome=outcome))
