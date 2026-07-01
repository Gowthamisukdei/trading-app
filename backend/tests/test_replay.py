"""
Tests for the daily High/Low replay — the backfill that rebuilds armed/fired
state from each day's bhavcopy candle, so a setup that happened on a day the live
scan wasn't running is never lost.

Two layers:
  1. evaluate_day(): the pure day-level state machine.
  2. SignalService.replay_days(): walks the days, merges without downgrading,
     logs a fire exactly once.

Runnable two ways:
  1. plain:   python -m tests.test_replay
  2. pytest:  pytest
"""

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Repository
from app.providers import DataProvider
from app.service import SignalService
from app.strategy import OHLC, ReverseState, SignalState, compute_levels, evaluate_day

# Real 360ONE levels (the regression row): an inside day. CONTINUATION model:
# arm on a box break, ENTER at the BUY/SELL LEVEL.
#   mon_tue_high 1217.9  mon_tue_low 1164.3   buy_level 1230.55  sell_level 1151.65
LV = compute_levels(
    OHLC(1179.8, 1188.0, 1165.2, 1170.6),
    OHLC(1164.4, 1217.9, 1164.3, 1191.6),
    OHLC(1198.0, 1198.0, 1170.2, 1190.0),
)


# ----------------------------- evaluate_day ------------------------------

def test_day_arms_buy_on_high_above_ceiling():
    # A day whose HIGH broke above the ceiling (but not yet the buy_level) arms BUY.
    s = evaluate_day(LV, high=1220.0, low=1200.0, state=SignalState())  # 1217.9 < 1220 < 1230.55
    assert s.status == "ARMED_BUY"


def test_day_arms_sell_on_low_below_floor():
    # Low below the floor (but above sell_level), high stays inside -> arm SELL.
    s = evaluate_day(LV, high=1200.0, low=1160.0, state=SignalState())  # 1151.65 < 1160 < 1164.3
    assert s.status == "ARMED_SELL"


def test_quiet_day_does_nothing():
    s = evaluate_day(LV, high=1200.0, low=1180.0, state=SignalState())
    assert s.status == "NONE"


def test_armed_buy_enters_when_day_high_reaches_level():
    armed = SignalState(armed_dir="BUY")
    fired = evaluate_day(LV, high=1231.0, low=1200.0, state=armed)  # 1231 >= buy_level 1230.55
    assert fired.status == "BUY"
    # Just shy of the level -> stays armed.
    still = evaluate_day(LV, high=1230.0, low=1200.0, state=armed)
    assert still.status == "ARMED_BUY"


def test_armed_sell_enters_when_day_low_reaches_level():
    armed = SignalState(armed_dir="SELL")
    fired = evaluate_day(LV, high=1200.0, low=1151.0, state=armed)  # 1151 <= sell_level 1151.65
    assert fired.status == "SELL"


def test_fired_is_terminal():
    done = SignalState(armed_dir="BUY", fired_dir="BUY")
    assert evaluate_day(LV, high=99999, low=0, state=done).status == "BUY"


# --------------------------- replay_days() -------------------------------

class _Stub(DataProvider):
    """A provider that serves canned daily candles by date, so we can drive
    replay_days() with no network."""

    def __init__(self, week_days, daily):
        self._week_days = week_days        # (mon, tue, wed) dates
        self._daily = daily                # {symbol: {date: OHLC}}

    def get_fno_symbols(self):
        return list(self._daily.keys())

    def get_daily_ohlc(self, symbol, day):
        try:
            return self._daily[symbol][day]
        except KeyError as e:
            raise RuntimeError(f"no data for {symbol} on {day}") from e

    def get_live_price(self, symbol):
        return 0.0

    def get_week_days(self):
        return self._week_days

    def has_daily_data(self, day):
        return any(day in days for days in self._daily.values())


def _service_with_levels(stub) -> SignalService:
    repo = Repository(Path(tempfile.mkdtemp()) / "replay.db")
    svc = SignalService(stub, repo=repo)
    # Pretend the weekly compute already saved 360ONE-style levels for every symbol.
    for sym in stub.get_fno_symbols():
        repo.save_levels(sym, "2026-W27", LV, avg_x=None, good=False)
    return svc


