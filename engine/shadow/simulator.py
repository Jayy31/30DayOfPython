"""Runs every shadow rule over the chronological trade list and compares with actual P&L."""

from __future__ import annotations

from dataclasses import dataclass

from engine.config.schemas import AppConfig
from engine.shadow.rules import ShadowStep, build_rules
from engine.store.base import TradeRecord


@dataclass
class ShadowRun:
    rule_id: str
    steps: list[ShadowStep]
    actual_pnl: float
    simulated_pnl: float
    trades_taken: int
    trades_skipped: int

    @property
    def difference(self) -> float:
        return round(self.simulated_pnl - self.actual_pnl, 2)


class ShadowSimulator:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.rules = build_rules(cfg)   # fails at startup on an unknown rule type

    def run(self, trades: list[TradeRecord], starting_capital: float | None = None) -> dict[str, ShadowRun]:
        ordered = sorted(trades, key=lambda t: (t.day, t.trade_no))
        actual = round(sum(t.net_pnl for t in ordered), 2)
        out = {}
        for rule in self.rules:
            rule.reset(starting_capital if starting_capital is not None else self.cfg.rules.capital)
            steps = [rule.step(t) for t in ordered]
            out[rule.id] = ShadowRun(
                rule_id=rule.id, steps=steps, actual_pnl=actual,
                simulated_pnl=round(sum(s.sim_pnl for s in steps), 2),
                trades_taken=sum(s.taken for s in steps), trades_skipped=sum(not s.taken for s in steps),
            )
        return out

    @staticmethod
    def comparison(runs: dict[str, ShadowRun]) -> list[dict[str, object]]:
        return [
            {"rule": r.rule_id, "actual_pnl": r.actual_pnl, "simulated_pnl": r.simulated_pnl, "difference": r.difference, "taken": r.trades_taken, "skipped": r.trades_skipped}
            for r in runs.values()
        ]
