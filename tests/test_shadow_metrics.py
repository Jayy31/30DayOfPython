from __future__ import annotations

from datetime import date

import pytest
import yaml

from engine.broker_paper.position import Outcome
from engine.config.loader import ConfigError, load_config
from engine.config.schemas import TrailConfig
from engine.metrics.compute import breakdowns, compute
from engine.metrics.report_data import period_report_data
from engine.shadow.resim import resimulate
from engine.shadow.simulator import ShadowSimulator
from engine.store.base import MemoryStore
from tests.trade_factory import LOSS, WIN, make_trade

MON, TUE, WED, THU = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)
NEXT_MON = date(2026, 11, 9)


# ------------------------------------------------------------------ metrics
def test_metrics_basic(cfg):
    ts = [make_trade(cfg, MON, 1, WIN), make_trade(cfg, TUE, 1, LOSS), make_trade(cfg, WED, 1, LOSS), make_trade(cfg, THU, 1, WIN)]
    pnl = [t.net_pnl for t in ts]
    assert pnl[0] > 0 and pnl[1] < 0
    m = compute(ts, 15000)
    assert m.trades == 4 and m.wins == 2 and m.win_rate == 0.5
    assert m.expectancy == pytest.approx(0.5 * m.avg_win - 0.5 * m.avg_loss, abs=0.01)
    assert m.profit_factor == pytest.approx((pnl[0] + pnl[3]) / -(pnl[1] + pnl[2]), rel=1e-3)
    assert m.longest_losing_streak == 2
    assert m.max_drawdown == pytest.approx(-(pnl[1] + pnl[2]), abs=0.01)
    assert m.end_capital == pytest.approx(15000 + sum(pnl), abs=0.01)
    assert len(m.capital_curve) == 4


def test_metrics_empty_and_no_losses(cfg):
    assert compute([], 15000).trades == 0
    assert compute([make_trade(cfg, MON, 1, WIN)], 15000).profit_factor is None


def test_breakdowns(cfg):
    ts = [
        make_trade(cfg, MON, 1, WIN, setup="orb", tags=["event:RBI_POLICY"]),
        make_trade(cfg, MON, 2, LOSS, setup="vwap_reclaim", start=__import__("datetime").time(13, 45), regime="range"),
    ]
    b = breakdowns(ts, 15000)
    assert set(b) >= {"setup", "index", "time_window", "regime", "vix_band", "event", "expiry_day", "trade_no"}
    assert set(b["time_window"]) == {"window_1", "window_2"}
    assert set(b["event"]) == {"RBI_POLICY", "no_event"}
    assert b["trade_no"]["trade_2"]["trades"] == 1 and b["vix_band"]["unknown"]["trades"] == 2


# ------------------------------------------------------------------ resim
@pytest.mark.parametrize("prices", [WIN, LOSS, [105, 110, 104, 108]])
def test_resim_reproduces_original(cfg, prices):
    t = make_trade(cfg, MON, 1, prices, lots=3)
    sim = resimulate(t.position)
    assert sim.net_pnl == pytest.approx(t.net_pnl, abs=0.01) and sim.outcome is t.position.outcome


def test_resim_one_lot_differs_from_two(cfg):
    t = make_trade(cfg, MON, 1, WIN, lots=2)
    one = resimulate(t.position, lots=1)
    assert one.lots == 1 and one.outcome is Outcome.TARGET
    assert [leg.reason.value for leg in one.legs][1:] != [leg.reason.value for leg in t.position.legs][1:]


def test_resim_uses_path_after_original_exit(cfg):
    # 2 lots: 1R books 1, then breakeven stop; afterwards the premium runs - a 1-lot variant
    # with the same stop also exits; prices after exit must still be available to variants
    t = make_trade(cfg, MON, 1, [122, 106, 140, 160])
    assert len(t.position.path) == 4 and not t.position.is_open


def test_trail_variant_changes_exit(cfg):
    up = [122, 132] + [140, 145, 138, 146, 150, 152, 149, 136, 150, 151, 153, 125]
    t = make_trade(cfg, MON, 1, up, lots=1)
    fast = resimulate(t.position, trail=TrailConfig(timeframe_min=1, pivot_bars=1, buffer_pct=0))
    assert fast.net_pnl != pytest.approx(t.net_pnl)


# ------------------------------------------------------------------ shadow rules
def run(cfg, trades):
    return ShadowSimulator(cfg).run(trades, 15000)


