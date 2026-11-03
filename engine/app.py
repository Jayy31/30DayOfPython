"""Application container: wires config, data, decision, risk, broker, news, LLM, alerts, store."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from engine.alerts.notifier import MemoryNotifier, Notifier, TelegramNotifier
from engine.analytics.strike import select_expiry
from engine.broker_paper.broker import PaperBroker
from engine.config.schemas import AppConfig
from engine.config.settings import AppMode, Settings
from engine.core.models import Index
from engine.core.timeutils import IST
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

    def __post_init__(self) -> None:
        self.calendar = TradingCalendar(self.cfg.holidays)
        self.notifier = self.notifier or make_notifier(self.settings)
        self.llm = self.llm or create_llm_service(self.settings, self.cfg.llm)
        self.news = NewsPipeline(self.cfg.news, self.llm)
        self.risk = RiskEngine(self.cfg.rules, self.calendar)
        self.broker = PaperBroker(self.cfg, self.capital)
        self.decision = DecisionEngine(self.cfg, self.risk)
        self.tick_store = self.tick_store or ParquetTickStore(Path("data"))
        self.last_result: DayResult | None = None
        self.live: LiveEngine | None = None
        self.premarket_data: dict[str, object] = {}

    # ----------------------------------------------------------- providers
    def provider_for(self, day: date, seed: int | None = None) -> MockMarketDataProvider:
        if self.settings.app_mode is AppMode.LIVE_DATA:
            raise NotImplementedError("Kotak provider arrives in Phase 10")
        market = default_market(day, seed=seed if seed is not None else self.replay_seed, step_seconds=self.replay_step_seconds)
        recorded = self.tick_store.read_day(day) if self.tick_store else []
        provider = MockMarketDataProvider(recorded or market.ticks(), market.instruments)
        assert_data_only(provider)
        return provider

    async def previous_day_candles(self, day: date) -> dict[Index, list[Candle]]:
        prev = self.calendar.previous_trading_day(day)
        p = self.provider_for(prev, seed=self.replay_seed + 1)
        await p.connect()
        insts = await p.instruments(prev)
        out = {}
        start, end = datetime.combine(prev, time(9, 15), IST), datetime.combine(prev, time(15, 30), IST)
        for idx in Index:
            spot = next((i for i in insts if i.underlying is idx and i.kind is InstrumentKind.INDEX), None)
            if spot is not None:
                out[idx] = await p.candles(spot, start, end, 1)
        return out

    # --------------------------------------------------------------- live
    async def run_live_day(self, day: date) -> DayResult:
        provider: MarketDataProvider = self.provider_for(day)
        instruments = await provider.instruments(day)
        prev = await self.previous_day_candles(day)
        states = {}
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
        engine = LiveEngine(
            self.cfg, provider, states, self.decision, self.risk, self.broker, self.notifier, self.store,  # type: ignore[arg-type]
            external_bias=lambda idx, now: {"news": self.news.bias(now)},
            events=self.news.active_events,
        )
        engine.recorded = []
        self.live = engine
        self.last_result = await engine.run_day(day, instruments)
        self._day_ticks = engine.recorded
        return self.last_result

    def save_ticks(self, day: date) -> int:
        ticks = getattr(self, "_day_ticks", None)
        if not ticks or self.tick_store is None:
            return 0
        n = self.tick_store.write_day(day, ticks)
        self._day_ticks = []
        return n

    def week_bounds(self, d: date) -> tuple[date, date]:
        start = d - timedelta(days=d.weekday())
        return start, start + timedelta(days=6)
