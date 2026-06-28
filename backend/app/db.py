"""
db.py — the ONLY file that talks to the database.

Everything the app needs to remember between restarts lives here: each stock's
signal state (armed/fired), its weekly levels, and its 4-week range buffer.

We use SQLite: a single file on disk (data/signals.db), built into Python, no
server or account needed. Because every read/write goes through this one
Repository class, switching to Supabase Postgres later means rewriting THIS FILE
only — service.py, the engine, and the web layer never change.

Why a database at all: state must survive a backend restart. If the server
crashes mid-week, a stock that had already fired a BUY must still say BUY when
the server comes back — not silently reset to NONE.
"""

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.strategy import Levels, OHLC, SignalState, WeekBuffer

# Where the SQLite file lives. Locally this is backend/data/signals.db. On Railway
# we set TRADING_DB_PATH to a path on the mounted persistent VOLUME (e.g.
# /data/signals.db) so the database survives redeploys — Railway's normal disk is
# wiped on every deploy, which would erase all signal state.
_DEFAULT_DB = Path(
    os.getenv("TRADING_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "signals.db"))
)


class Repository:
    def __init__(self, db_path: Path | str = _DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per operation keeps things thread-safe under the
        # web server. SQLite's own file locking handles concurrent access at our
        # scale (a few hundred stocks, a scan every 5 minutes).
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS signal_state (
                    symbol     TEXT PRIMARY KEY,
                    armed_dir  TEXT,
                    armed_at   TEXT,
                    fired_dir  TEXT,
                    fired_at   TEXT,
                    last_ltp   REAL,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS levels (
                    symbol       TEXT PRIMARY KEY,
                    week_id      TEXT,
                    mon_tue_high REAL, mon_tue_low REAL, wed_inside INTEGER,
                    h REAL, l REAL, x REAL,
                    buy_t1 REAL, buy_t2 REAL, buy_t3 REAL,
                    sell_t1 REAL, sell_t2 REAL, sell_t3 REAL,
                    avg_x REAL, good_invest INTEGER,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS week_buffer (
                    symbol    TEXT PRIMARY KEY,
                    current_x REAL, prev1_x REAL, prev2_x REAL, prev3_x REAL
                );

                -- The RAW daily candles that fed this week's levels. We keep them
                -- so the stock-detail page can show exactly what Monday/Tuesday/
                -- Wednesday traded (like the Excel), not just the derived levels.
                CREATE TABLE IF NOT EXISTS weekly_ohlc (
                    symbol  TEXT PRIMARY KEY,
                    week_id TEXT,
                    mon_o REAL, mon_h REAL, mon_l REAL, mon_c REAL,
                    tue_o REAL, tue_h REAL, tue_l REAL, tue_c REAL,
                    wed_o REAL, wed_h REAL, wed_l REAL, wed_c REAL,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );

                -- One row per signal that actually FIRED (a BUY or SELL trigger).
                -- This is the permanent track record the History page reads. We
                -- record the target ladder at fire time, then later mark hit_t3
                -- once price reaches T3 (the final profit target).
                CREATE TABLE IF NOT EXISTS signal_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT NOT NULL,
                    signal      TEXT NOT NULL,        -- 'BUY' or 'SELL'
                    entry       REAL,                 -- T1, where the trade enters
                    t1          REAL, t2 REAL, t3 REAL,
                    week_id     TEXT,
                    fired_at    TEXT NOT NULL,
                    hit_t3      INTEGER DEFAULT 0,    -- 0 = still open, 1 = reached T3
                    resolved_at TEXT                  -- when T3 was hit (else NULL)
                );
                """
            )

    # ---------------- signal state (the armed/fired memory) ----------------

    def save_signal_state(self, symbol: str, state: SignalState, last_ltp: float | None = None) -> None:
        now = _now_iso()
        with self._connect() as conn:
            # Preserve existing armed_at/fired_at timestamps; only stamp the
            # moment a stage is first reached.
            prev = conn.execute(
                "SELECT armed_dir, armed_at, fired_dir, fired_at FROM signal_state WHERE symbol = ?",
                (symbol,),
            ).fetchone()

            armed_at = prev["armed_at"] if prev else None
            fired_at = prev["fired_at"] if prev else None
            if state.armed_dir and not (prev and prev["armed_dir"]):
                armed_at = now
            if not state.armed_dir:
                armed_at = None
            if state.fired_dir and not (prev and prev["fired_dir"]):
                fired_at = now
            if not state.fired_dir:
                fired_at = None

            conn.execute(
                """
                INSERT INTO signal_state (symbol, armed_dir, armed_at, fired_dir, fired_at, last_ltp, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    armed_dir=excluded.armed_dir, armed_at=excluded.armed_at,
                    fired_dir=excluded.fired_dir, fired_at=excluded.fired_at,
                    last_ltp=excluded.last_ltp,   updated_at=excluded.updated_at
                """,
                (symbol, state.armed_dir, armed_at, state.fired_dir, fired_at, last_ltp, now),
            )

    def load_signal_state(self, symbol: str) -> tuple[SignalState, float | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT armed_dir, fired_dir, last_ltp FROM signal_state WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        if row is None:
            return SignalState(), None
        return SignalState(armed_dir=row["armed_dir"], fired_dir=row["fired_dir"]), row["last_ltp"]

    # ---------------- weekly levels ----------------

    def get_levels_week_id(self, symbol: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT week_id FROM levels WHERE symbol = ?", (symbol,)).fetchone()
        return row["week_id"] if row else None

    def save_levels(self, symbol: str, week_id: str, lv: Levels, avg_x: float | None, good: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO levels (symbol, week_id, mon_tue_high, mon_tue_low, wed_inside,
                    h, l, x, buy_t1, buy_t2, buy_t3, sell_t1, sell_t2, sell_t3,
                    avg_x, good_invest, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    week_id=excluded.week_id, mon_tue_high=excluded.mon_tue_high,
                    mon_tue_low=excluded.mon_tue_low, wed_inside=excluded.wed_inside,
                    h=excluded.h, l=excluded.l, x=excluded.x,
                    buy_t1=excluded.buy_t1, buy_t2=excluded.buy_t2, buy_t3=excluded.buy_t3,
                    sell_t1=excluded.sell_t1, sell_t2=excluded.sell_t2, sell_t3=excluded.sell_t3,
                    avg_x=excluded.avg_x, good_invest=excluded.good_invest,
                    updated_at=excluded.updated_at
                """,
                (symbol, week_id, lv.mon_tue_high, lv.mon_tue_low, int(lv.wed_inside),
                 lv.H, lv.L, lv.X, lv.buy_t1, lv.buy_t2, lv.buy_t3,
                 lv.sell_t1, lv.sell_t2, lv.sell_t3,
                 avg_x, int(good), _now_iso()),
            )

    def load_levels(self, symbol: str) -> tuple[Levels, str, float | None, bool] | None:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM levels WHERE symbol = ?", (symbol,)).fetchone()
        if r is None:
            return None
        lv = Levels(
            mon_tue_high=r["mon_tue_high"], mon_tue_low=r["mon_tue_low"],
            wed_inside=bool(r["wed_inside"]), H=r["h"], L=r["l"], X=r["x"],
            buy_t1=r["buy_t1"], buy_t2=r["buy_t2"], buy_t3=r["buy_t3"],
            sell_t1=r["sell_t1"], sell_t2=r["sell_t2"], sell_t3=r["sell_t3"],
        )
        return lv, r["week_id"], r["avg_x"], bool(r["good_invest"])

    # ---------------- raw weekly daily candles (Mon/Tue/Wed OHLC) ----------------

    def save_weekly_ohlc(self, symbol: str, week_id: str,
                         mon: OHLC, tue: OHLC, wed: OHLC) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO weekly_ohlc (symbol, week_id,
                    mon_o, mon_h, mon_l, mon_c,
                    tue_o, tue_h, tue_l, tue_c,
                    wed_o, wed_h, wed_l, wed_c, updated_at)
                VALUES (?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    week_id=excluded.week_id,
                    mon_o=excluded.mon_o, mon_h=excluded.mon_h, mon_l=excluded.mon_l, mon_c=excluded.mon_c,
                    tue_o=excluded.tue_o, tue_h=excluded.tue_h, tue_l=excluded.tue_l, tue_c=excluded.tue_c,
                    wed_o=excluded.wed_o, wed_h=excluded.wed_h, wed_l=excluded.wed_l, wed_c=excluded.wed_c,
                    updated_at=excluded.updated_at
                """,
                (symbol, week_id,
                 mon.open, mon.high, mon.low, mon.close,
                 tue.open, tue.high, tue.low, tue.close,
                 wed.open, wed.high, wed.low, wed.close, _now_iso()),
            )

    def load_weekly_ohlc(self, symbol: str) -> dict[str, OHLC] | None:
        """Return {'mon': OHLC, 'tue': OHLC, 'wed': OHLC} for the symbol, or None."""
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM weekly_ohlc WHERE symbol = ?", (symbol,)).fetchone()
        if r is None:
            return None
        return {
            "mon": OHLC(r["mon_o"], r["mon_h"], r["mon_l"], r["mon_c"]),
            "tue": OHLC(r["tue_o"], r["tue_h"], r["tue_l"], r["tue_c"]),
            "wed": OHLC(r["wed_o"], r["wed_h"], r["wed_l"], r["wed_c"]),
        }

    # ---------------- 4-week rolling buffer (transactional) ----------------

    def load_buffer(self, symbol: str) -> WeekBuffer:
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM week_buffer WHERE symbol = ?", (symbol,)).fetchone()
        if r is None:
            return WeekBuffer()
        return WeekBuffer(current=r["current_x"], prev1=r["prev1_x"], prev2=r["prev2_x"], prev3=r["prev3_x"])

    def set_buffer(self, symbol: str, buf: WeekBuffer) -> None:
        """Overwrite the buffer outright (no shift). Used to update the CURRENT
        week's range in place on a same-week recompute, without rolling."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO week_buffer (symbol, current_x, prev1_x, prev2_x, prev3_x)
                VALUES (?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    current_x=excluded.current_x, prev1_x=excluded.prev1_x,
                    prev2_x=excluded.prev2_x, prev3_x=excluded.prev3_x
                """,
                (symbol, buf.current, buf.prev1, buf.prev2, buf.prev3),
            )

    def roll_buffer(self, symbol: str, new_x: float) -> WeekBuffer:
        """Shift the 4-week buffer right and drop in this week's range, as ONE
        transaction. The `with self._connect()` block commits only if the whole
        read-then-write succeeds; any error rolls it back, so a crash mid-shift
        can never leave the buffer half-updated."""
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM week_buffer WHERE symbol = ?", (symbol,)).fetchone()
            old = (
                WeekBuffer(r["current_x"], r["prev1_x"], r["prev2_x"], r["prev3_x"])
                if r else WeekBuffer()
            )
            # shift right: current -> prev1 -> prev2 -> prev3 (old prev3 dropped)
            new = WeekBuffer(current=new_x, prev1=old.current, prev2=old.prev1, prev3=old.prev2)
            conn.execute(
                """
                INSERT INTO week_buffer (symbol, current_x, prev1_x, prev2_x, prev3_x)
                VALUES (?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    current_x=excluded.current_x, prev1_x=excluded.prev1_x,
                    prev2_x=excluded.prev2_x, prev3_x=excluded.prev3_x
                """,
                (symbol, new.current, new.prev1, new.prev2, new.prev3),
            )
        return new

    # ---------------- signal log (the permanent track record) ----------------

    def append_signal_log(
        self, symbol: str, signal: str, entry: float | None,
        t1: float | None, t2: float | None, t3: float | None,
        week_id: str | None, fired_at: str | None = None,
    ) -> int:
        """Record a freshly fired signal. Returns the new row's id."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO signal_log (symbol, signal, entry, t1, t2, t3, week_id, fired_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (symbol, signal, entry, t1, t2, t3, week_id, fired_at or _now_iso()),
            )
            return cur.lastrowid

    def get_open_signal_logs(self, symbol: str) -> list[sqlite3.Row]:
        """Signals for this symbol that haven't reached T3 yet (still open)."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM signal_log WHERE symbol = ? AND hit_t3 = 0 AND resolved_at IS NULL",
                (symbol,),
            ).fetchall()

    def resolve_signal_log(self, log_id: int, resolved_at: str | None = None) -> None:
        """Mark a logged signal as having reached T3."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE signal_log SET hit_t3 = 1, resolved_at = ? WHERE id = ?",
                (resolved_at or _now_iso(), log_id),
            )

    def load_history(self, limit: int = 100) -> list[sqlite3.Row]:
        """Most-recent-first list of every signal ever fired, for the History page."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM signal_log ORDER BY fired_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # ---------------- small key/value meta (last run timestamps) ----------------

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
