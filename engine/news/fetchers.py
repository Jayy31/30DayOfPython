"""News fetchers: RSS (feedparser), GDELT doc API, Finnhub. All behind one interface.

HTTP is injected (httpx.AsyncClient) so tests run offline. Polite defaults: one
request per source per poll, timeouts, a descriptive user agent.
"""

from __future__ import annotations

import calendar
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import feedparser
import httpx

from engine.config.schemas import NewsSourceConfig
from engine.news.models import NewsItem

log = logging.getLogger(__name__)
USER_AGENT = "papertrade-research/0.1 (paper trading; low-rate polling)"


class NewsFetcher(ABC):
    def __init__(self, source: NewsSourceConfig) -> None:
        self.source = source

    @abstractmethod
    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]: ...


def _ts(struct) -> datetime | None:  # type: ignore[no-untyped-def]
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc) if struct else None


def parse_rss(text: str, source: NewsSourceConfig) -> list[NewsItem]:
    feed = feedparser.parse(text)
    out = []
    for e in feed.entries:
        ts = _ts(e.get("published_parsed") or e.get("updated_parsed"))
        title = (e.get("title") or "").strip()
        if not title or ts is None:
            continue
        out.append(NewsItem(source=source.id, title=title, published_at=ts, url=e.get("link", ""), summary=(e.get("summary") or "")[:1000], official=source.official))
    return out


class RssFetcher(NewsFetcher):
    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        r = await client.get(self.source.url, headers={"User-Agent": USER_AGENT}, timeout=15)
        r.raise_for_status()
        return parse_rss(r.text, self.source)


class GdeltFetcher(NewsFetcher):
    URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        params = {"query": self.source.query, "mode": "ArtList", "format": "json", "maxrecords": "50", "timespan": "1h", "sort": "DateDesc"}
        r = await client.get(self.URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        out = []
        for a in r.json().get("articles", []):
            try:
                ts = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            out.append(NewsItem(source=f"{self.source.id}:{a.get('domain', '')}", title=a.get("title", "").strip(), published_at=ts, url=a.get("url", "")))
        return [i for i in out if i.title]


class FinnhubFetcher(NewsFetcher):
    URL = "https://finnhub.io/api/v1/news"

    def __init__(self, source: NewsSourceConfig, api_key: str | None) -> None:
        super().__init__(source)
        self.api_key = api_key

    async def fetch(self, client: httpx.AsyncClient) -> list[NewsItem]:
        if not self.api_key:
            return []
        r = await client.get(self.URL, params={"category": self.source.query or "general"}, headers={"X-Finnhub-Token": self.api_key}, timeout=15)
        r.raise_for_status()
        return [
            NewsItem(source=f"{self.source.id}:{a.get('source', '')}", title=a["headline"].strip(), published_at=datetime.fromtimestamp(a["datetime"], tz=timezone.utc), url=a.get("url", ""), summary=(a.get("summary") or "")[:1000])
            for a in r.json()
            if a.get("headline") and a.get("datetime")
        ]


def build_fetchers(sources: list[NewsSourceConfig], finnhub_key: str | None = None) -> list[NewsFetcher]:
    out: list[NewsFetcher] = []
    for s in sources:
        if not s.enabled:
            continue
        if s.kind == "rss":
            out.append(RssFetcher(s))
        elif s.kind == "gdelt":
            out.append(GdeltFetcher(s))
        elif s.kind == "finnhub":
            out.append(FinnhubFetcher(s, finnhub_key))
    return out


async def fetch_all(fetchers: list[NewsFetcher], client: httpx.AsyncClient) -> tuple[list[NewsItem], dict[str, str]]:
    """Fetch every source; one failing source never blocks the others."""
    import asyncio

    results = await asyncio.gather(*(f.fetch(client) for f in fetchers), return_exceptions=True)
    items: list[NewsItem] = []
    errors: dict[str, str] = {}
    for f, res in zip(fetchers, results, strict=True):
        if isinstance(res, BaseException):
            errors[f.source.id] = f"{type(res).__name__}: {res}"
            log.warning("news source failed", extra={"source": f.source.id, "error": str(res)})
        else:
            items.extend(res)
    return items, errors
