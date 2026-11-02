from __future__ import annotations

import shutil
from datetime import date, datetime, time
from pathlib import Path

import pytest

from engine.config.loader import load_config
from engine.config.schemas import AppConfig
from engine.core.timeutils import IST

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


@pytest.fixture(scope="session")
def cfg() -> AppConfig:
    return load_config(CONFIG_DIR)


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, dst)
    return dst


DAY = date(2026, 11, 3)  # a Tuesday


def at(hh: int, mm: int, ss: int = 0, d: date = DAY) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), tzinfo=IST)
