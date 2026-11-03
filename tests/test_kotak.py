from __future__ import annotations

import ast
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from neo_api_client.websocket.feed.models import DepthLevel as SDepth
from neo_api_client.websocket.feed.models import SFeedIndex, SFeedScrip, SFeedScripLite

from engine.core.models import Index, OptionType
from engine.data.broker.base import assert_data_only
from engine.data.broker.kotak import (
    KotakDataClient, KotakDataProvider, KotakError, atm_from_parity, parse_candles, parse_option_chain, parse_quote,
)
from engine.data.models import InstrumentKind
from tests.conftest import DAY, at

EXP1, EXP2 = DAY + timedelta(days=7), DAY + timedelta(days=14)


def chain_resp(seg="nse_fo", base=24000, step=50, n=3, lot=65, expiry=EXP1):
    def side(ot, sign):
        out = []
        for k in range(-n, n + 1):
            strike = base + k * step
            ltp = max(5.0, 120 - sign * k * 30)
            out.append({
                "instrument": {"neoSymbol": f"{seg}|{70000 + k * 2 + (0 if ot == 'CE' else 1)}", "symbol": f"NIFTY{strike}{ot}", "optionType": ot, "strikePrice": strike, "moneyness": "ATM"},
                "quote": {"ltp": ltp, "open": ltp, "high": ltp, "low": ltp, "prevClose": ltp, "close": None, "volume": 120000},
                "openInterest": {"current": 500000 + k, "previous": 490000, "change": 10000 + k, "changePct": 2.0},
            })
        return out
    return {"data": {"common_data": {"mktLot": lot, "multiplier": 1, "unlSymbol": "NIFTY", "exSeg": seg, "expiryDt": expiry.isoformat()}, "call": side("CE", 1), "put": side("PE", -1)}}


FUT = {"data": {"common_data": {"mktLot": 65}, "call": [], "put": [], "fut": [
    {"inst": {"neoSymbol": "nse_fo|55555", "symbol": "NIFTY26DECFUT", "expiryDt": "2026-12-29"}, "quote": {"ltp": "24100"}, "oi": {"cur": "1"}},
    {"inst": {"neoSymbol": "nse_fo|44444", "symbol": "NIFTY26NOVFUT", "expiryDt": "2026-11-24"}, "quote": {"ltp": "24050"}, "oi": {"cur": "1"}},
]}}


