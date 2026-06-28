"""
scheduler.py — makes the backend run itself.

Two jobs, both in IST:
  * WEEKLY: every Wednesday 18:30 IST (after the EOD bhavcopy is published),
    recompute levels + roll the 4-week buffer. Skipped on holidays.
  * LIVE SCAN: every SCAN_MINUTES (default 15), but only acts while the market
    is open (09:15-15:30, trading days). Outside those hours it's a no-op.

The jobs only DECIDE when to call the SignalService; all the real work lives in
service.py and the strategy engine. APScheduler runs them on a background thread
inside the same process as the web server.

Dev mode (config.DEV_MODE) swaps the 5-minute gated scan for a fast timer that
ignores market hours, so we can watch the dashboard advance on demand.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import DEV_MODE, DEV_SCAN_SECONDS, SCAN_MINUTES, WEEKLY_HOUR, WEEKLY_MINUTE
from app.market_calendar import IST, is_market_open, is_trading_day, now_ist
from app.service import SignalService

log = logging.getLogger("scheduler")


def create_scheduler(service: SignalService) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=IST)

    def weekly_job() -> None:
        if not is_trading_day(now_ist().date()):
            log.info("weekly job: holiday, skipping")
            return
        # force=True: refresh this week's levels with the latest (Wednesday)
        # data. run_weekly only rolls the buffer once per week, so forcing here
        # is safe — it updates in place if the week was already computed.
        service.run_weekly(force=True)
        log.info("weekly job: levels recomputed")

    def scan_job() -> None:
        # In production, only scan during the live session. In dev, always scan.
        if not DEV_MODE and not is_market_open():
            return
        service.scan()

    # --- live scan job ---
    if DEV_MODE:
        sched.add_job(
            scan_job, IntervalTrigger(seconds=DEV_SCAN_SECONDS),
            id="scan", replace_existing=True, max_instances=1, coalesce=True,
        )
        log.warning("DEV MODE: scanning every %ss, ignoring market hours", DEV_SCAN_SECONDS)
    else:
        sched.add_job(
            scan_job, IntervalTrigger(minutes=SCAN_MINUTES),
            id="scan", replace_existing=True, max_instances=1, coalesce=True,
        )
        log.info("live scan every %s min during market hours", SCAN_MINUTES)

    # --- weekly compute job: Wednesday WEEKLY_HOUR:WEEKLY_MINUTE IST ---
    # Default 18:30 so Wednesday's end-of-day bhavcopy is already published.
    sched.add_job(
        weekly_job,
        CronTrigger(day_of_week="wed", hour=WEEKLY_HOUR, minute=WEEKLY_MINUTE, timezone=IST),
        id="weekly", replace_existing=True, max_instances=1, coalesce=True,
    )
    log.info("weekly compute scheduled for Wed %02d:%02d IST", WEEKLY_HOUR, WEEKLY_MINUTE)

    return sched
