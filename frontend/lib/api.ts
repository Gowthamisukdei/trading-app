// lib/api.ts — the single place the frontend talks to the backend.
// Every component imports from here, so if the API URL or shape changes we edit
// one file. The base URL comes from an env var so local dev and the deployed
// Vercel site can point at different backends without code changes.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

// Mirror of the JSON each /api/signals row returns (see backend service.py).
export type Status = "NONE" | "ARMED_BUY" | "ARMED_SELL" | "BUY" | "SELL";

export interface Signal {
  symbol: string;
  status: Status;
  ltp: number | null;
  monTueHigh: number;
  monTueLow: number;
  buyT1: number;
  buyT2: number;
  buyT3: number;
  sellT1: number;
  sellT2: number;
  sellT3: number;
  goodInvest: boolean;
  weekId: string;
}

// Mirror of each /api/history row (see backend service.build_history()).
// One per signal that ever fired, with whether price later reached T3.
export interface HistoryRow {
  id: number;
  symbol: string;
  signal: "BUY" | "SELL";
  entry: number;
  t1: number;
  t2: number;
  t3: number;
  weekId: string;
  firedAt: string;
  hitT3: boolean;
  resolvedAt: string | null;
}

// One day's candle, and the full stock-detail payload (see service.build_stock_detail).
export interface DayOHLC {
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface StockDetail {
  symbol: string;
  weekId: string;
  status: Status;
  ltp: number | null;
  days: { mon: DayOHLC; tue: DayOHLC; wed: DayOHLC };
  monTueHigh: number;
  monTueLow: number;
  wedInside: boolean;
  H: number;
  L: number;
  X: number;
  buyT1: number;
  buyT2: number;
  buyT3: number;
  sellT1: number;
  sellT2: number;
  sellT3: number;
  goodInvest: boolean;
}

export interface Health {
  ok: boolean;
  lastWeeklyRunAt: string | null;
  lastScanAt: string | null;
  providerStatus: string;
  trackedSymbols: number;
}

export async function getSignals(): Promise<Signal[]> {
  const res = await fetch(`${API_BASE}/api/signals`, { cache: "no-store" });
  if (!res.ok) throw new Error(`signals: ${res.status}`);
  return res.json();
}

export async function getHistory(): Promise<HistoryRow[]> {
  const res = await fetch(`${API_BASE}/api/history`, { cache: "no-store" });
  if (!res.ok) throw new Error(`history: ${res.status}`);
  return res.json();
}

export async function getStockDetail(symbol: string): Promise<StockDetail> {
  const res = await fetch(`${API_BASE}/api/stock/${encodeURIComponent(symbol)}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`stock ${symbol}: ${res.status}`);
  return res.json();
}

export async function getHealth(): Promise<Health> {
  const res = await fetch(`${API_BASE}/api/health`, { cache: "no-store" });
  if (!res.ok) throw new Error(`health: ${res.status}`);
  return res.json();
}

// Demo helper: force a live scan so we can watch the badges advance in-browser.
// The real scheduler will do this automatically every 5 min during market hours.
export async function scanNow(): Promise<void> {
  await fetch(`${API_BASE}/api/scan-now`, { method: "POST" });
}
