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

# Which data source to use: "fake" (the 3 hardcoded demo stocks) or "nse" (the
# real NSE scraper). Defaults to fake so nothing breaks if the scraper has a bad
# day; flip to nse via TRADING_PROVIDER=nse once the scraper is verified.
PROVIDER: str = os.getenv("TRADING_PROVIDER", "fake").strip().lower()
