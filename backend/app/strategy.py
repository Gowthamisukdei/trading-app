"""
strategy.py — the brain of the trading app.

These are PURE functions: same input always gives same output, no internet,
no database, no clock. That's deliberate. Pure functions are trivial to test,
so we can prove the trading logic is correct (against weeklyalgo.xlsx) before
we build anything else around them.

Vocabulary (from the Excel):
  OHLC   = Open / High / Low / Close prices for one stock on one day
  Levels = the weekly reference prices + target ladders we trade around
  X      = the weekly "range", the single number that drives every target
"""

from dataclasses import dataclass


# Prices on NSE move in 2-decimal increments, and the Excel shows 2 decimals.
# We round all derived numbers to 2 places so our output matches the sheet
# exactly instead of carrying floating-point noise like 27.79999999.
_DP = 2


@dataclass(frozen=True)
class OHLC:
    """One day's Open, High, Low, Close for one stock."""
    open: float
    high: float
    low: float
    close: float

    def values(self) -> tuple[float, float, float, float]:
        return (self.open, self.high, self.low, self.close)


@dataclass(frozen=True)
class Levels:
    """
    Everything we compute once per week (Wednesday after close) for one stock.
    These are the lines on the chart we watch live during the week.
    """
    mon_tue_high: float   # highest price across Mon + Tue (all 8 OHLC values)
    mon_tue_low: float    # lowest price across Mon + Tue
    wed_inside: bool      # True if Wednesday traded fully inside the Mon-Tue range
    H: float              # upper reference level (the "ceiling")
    L: float              # lower reference level (the "floor")
    X: float              # the range that drives targets

    # BUY ladder — prices ABOVE H. T1 = entry, T3 = final profit target.
    buy_t1: float
    buy_t2: float
    buy_t3: float

    # SELL ladder — prices BELOW L. T1 = entry, T3 = final profit target.
    sell_t1: float
    sell_t2: float
    sell_t3: float


def compute_levels(monday: OHLC, tuesday: OHLC, wednesday: OHLC) -> Levels:
    """
    Turn Monday + Tuesday + Wednesday OHLC into the weekly Levels.

    Step 1 — find the Mon-Tue envelope:
        Look at ALL 8 numbers (O,H,L,C for Mon and for Tue) and take the
        single highest and single lowest. That box is the week's battlefield.

    Step 2 — the Wednesday "inside day" tightening:
        If Wednesday traded entirely INSIDE the Mon-Tue box (its high didn't
        exceed the box top AND its low didn't break the box bottom), the market
        is coiling. We tighten our reference levels to Wednesday's own high/low,
        because the breakout from a tighter coil is a cleaner signal.
        Otherwise we keep the wider Mon-Tue box.

    Step 3 — build the target ladders from H, L and the range X.
    """
    # --- Step 1: the Mon-Tue envelope (max/min over all 8 values) ---
    mon_tue_values = monday.values() + tuesday.values()
    mon_tue_high = max(mon_tue_values)
    mon_tue_low = min(mon_tue_values)

    # --- Step 2: Wednesday inside-day check ---
    wed_inside = wednesday.high < mon_tue_high and wednesday.low > mon_tue_low
    if wed_inside:
        H = wednesday.high
        L = wednesday.low
    else:
        H = mon_tue_high
        L = mon_tue_low
    X = H - L

    # --- Step 3: target ladders ---
    #   BUY  (break upward above H):   H + X/2,  H + X,  H + 2X
    #   SELL (break downward below L): L - X/2,  L - X,  L - 2X
    return Levels(
        mon_tue_high=round(mon_tue_high, _DP),
        mon_tue_low=round(mon_tue_low, _DP),
        wed_inside=wed_inside,
        H=round(H, _DP),
        L=round(L, _DP),
        X=round(X, _DP),
        buy_t1=round(H + X / 2, _DP),
        buy_t2=round(H + X, _DP),
        buy_t3=round(H + 2 * X, _DP),
        sell_t1=round(L - X / 2, _DP),
        sell_t2=round(L - X, _DP),
        sell_t3=round(L - 2 * X, _DP),
    )


