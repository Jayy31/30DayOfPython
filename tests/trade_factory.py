"""Builds realistic TradeRecords by driving a real Position along a premium path."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from engine.broker_paper.position import Position
from engine.core.models import Direction, Index, OptionContract, OptionQuote, OptionType
from engine.core.timeutils import IST
from engine.store.base import TradeRecord


def make_trade(cfg, day: date, no: int, prices: list[float], *, lots: int = 2, entry: float = 101.0, start: time = time(9, 45),
               index: Index = Index.NIFTY, tags: list[str] | None = None, setup: str = "orb", regime: str = "trend", step_min: int = 1) -> TradeRecord:
    lot = 65 if index is Index.NIFTY else 20
    contract = OptionContract(index=index, expiry=day + timedelta(days=7), strike=24000, option_type=OptionType.CE, lot_size=lot, symbol=f"{index}24000CE")
    t0 = datetime.combine(day, start, IST)
    pos = Position(contract=contract, exchange="NSE" if index is Index.NIFTY else "BSE", direction=Direction.LONG, lots=lots,
                   entry_price=entry, entry_ts=t0, rules=cfg.rules, charges=cfg.charges, setup=setup, tags=tags or [])
    ts = t0
    for px in prices:
        ts += timedelta(minutes=step_min)
        q = OptionQuote(ts=ts, bid=px, ask=px + 0.5, ltp=px)
        if not pos.is_open:
            pos.path.append(q)   # live engine keeps extending the path after exit
            continue
        if ts.time() >= cfg.rules.square_off:
            pos.square_off(q)
        else:
            pos.on_quote(q)
    if pos.is_open:
        pos.square_off(OptionQuote(ts=ts, bid=prices[-1], ask=prices[-1] + 0.5, ltp=prices[-1]))
    return TradeRecord(position=pos, day=day, trade_no=no, setup=setup, reason_log={"regime": {"label": regime}, "probability": {"p": 0.62, "label": "uncalibrated (priors)"}}, capital_after=0)


WIN = [110, 122, 128, 133, 140, 150, 145, 140, 135, 120]    # 1R, 1.5R, then lock-SL/trail exit
LOSS = [95, 85, 80]                                         # straight to SL
