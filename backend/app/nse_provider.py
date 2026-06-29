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
import zipfile
from datetime import date, timedelta

from app.nse_client import NSEClient, NSEError
from app.providers import DataProvider
from app.strategy import OHLC

log = logging.getLogger(__name__)

MASTER_QUOTE = "https://www.nseindia.com/api/master-quote"
OPTION_CHAIN = "https://www.nseindia.com/api/option-chain-equities?symbol={sym}"
PREOPEN_FO = "https://www.nseindia.com/api/market-data-pre-open?key=FO"
PREOPEN_REFERER = (
    "https://www.nseindia.com/market-data/pre-open-market-cm-and-emerge-market"
)
BHAVCOPY = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
)
OC_REFERER = "https://www.nseindia.com/option-chain"


class NSEProvider(DataProvider):
    def __init__(self, client: NSEClient | None = None):
        self._client = client or NSEClient()
        self._symbols: list[str] | None = None
        # date (YYYYMMDD) -> {symbol: OHLC} for that day's EQ rows. Bhavcopy is
        # end-of-day and immutable, so once parsed we cache it for the process.
        self._bhav_cache: dict[str, dict[str, OHLC]] = {}

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
        """Download+parse one day's bhavcopy into {symbol: OHLC}; cached per day."""
        key = day.strftime("%Y%m%d")
        if key in self._bhav_cache:
            return self._bhav_cache[key]
        url = BHAVCOPY.format(ymd=key)
        raw = self._client.get_bytes(url)  # raises NSEError if 404 (non-trading day)
        parsed = _parse_bhavcopy(raw)
        self._bhav_cache[key] = parsed
        return parsed

    # -- live price --------------------------------------------------------

    def get_live_price(self, symbol: str) -> float:
        """Live underlying value from the option-chain endpoint; if that's empty
        (market closed) or fails, fall back to the latest bhavcopy close so the
        app always has a number to evaluate."""
        sym = symbol.upper()
        try:
            data = self._client.get_json(OPTION_CHAIN.format(sym=sym), referer=OC_REFERER, tries=2)
            if isinstance(data, dict):
                val = data.get("records", {}).get("underlyingValue")
                if val:
                    return float(val)
        except NSEError as e:
            log.warning("live price for %s failed (%s); using last close", sym, e)
        return self._last_close(sym)

    def get_all_live_prices(self) -> dict[str, float]:
        """ONE call for every F&O stock's live price, instead of one call per
        symbol. Hits the pre-open/quotes feed that backs NSE's 'Securities in F&O'
        table and returns {SYMBOL: lastPrice}. Cuts a 211-symbol scan from 211 NSE
        calls down to 1 — Gowtham's idea.

        CAVEAT: this endpoint is the pre-open snapshot. It is only safe to drive
        live signals from it if metadata.lastPrice actually updates during
        continuous trading (09:15-15:30). That is verified separately
        (spikes/preopen_live_test.py); until confirmed, scan() stays on the
        per-symbol path. Raises NSEError on failure so the caller can fall back."""
        data = self._client.get_json(PREOPEN_FO, referer=PREOPEN_REFERER, tries=3)
        rows = data.get("data") if isinstance(data, dict) else None
        if not rows:
            raise NSEError("pre-open FO feed returned no rows")
        out: dict[str, float] = {}
        for r in rows:
            meta = r.get("metadata", {})
            sym, price = meta.get("symbol"), meta.get("lastPrice")
            if sym and price:
                try:
                    out[str(sym).strip().upper()] = float(price)
                except (TypeError, ValueError):
                    continue
        if not out:
            raise NSEError("pre-open FO feed had no usable symbol/price rows")
        return out

    def _last_close(self, symbol: str) -> float:
        """Most recent available bhavcopy close for the symbol."""
        mon, tue, wed = self._resolve_week_days()
        ohlc = self._bhavcopy(wed).get(symbol)
        if ohlc is None:
            raise NSEError(f"no last close available for {symbol}")
        return ohlc.close


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
