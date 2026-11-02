"""Alerts: Telegram in production, in-memory/log for replay and tests. Never raises."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

log = logging.getLogger(__name__)
TELEGRAM_LIMIT = 4000


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts


class Notifier(ABC):
    @abstractmethod
    async def send(self, text: str) -> bool: ...


class MemoryNotifier(Notifier):
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        log.info("alert", extra={"alert": text[:200]})
        return True


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.AsyncClient(timeout=10)

    async def send(self, text: str) -> bool:
        ok = True
        for part in split_message(text):
            try:
                r = await self.client.post(self.url, json={"chat_id": self.chat_id, "text": part, "disable_web_page_preview": True})
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.error("telegram send failed", extra={"error": str(e)})
                ok = False
        return ok
