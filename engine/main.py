"""Engine entrypoint.

  python -m engine.main                   # validate config and exit
  python -m engine.main replay 2026-11-03 [--days N] [--serve]
                                          # run synthetic/recorded day(s) end to end; --serve keeps the API up
  python -m engine.main run               # scheduler (all daily jobs) + dashboard API
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date

from engine.config.loader import ConfigError, load_config
from engine.config.settings import AppMode, Settings
from engine.core.logs import setup_logging
from engine.core.timeutils import now_ist
from engine.core.trading_calendar import TradingCalendar

log = logging.getLogger("engine")


def _store(settings: Settings):  # type: ignore[no-untyped-def]
    from engine.store.base import MemoryStore

    if settings.store_backend == "postgres":
        from engine.db.session import make_engine
        from engine.store.sql import SqlStore

        return SqlStore(make_engine(settings.database_url))
    return MemoryStore()


async def _replay(settings: Settings, cfg, day: date, days: int, serve: bool) -> int:  # type: ignore[no-untyped-def]
    from engine.app import App
    from engine.reports.builder import trade_summary
    from engine.scheduler.jobs import JOBS, JobRunner

    app = App(settings, cfg, store=_store(settings))
    runner = JobRunner(app)
    job = {j.name: j for j in JOBS}
    d = day
    for i in range(days):
        if i:
            d = app.calendar.next_trading_day(d)
        app.replay_seed = 7 + i
        for name in ("premarket", "start_live", "postmarket"):
            await runner.run(job[name], d, force=True)
        s = trade_summary(app.store.trades_between(d, d))
        log.info("replay day done", extra={"day": d.isoformat(), "trades": s["trades"], "net_pnl": s["net_pnl"], "capital": app.broker.capital})
    await runner.run(job["weekly"], d, force=True)
    if serve:
        from engine.api.wiring import serve as serve_api

        log.info("serving API", extra={"host": settings.engine_api_host, "port": settings.engine_api_port})
        await serve_api(app)
    return 0


async def _run(settings: Settings, cfg) -> int:  # type: ignore[no-untyped-def]
    from engine.app import App
    from engine.scheduler.jobs import build_scheduler

    from engine.api.wiring import serve as serve_api

    app = App(settings, cfg, store=_store(settings))
    sched = build_scheduler(app)
    sched.start()
    log.info("scheduler started", extra={"jobs": [j.id for j in sched.get_jobs()]})
    await serve_api(app)   # runs until stopped; scheduler jobs share this event loop
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="engine")
    sub = parser.add_subparsers(dest="cmd")
    rp = sub.add_parser("replay")
    rp.add_argument("day", type=date.fromisoformat)
    rp.add_argument("--days", type=int, default=1)
    rp.add_argument("--serve", action="store_true")
    sub.add_parser("run")
    sub.add_parser("verify-kotak", help="read-only check of the Kotak account against the engine's assumptions")
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
        return asyncio.run(_replay(settings, cfg, args.day, args.days, args.serve))
    if args.cmd == "verify-kotak":
        from engine.app import App
        from engine.tools.verify_kotak import main as verify

        return verify(cfg, App(settings.model_copy(update={"app_mode": AppMode.LIVE_DATA}), cfg).kotak())
    if args.cmd == "run":
        return asyncio.run(_run(settings, cfg))
    cal = TradingCalendar(cfg.holidays)
    log.info("engine ready", extra={"mode": settings.app_mode.value, "capital": cfg.rules.capital, "trading_day": cal.is_trading_day(now_ist().date())})
    return 0


if __name__ == "__main__":
    sys.exit(main())
