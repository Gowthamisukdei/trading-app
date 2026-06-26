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

# How often to scan in dev mode (seconds). In production the scan is every 5 min.
DEV_SCAN_SECONDS: int = int(os.getenv("TRADING_DEV_SCAN_SECONDS", "5"))
