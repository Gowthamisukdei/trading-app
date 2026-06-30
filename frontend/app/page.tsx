"use client";

// The dashboard. A client component because it polls the backend on a timer and
// holds live state. It does three things:
//   1. fetch /api/signals + /api/health every few seconds
//   2. let the trader filter (all / signals / armed / search)
//   3. render one colour-coded row per stock
// All backend talk goes through lib/api.ts.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getHealth,
  getSignals,
  scanNow,
  type Candles,
  type Health,
  type Quality,
  type Signal,
  type Status,
} from "@/lib/api";

const REFRESH_MS = 5000;

// Visual style per status. Tailwind classes for the badge pill.
const STATUS_STYLE: Record<Status, { label: string; cls: string }> = {
  BUY: { label: "BUY", cls: "bg-green-500/15 text-green-400 ring-green-500/30" },
  SELL: { label: "SELL", cls: "bg-red-500/15 text-red-400 ring-red-500/30" },
  ARMED_BUY: { label: "Waiting for BUY", cls: "bg-amber-500/15 text-amber-400 ring-amber-500/30" },
  ARMED_SELL: { label: "Waiting for SELL", cls: "bg-amber-500/15 text-amber-400 ring-amber-500/30" },
  NONE: { label: "—", cls: "bg-zinc-700/30 text-zinc-400 ring-zinc-600/30" },
};

// Volatility-tier badge (the Excel's Good invest / Invest / Breakout flags).
const QUALITY_STYLE: Record<Quality, { label: string; cls: string } | null> = {
  good: { label: "Good invest", cls: "bg-sky-500/15 text-sky-400 ring-sky-500/30" },
  invest: { label: "Invest", cls: "bg-teal-500/15 text-teal-400 ring-teal-500/30" },
  breakout: { label: "Breakout", cls: "bg-orange-500/15 text-orange-400 ring-orange-500/30" },
  none: null,
};

// Mon/Tue/Wed candle-quality dots: green = Good (decisive body), grey = Volatile.
function CandleDots({ candles }: { candles: Candles | null }) {
  if (!candles) return <span className="text-zinc-600">—</span>;
  const days: [string, string][] = [
    ["M", candles.mon],
    ["T", candles.tue],
    ["W", candles.wed],
  ];
  return (
    <span className="inline-flex gap-1" title="Mon / Tue / Wed candle quality">
      {days.map(([d, q]) => (
        <span
          key={d}
          className={`inline-flex h-5 w-5 items-center justify-center rounded text-[10px] font-semibold ${
            q === "Good"
              ? "bg-emerald-500/20 text-emerald-400"
              : "bg-zinc-700/40 text-zinc-500"
          }`}
          title={`${d}: ${q}`}
        >
          {d}
        </span>
      ))}
    </span>
  );
}

type Filter = "all" | "signals" | "armed";

// Pick the target ladder that matters for this row's direction.
function ladder(s: Signal): { entry: number; t2: number; t3: number } | null {
  if (s.status === "BUY" || s.status === "ARMED_BUY")
    return { entry: s.buyT1, t2: s.buyT2, t3: s.buyT3 };
  if (s.status === "SELL" || s.status === "ARMED_SELL")
    return { entry: s.sellT1, t2: s.sellT2, t3: s.sellT3 };
  return null;
}

function fmt(n: number | null): string {
  return n == null
    ? "—"
    : n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.floor(secs)}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  return `${Math.floor(secs / 3600)}h ago`;
}

