from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from engine.analytics import greeks as G
from engine.analytics.breadth import heavyweight_breadth
from engine.analytics.gex import compute_gex
from engine.analytics.levels import (
    FailedBreakDetector, Level, OpeningRange, Vwap, confluence_zones, nearest_zone,
    previous_day_levels, volume_profile,
)
from engine.analytics.oi import oi_map, wall_shift
from engine.analytics.opening import OpeningType, classify_opening, initial_balance
from engine.analytics.orderflow import (
    CumulativeDelta, FuturesOiClass, classify_aggressor, classify_futures_oi,
    depth_imbalance, detect_absorption, option_premium_flow,
)
from engine.analytics.straddle import StraddleTracker
from engine.core.models import Index, OptionType
from engine.data.models import Candle, DepthLevel, OptionChain, OptionChainRow, Tick
from tests.conftest import DAY, at

CE, PE = OptionType.CE, OptionType.PE
EXP = DAY + timedelta(days=7)


# ------------------------------------------------------------------ greeks
def test_time_to_expiry():
    assert G.time_to_expiry(at(15, 30, d=EXP), EXP) == G.MIN_T
    assert G.time_to_expiry(at(15, 30), EXP) == pytest.approx(7 / 365)


@pytest.mark.parametrize("ot", [CE, PE])
@pytest.mark.parametrize("k", [23500, 24000, 24600])
def test_greeks_match_py_vollib(ot, k):
    vollib = pytest.importorskip("py_vollib.black_scholes.greeks.analytical")
    from py_vollib.black_scholes import black_scholes
    s, t, iv, r = 24000.0, 7 / 365, 0.14, G.RISK_FREE
    flag = "c" if ot is CE else "p"
    g = G.greeks(s, k, t, iv, ot)
    assert G.price(s, k, t, iv, ot) == pytest.approx(black_scholes(flag, s, k, t, r, iv), rel=1e-6)
    assert g.delta == pytest.approx(vollib.delta(flag, s, k, t, r, iv), rel=1e-6)
    assert g.gamma == pytest.approx(vollib.gamma(flag, s, k, t, r, iv), rel=1e-6)
    assert g.vega == pytest.approx(vollib.vega(flag, s, k, t, r, iv), rel=1e-6)
    assert g.theta == pytest.approx(vollib.theta(flag, s, k, t, r, iv), rel=1e-6)


@pytest.mark.parametrize("ot", [CE, PE])
def test_iv_roundtrip(ot):
    px = G.price(24000, 24100, 3 / 365, 0.18, ot)
    assert G.implied_vol(px, 24000, 24100, 3 / 365, ot) == pytest.approx(0.18, abs=1e-5)


def test_iv_rejects_arbitrage_prices():
    assert G.implied_vol(0.0, 24000, 24000, 0.01, CE) is None
    assert G.implied_vol(30000, 24000, 24000, 0.01, CE) is None
    assert G.greeks_from_price(1.0, 24000, 22000, 0.01, CE) is None   # below intrinsic


def test_atm_delta_near_half():
    assert 0.45 < G.greeks(24000, 24000, 7 / 365, 0.14, CE).delta < 0.6
    assert G.greeks(24000, 24000, 7 / 365, 0.14, CE).theta < 0


# ------------------------------------------------------------------ levels
def candle(ts, o, h, l, c, v=100):  # noqa: E741
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def test_vwap_uses_volume_deltas():
    v = Vwap()
    assert v.update(100, 1000) is None        # first print only seeds cumulative volume
    v.update(102, 1100)                       # 100 @ 102
    v.update(104, 1400)                       # 300 @ 104
    assert v.value == pytest.approx((102 * 100 + 104 * 300) / 400)


def test_opening_range():
    o = OpeningRange()
    for (h, m), px in [((9, 15), 100), ((9, 20), 105), ((9, 29), 97), ((9, 31), 120)]:
        o.update(at(h, m), px)
    assert (o.high, o.low, o.complete) == (105, 97, True)


def test_volume_profile_poc_and_value_area():
    cs = [candle(at(9, 15 + i), 100, 101, 100, 100, 10) for i in range(5)]
    cs += [candle(at(10, 0), 105, 106, 105, 105, 1000)]
    cs += [candle(at(10, 1), 110, 111, 110, 110, 10)]
    vp = volume_profile(cs, bin_size=1)
    assert vp.poc == 105
    assert vp.val <= 105 < vp.vah


def test_previous_day_levels():
    cs = [candle(at(9, 15), 100, 110, 95, 105), candle(at(15, 29), 105, 108, 90, 99)]
    assert [(l.source, l.price) for l in previous_day_levels(cs)] == [("PDH", 110), ("PDL", 90), ("PDC", 99)]


