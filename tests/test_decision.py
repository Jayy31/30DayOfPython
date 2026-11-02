from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from engine.analytics import greeks as G
from engine.analytics.bias import read_bias
from engine.analytics.gex import GexResult
from engine.analytics.levels import FailedBreak, Level, confluence_zones
from engine.analytics.oi import WallShift
from engine.analytics.opening import OpeningRead, OpeningType
from engine.analytics.orderflow import FuturesOiClass
from engine.analytics.regime import read_regime
from engine.analytics.setups import REGISTRY, MarketContext, load_setups
from engine.analytics.stats import UNCALIBRATED_LABEL, PriorModel, ProbabilityInputs
from engine.analytics.strike import evaluate_strikes, select_expiry
from engine.analytics.straddle import StraddleState
from engine.analytics.triggers import TriggerInputs, read_trigger
from engine.broker_paper.position import Outcome
from engine.core.models import Direction, Index, OptionType
from engine.core.trading_calendar import TradingCalendar
from engine.data.models import OptionChain, OptionChainRow
from engine.decision.engine import DecisionEngine, IndexInputs
from engine.decision.reasons import confidence_1_to_5, render_text
from engine.risk.engine import RiskEngine
from tests.conftest import DAY, at

EXP = DAY + timedelta(days=7)
NOW = at(10, 0)


def make_chain(index=Index.NIFTY, spot=24000.0, step=50, vol=50_000, oi=200_000, iv=0.14, expiry=EXP):
    t = G.time_to_expiry(NOW, expiry)
    rows = []
    atm = round(spot / step) * step
    for k in range(-5, 6):
        strike = atm + k * step
        for ot in OptionType:
            px = G.price(spot, strike, t, iv, ot)
            rows.append(OptionChainRow(strike=strike, option_type=ot, token=f"{index}{int(strike)}{ot}", ltp=round(px, 1), bid=round(px - 0.3, 1), ask=round(px + 0.3, 1), volume=vol, oi=oi))
    return OptionChain(ts=NOW, index=index, expiry=expiry, spot=spot, rows=tuple(rows))


@pytest.fixture
def risk(cfg):
    return RiskEngine(cfg.rules, TradingCalendar(cfg.holidays))


def range_regime(cfg):
    return read_regime(
        cfg.decision.regime,
        gex=GexResult(total=1e9, by_strike={}, flip=23800, regime="positive"),
        straddle=StraddleState(300, 260, 290, -0.1, 300, 110, 40, 0.36, "decaying"),
        opening=OpeningRead(OpeningType.OPEN_AUCTION, 0, 0.7),
    )


def bearish_ctx(cfg, index=Index.NIFTY, spot=24000.0, **over):
    pdh = Level(spot + 20, "PDH")
    fb = FailedBreak(pdh, "above", NOW - timedelta(minutes=6), NOW, spot + 55)
    zones = tuple(confluence_zones([pdh, Level(spot + 20, "CALL_WALL"), Level(spot + 25, "VAH")], tolerance_pct=0.1))
    bias = read_bias(cfg.decision.bias, index, {"overnight": -0.6, "writers": -0.5, "futures_oi": -1.0, "news": None})
    trig = read_trigger(TriggerInputs(window_delta=-0.7, depth_imbalance=-0.4, futures_oi=FuturesOiClass.SHORT_BUILDUP, failed_break=fb))
    base = dict(ts=NOW, index=index, spot=spot, regime=range_regime(cfg), bias=bias, trigger=trig, zones=zones,
                closes_1m=tuple([spot + 10] * 10), vwap=spot - 5, failed_break=fb, day_implied_move=200.0, events=("RBI_POLICY",))
    base.update(over)
    return MarketContext(**base)


# ------------------------------------------------------------------ layer A/B/D
def test_regime_labels(cfg):
    r = range_regime(cfg)
    assert r.label == "range" and r.confidence > 0.5 and "mean_reversion" in r.allowed_families
    t = read_regime(cfg.decision.regime, gex=GexResult(-1, {}, None, "negative"), opening=OpeningRead(OpeningType.OPEN_DRIVE, 1, 0.9))
    assert t.label == "trend" and "breakout" in t.allowed_families
    u = read_regime(cfg.decision.regime, gex=GexResult(-1, {}, None, "negative"), opening=OpeningRead(OpeningType.OPEN_AUCTION, 0, 0.5))
    assert u.label == "unclear" and u.allowed_families == ("reversal",)


def test_bias_excludes_missing_and_weights(cfg):
    b = read_bias(cfg.decision.bias, Index.NIFTY, {"overnight": 1.0, "news": None, "gift_nifty": -1.0})
    assert b.score == pytest.approx(1.0) and b.label == "bullish"   # gift weight 0, news missing
    assert {d.name for d in b.drivers} == {"overnight"}


