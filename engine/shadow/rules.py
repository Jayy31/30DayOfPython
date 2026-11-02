"""Shadow rules: simulated in parallel on the real trades; they never block real paper trading.

Each rule `type` is a plugin. New rules are added in config/shadow_rules.yaml with an
existing type and params (no engine change); a brand-new behaviour is one new class here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from engine.broker_paper.position import Outcome
from engine.broker_paper.sizing import lots_for_capital
from engine.config.schemas import AppConfig, ShadowRuleConfig, TrailConfig
from engine.shadow.resim import resimulate
from engine.store.base import TradeRecord


@dataclass
class ShadowStep:
    trade: TradeRecord
    taken: bool               # would the trade have been taken under this rule
    sim_pnl: float
    note: str = ""
    state: dict[str, Any] = field(default_factory=dict)


class ShadowRule(ABC):
    def __init__(self, cfg: AppConfig, rule: ShadowRuleConfig) -> None:
        self.cfg = cfg
        self.id = rule.id
        self.params = rule.params
        self.capital = cfg.rules.capital

    def reset(self, starting_capital: float) -> None:
        self.capital = starting_capital

    @abstractmethod
    def step(self, t: TradeRecord) -> ShadowStep: ...

    def _book(self, t: TradeRecord, taken: bool, pnl: float, note: str = "", **state: Any) -> ShadowStep:
        if taken:
            self.capital += pnl
        return ShadowStep(t, taken, round(pnl if taken else 0.0, 2), note, {"capital": round(self.capital, 2), **state})


REGISTRY: dict[str, type[ShadowRule]] = {}


def register(type_name: str):  # type: ignore[no-untyped-def]
    def deco(cls: type[ShadowRule]) -> type[ShadowRule]:
        REGISTRY[type_name] = cls
        return cls
    return deco


def _week(d: date) -> tuple[int, int]:
    y, w, _ = d.isocalendar()
    return y, w


@register("weekly_pnl_cap")
class WeeklyPnlCap(ShadowRule):
    """Stop for the week once the week's P&L reaches +cap."""

    def reset(self, starting_capital: float) -> None:
        super().reset(starting_capital)
        self.week: tuple[int, int] | None = None
        self.week_pnl = 0.0

    def step(self, t: TradeRecord) -> ShadowStep:
        wk = _week(t.day)
        if wk != self.week:
            self.week, self.week_pnl = wk, 0.0
        if self.week_pnl >= float(self.params["cap"]):
            return self._book(t, False, 0, "weekly profit cap reached", week_pnl=self.week_pnl)
        self.week_pnl += t.net_pnl
        return self._book(t, True, t.net_pnl, week_pnl=round(self.week_pnl, 2))


@register("weekly_sl_count")
class WeeklySlPause(ShadowRule):
    """Stop for the week after max_sl stop-losses."""

    def reset(self, starting_capital: float) -> None:
        super().reset(starting_capital)
        self.week: tuple[int, int] | None = None
        self.sls = 0

    def step(self, t: TradeRecord) -> ShadowStep:
        wk = _week(t.day)
        if wk != self.week:
            self.week, self.sls = wk, 0
        if self.sls >= int(self.params["max_sl"]):
            return self._book(t, False, 0, f"{self.sls} SLs this week", sls=self.sls)
        if t.position.outcome is Outcome.SL:
            self.sls += 1
        return self._book(t, True, t.net_pnl, sls=self.sls)


@register("monthly_drawdown")
class MonthlyDrawdown(ShadowRule):
    """Pause for the rest of the month once capital falls pct_from_peak below the month's peak."""

    def reset(self, starting_capital: float) -> None:
        super().reset(starting_capital)
        self.month: tuple[int, int] | None = None
        self.peak = starting_capital
        self.paused = False

    def step(self, t: TradeRecord) -> ShadowStep:
        m = (t.day.year, t.day.month)
        if m != self.month:
            self.month, self.peak, self.paused = m, self.capital, False
        if self.paused:
            return self._book(t, False, 0, "monthly drawdown pause", peak=self.peak)
        step = self._book(t, True, t.net_pnl)
        self.peak = max(self.peak, self.capital)
        if self.capital <= self.peak * (1 - float(self.params["pct_from_peak"]) / 100):
            self.paused = True
        step.state.update(peak=round(self.peak, 2), paused=self.paused)
        return step


@register("size_multiplier")
class SizeMultiplier(ShadowRule):
    """Deploy only multiplier x capital per trade; re-simulates the exit ladder for the new lot count."""

    def step(self, t: TradeRecord) -> ShadowStep:
        p = t.position
        lots = lots_for_capital(self.capital * float(self.params["multiplier"]), p.entry_price, p.contract.lot_size, p.exchange, self.cfg.charges)
        if lots < 1:
            return self._book(t, False, 0, "1 lot exceeds the reduced capital", lots=0)
        sim = resimulate(p, lots=lots)
        return self._book(t, True, sim.net_pnl, lots=lots, outcome=sim.outcome.value if sim.outcome else None)


@register("trail_variant")
class TrailVariant(ShadowRule):
    """Same trades, different trailing settings (timeframe / pivot bars / buffer)."""

    def step(self, t: TradeRecord) -> ShadowStep:
        tr = TrailConfig(**self.params)
        sim = resimulate(t.position, trail=tr)
        return self._book(t, True, sim.net_pnl, outcome=sim.outcome.value if sim.outcome else None)


@register("index_switch_flag")
class IndexSwitchFlag(ShadowRule):
    """Tag only: flags a 2nd trade on the other index in the same direction after an SL.
    Simulated P&L = what you'd have if those trades were skipped."""

    def step(self, t: TradeRecord) -> ShadowStep:
        flagged = "index_switch_same_bet" in t.position.tags
        return self._book(t, not flagged, t.net_pnl, "flagged: same bet after SL" if flagged else "", flagged=flagged)


def build_rules(cfg: AppConfig) -> list[ShadowRule]:
    unknown = {r.type for r in cfg.shadow.rules} - set(REGISTRY)
    if unknown:
        raise ValueError(f"unknown shadow rule types: {sorted(unknown)}")
    return [REGISTRY[r.type](cfg, r) for r in cfg.shadow.rules]
