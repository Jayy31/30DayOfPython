"""Option writers' map: OI walls, fresh writing, wall shifts, PCR."""

from __future__ import annotations

from dataclasses import dataclass

from engine.core.models import OptionType
from engine.data.models import OptionChain


@dataclass(frozen=True)
class OiMap:
    call_wall: float           # resistance: highest (fresh-weighted) call OI
    put_wall: float            # support: highest (fresh-weighted) put OI
    pcr: float                 # total put OI / total call OI
    fresh_call_writing: float  # strike with the largest call OI addition today
    fresh_put_writing: float


def oi_map(chain: OptionChain, fresh_weight: float = 1.0) -> OiMap:
    """Wall score = OI + fresh_weight * max(oi_change, 0): fresh writing counts extra."""
    calls = [r for r in chain.rows if r.option_type is OptionType.CE]
    puts = [r for r in chain.rows if r.option_type is OptionType.PE]
    if not calls or not puts:
        raise ValueError("chain needs both calls and puts")

    def score(r) -> float:  # type: ignore[no-untyped-def]
        return r.oi + fresh_weight * max(r.oi_change, 0)

    call_oi = sum(r.oi for r in calls)
    return OiMap(
        call_wall=max(calls, key=score).strike,
        put_wall=max(puts, key=score).strike,
        pcr=sum(r.oi for r in puts) / call_oi if call_oi else float("inf"),
        fresh_call_writing=max(calls, key=lambda r: r.oi_change).strike,
        fresh_put_writing=max(puts, key=lambda r: r.oi_change).strike,
    )


@dataclass(frozen=True)
class WallShift:
    call_wall_move: float   # <0: call writers moving down = resistance coming down (bearish)
    put_wall_move: float    # >0: put writers moving up = support rising (bullish)

    @property
    def bias(self) -> int:
        """+1 bullish, -1 bearish, 0 mixed/none."""
        score = (self.put_wall_move > 0) - (self.put_wall_move < 0) + (self.call_wall_move > 0) - (self.call_wall_move < 0)
        return (score > 0) - (score < 0)


def wall_shift(before: OiMap, after: OiMap) -> WallShift:
    return WallShift(call_wall_move=after.call_wall - before.call_wall, put_wall_move=after.put_wall - before.put_wall)
