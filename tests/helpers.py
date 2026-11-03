from __future__ import annotations

from datetime import date, datetime

from engine.core.models import Index, OptionContract, OptionQuote, OptionType

NIFTY_CE = OptionContract(index=Index.NIFTY, expiry=date(2026, 11, 10), strike=24000, option_type=OptionType.CE, lot_size=65)
NIFTY_PE = OptionContract(index=Index.NIFTY, expiry=date(2026, 11, 10), strike=24000, option_type=OptionType.PE, lot_size=65)
SENSEX_CE = OptionContract(index=Index.SENSEX, expiry=date(2026, 11, 12), strike=80000, option_type=OptionType.CE, lot_size=20)


def q(ts: datetime, bid: float, ask: float | None = None, ltp: float | None = None, fast: bool = False) -> OptionQuote:
    ask = bid + 0.5 if ask is None else ask
    return OptionQuote(ts=ts, bid=bid, ask=ask, ltp=ltp if ltp is not None else bid, fast_market=fast)
