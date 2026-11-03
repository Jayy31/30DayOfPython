"""Anthropic implementation: structured outputs via messages.parse, cached system prompt."""

from __future__ import annotations

from typing import Any

import anthropic

from engine.llm.base import LLMError, LLMProvider, LLMRefusal, LLMResult, T, Usage


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=2)

    async def complete(self, *, model: str, system: str, user: str, schema: type[T], max_tokens: int, effort: str) -> LLMResult[T]:
        try:
            resp: Any = await self.client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                # fixed rulebook first + cache breakpoint; volatile content stays in messages
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": effort},
                output_format=schema,
            )
        except anthropic.APIStatusError as e:
            raise LLMError(f"anthropic {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic connection error: {e}") from e
        except ValueError as e:  # schema validation of the returned JSON (also hit by refusals with empty bodies)
            raise LLMError(f"invalid structured output: {e}") from e

        if resp.stop_reason == "refusal":
            raise LLMRefusal(f"refused: {getattr(resp.stop_details, 'category', None)}")
        if resp.stop_reason == "max_tokens":
            raise LLMError("output truncated (max_tokens)")
        parsed = resp.parsed_output
        if parsed is None:
            raise LLMError("no parsed output")
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        return LLMResult(output=parsed, model=resp.model, usage=usage)