# A clean July week with no holidays: Wed 7/1, then Thu 7/2, Fri 7/3.
WED = date(2026, 7, 1)
THU = date(2026, 7, 2)
FRI = date(2026, 7, 3)
TODAY = date(2026, 7, 3)


def test_replay_backfills_trap_then_reverse_across_days():
    # REVERSE: AAA fakes DOWN to sell_t1 Thursday (trap), then snaps back UP to the
    # box high H Friday -> enter BUY.  sell_t1 = L - X/2 = 1156.3, H = 1198.0.
    stub = _Stub(
        (date(2026, 6, 29), date(2026, 6, 30), WED),
        {"AAA": {THU: OHLC(1180, 1185, 1150, 1160),    # low 1150 <= sell_t1 -> trap DOWN
                 FRI: OHLC(1190, 1205, 1188, 1200)}},  # high 1205 >= H 1198 -> reverse BUY
    )
    svc = _service_with_levels(stub)
    svc.replay_days(today=TODAY)

    state, _ = svc.repo.load_reverse_state("AAA")
    assert state.status == "BUY", state.status
    # And it must be written to the permanent track record exactly once.
    hist = svc.build_history()
    assert len(hist) == 1 and hist[0]["symbol"] == "AAA" and hist[0]["signal"] == "BUY"


def test_replay_is_idempotent_no_duplicate_log():
    stub = _Stub(
        (date(2026, 6, 29), date(2026, 6, 30), WED),
        {"AAA": {THU: OHLC(1180, 1185, 1150, 1160),
                 FRI: OHLC(1190, 1205, 1188, 1200)}},
    )
    svc = _service_with_levels(stub)
    svc.replay_days(today=TODAY)
    svc.replay_days(today=TODAY)  # run again
    assert len(svc.build_history()) == 1  # still ONE row, not two


def test_replay_only_traps_when_no_reverse_yet():
    # BBB fakes DOWN (trap) but never snaps back to H -> stays FAKED_DOWN, no history.
    stub = _Stub(
        (date(2026, 6, 29), date(2026, 6, 30), WED),
        {"BBB": {THU: OHLC(1180, 1185, 1150, 1160),    # low 1150 <= sell_t1 -> trap DOWN
                 FRI: OHLC(1180, 1190, 1175, 1185)}},  # high 1190 < H 1198 -> no reverse
    )
    svc = _service_with_levels(stub)
    svc.replay_days(today=TODAY)
    state, _ = svc.repo.load_reverse_state("BBB")
    assert state.status == "FAKED_DOWN"
    assert svc.build_history() == []


def test_replay_never_downgrades_live_state():
    # The live scan already trapped CCC today; replay sees no trap in the closed
    # days -> it must NOT reset CCC back to NONE.
    stub = _Stub(
        (date(2026, 6, 29), date(2026, 6, 30), WED),
        {"CCC": {THU: OHLC(1200, 1205, 1180, 1190),    # quiet, inside range (no trap)
                 FRI: OHLC(1195, 1205, 1185, 1200)}},
    )
    svc = _service_with_levels(stub)
    svc.repo.save_reverse_state("CCC", ReverseState(faked_dir="UP"), last_ltp=1213.0)
    svc.replay_days(today=TODAY)
    state, ltp = svc.repo.load_reverse_state("CCC")
    assert state.status == "FAKED_UP"     # preserved, not downgraded
    assert ltp == 1213.0                  # live price preserved too


def _run_standalone() -> int:
    checks = [
        ("day arms BUY on high>ceiling", test_day_arms_buy_on_high_above_ceiling),
        ("day arms SELL on low<floor", test_day_arms_sell_on_low_below_floor),
        ("quiet day does nothing", test_quiet_day_does_nothing),
        ("armed BUY enters at day high>=level", test_armed_buy_enters_when_day_high_reaches_level),
        ("armed SELL enters at day low<=level", test_armed_sell_enters_when_day_low_reaches_level),
        ("fired is terminal", test_fired_is_terminal),
        ("replay backfills trap->reverse across days", test_replay_backfills_trap_then_reverse_across_days),
        ("replay idempotent (no dup log)", test_replay_is_idempotent_no_duplicate_log),
        ("replay only traps when no reverse", test_replay_only_traps_when_no_reverse_yet),
        ("replay never downgrades live state", test_replay_never_downgrades_live_state),
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
