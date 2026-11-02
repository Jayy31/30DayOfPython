from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from engine.config.schemas import NewsSourceConfig
from engine.llm.mock import MockProvider
from engine.llm.service import LLMService
from engine.llm.usage import UsageLedger
from engine.news.fetchers import RssFetcher, build_fetchers, fetch_all, parse_rss
from engine.news.models import NewsItem
from engine.news.pipeline import NewsPipeline
from engine.news.processing import Deduper, SpikeDetector, is_relevant, market_confirms, tag_relevance
from engine.news.scoring import SYSTEM_PROMPT, rule_score
from tests.conftest import at

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>RBI cuts repo rate by 25 bps in surprise move</title><link>http://x/1</link>
<pubDate>Tue, 03 Nov 2026 04:30:00 GMT</pubDate><description>Policy</description></item>
<item><title>No date item</title></item>
</channel></rss>"""

SRC = NewsSourceConfig(id="et", kind="rss", url="http://feed")


def item(title, src="a", m=0, h=10, official=False):
    return NewsItem(source=src, title=title, published_at=at(h, m), official=official)


def test_parse_rss_skips_undated_and_converts_to_ist():
    items = parse_rss(RSS, SRC)
    assert len(items) == 1 and items[0].published_at == at(10, 0) and items[0].source == "et"


async def test_fetch_all_isolates_failures():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=RSS) if "good" in str(req.url) else httpx.Response(503)

    fetchers = [RssFetcher(NewsSourceConfig(id="good", kind="rss", url="http://good")), RssFetcher(NewsSourceConfig(id="bad", kind="rss", url="http://bad"))]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        items, errors = await fetch_all(fetchers, c)
    assert len(items) == 1 and "bad" in errors


def test_build_fetchers_from_config(cfg):
    fs = build_fetchers(cfg.news.sources)
    assert len(fs) == len([s for s in cfg.news.sources if s.enabled])


def test_dedupe_clusters_across_sources_and_prefers_official():
    d = Deduper(0.6, timedelta(hours=3))
    out = d.add([
        item("RBI cuts repo rate by 25 bps in surprise move", "et"),
        item("RBI cuts repo rate by 25 bps in a surprise move", "mint", m=2),
        item("RBI cuts repo rate by 25 bps in surprise move", "rbi", m=5, official=True),
        item("Crude oil jumps 4% on supply fears", "bs", m=1),
    ])
    assert len(out) == 2
    rbi = next(c for c in out if "RBI" in c.lead.title)
    assert set(rbi.sources) == {"et", "mint", "rbi"} and rbi.lead.official
    assert d.add([item("RBI cuts repo rate by 25 bps in surprise move", "et")]) == []   # exact repeat ignored


def test_relevance_tagging(cfg):
    c = Deduper(0.6, timedelta(hours=3)).add([item("Fed signals rate cut as Nasdaq hits record high")])[0]
    tag_relevance(c, cfg.news)
    assert {"central_banks", "global_markets"} <= set(c.topics) and is_relevant(c)
    junk = Deduper(0.6, timedelta(hours=3)).add([item("Celebrity wedding photos go viral")])[0]
    assert not is_relevant(tag_relevance(junk, cfg.news))


def test_spike_detection():
    sd = SpikeDetector(3, timedelta(minutes=20))
    d = Deduper(0.9, timedelta(hours=3))
    hits = []
    for i, (src, title) in enumerate([("a", "Missile strike on oil tanker"), ("b", "Military attack near strait"), ("c", "Border conflict escalates")]):
        c = d.add([item(title, src, m=i * 5)])[0]
        c.topics = ["geopolitics_war"]
        hits.append(sd.add(c))
    assert hits[:2] == [[], []] and hits[2] == ["geopolitics_war"]


def test_market_confirmation(cfg):
    th = cfg.news.confirm_moves_pct
    assert market_confirms({"CRUDE": 1.5, "GIFT_NIFTY": 0.1}, th) is True
    assert market_confirms({"CRUDE": 0.2, "GIFT_NIFTY": -0.1}, th) is False
    assert market_confirms({"CRUDE": None}, th) is None


def test_rule_scorer():
    c = Deduper(0.6, timedelta(hours=3)).add([item("US slaps new tariff on Indian steel; markets selloff")])[0]
    s = rule_score(0, c)
    assert s.direction == "bearish" and s.impact <= 0.5 and "rule-based" in s.rationale


def llm_for(cfg, provider):
    return LLMService(provider, cfg.llm, UsageLedger(cfg.llm.pricing, 40), "claude-haiku-5-5", "claude-sonnet-5-5")


async def test_pipeline_with_llm_scores_bias_and_events(cfg):
    def responder(system, user, schema):
        assert system == SYSTEM_PROMPT    # fixed rulebook -> cacheable
        return {"scores": [{"id": 0, "category": "central_banks", "direction": "bullish", "surprise": 0.8, "impact": 0.8, "affected": ["NIFTY", "banks"], "rationale": "unexpected cut"}]}

    p = NewsPipeline(cfg.news, llm_for(cfg, MockProvider(responder)), moves=lambda ts: {"GIFT_NIFTY": 0.6})
    upd = await p.process([item("RBI cuts repo rate by 25 bps in surprise move", "et")], at(10, 5))
    assert len(upd.scored) == 1 and upd.scored[0].scored_by == "llm" and upd.scored[0].market_confirmed
    b = p.bias(at(10, 10))
    assert b is not None and b > 0.5
    assert p.bias(at(18, 0)) == pytest.approx(b, abs=0.05)   # single story: decay cancels in the ratio
    assert "central_banks" in p.active_events(at(10, 30))


async def test_pipeline_falls_back_to_rules_when_llm_fails(cfg):
    p = NewsPipeline(cfg.news, llm_for(cfg, MockProvider(queue=[{"bad": 1}, {"bad": 2}])))
    upd = await p.process([item("Crude oil plunge as OPEC deal eases supply fears")], at(10, 0))
    assert upd.scored and upd.scored[0].scored_by == "rules"


async def test_unconfirmed_story_impact_capped(cfg):
    def responder(system, user, schema):
        return {"scores": [{"id": 0, "category": "geopolitics_war", "direction": "bearish", "surprise": -0.5, "impact": 0.9, "rationale": "x"}]}

    p = NewsPipeline(cfg.news, llm_for(cfg, MockProvider(responder)), moves=lambda ts: {"GIFT_NIFTY": 0.05, "CRUDE": 0.1})
    upd = await p.process([item("Missile attack reported near border")], at(10, 0))
    assert upd.scored[0].market_confirmed is False and upd.scored[0].effective_impact == 0.4
    assert p.active_events(at(10, 5)) == []


async def test_same_story_not_rescored_on_next_poll(cfg):
    prov = MockProvider(responder=lambda s, u, sc: {"scores": [{"id": 0, "category": "other", "direction": "neutral", "surprise": 0, "impact": 0.1, "rationale": "x"}]})
    p = NewsPipeline(cfg.news, llm_for(cfg, prov))
    await p.process([item("Sensex flat as FII selling continues", "a")], at(10, 0))
    await p.process([item("Sensex flat as FII selling continues today", "b", m=3)], at(10, 3))
    assert len(prov.calls) == 1


def test_news_bias_none_without_news(cfg):
    assert NewsPipeline(cfg.news).bias(at(10, 0)) is None
