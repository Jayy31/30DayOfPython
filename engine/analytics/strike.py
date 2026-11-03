"""Layer E: choose the strike (ATM +/- N) with the best expected gain per rupee of premium."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from engine.analytics.greeks import Greeks, greeks_from_price, time_to_expiry
from engine.broker_paper.charges import round_trip_estimate
from engine.broker_paper.sizing import lots_for_capital
from engine.config.schemas import ChargesConfig, StrikeConfig
from engine.core.models import Direction
from engine.data.models import OptionChain, OptionChainRow


@dataclass(frozen=True)
class StrikeCandidate:
    row: OptionChainRow
    greeks: Greeks | None
    mid: float
    expected_gain: float      # Rs per unit if the target is reached within hold_minutes
    theta_cost: float         # Rs per unit over hold_minutes
    score: float              # (expected gain - costs) / premium
    lots: int
    rejects: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.rejects


def select_expiry(expiries: list[date], now: datetime, switch_after: datetime | None = None) -> date:
    """Nearest expiry, except on expiry day: next expiry after the switch time."""
    future = sorted(e for e in expiries if e >= now.date())
    if not future:
        raise ValueError("no live expiry")
    if future[0] == now.date() and len(future) > 1 and (switch_after is None or now >= switch_after):
        return future[1]
    return future[0]


def evaluate_strikes(
    chain: OptionChain,
    direction: Direction,
    target_move_points: float,
    now: datetime,
    *,
    cfg: StrikeConfig,
    charges: ChargesConfig,
    exchange: str,
    lot_size: int,
    capital: float,
    min_delta: float,
    strike_range: int,
    reference_iv: float | None = None,
) -> list[StrikeCandidate]:
    """All candidates (best first); rejected ones carry reasons."""
    ot = direction.option_type
    strikes = chain.strikes()
    atm = chain.atm_strike()
    i = strikes.index(atm)
    window = strikes[max(0, i - strike_range) : i + strike_range + 1]
    t = time_to_expiry(now, chain.expiry)
    hold_frac_day = cfg.hold_minutes / (24 * 60)
    out: list[StrikeCandidate] = []
    for k in window:
        row = chain.row(k, ot)
        if row is None:
            continue
        rejects: list[str] = []
        mid = (row.bid + row.ask) / 2 if row.bid > 0 and row.ask > 0 else row.ltp
        g = greeks_from_price(mid, chain.spot, k, t, ot) if mid > 0 else None
        if g is None:
            out.append(StrikeCandidate(row, None, mid, 0, 0, float("-inf"), 0, ("no valid IV/greeks",)))
            continue
        spread_pct = (row.ask - row.bid) / mid * 100 if mid else 100
        move = target_move_points * (1 if direction is Direction.LONG else -1)
        gain = g.delta * move + 0.5 * g.gamma * move * move
        theta_cost = abs(g.theta) * hold_frac_day
        lots = lots_for_capital(capital, row.ask, lot_size, exchange, charges)
        qty = max(lots, 1) * lot_size
        cost_per_unit = round_trip_estimate(mid, qty, exchange, charges) / qty + (row.ask - row.bid)
        score = (gain - theta_cost - cost_per_unit) / mid

        if abs(g.delta) < min_delta:
            rejects.append(f"delta {abs(g.delta):.2f} < {min_delta}")
        if spread_pct > cfg.max_spread_pct:
            rejects.append(f"spread {spread_pct:.1f}% > {cfg.max_spread_pct}%")
        if row.volume < cfg.min_volume:
            rejects.append(f"volume {row.volume} < {cfg.min_volume}")
        if row.oi < cfg.min_oi:
            rejects.append(f"OI {row.oi} < {cfg.min_oi}")
        if lots < 1:
            rejects.append("premium exceeds capital for 1 lot")
        if gain < cfg.min_gain_to_theta * theta_cost:
            rejects.append(f"expected gain {gain:.1f} < {cfg.min_gain_to_theta}x theta {theta_cost:.1f}")
        if reference_iv and g.iv > reference_iv * (1 + cfg.max_iv_jump_pct / 100):
            rejects.append(f"IV spiking {g.iv:.1%} vs ref {reference_iv:.1%}")
        out.append(StrikeCandidate(row, g, mid, gain, theta_cost, score, lots, tuple(rejects)))
    return sorted(out, key=lambda c: (c.ok, c.score), reverse=True)
