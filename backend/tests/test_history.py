"""
Tests for the signal log / History feed (Step 8).

Covers the two things that make a track record trustworthy:
  1. A signal that FIRES gets recorded exactly once, with its target ladder.
  2. An open signal gets RESOLVED (hit_t3) only when price actually reaches T3.

Runnable two ways:
  1. plain:   python -m tests.test_history
  2. pytest:  pytest
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Repository
from app.providers import FakeProvider
from app.service import SignalService


def _temp_service() -> SignalService:
    tmp = Path(tempfile.mkdtemp()) / "history.db"
    return SignalService(FakeProvider(), repo=Repository(tmp))


def test_fired_signal_is_logged_once():
    """Driving 360ONE to a BUY must create exactly one signal_log row, with the
    BUY target ladder and still-open (hit_t3 False) status."""
    svc = _temp_service()
    svc.run_weekly()
    svc.scan()  # quiet
    svc.scan()  # armed
    svc.scan()  # fires BUY

    history = svc.build_history()
    buys = [h for h in history if h["symbol"] == "360ONE"]
    assert len(buys) == 1, f"expected one logged BUY, got {len(buys)}"
    row = buys[0]
    assert row["signal"] == "BUY"
    assert row["entry"] == 1230.55      # entry = the BUY LEVEL (23.6% breakout)
    assert row["t1"] == 1211.9          # T1 is the first target, not the entry
    assert row["t3"] == 1253.6          # the Excel-verified BUY T3
    assert row["hitT3"] is False        # 1231 never reached 1253.6
    assert row["resolvedAt"] is None

    # Scanning again must NOT duplicate the log (already fired, not a new fire).
    svc.scan()
    again = [h for h in svc.build_history() if h["symbol"] == "360ONE"]
    assert len(again) == 1, "fired signal was logged more than once"


def test_open_signal_resolves_when_price_hits_t3():
    """A BUY resolves only once price climbs to T3; a SELL only once price falls
    to T3. We append logs directly and push prices to prove both directions."""
    svc = _temp_service()
    repo = svc.repo

    # A BUY targeting T3 = 1253.6; price 1230 is not enough, 1260 is.
    repo.append_signal_log("AAA", "BUY", entry=1211.9, t1=1211.9, t2=1225.8, t3=1253.6, week_id="2026-W26")
    svc._resolve_open_logs("AAA", 1230.0)
    assert svc.build_history()[0]["hitT3"] is False, "BUY resolved too early"
    svc._resolve_open_logs("AAA", 1260.0)
    aaa = [h for h in svc.build_history() if h["symbol"] == "AAA"][0]
    assert aaa["hitT3"] is True and aaa["resolvedAt"] is not None, "BUY never resolved at T3"

    # A SELL targeting T3 = 1114.6; price 1150 is not enough, 1100 is.
    repo.append_signal_log("BBB", "SELL", entry=1156.3, t1=1156.3, t2=1142.4, t3=1114.6, week_id="2026-W26")
    svc._resolve_open_logs("BBB", 1150.0)
    bbb_open = [h for h in svc.build_history() if h["symbol"] == "BBB"][0]
    assert bbb_open["hitT3"] is False, "SELL resolved too early"
    svc._resolve_open_logs("BBB", 1100.0)
    bbb = [h for h in svc.build_history() if h["symbol"] == "BBB"][0]
    assert bbb["hitT3"] is True, "SELL never resolved at T3"

    # A resolved signal is no longer "open", so it won't be touched again.
    assert repo.get_open_signal_logs("AAA") == []
    assert repo.get_open_signal_logs("BBB") == []


def _run_standalone() -> int:
    checks = [
        ("fired signal is logged once", test_fired_signal_is_logged_once),
        ("open signal resolves at T3", test_open_signal_resolves_when_price_hits_t3),
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
        print(">>> PASS — signal log records and resolves correctly.")
        return 0
    print(">>> FAIL — review failures above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
