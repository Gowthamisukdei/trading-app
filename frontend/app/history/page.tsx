"use client";

// The History page (/history). A client component that fetches the permanent
// signal log and shows it newest-first. The point of this page is the track
// record: for every BUY/SELL that fired, did price later reach T3 (the final
// profit target)? A ✅ means the strategy fully worked that time; ⏳ means the
// trade is still open / hasn't reached T3 yet.
// All backend talk goes through lib/api.ts.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getHistory, type HistoryRow } from "@/lib/api";

const REFRESH_MS = 10000;

function fmt(n: number | null): string {
  return n == null
    ? "—"
    : n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Show a fired-at timestamp as a short local date + time (IST on his machine).
function when(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function History() {
  const [rows, setRows] = useState<HistoryRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRows(await getHistory());
      setError(null);
    } catch {
      setError(
        "Can't reach the backend. Is it running on " +
          (process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000") +
          " ?"
      );
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const stats = useMemo(() => {
    const total = rows.length;
    const won = rows.filter((r) => r.hitT3).length;
    return { total, won, open: total - won };
  }, [rows]);

  return (
    <main className="mx-auto w-full max-w-6xl px-5 py-8 text-zinc-100">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Signal history</h1>
          <p className="mt-1 text-sm text-zinc-400">
            Every signal that fired, and whether price reached T3.
          </p>
        </div>
        <Link
          href="/"
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800"
        >
          ← Dashboard
        </Link>
      </div>

      {/* Summary strip */}
      <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3 text-sm">
        <span className="text-zinc-400">
          Total fired: <span className="text-zinc-200">{stats.total}</span>
        </span>
        <span className="text-zinc-400">
          Reached T3: <span className="text-green-400">{stats.won}</span>
        </span>
        <span className="text-zinc-400">
          Still open: <span className="text-amber-400">{stats.open}</span>
        </span>
      </div>

      {error && (
        <div className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="mt-4 overflow-hidden rounded-xl border border-zinc-800">
        <table className="w-full text-sm">
          <thead className="bg-zinc-900/80 text-left text-zinc-400">
            <tr>
              <th className="px-4 py-3 font-medium">Symbol</th>
              <th className="px-4 py-3 font-medium">Signal</th>
              <th className="px-4 py-3 font-medium">Fired</th>
              <th className="px-4 py-3 text-right font-medium" title="BUY/SELL LEVEL">Entry</th>
              <th className="px-4 py-3 text-right font-medium">T1</th>
              <th className="px-4 py-3 text-right font-medium">T2</th>
              <th className="px-4 py-3 text-right font-medium">T3</th>
              <th className="px-4 py-3 font-medium">Week</th>
              <th className="px-4 py-3 font-medium">Result</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {rows.map((r) => (
              <tr key={r.id} className="hover:bg-zinc-900/40">
                <td className="px-4 py-3 font-medium">{r.symbol}</td>
                <td className="px-4 py-3">
                  <span
                    className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${
                      r.signal === "BUY"
                        ? "bg-green-500/15 text-green-400 ring-green-500/30"
                        : "bg-red-500/15 text-red-400 ring-red-500/30"
                    }`}
                  >
                    {r.signal}
                  </span>
                </td>
                <td className="px-4 py-3 text-zinc-400">{when(r.firedAt)}</td>
                <td className="px-4 py-3 text-right tabular-nums">{fmt(r.entry)}</td>
                <td className="px-4 py-3 text-right tabular-nums text-zinc-400">{fmt(r.t1)}</td>
                <td className="px-4 py-3 text-right tabular-nums text-zinc-400">{fmt(r.t2)}</td>
                <td className="px-4 py-3 text-right tabular-nums text-zinc-400">{fmt(r.t3)}</td>
                <td className="px-4 py-3 text-zinc-400">{r.weekId}</td>
                <td className="px-4 py-3">
                  {r.hitT3 ? (
                    <span className="inline-flex rounded-full bg-green-500/15 px-2.5 py-1 text-xs font-medium text-green-400 ring-1 ring-green-500/30">
                      ✓ Hit T3
                    </span>
                  ) : (
                    <span className="inline-flex rounded-full bg-zinc-700/30 px-2.5 py-1 text-xs font-medium text-zinc-400 ring-1 ring-zinc-600/30">
                      ⏳ Open
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={9} className="px-4 py-10 text-center text-zinc-500">
                  No signals have fired yet. When one does, it shows up here.
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
