"""Load and validate all YAML config. Any error aborts startup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from engine.config.schemas import AppConfig


class ConfigError(RuntimeError):
    """Raised when config is missing or invalid. The engine must not start."""


_FILES = {
    "rules": "rules.yaml",
    "charges": "charges.yaml",
    "instruments": "instruments.yaml",
    "shadow": "shadow_rules.yaml",
    "holidays": "holidays.yaml",
}


def _read(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        return yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e


def load_config(config_dir: str | Path = "config") -> AppConfig:
    base = Path(config_dir)
    raw = {key: _read(base / name) for key, name in _FILES.items()}
    raw["holidays"] = {int(y): [str(d) for d in (v or [])] for y, v in raw["holidays"].items()}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"invalid config in {base}:\n{e}") from e
