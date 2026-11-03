"""In-memory market state per index: turns ticks into a MarketContext + OptionChain.

Cheap work happens per tick (candles, VWAP, delta, OR, failed breaks); heavier
work (chain, OI map, GEX, regime, zones) happens on refresh(), once a minute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from engine.analytics.bias import BiasRead, read_bias
from engine.analytics.breadth import Breadth
from engine.analytics.gex import GexResult, compute_gex
from engine.analytics.levels import (
    FailedBreak, FailedBreakDetector, Level, OpeningRange, Vwap, confluence_zones, previous_day_levels,
    profile_levels, volume_profile,
)
from engine.analytics.oi import OiMap, WallShift, oi_map, wall_shift
from engine.analytics.opening import InitialBalance, OpeningRead, classify_opening, initial_balance
from engine.analytics.orderflow import CumulativeDelta, FuturesOiClass, classify_futures_oi, depth_imbalance
from engine.analytics.regime import RegimeRead, read_regime
from engine.analytics.setups import MarketContext
from engine.analytics.straddle import StraddleTracker
from engine.analytics.triggers import TriggerInputs, TriggerRead, read_trigger
from engine.config.schemas import AppConfig
from engine.core.models import Index, OptionType
from engine.core.timeutils import floor_to_minutes, to_ist
from engine.data.models import Candle, Instrument, InstrumentKind, OptionChain, OptionChainRow, Tick


@dataclass
class _Bar:
    ts: datetime
    o: float
    h: float
    lo: float
    c: float
    v: int = 0

    def candle(self) -> Candle:
        return Candle(ts=self.ts, open=self.o, high=self.h, low=self.lo, close=self.c, volume=self.v)


@dataclass
class IndexState:
    cfg: AppConfig
    index: Index
    instruments: Sequence[Instrument]
    expiry: object  # date of the option series being traded
    prev_day: Sequence[Candle] = ()
    lot_size: int = 1
    failed_break_window: timedelta = timedelta(minutes=15)
    chain_refresh: timedelta = timedelta(minutes=3)   # option chain / OI / GEX cadence

    spot: float | None = None
    prev_close: float | None = None
    bars: dict[datetime, _Bar] = field(default_factory=dict)
    vwap: Vwap = field(default_factory=Vwap)
    opening_range: OpeningRange = field(default_factory=OpeningRange)
    delta: CumulativeDelta = field(default_factory=CumulativeDelta)
    last_depth: float = 0.0
    fut_open: tuple[float, int] | None = None
    fut_last: tuple[float, int] | None = None
    options: dict[str, Tick] = field(default_factory=dict)
    option_open_oi: dict[str, int] = field(default_factory=dict)
    straddle: StraddleTracker | None = None
    opening: OpeningRead | None = None
    ib: InitialBalance | None = None
    oi: OiMap | None = None
    prev_oi: OiMap | None = None
    shift: WallShift | None = None
    gex: GexResult | None = None
    regime: RegimeRead | None = None
    reference_iv: float | None = None
    failed_breaks: list[FailedBreak] = field(default_factory=list)
    _detectors: list[FailedBreakDetector] = field(default_factory=list)
    _or_detectors_added: bool = False
    _last_chain_refresh: datetime | None = None

    def __post_init__(self) -> None:
        self._spot_token = next(i.token for i in self.instruments if i.kind is InstrumentKind.INDEX)
        self._fut_token = next((i.token for i in self.instruments if i.kind is InstrumentKind.FUTURE), None)
        self._opts = {i.token: i for i in self.instruments if i.kind is InstrumentKind.OPTION and i.expiry == self.expiry}
        if self._opts:
            self.lot_size = next(iter(self._opts.values())).lot_size
        self.straddle = StraddleTracker(self.expiry)  # type: ignore[arg-type]
        self._static_levels: list[Level] = []
        if self.prev_day:
            self._static_levels += previous_day_levels(self.prev_day)
            step = self.cfg.instruments[self.index.value].strike_step  # type: ignore[index]
            self._static_levels += profile_levels(volume_profile(self.prev_day, bin_size=step / 5))
            for lvl in self._static_levels[:2]:  # PDH, PDL
                self._detectors.append(FailedBreakDetector(lvl, within=self.failed_break_window))

    # ------------------------------------------------------------------ ticks
    def tokens(self) -> set[str]:
        return {self._spot_token, *({self._fut_token} if self._fut_token else set()), *self._opts}

    def on_tick(self, t: Tick) -> None:
        if t.token == self._spot_token:
            self._on_spot(t)
        elif t.token == self._fut_token:
            self.vwap.update(t.ltp, t.volume)
            self.delta.update(t)
            self.last_depth = depth_imbalance(t)
            if self.fut_open is None:
                self.fut_open = (t.ltp, t.oi)
            self.fut_last = (t.ltp, t.oi)
        elif t.token in self._opts:
            self.options[t.token] = t
            self.option_open_oi.setdefault(t.token, t.oi)

    def _on_spot(self, t: Tick) -> None:
        self.spot = t.ltp
        if t.close:
            self.prev_close = t.close
        m = floor_to_minutes(t.ts, 1)
        b = self.bars.get(m)
        if b is None:
            self.bars[m] = _Bar(m, t.ltp, t.ltp, t.ltp, t.ltp)
        else:
            b.h, b.lo, b.c = max(b.h, t.ltp), min(b.lo, t.ltp), t.ltp
        self.opening_range.update(t.ts, t.ltp)
        for d in self._detectors:
            fb = d.update(t.ts, t.ltp)
            if fb is not None:
                self.failed_breaks.append(fb)

    # --------------------------------------------------------------- derived
    def chain(self, now: datetime) -> OptionChain | None:
        if self.spot is None or not self.options:
            return None
        rows = []
        for tok, t in self.options.items():
            inst = self._opts[tok]
            assert inst.strike is not None and inst.option_type is not None
            rows.append(OptionChainRow(strike=inst.strike, option_type=inst.option_type, token=tok, ltp=t.ltp, bid=t.bid, ask=t.ask, volume=t.volume, oi=t.oi, oi_change=t.oi - self.option_open_oi.get(tok, t.oi)))
        return OptionChain(ts=now, index=self.index, expiry=self.expiry, spot=self.spot, rows=tuple(sorted(rows, key=lambda r: (r.strike, r.option_type))))  # type: ignore[arg-type]

    def closes(self, n: int = 30, before: datetime | None = None) -> list[float]:
        keys = sorted(k for k in self.bars if before is None or k < before)
        return [self.bars[k].c for k in keys[-n:]]

    def _candles_between(self, start: time, end: time) -> list[Candle]:
        return [b.candle() for k, b in sorted(self.bars.items()) if start <= k.time() < end]

    def refresh(self, now: datetime) -> None:
        now = to_ist(now)
        due = self._last_chain_refresh is None or now - self._last_chain_refresh >= self.chain_refresh
        chain = self.chain(now) if due else None
        if chain is not None and len(chain.rows) >= 4:
            self._last_chain_refresh = now
            atm = chain.atm_strike()
            ce, pe = chain.row(atm, OptionType.CE), chain.row(atm, OptionType.PE)
            if ce and pe and self.straddle is not None:
                self.straddle.update(now, (ce.bid + ce.ask) / 2, (pe.bid + pe.ask) / 2, chain.spot)
            new_oi = oi_map(chain)
            if self.oi is not None:
                self.prev_oi = self.oi
                self.shift = wall_shift(self.prev_oi, new_oi)
            self.oi = new_oi
            self.gex = compute_gex(chain, now, flip_steps=41)
            if self.reference_iv is None and ce is not None:
                from engine.analytics.greeks import implied_vol, time_to_expiry

                self.reference_iv = implied_vol((ce.bid + ce.ask) / 2, chain.spot, atm, time_to_expiry(now, chain.expiry), OptionType.CE)
        if self.opening is None and now.time() >= time(9, 30):
            first = self._candles_between(time(9, 15), time(9, 30))
            if len(first) >= 3:
                self.opening = classify_opening(first)
        st = self.straddle.state() if self.straddle else None
        if self.ib is None and now.time() >= time(10, 15) and st is not None:
            first_hour = self._candles_between(time(9, 15), time(10, 15))
            if first_hour:
                self.ib = initial_balance(first_hour, st.day_implied_move)
        if self.opening_range.complete and not self._or_detectors_added and self.opening_range.high is not None:
            for price, src in ((self.opening_range.high, "OR_HIGH"), (self.opening_range.low, "OR_LOW")):
                self._detectors.append(FailedBreakDetector(Level(price, src), within=self.failed_break_window))  # type: ignore[arg-type]
            self._or_detectors_added = True
        self.regime = read_regime(self.cfg.decision.regime, gex=self.gex, straddle=st, opening=self.opening, ib=self.ib)

    def futures_class(self) -> FuturesOiClass:
        if not (self.fut_open and self.fut_last) or not self.fut_open[1]:
            return FuturesOiClass.NEUTRAL
        p0, oi0 = self.fut_open
        p1, oi1 = self.fut_last
        return classify_futures_oi((p1 / p0 - 1) * 100, (oi1 / oi0 - 1) * 100)

    def levels(self) -> list[Level]:
        lv = list(self._static_levels)
        orr = self.opening_range
        if orr.complete and orr.high is not None and orr.low is not None:
            lv += [Level(orr.high, "OR_HIGH"), Level(orr.low, "OR_LOW")]
        if self.vwap.value is not None:
            lv.append(Level(self.vwap.value, "VWAP"))
        if self.oi is not None:
            lv += [Level(self.oi.call_wall, "CALL_WALL"), Level(self.oi.put_wall, "PUT_WALL")]
        if self.gex is not None and self.gex.flip is not None:
            lv.append(Level(self.gex.flip, "GAMMA_FLIP", 0.5))
        return lv

    def bias(self, external: Mapping[str, float | None], nifty_futures_proxy: float | None = None) -> BiasRead:
        inputs = dict(external)
        fc = self.futures_class()
        inputs.setdefault("futures_oi", float(fc.bias) if fc is not FuturesOiClass.NEUTRAL else None)
        inputs.setdefault("writers", float(self.shift.bias) if self.shift and self.shift.bias else None)
        return read_bias(self.cfg.decision.bias, self.index, inputs, nifty_futures_proxy)

    def trigger(self, now: datetime, breadth: Breadth | None = None) -> TriggerRead:
        recent = [fb for fb in self.failed_breaks if now - fb.rejected_at <= timedelta(minutes=5)]
        return read_trigger(
            TriggerInputs(
                window_delta=self.delta.window_normalized,
                depth_imbalance=self.last_depth,
                futures_oi=self.futures_class(),
                breadth=breadth,
                failed_break=recent[-1] if recent else None,
            )
        )

    def change_pct(self) -> float | None:
        if self.spot is None or not self.prev_close:
            return None
        return (self.spot / self.prev_close - 1) * 100

    def context(self, now: datetime, external_bias: Mapping[str, float | None], events: Sequence[str] = (), nifty_futures_proxy: float | None = None, breadth: Breadth | None = None) -> MarketContext | None:
        if self.spot is None or self.regime is None:
            return None
        st = self.straddle.state() if self.straddle else None
        lc = self.cfg.decision.location
        recent_fb = [fb for fb in self.failed_breaks if now - fb.rejected_at <= timedelta(minutes=5)]
        orr = self.opening_range
        return MarketContext(
            ts=now, index=self.index, spot=self.spot, regime=self.regime,
            bias=self.bias(external_bias, nifty_futures_proxy), trigger=self.trigger(now, breadth),
            zones=tuple(confluence_zones(self.levels(), lc.zone_tolerance_pct, lc.min_sources)),
            closes_1m=tuple(self.closes(30, before=floor_to_minutes(now, 1))),
            vwap=self.vwap.value, or_high=orr.high, or_low=orr.low, or_complete=orr.complete,
            failed_break=recent_fb[-1] if recent_fb else None, wall_shift=self.shift, gex=self.gex,
            day_implied_move=st.day_implied_move if st else None, events=tuple(events),
        )


    # ------------------------------------------------------- option window
    def option_window(self, center: float, n: int) -> list[Instrument]:
        """Options of the traded expiry within center +/- n strikes (both CE and PE)."""
        strikes = sorted({i.strike for i in self._opts.values() if i.strike is not None})
        if not strikes:
            return []
        c = min(strikes, key=lambda k: abs(k - center))
        i = strikes.index(c)
        keep = set(strikes[max(0, i - n) : i + n + 1])
        return [inst for inst in self._opts.values() if inst.strike in keep]

    def drop_options(self, tokens: set[str]) -> None:
        for tok in tokens:
            self.options.pop(tok, None)