# ---------------------------------------------------------------------------
# SIGNAL ENGINE — the two-stage arm -> trigger state machine
# ---------------------------------------------------------------------------
#
# This runs LIVE during market hours on every price tick. It's still a pure
# function: you hand it the levels, the latest price, and the stock's current
# state; it hands back the NEW state. No memory of its own — the caller stores
# state in the database so it survives a backend restart.
#
# The five possible statuses a stock can be in:
#   NONE       — nothing happening, just watching
#   ARMED_BUY  — price faked DOWN below the floor; we're waiting to go LONG
#   ARMED_SELL — price faked UP above the ceiling; we're waiting to go SHORT
#   BUY        — armed-buy confirmed: real BUY signal fired
#   SELL       — armed-sell confirmed: real SELL signal fired
#
# The reversal idea: a false break one way ARMS the setup; the real break the
# OTHER way TRIGGERS the entry.

STATUS_NONE = "NONE"
STATUS_ARMED_BUY = "ARMED_BUY"
STATUS_ARMED_SELL = "ARMED_SELL"
STATUS_BUY = "BUY"
STATUS_SELL = "SELL"


@dataclass(frozen=True)
class SignalState:
    """
    A stock's live position in the state machine. Persist this in the DB.
      armed_dir : "BUY" | "SELL" | None  — which way we're armed, if any
      fired_dir : "BUY" | "SELL" | None  — which way we've already fired, if any
    Once fired, a stock is done for the week until fresh levels reset it.
    """
    armed_dir: str | None = None
    fired_dir: str | None = None

    @property
    def status(self) -> str:
        if self.fired_dir == "BUY":
            return STATUS_BUY
        if self.fired_dir == "SELL":
            return STATUS_SELL
        if self.armed_dir == "BUY":
            return STATUS_ARMED_BUY
        if self.armed_dir == "SELL":
            return STATUS_ARMED_SELL
        return STATUS_NONE


def evaluate_signal(levels: Levels, ltp: float, state: SignalState) -> SignalState:
    """
    Advance the state machine by one price tick. Returns the NEW state.

    ltp = "last traded price" (the live price right now).

    Order of checks matters:
      1. Already fired? Then we're locked for the week — return unchanged.
      2. Armed already? Look only for that direction's TRIGGER:
           - armed BUY  + price breaks UP   through buy_t1  -> fire BUY
           - armed SELL + price breaks DOWN through sell_t1 -> fire SELL
      3. Not armed yet? Look for an ARM:
           - price drops BELOW mon_tue_low  -> arm BUY  (faked down, expect reversal up)
           - price rises ABOVE mon_tue_high -> arm SELL (faked up, expect reversal down)

    NOTE / decision to confirm with Gowtham: once armed in one direction we keep
    watching that direction until it fires or the week resets (we do NOT flip to
    the opposite arm if price later pokes the other extreme). This matches
    "an armed setup stays valid until the next Wednesday".
    """
    # 1. Terminal for the week once a real signal has fired.
    if state.fired_dir is not None:
        return state

    # 2. Armed -> watch for the trigger (entry).
    if state.armed_dir == "BUY":
        if ltp >= levels.buy_t1:
            return SignalState(armed_dir="BUY", fired_dir="BUY")
        return state
    if state.armed_dir == "SELL":
        if ltp <= levels.sell_t1:
            return SignalState(armed_dir="SELL", fired_dir="SELL")
        return state

    # 3. Not armed yet -> watch for an arm (the false break).
    if ltp < levels.mon_tue_low:
        return SignalState(armed_dir="BUY")
    if ltp > levels.mon_tue_high:
        return SignalState(armed_dir="SELL")

    return state


