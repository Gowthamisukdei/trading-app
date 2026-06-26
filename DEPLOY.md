# Deploying the Weekly F&O Reversal Signal app

Two pieces go to two places (same stack as isukdei):

| Piece    | Folder      | Host    | What it is                |
| -------- | ----------- | ------- | ------------------------- |
| Backend  | `backend/`  | Railway | FastAPI + SQLite + scheduler |
| Frontend | `frontend/` | Vercel  | Next.js dashboard         |

The repo is a **monorepo**: one GitHub repo holds both folders, and each host is
told which sub-folder to build (its "root directory").

---

## Step 1 — Push to GitHub

1. Create a new **empty** repo on https://github.com/new — name it `trading-app`.
   Do **not** add a README/.gitignore there (we already have them).
2. Back here, connect and push (replace `<you>` with your GitHub username):

   ```bash
   git remote add origin https://github.com/<you>/trading-app.git
   git push -u origin main
   ```

   On Windows the Git Credential Manager will pop a browser to log you into
   GitHub the first time. That's normal.

---

## Step 2 — Backend on Railway

1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo** →
   pick `trading-app`.
2. Open the service → **Settings**:
   - **Root Directory** → `backend`
   - Railway auto-detects Python (from `requirements.txt`) and uses the
     `Procfile` start command. No manual build command needed.
3. **Add a Volume** (this is what keeps the database alive across redeploys):
   - Service → **Volumes** → New Volume → mount path **`/data`**.
4. **Variables** (Service → Variables), add:
   - `TRADING_DB_PATH` = `/data/signals.db`  ← puts SQLite on the volume
   - *(leave `TRADING_DEV` UNSET — dev mode must be off in production)*
5. **Settings → Networking → Generate Domain.** Copy the URL, e.g.
   `https://trading-app-production.up.railway.app`. Test it:
   open `<that-url>/api/health` — you should see JSON.

> ⚠️ Keep this at **1 instance** (don't enable horizontal scaling). SQLite is a
> single file; multiple instances would each have their own copy. To scale out
> later, that's the moment to move to Supabase Postgres (only `db.py` changes).

---

## Step 3 — Frontend on Vercel

1. Go to https://vercel.com → **Add New… → Project** → import `trading-app`.
2. **Root Directory** → `frontend`. Vercel auto-detects Next.js.
3. **Environment Variables**, add:
   - `NEXT_PUBLIC_API_BASE` = your Railway URL from Step 2.5
     (e.g. `https://trading-app-production.up.railway.app`)
4. **Deploy.** Open the Vercel URL — the dashboard should load and show "Backend
   live" once it reaches Railway.

---

## Step 4 — Lock down CORS (after you know the Vercel URL)

Back in Railway → Variables, add:

- `FRONTEND_ORIGINS` = your Vercel URL (e.g. `https://trading-app.vercel.app`)

Redeploy the backend. Now only your site can call the API from a browser.

---

## Reminders before real use

- **`NSE_HOLIDAYS`** in `backend/app/market_calendar.py` is empty — fill it from
  the official NSE holiday calendar so the scanner skips holidays.
- The app still runs on the **FakeProvider** (hardcoded demo data). The real NSE
  scraper is the last build step; swapping it in is one line in `service.py`.
- This is a **signal tool, not an auto-trader**, and not financial advice.
