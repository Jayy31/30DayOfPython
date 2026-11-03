"""Story scoring: LLM (Haiku-class, batched, JSON schema) with a rule-based fallback."""

from __future__ import annotations

import json

from engine.llm.service import LLMService
from engine.news.models import NewsCluster, NewsScore, NewsScoreBatch

# Fixed rulebook: identical bytes every call so the prompt cache hits.
SYSTEM_PROMPT = """You score financial news for an intraday Nifty/Sensex options research system.
For each story return: category (one of: geopolitics_war, trade_tariffs, central_banks,
commodities, global_markets, india_macro, corporate, tech_ai, elections, policymakers, other);
direction for Indian equity indices (bullish, bearish, neutral); surprise in [-1, 1] comparing
the actual outcome with what markets expected (0 if no expectation is stated or it matched);
impact in [0, 1] on Nifty/Sensex today (0.8+ only for market-wide shocks such as wars, surprise
policy moves or large macro misses); affected sectors/stocks/indices; a rationale under 300
characters. Judge only from the text given. Do not invent numbers. Use the id of each story."""

_BULL = ["rate cut", "beats", "surge", "rally", "record high", "ceasefire", "upgrade", "inflows", "eases", "cools", "stimulus", "deal signed"]
_BEAR = ["rate hike", "misses", "plunge", "crash", "war", "attack", "sanctions", "downgrade", "outflows", "tariff", "selloff", "default", "spikes"]


def rule_score(i: int, cluster: NewsCluster) -> NewsScore:
    t = f" {cluster.lead.title.lower()} "
    bull = sum(w in t for w in _BULL)
    bear = sum(w in t for w in _BEAR)
    direction = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    impact = min(0.3 + 0.1 * (len(cluster.sources) - 1), 0.5) if direction != "neutral" else 0.1
    return NewsScore(id=i, category=(cluster.topics or ["other"])[0], direction=direction, surprise=0.0, impact=impact, affected=cluster.relevant_to, rationale="rule-based keyword score (LLM unavailable)")


def build_user_prompt(clusters: list[NewsCluster]) -> str:
    stories = [
        {"id": i, "title": c.lead.title, "summary": c.lead.summary[:400], "sources": c.sources, "official": c.lead.official, "time_ist": c.first_seen.isoformat(timespec="minutes")}
        for i, c in enumerate(clusters)
    ]
    return "Score these stories:\n" + json.dumps(stories, ensure_ascii=False)


async def score_clusters(clusters: list[NewsCluster], llm: LLMService | None, batch_size: int) -> list[tuple[NewsScore, str]]:
    """Returns (score, scored_by) aligned with `clusters`."""
    out: list[tuple[NewsScore, str] | None] = [None] * len(clusters)
    for start in range(0, len(clusters), batch_size):
        batch = clusters[start : start + batch_size]
        result = await llm.run("news_scoring", SYSTEM_PROMPT, build_user_prompt(batch), NewsScoreBatch) if llm else None
        by_id = {s.id: s for s in result.scores} if result else {}
        for j, c in enumerate(batch):
            s = by_id.get(j)
            out[start + j] = (s.model_copy(update={"id": start + j}), "llm") if s else (rule_score(start + j, c), "rules")
    return [x for x in out if x is not None]
