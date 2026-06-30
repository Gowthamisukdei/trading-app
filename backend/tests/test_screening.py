"""
Tests for the extra SCREENING filters lifted from weeklyalgo.xlsx:
invest_tier, fib_levels, candle_quality, volatility_pct.

Where the Excel gives a checkable number for 360ONE we assert against it, the
same way test_strategy locks compute_levels. The rest are small unit checks.

Runnable two ways:
  1. plain:   python -m tests.test_screening
  2. pytest:  pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy import (
    OHLC,
    candle_quality,
    fib_levels,
    invest_tier,
    volatility_pct,
)

# Same golden 360ONE candles as test_strategy.
MONDAY = OHLC(open=1179.8, high=1188.0, low=1165.2, close=1170.6)
TUESDAY = OHLC(open=1164.4, high=1217.9, low=1164.3, close=1191.6)
WEDNESDAY = OHLC(open=1198.0, high=1198.0, low=1170.2, close=1190.0)

MON_TUE_HIGH = 1217.9
MON_TUE_LOW = 1164.3


def test_fib_levels_match_excel():
    # Excel cols N/V for 360ONE: BUY LEVEL 1230.55, SELL LEVEL 1151.65
    # (uses the FULL Mon-Tue range, not the inside-day X).
    fib_buy, fib_sell = fib_levels(MON_TUE_HIGH, MON_TUE_LOW)
    assert fib_buy == 1230.55, fib_buy
    assert fib_sell == 1151.65, fib_sell


def test_volatility_pct_match_excel():
    # Excel col Q (XFH): range / (price/100) = 53.6 / 12.179 = 4.40
    assert volatility_pct(MON_TUE_HIGH, MON_TUE_LOW) == 4.4


def test_candle_quality_match_excel():
    # Excel cols E/F: Monday body 9.2/22.8 = 0.40 -> Volatile;
    #                 Tuesday body 27.2/53.6 = 0.51 -> Good.
    assert candle_quality(MONDAY) == "Volatile"
    assert candle_quality(TUESDAY) == "Good"
    assert candle_quality(WEDNESDAY) == "Volatile"


def test_invest_tier():
    assert invest_tier(10, 30) == "good"      # 10 < 30/2
    assert invest_tier(20, 30) == "invest"    # 15 <= 20 < 30
    assert invest_tier(50, 30) == "breakout"  # 50 > 1.5*30
    assert invest_tier(35, 30) == "none"      # 30 <= 35 <= 45
    assert invest_tier(10, None) == "none"    # not enough history
    assert invest_tier(None, 30) == "none"


_CHECKS = [
    ("fib levels match Excel", test_fib_levels_match_excel),
    ("volatility % matches Excel", test_volatility_pct_match_excel),
    ("candle quality matches Excel", test_candle_quality_match_excel),
    ("invest tier classification", test_invest_tier),
]


def _run_standalone() -> int:
    all_ok = True
    for name, fn in _CHECKS:
        try:
            fn()
            print(f"  [OK ] {name}")
        except AssertionError as e:
            all_ok = False
            print(f"  [XX ] {name}: {e}")
    print()
    if all_ok:
        print(">>> PASS — screening filters match the Excel.")
        return 0
    print(">>> FAIL — screening filters do NOT match the Excel.")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