export default function Dashboard() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [scanning, setScanning] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, h] = await Promise.all([getSignals(), getHealth()]);
      setSignals(s);
      setHealth(h);
      setError(null);
    } catch {
      setError(
        "Can't reach the backend. Is it running on " +
          (process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000") +
          " ?"
      );
    }
  }, []);

  // Poll on mount and every REFRESH_MS.
  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const handleScan = async () => {
    setScanning(true);
    await scanNow();
    await refresh();
    setScanning(false);
  };

  const rows = useMemo(() => {
    return signals
      .filter((s) => {
        if (filter === "signals") return s.status === "BUY" || s.status === "SELL";
        if (filter === "armed") return s.status === "ARMED_BUY" || s.status === "ARMED_SELL";
        return true;
      })
      .filter((s) => s.symbol.toLowerCase().includes(query.toLowerCase()));
  }, [signals, filter, query]);

  const counts = useMemo(() => {
    const live = signals.filter((s) => s.status === "BUY" || s.status === "SELL").length;
    const armed = signals.filter(
      (s) => s.status === "ARMED_BUY" || s.status === "ARMED_SELL"
    ).length;
    return { live, armed };
  }, [signals]);

  return (
    <main className="mx-auto w-full max-w-6xl px-5 py-8 text-zinc-100">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Weekly F&amp;O Reversal Signals
          </h1>
          <p className="mt-1 text-sm text-zinc-400">
            Mon-Tue level broken → armed; price hits T1 → signal fires.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Link
            href="/history"
            className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800"
          >
            History →
          </Link>
          <button
            onClick={handleScan}
            disabled={scanning}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {scanning ? "Scanning…" : "Scan now"}
          </button>
        </div>
      </div>

      {/* Health strip */}
      <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3 text-sm">
        <span className="flex items-center gap-2">
          <span
            className={`h-2.5 w-2.5 rounded-full ${
              health == null ? "bg-zinc-500" : health.ok ? "bg-green-400" : "bg-red-400"
            }`}
          />
          {health == null ? "Connecting…" : health.ok ? "Backend live" : "Backend down"}
        </span>
        <span className="text-zinc-400">
          Source: <span className="text-zinc-200">{health?.providerStatus ?? "—"}</span>
        </span>
        <span className="text-zinc-400">
          Tracking: <span className="text-zinc-200">{health?.trackedSymbols ?? 0}</span>
        </span>
        <span className="text-zinc-400">
          Last scan: <span className="text-zinc-200">{timeAgo(health?.lastScanAt ?? null)}</span>
        </span>
        <span className="text-zinc-400">
          Week: <span className="text-zinc-200">{signals[0]?.weekId ?? "—"}</span>
        </span>
        <span className="ml-auto text-zinc-400">
          <span className="text-green-400">{counts.live}</span> live ·{" "}
          <span className="text-amber-400">{counts.armed}</span> armed
        </span>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Controls */}
      <div className="mt-5 flex flex-wrap items-center gap-2">
        {(["all", "signals", "armed"] as Filter[]).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`rounded-lg px-3 py-1.5 text-sm capitalize ring-1 ${
              filter === f
                ? "bg-zinc-100 text-zinc-900 ring-zinc-100"
                : "bg-zinc-900 text-zinc-300 ring-zinc-700 hover:bg-zinc-800"
            }`}
          >
            {f === "all" ? "All" : f}
          </button>
        ))}
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search symbol…"
          className="ml-auto rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-1.5 text-sm placeholder-zinc-500 focus:border-zinc-500 focus:outline-none"
        />
      </div>

      {/* Table */}
      <div className="mt-4 overflow-hidden rounded-xl border border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-900/80 text-left text-zinc-400">
            <tr>
              <th className="px-4 py-3 font-medium">Symbol</th>
              <th className="px-4 py-3 font-medium">Status</th>
              <th className="px-4 py-3 text-right font-medium">LTP</th>
              <th className="px-4 py-3 text-right font-medium">Mon-Tue range</th>
              <th className="px-4 py-3 text-right font-medium">Entry (T1)</th>
              <th className="px-4 py-3 text-right font-medium">T2</th>
              <th className="px-4 py-3 text-right font-medium">T3</th>
              <th className="px-4 py-3 text-right font-medium" title="Mon-Tue range as % of price">Vol %</th>
              <th className="px-4 py-3 font-medium" title="Mon / Tue / Wed candle quality">Candles</th>
              <th className="px-4 py-3 font-medium">Quality</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {rows.map((s) => {
              const st = STATUS_STYLE[s.status];
              const lad = ladder(s);
              // A target is "hit" once the live price reaches it: a BUY ladder
              // climbs (ltp >= target), a SELL ladder falls (ltp <= target).
              const dir =
                s.status === "BUY" || s.status === "ARMED_BUY"
                  ? "buy"
                  : s.status === "SELL" || s.status === "ARMED_SELL"
                  ? "sell"
                  : null;
              const hit = (target: number) =>
                lad != null &&
                s.ltp != null &&
                dir != null &&
                (dir === "buy" ? s.ltp >= target : s.ltp <= target);
              // Hit target = bright like T1; not-yet-hit = dimmed.
              const tgtCls = (target: number) =>
                hit(target) ? "text-zinc-100" : "text-zinc-400";
              return (
                <tr key={s.symbol} className="hover:bg-zinc-900/40">
                  <td className="px-4 py-3 font-medium">
                    <Link href={`/stock/${s.symbol}`} className="hover:text-indigo-400 hover:underline">
                      {s.symbol}
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${st.cls}`}
                    >
                      {st.label}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">{fmt(s.ltp)}</td>
                  <td className="px-4 py-3 text-right tabular-nums text-zinc-400">
                    {fmt(s.monTueLow)} – {fmt(s.monTueHigh)}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">{lad ? fmt(lad.entry) : "—"}</td>
                  <td className={`px-4 py-3 text-right tabular-nums ${lad ? tgtCls(lad.t2) : "text-zinc-400"}`}>
                    {lad ? fmt(lad.t2) : "—"}
                  </td>
                  <td className={`px-4 py-3 text-right tabular-nums ${lad ? tgtCls(lad.t3) : "text-zinc-400"}`}>
                    {lad ? fmt(lad.t3) : "—"}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-zinc-400">
                    {s.volPct.toFixed(1)}%
                  </td>
                  <td className="px-4 py-3">
                    <CandleDots candles={s.candles} />
                  </td>
                  <td className="px-4 py-3">
                    {QUALITY_STYLE[s.quality] && (
                      <span
                        className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${
                          QUALITY_STYLE[s.quality]!.cls
                        }`}
                      >
                        {QUALITY_STYLE[s.quality]!.label}
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={10} className="px-4 py-10 text-center text-zinc-500">
                  No stocks match this filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="mt-4 text-xs text-zinc-500">
        Auto-refreshes every {REFRESH_MS / 1000}s. Signal tool only — not financial advice.
      </p>
    </main>
  );
}
