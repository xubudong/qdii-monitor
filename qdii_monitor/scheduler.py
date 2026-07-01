from __future__ import annotations

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .service import MonitorService
from .settings import PREMARKET_REFRESH_MINUTES, QUOTE_REFRESH_MINUTES, TIMEZONE, US_CLOSE_REFRESH_TIMES


def create_scheduler(service: MonitorService, run_startup_refresh: bool = True) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    quote_step = _minute_step(QUOTE_REFRESH_MINUTES)
    premarket_step = _minute_step(PREMARKET_REFRESH_MINUTES)
    scheduler.add_job(
        service.refresh_premarket_anchors,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=f"*/{premarket_step}", second=0, timezone=TIMEZONE),
        id="premarket_anchor_8",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_premarket_anchors,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=f"0-25/{premarket_step}", second=0, timezone=TIMEZONE),
        id="premarket_anchor_9",
        replace_existing=True,
    )
    for index, (hour, minute) in enumerate(_parse_clock_times(US_CLOSE_REFRESH_TIMES), start=1):
        scheduler.add_job(
            service.refresh_references,
            CronTrigger(day_of_week="tue-sat", hour=hour, minute=minute, second=0, timezone=TIMEZONE),
            id=f"us_close_reference_anchor_{index}",
            replace_existing=True,
        )
        # 美股收盘附近同时保存一份完整行情快照，便于后续核对官方 NAV。
        scheduler.add_job(
            service.refresh_quotes,
            CronTrigger(day_of_week="tue-sat", hour=hour, minute=minute, second=30, timezone=TIMEZONE),
            id=f"us_close_quote_snapshot_{index}",
            replace_existing=True,
        )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=f"30-59/{quote_step}", second=0, timezone=TIMEZONE),
        id="quote_am_open",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=10, minute=f"*/{quote_step}", second=0, timezone=TIMEZONE),
        id="quote_am",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=11, minute=f"0-30/{quote_step}", second=0, timezone=TIMEZONE),
        id="quote_am_close",
        replace_existing=True,
    )
    scheduler.add_job(
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour="13-14", minute=f"*/{quote_step}", second=0, timezone=TIMEZONE),
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
        service.refresh_quotes,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=5, second=0, timezone=TIMEZONE),
        id="quote_pm_close_confirm",
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


def _minute_step(value: int) -> int:
    return min(max(int(value), 1), 59)


def _parse_clock_times(values: tuple[str, ...]) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for value in values:
        hour_text, separator, minute_text = value.partition(":")
        if separator != ":":
            continue
        try:
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            result.append((hour, minute))
    return result or [(4, 5), (5, 5)]
