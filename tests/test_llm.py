from __future__ import annotations

import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from engine.config.settings import Settings
from engine.llm.anthropic_provider import AnthropicProvider
from engine.llm.base import LLMError, LLMRefusal, Usage  # noqa: F401
from engine.llm.factory import create_llm_service
from engine.llm.mock import MockProvider
from engine.llm.service import LLMService
from engine.llm.usage import UsageLedger, cost_usd
from tests.conftest import at


class Out(BaseModel):
    answer: str
    score: float


def svc(cfg, provider, budget=40.0):
    return LLMService(provider, cfg.llm, UsageLedger(cfg.llm.pricing, budget), "claude-haiku-5-5", "claude-sonnet-5-5")


def test_cost_haiku_and_long_context(cfg):
    p = cfg.llm.pricing["claude-haiku-5-5"]
    assert cost_usd(Usage(50_000, 10_000), p) == pytest.approx(0.005 + 0.005)
    assert cost_usd(Usage(200_000, 0), p) == pytest.approx(0.02 * 5)   # prompt above 100K -> 5x
    assert cost_usd(Usage(0, 0, cache_read_tokens=1_000_000), cfg.llm.pricing["claude-sonnet-5-5"]) == pytest.approx(0.20)


async def test_routes_task_to_tier_and_records_usage(cfg):
    m = MockProvider(queue=[{"answer": "ok", "score": 0.5}, {"answer": "ok2", "score": 0.1}])
    s = svc(cfg, m)
    assert (await s.run("news_scoring", "sys", "u", Out, at(10, 0))).answer == "ok"
    assert (await s.run("premarket_report", "sys", "u", Out, at(10, 0))).answer == "ok2"
    assert [c["model"] for c in m.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert m.calls[0]["effort"] == "low"
    tasks = s.ledger.by_task()
    assert tasks["news_scoring"]["calls"] == 1 and tasks["premarket_report"]["cost_usd"] > tasks["news_scoring"]["cost_usd"]


async def test_invalid_output_retried_once_then_none(cfg):
    m = MockProvider(queue=[{"answer": "x"}, {"answer": "fixed", "score": 1}])
    assert (await svc(cfg, m).run("news_scoring", "s", "u", Out)).answer == "fixed"
    m2 = MockProvider(queue=[{"bad": 1}, LLMRefusal("nope")])
    s2 = svc(cfg, m2)
    assert await s2.run("news_scoring", "s", "u", Out) is None
    assert len(m2.calls) == 2 and s2.ledger.records == []


async def test_budget_blocks_calls(cfg):
    m = MockProvider(queue=[{"answer": "a", "score": 0}] * 3, usage=Usage(10_000_000, 1_000_000))
    s = svc(cfg, m, budget=1.0)
    assert await s.run("news_scoring", "s", "u", Out, at(10, 0)) is not None   # $1.50 -> over budget now
    assert await s.run("news_scoring", "s", "u", Out, at(11, 0)) is None
    assert len(m.calls) == 1


async def test_oversized_prompt_refused_not_truncated(cfg):
    m = MockProvider(queue=[{"answer": "a", "score": 0}])
    assert await svc(cfg, m).run("news_scoring", "s", "x" * 400_000, Out) is None
    assert m.calls == []


async def test_unknown_task_raises(cfg):
    with pytest.raises(KeyError):
        await svc(cfg, MockProvider()).run("nope", "s", "u", Out)


def test_factory_uses_mock_without_key(cfg):
    s = create_llm_service(Settings(_env_file=None), cfg.llm)
    assert isinstance(s.provider, MockProvider)


# ------------------------------------------------------- anthropic, offline
def _client(handler) -> AsyncAnthropic:
    return AsyncAnthropic(api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)), max_retries=0)


def _msg(text: str, stop: str = "end_turn") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 40, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0},
    }


async def test_anthropic_request_shape_and_parsing():
    seen = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=_msg(json.dumps({"answer": "hi", "score": 0.7})))

    res = await AnthropicProvider(client=_client(handler)).complete(model="claude-haiku-5-5", system="RULES", user="story", schema=Out, max_tokens=500, effort="low")
    b = seen["body"]
    assert b["model"] == "claude-haiku-5-5"
    assert b["system"] == [{"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}}]
    assert b["output_config"]["effort"] == "low" and b["output_config"]["format"]["type"] == "json_schema"
    assert res.output == Out(answer="hi", score=0.7)
    assert res.usage == Usage(120, 40, 900, 0)


async def test_anthropic_refusal_and_bad_json():
    def refuse(req):
        m = _msg("")
        m["stop_reason"] = "refusal"
        m["stop_details"] = {"type": "refusal", "category": None, "explanation": None}
        return httpx2.Response(200, json=m)

    with pytest.raises(LLMError):   # parse() fails on the empty refusal body before stop_reason is visible
        await AnthropicProvider(client=_client(refuse)).complete(model="m", system="s", user="u", schema=Out, max_tokens=10, effort="low")

    def bad(req):
        return httpx2.Response(200, json=_msg('{"answer": 5}'))

    with pytest.raises(LLMError):
        await AnthropicProvider(client=_client(bad)).complete(model="m", system="s", user="u", schema=Out, max_tokens=10, effort="low")

    def err(req):
        return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})

    with pytest.raises(LLMError):
        await AnthropicProvider(client=_client(err)).complete(model="m", system="s", user="u", schema=Out, max_tokens=10, effort="low")
