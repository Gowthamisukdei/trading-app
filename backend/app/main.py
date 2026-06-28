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
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
