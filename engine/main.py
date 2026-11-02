"""Engine entrypoint.

  python -m engine.main                   # validate config and exit
  python -m engine.main replay 2026-11-03 # run one synthetic/recorded day end to end
  python -m engine.main run               # start the scheduler (all daily jobs)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

from engine.config.loader import ConfigError, load_config
from engine.config.settings import Settings
from engine.core.logs import setup_logging
from engine.core.timeutils import now_ist
from engine.core.trading_calendar import TradingCalendar

log = logging.getLogger("engine")


async def _replay(settings: Settings, cfg, day: date) -> int:  # type: ignore[no-untyped-def]
    from engine.app import App
    from engine.reports.builder import trade_summary
    from engine.scheduler.jobs import JOBS, JobRunner

    app = App(settings, cfg)
    runner = JobRunner(app)
    for name in ("premarket", "start_live", "postmarket"):
        status = await runner.run(next(j for j in JOBS if j.name == name), day, force=True)
        log.info("job", extra={"job": name, "status": status})
    s = trade_summary(app.store.trades_between(day, day))
    log.info("replay done", extra={"trades": s["trades"], "net_pnl": s["net_pnl"], "capital": app.broker.capital, "signals": app.last_result.signals if app.last_result else 0})
    return 0


async def _run(settings: Settings, cfg) -> int:  # type: ignore[no-untyped-def]
    from engine.app import App
    from engine.scheduler.jobs import build_scheduler

    sched = build_scheduler(App(settings, cfg))
    sched.start()
    log.info("scheduler started", extra={"jobs": [j.id for j in sched.get_jobs()]})
    await asyncio.Event().wait()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engine")
    sub = parser.add_subparsers(dest="cmd")
    rp = sub.add_parser("replay")
    rp.add_argument("day", type=date.fromisoformat)
    sub.add_parser("run")
    args = parser.parse_args(argv)

    settings = Settings()
    setup_logging(settings.log_level)
    try:
        settings.require_live_credentials()
        cfg = load_config(settings.config_dir)
    except (ConfigError, ValueError) as e:
        log.critical("refusing to start: %s", e)
        return 1
    if args.cmd == "replay":
        return asyncio.run(_replay(settings, cfg, args.day))
    if args.cmd == "run":
        return asyncio.run(_run(settings, cfg))
    cal = TradingCalendar(cfg.holidays)
    log.info("engine ready", extra={"mode": settings.app_mode.value, "capital": cfg.rules.capital, "trading_day": cal.is_trading_day(now_ist().date())})
    return 0


if __name__ == "__main__":
    sys.exit(main())
