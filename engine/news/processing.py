"""Dedupe, relevance filter, spike detection and market confirmation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from engine.config.schemas import NewsConfig
from engine.news.models import NewsCluster, NewsItem, normalize_title

_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at", "by", "is", "with", "after", "from", "amid", "over"}


def _tokens(title: str) -> set[str]:
    return {t for t in normalize_title(title).split() if t not in _STOP and len(t) > 1}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class Deduper:
    """Clusters similar headlines across sources within a time window. Stateful across polls."""

    def __init__(self, similarity: float, window: timedelta) -> None:
        self.similarity = similarity
        self.window = window
        self.clusters: list[tuple[set[str], NewsCluster]] = []
        self._hashes: set[str] = set()

    def add(self, items: Iterable[NewsItem]) -> list[NewsCluster]:
        """Returns clusters that are new or gained a new source in this call."""
        touched: dict[int, NewsCluster] = {}
        for item in sorted(items, key=lambda i: i.published_at):
            key = f"{item.source}|{item.text_hash}"
            if key in self._hashes:
                continue
            self._hashes.add(key)
            toks = _tokens(item.title)
            match = None
            for toks2, c in self.clusters:
                if abs(item.published_at - c.first_seen) <= self.window and jaccard(toks, toks2) >= self.similarity:
                    match = c
                    break
            if match is None:
                match = NewsCluster(lead=item, sources=[item.source], first_seen=item.published_at)
                self.clusters.append((toks, match))
            elif item.source not in match.sources:
                match.sources.append(item.source)
                if item.official and not match.lead.official:
                    match.lead = item   # prefer the official wording
            touched[id(match)] = match
        return list(touched.values())

    def prune(self, now: datetime) -> None:
        self.clusters = [(t, c) for t, c in self.clusters if now - c.first_seen <= self.window * 2]


def _match(text: str, words: list[str]) -> bool:
    padded = f" {text} "
    return any(f" {normalize_title(w)} " in padded for w in words)


def tag_relevance(cluster: NewsCluster, cfg: NewsConfig) -> NewsCluster:
    text = normalize_title(f"{cluster.lead.title} {cluster.lead.summary}")
    cluster.topics = [t for t, words in cfg.topics.items() if _match(text, words)]
    cluster.relevant_to = [k for k, words in cfg.index_keywords.items() if _match(text, words)]
    return cluster


def is_relevant(cluster: NewsCluster) -> bool:
    return bool(cluster.topics or cluster.relevant_to or cluster.lead.official)


class SpikeDetector:
    """A topic reported by many distinct sources in a short window = breaking event."""

    def __init__(self, min_sources: int, window: timedelta) -> None:
        self.min_sources = min_sources
        self.window = window
        self._seen: list[tuple[datetime, str, str]] = []   # (ts, topic, source)

    def add(self, cluster: NewsCluster) -> list[str]:
        for topic in cluster.topics:
            for src in cluster.sources:
                self._seen.append((cluster.first_seen, topic, src.split(":")[0]))
        latest = max((t for t, _, _ in self._seen), default=cluster.first_seen)
        self._seen = [x for x in self._seen if latest - x[0] <= self.window]
        spiking = []
        for topic in cluster.topics:
            srcs = {s for _, t, s in self._seen if t == topic}
            if len(srcs) >= self.min_sources:
                spiking.append(topic)
        return spiking


def market_confirms(moves_pct: Mapping[str, float | None], thresholds: Mapping[str, float]) -> bool | None:
    """moves_pct: instrument -> % move since the story. None if no data at all."""
    checked = [(k, v) for k, v in moves_pct.items() if v is not None and k in thresholds]
    if not checked:
        return None
    return any(abs(v) >= thresholds[k] for k, v in checked)
