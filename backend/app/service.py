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

import logging
from datetime import date, datetime, timezone

from app.db import Repository
from app.providers import DataProvider, FakeProvider
from app.strategy import SignalState, WeekBuffer, compute_levels, evaluate_signal

log = logging.getLogger(__name__)


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

            # One symbol's data can be missing (a corporate action drops it from
            # the day's bhavcopy, the scraper hiccups, etc.). Skip that symbol and
            # keep computing the rest — never let it kill the whole weekly run.
            try:
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
            except Exception as e:  # noqa: BLE001 - resilience over completeness
                log.warning("run_weekly: skipping %s (%s)", symbol, e)
        self.repo.set_meta("last_weekly_run_at", _now_iso())

    # --- live step: advance every stock's state machine by one tick ---
    def scan(self) -> None:
        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                continue  # weekly hasn't run yet (or this symbol was skipped)
            levels, week_id = loaded[0], loaded[1]
            # A live-price fetch can fail for one symbol; skip it this tick rather
            # than abort the whole scan.
            try:
                ltp = self.provider.get_live_price(symbol)
            except Exception as e:  # noqa: BLE001
                log.warning("scan: no price for %s (%s); skipping this tick", symbol, e)
                continue
            state, _ = self.repo.load_signal_state(symbol)
            new_state = evaluate_signal(levels, ltp, state)
            self.repo.save_signal_state(symbol, new_state, last_ltp=ltp)

            # If the stock just FIRED this tick (wasn't fired before, is now),
            # write it into the permanent track record.
            if new_state.fired_dir and not state.fired_dir:
                self._log_fired(symbol, new_state.fired_dir, levels, week_id)

            # Whether it fired now or earlier, see if price has since reached T3
            # for any still-open logged signal, and resolve it.
            self._resolve_open_logs(symbol, ltp)
        self.repo.set_meta("last_scan_at", _now_iso())

    def _log_fired(self, symbol: str, direction: str, levels, week_id: str) -> None:
        """Record a newly fired BUY/SELL with its target ladder. T1 is the entry."""
        if direction == "BUY":
            t1, t2, t3 = levels.buy_t1, levels.buy_t2, levels.buy_t3
        else:  # SELL
            t1, t2, t3 = levels.sell_t1, levels.sell_t2, levels.sell_t3
        self.repo.append_signal_log(symbol, direction, entry=t1, t1=t1, t2=t2, t3=t3, week_id=week_id)

    def _resolve_open_logs(self, symbol: str, ltp: float) -> None:
        """A BUY reaches its goal when price rises to T3; a SELL when price falls
        to T3. Mark any open log that the current price has satisfied."""
        for row in self.repo.get_open_signal_logs(symbol):
            hit = (row["signal"] == "BUY" and ltp >= row["t3"]) or \
                  (row["signal"] == "SELL" and ltp <= row["t3"])
            if hit:
                self.repo.resolve_signal_log(row["id"])

    # --- read step: the History page feed ---
    def build_history(self, limit: int = 100) -> list[dict]:
        rows: list[dict] = []
        for r in self.repo.load_history(limit):
            rows.append(
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "signal": r["signal"],
                    "entry": r["entry"],
                    "t1": r["t1"],
                    "t2": r["t2"],
                    "t3": r["t3"],
                    "weekId": r["week_id"],
                    "firedAt": r["fired_at"],
                    "hitT3": bool(r["hit_t3"]),
                    "resolvedAt": r["resolved_at"],
                }
            )
        return rows

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


def _make_provider() -> DataProvider:
    """Pick the data source from config. 'nse' = the real scraper; anything else
    (default) = the safe FakeProvider. If the real provider can't even be built,
    fall back to fake rather than crash the whole backend on boot."""
    from app import config

    if config.PROVIDER == "nse":
        try:
            from app.nse_provider import NSEProvider

            log.info("data provider: NSEProvider (real NSE data)")
            return NSEProvider()
        except Exception as e:  # noqa: BLE001
            log.error("NSEProvider unavailable (%s); falling back to FakeProvider", e)
    else:
        log.info("data provider: FakeProvider (demo data)")
    return FakeProvider()


# A single shared service instance, wired to the configured provider + default DB.
# Swapping fake <-> real NSE data is now just TRADING_PROVIDER=nse, no code change.
service = SignalService(_make_provider())
# Compute levels once (idempotent per week) so the API has data immediately.
service.run_weekly()
