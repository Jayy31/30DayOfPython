"""Mock provider for tests and replay mode: no network, no key."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from engine.llm.base import LLMError, LLMProvider, LLMResult, T, Usage

Responder = Callable[[str, str, type[Any]], Any]


class MockProvider(LLMProvider):
    """`responder(system, user, schema)` returns a dict/model (validated against `schema`),
    or raises. A queue of responses can be given instead; each call pops one."""

    name = "mock"

    def __init__(self, responder: Responder | None = None, queue: list[Any] | None = None, usage: Usage | None = None) -> None:
        self.responder = responder
        self.queue = list(queue or [])
        self.usage = usage or Usage(input_tokens=1000, output_tokens=200)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, model: str, system: str, user: str, schema: type[T], max_tokens: int, effort: str) -> LLMResult[T]:
        self.calls.append({"model": model, "system": system, "user": user, "schema": schema.__name__, "effort": effort})
        if self.queue:
            raw = self.queue.pop(0)
        elif self.responder is not None:
            raw = self.responder(system, user, schema)
        else:
            raise LLMError("mock has no response")
        if isinstance(raw, Exception):
            raise raw
        try:
            out = raw if isinstance(raw, schema) else schema.model_validate(raw)
        except ValidationError as e:
            raise LLMError(f"invalid structured output: {e}") from e
        return LLMResult(output=out, model=model, usage=self.usage)
