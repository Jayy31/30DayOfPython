"""Assembles the metrics + shadow-rule section for weekly / monthly reports."""

from __future__ import annotations

from datetime import date
from typing import Any

from engine.config.schemas import AppConfig
from engine.llm.usage import UsageLedger
from engine.metrics.compute import breakdowns, compute
from engine.shadow.simulator import ShadowSimulator
from engine.store.base import Store, TradeRecord

MIN_TRADES_TO_JUDGE = 100


def period_report_data(cfg: AppConfig, store: Store, trades: list[TradeRecord], start_capital: float, period: tuple[date, date], ledger: UsageLedger | None = None, all_trades: list[TradeRecord] | None = None) -> dict[str, Any]:
    m = compute(trades, start_capital)
    sim = ShadowSimulator(cfg)
    runs = sim.run(trades, start_capital)
    for run in runs.values():
        for st in run.steps:
            changed = (not st.taken) or abs(st.sim_pnl - st.trade.net_pnl) > 0.005
            store.save_shadow(run.rule_id, st.trade, changed, st.sim_pnl, {**st.state, "note": st.note})
    total = len(all_trades) if all_trades is not None else len(trades)
    data: dict[str, Any] = {
        "period": [period[0].isoformat(), period[1].isoformat()],
        "metrics": m.as_dict(curve=True),
        "targets": {"expectancy_positive": m.expectancy > 0, "profit_factor_gt_1_3": (m.profit_factor or 0) > 1.3},
        "breakdowns": breakdowns(trades, start_capital),
        "shadow_rules": ShadowSimulator.comparison(runs),
        "sample": {"trades_total": total, "min_to_judge": MIN_TRADES_TO_JUDGE, "judgeable": total >= MIN_TRADES_TO_JUDGE},
        "probabilities": "uncalibrated (priors)",
    }
    if ledger is not None:
        data["llm_usage"] = {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in ledger.by_task().items()}
        data["llm_month_spend_usd"] = round(ledger.month_spend(), 4)
    return data
