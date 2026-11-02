"""Daily job schedule (IST). Every job is idempotent per (job, day) and skips NSE holidays.

Jobs that need external accounts (Kotak login, NSE downloads, global data) are
registered now and become real in Phase 10; until then they record a 'not configured' event.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from engine.app import App
from engine.core.timeutils import IST, now_ist
from engine.metrics.report_data import period_report_data
from engine.reports.builder import build_report, trade_summary

log = logging.getLogger(__name__)
JobFn = Callable[[App, date], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    name: str
    cron: dict[str, str]
    fn: JobFn
    trading_day_only: bool = True


# ------------------------------------------------------------------ bodies
async def _not_configured(app: App, day: date, what: str) -> None:
    app.store.save_event(now_ist(), "job_not_configured", {"job": what, "day": day.isoformat(), "phase": 10})


async def overnight_data(app: App, day: date) -> None:
    await _not_configured(app, day, "overnight_data")


async def broker_login(app: App, day: date) -> None:
    await _not_configured(app, day, "broker_login_scrip_master")


async def nse_data(app: App, day: date) -> None:
    await _not_configured(app, day, "nse_data")


async def premarket(app: App, day: date) -> None:
    data = {
        "bias": {"news": app.news.bias(now_ist())},
        "events": app.news.active_events(now_ist()),
        "capital": app.broker.capital,
        "note": "levels, expected range and strike watchlist arrive with live data (Phase 10)",
    }
    rep = await build_report("premarket", day, data, app.llm)
    app.store.save_report(rep)
    await app.notifier.send(rep.body)  # type: ignore[union-attr]


async def start_live(app: App, day: date) -> None:
    await app.run_live_day(day)


async def square_off(app: App, day: date) -> None:
    # The live engine squares off itself at 15:15; this job is the safety net.
    if app.broker.has_open_position and app.broker.position and app.broker.position.last_quote:
        app.broker.square_off(app.broker.position.last_quote)
        await app.notifier.send("Safety square-off executed by scheduler")  # type: ignore[union-attr]


async def end_of_day(app: App, day: date) -> None:
    n = app.save_ticks(day)
    app.store.save_event(now_ist(), "ticks_saved", {"day": day.isoformat(), "rows": n})


async def postmarket(app: App, day: date) -> None:
    trades = app.store.trades_between(day, day)
    res = app.last_result
    data = {"summary": trade_summary(trades), "capital": app.broker.capital, "signals": res.signals if res else 0, "feed_gaps": res.feed_gaps if res else 0}
    rep = await build_report("postmarket", day, data, app.llm)
    app.store.save_report(rep)
    await app.notifier.send(rep.body)  # type: ignore[union-attr]


async def fii_evening(app: App, day: date) -> None:
    await _not_configured(app, day, "nse_fii_dii_evening")


def _period_data(app: App, start: date, end: date) -> dict:  # type: ignore[type-arg]
    trades = app.store.trades_between(start, end)
    before = app.store.trades_between(date.min, start.fromordinal(start.toordinal() - 1))
    start_capital = app.cfg.rules.capital + sum(t.net_pnl for t in before)
    data = period_report_data(app.cfg, app.store, trades, start_capital, (start, end), app.llm.ledger if app.llm else None, all_trades=before + trades)
    data["summary"] = trade_summary(trades)
    data["capital"] = app.broker.capital
    return data


async def weekly(app: App, day: date) -> None:
    start, end = app.week_bounds(day)
    rep = await build_report("weekly", start, _period_data(app, start, end), app.llm)
    app.store.save_report(rep)
    await app.notifier.send(rep.body)  # type: ignore[union-attr]


async def monthly(app: App, day: date) -> None:
    last_month_end = day.replace(day=1).fromordinal(day.replace(day=1).toordinal() - 1)
    start = last_month_end.replace(day=1)
    rep = await build_report("monthly", start, _period_data(app, start, last_month_end), app.llm)
    app.store.save_report(rep)
    await app.notifier.send(rep.body)  # type: ignore[union-attr]
    await _not_configured(app, day, "supabase_backup_export")


JOBS: list[Job] = [
    Job("overnight_data", {"hour": "6", "minute": "30"}, overnight_data),
    Job("broker_login", {"hour": "8", "minute": "0"}, broker_login),
    Job("nse_data", {"hour": "8", "minute": "10"}, nse_data),
    Job("premarket", {"hour": "8", "minute": "45"}, premarket),
    Job("start_live", {"hour": "9", "minute": "0"}, start_live),
    Job("square_off", {"hour": "15", "minute": "15"}, square_off),
    Job("end_of_day", {"hour": "15", "minute": "35"}, end_of_day),
    Job("postmarket", {"hour": "16", "minute": "0"}, postmarket),
    Job("fii_evening", {"hour": "19", "minute": "0"}, fii_evening),
    Job("weekly", {"day_of_week": "sat", "hour": "10", "minute": "0"}, weekly, trading_day_only=False),
    Job("monthly", {"day": "1", "hour": "10", "minute": "30"}, monthly, trading_day_only=False),
]


class JobRunner:
    def __init__(self, app: App) -> None:
        self.app = app

    async def run(self, job: Job, day: date | None = None, *, force: bool = False) -> str:
        day = day or now_ist().date()
        if job.trading_day_only and not self.app.calendar.is_trading_day(day):
            return "skipped_holiday"
        if not force and self.app.store.job_done(job.name, day):
            return "skipped_done"
        try:
            await job.fn(self.app, day)
        except Exception as e:  # noqa: BLE001 - a failing job must alert, never kill the scheduler
            log.exception("job failed", extra={"job": job.name})
            self.app.store.save_event(now_ist(), "job_failed", {"job": job.name, "day": day.isoformat(), "error": str(e)})
            await self.app.notifier.send(f"ALERT: job {job.name} failed: {e}")  # type: ignore[union-attr]
            return "failed"
        self.app.store.mark_job(job.name, day)
        return "ok"


def build_scheduler(app: App, jobs: list[Job] | None = None) -> AsyncIOScheduler:
    runner = JobRunner(app)
    sched = AsyncIOScheduler(timezone=IST)
    for job in jobs or JOBS:
        sched.add_job(runner.run, CronTrigger(timezone=IST, **job.cron), args=[job], id=job.name, name=job.name, misfire_grace_time=300, coalesce=True, max_instances=1)
    return sched


def next_runs(sched: AsyncIOScheduler, now: datetime) -> dict[str, datetime]:
    return {j.id: j.trigger.get_next_fire_time(None, now) for j in sched.get_jobs()}
