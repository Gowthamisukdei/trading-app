r"""
main.py — the web server (FastAPI).

It's deliberately thin: every endpoint just calls the SignalService and returns
the result as JSON. All the real work lives in strategy.py (the brain) and
service.py (the glue). That separation means the trading logic is testable
without a web server, and the web layer is swappable without touching logic.

Run locally:
    venv\Scripts\activate
    uvicorn app.main:app --reload
Then open http://127.0.0.1:8000/docs for an interactive API explorer.
"""

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.scheduler import create_scheduler
from app.service import service

logging.basicConfig(level=logging.INFO)

# Which website origins may call this API from a browser. Default "*" (any) is
# fine while developing; in production set FRONTEND_ORIGINS to your Vercel URL
# (comma-separated if more than one) to lock it down.
_origins = os.getenv("FRONTEND_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _origins.split(",")] if _origins != "*" else ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Compute the initial weekly levels in a BACKGROUND thread, not inline: with
    # all ~211 F&O stocks on the real NSE provider this takes long enough that
    # doing it before the server is ready would make Railway time out the boot and
    # restart us in a 502 loop. Backgrounding it lets the server answer instantly;
    # /api/signals just returns whatever's already computed until this finishes.
    threading.Thread(
        target=service.run_weekly, name="initial-weekly", daemon=True
    ).start()

    # Start the background scheduler when the server boots, stop it on shutdown.
    # This is what makes the backend scan/compute on its own.
    scheduler = create_scheduler(service)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Weekly F&O Reversal Signals", version="0.1.0", lifespan=lifespan)

# Allow the Next.js dashboard (any origin for now) to call this API from the
# browser. We'll tighten this to the real frontend URL before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health():
    """Is the backend alive, and when did it last compute / scan? The dashboard
    shows this in a status strip so the trader knows data is fresh."""
    return service.health()


@app.get("/api/signals")
def signals():
    """The main feed: one row per stock with its status and target ladder."""
    return service.build_signals()


@app.get("/api/stock/{symbol}")
def stock_detail(symbol: str):
    """One stock's detail: raw Mon/Tue/Wed candles + combined Mon-Tue high/low +
    levels/ladders + current status. 404 if we have no data for it."""
    detail = service.build_stock_detail(symbol)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no data for {symbol}")
    return detail


@app.get("/api/history")
def history():
    """Every signal that ever fired, newest first, with whether it reached T3.
    This is the strategy's track record, shown on the History page."""
    return service.build_history()


@app.post("/api/scan-now")
def scan_now():
    """Force a live scan right now (advances every stock's state by one tick).
    Handy for demos and testing; the scheduler will call this automatically
    every 5 minutes during market hours later."""
    service.scan()
    return {"scanned": True, **service.health()}


@app.post("/api/run-weekly")
def run_weekly():
    """Force a weekly recompute (recomputes levels, clears armed/fired state)."""
    service.run_weekly()
    return {"weeklyRan": True, **service.health()}


@app.post("/api/seed-history")
def seed_history():
    """One-off backfill: fill the 4-week buffer with the last 3 weeks' REAL ranges
    so the Good invest / Invest / Breakout tiers work now instead of in ~3 weeks.
    Safe to call again; the current week is never disturbed."""
    summary = service.seed_history()
    return {**summary, **service.health()}


@app.post("/api/rebuild-state")
def rebuild_state():
    """One-off after an engine change: wipe stale signal state + history and rebuild
    from daily High/Low with the current (continuation) engine."""
    summary = service.rebuild_state()
    return {**summary, **service.health()}


@app.post("/api/replay-now")
def replay_now():
    """Force the daily backfill right now: re-read each closed day's High/Low since
    the levels' Wednesday and fold any arm/fire into state. Recovers setups that
    happened on a day the live scan wasn't running. Safe to call repeatedly."""
    service.replay_days()
    return {"replayed": True, **service.health()}
