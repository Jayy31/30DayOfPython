"""Paper broker. Simulated execution only: there is no order API anywhere in this package."""

from __future__ import annotations

import logging
from datetime import datetime

from engine.broker_paper.fills import buy_fill_price
from engine.broker_paper.position import Leg, Position
from engine.broker_paper.sizing import lots_for_capital
from engine.config.schemas import AppConfig
from engine.core.models import Direction, OptionContract, OptionQuote

log = logging.getLogger(__name__)


class InsufficientCapital(RuntimeError):
    pass


class PaperBroker:
    def __init__(self, cfg: AppConfig, capital: float | None = None) -> None:
        self.cfg = cfg
        self.capital = float(capital if capital is not None else cfg.rules.capital)
        self.position: Position | None = None
        self.closed: list[Position] = []

    def _exchange(self, contract: OptionContract) -> str:
        return self.cfg.instruments[contract.index.value].exchange  # type: ignore[index]

    def open(
        self,
        contract: OptionContract,
        quote: OptionQuote,
        direction: Direction,
        *,
        setup: str = "",
        probability: float | None = None,
        tags: list[str] | None = None,
        size_fraction: float = 1.0,
    ) -> Position:
        """Enter at ask + slippage, timestamped at decision time (quote.ts)."""
        if self.position is not None and self.position.is_open:
            raise RuntimeError("a position is already open")
        if contract.option_type is not direction.option_type:
            raise ValueError(f"{direction} must buy {direction.option_type}, got {contract.option_type}")
        price = buy_fill_price(quote, self.cfg.charges)
        exchange = self._exchange(contract)
        lots = lots_for_capital(self.capital * size_fraction, price, contract.lot_size, exchange, self.cfg.charges)
        if lots < 1:
            raise InsufficientCapital(f"capital {self.capital:.2f} cannot buy 1 lot at {price}")
        pos = Position(
            contract=contract,
            exchange=exchange,
            direction=direction,
            lots=lots,
            entry_price=price,
            entry_ts=quote.ts,
            rules=self.cfg.rules,
            charges=self.cfg.charges,
            setup=setup,
            probability=probability,
            tags=tags,
        )
        self.position = pos
        log.info("paper entry", extra={"trade_id": str(pos.id), "symbol": contract.key, "lots": lots, "price": price})
        return pos

    def on_quote(self, quote: OptionQuote) -> list[Leg]:
        """Feed every tick (or REST fallback quote) for the open contract."""
        pos = self.position
        if pos is None or not pos.is_open:
            return []
        if quote.ts.time() >= self.cfg.rules.square_off:
            legs = pos.square_off(quote)
        else:
            legs = pos.on_quote(quote)
        if not pos.is_open:
            self._settle(pos)
        return legs

    def square_off(self, quote: OptionQuote) -> list[Leg]:
        pos = self.position
        if pos is None or not pos.is_open:
            return []
        legs = pos.square_off(quote)
        self._settle(pos)
        return legs

    def _settle(self, pos: Position) -> None:
        self.capital = round(self.capital + pos.net_pnl, 2)
        self.closed.append(pos)
        self.position = None
        log.info(
            "paper exit",
            extra={"trade_id": str(pos.id), "outcome": pos.outcome, "net_pnl": pos.net_pnl, "capital": self.capital},
        )

    @property
    def has_open_position(self) -> bool:
        return self.position is not None and self.position.is_open

    def mark_to_market(self, now: datetime | None = None) -> float:
        pos = self.position
        if pos is None or pos.last_quote is None:
            return 0.0
        ls = pos.contract.lot_size
        return round(pos.gross_pnl + (pos.last_quote.bid - pos.entry_price) * pos.open_lots * ls - pos.total_charges, 2)
