"""
Tests for the live signal engine (arm -> trigger) and the week-rolling buffer.

Like test_strategy.py, runnable two ways:
  1. plain:   python -m tests.test_signal_engine
  2. pytest:  pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy import (
    OHLC,
    SignalState,
    WeekBuffer,
    compute_levels,
    evaluate_signal,
    good_invest,
    roll_weeks,
    STATUS_NONE,
    STATUS_ARMED_BUY,
    STATUS_ARMED_SELL,
    STATUS_BUY,
    STATUS_SELL,
)

# Reuse the 360ONE levels so the trigger prices are real, sheet-verified numbers.
#   mon_tue_low = 1164.3   mon_tue_high = 1217.9
#   buy_t1 = 1211.9        sell_t1 = 1156.3
LEVELS = compute_levels(
    OHLC(1179.8, 1188.0, 1165.2, 1170.6),
    OHLC(1164.4, 1217.9, 1164.3, 1191.6),
    OHLC(1198.0, 1198.0, 1170.2, 1190.0),
)

# A NON-inside-day stock. Here Wednesday breaks out, so H = mon_tue_high and the
# BUY trigger sits ABOVE the ceiling — which leaves a price band where you can
# poke the ceiling WITHOUT firing. We need this to test the "no flip" rule.
#   mon_tue_high = 112   mon_tue_low = 90   X = 22
#   buy_t1 = 123 (above the ceiling 112)    sell_t1 = 79
NON_INSIDE = compute_levels(
    OHLC(100.0, 110.0, 90.0, 105.0),
    OHLC(104.0, 112.0, 95.0, 108.0),
    OHLC(109.0, 115.0, 100.0, 113.0),  # high 115 > 112 -> NOT an inside day
)


# Each case: (description, levels, sequence_of_prices, expected_final_status)
CASES = [
    (
        "quiet price inside the box -> stays NONE",
        LEVELS,
        [1190.0, 1200.0, 1185.0],
        STATUS_NONE,
    ),
    (
        "price fakes DOWN below floor -> ARMED_BUY",
        LEVELS,
        [1190.0, 1160.0],  # 1160 < 1164.3
        STATUS_ARMED_BUY,
    ),
    (
        "price fakes UP above ceiling -> ARMED_SELL",
        LEVELS,
        [1190.0, 1225.0],  # 1225 > 1217.9
        STATUS_ARMED_SELL,
    ),
    (
        "armed BUY then breaks up through buy_t1 -> BUY fires",
        LEVELS,
        [1160.0, 1190.0, 1212.0],  # arm down, recover, then >= 1211.9
        STATUS_BUY,
    ),
    (
        "armed SELL then breaks down through sell_t1 -> SELL fires",
        LEVELS,
        [1225.0, 1190.0, 1150.0],  # arm up, recover, then <= 1156.3
        STATUS_SELL,
    ),
    (
        "armed BUY but never reaches buy_t1 -> stays ARMED_BUY",
        LEVELS,
        [1160.0, 1190.0, 1205.0],  # 1205 < 1211.9
        STATUS_ARMED_BUY,
    ),
    (
        "once BUY fires it latches even if price falls back",
        LEVELS,
        [1160.0, 1212.0, 1100.0],  # fire BUY, then dump
        STATUS_BUY,
    ),
    (
        # Non-inside stock: buy_t1=123 sits above ceiling=112, so price can poke
        # the ceiling (113) without triggering. Armed BUY must NOT flip to SELL.
        "armed BUY does NOT flip to SELL when price pokes the ceiling",
        NON_INSIDE,
        [85.0, 113.0],  # 85<90 arms BUY; 113>112 ceiling but <123 buy_t1
        STATUS_ARMED_BUY,
    ),
]


def _replay(levels, prices):
    """Feed a price sequence through the engine, return the final state."""
    state = SignalState()
    for p in prices:
        state = evaluate_signal(levels, p, state)
    return state


def test_signal_engine_cases():
    for desc, levels, prices, expected in CASES:
        final = _replay(levels, prices)
        assert final.status == expected, f"{desc}: expected {expected}, got {final.status}"


def test_roll_weeks_shifts_right():
    buf = WeekBuffer(current=10.0, prev1=20.0, prev2=30.0, prev3=40.0)
    rolled = roll_weeks(buf, 5.0)
    assert (rolled.current, rolled.prev1, rolled.prev2, rolled.prev3) == (5.0, 10.0, 20.0, 30.0)


def test_good_invest_flags_compression():
    # prev avg = (20+20+20)/3 = 20; current 8 < 10 -> compressed -> True
    assert good_invest(WeekBuffer(current=8.0, prev1=20.0, prev2=20.0, prev3=20.0)) is True
    # current 15 is not < 10 -> False
    assert good_invest(WeekBuffer(current=15.0, prev1=20.0, prev2=20.0, prev3=20.0)) is False
    # not enough history -> False
    assert good_invest(WeekBuffer(current=8.0, prev1=20.0)) is False


def _run_standalone() -> int:
    all_ok = True
    print("Signal engine:")
    for desc, levels, prices, expected in CASES:
        final = _replay(levels, prices)
        ok = final.status == expected
        all_ok = all_ok and ok
        print(f"  [{'OK ' if ok else 'XX '}] {desc}\n        prices={prices} -> {final.status}")

    print("\nWeek buffer:")
    rolled = roll_weeks(WeekBuffer(10.0, 20.0, 30.0, 40.0), 5.0)
    ok = (rolled.current, rolled.prev1, rolled.prev2, rolled.prev3) == (5.0, 10.0, 20.0, 30.0)
    all_ok = all_ok and ok
    print(f"  [{'OK ' if ok else 'XX '}] roll_weeks shifts right -> {rolled}")

    gi = good_invest(WeekBuffer(8.0, 20.0, 20.0, 20.0))
    ok = gi is True
    all_ok = all_ok and ok
    print(f"  [{'OK ' if ok else 'XX '}] good_invest flags compression -> {gi}")

    print()
    if all_ok:
        print(">>> PASS — signal engine + buffer behave correctly.")
        return 0
    print(">>> FAIL — review the failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
