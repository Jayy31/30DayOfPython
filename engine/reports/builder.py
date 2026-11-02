"""Reports: the numbers come from code; the LLM (Sonnet-class) only writes the narrative.
If the LLM is unavailable, a template body is used. Reports upsert by (kind, period)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from engine.llm.service import LLMService
from engine.store.base import ReportRecord, TradeRecord

REPORT_SYSTEM = """You write concise trading-desk reports for a paper-trading research system on
Nifty/Sensex index options (option buying only, intraday). You receive JSON computed by code.
Never invent numbers, levels or trades that are not in the JSON. Probabilities labelled
"uncalibrated (priors)" must be described as uncalibrated. Plain text, short sections,
no financial advice language. The system is judged only after 100+ trades."""


class ReportText(BaseModel):
    title: str = Field(max_length=120)
    body: str = Field(max_length=6000)


def trade_summary(trades: Sequence[TradeRecord]) -> dict[str, Any]:
    pnl = [t.net_pnl for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl": round(sum(pnl), 2),
        "gross_profit": round(sum(wins), 2),
        "gross_loss": round(sum(losses), 2),
        "items": [
            {
                "day": t.day.isoformat(), "no": t.trade_no, "index": t.position.contract.index.value,
                "setup": t.setup, "direction": t.position.direction.value, "strike": t.position.contract.strike,
                "type": t.position.contract.option_type.value, "lots": t.position.lots,
                "entry": t.position.entry_price, "exit": t.position.avg_exit_price,
                "outcome": t.position.outcome.value if t.position.outcome else None,
                "net_pnl": t.net_pnl, "r": t.position.r_multiple,
                "probability": (t.reason_log.get("probability") or {}).get("p"),
                "probability_label": (t.reason_log.get("probability") or {}).get("label"),
                "tags": list(t.position.tags),
            }
            for t in trades
        ],
    }


def _template(kind: str, period: date, data: dict[str, Any]) -> ReportText:
    lines = [f"{kind.replace('_', ' ').title()} - {period.isoformat()}"]
    if "summary" in data:
        s = data["summary"]
        lines.append(f"Trades {s['trades']} (W {s['wins']} / L {s['losses']}), net P&L {s['net_pnl']:+.2f}")
        for it in s["items"]:
            lines.append(f"- #{it['no']} {it['index']} {it['setup']} {it['direction']} {it['strike']}{it['type']} x{it['lots']}: {it['outcome']} {it['net_pnl']:+.2f} ({it['r']:+.2f}R)")
    for k in ("capital", "bias", "levels", "events", "watchlist", "signals"):
        if k in data:
            lines.append(f"{k.title()}: {json.dumps(data[k], default=str)[:800]}")
    return ReportText(title=lines[0], body="\n".join(lines))


async def build_report(kind: str, period: date, data: dict[str, Any], llm: LLMService | None) -> ReportRecord:
    task = {"premarket": "premarket_report", "postmarket": "postmarket_review", "weekly": "weekly_review", "monthly": "monthly_review"}[kind]
    text = None
    if llm is not None:
        user = f"Write the {kind} report for {period.isoformat()} from this data:\n" + json.dumps(data, default=str, sort_keys=True)
        text = await llm.run(task, REPORT_SYSTEM, user, ReportText)
    by = "llm" if text is not None else "template"
    text = text or _template(kind, period, data)
    return ReportRecord(kind=kind, period=period, body=f"{text.title}\n\n{text.body}", data={**data, "narrative_by": by})
