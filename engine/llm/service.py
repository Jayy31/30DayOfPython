"""LLM service: task -> model routing, size + budget guards, one retry, then None.

Callers treat None as "LLM unavailable" and fall back to rule-only logic.
"""

from __future__ import annotations

import logging
from datetime import datetime

from engine.config.schemas import LlmConfig
from engine.llm.base import LLMError, LLMProvider, T
from engine.llm.usage import UsageLedger

log = logging.getLogger(__name__)


def estimate_tokens(text: str) -> int:
    """Conservative estimate (~3.5 chars/token for English + numbers)."""
    return int(len(text) / 3.5) + 1


class LLMService:
    def __init__(self, provider: LLMProvider, cfg: LlmConfig, ledger: UsageLedger, model_fast: str, model_smart: str) -> None:
        self.provider = provider
        self.cfg = cfg
        self.ledger = ledger
        self.models = {"fast": model_fast, "smart": model_smart}

    def model_for(self, task: str) -> str:
        return self.models[self.cfg.tasks[task].tier]

    async def run(self, task: str, system: str, user: str, schema: type[T], now: datetime | None = None) -> T | None:
        if task not in self.cfg.tasks:
            raise KeyError(f"unknown LLM task {task}")
        tcfg = self.cfg.tasks[task]
        model = self.model_for(task)
        if self.ledger.over_budget(now):
            log.warning("llm budget exhausted; rule-only fallback", extra={"task": task, "spend": self.ledger.month_spend(now)})
            return None
        est = estimate_tokens(system) + estimate_tokens(user)
        if est > self.cfg.max_prompt_tokens:
            log.error("prompt too large; split the input instead", extra={"task": task, "est_tokens": est})
            return None
        for attempt in (1, 2):
            try:
                res = await self.provider.complete(model=model, system=system, user=user, schema=schema, max_tokens=tcfg.max_tokens, effort=tcfg.effort)
            except LLMError as e:
                log.warning("llm call failed", extra={"task": task, "attempt": attempt, "error": str(e)})
                continue
            self.ledger.record(task, res.model, res.usage, now)
            return res.output
        return None
