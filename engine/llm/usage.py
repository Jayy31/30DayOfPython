"""Token / cost accounting per task and the monthly budget guard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from engine.config.schemas import ModelPricing
from engine.core.timeutils import now_ist, to_ist
from engine.llm.base import Usage


@dataclass(frozen=True)
class UsageRecord:
    ts: datetime
    task: str
    model: str
    usage: Usage
    cost_usd: float


def cost_usd(usage: Usage, pricing: ModelPricing) -> float:
    prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    mult = pricing.long_context_multiplier if pricing.long_context_threshold and prompt > pricing.long_context_threshold else 1.0
    return mult * (
        usage.input_tokens * pricing.input
        + usage.output_tokens * pricing.output
        + usage.cache_read_tokens * pricing.cache_read
        + usage.cache_write_tokens * pricing.cache_write
    ) / 1_000_000


class UsageLedger:
    """In-memory ledger; `sink` persists each record (llm_usage table) when wired."""

    def __init__(self, pricing: dict[str, ModelPricing], monthly_budget_usd: float, sink: Callable[[UsageRecord], None] | None = None) -> None:
        self.pricing = pricing
        self.budget = monthly_budget_usd
        self.sink = sink
        self.records: list[UsageRecord] = []

    def record(self, task: str, model: str, usage: Usage, ts: datetime | None = None) -> UsageRecord:
        p = self.pricing.get(model)
        rec = UsageRecord(ts=to_ist(ts) if ts else now_ist(), task=task, model=model, usage=usage, cost_usd=cost_usd(usage, p) if p else 0.0)
        self.records.append(rec)
        if self.sink:
            self.sink(rec)
        return rec

    def month_spend(self, when: datetime | None = None) -> float:
        w = to_ist(when) if when else now_ist()
        return sum(r.cost_usd for r in self.records if (r.ts.year, r.ts.month) == (w.year, w.month))

    def over_budget(self, when: datetime | None = None) -> bool:
        return self.month_spend(when) >= self.budget

    def by_task(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for r in self.records:
            d = out.setdefault(r.task, {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cost_usd": 0.0})
            d["calls"] += 1
            d["input"] += r.usage.input_tokens
            d["output"] += r.usage.output_tokens
            d["cache_read"] += r.usage.cache_read_tokens
            d["cost_usd"] += r.cost_usd
        return out