def test_weekly_profit_cap(cfg):
    big = [make_trade(cfg, d, 1, WIN, lots=3) for d in (MON, TUE, WED)] + [make_trade(cfg, NEXT_MON, 1, WIN, lots=3)]
    assert big[0].net_pnl > 1000
    r = run(cfg, big)["weekly_profit_cap"]
    week_pnl = 0.0
    expected_skips = 0
    for t in big[:3]:
        if week_pnl >= 3500:
            expected_skips += 1
        else:
            week_pnl += t.net_pnl
    assert r.trades_skipped == expected_skips and r.steps[3].taken   # new week resets


def test_weekly_sl_pause(cfg):
    ts = [make_trade(cfg, d, 1, LOSS) for d in (MON, TUE, WED)] + [make_trade(cfg, THU, 1, WIN), make_trade(cfg, NEXT_MON, 1, WIN)]
    r = run(cfg, ts)["weekly_sl_pause"]
    assert [s.taken for s in r.steps] == [True, True, True, False, True]
    assert r.simulated_pnl == pytest.approx(sum(t.net_pnl for t in ts[:3]) + ts[4].net_pnl, abs=0.01)


def test_monthly_drawdown_pause(cfg):
    ts = [make_trade(cfg, date(2026, 11, d), 1, LOSS, lots=2) for d in (2, 3, 4, 5, 6, 9)] + [make_trade(cfg, date(2026, 12, 1), 1, WIN)]
    r = run(cfg, ts)["monthly_drawdown_cap"]
    first_skip = next(i for i, s in enumerate(r.steps) if not s.taken)
    lost = -sum(t.net_pnl for t in ts[:first_skip])
    assert lost >= 0.30 * 15000 and r.steps[-1].taken   # new month resumes


def test_half_sizing_recomputes_lots(cfg):
    t = make_trade(cfg, MON, 1, WIN, lots=2, entry=101.0)
    step = run(cfg, [t])["half_sizing"].steps[0]
    assert step.taken and step.state["lots"] == 1
    assert step.sim_pnl == pytest.approx(resimulate(t.position, lots=1).net_pnl, abs=0.01)
    pricey = make_trade(cfg, MON, 1, WIN, lots=1, entry=200.0)
    assert not run(cfg, [pricey])["half_sizing"].steps[0].taken


def test_index_switch_flag(cfg):
    ts = [make_trade(cfg, MON, 1, LOSS), make_trade(cfg, MON, 2, WIN, tags=["after_sl", "index_switch_same_bet"])]
    r = run(cfg, ts)["index_switch_tag"]
    assert [s.taken for s in r.steps] == [True, False] and r.steps[1].state["flagged"]


def test_trail_variants_configured(cfg):
    r = run(cfg, [make_trade(cfg, MON, 1, WIN, lots=1)])
    assert {"trail_1min", "trail_5min"} <= set(r)


def test_new_rule_via_config_only(cfg, config_copy):
    p = config_copy / "shadow_rules.yaml"
    data = yaml.safe_load(p.read_text())
    data["rules"].append({"id": "weekly_cap_2000", "type": "weekly_pnl_cap", "params": {"cap": 2000}})
    p.write_text(yaml.safe_dump(data))
    c2 = load_config(config_copy)
    assert "weekly_cap_2000" in ShadowSimulator(c2).run([make_trade(c2, MON, 1, WIN)])


def test_unknown_rule_type_refused(cfg, config_copy):
    p = config_copy / "shadow_rules.yaml"
    p.write_text(yaml.safe_dump({"rules": [{"id": "x", "type": "nope"}]}))
    c2 = load_config(config_copy)
    with pytest.raises(ValueError, match="unknown shadow rule types"):
        ShadowSimulator(c2)


# ------------------------------------------------------------------ report data
def test_period_report_data_persists_shadow(cfg):
    store = MemoryStore()
    ts = [make_trade(cfg, MON, 1, LOSS), make_trade(cfg, MON, 2, WIN, tags=["index_switch_same_bet"])]
    data = period_report_data(cfg, store, ts, 15000, (MON, MON))
    assert data["sample"]["judgeable"] is False and data["probabilities"] == "uncalibrated (priors)"
    assert {row["rule"] for row in data["shadow_rules"]} == {r.id for r in cfg.shadow.rules}
    assert len(store.shadow) == len(cfg.shadow.rules) * 2
    flagged = store.shadow[("index_switch_tag", str(ts[1].position.id))]
    assert flagged[0] is True and flagged[1] == 0
