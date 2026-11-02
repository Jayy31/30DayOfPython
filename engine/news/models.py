from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.core.timeutils import to_ist

_WS = re.compile(r"[^a-z0-9 ]+")


def normalize_title(title: str) -> str:
    return " ".join(_WS.sub(" ", title.lower()).split())


class NewsItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    title: str
    published_at: datetime
    url: str = ""
    summary: str = ""
    official: bool = False

    @field_validator("published_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return to_ist(v)

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(normalize_title(self.title).encode()).hexdigest()


class NewsCluster(BaseModel):
    """One story, possibly reported by several sources."""

    uid: str = Field(default_factory=lambda: uuid.uuid4().hex)
    lead: NewsItem
    sources: list[str]
    first_seen: datetime
    topics: list[str] = Field(default_factory=list)
    relevant_to: list[str] = Field(default_factory=list)   # NIFTY / HEAVYWEIGHTS / macro buckets


Direction = Literal["bullish", "bearish", "neutral"]


class NewsScore(BaseModel):
    """LLM (or rule) output per story. Schema-validated."""

    id: int = Field(description="index of the story in the input list")
    category: str
    direction: Direction
    surprise: float = Field(ge=-1, le=1, description="actual vs expected: -1 much worse, +1 much better, 0 as expected/unknown")
    impact: float = Field(ge=0, le=1, description="expected impact on Indian index levels")
    affected: list[str] = Field(default_factory=list, description="sectors / stocks / indices")
    rationale: str = Field(max_length=300)


class NewsScoreBatch(BaseModel):
    scores: list[NewsScore]


class ScoredNews(BaseModel):
    cluster: NewsCluster
    score: NewsScore
    scored_by: Literal["llm", "rules"]
    breaking: bool = False
    market_confirmed: bool | None = None   # None = not checked yet

    @property
    def effective_impact(self) -> float:
        # high impact only if markets confirm; unconfirmed stories are capped
        if self.market_confirmed is False:
            return min(self.score.impact, 0.4)
        return self.score.impact