def evaluate_day(levels: Levels, high: float, low: float, state: SignalState) -> SignalState:
    """Advance the state machine using ONE day's High and Low (from the daily
    bhavcopy), instead of a single live snapshot.

    This is how the strategy is really meant to be read off the daily candles, and
    it fixes two holes in the live-snapshot scan:
      * it catches an arm/fire that happened on a day we WEREN'T live-scanning
        (e.g. before the backend went live, or across a restart), and
      * it catches a price that WICKED through a level and reversed within a scan
        gap — the day's High/Low sees the touch, a 15-min snapshot might miss it.

    Same rules as evaluate_signal, but the day's EXTREME does the crossing:
      * armed BUY  fires if the day's HIGH reached buy_t1   (entry)
      * armed SELL fires if the day's LOW  reached sell_t1  (entry)
      * not armed: a day whose LOW broke below the floor arms BUY; whose HIGH
        broke above the ceiling arms SELL.

    Conservative SAME-DAY rule: a stock that ARMS on this day does NOT also fire on
    the same day. Daily OHLC can't tell us whether the High or the Low came first,
    so we can't prove a same-day fake-then-reverse happened in that order. The
    multi-day whipsaw the strategy targets (arm one day, fire a LATER day) is
    unambiguous and handled — the armed state carries into the next day's call.
    """
    # 1. Terminal for the week once a real signal has fired.
    if state.fired_dir is not None:
        return state

    # 2. Armed -> the day's extreme in the trigger direction confirms the entry.
    if state.armed_dir == "BUY":
        if high >= levels.buy_t1:
            return SignalState(armed_dir="BUY", fired_dir="BUY")
        return state
    if state.armed_dir == "SELL":
        if low <= levels.sell_t1:
            return SignalState(armed_dir="SELL", fired_dir="SELL")
        return state

    # 3. Not armed yet -> a day that broke the floor/ceiling arms the setup.
    if low < levels.mon_tue_low:
        return SignalState(armed_dir="BUY")
    if high > levels.mon_tue_high:
        return SignalState(armed_dir="SELL")

    return state


# ---------------------------------------------------------------------------
# WEEK-ROLLING BUFFER — keep current + previous 3 weeks per stock
# ---------------------------------------------------------------------------
#
# Mirrors the Excel's Current / Prev-1 / Prev-2 / Prev-3 columns. We keep the
# range X from the last few weeks so we can spot "compression": when this week's
# range is unusually tight versus recent history, a big move often follows.


@dataclass(frozen=True)
class WeekBuffer:
    """
    Four weekly ranges for one stock, newest first. Each entry is that week's
    X (range). None means "no data yet" (e.g. a freshly added stock).
    """
    current: float | None = None
    prev1: float | None = None
    prev2: float | None = None
    prev3: float | None = None


def roll_weeks(buffer: WeekBuffer, new_x: float) -> WeekBuffer:
    """
    Shift the buffer right and drop in this week's freshly computed range.

        prev3   <- prev2
        prev2   <- prev1
        prev1   <- current   (the week that just finished)
        current <- new_x     (this week)
        (old prev3 falls off the end)

    Pure function: the DB layer will apply the result as ONE transaction so a
    crash mid-shift can never corrupt the buffer.
    """
    return WeekBuffer(
        current=new_x,
        prev1=buffer.current,
        prev2=buffer.prev1,
        prev3=buffer.prev2,
    )


def good_invest(buffer: WeekBuffer) -> bool:
    """
    "Good invest" quality flag: True when this week's range is less than HALF
    the average of the previous 3 weeks — i.e. the stock has coiled unusually
    tight, which often precedes a strong move.

    Returns False if we don't yet have all 3 prior weeks (not enough history to
    judge). It's a badge, not a hard filter, unless Gowtham asks to gate on it.
    """
    if buffer.current is None:
        return False
    prevs = [buffer.prev1, buffer.prev2, buffer.prev3]
    if any(p is None for p in prevs):
        return False
    avg_x = sum(prevs) / 3
    if avg_x == 0:
        return False
    return buffer.current < avg_x / 2

