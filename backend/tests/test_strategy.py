"""
Regression test for compute_levels().

This is THE test that proves our engine matches weeklyalgo.xlsx. The numbers
below were taken directly from the Excel for stock 360ONE on a Wednesday
inside-day. If compute_levels() ever stops producing exactly these numbers,
the trading brain is broken and nothing downstream can be trusted.

Runnable two ways:
  1. plain:   python -m tests.test_strategy      (no dependencies)
  2. pytest:  pytest                              (once pytest is installed)
"""

import sys
from pathlib import Path

# Let this file find the `app` package whether run via pytest or directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy import OHLC, compute_levels


# --- The golden fixture: 360ONE, from the Excel ---
MONDAY = OHLC(open=1179.8, high=1188.0, low=1165.2, close=1170.6)
TUESDAY = OHLC(open=1164.4, high=1217.9, low=1164.3, close=1191.6)
WEDNESDAY = OHLC(open=1198.0, high=1198.0, low=1170.2, close=1190.0)

# What the Excel says the answer must be.
EXPECTED = {
    "mon_tue_high": 1217.9,
    "mon_tue_low": 1164.3,
    "wed_inside": True,
    "H": 1198.0,
    "L": 1170.2,
    "X": 27.8,
    "buy_level": 1230.55,   # box high + 23.6% of the full Mon-Tue range
    "sell_level": 1151.65,  # box low  - 23.6% of the full Mon-Tue range
    "buy_t1": 1211.9,
    "buy_t2": 1225.8,
    "buy_t3": 1253.6,
    "sell_t1": 1156.3,
    "sell_t2": 1142.4,
    "sell_t3": 1114.6,
}


def test_360one_regression():
    """compute_levels must reproduce the 360ONE row from weeklyalgo.xlsx."""
    levels = compute_levels(MONDAY, TUESDAY, WEDNESDAY)
    for field, expected in EXPECTED.items():
        actual = getattr(levels, field)
        assert actual == expected, f"{field}: expected {expected}, got {actual}"


def _run_standalone() -> int:
    """Pretty PASS/FAIL output when run without pytest."""
    levels = compute_levels(MONDAY, TUESDAY, WEDNESDAY)
    print("Stock: 360ONE  (Wednesday inside-day)\n")
    all_ok = True
    for field, expected in EXPECTED.items():
        actual = getattr(levels, field)
        ok = actual == expected
        all_ok = all_ok and ok
        mark = "OK " if ok else "XX "
        print(f"  [{mark}] {field:<13} expected {expected!s:<8} got {actual}")
    print()
    if all_ok:
        print(">>> PASS — engine matches the Excel exactly.")
        return 0
    print(">>> FAIL — engine does NOT match the Excel. Fix before continuing.")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
