"""
config.py — runtime settings read from environment variables.

Keeping these in one place means behaviour can change per environment (local dev
vs Railway) without code edits.
"""

import os


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# DEV MODE: scan on a fast timer and IGNORE market hours, so we can watch the
# state machine advance live without waiting for a real trading day. NEVER set
# this in production — there the real market-hours gate must apply.
DEV_MODE: bool = _flag("TRADING_DEV")

# How often to scan in dev mode (seconds). In production the scan is every
# SCAN_MINUTES (below).
DEV_SCAN_SECONDS: int = int(os.getenv("TRADING_DEV_SCAN_SECONDS", "5"))

# How often the live scan runs in production, in minutes. Default 15: with ~211
# F&O stocks the scan makes one NSE call per stock (no bulk live endpoint), so a
# gentle interval avoids rate-limiting/blocks. A weekly swing strategy doesn't
# need finer resolution. Tune via TRADING_SCAN_MINUTES without a code change.
SCAN_MINUTES: int = int(os.getenv("TRADING_SCAN_MINUTES", "15"))

# When the weekly levels are computed (IST), every Wednesday. Default 18:30 — the
# strategy needs Wednesday's daily OHLC, which comes from NSE's end-of-day
# bhavcopy. That file is published AFTER the 15:30 close, usually not until the
# evening, so we wait until 18:30 to be sure it exists. Running later doesn't
# change WHAT we compute (it's all closed-day data) — only that the data is ready.
WEEKLY_HOUR: int = int(os.getenv("TRADING_WEEKLY_HOUR", "18"))
WEEKLY_MINUTE: int = int(os.getenv("TRADING_WEEKLY_MINUTE", "30"))

# When the DAILY replay runs (IST), every trading day. It re-reads each closed
# day's High/Low from the bhavcopy and folds any arm/fire into state — the
# authoritative backfill that the live snapshot scan can't do. Default 19:00, like
# the weekly compute, because that day's bhavcopy isn't published until the evening.
REPLAY_HOUR: int = int(os.getenv("TRADING_REPLAY_HOUR", "19"))
REPLAY_MINUTE: int = int(os.getenv("TRADING_REPLAY_MINUTE", "0"))

# Delay (milliseconds) between per-stock live-price calls within one scan. There's
# no bulk live endpoint, so tracking all ~211 F&O stocks means one NSE call each;
# a small gap spreads them into a trickle instead of a burst, so NSE doesn't see
# a flood and block our session. Default 0 (instant) for fake/dev/tests; set to
# ~300 in production when tracking the full list. At 300ms, 211 stocks take ~1 min
# per scan — fine on a 15-min interval.
SCAN_THROTTLE_MS: int = int(os.getenv("TRADING_SCAN_THROTTLE_MS", "0"))

# Use the ONE-call bulk live-price feed (market-data-pre-open?key=FO) instead of
# one option-chain call per symbol. Off by default: this endpoint is the pre-open
# snapshot, only safe for live signals if its prices actually move during
# continuous trading (verified via spikes/preopen_live_test.py). Once confirmed
# LIVE, set TRADING_BULK_LIVE=1 on Railway and the throttle/per-symbol path is no
# longer needed even for all 211 stocks (1 call covers them all). If the bulk
# fetch fails mid-scan, scan() falls back to the per-symbol get_live_price.
BULK_LIVE: bool = _flag("TRADING_BULK_LIVE")

# Which data source to use: "fake" (the 3 hardcoded demo stocks) or "nse" (the
# real NSE scraper). Defaults to fake so nothing breaks if the scraper has a bad
# day; flip to nse via TRADING_PROVIDER=nse once the scraper is verified.
PROVIDER: str = os.getenv("TRADING_PROVIDER", "fake").strip().lower()
