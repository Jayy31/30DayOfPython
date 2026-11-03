"""Application container: wires config, data, decision, risk, broker, news, LLM, alerts, store.

APP_MODE=replay    -> synthetic / recorded ticks, no credentials, no network
APP_MODE=live_data -> Kotak feed + REST, NSE, global prices, news, object storage
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from engine.alerts.notifier import MemoryNotifier, Notifier, TelegramNotifier
from engine.analytics.strike import select_expiry
from engine.broker_paper.broker import PaperBroker
from engine.config.schemas import AppConfig
from engine.config.settings import AppMode, Settings
from engine.core.models import Index
from engine.core.timeutils import IST, now_ist
from engine.core.trading_calendar import TradingCalendar
from engine.data.broker.base import MarketDataProvider, assert_data_only
from engine.data.broker.mock import MockMarketDataProvider
from engine.data.broker.synthetic import default_market
from engine.data.models import Candle, InstrumentKind
from engine.data.storage import ParquetTickStore
from engine.decision.engine import DecisionEngine
from engine.llm.factory import create_llm_service
from engine.llm.service import LLMService
from engine.news.pipeline import NewsPipeline
from engine.risk.engine import RiskEngine
from engine.runtime.live import DayResult, LiveEngine
from engine.state.market_state import IndexState
from engine.store.base import MemoryStore, Store

log = logging.getLogger(__name__)


def make_notifier(settings: Settings) -> Notifier:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        return TelegramNotifier(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id)
    return MemoryNotifier()


def _secret(x: Any) -> str | None:
    return x.get_secret_value() if x is not None else None


@dataclass
class App:
    settings: Settings
    cfg: AppConfig
    store: Store = field(default_factory=MemoryStore)
    notifier: Notifier | None = None
    llm: LLMService | None = None
    tick_store: ParquetTickStore | None = None
    capital: float | None = None
    replay_step_seconds: int = 5
    replay_seed: int = 7
    data_dir: Path = Path("data")

    def __post_init__(self) -> None:
        self.calendar = TradingCalendar(self.cfg.holidays)
        self.notifier = self.notifier or make_notifier(self.settings)
        self.llm = self.llm or create_llm_service(self.settings, self.cfg.llm)
        self.risk = RiskEngine(self.cfg.rules, self.calendar)
        self.broker = PaperBroker(self.cfg, self.capital)
        self.decision = DecisionEngine(self.cfg, self.risk)
        self.tick_store = self.tick_store or ParquetTickStore(self.data_dir)
        self.last_result: DayResult | None = None
        self.live: LiveEngine | None = None
        self.overnight: dict[str, Any] = {}
        self.positioning: dict[str, Any] = {}
        self.flows: list[Any] = []
        self._kotak: Any = None
        self._nse: Any = None
        self._global: Any = None
        self._storage: Any = None
        self._news_fetchers: list[Any] | None = None
        self.news = NewsPipeline(self.cfg.news, self.llm, moves=self._moves_since)

    @property
    def is_live(self) -> bool:
        return self.settings.app_mode is AppMode.LIVE_DATA

    # ------------------------------------------------------- external services
    def kotak(self):  # type: ignore[no-untyped-def]
        if self._kotak is None:
            from engine.data.broker.kotak import KotakDataClient, KotakDataProvider

            s = self.settings
            client = KotakDataClient(_secret(s.kotak_consumer_key) or "", s.kotak_env)
            creds = {"mobile": s.kotak_mobile or "", "ucc": s.kotak_ucc or "", "totp_secret": _secret(s.kotak_totp_secret) or "", "mpin": _secret(s.kotak_mpin) or ""}
            self._kotak = KotakDataProvider(self.cfg, client, credentials=creds)
            assert_data_only(self._kotak)
        return self._kotak

    def nse(self):  # type: ignore[no-untyped-def]
        if self._nse is None:
            from engine.data.nse import NseClient

            self._nse = NseClient(self.cfg.feeds.nse, self.data_dir / "nse")
        return self._nse

    def global_markets(self):  # type: ignore[no-untyped-def]
        if self._global is None:
            from engine.data.global_markets import GlobalMarkets

            self._global = GlobalMarkets(self.cfg.feeds.global_markets)
        return self._global

    def storage(self):  # type: ignore[no-untyped-def]
        s = self.settings
        if self._storage is None and s.storage_endpoint and s.storage_bucket and s.storage_access_key and s.storage_secret_key:
            from engine.data.object_storage import ObjectStorage

            self._storage = ObjectStorage(s.storage_endpoint, s.storage_bucket, _secret(s.storage_access_key) or "", _secret(s.storage_secret_key) or "", self.cfg.feeds.storage.prefix)
        return self._storage

    def _moves_since(self, ts: datetime) -> dict[str, float | None]:
        return self._global.moves_since(ts) if self._global is not None else {}

    def external_bias(self, idx: Index, now: datetime) -> dict[str, float | None]:
        return {"news": self.news.bias(now), "overnight": self.overnight.get("score"), "positioning": self.positioning.get("bias")}

    async def poll_news(self, now: datetime | None = None) -> int:
        import httpx

        from engine.news.fetchers import build_fetchers, fetch_all

        if self._news_fetchers is None:
            self._news_fetchers = build_fetchers(self.cfg.news.sources, _secret(self.settings.finnhub_api_key))
        async with httpx.AsyncClient(follow_redirects=True) as client:
            items, errors = await fetch_all(self._news_fetchers, client)
        upd = await self.news.process(items, now or now_ist())
        for sn in upd.breaking:
            await self.notifier.send(f"BREAKING ({', '.join(sn.cluster.topics)}): {sn.cluster.lead.title} [{sn.score.direction}, impact {sn.effective_impact:.1f}]")  # type: ignore[union-attr]
        return len(upd.scored)

    # ----------------------------------------------------------- providers
    def provider_for(self, day: date, seed: int | None = None) -> MarketDataProvider:
        if self.is_live:
            return self.kotak()
        market = default_market(day, seed=seed if seed is not None else self.replay_seed, step_seconds=self.replay_step_seconds)
        recorded = self.tick_store.read_day(day) if self.tick_store else []
        provider = MockMarketDataProvider(recorded or market.ticks(), market.instruments)
        assert_data_only(provider)
        return provider

    async def previous_day_candles(self, day: date) -> dict[Index, list[Candle]]:
        prev = self.calendar.previous_trading_day(day)
        start, end = datetime.combine(prev, time(9, 15), IST), datetime.combine(prev, time(15, 30), IST)
        if self.is_live:
            p = self.kotak()
            out = {}
            for inst in await p.instruments(day):
                if inst.kind is InstrumentKind.INDEX and inst.underlying is not None:
                    try:
                        out[inst.underlying] = await p.candles(inst, start, end, 1)
                    except Exception:  # noqa: BLE001 - missing history must not stop the day
                        log.warning("previous-day candles unavailable", extra={"index": inst.underlying.value}, exc_info=True)
            return out
        p = self.provider_for(prev, seed=self.replay_seed + 1)
        await p.connect()
        insts = await p.instruments(prev)
        out = {}
        for idx in Index:
            spot = next((i for i in insts if i.underlying is idx and i.kind is InstrumentKind.INDEX), None)
            if spot is not None:
                out[idx] = await p.candles(spot, start, end, 1)
        return out

    # --------------------------------------------------------------- live
    async def run_live_day(self, day: date) -> DayResult:
        provider = self.provider_for(day)
        if self.is_live and not provider.client.logged_in:  # type: ignore[attr-defined]
            await provider.login()  # type: ignore[attr-defined]
        instruments = await provider.instruments(day)
        prev = await self.previous_day_candles(day)
        states: dict[Index, IndexState] = {}
        centers: dict[Index, float] = {}
        open_dt = datetime.combine(day, time(9, 15), IST)
        switch = datetime.combine(day, self.cfg.decision.strike.expiry_day_switch, IST)
        for idx in Index:
            if idx.value not in self.cfg.rules.indices:
                continue
            idx_insts = [i for i in instruments if i.underlying is idx]
            expiries = await provider.expiries(idx, day)
            if not expiries:
                continue
            expiry = select_expiry(expiries, open_dt, switch)
            states[idx] = IndexState(self.cfg, idx, idx_insts, expiry, prev_day=prev.get(idx, []))
            if self.is_live:
                atm = provider.initial_atm(idx, expiry)  # type: ignore[attr-defined]
                if atm:
                    centers[idx] = atm
        kw: dict[str, Any] = {}
        if self.is_live:
            kf = self.cfg.feeds.kotak
            kw = {
                "option_window": kf.strikes_each_side,
                "initial_centers": centers,
                "heavyweights": {f"nse_cm|{h.token}": (h.symbol, h.weight) for h in kf.heavyweights},
                "vix_token": f"{kf.vix.segment}|{kf.vix.token}",
                "extra_instruments": [i for i in instruments if i.underlying is None],
                "feed_gap_seconds": 10.0,
            }
        engine = LiveEngine(
            self.cfg, provider, states, self.decision, self.risk, self.broker, self.notifier, self.store,  # type: ignore[arg-type]
            external_bias=self.external_bias, events=self.news.active_events, **kw,
        )
        engine.recorded = []
        self.live = engine
        tasks = [asyncio.create_task(self._background(engine))] if self.is_live else []
        try:
            self.last_result = await engine.run_day(day, instruments)
        finally:
            for t in tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        self._day_ticks = engine.recorded
        return self.last_result

    async def _background(self, engine: LiveEngine) -> None:
        """Live only: feed watchdog (2s), global prices (3 min), news (config cadence)."""
        last_news = last_global = 0.0
        loop = asyncio.get_running_loop()
        while True:
            now = now_ist()
            try:
                await engine.watchdog(now)
                t = loop.time()
                if t - last_global >= 180:
                    last_global = t
                    await self.global_markets().refresh()
                if t - last_news >= self.cfg.news.poll_market_seconds:
                    last_news = t
                    await self.poll_news(now)
                if now.time() >= time(15, 30):
                    await self.provider_for(now.date()).close()
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - background helpers must never kill the trading loop
                log.warning("background task error", exc_info=True)
            await asyncio.sleep(2)

    def save_ticks(self, day: date) -> int:
        ticks = getattr(self, "_day_ticks", None)
        if not ticks or self.tick_store is None:
            return 0
        n = self.tick_store.write_day(day, ticks)
        self._day_ticks = []
        st = self.storage()
        if st is not None:
            st.upload(self.tick_store.path_for(day), "ticks", f"date={day.isoformat()}", "ticks.parquet")
        return n

    def week_bounds(self, d: date) -> tuple[date, date]:
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
