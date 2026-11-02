"""Performance metrics and breakdowns. Pure functions over TradeRecords."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import time
from typing import Any

from engine.store.base import TradeRecord


@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0           # positive number
    expectancy: float = 0.0         # per trade, Rs: win% * avg_win - loss% * avg_loss
    profit_factor: float | None = None
    net_pnl: float = 0.0
    avg_r: float = 0.0
    max_drawdown: float = 0.0       # Rs, positive
    max_drawdown_pct: float = 0.0
    longest_losing_streak: int = 0
    end_capital: float = 0.0
    capital_curve: list[tuple[str, float]] = field(default_factory=list)

    def as_dict(self, curve: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not curve:
            d.pop("capital_curve")
        return d


def compute(trades: Sequence[TradeRecord], starting_capital: float) -> Metrics:
    ts = sorted(trades, key=lambda t: (t.day, t.trade_no))
    m = Metrics(trades=len(ts), end_capital=starting_capital)
    if not ts:
        return m
    pnl = [t.net_pnl for t in ts]
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p <= 0]
    m.wins, m.losses = len(wins), len(losses)
    m.win_rate = len(wins) / len(ts)
    m.avg_win = sum(wins) / len(wins) if wins else 0.0
    m.avg_loss = sum(losses) / len(losses) if losses else 0.0
    m.expectancy = m.win_rate * m.avg_win - (1 - m.win_rate) * m.avg_loss
    gross_loss = sum(losses)
    m.profit_factor = sum(wins) / gross_loss if gross_loss > 0 else None
    m.net_pnl = sum(pnl)
    m.avg_r = sum(t.position.r_multiple for t in ts) / len(ts)

    cap = peak = starting_capital
    streak = 0
    for t, p in zip(ts, pnl, strict=True):
        cap += p
        m.capital_curve.append((f"{t.day.isoformat()}#{t.trade_no}", round(cap, 2)))
        peak = max(peak, cap)
        dd = peak - cap
        if dd > m.max_drawdown:
            m.max_drawdown, m.max_drawdown_pct = dd, dd / peak * 100
        streak = streak + 1 if p <= 0 else 0
        m.longest_losing_streak = max(m.longest_losing_streak, streak)
    m.end_capital = cap
    for k in ("win_rate", "avg_win", "avg_loss", "expectancy", "net_pnl", "avg_r", "max_drawdown", "max_drawdown_pct", "end_capital"):
        setattr(m, k, round(getattr(m, k), 4 if k in ("win_rate", "avg_r") else 2))
    if m.profit_factor is not None:
        m.profit_factor = round(m.profit_factor, 3)
    return m


# ------------------------------------------------------------------ keys
def _window(t: TradeRecord) -> str:
    e = t.position.entry_ts.time()
    return "window_1" if e < time(11, 30) else "window_2" if time(13, 30) <= e < time(14, 30) else "other"


def _vix_band(t: TradeRecord) -> str:
    v = t.reason_log.get("vix")
    if v is None:
        return "unknown"
    return "<12" if v < 12 else "12-16" if v < 16 else "16-20" if v < 20 else ">=20"


def _events(t: TradeRecord) -> list[str]:
    ev = [x.removeprefix("event:") for x in t.position.tags if x.startswith("event:")]
    return ev or ["no_event"]


KEYS: dict[str, Callable[[TradeRecord], str | list[str]]] = {
    "setup": lambda t: t.setup,
    "index": lambda t: t.position.contract.index.value,
    "time_window": _window,
    "regime": lambda t: (t.reason_log.get("regime") or {}).get("label", "unknown"),
    "vix_band": _vix_band,
    "event": _events,
    "expiry_day": lambda t: "expiry" if t.position.contract.expiry == t.day else "non_expiry",
    "trade_no": lambda t: f"trade_{t.trade_no}",
    "outcome": lambda t: t.position.outcome.value if t.position.outcome else "open",
}


def breakdowns(trades: Sequence[TradeRecord], starting_capital: float, keys: Sequence[str] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for key in keys or KEYS:
        groups: dict[str, list[TradeRecord]] = defaultdict(list)
        for t in trades:
            vals = KEYS[key](t)
            for v in vals if isinstance(vals, list) else [vals]:
                groups[v].append(t)
        out[key] = {g: compute(ts, starting_capital).as_dict(curve=False) for g, ts in sorted(groups.items())}
    return out
