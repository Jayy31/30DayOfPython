"""Partial-booking plan per lot count (locked rules)."""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class BookingPlan(BaseModel):
    at_first: int   # lots booked at 1R
    at_second: int  # lots booked at 1.5R
    trail: int      # lots left to trail

    @model_validator(mode="after")
    def _check(self) -> BookingPlan:
        if min(self.at_first, self.at_second) < 0 or self.trail < 1:
            raise ValueError("invalid booking plan")
        return self


def booking_plan(lots: int) -> BookingPlan:
    """1 -> 0/0/1, 2 -> 1/0/1, 3 -> 1/1/1, n>=4 -> floor(n/2) / max(1, floor(n/4)) / rest (rest >= 1)."""
    if lots < 1:
        raise ValueError("lots must be >= 1")
    if lots == 1:
        return BookingPlan(at_first=0, at_second=0, trail=1)
    if lots == 2:
        return BookingPlan(at_first=1, at_second=0, trail=1)
    if lots == 3:
        return BookingPlan(at_first=1, at_second=1, trail=1)
    first = lots // 2
    second = min(max(1, lots // 4), lots - first - 1)
    return BookingPlan(at_first=first, at_second=second, trail=lots - first - second)