def test_sensex_uses_nifty_futures_proxy(cfg):
    own_only = read_bias(cfg.decision.bias, Index.SENSEX, {"futures_oi": 1.0})
    blended = read_bias(cfg.decision.bias, Index.SENSEX, {"futures_oi": 1.0}, nifty_futures_proxy=-1.0)
    assert own_only.score == 1.0 and blended.score == pytest.approx(0.0)
    assert read_bias(cfg.decision.bias, Index.SENSEX, {}, nifty_futures_proxy=-1.0).label == "bearish"


def test_trigger_votes():
    t = read_trigger(TriggerInputs(window_delta=0.8, depth_imbalance=0.5, futures_oi=FuturesOiClass.LONG_BUILDUP))
    assert t.direction == 1 and t.strength > 0.6 and t.supports(1, 0.5) and not t.supports(-1, 0.1)
    assert read_trigger(TriggerInputs()).direction == 0


# ------------------------------------------------------------------ layer E/F
def test_strike_selection_prefers_valid_and_filters(cfg):
    chain = make_chain()
    cands = evaluate_strikes(chain, Direction.SHORT, 100, NOW, cfg=cfg.decision.strike, charges=cfg.charges, exchange="NSE", lot_size=65, capital=15000, min_delta=0.35, strike_range=2)
    assert len(cands) == 5 and all(c.row.option_type is OptionType.PE for c in cands)
    best = cands[0]
    assert best.ok and abs(best.greeks.delta) >= 0.35 and best.lots >= 1
    assert [c.ok for c in cands] == sorted([c.ok for c in cands], reverse=True)
    strict = evaluate_strikes(chain, Direction.SHORT, 100, NOW, cfg=cfg.decision.strike, charges=cfg.charges, exchange="NSE", lot_size=65, capital=15000, min_delta=0.45, strike_range=2)
    otm = [c for c in strict if c.row.strike == 23900][0]
    assert any("delta" in r for r in otm.rejects)


def test_strike_rejects_illiquid_and_iv_spike(cfg):
    thin = make_chain(vol=10, oi=10)
    c = evaluate_strikes(thin, Direction.LONG, 100, NOW, cfg=cfg.decision.strike, charges=cfg.charges, exchange="NSE", lot_size=65, capital=15000, min_delta=0.35, strike_range=2)
    assert not any(x.ok for x in c)
    spiky = evaluate_strikes(make_chain(), Direction.LONG, 100, NOW, cfg=cfg.decision.strike, charges=cfg.charges, exchange="NSE", lot_size=65, capital=15000, min_delta=0.35, strike_range=2, reference_iv=0.10)
    assert any("IV spiking" in r for r in spiky[0].rejects)


def test_select_expiry_on_expiry_day():
    e1, e2 = DAY, DAY + timedelta(days=7)
    assert select_expiry([e2, e1], at(11, 0), switch_after=at(12, 0)) == e1
    assert select_expiry([e2, e1], at(12, 30), switch_after=at(12, 0)) == e2
    assert select_expiry([e2], at(10, 0)) == e2


def test_prior_model_is_labelled_uncalibrated(cfg):
    est = PriorModel(cfg.decision.priors).estimate(ProbabilityInputs("failed_break", 0.8, 0.6, 3, 0.8, 0.3))
    assert not est.calibrated and est.label == UNCALIBRATED_LABEL and est.model_version == "priors-v1"
    lo, hi = cfg.decision.priors.clip
    assert lo <= est.p <= hi
    weak = PriorModel(cfg.decision.priors).estimate(ProbabilityInputs("failed_break", 0, -1, 2, 0, -1))
    assert weak.p < est.p


def test_confidence_scale():
    assert [confidence_1_to_5(p) for p in (0.55, 0.62, 0.68, 0.72, 0.8)] == [1, 2, 3, 4, 5]


# ------------------------------------------------------------------ setups
def test_registry_and_config():
    assert set(REGISTRY) >= {"orb", "level_bounce", "vwap_reclaim", "trend_pullback", "oi_shift_reversal", "failed_break"}
    with pytest.raises(ValueError):
        load_setups(["nope"])


def test_setups_fire_on_their_patterns(cfg):
    s = {x.name: x for x in load_setups(list(REGISTRY))}
    ctx = bearish_ctx(cfg)
    fb = s["failed_break"].evaluate(ctx)
    assert fb is not None and fb.direction is Direction.SHORT and fb.invalidation == 24055

    orb = s["orb"].evaluate(replace(ctx, failed_break=None, or_complete=True, or_high=24010, or_low=23950, closes_1m=(24005, 24015)))
    assert orb is not None and orb.direction is Direction.LONG and orb.family == "breakout"

    vw = s["vwap_reclaim"].evaluate(replace(ctx, vwap=24000, closes_1m=(23990, 23995, 24001, 24004)))
    assert vw is not None and vw.direction is Direction.LONG

    neg = GexResult(-1, {}, None, "negative")
    tp = s["trend_pullback"].evaluate(replace(ctx, gex=neg, vwap=23950, closes_1m=(23990, 23985, 23980, 23975, 23970, 23965)))
    assert tp is None    # no pullback-and-resume shape
    tp = s["trend_pullback"].evaluate(replace(ctx, gex=neg, vwap=24050, closes_1m=(23990, 23980, 23970, 23960, 23975, 23965)))
    assert tp is not None and tp.direction is Direction.SHORT

    oi = s["oi_shift_reversal"].evaluate(replace(ctx, wall_shift=WallShift(call_wall_move=0, put_wall_move=100), closes_1m=tuple(24100 - i * 10 for i in range(10))))
    assert oi is not None and oi.direction is Direction.LONG


