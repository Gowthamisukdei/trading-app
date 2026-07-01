"""
service.py — glue between the data provider, the strategy brain, and the database.

Responsibilities:
  1. Weekly: compute each stock's Levels, roll its 4-week buffer, flag "good
     invest", and persist all of it.
  2. Every scan: load each stock's saved state, advance its state machine on the
     live price, save the new state.
  3. On request: read saved state + levels and shape it for the dashboard.

State now lives in the DATABASE (via Repository), not memory — so everything
survives a backend restart.

LIVE PLAY = the REVERSE breakout (the founder's play): a fake to T1 on one side,
then a snap-back that enters at the OPPOSITE box edge. The old continuation engine
(evaluate_signal / evaluate_day) is retired and no longer wired in here.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

from app.db import Repository
from app.market_calendar import IST, is_trading_day
from app.providers import DataProvider, FakeProvider
from app.strategy import (
    OHLC,
    ReverseState,
    WeekBuffer,
    candle_quality,
    compute_levels,
    evaluate_day_reverse,
    evaluate_signal_reverse,
    invest_tier,
    reverse_targets,
    volatility_pct,
)

log = logging.getLogger(__name__)


def current_week_id(today: date | None = None) -> str:
    """A stable id for the current TRADING CYCLE, e.g. 2026-W26. Drives idempotency:
    weekly levels are computed (and the buffer rolled + state cleared) at most once
    per cycle, so a restart that re-runs run_weekly does NOT re-roll or wipe state.

    The cycle boundary is WEDNESDAY, not Monday. Fresh levels are computed after
    Wednesday's close, and an armed setup must stay valid from when it arms until
    the NEXT Wednesday (it can carry across the weekend into Mon/Tue). We get a
    Wednesday boundary by shifting the date back 2 days before taking the ISO week:
    that makes Wed..Tue share one id which flips each Wednesday — exactly when the
    weekly job rolls the buffer and clears state. (Keying on the plain ISO Monday
    boundary, as before, wiped armed state every Monday — two days too early.)

    Uses IST, not the server's UTC date, so the boundary lands on the right day on
    Railway (whose clock is UTC)."""
    if today is None:
        today = datetime.now(IST).date()
    year, week, _ = (today - timedelta(days=2)).isocalendar()
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
                # Keep the raw daily candles so the stock-detail page can show
                # exactly what Mon/Tue/Wed traded, not just the derived levels.
                self.repo.save_weekly_ohlc(symbol, week_id, mon, tue, wed)

                if is_new_week:
                    buffer = self.repo.roll_buffer(symbol, levels.X)  # roll ONCE/week
                    self.repo.save_reverse_state(symbol, ReverseState())  # clear state
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
        # Fold in every trading day that has closed since the levels' Wednesday, so
        # an arm/fire from a day we weren't live-scanning is recovered immediately.
        self.replay_days()

    # --- one-off: wipe stale state and rebuild with the current engine ---
    def rebuild_state(self) -> dict:
        """Full reset to the CURRENT engine: clear old state/history, RECOMPUTE the
        weekly levels (so a change to compute_levels — e.g. the box-based targets —
        is actually re-saved, not left stale in the DB), then rebuild state from the
        daily High/Low replay. Use once after an engine-logic change."""
        self.repo.clear_all_state()
        self.run_weekly(force=True)  # recompute + re-save levels, then replay rebuilds state
        log.info("rebuild_state: cleared state, recomputed levels, replayed")
        return {"rebuilt": True}

    # --- one-off: seed the buffer with real prior-week ranges ---
    def seed_history(self) -> dict:
        """Backfill prev1/prev2/prev3 of the rolling buffer with the REAL ranges of
        the 3 weeks before the current levels week, so the volatility tiers (Good
        invest / Invest / Breakout) work right now instead of after ~3 Wednesdays.

        One-off and self-correcting: the current slot is left exactly as run_weekly
        computed it; only history is filled. As real weeks roll in each Wednesday,
        these seeded ranges shift along and drop off naturally. Per-symbol failures
        are skipped, never aborting the whole backfill. Returns a small summary."""
        get_weeks = getattr(self.provider, "get_recent_week_ohlc", None)
        if get_weeks is None:
            log.info("seed_history: provider can't fetch history; nothing to do")
            return {"seeded": 0, "skipped": 0, "supported": False}

        seeded = skipped = 0
        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                skipped += 1
                continue
            levels, week_id = loaded[0], loaded[1]
            try:
                weeks = get_weeks(symbol, 4)  # newest first: current, prev1, prev2, prev3
            except Exception as e:  # noqa: BLE001 - one symbol shouldn't kill the run
                log.warning("seed_history: skipping %s (%s)", symbol, e)
                skipped += 1
                continue
            if len(weeks) < 4:
                skipped += 1
                continue

            xs = [compute_levels(m, t, w).X for (m, t, w) in weeks]
            cur = self.repo.load_buffer(symbol)
            buffer = WeekBuffer(
                current=cur.current if cur.current is not None else xs[0],
                prev1=xs[1], prev2=xs[2], prev3=xs[3],
            )
            self.repo.set_buffer(symbol, buffer)
            self.repo.save_levels(
                symbol, week_id, levels,
                avg_x=_avg_x(buffer), good=_good_invest_from_buffer(buffer),
            )
            seeded += 1

        self.repo.set_meta("last_seed_history_at", _now_iso())
        log.info("seed_history: seeded %d symbols, skipped %d", seeded, skipped)
        return {"seeded": seeded, "skipped": skipped, "supported": True}

    # --- daily backfill: rebuild state from each day's High/Low since Wednesday ---
    def replay_days(self, today: date | None = None) -> None:
        """Rebuild each stock's armed/fired state from the DAILY High/Low of every
        trading day since the levels' Wednesday.

        Why this exists: the live scan only sees the price at the instant it looks
        (every 15 min, and only once we're running). Anything that happened on a
        day we weren't watching — or that wicked through a level between scans — is
        invisible to it. The daily bhavcopy High/Low is the complete record, so
        replaying it is how we match the Excel and never miss a setup.

        Idempotent and restart-proof: state is recomputed from immutable bhavcopy,
        so re-running gives the same answer and a redeploy can't lose an arm. We
        never DOWNGRADE state the live scan already advanced today (we keep
        whichever is further along the machine), so today's intraday arm/fire isn't
        wiped by a replay that only covers up to the last completed day.
        """
        get_days = getattr(self.provider, "get_week_days", None)
        if get_days is None:
            return  # provider can't date the week (e.g. FakeProvider) -> nothing to do
        try:
            _, _, wed = get_days()
        except Exception as e:  # noqa: BLE001
            log.warning("replay: cannot resolve week days (%s); skipping", e)
            return

        if today is None:
            today = datetime.now(IST).date()
        days = [d for d in _daterange(wed + timedelta(days=1), today) if is_trading_day(d)]
        # Keep only days whose bhavcopy is actually published (skips today before the
        # close). Probing once per day here also caches each file, so the per-symbol
        # loop below reads from memory instead of re-downloading.
        has_data = getattr(self.provider, "has_daily_data", None)
        if has_data is not None:
            days = [d for d in days if has_data(d)]
        if not days:
            log.info("replay: no closed trading day since %s yet", wed)
            return

        rebuilt_n = fired_n = 0
        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                continue
            levels, week_id = loaded[0], loaded[1]

            rebuilt = ReverseState()
            saw_day = False
            for d in days:
                try:
                    ohlc = self.provider.get_daily_ohlc(symbol, d)
                except Exception:  # noqa: BLE001 - symbol missing from that day's file
                    continue
                saw_day = True
                rebuilt = evaluate_day_reverse(levels, ohlc.high, ohlc.low, rebuilt)
            if not saw_day:
                continue

            prev, prev_ltp = self.repo.load_reverse_state(symbol)
            # Never move backwards: keep whichever state is further along (FIRED >
            # TRAPPED > NONE), so a live intraday trap/fire from today survives.
            merged = rebuilt if _rank(rebuilt) >= _rank(prev) else prev
            self.repo.save_reverse_state(symbol, merged, last_ltp=prev_ltp)
            rebuilt_n += 1
            if merged.fired_dir and not prev.fired_dir:
                self._log_fired(symbol, merged.fired_dir, levels, week_id)
                fired_n += 1
        log.info("replay: rebuilt %d symbols over %d day(s); %d newly fired",
                 rebuilt_n, len(days), fired_n)
        self.repo.set_meta("last_replay_at", _now_iso())

    # --- live step: advance every stock's state machine by one tick ---
    def scan(self) -> None:
        from app.config import BULK_LIVE, SCAN_THROTTLE_MS

        throttle = SCAN_THROTTLE_MS / 1000.0  # seconds between live-price calls

        # Bulk path: ONE call fetches every stock's live price up front. If it's
        # enabled and works, we read prices from this dict in the loop (no per-
        # symbol calls, no throttle). If it fails, bulk stays None and we fall
        # back to the per-symbol get_live_price below — the scan never aborts.
        bulk: dict[str, float] | None = None
        if BULK_LIVE and hasattr(self.provider, "get_all_live_prices"):
            try:
                bulk = self.provider.get_all_live_prices()
                log.info("scan: bulk live prices for %d symbols in one call", len(bulk))
            except Exception as e:  # noqa: BLE001
                log.warning("scan: bulk live-price fetch failed (%s); per-symbol fallback", e)

        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                continue  # weekly hasn't run yet (or this symbol was skipped)
            levels, week_id = loaded[0], loaded[1]
            # A live-price fetch can fail for one symbol; skip it this tick rather
            # than abort the whole scan.
            try:
                if bulk is not None and symbol in bulk:
                    ltp = bulk[symbol]
                else:
                    ltp = self.provider.get_live_price(symbol)
                    # Trickle per-symbol calls so all ~211 stocks don't burst NSE.
                    if throttle:
                        time.sleep(throttle)
            except Exception as e:  # noqa: BLE001
                log.warning("scan: no price for %s (%s); skipping this tick", symbol, e)
                continue
            state, _ = self.repo.load_reverse_state(symbol)
            new_state = evaluate_signal_reverse(levels, ltp, state)
            self.repo.save_reverse_state(symbol, new_state, last_ltp=ltp)

            # If the stock just FIRED this tick (wasn't fired before, is now),
            # write it into the permanent track record.
            if new_state.fired_dir and not state.fired_dir:
                self._log_fired(symbol, new_state.fired_dir, levels, week_id)

            # Whether it fired now or earlier, see if price has since reached T3
            # for any still-open logged signal, and resolve it.
            self._resolve_open_logs(symbol, ltp)
        self.repo.set_meta("last_scan_at", _now_iso())

    def _log_fired(self, symbol: str, direction: str, levels, week_id: str) -> None:
        """Record a newly fired REVERSE BUY/SELL. The ENTRY is the OPPOSITE box edge
        the reversal exits through (H for a BUY, L for a SELL); T1/T2/T3 are the
        compressed inside-day profit targets that ladder out from there."""
        entry, t1, t2, t3 = reverse_targets(levels, direction)
        self.repo.append_signal_log(symbol, direction, entry=entry, t1=t1, t2=t2, t3=t3, week_id=week_id)

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
        # All candles up front (one query) so we can grade each day's quality
        # without a per-symbol read.
        all_candles = self.repo.load_all_weekly_ohlc()
        rows: list[dict] = []
        for symbol in self.provider.get_fno_symbols():
            loaded = self.repo.load_levels(symbol)
            if loaded is None:
                continue
            levels, week_id, avg_x, good = loaded
            state, last_ltp = self.repo.load_reverse_state(symbol)
            # REVERSE ladders: entry = the opposite box edge, targets = compressed
            # inside-day T1/T2/T3 (H/L/X). A BUY reverses up off the box low; a SELL
            # reverses down off the box high.
            buy_entry, buy_t1, buy_t2, buy_t3 = reverse_targets(levels, "BUY")
            sell_entry, sell_t1, sell_t2, sell_t3 = reverse_targets(levels, "SELL")
            rows.append(
                {
                    "symbol": symbol,
                    "status": state.status,
                    "ltp": last_ltp,
                    "monTueHigh": levels.mon_tue_high,
                    "monTueLow": levels.mon_tue_low,
                    "buyEntry": buy_entry,
                    "buyT1": buy_t1,
                    "buyT2": buy_t2,
                    "buyT3": buy_t3,
                    "sellEntry": sell_entry,
                    "sellT1": sell_t1,
                    "sellT2": sell_t2,
                    "sellT3": sell_t3,
                    "goodInvest": good,
                    # --- Excel screening extras ---
                    "quality": invest_tier(levels.X, avg_x),  # good|invest|breakout|none
                    "volPct": volatility_pct(levels.mon_tue_high, levels.mon_tue_low),
                    "candles": _candle_grades(all_candles.get(symbol)),
                    "weekId": week_id,
                }
            )
        return rows

    def build_stock_detail(self, symbol: str) -> dict | None:
        """Everything for one stock's detail page: the raw Mon/Tue/Wed candles,
        the combined Mon-Tue high/low, the levels/ladders, and current status.
        Returns None if we have no data for this symbol yet."""
        symbol = symbol.upper()
        loaded = self.repo.load_levels(symbol)
        candles = self.repo.load_weekly_ohlc(symbol)
        if loaded is None or candles is None:
            return None
        levels, week_id, avg_x, good = loaded
        state, last_ltp = self.repo.load_reverse_state(symbol)
        buy_entry, buy_t1, buy_t2, buy_t3 = reverse_targets(levels, "BUY")
        sell_entry, sell_t1, sell_t2, sell_t3 = reverse_targets(levels, "SELL")

        def day(o):
            return {"open": o.open, "high": o.high, "low": o.low, "close": o.close}

        return {
            "symbol": symbol,
            "weekId": week_id,
            "status": state.status,
            "ltp": last_ltp,
            "days": {
                "mon": day(candles["mon"]),
                "tue": day(candles["tue"]),
                "wed": day(candles["wed"]),
            },
            "monTueHigh": levels.mon_tue_high,
            "monTueLow": levels.mon_tue_low,
            "wedInside": levels.wed_inside,
            "H": levels.H,
            "L": levels.L,
            "X": levels.X,
            # REVERSE ladders (compressed inside-day levels): entry = opposite box edge.
            "buyEntry": buy_entry, "buyT1": buy_t1, "buyT2": buy_t2, "buyT3": buy_t3,
            "sellEntry": sell_entry, "sellT1": sell_t1, "sellT2": sell_t2, "sellT3": sell_t3,
            "goodInvest": good,
            # --- Excel screening extras ---
            "quality": invest_tier(levels.X, avg_x),
            "avgX": avg_x,
            "volPct": volatility_pct(levels.mon_tue_high, levels.mon_tue_low),
            "candles": _candle_grades(candles),
        }

    def health(self) -> dict:
        return {
            "ok": True,
            "lastWeeklyRunAt": self.repo.get_meta("last_weekly_run_at"),
            "lastScanAt": self.repo.get_meta("last_scan_at"),
            "lastReplayAt": self.repo.get_meta("last_replay_at"),
            "providerStatus": type(self.provider).__name__,
            "trackedSymbols": len(self.provider.get_fno_symbols()),
        }


def _candle_grades(candles: dict[str, OHLC] | None) -> dict[str, str] | None:
    """Grade Mon/Tue/Wed candles 'Good'/'Volatile' for the UI, or None if we have
    no candles for this symbol yet."""
    if not candles:
        return None
    return {d: candle_quality(candles[d]) for d in ("mon", "tue", "wed")}


def _rank(state: ReverseState) -> int:
    """How far along the reverse state machine a state is: FIRED(2) > TRAPPED(1) >
    NONE(0). The replay uses this to merge without downgrading live-scan progress."""
    if state.fired_dir:
        return 2
    if state.faked_dir:
        return 1
    return 0


def _daterange(start: date, end: date):
    """Yield every calendar date from start to end inclusive."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


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
# NOTE: we deliberately do NOT run the weekly compute here at import time. With the
# real NSE provider and all ~211 F&O stocks, computing every stock's levels takes
# long enough that it would block the web server from binding its port on boot —
# Railway then thinks the app is dead and restarts it, causing a 502 crash loop.
# Instead main.py kicks off the initial run_weekly() in a BACKGROUND thread inside
# the app lifespan, so the server answers immediately and levels fill in shortly.
