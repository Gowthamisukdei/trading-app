"""
service.py — glue between the data provider, the strategy brain, and the database.

Responsibilities:
  1. Weekly: compute each stock's Levels, roll its 4-week buffer, flag "good
     invest", and persist all of it.
  2. Every scan: load each stock's saved state, advance its state machine on the
     live price, save the new state.
  3. On request: read saved state + levels and shape it for the dashboard.

State now lives in the DATABASE (via Repository), not memory — so everything
survives a backend restart. Note how the engine import (compute_levels /
evaluate_signal) is unchanged from before: only the storage swapped.
"""

from datetime import date, datetime, timezone

from app.db import Repository
from app.providers import DataProvider, FakeProvider
from app.strategy import SignalState, WeekBuffer, compute_levels, evaluate_signal


def current_week_id(today: date | None = None) -> str:
    """A stable id for "this trading week", e.g. 2026-W26. Drives idempotency:
    we compute a stock's weekly levels at most once per calendar week, so a
    restart (which re-runs run_weekly) does NOT re-roll the buffer or wipe state."""
    today = today or date.today()
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


def _avg_x(buffer: WeekBuffer) -> float | None:
    prevs = [buffer.prev1, buffer.prev2, buffer.prev3]
    if any(p is None for p in prevs):
        return None
    return round(sum(prevs) / 3, 2)


def _good_invest_from_buffer(buffer: WeekBuffer) -> bool:
    avg = _avg_x(buffer)
    if avg is None or avg == 0 or buffer.current is None:
        return False
    return buffer.current < avg / 2


class SignalService:
    def __init__(self, provider: DataProvider, repo: Repository | None = None):
        self.provider = provider
        self.repo = repo or Repository()

    # --- weekly step: compute levels, roll buffer (once/week), persist ---
    def run_weekly(self, force: bool = False) -> None:
        """Compute each stock's weekly levels and maintain its 4-week buffer.

        Three cases per stock:
          * Same week, not forced  -> skip entirely (this is what makes restarts
            safe: re-running doesn't re-roll the buffer or wipe armed/fired state).
          * NEW week                -> roll the buffer once, clear last week's
            state, save the fresh levels.
          * Same week, FORCED       -> recompute levels from the latest data and
            update the CURRENT week's range IN PLACE (no second roll, no state
            wipe). This is what the Wednesday job does when it refreshes with
            Wednesday's data.
        """
        week_id = current_week_id()
        for symbol in self.provider.get_fno_symbols():
            is_new_week = self.repo.get_levels_week_id(symbol) != week_id
            if not is_new_week and not force:
                continue

            mon, tue, wed = self.provider.get_week_ohlc(symbol)
            levels = compute_levels(mon, tue, wed)

            if is_new_week:
                buffer = self.repo.roll_buffer(symbol, levels.X)  # roll ONCE/week
                self.repo.save_signal_state(symbol, SignalState())  # clear state
            else:
                cur = self.repo.load_buffer(symbol)
                buffer = WeekBuffer(
                    current=levels.X, prev1=cur.prev1, prev2=cur.prev2, prev3=cur.prev3
                )
                self.repo.set_buffer(symbol, buffer)  # in-place, no roll

            self.repo.save_levels(
                symbol, week_id, levels,
                avg_x=_avg_x(buffer),
                good=_good_invest_from_buffer(buffer),
            )
        self.repo.set_meta("last_weekly_run_at", _now_iso())

    # --- live step: advance every stock's state machine by one tick ---
    def scan(self) -> None:
        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                continue  # weekly hasn't run yet
            levels = loaded[0]
            ltp = self.provider.get_live_price(symbol)
            state, _ = self.repo.load_signal_state(symbol)
            new_state = evaluate_signal(levels, ltp, state)
            self.repo.save_signal_state(symbol, new_state, last_ltp=ltp)
        self.repo.set_meta("last_scan_at", _now_iso())

    # --- read step: shape current state for the dashboard ---
    def build_signals(self) -> list[dict]:
        rows: list[dict] = []
        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                continue
            levels, week_id, avg_x, good = loaded
            state, last_ltp = self.repo.load_signal_state(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "status": state.status,
                    "ltp": last_ltp,
                    "monTueHigh": levels.mon_tue_high,
                    "monTueLow": levels.mon_tue_low,
                    "buyT1": levels.buy_t1,
                    "buyT2": levels.buy_t2,
                    "buyT3": levels.buy_t3,
                    "sellT1": levels.sell_t1,
                    "sellT2": levels.sell_t2,
                    "sellT3": levels.sell_t3,
                    "goodInvest": good,
                    "weekId": week_id,
                }
            )
        return rows

    def health(self) -> dict:
        return {
            "ok": True,
            "lastWeeklyRunAt": self.repo.get_meta("last_weekly_run_at"),
            "lastScanAt": self.repo.get_meta("last_scan_at"),
            "providerStatus": type(self.provider).__name__,
            "trackedSymbols": len(self.provider.get_fno_symbols()),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# A single shared service instance, wired to the FAKE provider + default DB file.
# Swapping to real data later = FakeProvider() -> NSEProvider(), one line.
service = SignalService(FakeProvider())
# Compute levels once (idempotent per week) so the API has data immediately.
service.run_weekly()
