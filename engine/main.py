"""Engine entrypoint. Phase 1: validate settings + config, then exit.

Feeds, scheduler and API are wired in later phases.
"""

from __future__ import annotations

import logging
import sys

from engine.config.loader import ConfigError, load_config
from engine.config.settings import Settings
from engine.core.logs import setup_logging
from engine.core.timeutils import now_ist
from engine.core.trading_calendar import TradingCalendar

log = logging.getLogger("engine")


def main() -> int:
    settings = Settings()
    setup_logging(settings.log_level)
    try:
        settings.require_live_credentials()
        cfg = load_config(settings.config_dir)
    except (ConfigError, ValueError) as e:
        log.critical("refusing to start: %s", e)
        return 1
    cal = TradingCalendar(cfg.holidays)
    today = now_ist().date()
    log.info(
        "engine ready",
        extra={"mode": settings.app_mode.value, "capital": cfg.rules.capital, "trading_day": cal.is_trading_day(today)},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
