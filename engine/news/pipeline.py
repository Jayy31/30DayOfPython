"""News pipeline: fetch -> dedupe -> filter -> score -> spike -> confirm -> bias + alerts.

Output feeds the Layer B 'news' driver, trade reasons and breaking-news alerts.
News is context only: it never triggers an entry.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.config.schemas import NewsConfig
from engine.llm.service import LLMService
from engine.news.models import NewsItem, ScoredNews
from engine.news.processing import Deduper, SpikeDetector, is_relevant, market_confirms, tag_relevance
from engine.news.scoring import score_clusters

MovesFn = Callable[[datetime], Mapping[str, float | None]]   # % moves since a timestamp


@dataclass
class NewsUpdate:
    scored: list[ScoredNews] = field(default_factory=list)
    breaking: list[ScoredNews] = field(default_factory=list)


class NewsPipeline:
    def __init__(self, cfg: NewsConfig, llm: LLMService | None = None, moves: MovesFn | None = None, half_life_minutes: float = 120) -> None:
        self.cfg = cfg
        self.llm = llm
        self.moves = moves
        self.half_life = half_life_minutes
        self.deduper = Deduper(cfg.dedupe_similarity, timedelta(minutes=cfg.dedupe_window_minutes))
        self.spikes = SpikeDetector(cfg.spike_min_sources, timedelta(minutes=cfg.spike_window_minutes))
        self.history: dict[str, ScoredNews] = {}

    async def process(self, items: list[NewsItem], now: datetime) -> NewsUpdate:
        touched = [tag_relevance(c, self.cfg) for c in self.deduper.add(items)]
        relevant = [c for c in touched if is_relevant(c)]
        fresh = [c for c in relevant if c.uid not in self.history]
        scores = await score_clusters(fresh, self.llm, self.cfg.llm_batch_size)
        upd = NewsUpdate()
        for c, (score, by) in zip(fresh, scores, strict=True):
            self.history[c.uid] = ScoredNews(cluster=c, score=score, scored_by=by)  # type: ignore[arg-type]
        for c in relevant:
            sn = self.history[c.uid]
            spiking = self.spikes.add(c)
            if spiking and not sn.breaking:
                sn.breaking = True
                upd.breaking.append(sn)
            if self.moves is not None:
                sn.market_confirmed = market_confirms(self.moves(c.first_seen), self.cfg.confirm_moves_pct)
            upd.scored.append(sn)
        self.deduper.prune(now)
        return upd

    def bias(self, now: datetime) -> float | None:
        """Layer B 'news' driver in [-1, 1]: impact-weighted, time-decayed direction. None if no news."""
        num = den = 0.0
        for sn in self.history.values():
            age = (now - sn.cluster.first_seen).total_seconds() / 60
            if age < 0:
                continue
            w = sn.effective_impact * math.exp(-math.log(2) * age / self.half_life)
            d = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}[sn.score.direction]
            num += w * d * (1 + 0.5 * abs(sn.score.surprise))
            den += w * 1.5
        if den < 1e-6:
            return None
        return max(-1.0, min(1.0, num / den * 1.5))

    def active_events(self, now: datetime, min_impact: float = 0.6) -> list[str]:
        """Tags for trades: high-impact or breaking topics from the last 6 hours."""
        tags = set()
        for sn in self.history.values():
            if now - sn.cluster.first_seen <= timedelta(hours=6) and (sn.breaking or sn.effective_impact >= min_impact):
                tags.update(sn.cluster.topics or [sn.score.category])
        return sorted(tags)