def test_confluence_needs_two_sources():
    levels = [Level(24000, "PDH"), Level(24010, "CALL_WALL"), Level(24400, "VWAP"), Level(24405, "VWAP")]
    zones = confluence_zones(levels, tolerance_pct=0.1)
    assert len(zones) == 1 and set(zones[0].sources) == {"PDH", "CALL_WALL"}
    assert nearest_zone(zones, 24030, max_distance_pct=0.2) is zones[0]
    assert nearest_zone(zones, 24300, max_distance_pct=0.2) is None


def test_failed_break_above_fires_within_window():
    d = FailedBreakDetector(Level(24000, "PDH"), within=timedelta(minutes=15))
    assert d.update(at(10, 0), 24012) is None
    assert d.update(at(10, 3), 24030) is None
    fb = d.update(at(10, 8), 23995)
    assert fb is not None and fb.side == "above" and fb.extreme == 24030


def test_break_that_holds_is_not_failed():
    d = FailedBreakDetector(Level(24000, "PDL"), within=timedelta(minutes=10))
    d.update(at(10, 0), 23980)
    assert d.update(at(10, 20), 24005) is None   # back inside only after the window


# ------------------------------------------------------------------ OI + GEX
def chain(rows, spot=24000.0):
    return OptionChain(ts=at(10, 0), index=Index.NIFTY, expiry=EXP, spot=spot, rows=tuple(rows))


def row(k, ot, oi, chg=0, s=24000.0):
    px = G.price(s, k, G.time_to_expiry(at(10, 0), EXP), 0.14, ot)
    return OptionChainRow(strike=k, option_type=ot, token=f"{k}{ot}", ltp=px, bid=px - 0.5, ask=px + 0.5, oi=oi, oi_change=chg)


def test_oi_walls_and_fresh_weighting():
    rows = [row(24200, CE, 900), row(24300, CE, 800, chg=300), row(23800, PE, 1000), row(23900, PE, 500)]
    m = oi_map(chain(rows))
    assert m.call_wall == 24300           # 800 + 300 fresh beats 900
    assert m.put_wall == 23800
    assert m.pcr == pytest.approx(1500 / 1700)
    assert oi_map(chain(rows), fresh_weight=0).call_wall == 24200


def test_wall_shift_bias():
    a = oi_map(chain([row(24300, CE, 900), row(23800, PE, 900)]))
    b = oi_map(chain([row(24200, CE, 900), row(23700, PE, 900)]))
    s = wall_shift(a, b)
    assert s.call_wall_move == -100 and s.bias == -1


def test_gex_signs_and_flip():
    heavy_calls = chain([row(k, CE, 5000) for k in (24000, 24100)] + [row(k, PE, 100) for k in (23900,)])
    assert compute_gex(heavy_calls, at(10, 0)).regime == "positive"
    heavy_puts = chain([row(k, PE, 5000) for k in (24000, 23900)] + [row(24100, CE, 100)])
    assert compute_gex(heavy_puts, at(10, 0)).regime == "negative"
    # calls above, puts below -> flip between the walls
    mixed = chain([row(24300, CE, 5000), row(23700, PE, 5000)])
    r = compute_gex(mixed, at(10, 0))
    assert r.flip is not None and 23700 < r.flip < 24300


def test_gex_uses_iv_from_chain_percent():
    r1 = compute_gex(chain([row(24000, CE, 1000)]), at(10, 0))
    rr = row(24000, CE, 1000)
    r2 = compute_gex(chain([rr.model_copy(update={"iv": 14.0})]), at(10, 0))
    assert r2.total == pytest.approx(r1.total, rel=1e-3)


# ------------------------------------------------------------------ straddle
def test_straddle_expanding_vs_decaying():
    tr = StraddleTracker(EXP)
    tr.update(at(9, 20), 150, 150, 24000)
    tr.update(at(11, 0), 175, 160, 24150)
    st = tr.state()
    assert st is not None and st.label == "expanding" and st.realised_range == 150
    tr2 = StraddleTracker(EXP)
    tr2.update(at(9, 20), 150, 150, 24000)
    tr2.update(at(14, 0), 130, 130, 24010)
    assert tr2.state().label == "decaying"   # type: ignore[union-attr]
    assert st.day_implied_move == pytest.approx(300 / math.sqrt(7 + 6.1667 / 24), rel=1e-3)   # 09:20 -> expiry 15:30


# ------------------------------------------------------------------ opening
def mins(prices):
    """prices: list of (o,h,l,c)."""
    return [candle(at(9, 15 + i), *p) for i, p in enumerate(prices)]


