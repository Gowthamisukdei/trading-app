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

    # ENTRY triggers — the Fibonacci 23.6% breakout beyond the Mon-Tue box.
    # A continuation BUY enters when price clears buy_level; a SELL at sell_level.
    buy_level: float
    sell_level: float

    # BUY target ladder — prices ABOVE H. These are PROFIT TARGETS (not the entry):
    # T1 = first target (+½X), T2 (+X), T3 (+2X).
    buy_t1: float
    buy_t2: float
    buy_t3: float

    # SELL target ladder — prices BELOW L. T1 = first target, T3 = final target.
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

    # --- Step 3: entry levels + target ladders (the CONTINUATION play) ---
    # Both the entry AND the targets are measured off the FULL Mon-Tue box, so they
    # always nest correctly: box top < buy_level(+23.6%) < T1(+50%) < T2(+100%) <
    # T3(+200%). We deliberately do NOT use the inside-day-tightened H/X here: on an
    # inside day that would put T1 *below* the box top and below the entry (the bug
    # BOSCHLTD exposed). H/L/X stay on the Levels for the compression flag and for
    # Play 2 (the reverse breakout), where the tightened ladder belongs.
    span = mon_tue_high - mon_tue_low  # the box range drives entry AND targets
    return Levels(
        mon_tue_high=round(mon_tue_high, _DP),
        mon_tue_low=round(mon_tue_low, _DP),
        wed_inside=wed_inside,
        H=round(H, _DP),
        L=round(L, _DP),
        X=round(X, _DP),
        buy_level=round(mon_tue_high + 0.236 * span, _DP),
        sell_level=round(mon_tue_low - 0.236 * span, _DP),
        buy_t1=round(mon_tue_high + span / 2, _DP),
        buy_t2=round(mon_tue_high + span, _DP),
        buy_t3=round(mon_tue_high + 2 * span, _DP),
        sell_t1=round(mon_tue_low - span / 2, _DP),
        sell_t2=round(mon_tue_low - span, _DP),
        sell_t3=round(mon_tue_low - 2 * span, _DP),
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
# This is the CONTINUATION breakout (Play 1, validated by backtest): price breaks
# out of the Mon-Tue box and we go WITH it once it confirms past the BUY/SELL LEVEL
# (the 23.6% extension). T1/T2/T3 are profit targets, NOT the entry.
#
# The five possible statuses a stock can be in:
#   NONE       — inside the box, just watching
#   ARMED_BUY  — price broke UP above the ceiling; waiting to confirm at buy_level
#   ARMED_SELL — price broke DOWN below the floor; waiting to confirm at sell_level
#   BUY        — confirmed: price cleared buy_level -> LONG entered (targets T1/T2/T3)
#   SELL       — confirmed: price cleared sell_level -> SHORT entered
#
# The idea: breaking the box ARMS the setup; clearing the 23.6% level CONFIRMS and
# ENTERS it. (The rare REVERSE breakout — fake one way then break the other — is a
# separate play we add later; this engine is the common continuation one.)

STATUS_NONE = "NONE"
STATUS_ARMED_BUY = "ARMED_BUY"
STATUS_ARMED_SELL = "ARMED_SELL"
STATUS_BUY = "BUY"
STATUS_SELL = "SELL"

# Play 2 (reverse breakout) statuses — the trap phase before the reverse entry.
STATUS_FAKED_DOWN = "FAKED_DOWN"   # first move tagged sell_t1 — watch reverse UP -> BUY
STATUS_FAKED_UP = "FAKED_UP"       # first move tagged buy_t1 — watch reverse DOWN -> SELL


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

    CONTINUATION model — the entry is the BUY/SELL LEVEL, not T1:
      1. Already fired? Locked for the week — return unchanged.
      2. Armed already? Watch that direction's ENTRY (the 23.6% confirmation):
           - armed BUY  + price clears buy_level  -> ENTER BUY
           - armed SELL + price clears sell_level -> ENTER SELL
      3. Not armed yet? A break of the box ARMS the setup the SAME way:
           - price rises ABOVE mon_tue_high -> arm BUY  (broke up, may continue)
           - price drops BELOW mon_tue_low  -> arm SELL (broke down, may continue)
         If a single tick is already past the level, enter directly (no wait).

    Once armed we keep watching that direction until it enters or the week resets;
    we do not flip to the opposite arm (an armed setup stays valid until next Wed).
    """
    # 1. Terminal for the week once a real signal has fired.
    if state.fired_dir is not None:
        return state

    # 2. Armed -> watch for the entry (the 23.6% confirmation).
    if state.armed_dir == "BUY":
        if ltp >= levels.buy_level:
            return SignalState(armed_dir="BUY", fired_dir="BUY")
        return state
    if state.armed_dir == "SELL":
        if ltp <= levels.sell_level:
            return SignalState(armed_dir="SELL", fired_dir="SELL")
        return state

    # 3. Not armed yet -> a break of the box arms (or, if already past the level,
    #    enters straight away) in the SAME direction as the break.
    if ltp >= levels.buy_level:
        return SignalState(armed_dir="BUY", fired_dir="BUY")
    if ltp > levels.mon_tue_high:
        return SignalState(armed_dir="BUY")
    if ltp <= levels.sell_level:
        return SignalState(armed_dir="SELL", fired_dir="SELL")
    if ltp < levels.mon_tue_low:
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

    Same CONTINUATION rules as evaluate_signal, but the day's EXTREME does the
    crossing:
      * armed BUY  enters if the day's HIGH reached buy_level
      * armed SELL enters if the day's LOW  reached sell_level
      * not armed: a day whose HIGH cleared buy_level enters BUY directly (else a
        HIGH above the ceiling arms BUY); a LOW past sell_level enters SELL (else a
        LOW below the floor arms SELL).

    Unlike the old reversal reading, continuation needs no fake-then-reverse order,
    so a day that breaks out and clears the level can enter the same day — the day's
    High/Low touching the level IS the breakout. (If a wild outside day hits BOTH
    levels we can't tell the order, so we resolve BUY first; rare.)
    """
    # 1. Terminal for the week once a real signal has fired.
    if state.fired_dir is not None:
        return state

    # 2. Armed -> the day's extreme reaching the level confirms the entry.
    if state.armed_dir == "BUY":
        if high >= levels.buy_level:
            return SignalState(armed_dir="BUY", fired_dir="BUY")
        return state
    if state.armed_dir == "SELL":
        if low <= levels.sell_level:
            return SignalState(armed_dir="SELL", fired_dir="SELL")
        return state

    # 3. Not armed yet -> break of the box arms; clearing the level enters.
    if high >= levels.buy_level:
        return SignalState(armed_dir="BUY", fired_dir="BUY")
    if low <= levels.sell_level:
        return SignalState(armed_dir="SELL", fired_dir="SELL")
    if high > levels.mon_tue_high:
        return SignalState(armed_dir="BUY")
    if low < levels.mon_tue_low:
        return SignalState(armed_dir="SELL")

    return state


# ---------------------------------------------------------------------------
# PLAY 2 — the REVERSE breakout (the founder's signature — the WHOLE Excel)
# ---------------------------------------------------------------------------
#
# This is the play the founder actually taught (his student built weeklyalgo.xlsx
# from it). It is NOT the continuation play above — it's a failed-breakout reversal:
#
#   1. TRAP  — the first move runs all the way to T1 on one side, sucking in
#              breakout traders. A HIGH reaching buy_t1 is an UP trap; a LOW
#              reaching sell_t1 is a DOWN trap.
#   2. REVERSE + ENTER — the move fails and snaps back through the whole box; we
#              enter as it exits the OPPOSITE box edge:
#                UP trap   -> price falls back to the box LOW (L)  -> ENTER SELL
#                DOWN trap -> price rises back to the box HIGH (H) -> ENTER BUY
#   3. TARGET — the opposite side's T1/T2/T3 ladder.
#
# The trap must come FIRST and the entry AFTER — that ordering is the edge. We drive
# it off a day's High/Low (bhavcopy) and carry a tiny state (which trap has sprung).
#
# LEVELS NOTE: we use the TIGHTENED box the Excel shows — levels.H / levels.L and
# the tightened first target T1 = H +/- X/2 (on an inside day H/L/X are Wednesday's
# tighter range; otherwise they equal the Mon-Tue box). We deliberately do NOT use
# the full-box mon_tue_high / buy_t1 fields the continuation engine uses, so these
# numbers match weeklyalgo.xlsx exactly (360ONE: H 1198, L 1170.2, buy_t1 1211.9).


@dataclass(frozen=True)
class ReverseState:
    """A stock's position in the REVERSE-breakout state machine. Persist in the DB.
      faked_dir : "UP" | "DOWN" | None  — which side's T1 the fake move tagged (trap)
      fired_dir : "BUY" | "SELL" | None — which reverse entry has fired, if any
    An UP trap (faked up to buy_t1) reverses into a SELL; a DOWN trap (faked down to
    sell_t1) reverses into a BUY. Once fired, the stock is done for the week.
    """
    faked_dir: str | None = None
    fired_dir: str | None = None

    @property
    def status(self) -> str:
        if self.fired_dir == "BUY":
            return STATUS_BUY
        if self.fired_dir == "SELL":
            return STATUS_SELL
        if self.faked_dir == "DOWN":
            return STATUS_FAKED_DOWN
        if self.faked_dir == "UP":
            return STATUS_FAKED_UP
        return STATUS_NONE


def evaluate_day_reverse(levels: Levels, high: float, low: float,
                         state: ReverseState) -> ReverseState:
    """Advance the REVERSE-breakout state machine by one day's High/Low.

    Phase 1 — spring the trap: the first move must reach T1 on one side. A day whose
    HIGH tags buy_t1 sets an UP trap (a SELL is coming); a LOW tagging sell_t1 sets a
    DOWN trap (a BUY is coming).

    Phase 2 — take the reverse at the OPPOSITE box edge: once trapped UP, a LATER day
    whose LOW falls back to the box low (L) enters SELL; once trapped DOWN, a LATER
    day whose HIGH rises to the box high (H) enters BUY.

    Uses the Excel's tightened box (H, L) and its tightened first target
    (T1 = H +/- X/2). The trap must precede the entry, so we never fire on the same
    call that sets the trap; a wild outside day that tags both T1s just sets the UP
    trap and waits (conservative — we never invent a same-day reverse from daily data).
    """
    # The Excel's tightened first targets — the "T1" the fake move must tag.
    buy_t1 = levels.H + levels.X / 2      # up-side first target
    sell_t1 = levels.L - levels.X / 2     # down-side first target

    # Terminal for the week once the reverse has fired.
    if state.fired_dir is not None:
        return state

    # Phase 2 — trapped already: watch for the snap-back through the OPPOSITE box edge.
    if state.faked_dir == "UP":            # faked up to buy_t1 -> reversal is a SELL
        if low <= levels.L:
            return ReverseState(faked_dir="UP", fired_dir="SELL")
        return state
    if state.faked_dir == "DOWN":          # faked down to sell_t1 -> reversal is a BUY
        if high >= levels.H:
            return ReverseState(faked_dir="DOWN", fired_dir="BUY")
        return state

    # Phase 1 — no trap yet: the first move must reach T1 to spring it.
    if high >= buy_t1:
        return ReverseState(faked_dir="UP")
    if low <= sell_t1:
        return ReverseState(faked_dir="DOWN")
    return state


def evaluate_signal_reverse(levels: Levels, ltp: float, state: ReverseState) -> ReverseState:
    """Live-tick version of evaluate_day_reverse: advance the REVERSE-breakout state
    machine using ONE live price instead of a day's High/Low. Same two phases —
    a price that reaches T1 on one side springs the trap; once trapped, a price that
    snaps back to the OPPOSITE box edge fires the reverse entry. The daily High/Low
    replay stays the authoritative record; this just gives an intraday preview."""
    return evaluate_day_reverse(levels, high=ltp, low=ltp, state=state)


def reverse_targets(levels: Levels, direction: str) -> tuple[float, float, float, float]:
    """The reverse trade's (entry, T1, T2, T3) using the Excel's TIGHTENED box
    (H/L/X — Wednesday's tighter range on an inside day). The entry is the OPPOSITE
    box edge the reversal exits through; the targets ladder out from there:
      BUY  (a DOWN trap that reverses up): enter at H, targets H+X/2, H+X, H+2X
      SELL (an UP trap that reverses down): enter at L, targets L-X/2, L-X, L-2X
    These are the compressed inside-day levels the founder's play actually uses —
    NOT the full-box buy_t1/… fields (those belong to the retired continuation play)."""
    H, L, X = levels.H, levels.L, levels.X
    if direction == "BUY":
        return (round(H, _DP), round(H + X / 2, _DP), round(H + X, _DP), round(H + 2 * X, _DP))
    return (round(L, _DP), round(L - X / 2, _DP), round(L - X, _DP), round(L - 2 * X, _DP))


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


# ---------------------------------------------------------------------------
# EXTRA SCREENING FILTERS — straight from the original weeklyalgo.xlsx
# ---------------------------------------------------------------------------
# The Excel didn't just compute levels; it also GRADED each stock so the owner
# could decide which signals were worth trading. These four pure functions
# reproduce that grading exactly. None of them change a BUY/SELL — they're
# decoration the trader screens on.


def invest_tier(x: float | None, avg_x: float | None) -> str:
    """Classify this week's range (X) against the 3-week average range (avgX).
    Mirrors the Excel's three flags (Good invest / Invest / Breakout):

        x < avgX/2     -> "good"      strong compression (a coiled spring)
        x < avgX       -> "invest"    mild compression (below-average range)
        x > 1.5*avgX   -> "breakout"  expansion — an unusually WIDE week
        otherwise      -> "none"      a normal week, nothing to flag

    Tightest test wins, so "good" takes precedence over "invest". Returns
    "none" until we have 3 prior weeks of history to average."""
    if x is None or avg_x is None or avg_x <= 0:
        return "none"
    if x < avg_x / 2:
        return "good"
    if x < avg_x:
        return "invest"
    if x > avg_x * 1.5:
        return "breakout"
    return "none"


def fib_levels(mon_tue_high: float, mon_tue_low: float) -> tuple[float, float]:
    """The Excel's SECOND entry trigger: a 23.6% Fibonacci extension beyond the
    Mon-Tue box (cols N "BUY LEVEL" / V "SELL LEVEL"). Note it uses the FULL
    Mon-Tue range, not the inside-day-tightened X.

        fibBuy  = monTueHigh + 0.236 * (monTueHigh - monTueLow)
        fibSell = monTueLow  - 0.236 * (monTueHigh - monTueLow)
    """
    span = mon_tue_high - mon_tue_low
    ext = span * 0.236
    return round(mon_tue_high + ext, _DP), round(mon_tue_low - ext, _DP)


def candle_quality(day: OHLC) -> str:
    """The Excel's per-day "Good" vs "Volatile" flag (cols E/F/G): is the candle
    a decisive directional body, or mostly wick (indecision/chop)?

        |open - close| / (high - low) > 0.5  ->  "Good" (strong body)
        otherwise                            ->  "Volatile" (wicky / choppy)
    """
    rng = day.high - day.low
    if rng <= 0:
        return "Volatile"  # no range at all -> not a clean directional candle
    return "Good" if abs(day.open - day.close) / rng > 0.5 else "Volatile"


def volatility_pct(mon_tue_high: float, mon_tue_low: float) -> float:
    """The Excel's "XFH" (col Q): the Mon-Tue range as a PERCENT of price, so you
    can compare volatility across stocks of very different prices. A ₹5,000 stock
    with a ₹50 range (1%) is calmer than a ₹140 stock with a ₹10 range (7%).

        range / (price / 100)   where price = monTueHigh
    """
    if mon_tue_high <= 0:
        return 0.0
    return round((mon_tue_high - mon_tue_low) / (mon_tue_high / 100), _DP)

