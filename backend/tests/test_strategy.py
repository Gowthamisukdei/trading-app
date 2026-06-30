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

# Box + entry numbers match the Excel exactly. The T1/T2/T3 targets are measured
# off the FULL box for the continuation play (box high/low +/- 1/2, 1, 2 x span),
# so they nest above the entry. On an inside day like 360ONE these intentionally
# differ from the Excel's tightened R/S/T columns (which belong to Play 2): the
# tightened ladder would sit below the entry, which is the bug we're fixing.
#   span = 1217.9 - 1164.3 = 53.6
EXPECTED = {
    "mon_tue_high": 1217.9,
    "mon_tue_low": 1164.3,
    "wed_inside": True,
    "H": 1198.0,
    "L": 1170.2,
    "X": 27.8,
    "buy_level": 1230.55,   # box high + 23.6% of the box span
    "sell_level": 1151.65,  # box low  - 23.6% of the box span
    "buy_t1": 1244.7,       # 1217.9 + 53.6/2
    "buy_t2": 1271.5,       # 1217.9 + 53.6
    "buy_t3": 1325.1,       # 1217.9 + 2*53.6
    "sell_t1": 1137.5,      # 1164.3 - 53.6/2
    "sell_t2": 1110.7,      # 1164.3 - 53.6
    "sell_t3": 1057.1,      # 1164.3 - 2*53.6
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
