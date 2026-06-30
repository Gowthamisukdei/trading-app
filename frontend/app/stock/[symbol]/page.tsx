"use client";

// Stock detail page (/stock/[symbol]). Shows the RAW Monday/Tuesday/Wednesday
// candles that fed this week's levels, the combined Mon-Tue high/low, the
// inside-day tightening, and the full target ladder — like one row of the Excel
// expanded. The symbol comes from the URL via useParams (Next 16 client hook).

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  getStockDetail,
  type Candle,
  type DayOHLC,
  type Quality,
  type StockDetail,
  type Status,
} from "@/lib/api";

const STATUS_STYLE: Record<Status, { label: string; cls: string }> = {
  BUY: { label: "BUY", cls: "bg-green-500/15 text-green-400 ring-green-500/30" },
  SELL: { label: "SELL", cls: "bg-red-500/15 text-red-400 ring-red-500/30" },
  ARMED_BUY: { label: "Waiting for BUY", cls: "bg-amber-500/15 text-amber-400 ring-amber-500/30" },
  ARMED_SELL: { label: "Waiting for SELL", cls: "bg-amber-500/15 text-amber-400 ring-amber-500/30" },
  NONE: { label: "—", cls: "bg-zinc-700/30 text-zinc-400 ring-zinc-600/30" },
};

const QUALITY_STYLE: Record<Quality, { label: string; cls: string } | null> = {
  good: { label: "Good invest", cls: "bg-sky-500/15 text-sky-400 ring-sky-500/30" },
  invest: { label: "Invest", cls: "bg-teal-500/15 text-teal-400 ring-teal-500/30" },
  breakout: { label: "Breakout", cls: "bg-orange-500/15 text-orange-400 ring-orange-500/30" },
  none: null,
};