# ------------------------------------------------------------------ engine
def test_end_to_end_bearish_failed_break_produces_plan(cfg, risk):
    eng = DecisionEngine(cfg, risk)
    res = eng.evaluate(NOW, [IndexInputs(bearish_ctx(cfg), make_chain(), 65)], capital=15000)
    assert res.plan is not None, [s.reject_reasons for s in res.signals]
    p = res.plan
    assert p.direction is Direction.SHORT and p.contract.option_type is OptionType.PE and p.setup == "failed_break"
    assert p.probability.p >= 0.60 and not p.probability.calibrated
    assert p.planned_sl == pytest.approx(p.est_entry * 0.8, abs=0.06)
    assert "event:RBI_POLICY" in p.tags
    log = p.reason_log
    assert log["probability"]["label"] == UNCALIBRATED_LABEL and log["probability"]["model_version"] == "priors-v1"
    assert {"regime", "bias", "location", "trigger", "strike", "plan", "confidence"} <= set(log)
    text = render_text(log)
    assert "uncalibrated (priors)" in text and "PDH" in text
    taken = [s for s in res.signals if s.taken]
    assert len(taken) == 1 and taken[0].setup == "failed_break"


def test_picks_higher_scoring_index(cfg, risk):
    eng = DecisionEngine(cfg, risk)
    weak = bearish_ctx(cfg, index=Index.SENSEX, spot=80000.0, trigger=read_trigger(TriggerInputs(window_delta=-0.6)))
    strong = bearish_ctx(cfg)
    res = eng.evaluate(NOW, [IndexInputs(weak, make_chain(Index.SENSEX, 80000, 100), 20), IndexInputs(strong, make_chain(), 65)], capital=15000)
    assert res.plan is not None and res.plan.index is Index.NIFTY
    sensex = [s for s in res.signals if s.index is Index.SENSEX]
    assert sensex and not sensex[0].taken and sensex[0].score < res.plan.score


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"zones": ()}, "not at a confluence level"),
        ({"trigger": read_trigger(TriggerInputs(window_delta=0.8))}, "trigger not confirming"),
    ],
)
def test_rejections_are_recorded(cfg, risk, override, reason):
    res = DecisionEngine(cfg, risk).evaluate(NOW, [IndexInputs(bearish_ctx(cfg, **override), make_chain(), 65)], 15000)
    assert res.plan is None
    assert any(reason in r for s in res.signals for r in s.reject_reasons)


def test_against_bias_rejected(cfg, risk):
    bull = read_bias(cfg.decision.bias, Index.NIFTY, {"overnight": 1.0, "futures_oi": 1.0})
    res = DecisionEngine(cfg, risk).evaluate(NOW, [IndexInputs(bearish_ctx(cfg, bias=bull), make_chain(), 65)], 15000)
    assert res.plan is None and any("against bias" in r for s in res.signals for r in s.reject_reasons)


def test_outside_window_rejected_by_risk(cfg, risk):
    ctx = bearish_ctx(cfg, ts=at(12, 0))
    res = DecisionEngine(cfg, risk).evaluate(at(12, 0), [IndexInputs(ctx, make_chain(), 65)], 15000)
    assert res.plan is None and any("outside entry windows" in r for s in res.signals for r in s.reject_reasons)


def test_after_sl_needs_higher_probability(cfg, risk):
    risk.register_open(at(9, 40))
    risk.register_close(at(9, 50), Index.NIFTY, Direction.LONG, Outcome.SL)
    weak_trig = read_trigger(TriggerInputs(window_delta=-0.6, depth_imbalance=-0.3))
    mild = read_bias(cfg.decision.bias, Index.NIFTY, {"overnight": -0.2})
    res = DecisionEngine(cfg, risk).evaluate(NOW, [IndexInputs(bearish_ctx(cfg, trigger=weak_trig, bias=mild), make_chain(), 65)], 15000)
    sig = res.signals[0]
    assert sig.probability is not None and 0.60 <= sig.probability.p < 0.70   # would pass on a fresh day
    assert res.plan is None and any("required 0.70" in r for r in sig.reject_reasons)


def test_family_not_allowed_in_regime(cfg, risk):
    ctx = bearish_ctx(cfg, failed_break=None, or_complete=True, or_high=23990, or_low=23950, closes_1m=(23985, 23995))
    res = DecisionEngine(cfg, risk).evaluate(NOW, [IndexInputs(ctx, make_chain(), 65)], 15000)
    orb = [s for s in res.signals if s.setup == "orb"]
    assert orb and any("not allowed in range regime" in r for r in orb[0].reject_reasons)
