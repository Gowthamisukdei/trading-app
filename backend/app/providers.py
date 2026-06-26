"""
providers.py — where market data comes FROM.

The whole app talks to data only through the DataProvider interface below.
It does NOT know or care whether the numbers come from a fake hardcoded table,
an NSE scraper, or a paid broker API. That's the single most important design
choice in this project: when the NSE scraper inevitably gives us trouble, we
swap in a different provider class and change ONE line — nothing else moves.

Today we ship a FakeProvider with hardcoded data so we can build and SEE the
entire app working before we ever touch the risky scraping. The real
NSEProvider comes LAST, behind this exact same interface.
"""

from abc import ABC, abstractmethod
from datetime import date

from app.strategy import OHLC


class DataProvider(ABC):
    """The contract every data source must satisfy. Nothing else changes when
    we swap one implementation for another."""

    @abstractmethod
    def get_fno_symbols(self) -> list[str]:
        """All F&O stock symbols we should track."""

    @abstractmethod
    def get_daily_ohlc(self, symbol: str, day: date) -> OHLC:
        """That stock's Open/High/Low/Close for one trading day."""

    @abstractmethod
    def get_live_price(self, symbol: str) -> float:
        """The latest traded price for that stock, right now."""


# Three named weekdays we care about. The real provider will resolve these from
# the calendar; the fake one just maps them to its hardcoded Mon/Tue/Wed.
class _FakeStock:
    """One hardcoded stock: its Mon/Tue/Wed OHLC plus a SCRIPT of live prices.

    The price script is the clever bit. Each time we 'scan', get_live_price
    returns the next price in the list, so successive scans walk the stock
    through its story: quiet -> Mon-Tue level broken (armed) -> hits T1 (fires).
    This lets us watch the state machine come alive on fake data exactly the way
    it will on real data, just without waiting for a real market day.
    """

    def __init__(self, mon: OHLC, tue: OHLC, wed: OHLC, price_script: list[float]):
        self.mon = mon
        self.tue = tue
        self.wed = wed
        self.price_script = price_script
        self._tick = 0

    def next_price(self) -> float:
        # Walk forward through the script; hold on the last price once exhausted
        # (a real stock keeps trading; it doesn't run out of prices).
        price = self.price_script[min(self._tick, len(self.price_script) - 1)]
        self._tick += 1
        return price


class FakeProvider(DataProvider):
    """Hardcoded data that exercises every status the dashboard can show."""

    def __init__(self):
        self._stocks: dict[str, _FakeStock] = {
            # 360ONE — the real Excel row. Inside-day. Script walks it to a BUY:
            #   1190 quiet -> 1160 breaks Mon-Tue low (armed buy) -> 1212 >= buy_t1 (BUY)
            "360ONE": _FakeStock(
                mon=OHLC(1179.8, 1188.0, 1165.2, 1170.6),
                tue=OHLC(1164.4, 1217.9, 1164.3, 1191.6),
                wed=OHLC(1198.0, 1198.0, 1170.2, 1190.0),
                price_script=[1190.0, 1160.0, 1212.0],
            ),
            # RELIANCE — script walks it to a SELL:
            #   2900 quiet -> 3010 breaks Mon-Tue high (armed sell) -> 2840 <= sell_t1 (SELL)
            "RELIANCE": _FakeStock(
                mon=OHLC(2950.0, 2990.0, 2900.0, 2960.0),
                tue=OHLC(2965.0, 3000.0, 2940.0, 2980.0),
                wed=OHLC(2975.0, 2995.0, 2955.0, 2970.0),
                price_script=[2900.0, 3010.0, 2840.0],
            ),
            # TCS — stays quiet inside its range the whole time (status NONE).
            "TCS": _FakeStock(
                mon=OHLC(3850.0, 3900.0, 3820.0, 3870.0),
                tue=OHLC(3865.0, 3910.0, 3840.0, 3880.0),
                wed=OHLC(3875.0, 3905.0, 3855.0, 3890.0),
                price_script=[3880.0, 3885.0, 3878.0],
            ),
        }

    def get_fno_symbols(self) -> list[str]:
        return list(self._stocks.keys())

    def get_daily_ohlc(self, symbol: str, day: date) -> OHLC:
        # The fake provider ignores the real date and uses its scripted week.
        # Callers pass a tag day via the helper below; we map by weekday name.
        raise NotImplementedError(
            "FakeProvider uses get_week_ohlc(); real providers implement this by date."
        )

    # Convenience the fake/real week-compute step actually uses. The real
    # provider will build this from get_daily_ohlc() across the week's dates.
    def get_week_ohlc(self, symbol: str) -> tuple[OHLC, OHLC, OHLC]:
        s = self._stocks[symbol]
        return s.mon, s.tue, s.wed

    def get_live_price(self, symbol: str) -> float:
        return self._stocks[symbol].next_price()