// A small pill for a single day's candle grade.
function CandleBadge({ q }: { q: Candle | undefined }) {
  if (!q) return <span className="text-zinc-600">—</span>;
  return (
    <span
      className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${
        q === "Good" ? "bg-emerald-500/20 text-emerald-400" : "bg-zinc-700/40 text-zinc-400"
      }`}
    >
      {q}
    </span>
  );
}

function fmt(n: number | null): string {
  return n == null
    ? "—"
    : n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function DayRow({
  label,
  d,
  quality,
  highlight,
}: {
  label: string;
  d: DayOHLC;
  quality?: Candle;
  highlight?: boolean;
}) {
  return (
    <tr className={highlight ? "bg-zinc-900/40" : undefined}>
      <td className="px-4 py-3 font-medium">{label}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(d.open)}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(d.high)}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(d.low)}</td>
      <td className="px-4 py-3 text-right tabular-nums">{fmt(d.close)}</td>
      <td className="px-4 py-3 text-right">
        <CandleBadge q={quality} />
      </td>
    </tr>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 px-4 py-3">
      <div className="text-xs text-zinc-500">{label}</div>
      <div className={`mt-1 text-lg font-semibold tabular-nums ${tone ?? "text-zinc-100"}`}>{value}</div>
    </div>
  );
}

export default function StockPage() {
  const params = useParams<{ symbol: string }>();
  const symbol = (params.symbol ?? "").toUpperCase();

  const [data, setData] = useState<StockDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await getStockDetail(symbol));
      setError(null);
    } catch {
      setError(`No data for ${symbol} yet. Run the weekly compute, then come back.`);
    }
  }, [symbol]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, [refresh]);

  const st = data ? STATUS_STYLE[data.status] : null;

  return (
    <main className="mx-auto w-full max-w-4xl px-5 py-8 text-zinc-100">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{symbol}</h1>
          <p className="mt-1 text-sm text-zinc-400">
            Week {data?.weekId ?? "—"} · the daily candles behind this week&apos;s levels.
          </p>
        </div>
        <Link
          href="/"
          className="rounded-lg border border-zinc-700 bg-zinc-900 px-4 py-2 text-sm font-medium text-zinc-200 hover:bg-zinc-800"
        >
          ← Dashboard
        </Link>
      </div>

      {error && (
        <div className="mt-5 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {data && st && (
        <>
          {/* status + ltp strip */}
          <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-zinc-800 bg-zinc-900/50 px-4 py-3 text-sm">
            <span className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${st.cls}`}>
              {st.label}
            </span>
            <span className="text-zinc-400">
              LTP: <span className="text-zinc-200 tabular-nums">{fmt(data.ltp)}</span>
            </span>
            {QUALITY_STYLE[data.quality] && (
              <span
                className={`inline-flex rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${
                  QUALITY_STYLE[data.quality]!.cls
                }`}
              >
                {QUALITY_STYLE[data.quality]!.label}
              </span>
            )}
            <span className="text-zinc-400">
              Vol: <span className="text-zinc-200 tabular-nums">{data.volPct.toFixed(1)}%</span>
            </span>
          </div>

          {/* the raw daily candles */}
          <h2 className="mt-7 text-sm font-medium text-zinc-400">Daily candles</h2>
          <div className="mt-2 overflow-hidden rounded-xl border border-zinc-800">
            <table className="w-full text-sm">
              <thead className="bg-zinc-900/80 text-left text-zinc-400">
                <tr>
                  <th className="px-4 py-3 font-medium">Day</th>
                  <th className="px-4 py-3 text-right font-medium">Open</th>
                  <th className="px-4 py-3 text-right font-medium">High</th>
                  <th className="px-4 py-3 text-right font-medium">Low</th>
                  <th className="px-4 py-3 text-right font-medium">Close</th>
                  <th className="px-4 py-3 text-right font-medium" title="Body vs range: a decisive candle or a wicky/choppy one">Candle</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-800">
                <DayRow label="Monday" d={data.days.mon} quality={data.candles?.mon} />
                <DayRow label="Tuesday" d={data.days.tue} quality={data.candles?.tue} />
                <DayRow label="Wednesday" d={data.days.wed} quality={data.candles?.wed} highlight={data.wedInside} />
              </tbody>
            </table>
          </div>
          {data.wedInside && (
            <p className="mt-2 text-xs text-amber-400/90">
              Wednesday was an inside day (traded fully within the Mon-Tue box) → levels tightened to
              Wednesday&apos;s high/low.
            </p>
          )}

          {/* combined Mon-Tue + derived levels */}
          <h2 className="mt-7 text-sm font-medium text-zinc-400">Combined &amp; levels</h2>
          <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Stat label="Mon-Tue High" value={fmt(data.monTueHigh)} tone="text-green-400" />
            <Stat label="Mon-Tue Low" value={fmt(data.monTueLow)} tone="text-red-400" />
            <Stat label="Range X" value={fmt(data.X)} />
            <Stat label="H (ceiling)" value={fmt(data.H)} />
            <Stat label="L (floor)" value={fmt(data.L)} />
            <Stat label="Avg X (3 wk)" value={data.avgX == null ? "—" : fmt(data.avgX)} />
          </div>

          {/* ladders */}
          <h2 className="mt-7 text-sm font-medium text-zinc-400">Target ladders</h2>
          <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="rounded-xl border border-green-500/20 bg-green-500/5 px-4 py-3">
              <div className="text-xs font-medium text-green-400">BUY (break up above H)</div>
              <div className="mt-2 flex justify-between text-sm tabular-nums text-zinc-200">
                <span>T1 {fmt(data.buyT1)}</span>
                <span>T2 {fmt(data.buyT2)}</span>
                <span>T3 {fmt(data.buyT3)}</span>
              </div>
            </div>
            <div className="rounded-xl border border-red-500/20 bg-red-500/5 px-4 py-3">
              <div className="text-xs font-medium text-red-400">SELL (break down below L)</div>
              <div className="mt-2 flex justify-between text-sm tabular-nums text-zinc-200">
                <span>T1 {fmt(data.sellT1)}</span>
                <span>T2 {fmt(data.sellT2)}</span>
                <span>T3 {fmt(data.sellT3)}</span>
              </div>
            </div>
          </div>

          {/* Fibonacci alternate entry levels (the Excel's BUY/SELL LEVEL cols) */}
          <h2 className="mt-7 text-sm font-medium text-zinc-400">
            Fib breakout levels (23.6% beyond the Mon-Tue box)
          </h2>
          <div className="mt-2 grid grid-cols-2 gap-3">
            <Stat label="Fib BUY level" value={fmt(data.fibBuy)} tone="text-green-400" />
            <Stat label="Fib SELL level" value={fmt(data.fibSell)} tone="text-red-400" />
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            A second, shallower breakout trigger from the original Excel: 23.6% of the full
            Mon-Tue range projected above the high / below the low.
          </p>
        </>
      )}

      <p className="mt-7 text-xs text-zinc-500">
        Auto-refreshes every 10s. Signal tool only — not financial advice.
      </p>
    </main>
  );
}
