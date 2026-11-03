"""Starting set of named setups."""

from __future__ import annotations

from engine.analytics.setups.base import MarketContext, Setup, SetupCandidate, default_target, direction_of, register
from engine.core.models import Direction


@register
class OpeningRangeBreakout(Setup):
    """ORB breakout (trend days) or ORB failure (break rejected back inside)."""

    name = "orb"
    family = "breakout"

    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None:
        if not ctx.or_complete or ctx.or_high is None or ctx.or_low is None or len(ctx.closes_1m) < 2:
            return None
        fb = ctx.failed_break
        if fb is not None and fb.level.source in ("OR_HIGH", "OR_LOW"):
            d = Direction.SHORT if fb.side == "above" else Direction.LONG
            target = ctx.or_low if d is Direction.SHORT else ctx.or_high
            return SetupCandidate(self.name, "reversal", d, target, fb.extreme, (f"ORB failure at {fb.level.price:.0f}",))
        prev, last = ctx.closes_1m[-2], ctx.closes_1m[-1]
        if prev <= ctx.or_high < last:
            return SetupCandidate(self.name, self.family, Direction.LONG, default_target(ctx, Direction.LONG), ctx.or_low, ("close above OR high",))
        if prev >= ctx.or_low > last:
            return SetupCandidate(self.name, self.family, Direction.SHORT, default_target(ctx, Direction.SHORT), ctx.or_high, ("close below OR low",))
        return None


@register
class LevelBounce(Setup):
    """Touch of a confluence zone followed by a close back away from it."""

    name = "level_bounce"
    family = "mean_reversion"

    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None:
        if len(ctx.closes_1m) < 3 or not ctx.zones:
            return None
        recent = ctx.closes_1m[-3:]
        for z in ctx.zones:
            tol = max(z.high - z.low, z.price * 0.0005)
            touched_from_above = min(recent) <= z.high + tol and recent[-1] > recent[-2] and recent[-1] > z.high
            touched_from_below = max(recent) >= z.low - tol and recent[-1] < recent[-2] and recent[-1] < z.low
            if touched_from_above and ctx.spot > z.price:
                return SetupCandidate(self.name, self.family, Direction.LONG, default_target(ctx, Direction.LONG), z.low - tol, (f"bounce off {'/'.join(sorted(set(z.sources)))} {z.price:.0f}",))
            if touched_from_below and ctx.spot < z.price:
                return SetupCandidate(self.name, self.family, Direction.SHORT, default_target(ctx, Direction.SHORT), z.high + tol, (f"rejection at {'/'.join(sorted(set(z.sources)))} {z.price:.0f}",))
        return None


@register
class VwapReclaim(Setup):
    """Close back above VWAP after trading below it (or the reverse)."""

    name = "vwap_reclaim"
    family = "vwap"

    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None:
        if ctx.vwap is None or len(ctx.closes_1m) < 4:
            return None
        before, prev, last = ctx.closes_1m[-4:-2], ctx.closes_1m[-2], ctx.closes_1m[-1]
        v = ctx.vwap
        if all(c < v for c in before) and prev >= v and last > v:
            return SetupCandidate(self.name, self.family, Direction.LONG, default_target(ctx, Direction.LONG), min(before), ("VWAP reclaim",))
        if all(c > v for c in before) and prev <= v and last < v:
            return SetupCandidate(self.name, self.family, Direction.SHORT, default_target(ctx, Direction.SHORT), max(before), ("VWAP rejection",))
        return None


@register
class TrendPullback(Setup):
    """Negative-gamma trend: pullback toward VWAP in the bias direction, then resumption."""

    name = "trend_pullback"
    family = "trend"

    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None:
        if ctx.gex is None or ctx.gex.regime != "negative" or ctx.vwap is None or len(ctx.closes_1m) < 6:
            return None
        d = direction_of(ctx.bias.direction)
        if d is None:
            return None
        c = ctx.closes_1m[-6:]
        s = 1 if d is Direction.LONG else -1
        on_trend_side = all((x - ctx.vwap) * s > 0 for x in c)
        pulled_back = (c[-2] - max(c[:-2]) if s > 0 else min(c[:-2]) - c[-2]) < 0
        resumed = (c[-1] - c[-2]) * s > 0
        if on_trend_side and pulled_back and resumed:
            inval = min(c[-3:]) if s > 0 else max(c[-3:])
            return SetupCandidate(self.name, self.family, d, default_target(ctx, d), inval, ("pullback held above VWAP" if s > 0 else "pullback held below VWAP",))
        return None


@register
class OiShiftReversal(Setup):
    """Writers shift walls against the recent move: e.g. after a drop, put writers move up."""

    name = "oi_shift_reversal"
    family = "reversal"

    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None:
        ws = ctx.wall_shift
        if ws is None or ws.bias == 0 or len(ctx.closes_1m) < 10:
            return None
        move = ctx.closes_1m[-1] - ctx.closes_1m[-10]
        if move * ws.bias >= 0:
            return None   # shift agrees with the move: not a reversal
        d = direction_of(ws.bias)
        assert d is not None
        recent = ctx.closes_1m[-10:]
        inval = min(recent) if d is Direction.LONG else max(recent)
        return SetupCandidate(self.name, self.family, d, default_target(ctx, d), inval, (f"walls shifted (call {ws.call_wall_move:+.0f}, put {ws.put_wall_move:+.0f}) against move {move:+.0f}",))


@register
class FailedPdhPdlBreak(Setup):
    """PDH/PDL break rejected back inside: trapped traders fuel the reversal."""

    name = "failed_break"
    family = "reversal"

    def evaluate(self, ctx: MarketContext) -> SetupCandidate | None:
        fb = ctx.failed_break
        if fb is None or fb.level.source not in ("PDH", "PDL"):
            return None
        d = Direction.SHORT if fb.side == "above" else Direction.LONG
        return SetupCandidate(self.name, self.family, d, default_target(ctx, d), fb.extreme, (f"failed {fb.level.source} break, extreme {fb.extreme:.0f}",))