def test_open_drive_up():
    r = classify_opening(mins([(100, 103, 99.8, 103), (103, 106, 102, 106), (106, 110, 105, 109)]))
    assert r.type is OpeningType.OPEN_DRIVE and r.direction == 1


def test_open_test_drive_up():
    r = classify_opening(mins([(100, 100.5, 96, 97), (97, 101, 96.5, 101), (101, 108, 100.5, 108)]))
    assert r.type is OpeningType.OPEN_TEST_DRIVE and r.direction == 1


def test_open_rejection_reverse():
    r = classify_opening(mins([(100, 106, 99.5, 105), (105, 106.5, 101, 101), (101, 102, 97, 97.5)]))
    assert r.type is OpeningType.OPEN_REJECTION_REVERSE and r.direction == -1


def test_open_auction():
    r = classify_opening(mins([(100, 102, 98, 101), (101, 102, 98, 99), (99, 101.5, 98.5, 100.2)]))
    assert r.type is OpeningType.OPEN_AUCTION


def test_initial_balance_odds():
    cs = mins([(100, 110, 95, 105)] * 3)
    assert initial_balance(cs, day_implied_move=60).extension_odds == "high"
    assert initial_balance(cs, day_implied_move=10).extension_odds == "low"


# ------------------------------------------------------------------ order flow
def tk(m, ltp, bid, ask, vol, s=0, **kw):
    return Tick(ts=at(10, m, s), token="F", ltp=ltp, bid=bid, ask=ask, volume=vol, last_qty=kw.pop("q", 0), **kw)


def test_aggressor_rules():
    assert classify_aggressor(tk(0, 101, 100, 101, 0), None) == 1
    assert classify_aggressor(tk(0, 100, 100, 101, 0), None) == -1
    assert classify_aggressor(tk(0, 100.5, 100, 101, 0), 100.2) == 1   # tick rule
    assert classify_aggressor(tk(0, 100.5, 100, 101, 0), None) == 0


def test_cumulative_delta_from_volume_increments():
    cd = CumulativeDelta()
    cd.update(tk(0, 101, 100, 101, 1000))
    cd.update(tk(0, 101, 100, 101, 1300, s=1))   # +300 buy
    cd.update(tk(0, 100, 100, 101, 1400, s=2))   # -100 sell
    assert cd.cum == 300 - 100   # first print has no known increment


def test_depth_imbalance():
    t = Tick(ts=at(10, 0), token="F", ltp=100, bids=(DepthLevel(price=99.9, qty=300),), asks=(DepthLevel(price=100, qty=100),))
    assert depth_imbalance(t) == pytest.approx(0.5)
    assert depth_imbalance(tk(0, 100, 99.9, 100, 0, bid_qty=10, ask_qty=30)) == pytest.approx(-0.5)


def test_absorption_at_support():
    cd = CumulativeDelta()
    vol = 0
    for i in range(10):
        vol += 500
        cd.update(tk(1, 24000, 24000, 24001, vol, s=i))   # sellers keep hitting the bid, price holds
    a = detect_absorption(cd, level=24000, tolerance=2, min_delta=2000)
    assert a is not None and a.side == 1


@pytest.mark.parametrize(
    ("p", "oi", "cls"),
    [
        (0.3, 1.0, FuturesOiClass.LONG_BUILDUP),
        (-0.3, 1.0, FuturesOiClass.SHORT_BUILDUP),
        (0.3, -1.0, FuturesOiClass.SHORT_COVERING),
        (-0.3, -1.0, FuturesOiClass.LONG_UNWINDING),
        (0.01, 2.0, FuturesOiClass.NEUTRAL),
    ],
)
def test_futures_oi_classes(p, oi, cls):
    assert classify_futures_oi(p, oi) is cls


def test_option_premium_flow_sign():
    ce = [tk(0, 101, 100, 101, 0, q=100), tk(0, 102, 101, 102, 0, s=1, q=100)]
    pe = [tk(0, 50, 50, 51, 0, q=100)]
    assert option_premium_flow(ce, pe) > 0.5


# ------------------------------------------------------------------ breadth
W = {"HDFCBANK": 13.0, "RELIANCE": 9.0, "ICICIBANK": 8.0, "INFY": 5.0}


def test_breadth_agrees():
    b = heavyweight_breadth(W, {"HDFCBANK": 0.5, "RELIANCE": 0.4, "ICICIBANK": 0.3, "INFY": 0.2}, 0.4)
    assert b.direction == 1 and not b.divergence and b.advancing_weight == 1


def test_breadth_divergence():
    b = heavyweight_breadth(W, {"HDFCBANK": -0.6, "RELIANCE": -0.4, "ICICIBANK": -0.2, "INFY": 0.1}, 0.3)
    assert b.divergence and b.direction == -1
