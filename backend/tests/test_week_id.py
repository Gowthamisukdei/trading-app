"""
Tests for current_week_id — the TRADING CYCLE boundary that decides when weekly
levels roll and armed state clears.

The whole point: the cycle must flip on WEDNESDAY (when fresh levels are computed),
not on the ISO-calendar Monday. An armed setup from last week must keep the same id
through the weekend and into Mon/Tue, then get a NEW id on Wednesday.

Runnable two ways:
  1. plain:   python -m tests.test_week_id
  2. pytest:  pytest
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.service import current_week_id

# A known cycle: levels computed Wed 2026-06-24 are valid until the next Wednesday
# 2026-07-01. So Wed 06-24 .. Tue 06-30 must ALL share one id, and 07-01 flips.
WED = date(2026, 6, 24)
THU = date(2026, 6, 25)
FRI = date(2026, 6, 26)
SAT = date(2026, 6, 27)
SUN = date(2026, 6, 28)
MON = date(2026, 6, 29)
TUE = date(2026, 6, 30)
NEXT_WED = date(2026, 7, 1)


def test_cycle_is_constant_wed_through_tue():
    # Every day from Wed through the following Tue belongs to the SAME cycle.
    ids = {current_week_id(d) for d in (WED, THU, FRI, SAT, SUN, MON, TUE)}
    assert len(ids) == 1, f"expected one id Wed..Tue, got {ids}"


def test_cycle_flips_on_wednesday():
    # The id must change exactly on the next Wednesday (new levels generate).
    assert current_week_id(TUE) != current_week_id(NEXT_WED)
    # ...and NOT change on Monday (the old bug wiped state every Monday).
    assert current_week_id(SUN) == current_week_id(MON)


def test_armed_state_survives_the_weekend_into_monday():
    # Concretely what Gowtham wants: an arm on Friday keeps its cycle id on Monday,
    # so run_weekly does NOT treat Monday as a new week and clear it.
    assert current_week_id(FRI) == current_week_id(MON)


def _run_standalone() -> int:
    checks = [
        ("cycle constant Wed..Tue", test_cycle_is_constant_wed_through_tue),
        ("cycle flips on Wednesday, not Monday", test_cycle_flips_on_wednesday),
        ("armed state survives weekend into Monday", test_armed_state_survives_the_weekend_into_monday),
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
