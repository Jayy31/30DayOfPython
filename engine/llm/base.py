"""Provider-agnostic LLM interface. Outputs are always schema-validated Pydantic objects."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Call failed or output did not validate. Callers fall back to rule-only logic."""


class LLMRefusal(LLMError):
    pass


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    output: T
    model: str
    usage: Usage


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[T],
        max_tokens: int,
        effort: str,
    ) -> LLMResult[T]:
        """`system` is the fixed, cacheable rulebook; `user` holds the volatile content."""