class FakeNeo:
    """Shaped after the documented SDK responses. Deliberately has order methods so we can
    prove they are unreachable through our facade."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def totp_login(self, mobile_number, ucc, totp):
        self.calls.append(("totp_login", mobile_number, ucc, len(totp)))
        return {"data": {"token": "view"}}

    def totp_validate(self, mpin):
        self.calls.append(("totp_validate",))
        return {"data": {"token": "trade", "sid": "s"}}

    def expiries(self, exchange, underlying, instrument_type=None):
        self.calls.append(("expiries", exchange, underlying))
        return {"exchange": exchange, "underlying": underlying, "expiries": [(DAY - timedelta(days=1)).isoformat(), EXP1.isoformat(), EXP2.isoformat()]}

    def option_chain(self, exchange, underlying, expiry=None, instrument_type=None, count=None):
        self.calls.append(("option_chain", exchange, underlying, expiry, instrument_type, count))
        if instrument_type == "fut":
            return FUT
        base, step, lot = (80000, 100, 20) if underlying == "SENSEX" else (24000, 50, 65)
        return chain_resp(exchange, base, step, lot=lot, expiry=date.fromisoformat(expiry))

    def quotes(self, instrument_tokens, quote_type=None):
        self.calls.append(("quotes", instrument_tokens[0]["instrument_token"]))
        return [{"exchange_token": instrument_tokens[0]["instrument_token"], "ltp": "101.5", "last_traded_quantity": "65", "last_volume": "1300",
                 "open_int": "250000", "ohlc": {"open": "99", "high": "103", "low": "98", "close": "100"},
                 "depth": {"buy": [{"price": "101.40", "quantity": "650", "orders": "3"}], "sell": [{"price": "101.60", "quantity": "325", "orders": "2"}]}}]

    def historical_data(self, neosymbol, interval, from_date, to_date):
        self.calls.append(("historical_data", neosymbol, interval, from_date, to_date))
        d = date.fromisoformat(from_date)
        return {"status": "ok", "interval": interval, "data": {"candles": [[f"{d.isoformat()}T09:15:00+0530", 1, 2, 0.5, 1.5, 100, None], ["bad"]]}}

    def scrip_master(self, exchange_segment=None):
        return {"filesPaths": []}

    def create_websocket(self, **kw):
        return FakeWs()

    def place_order(self, *a, **k):   # pragma: no cover - must never be called
        raise AssertionError("order API called")

    def cancel_order(self, *a, **k):  # pragma: no cover
        raise AssertionError("order API called")


class FakeWs:
    def __init__(self, messages=None) -> None:
        self.messages = list(messages or [])
        self.subs: list[tuple[str, list[str]]] = []
        self.connected = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def subscribe_index(self, toks):
        self.subs.append(("index", [t.inputtoken for t in toks]))

    async def subscribe_depth(self, toks):
        self.subs.append(("depth", [t.inputtoken for t in toks]))

    async def subscribe_scrips(self, toks):
        self.subs.append(("scrips", [t.inputtoken for t in toks]))

    async def unsubscribe_depth(self, toks):
        self.subs.append(("-depth", [t.inputtoken for t in toks]))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        m = self.messages.pop(0)
        if isinstance(m, Exception):
            raise m
        return m


CREDS = {"mobile": "+919999999999", "ucc": "ABC123", "totp_secret": "JBSWY3DPEHPK3PXP", "mpin": "123456"}


def provider(cfg, neo=None, now=lambda: at(10, 0)):
    return KotakDataProvider(cfg, KotakDataClient("ck", neo=neo or FakeNeo()), credentials=CREDS, now=now)


def scrip(token, seg="nse_fo", ltp=101.0, bid=100.9, ask=101.1, close=95.0):
    base = dict(exchange_segment=seg, instrument_token=token, level=8, last_traded_price=ltp, open_price=ltp, high_price=ltp, low_price=ltp, close_price=close,
                average_trade_price=ltp, last_trade_time=0, last_update_time=0, last_trade_qty=65, total_buy_quantity=1, total_sell_quantity=1,
                volume_traded_today=1300, open_interest=5000, net_change=0, net_change_percent=0, upper_circuit_limit=0, lower_circuit_limit=0,
                yearly_high=0, yearly_low=0, total_traded_value=0, market_lot=65, precision=2, multiplier=1)
    return SFeedScrip(**base, buy=[SDepth(quantity=650, price=bid, orders=2)], sell=[SDepth(quantity=325, price=ask, orders=1)])


def index_msg(name="Nifty 50", seg="nse_cm", ltp=24010.0, token="26000"):
    return SFeedIndex(exchange_segment=seg, instrument_token=token, name=name, last_traded_price=ltp, open_price=ltp, high_price=ltp, low_price=ltp, close_price=23900.0,
                      change=0, net_change_percent=0, yearly_high=0, yearly_low=0, last_trade_time=0, precision=2, multiplier=1)


# ------------------------------------------------------------------ safety
def test_facade_has_no_order_methods_and_hides_sdk():
    c = KotakDataClient("ck", neo=FakeNeo())
    assert_data_only(c)
    assert not any(n in dir(c) for n in ("neo", "_neo", "place_order", "cancel_order", "modify_order"))


def test_no_order_api_calls_anywhere_in_engine():
    banned = {"place_order", "modify_order", "cancel_order", "subscribe_to_orderfeed", "create_order_feed", "exit_order"}
    hits = []
    for path in Path("engine").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in banned:
                hits.append(f"{path}:{node.lineno} {node.attr}")
    assert hits == []


# ------------------------------------------------------------------ auth / REST
def test_login_generates_totp_and_validates(cfg):
    neo = FakeNeo()
    p = provider(cfg, neo)
    asyncio.run(p.login())
    assert neo.calls[0][0] == "totp_login" and neo.calls[0][3] == 6 and neo.calls[1] == ("totp_validate",)
    assert p.client.logged_in


def test_login_errors_raise(cfg):
    neo = FakeNeo()
    neo.totp_login = lambda **k: {"Error": "Invalid TOTP"}   # type: ignore[method-assign]
    with pytest.raises(KotakError, match="Invalid TOTP"):
        asyncio.run(provider(cfg, neo).login())
    with pytest.raises(KotakError, match="missing"):
        asyncio.run(KotakDataProvider(cfg, KotakDataClient("ck", neo=FakeNeo()), credentials={}).login())


def test_parse_option_chain_and_parity_atm():
    insts, rows, lot = parse_option_chain(chain_resp(), Index.NIFTY, EXP1)
    assert lot == 65 and len(insts) == 14 and all(i.lot_size == 65 and i.expiry == EXP1 for i in insts)
    assert {i.option_type for i in insts} == {OptionType.CE, OptionType.PE} and insts[0].token.startswith("nse_fo|")
    assert all(r.oi > 0 for r in rows) and atm_from_parity(rows) == 24000


async def test_instruments_universe(cfg):
    neo = FakeNeo()
    p = provider(cfg, neo)
    insts = await p.instruments(DAY)
    kinds = {k: [i for i in insts if i.kind is k] for k in InstrumentKind}
    assert {i.token for i in kinds[InstrumentKind.INDEX]} >= {"nse_cm|Nifty 50", "bse_cm|SENSEX", "nse_cm|INDIA VIX", "nse_cm|Nifty Bank"}
    fut = [i for i in kinds[InstrumentKind.FUTURE] if i.underlying is Index.NIFTY]
    assert fut and fut[0].token == "nse_fo|44444"   # nearest expiry
    assert len(kinds[InstrumentKind.EQUITY]) == len(cfg.feeds.kotak.heavyweights)
    sensex_opts = [i for i in kinds[InstrumentKind.OPTION] if i.underlying is Index.SENSEX]
    assert sensex_opts and {i.lot_size for i in sensex_opts} == {20} and sensex_opts[0].token.startswith("bse_fo|")
    chain_calls = [c for c in neo.calls if c[0] == "option_chain" and c[4] == "option"]
    assert {c[3] for c in chain_calls} == {EXP1.isoformat(), EXP2.isoformat()} and all(c[5] == cfg.feeds.kotak.universe_count for c in chain_calls)
    assert p.initial_atm(Index.NIFTY, EXP1) == 24000
    assert (await p.expiries(Index.NIFTY, DAY)) == [EXP1, EXP2]   # past expiry dropped


async def test_quote_and_chain_merge_feed_bid_ask(cfg):
    p = provider(cfg)
    insts = await p.instruments(DAY)
    opt = next(i for i in insts if i.kind is InstrumentKind.OPTION)
    q = await p.quote(opt)
    assert (q.ltp, q.bid, q.ask, q.oi, q.close) == (101.5, 101.4, 101.6, 250000, 100.0) and q.bids[0].qty == 650
    chain = await p.option_chain(Index.NIFTY, EXP1)
    row = next(r for r in chain.rows if r.token == opt.token)
    assert (row.bid, row.ask) == (101.4, 101.6)
    assert chain.spot == 24000   # no index tick yet -> parity ATM


def test_parse_quote_and_candles():
    t = parse_quote({"ltp": "10", "depth": {"buy": [], "sell": []}}, "x|1", at(10, 0))
    assert t.bid == 0 and t.ltp == 10
    cs = parse_candles({"data": {"candles": [["2026-11-03T09:15:00+0530", "1", "2", "0.5", "1.5", "10", "7"]]}})
    assert cs[0].ts == at(9, 15) and cs[0].oi == 7


async def test_candles_chunk_by_max_range(cfg):
    neo = FakeNeo()
    p = provider(cfg, neo)
    inst = (await p.instruments(DAY))[0]
    start = datetime(2026, 9, 1, 9, 15, tzinfo=at(9, 15).tzinfo)
    out = await p.candles(inst, start, at(15, 30), 1)
    calls = [c for c in neo.calls if c[0] == "historical_data"]
    assert len(calls) >= 3 and all(c[2] == "1min" for c in calls)
    for c in calls:
        assert (date.fromisoformat(c[4]) - date.fromisoformat(c[3])).days <= 29
    assert all(start <= c.ts < at(15, 30) for c in out)
    with pytest.raises(ValueError):
        await p.candles(inst, start, at(15, 30), 2)


# ------------------------------------------------------------------ feed
async def test_feed_subscriptions_and_tick_conversion(cfg):
    neo = FakeNeo()
    ws = FakeWs([index_msg(), scrip("70000"), SFeedScripLite(exchange_segment="nse_fo", instrument_token="70000", last_traded_price=102.0, last_trade_time=0, last_trade_qty=65, close_price=95, net_change=0, net_change_percent=0, market_lot=65, precision=2, multiplier=1), scrip("99999")])
    neo.create_websocket = lambda **kw: ws   # type: ignore[method-assign]
    p = provider(cfg, neo)
    insts = await p.instruments(DAY)
    await p.connect()
    nifty_idx = next(i for i in insts if i.token == "nse_cm|Nifty 50")
    opt = next(i for i in insts if i.token == "nse_fo|70000")
    eq = next(i for i in insts if i.kind is InstrumentKind.EQUITY)
    await p.subscribe([nifty_idx, opt, eq])
    assert ("index", ["nse_cm|Nifty 50"]) in ws.subs and ("depth", ["nse_fo|70000"]) in ws.subs and ("scrips", [eq.token]) in ws.subs
    got = []
    p._closed = False

    async def take():
        async for t in p.ticks():
            got.append(t)
            if len(got) == 3:
                await p.close()

    await asyncio.wait_for(take(), 5)
    assert got[0].token == "nse_cm|Nifty 50" and got[0].ltp == 24010 and got[0].close == 23900   # index matched by name
    assert (got[1].bid, got[1].ask, got[1].oi, got[1].bids[0].qty) == (100.9, 101.1, 5000, 650)
    assert got[2].ltp == 102 and got[2].bid == 100.9           # lite tick keeps last depth
    assert all(t.ts == at(10, 0) for t in got)                  # receive time


async def test_unsubscribe_while_connected(cfg):
    neo = FakeNeo()
    ws = FakeWs()
    neo.create_websocket = lambda **kw: ws   # type: ignore[method-assign]
    p = provider(cfg, neo)
    insts = await p.instruments(DAY)
    opt = next(i for i in insts if i.token == "nse_fo|70000")
    await p.connect()
    await p.subscribe([opt])
    await p.unsubscribe([opt])
    assert ("-depth", ["nse_fo|70000"]) in ws.subs and "nse_fo|70000" not in p._subs


async def test_feed_reconnects_with_backoff_and_resubscribes(cfg, monkeypatch):
    neo = FakeNeo()
    sockets = [FakeWs([ConnectionError("drop")]), FakeWs([scrip("70000")])]
    neo.create_websocket = lambda **kw: sockets.pop(0)   # type: ignore[method-assign]
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("engine.data.broker.kotak.asyncio.sleep", fake_sleep)
    p = provider(cfg, neo)
    insts = await p.instruments(DAY)
    opt = next(i for i in insts if i.token == "nse_fo|70000")
    await p.subscribe([opt])
    await p.connect()
    first_ws = p._ws
    got = []
    async for t in p.ticks():
        got.append(t)
        await p.close()
    assert sleeps == [1.0] and got and p._ws is None
    assert first_ws is not None and ("depth", ["nse_fo|70000"]) in first_ws.subs


# ------------------------------------------------------------------ verify tool
async def test_verify_kotak_report(cfg):
    from engine.tools.verify_kotak import run

    neo = FakeNeo()
    neo.create_websocket = lambda **kw: FakeWs([index_msg()])   # type: ignore[method-assign]
    rep = await run(cfg, provider(cfg, neo), feed_seconds=1)
    c = rep["checks"]
    assert rep["required_ok"] and c["login"]["ok"] and c["NIFTY_chain"]["has_oi"] and c["NIFTY_chain"]["lot_size"] == 65
    assert c["NIFTY_chain"]["has_iv_or_greeks"] is False and c["candles"]["oi_populated"] is False
    assert c["feed"]["ok"]
