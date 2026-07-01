"""
Tests for Play 2 — the REVERSE breakout state machine (evaluate_day_reverse).

The founder's failed-breakout reversal: the first move runs all the way to T1 on
one side (the trap), then fails and snaps back through the box; we enter as it
exits the OPPOSITE box edge. The trap must come FIRST and the entry AFTER — that
ordering is the whole edge, so these tests pin it down. Levels are the Excel's
tightened box (H, L) and tightened T1 = H +/- X/2.

Runnable two ways:
  1. plain:   python -m tests.test_reverse
  2. pytest:  pytest
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategy import OHLC, ReverseState, compute_levels, evaluate_day_reverse

# Real 360ONE levels (the regression row), an inside day. Tightened box + T1:
#   H 1198.0  L 1170.2  X 27.8   buy_t1 = 1211.9   sell_t1 = 1156.3
LV = compute_levels(
    OHLC(1179.8, 1188.0, 1165.2, 1170.6),
    OHLC(1164.4, 1217.9, 1164.3, 1191.6),
    OHLC(1198.0, 1198.0, 1170.2, 1190.0),
)


def test_first_move_up_to_t1_sets_up_trap():
    # A day whose HIGH tags buy_t1 (1211.9) springs the UP trap (a SELL is coming).
    s = evaluate_day_reverse(LV, high=1212.0, low=1200.0, state=ReverseState())
    assert s.status == "FAKED_UP"


def test_first_move_down_to_t1_sets_down_trap():
    # A day whose LOW tags sell_t1 (1156.3) springs the DOWN trap (a BUY is coming).
    s = evaluate_day_reverse(LV, high=1180.0, low=1156.0, state=ReverseState())
    assert s.status == "FAKED_DOWN"


def test_breaking_the_box_but_not_t1_sets_no_trap():
    # Poking above the box high (1198) but short of buy_t1 (1211.9) is NOT the trap.
    s = evaluate_day_reverse(LV, high=1205.0, low=1190.0, state=ReverseState())
    assert s.status == "NONE"


def test_quiet_day_sets_no_trap():
    s = evaluate_day_reverse(LV, high=1195.0, low=1175.0, state=ReverseState())
    assert s.status == "NONE"


def test_up_trap_then_reverse_to_box_low_fires_sell():
    trapped = ReverseState(faked_dir="UP")
    fired = evaluate_day_reverse(LV, high=1200.0, low=1170.0, state=trapped)  # low <= L 1170.2
    assert fired.status == "SELL"
    # A reversal that stalls above the box low stays trapped, no fire.
    still = evaluate_day_reverse(LV, high=1200.0, low=1175.0, state=trapped)
    assert still.status == "FAKED_UP"


def test_down_trap_then_reverse_to_box_high_fires_buy():
    trapped = ReverseState(faked_dir="DOWN")
    fired = evaluate_day_reverse(LV, high=1198.0, low=1175.0, state=trapped)  # high >= H 1198
    assert fired.status == "BUY"


def test_reverse_never_fires_same_call_as_trap():
    # A single wild outside day tags BOTH T1s. We can't know the intraday order from
    # daily data, so it only springs the trap this call — never fires yet.
    s = evaluate_day_reverse(LV, high=1215.0, low=1150.0, state=ReverseState())
    assert s.status in ("FAKED_DOWN", "FAKED_UP")
    assert s.fired_dir is None


def test_fired_is_terminal():
    done = ReverseState(faked_dir="UP", fired_dir="SELL")
    assert evaluate_day_reverse(LV, high=99999, low=0, state=done).status == "SELL"


def _run_standalone() -> int:
    checks = [
        ("first move UP to T1 sets UP trap", test_first_move_up_to_t1_sets_up_trap),
        ("first move DOWN to T1 sets DOWN trap", test_first_move_down_to_t1_sets_down_trap),
        ("box break short of T1 sets no trap", test_breaking_the_box_but_not_t1_sets_no_trap),
        ("quiet day sets no trap", test_quiet_day_sets_no_trap),
        ("UP trap -> reverse to box low fires SELL", test_up_trap_then_reverse_to_box_low_fires_sell),
        ("DOWN trap -> reverse to box high fires BUY", test_down_trap_then_reverse_to_box_high_fires_buy),
        ("reverse never fires same call as trap", test_reverse_never_fires_same_call_as_trap),
        ("fired is terminal", test_fired_is_terminal),
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
