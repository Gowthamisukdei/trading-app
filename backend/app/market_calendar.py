"""
market_calendar.py — "is the NSE open right now?" as pure, testable functions.

The scheduler asks these before doing anything: don't scan when the market is
closed, don't compute weekly levels on a holiday. Everything is in IST
(Asia/Kolkata) because that's the market's timezone — never the server's local
time, which on Railway/Vercel could be anywhere in the world.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE equity trading session.
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)

# NSE trading holidays (market fully closed). The market is also closed every
# Saturday and Sunday — that's handled by the weekday check, so only put actual
# weekday holidays here.
#
# 🚨 MUST be kept current each year from the official NSE holiday calendar
# (nseindia.com). An out-of-date list isn't catastrophic — on a missed holiday
# we'd just scan and find stale/empty data — but the weekly compute could land
# on the wrong day. Update this set every year.
NSE_HOLIDAYS: set[date] = set()


def now_ist() -> datetime:
    """The current moment, in IST. The scheduler and jobs use this, never
    datetime.now() without a timezone."""
    return datetime.now(IST)


def is_trading_day(d: date) -> bool:
    """True on a weekday that isn't an NSE holiday."""
    if d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return False
    return d not in NSE_HOLIDAYS


def is_market_open(dt: datetime | None = None) -> bool:
    """True if the NSE equity session is live at the given moment (default now).
    A naive datetime is assumed to already be IST wall-clock time."""
    dt = dt or now_ist()
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
    if not is_trading_day(dt.date()):
        return False
    return MARKET_OPEN <= dt.time() <= MARKET_CLOSE
