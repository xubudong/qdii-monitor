from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .service import MonitorService
from .settings import TIMEZONE


def create_scheduler(service: MonitorService, run_startup_refresh: bool = True) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        service.refresh_premarket_anchors,
        CronTrigger(day_of_week="mon-fri", hour=8, minute="*/5", second=0, timezone=TIMEZONE),
        id="premarket_anchor_8",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_premarket_anchors,
        CronTrigger(day_of_week="mon-fri", hour=9, minute="0-25/5", second=0, timezone=TIMEZONE),
        id="premarket_anchor_9",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_references,
        CronTrigger(day_of_week="tue-sat", hour=4, minute=5, second=0, timezone=TIMEZONE),
        id="us_close_reference_anchor",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=9, minute="30-59/5", second=0, timezone=TIMEZONE),
        id="quote_am_open",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=10, minute="*/5", second=0, timezone=TIMEZONE),
        id="quote_am",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=11, minute="0-30/5", second=0, timezone=TIMEZONE),
        id="quote_am_close",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour="13-14", minute="*/5", second=0, timezone=TIMEZONE),
        id="quote_pm",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=0, second=0, timezone=TIMEZONE),
        id="quote_pm_close",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_notices,
        CronTrigger(hour=18, minute=30, timezone=TIMEZONE),
        id="daily_notices",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quota,
        CronTrigger(hour=18, minute=30, timezone=TIMEZONE),
        id="daily_quota",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_daily_premiums,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=35, timezone=TIMEZONE),
        id="daily_premium_history",
        replace_existing=True,
    )
    scheduler.start()
    if run_startup_refresh:
        threading.Thread(target=_quiet_run, args=(service.refresh_notices,), daemon=True).start()
        threading.Thread(target=_quiet_run, args=(service.refresh_quota,), daemon=True).start()
    return scheduler


def _quiet_run(callback: object) -> None:
    try:
        callback()  # type: ignore[operator]
    except Exception:
        return
