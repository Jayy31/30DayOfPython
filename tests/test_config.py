from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
import yaml

from engine.config.loader import ConfigError, load_config
from engine.config.settings import AppMode, Settings


def _edit(cfg_dir: Path, name: str, **changes: object) -> None:
    p = cfg_dir / name
    data = yaml.safe_load(p.read_text())
    data.update(changes)
    p.write_text(yaml.safe_dump(data))


def test_shipped_config_is_valid(cfg):
    r = cfg.rules
    assert r.capital == 15000
    assert r.max_trades_per_day == 2 and r.max_open_trades == 1
    assert r.entry_windows == [(time(9, 30), time(11, 30)), (time(13, 30), time(14, 30))]
    assert r.square_off == time(15, 15)
    assert r.trail.timeframe_min == 3 and r.trail.pivot_bars == 2
    assert {s.id for s in cfg.shadow.rules} >= {"half_sizing", "trail_1min", "trail_5min"}


@pytest.mark.parametrize("sl", [17, 21, 25])
def test_sl_outside_18_20_refuses(config_copy, sl):
    _edit(config_copy, "rules.yaml", sl_pct=sl)
    with pytest.raises(ConfigError):
        load_config(config_copy)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_trades_per_day": 3},
        {"max_open_trades": 2},
        {"min_probability_after_sl": 0.5},
        {"second_target_rr": 1.0},
        {"entry_windows": [["09:20", "11:30"]]},            # before observe_until
        {"entry_windows": [["13:30", "14:30"], ["09:30", "11:30"]]},  # unordered
        {"entry_windows": [["14:00", "15:20"]]},            # past square-off
        {"square_off": "15:45"},
        {"unknown_key": 1},
        {"indices": ["NIFTY", "BANKNIFTY"]},
    ],
)
def test_invalid_rules_refuse(config_copy, changes):
    _edit(config_copy, "rules.yaml", **changes)
    with pytest.raises(ConfigError):
        load_config(config_copy)


def test_missing_file_refuses(config_copy):
    (config_copy / "charges.yaml").unlink()
    with pytest.raises(ConfigError, match="missing"):
        load_config(config_copy)


def test_duplicate_shadow_ids_refuse(config_copy):
    p = config_copy / "shadow_rules.yaml"
    p.write_text(yaml.safe_dump({"rules": [{"id": "a", "type": "x"}, {"id": "a", "type": "y"}]}))
    with pytest.raises(ConfigError):
        load_config(config_copy)


def test_live_mode_requires_kotak_credentials(monkeypatch):
    s = Settings(_env_file=None, app_mode=AppMode.LIVE_DATA)
    with pytest.raises(ValueError, match="KOTAK_"):
        s.require_live_credentials()
    Settings(_env_file=None).require_live_credentials()  # replay needs nothing
