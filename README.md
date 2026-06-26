# Weekly F&O Reversal Signal Website

Automatically tracks NSE F&O stocks, runs a weekly Monday–Tuesday–Wednesday
reversal strategy, and shows live BUY / SELL signals on a dashboard. Replaces a
manual Excel sheet.

## The strategy (two-stage reversal)

1. **Weekly levels** (computed Wednesday after close) from Mon+Tue, tightened by
   Wednesday if it's an inside day. The range `X` drives all targets.
2. **Arm:** price breaks the Mon-Tue *low* → armed for a BUY (or breaks the
   *high* → armed for a SELL).
3. **Trigger:** an armed setup that then crosses `T1` fires the actual signal,
   with profit targets `T2`/`T3`.

## Structure

```
backend/    FastAPI + strategy engine + SQLite + APScheduler   (→ Railway)
frontend/   Next.js dashboard                                  (→ Vercel)
```

The trading logic (`backend/app/strategy.py`) is pure and verified against the
Excel — see `backend/tests/`.

## Run locally

**Backend** (http://127.0.0.1:8000):
```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Demo mode (auto-scan every few seconds, ignores market hours):
```bash
TRADING_DEV=1 TRADING_DEV_SCAN_SECONDS=8 uvicorn app.main:app
```

**Frontend** (http://localhost:3000):
```bash
cd frontend
npm install
npm run dev
```

## Tests
```bash
cd backend
python -m tests.test_strategy
python -m tests.test_signal_engine
python -m tests.test_db
python -m tests.test_market
```

## Deploy
See [DEPLOY.md](./DEPLOY.md).

---
*Signal tool only — not an auto-trader, not financial advice.*
