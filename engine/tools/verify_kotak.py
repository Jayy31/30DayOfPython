"""One-shot check of the Kotak account against what the engine assumes.

  python -m engine.main verify-kotak

Answers the spec's open questions (chain OI/IV, candle OI, latency) and verifies the
VERIFY items in config (SENSEX segment/underlying, India VIX token, heavyweight tokens).
Read-only: login + market data. Prints a JSON report; exit code 1 if a required check fails.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from datetime import datetime, time, timedelta
from typing import Any

from engine.config.schemas import AppConfig
from engine.core.models import Index, OptionType
from engine.core.timeutils import IST, now_ist
from engine.core.trading_calendar import TradingCalendar
from engine.data.broker.kotak import KotakDataProvider, atm_from_parity
from engine.data.models import Instrument, InstrumentKind


async def run(cfg: AppConfig, provider: KotakDataProvider, feed_seconds: float = 20.0) -> dict[str, Any]:
    rep: dict[str, Any] = {"checked_at": now_ist().isoformat(), "required_ok": True, "checks": {}}
    chk = rep["checks"]

    def fail(name: str, err: Any, required: bool = True) -> None:
        chk[name] = {"ok": False, "error": str(err)[:400]}
        if required:
            rep["required_ok"] = False

    try:
        await provider.login()
        chk["login"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        fail("login", e)
        return rep

    day = now_ist().date()
    cal = TradingCalendar(cfg.holidays)
    prev = cal.previous_trading_day(day)
    for name in cfg.rules.indices:
        idx = Index(name)
        try:
            exps = await provider.expiries(idx, day)
            raw = provider.client.option_chain(provider._kcfg(idx).fo_segment, provider._kcfg(idx).underlying, exps[0].isoformat(), "option", 20)
            chain = await provider.option_chain(idx, exps[0])
            first = ((raw.get("data") or {}).get("call") or [{}])[0] if isinstance(raw, dict) else {}
            chk[f"{name}_chain"] = {
                "ok": bool(chain.rows), "expiries": [e.isoformat() for e in exps[:4]], "rows": len(chain.rows),
                "atm_by_parity": atm_from_parity(list(chain.rows)), "lot_size": (raw.get("data") or {}).get("common_data", {}).get("mktLot"),
                "has_oi": any(r.oi for r in chain.rows), "fields_in_first_row": sorted(first.keys()),
                "has_iv_or_greeks": any(k.lower() in ("iv", "greeks", "impliedvolatility") for k in json.dumps(first).lower().split('"')),
            }
        except Exception as e:  # noqa: BLE001
            fail(f"{name}_chain", e)

    insts = []
    try:
        insts = await provider.instruments(day)
        chk["instruments"] = {"ok": True, "count": len(insts)}
    except Exception as e:  # noqa: BLE001
        fail("instruments", e)

    idx_insts = [i for i in insts if i.kind is InstrumentKind.INDEX and i.underlying is not None]
    if idx_insts:
        try:
            start = datetime.combine(prev, time(9, 15), IST)
            candles = await provider.candles(idx_insts[0], start, start + timedelta(hours=6, minutes=15), 1)
            chk["candles"] = {"ok": bool(candles), "count": len(candles), "oi_populated": any(c.oi for c in candles), "first": candles[0].model_dump(mode="json") if candles else None}
        except Exception as e:  # noqa: BLE001
            fail("candles", e)

    extras = [i for i in insts if i.underlying is None]
    if extras:
        lat, bad = [], []
        for inst in extras:
            t0 = _time.perf_counter()
            try:
                q = await provider.quote(inst)
                lat.append(_time.perf_counter() - t0)
                if not q.ltp:
                    bad.append(inst.symbol)
            except Exception:  # noqa: BLE001
                bad.append(inst.symbol)
        chk["extra_tokens"] = {"ok": not bad, "unresolved": bad, "avg_quote_latency_ms": round(sum(lat) / len(lat) * 1000) if lat else None}
        if bad:
            rep["required_ok"] = rep["required_ok"]  # VIX/heavyweights are optional inputs

    try:
        await provider.connect()
        opts = [i for i in insts if i.kind is InstrumentKind.OPTION][:4]
        await provider.subscribe(idx_insts + opts + extras[:2])
        seen: dict[str, int] = {}
        sample: list[dict[str, Any]] = []

        async def collect() -> None:
            async for t in provider.ticks():
                seen[t.token] = seen.get(t.token, 0) + 1
                if len(sample) < 5:
                    sample.append(t.model_dump(mode="json", exclude={"bids", "asks"}))

        try:
            await asyncio.wait_for(collect(), timeout=feed_seconds)
        except asyncio.TimeoutError:
            pass
        await provider.close()
        chk["feed"] = {"ok": bool(seen), "ticks_by_token": seen, "sample": sample, "note": "outside market hours only indices/last prices may arrive"}
    except Exception as e:  # noqa: BLE001
        fail("feed", e)
    return rep


def main(cfg: AppConfig, provider: KotakDataProvider) -> int:
    rep = asyncio.run(run(cfg, provider))
    print(json.dumps(rep, indent=2, default=str))
    return 0 if rep["required_ok"] else 1
