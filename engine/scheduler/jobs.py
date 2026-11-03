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
    if not app.is_live:
        return await _not_configured(app, day, "overnight_data")
    gm = app.global_markets()
    await gm.refresh()
    app.overnight = gm.score()
    app.store.save_event(now_ist(), "overnight_data", app.overnight)
    try:
        await app.poll_news(now_ist())
    except Exception as e:  # noqa: BLE001 - news is context, never blocking
        app.store.save_event(now_ist(), "news_poll_failed", {"error": str(e)})


async def broker_login(app: App, day: date) -> None:
    if not app.is_live:
        return await _not_configured(app, day, "broker_login_scrip_master")
    p = app.kotak()
    await p.login()
    insts = await p.instruments(day)
    lots = {i.underlying.value: i.lot_size for i in insts if i.kind.value == "OPTION" and i.underlying}
    app.store.save_event(now_ist(), "broker_login", {"instruments": len(insts), "lot_sizes": lots})


async def nse_data(app: App, day: date) -> None:
    if not app.is_live:
        return await _not_configured(app, day, "nse_data")
    from engine.data.nse import positioning_score

    nse = app.nse()
    errors: dict[str, str] = {}
    try:
        hol = await nse.holidays(day)
        app.calendar = type(app.calendar)({day.year: [d.isoformat() for d in hol]} | {y: v for y, v in app.cfg.holidays.items() if y != day.year})
        app.risk.calendar = app.calendar
    except Exception as e:  # noqa: BLE001
        errors["holidays"] = str(e)
    prev = app.calendar.previous_trading_day(day)
    try:
        today_oi = await nse.participant_oi(prev)
        before = await nse.participant_oi(app.calendar.previous_trading_day(prev))
        app.positioning = positioning_score(today_oi, before)
    except Exception as e:  # noqa: BLE001
        errors["participant_oi"] = str(e)
    try:
        app.flows = await nse.fii_dii(day)
    except Exception as e:  # noqa: BLE001
        errors["fii_dii"] = str(e)
    try:
        path = await nse.fo_bhavcopy(prev)
        st = app.storage()
        if st is not None:
            st.upload(path, "nse", "bhavcopy", f"{prev.isoformat()}.csv.zip")
    except Exception as e:  # noqa: BLE001
        errors["fo_bhavcopy"] = str(e)
    app.store.save_event(now_ist(), "nse_data", {"positioning": app.positioning, "errors": errors})
    if errors:
        await app.notifier.send(f"NSE data partially failed: {', '.join(errors)}")  # type: ignore[union-attr]


async def _watchlist(app: App, day: date) -> dict:  # type: ignore[type-arg]
    from engine.core.models import Index, OptionType
    from engine.data.broker.kotak import atm_from_parity

    out = {}
    p = app.kotak()
    for name in app.cfg.rules.indices:
        idx = Index(name)
        exps = await p.expiries(idx, day)
        if not exps:
            continue
        chain = await p.option_chain(idx, exps[0])
        atm = atm_from_parity(list(chain.rows))
        if atm is None:
            continue
        ce, pe = chain.row(atm, OptionType.CE), chain.row(atm, OptionType.PE)
        strikes = sorted({r.strike for r in chain.rows})
        i = strikes.index(atm)
        near = strikes[max(0, i - 2) : i + 3]
        out[name] = {
            "expiry": exps[0].isoformat(), "atm": atm,
            "straddle": round((ce.ltp if ce else 0) + (pe.ltp if pe else 0), 2),
            "expected_range": [round(atm - ((ce.ltp if ce else 0) + (pe.ltp if pe else 0)), 0), round(atm + ((ce.ltp if ce else 0) + (pe.ltp if pe else 0)), 0)],
            "watchlist": [{"strike": k, "ce": getattr(chain.row(k, OptionType.CE), "ltp", None), "pe": getattr(chain.row(k, OptionType.PE), "ltp", None)} for k in near],
        }
    return out


async def premarket(app: App, day: date) -> None:
    now = now_ist()
    data = {
        "bias": {"news": app.news.bias(now), "overnight": app.overnight.get("score"), "positioning": app.positioning.get("bias")},
        "overnight": app.overnight,
        "positioning": app.positioning,
        "fii_dii": [f.__dict__ for f in app.flows][:4],
        "events": app.news.active_events(now),
        "capital": app.broker.capital,
        "gift_nifty": "not available - weight 0 (no reliable free source)",
    }
    if app.is_live:
        try:
            data["indices"] = await _watchlist(app, day)
        except Exception as e:  # noqa: BLE001
            data["indices_error"] = str(e)
    else:
        data["note"] = "replay mode: no live levels / watchlist"
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
    if not app.is_live:
        return await _not_configured(app, day, "nse_fii_dii_evening")
    nse = app.nse()
    cached = nse._cache(day, "fii_dii.json")
    if cached.exists():
        cached.unlink()   # morning copy holds yesterday's figures; refetch the provisional ones
    app.flows = await nse.fii_dii(day)
    rows = [f.__dict__ for f in app.flows]
    app.store.save_event(now_ist(), "fii_dii_evening", {"rows": rows})
    if rows:
        await app.notifier.send("FII/DII provisional: " + "; ".join(f"{r['category']} net {r['net_cr']:+,.0f} cr" for r in rows))  # type: ignore[union-attr]


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
    await backup_export(app, day)


async def backup_export(app: App, day: date) -> None:
    """Monthly: export every table to Parquet and upload (Supabase free tier has no PITR)."""
    from engine.store.sql import SqlStore

    st = app.storage()
    if not isinstance(app.store, SqlStore) or st is None:
        return await _not_configured(app, day, "backup_export (needs STORE_BACKEND=postgres + object storage)")
    from engine.store.backup import export_tables

    paths = export_tables(app.store.engine, app.data_dir / "backup" / day.isoformat())
    for path in paths:
        st.upload(path, "backup", day.isoformat(), path.name)
    app.store.save_event(now_ist(), "backup_export", {"tables": [p.name for p in paths]})


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
