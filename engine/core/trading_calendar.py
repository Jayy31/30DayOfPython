"""Trading-day calendar (weekends + configured exchange holidays)."""

from __future__ import annotations

from datetime import date, timedelta


class TradingCalendar:
    def __init__(self, holidays: dict[int, list[str]]) -> None:
        self._holidays = {date.fromisoformat(d) for days in holidays.values() for d in days}

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._holidays

    def is_holiday(self, d: date) -> bool:
        return d in self._holidays

    def next_trading_day(self, d: date) -> date:
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def previous_trading_day(self, d: date) -> date:
        d -= timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d
