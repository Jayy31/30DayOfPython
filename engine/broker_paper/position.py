"""Open-position state machine: SL, 1R / 1.5R partial bookings, SL steps and swing trail.

Triggers are evaluated on the bid (what we could actually sell at); fills are at
bid - slippage. Stages: 0 = initial, 1 = 1R done, 2 = 1.5R done (trail active).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from engine.broker_paper.charges import Side, leg_charges
from engine.broker_paper.fills import round_down_tick, round_up_tick, sell_fill_price
from engine.broker_paper.ladder import BookingPlan, booking_plan
from engine.broker_paper.swing import SwingTrail
from engine.config.schemas import ChargesConfig, RulesConfig
from engine.core.models import Direction, OptionContract, OptionQuote


class LegReason(StrEnum):
    ENTRY = "entry"
    TARGET_1R = "target_1r"
    TARGET_1_5R = "target_1_5r"
    SL = "sl"                    # initial stop
    BREAKEVEN_SL = "breakeven_sl"
    LOCK_SL = "lock_sl"          # +20% stop after 1.5R
    TRAIL_SL = "trail_sl"
    SQUARE_OFF = "square_off"


class Outcome(StrEnum):
    TARGET = "target"  # reached 1R at any point
    SL = "sl"          # stopped out before reaching 1R
    TIME = "time"      # square-off before reaching 1R


class Leg(BaseModel):
    ts: datetime
    side: Side
    lots: int
    price: float
    reason: LegReason
    charges: float


class Position:
    def __init__(
        self,
        *,
        contract: OptionContract,
        exchange: str,
        direction: Direction,
        lots: int,
        entry_price: float,
        entry_ts: datetime,
        rules: RulesConfig,
        charges: ChargesConfig,
        setup: str = "",
        probability: float | None = None,
        tags: list[str] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.contract = contract
        self.exchange = exchange
        self.direction = direction
        self.setup = setup
        self.probability = probability
        self.tags = list(tags or [])
        self.rules = rules
        self.charges_cfg = charges
        self.tick = charges.tick_size

        self.lots = lots
        self.open_lots = lots
        self.plan: BookingPlan = booking_plan(lots)
        self.entry_price = entry_price
        self.entry_ts = entry_ts
        self.risk_per_unit = entry_price * rules.sl_pct / 100
        self.initial_sl = round_down_tick(entry_price - self.risk_per_unit, self.tick)
        self.sl = self.initial_sl
        self.target_1 = round_up_tick(entry_price + self.risk_per_unit * rules.first_target_rr, self.tick)
        self.target_2 = round_up_tick(entry_price + self.risk_per_unit * rules.second_target_rr, self.tick)
        self.stage = 0
        self.reached_1r = False
        self.trailed = False
        self.trail = SwingTrail(rules.trail)
        self.outcome: Outcome | None = None
        self.exit_ts: datetime | None = None
        self.last_quote: OptionQuote | None = None
        self.path: list[OptionQuote] = []   # every quote seen while open (for shadow re-simulation)

        qty = lots * contract.lot_size
        self.legs: list[Leg] = [
            Leg(
                ts=entry_ts,
                side=Side.BUY,
                lots=lots,
                price=entry_price,
                reason=LegReason.ENTRY,
                charges=leg_charges(Side.BUY, entry_price, qty, exchange, charges).total,
            )
        ]

    # ----------------------------------------------------------------- state
    @property
    def is_open(self) -> bool:
        return self.open_lots > 0

    def _sell(self, quote: OptionQuote, lots: int, reason: LegReason) -> Leg:
        lots = min(lots, self.open_lots)
        price = sell_fill_price(quote, self.charges_cfg)
        qty = lots * self.contract.lot_size
        leg = Leg(
            ts=quote.ts,
            side=Side.SELL,
            lots=lots,
            price=price,
            reason=reason,
            charges=leg_charges(Side.SELL, price, qty, self.exchange, self.charges_cfg).total,
        )
        self.legs.append(leg)
        self.open_lots -= lots
        if not self.is_open:
            self.exit_ts = quote.ts
            self.outcome = Outcome.TARGET if self.reached_1r else (Outcome.TIME if reason is LegReason.SQUARE_OFF else Outcome.SL)
        return leg

    def _stop_reason(self) -> LegReason:
        if self.trailed:
            return LegReason.TRAIL_SL
        return (LegReason.SL, LegReason.BREAKEVEN_SL, LegReason.LOCK_SL)[self.stage]

    # ------------------------------------------------------------- tick path
    def on_quote(self, quote: OptionQuote) -> list[Leg]:
        """Process one tick (or REST quote while the feed is down). Returns exit legs."""
        if not self.is_open:
            return []
        self.last_quote = quote
        self.path.append(quote)
        self.trail.update(quote.ts, quote.ltp)
        out: list[Leg] = []
        bid = quote.bid

        if bid <= self.sl:
            out.append(self._sell(quote, self.open_lots, self._stop_reason()))
            return out

        if self.stage == 0 and bid >= self.target_1:
            self.stage, self.reached_1r = 1, True
            if self.plan.at_first:
                out.append(self._sell(quote, self.plan.at_first, LegReason.TARGET_1R))
            self.sl = max(self.sl, round_up_tick(self.entry_price * (1 + self.rules.breakeven_buffer_pct / 100), self.tick))

        if self.stage == 1 and bid >= self.target_2:
            self.stage = 2
            if self.plan.at_second:
                out.append(self._sell(quote, self.plan.at_second, LegReason.TARGET_1_5R))
            self.sl = max(self.sl, round_up_tick(self.entry_price * (1 + self.rules.lock_pct_at_second_target / 100), self.tick))

        if self.stage == 2:
            level = self.trail.stop_level()
            if level is not None:
                level = round_down_tick(level, self.tick)
                if level > self.sl:  # never moves down
                    self.sl = level
                    self.trailed = True
        return out

    def square_off(self, quote: OptionQuote) -> list[Leg]:
        if not self.is_open:
            return []
        self.last_quote = quote
        self.path.append(quote)
        return [self._sell(quote, self.open_lots, LegReason.SQUARE_OFF)]

    # ------------------------------------------------------------- results
    @property
    def total_charges(self) -> float:
        return round(sum(leg.charges for leg in self.legs), 2)

    @property
    def gross_pnl(self) -> float:
        ls = self.contract.lot_size
        sells = sum(leg.price * leg.lots * ls for leg in self.legs if leg.side is Side.SELL)
        return round(sells - self.entry_price * (self.lots - self.open_lots) * ls, 2)

    @property
    def net_pnl(self) -> float:
        return round(self.gross_pnl - self.total_charges, 2)

    @property
    def initial_risk(self) -> float:
        return (self.entry_price - self.initial_sl) * self.lots * self.contract.lot_size

    @property
    def r_multiple(self) -> float:
        return round(self.net_pnl / self.initial_risk, 3) if self.initial_risk else 0.0

    @property
    def avg_exit_price(self) -> float | None:
        sells = [leg for leg in self.legs if leg.side is Side.SELL]
        lots = sum(leg.lots for leg in sells)
        return round(sum(leg.price * leg.lots for leg in sells) / lots, 2) if lots else None

    def capital_required(self) -> float:
        return self.entry_price * self.lots * self.contract.lot_size + self.legs[0].charges
