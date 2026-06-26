"""
Tests for market_calendar — the "is the NSE open?" logic the scheduler relies on.

Runnable two ways:
  1. plain:   python -m tests.test_market
  2. pytest:  pytest
"""

import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import market_calendar as mc
from app.market_calendar import IST, is_market_open, is_trading_day

# Derive real weekdays from an anchor so we never hard-code a wrong day:
# subtracting weekday() always lands on that week's Monday (weekday 0).
_ANCHOR = date(2026, 6, 22)
MONDAY = _ANCHOR - timedelta(days=_ANCHOR.weekday())
WEDNESDAY = MONDAY + timedelta(days=2)
SATURDAY = MONDAY + timedelta(days=5)
SUNDAY = MONDAY + timedelta(days=6)


def _ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)


def test_trading_day_weekends_and_holidays():
    assert is_trading_day(MONDAY) is True
    assert is_trading_day(SATURDAY) is False
    assert is_trading_day(SUNDAY) is False
    # A weekday added to the holiday set is no longer a trading day.
    mc.NSE_HOLIDAYS.add(WEDNESDAY)
    try:
        assert is_trading_day(WEDNESDAY) is False
    finally:
        mc.NSE_HOLIDAYS.discard(WEDNESDAY)
    assert is_trading_day(WEDNESDAY) is True  # restored


def test_market_open_hours():
    assert is_market_open(_ist(WEDNESDAY, time(10, 0))) is True
    assert is_market_open(_ist(WEDNESDAY, time(9, 15))) is True   # open boundary
    assert is_market_open(_ist(WEDNESDAY, time(15, 30))) is True  # close boundary
    assert is_market_open(_ist(WEDNESDAY, time(9, 14))) is False  # 1 min early
    assert is_market_open(_ist(WEDNESDAY, time(15, 31))) is False # 1 min late
    assert is_market_open(_ist(WEDNESDAY, time(3, 0))) is False   # pre-dawn


def test_market_closed_on_weekend():
    assert is_market_open(_ist(SATURDAY, time(10, 0))) is False


def test_timezone_is_converted_not_assumed():
    # 10:00 IST == 04:30 UTC. A UTC-stamped time must convert to IST first.
    utc_open = datetime(WEDNESDAY.year, WEDNESDAY.month, WEDNESDAY.day, 4, 30, tzinfo=ZoneInfo("UTC"))
    assert is_market_open(utc_open) is True
    # 09:00 UTC == 14:30 IST (still open); 11:00 UTC == 16:30 IST (closed).
    utc_closed = datetime(WEDNESDAY.year, WEDNESDAY.month, WEDNESDAY.day, 11, 0, tzinfo=ZoneInfo("UTC"))
    assert is_market_open(utc_closed) is False


def _run_standalone() -> int:
    checks = [
        ("trading day: weekends & holidays", test_trading_day_weekends_and_holidays),
        ("market open hours & boundaries", test_market_open_hours),
        ("market closed on weekend", test_market_closed_on_weekend),
        ("timezone converted, not assumed", test_timezone_is_converted_not_assumed),
    ]
    all_ok = True
    for name, fn in checks:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            all_ok = False
            print(f"  [XX ] {name}: {e}")
    print()
    print(">>> PASS" if all_ok else ">>> FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
