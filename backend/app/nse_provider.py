"""
nse_provider.py — the REAL data source, behind the same DataProvider interface
the rest of the app already uses. Swapping FakeProvider -> NSEProvider is the
whole point of the interface: service.py, the engine, the DB and the web layer
do not change.

Where each piece of data comes from (proven in backend/spikes/):
  * get_fno_symbols   -> /api/master-quote            (list of F&O symbols)
  * get_week_ohlc     -> daily BHAVCOPY zip per trading day (Open/High/Low/Close
                         for every stock; the archive host is far less protected)
  * get_live_price    -> /api/option-chain-equities    (records.underlyingValue);
                         falls back to the latest bhavcopy close when the market
                         is closed or that call fails.

Resilience rule (from the project's #1 risk): NSE WILL fail sometimes. Every
method either returns good data or raises NSEError — it must never hang the app.
The scheduler/service already skip a symbol whose data is missing.
"""

import csv
import io
import logging
import os
import time
import zipfile
from datetime import date, timedelta

from app.nse_client import NSEClient, NSEError
from app.providers import DataProvider
from app.strategy import OHLC

log = logging.getLogger(__name__)

MASTER_QUOTE = "https://www.nseindia.com/api/master-quote"
# Live LTP for the MOVING stocks, in two calls (top gainers + top losers). This is
# the WORKING live feed — option-chain-equities returns an empty {} from a server
# IP even during market hours (see get_live_price). NSE spells losers "loosers".
LIVE_VARIATIONS = "https://www.nseindia.com/api/live-analysis-variations?index={idx}"
LIVE_VAR_REFERER = (
    "https://www.nseindia.com/market-data/live-market-indices/live-analysis"
)
_LAV_BUCKETS = ("FOSec", "NIFTY", "BANKNIFTY", "NIFTYNEXT50", "SecGtr20", "SecLwr20")
BHAVCOPY = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
)
# How long a 404'd bhavcopy day is remembered as missing before we re-probe it.
# Long enough to skip the not-yet-published TODAY file across a whole scan; short
# enough that today's file is found within half an hour of its evening publish.
_MISSING_TTL_S = 1800


