"""
Tests for the database layer (Repository) and weekly rolling persistence.

Uses a throwaway temp DB file so it never touches the real data/signals.db.

Runnable two ways:
  1. plain:   python -m tests.test_db
  2. pytest:  pytest
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Repository
from app.providers import FakeProvider
from app.service import SignalService
from app.strategy import OHLC, SignalState


def _temp_repo() -> Repository:
    tmp = Path(tempfile.mkdtemp()) / "test.db"
    return Repository(tmp)


def test_signal_state_round_trip():
    """A saved BUY state must come back as BUY (this is the restart guarantee)."""
    repo = _temp_repo()
    repo.save_signal_state("ACME", SignalState(armed_dir="BUY", fired_dir="BUY"), last_ltp=101.5)
    state, ltp = repo.load_signal_state("ACME")
    assert state.status == "BUY"
    assert ltp == 101.5
    # Unknown symbol returns a clean empty state, not a crash.
    empty, none_ltp = repo.load_signal_state("NOPE")
    assert empty.status == "NONE" and none_ltp is None


def test_roll_buffer_shifts_across_weeks():
    """Four successive weekly ranges should walk through the 4 slots in order."""
    repo = _temp_repo()
    repo.roll_buffer("ACME", 40.0)   # week 1
    repo.roll_buffer("ACME", 30.0)   # week 2
    repo.roll_buffer("ACME", 20.0)   # week 3
    buf = repo.roll_buffer("ACME", 10.0)  # week 4
    assert (buf.current, buf.prev1, buf.prev2, buf.prev3) == (10.0, 20.0, 30.0, 40.0)
    # Reload from disk proves it persisted, not just held in memory.
    reloaded = repo.load_buffer("ACME")
    assert (reloaded.current, reloaded.prev1, reloaded.prev2, reloaded.prev3) == (10.0, 20.0, 30.0, 40.0)


def test_weekly_idempotent_then_force():
    """run_weekly twice in the same week must NOT double-roll the buffer;
    force=True must recompute."""
    repo = _temp_repo()
    svc = SignalService(FakeProvider(), repo=repo)
    svc.run_weekly()
    buf_after_first = repo.load_buffer("360ONE")
    svc.run_weekly()  # same week, no force -> skip
    buf_after_second = repo.load_buffer("360ONE")
    assert (buf_after_first.current, buf_after_first.prev1) == (
        buf_after_second.current, buf_after_second.prev1
    ), "buffer was re-rolled on a same-week run_weekly"


def test_forced_recompute_same_week_keeps_state_and_buffer():
    """The Wednesday job calls run_weekly(force=True). On the SAME week that must
    refresh levels WITHOUT rolling the buffer again or wiping a fired signal."""
    repo = _temp_repo()
    svc = SignalService(FakeProvider(), repo=repo)
    svc.run_weekly()              # week computed, buffer rolled once
    svc.scan(); svc.scan(); svc.scan()  # drive 360ONE to BUY
    before_buf = repo.load_buffer("360ONE")
    before_state, _ = repo.load_reverse_state("360ONE")
    assert before_state.status == "BUY"

    svc.run_weekly(force=True)    # same week, forced
    after_buf = repo.load_buffer("360ONE")
    after_state, _ = repo.load_reverse_state("360ONE")

    # Buffer not re-rolled: current stays, prev slots unchanged.
    assert (after_buf.current, after_buf.prev1, after_buf.prev2, after_buf.prev3) == (
        before_buf.current, before_buf.prev1, before_buf.prev2, before_buf.prev3
    ), "forced same-week recompute re-rolled the buffer"
    # Fired BUY survives a forced recompute (not a new week).
    assert after_state.status == "BUY", "forced recompute wiped the fired state"


def test_weekly_ohlc_round_trip():
    """The raw Mon/Tue/Wed candles must save and reload intact (this is what the
    stock-detail page reads)."""
    repo = _temp_repo()
    mon = OHLC(10.0, 12.0, 9.0, 11.0)
    tue = OHLC(11.0, 13.0, 10.5, 12.5)
    wed = OHLC(12.0, 12.5, 11.5, 12.0)
    repo.save_weekly_ohlc("ACME", "2026-W26", mon, tue, wed)
    got = repo.load_weekly_ohlc("ACME")
    assert got is not None
    assert (got["mon"].open, got["mon"].high, got["mon"].low, got["mon"].close) == (10.0, 12.0, 9.0, 11.0)
    assert got["wed"].close == 12.0
    assert repo.load_weekly_ohlc("NOPE") is None


def test_state_survives_simulated_restart():
    """Scan to a BUY, then build a BRAND NEW service on the SAME db file
    (simulating a process restart). The BUY must still be there."""
    repo_path = Path(tempfile.mkdtemp()) / "restart.db"

    svc1 = SignalService(FakeProvider(), repo=Repository(repo_path))
    svc1.run_weekly()
    svc1.scan()  # quiet
    svc1.scan()  # arm
    svc1.scan()  # fire
    before = {r["symbol"]: r["status"] for r in svc1.build_signals()}
    assert before["360ONE"] == "BUY" and before["RELIANCE"] == "SELL"

    # New service, new provider, SAME database file = a fresh process.
    svc2 = SignalService(FakeProvider(), repo=Repository(repo_path))
    svc2.run_weekly()  # idempotent: must NOT wipe state
    after = {r["symbol"]: r["status"] for r in svc2.build_signals()}
    assert after["360ONE"] == "BUY", f"state lost on restart: {after}"
    assert after["RELIANCE"] == "SELL", f"state lost on restart: {after}"


def _run_standalone() -> int:
    checks = [
        ("signal state round-trip", test_signal_state_round_trip),
        ("roll buffer shifts across weeks", test_roll_buffer_shifts_across_weeks),
        ("weekly is idempotent in same week", test_weekly_idempotent_then_force),
        ("forced recompute keeps state & buffer", test_forced_recompute_same_week_keeps_state_and_buffer),
        ("weekly ohlc round-trip", test_weekly_ohlc_round_trip),
        ("state survives a restart", test_state_survives_simulated_restart),
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
    if all_ok:
        print(">>> PASS — database layer persists state correctly.")
        return 0
    print(">>> FAIL — review failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
