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

# Reuse the 360ONE levels so the prices are real, sheet-verified numbers.
# CONTINUATION model: arm on a box break, ENTER at the BUY/SELL LEVEL.
#   mon_tue_low = 1164.3   mon_tue_high = 1217.9
#   buy_level = 1230.55    sell_level = 1151.65   (T1/T2/T3 are now just targets)
LEVELS = compute_levels(
    OHLC(1179.8, 1188.0, 1165.2, 1170.6),
    OHLC(1164.4, 1217.9, 1164.3, 1191.6),
    OHLC(1198.0, 1198.0, 1170.2, 1190.0),
)

# A NON-inside-day stock — gives a band between the ceiling and the buy_level where
# price has broken the box but NOT yet confirmed, to test arm-without-entry + no-flip.
#   mon_tue_high = 112   mon_tue_low = 90   X = 22
#   buy_level = 117.19   sell_level = 84.81   (buy_t1 = 123 is a TARGET)
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
        "price breaks UP above the ceiling -> ARMED_BUY",
        LEVELS,
        [1190.0, 1220.0],  # 1217.9 < 1220 < 1230.55 (broke box, not yet confirmed)
        STATUS_ARMED_BUY,
    ),
    (
        "price breaks DOWN below the floor -> ARMED_SELL",
        LEVELS,
        [1190.0, 1160.0],  # 1151.65 < 1160 < 1164.3
        STATUS_ARMED_SELL,
    ),
    (
        "armed BUY then clears buy_level -> BUY enters",
        LEVELS,
        [1220.0, 1231.0],  # arm up, then >= 1230.55
        STATUS_BUY,
    ),
    (
        "armed SELL then clears sell_level -> SELL enters",
        LEVELS,
        [1160.0, 1150.0],  # arm down, then <= 1151.65
        STATUS_SELL,
    ),
    (
        "price gaps straight through buy_level -> BUY enters directly",
        LEVELS,
        [1231.0],  # 1231 >= 1230.55, no prior arm needed
        STATUS_BUY,
    ),
    (
        "armed BUY but never reaches buy_level -> stays ARMED_BUY",
        LEVELS,
        [1220.0, 1225.0],  # 1225 < 1230.55
        STATUS_ARMED_BUY,
    ),
    (
        "once BUY enters it latches even if price falls back",
        LEVELS,
        [1231.0, 1100.0],  # enter BUY, then dump
        STATUS_BUY,
    ),
    (
        # buy_level=117.19; price pokes ceiling (113) then dips below floor — armed
        # BUY must NOT flip to SELL (the setup stays valid until next Wed).
        "armed BUY does NOT flip to SELL when price pokes the other way",
        NON_INSIDE,
        [113.0, 88.0],  # 113>112 arms BUY; 88<90 floor but still > sell_level
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