class NSEProvider(DataProvider):
    def __init__(self, client: NSEClient | None = None):
        self._client = client or NSEClient()
        self._symbols: list[str] | None = None
        # date (YYYYMMDD) -> {symbol: OHLC} for that day's EQ rows. Bhavcopy is
        # end-of-day and immutable, so once parsed we cache it for the process.
        self._bhav_cache: dict[str, dict[str, OHLC]] = {}
        # date (YYYYMMDD) -> monotonic time we last got a 404 for that day's file.
        # Stops a scan from re-downloading TODAY's (not-yet-published) bhavcopy once
        # per flat stock — that was a ~300s storm. Short TTL so today's file is still
        # picked up within MISSING_TTL_S of being published in the evening.
        self._missing_days: dict[str, float] = {}

    # -- symbols -----------------------------------------------------------

    def get_fno_symbols(self) -> list[str]:
        """All F&O symbols. Override with TRADING_SYMBOLS (comma-separated) to run
        a small watchlist — the live scan makes one option-chain call per symbol,
        so 200+ symbols every 5 min is heavy; a watchlist keeps it light."""
        override = os.getenv("TRADING_SYMBOLS")
        if override:
            return [s.strip().upper() for s in override.split(",") if s.strip()]
        if self._symbols is None:
            data = self._client.get_json(MASTER_QUOTE)
            if not isinstance(data, list) or not data:
                raise NSEError("master-quote returned no symbols")
            self._symbols = [str(s).upper() for s in data]
        return self._symbols

    # -- daily / weekly OHLC (bhavcopy) -----------------------------------

    def get_daily_ohlc(self, symbol: str, day: date) -> OHLC:
        ohlc = self._bhavcopy(day).get(symbol.upper())
        if ohlc is None:
            raise NSEError(f"{symbol} not in bhavcopy for {day.isoformat()}")
        return ohlc

    def get_week_ohlc(self, symbol: str) -> tuple[OHLC, OHLC, OHLC]:
        """Monday/Tuesday/Wednesday OHLC for the most recent COMPLETED such week.
        Walks back week-by-week until all three bhavcopies exist (handles 'this
        week's Wednesday hasn't happened yet' and, crudely, a holiday in the week
        by using the prior week). Good enough for v1; revisit holiday handling."""
        mon, tue, wed = self._resolve_week_days()
        return (
            self.get_daily_ohlc(symbol, mon),
            self.get_daily_ohlc(symbol, tue),
            self.get_daily_ohlc(symbol, wed),
        )

    def get_week_days(self) -> tuple[date, date, date]:
        """The Mon/Tue/Wed dates whose bhavcopies built the current levels. The
        daily replay uses Wednesday as its anchor: it walks every trading day
        AFTER it, checking each day's High/Low against the levels."""
        return self._resolve_week_days()

    def has_daily_data(self, day: date) -> bool:
        """True if that day's bhavcopy exists (i.e. it's a completed trading day
        whose EOD file is published). Lets the replay skip today before the close
        without firing 211 doomed downloads for a file that isn't out yet."""
        return self._bhavcopy_exists(day)

    def _resolve_week_days(self, max_weeks_back: int = 6) -> tuple[date, date, date]:
        # Start from the Monday of the current ISO week.
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        for _ in range(max_weeks_back):
            mon, tue, wed = monday, monday + timedelta(days=1), monday + timedelta(days=2)
            if all(self._bhavcopy_exists(d) for d in (mon, tue, wed)):
                return mon, tue, wed
            monday -= timedelta(days=7)  # try the previous week
        raise NSEError("no complete Mon/Tue/Wed bhavcopy set found in recent weeks")

    def _bhavcopy_exists(self, day: date) -> bool:
        try:
            self._bhavcopy(day)
            return True
        except NSEError:
            return False

    def _bhavcopy(self, day: date) -> dict[str, OHLC]:
        """Download+parse one day's bhavcopy into {symbol: OHLC}; cached per day.

        A recently-404'd day (no file published yet — typically TODAY during market
        hours) is remembered in _missing_days for MISSING_TTL_S so a single scan
        doesn't re-download it once per flat stock. The TTL is short enough that
        today's file is still discovered soon after it's published in the evening."""
        key = day.strftime("%Y%m%d")
        if key in self._bhav_cache:
            return self._bhav_cache[key]
        missed_at = self._missing_days.get(key)
        if missed_at is not None and (time.monotonic() - missed_at) < _MISSING_TTL_S:
            raise NSEError(f"bhavcopy for {key} known-missing (cached)")
        url = BHAVCOPY.format(ymd=key)
        try:
            raw = self._client.get_bytes(url)  # raises NSEError if 404 (non-trading day)
        except NSEError:
            self._missing_days[key] = time.monotonic()
            raise
        parsed = _parse_bhavcopy(raw)
        self._bhav_cache[key] = parsed
        self._missing_days.pop(key, None)  # it exists now; forget any stale miss
        return parsed

    # -- live price --------------------------------------------------------

    def get_live_price(self, symbol: str) -> float:
        """Per-symbol price = the most recent bhavcopy close.

        The LIVE last-traded price now comes from the BULK feed
        (get_all_live_prices, used by scan() when TRADING_BULK_LIVE is on). We no
        longer call option-chain-equities per symbol: it returns an empty {} from a
        server IP even during market hours (confirmed live), so it never produced a
        live price — every stock silently fell back to last close anyway, at the
        cost of 211 doomed requests per scan. For a symbol the bulk feed didn't
        cover (a flat stock outside the gainers/losers lists, which can't cross a
        trigger anyway), the latest close is the right stand-in."""
        return self._last_close(symbol.upper())

    def get_all_live_prices(self) -> dict[str, float]:
        """LIVE last-traded price for every MOVING F&O stock, in two NSE calls (top
        gainers + top losers across the NIFTY / BankNifty / NiftyNext50 / F&O
        buckets of live-analysis-variations). Returns {SYMBOL: ltp}. Cuts a
        211-symbol scan from ~211 NSE calls down to 2.

        Why this and not option-chain: option-chain-equities returns an empty {}
        from a server IP during market hours (confirmed), so the old per-symbol
        live path froze every stock at last close. live-analysis-variations is the
        working live feed (verified updating intraday). It only lists stocks that
        are MOVING — about half the universe — which is exactly right: a flat stock
        can't cross a trigger, so the bulk feed covers every stock that could fire,
        and the rest fall back to last close in scan(). Raises NSEError only if
        BOTH calls fail, so scan() can fall back to the per-symbol path."""
        out: dict[str, float] = {}
        errors = 0
        for idx in ("gainers", "loosers"):  # NSE spells the losers index "loosers"
            try:
                data = self._client.get_json(
                    LIVE_VARIATIONS.format(idx=idx), referer=LIVE_VAR_REFERER, tries=3
                )
            except NSEError as e:
                log.warning("live-analysis %s failed (%s)", idx, e)
                errors += 1
                continue
            if not isinstance(data, dict):
                continue
            for bucket in _LAV_BUCKETS:
                b = data.get(bucket)
                rows = b.get("data") if isinstance(b, dict) else None
                for r in rows or []:
                    sym = str(r.get("symbol") or "").strip().upper()
                    ltp = r.get("ltp")
                    if not sym or ltp is None:
                        continue
                    try:
                        out[sym] = float(ltp)
                    except (TypeError, ValueError):
                        continue
        if not out:
            raise NSEError(
                f"live-analysis-variations yielded no prices ({errors} call errors)"
            )
        return out

    def _last_close(self, symbol: str) -> float:
        """The symbol's MOST RECENT available bhavcopy close. Walk back day-by-day
        from today to the latest trading day whose bhavcopy is published — so when
        no live price is available (market closed, or option-chain empty for this
        symbol) we show the freshest close, not a stale one.

        Bug this fixes: the old version always read the LEVELS-week Wednesday close,
        so every stock without a live price was frozen at last Wednesday's price
        even days later (e.g. BOSCHLTD stuck at its 6/24 close on the following
        Monday). Each day's file is cached, so the walk is cheap."""
        day = date.today()
        for _ in range(10):  # look back ~10 days to clear a weekend + holidays
            try:
                ohlc = self._bhavcopy(day).get(symbol)
                if ohlc is not None:
                    return ohlc.close
            except NSEError:
                pass  # that day's bhavcopy isn't published (weekend/holiday/today)
            day -= timedelta(days=1)
        raise NSEError(f"no recent close available for {symbol}")


def _parse_bhavcopy(raw_zip: bytes) -> dict[str, OHLC]:
    """Read the single CSV inside the bhavcopy zip and pull EQ-series OHLC.
    UDiFF columns: TckrSymb, SctySrs, OpnPric, HghPric, LwPric, ClsPric (verified
    against a real file in spikes/bhav_inspect.py)."""
    zf = zipfile.ZipFile(io.BytesIO(raw_zip))
    name = zf.namelist()[0]
    text = zf.read(name).decode("utf-8", "replace")
    out: dict[str, OHLC] = {}
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("SctySrs") != "EQ":
            continue  # skip non-equity series (BE, futures rows, etc.)
        try:
            out[row["TckrSymb"].strip().upper()] = OHLC(
                open=float(row["OpnPric"]),
                high=float(row["HghPric"]),
                low=float(row["LwPric"]),
                close=float(row["ClsPric"]),
            )
        except (KeyError, ValueError):
            continue  # malformed row -> skip, don't crash the whole parse
    if not out:
        raise NSEError("bhavcopy parsed to zero EQ rows (format may have changed)")
    return out
