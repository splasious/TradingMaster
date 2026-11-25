"use client";

/**
 * OI Change Buy 15 MIN's change in OI (24 Nov), under its card: each
 * 15-minute candle's Put and Call ΔOI as bars, the average the strategy
 * decides on (version 6: the last 2 candles) as two lines, and the side
 * that average picks at each close it reads (09:30-15:15) as a strip.
 * The latest session, and the candles before it that its first averages
 * use (shaded). Read-only; re-read every 30 s.
 */

import { useEffect, useRef, useState } from "react";

import { useOiChange } from "@/lib/hooks";
import type { OiChangeCandleOut } from "@/lib/types";

const PUT = "var(--series-put)";
const CALL = "var(--series-call)";
const PAD_L = 52;
const PAD_R = 12;

const hhmm = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const dayLabel = (d: string) => new Date(`${d}T00:00:00+05:30`).toLocaleDateString("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" });
const lakh = (v: number) =>
  `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v / 1e5).toLocaleString("en-IN", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} L`;

function niceTicks(lo: number, hi: number, count: number): number[] {
  const span = hi - lo || 1;
  const mag = Math.pow(10, Math.floor(Math.log10(span / count)));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => span / s <= count) ?? 10 * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

export function OiChangeChart({ deploymentId }: { deploymentId: string }) {
  const { data, isLoading, isError } = useOiChange(deploymentId, true);
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(300, el.clientWidth)));
    ro.observe(el);
    setWidth(Math.max(300, el.clientWidth));
    return () => ro.disconnect();
  }, [data?.candles.length]);

  const candles: OiChangeCandleOut[] = data?.candles ?? [];
  const n = candles.length;
  const W = width;
  const H_BARS = W < 520 ? 150 : 180;
  const STRIP_Y = 8 + H_BARS + 14;
  const H = STRIP_Y + 16 + 24;
  const band = n ? (W - PAD_L - PAD_R) / n : 1;
  const x = (i: number) => PAD_L + band * i + band / 2;
  const values = candles.flatMap((c) => [c.put_change, c.call_change, c.put_avg, c.call_avg]);
  const lo0 = Math.min(0, ...values);
  const hi0 = Math.max(0, ...values);
  const pad = (hi0 - lo0) * 0.06 || 1;
  const lo = lo0 - pad;
  const hi = hi0 + pad;
  const y = (v: number) => 8 + H_BARS * (1 - (v - lo) / (hi - lo));
  const bw = Math.max(1.5, (band * 0.78 - 2) / 2);
  const session = data?.session ?? null;
  const firstToday = candles.findIndex((c) => c.session === session);
  const path = (key: "put_avg" | "call_avg") => candles.map((c, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(c[key]).toFixed(1)}`).join("");
  const step = W < 520 ? 4 : 2;
  const h = hover !== null ? candles[hover] : null;

  return (
    <div className="space-y-2" aria-label="Change in OI">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h4 className="text-sm font-semibold text-text-primary">Change in OI, 15-minute candles</h4>
        {data && (
          <span className="text-xs text-text-secondary">
            side from the last {data.average_candles} candles&apos; average{session ? ` · ${dayLabel(session)}` : ""}
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-2.5 rounded-sm opacity-45" style={{ background: PUT }} />Put ΔOI</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-2.5 rounded-sm opacity-45" style={{ background: CALL }} />Call ΔOI</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-0.5 w-4 rounded" style={{ background: PUT }} />Put average</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-0.5 w-4 rounded" style={{ background: CALL }} />Call average</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-3.5 rounded-sm opacity-35" style={{ background: PUT }} />CE</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-3.5 rounded-sm opacity-35" style={{ background: CALL }} />PE</span>
      </div>
      {isLoading && <p className="text-xs text-text-muted">Reading the change-in-OI records…</p>}
      {isError && <p className="text-xs text-negative">The change-in-OI records couldn&apos;t be read just now; trying again in 30 seconds.</p>}
      {data && n === 0 && <p className="text-xs text-text-muted">No 15-minute change-in-OI record yet.</p>}
      <div ref={box} className="relative w-full">
        {n > 0 && (
          <svg
            viewBox={`0 0 ${W} ${H}`}
            className="block h-auto w-full overflow-visible"
            role="img"
            aria-label={`Put and Call change in OI every 15 minutes with their ${data?.average_candles}-candle averages and the side picked at each close`}
            onMouseLeave={() => setHover(null)}
          >
            {firstToday > 0 && (
              <rect x={PAD_L} y={4} width={band * firstToday} height={STRIP_Y + 16 - 4} fill="var(--surface-elevated)" opacity={0.7} />
            )}
            {niceTicks(lo, hi, 4).map((v) => (
              <g key={v}>
                <line x1={PAD_L} x2={W - PAD_R} y1={y(v)} y2={y(v)} stroke={v === 0 ? "var(--border-strong)" : "var(--border)"} strokeWidth={1} />
                <text x={PAD_L - 6} y={y(v) + 3.5} textAnchor="end" fontSize={10.5} fill="var(--text-muted)" className="font-financial">
                  {(v / 1e5).toLocaleString("en-IN")}
                </text>
              </g>
            ))}
            <text x={PAD_L - 6} y={y(hi) - 0} textAnchor="end" fontSize={10} fill="var(--text-muted)" dy={-2}>lakh</text>
            {candles.map((c, i) =>
              ([[c.put_change, PUT, -1], [c.call_change, CALL, 1]] as const).map(([v, color, s]) => (
                <rect
                  key={`${c.at}${s}`}
                  x={s < 0 ? x(i) - 1 - bw : x(i) + 1}
                  y={Math.min(y(0), y(v))}
                  width={bw}
                  height={Math.max(0.5, Math.abs(y(v) - y(0)))}
                  rx={Math.min(2, bw / 2)}
                  fill={color}
                  fillOpacity={0.42}
                />
              )),
            )}
            {(["put_avg", "call_avg"] as const).map((key) => (
              <g key={key}>
                <path d={path(key)} fill="none" stroke="var(--surface)" strokeWidth={5} strokeLinejoin="round" />
                <path d={path(key)} fill="none" stroke={key === "put_avg" ? PUT : CALL} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              </g>
            ))}
            {candles.map((c, i) =>
              c.side ? (
                <g key={`s${c.at}`}>
                  <rect x={PAD_L + band * i + 1} y={STRIP_Y} width={Math.max(1, band - 2)} height={14} rx={2} fill={c.side === "CE" ? PUT : CALL} fillOpacity={0.32} />
                  {band >= 15 && (i === 0 || candles[i - 1].side !== c.side) && (
                    <text x={x(i)} y={STRIP_Y + 10.5} textAnchor="middle" fontSize={9.5} fill="var(--text-primary)">{c.side === "CE" ? "C" : "P"}</text>
                  )}
                </g>
              ) : null,
            )}
            <text x={PAD_L - 6} y={STRIP_Y + 10.5} textAnchor="end" fontSize={10.5} fill="var(--text-secondary)">side</text>
            {candles.map((c, i) =>
              i % step === 0 || i === n - 1 ? (
                <text key={`t${c.at}`} x={x(i)} y={H - 6} textAnchor="middle" fontSize={10.5} fill="var(--text-muted)" className="font-financial">
                  {i < firstToday && i === 0 ? dayLabel(c.session) : hhmm(c.at)}
                </text>
              ) : null,
            )}
            {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={4} y2={STRIP_Y + 16} stroke="var(--border-strong)" strokeWidth={1} />}
            {candles.map((c, i) => (
              <rect key={`h${c.at}`} x={PAD_L + band * i} y={0} width={band} height={H} fill="transparent" onMouseEnter={() => setHover(i)} onClick={() => setHover(i)} />
            ))}
          </svg>
        )}
        {h && hover !== null && (
          <div
            className="pointer-events-none absolute top-2 z-10 min-w-[190px] rounded-md border border-border bg-surface-elevated p-2 text-xs shadow-md"
            style={
              W < 520
                ? x(hover) / W > 0.5 ? { left: 0 } : { right: 0 } // narrow: on the side away from the finger, inside the card
                : x(hover) / W > 0.6 ? { right: `${(1 - x(hover) / W) * 100 + 2}%` } : { left: `${(x(hover) / W) * 100 + 2}%` }
            }
          >
            <div className="mb-1 font-semibold text-text-primary">
              {h.session !== session ? `${dayLabel(h.session)} · ` : ""}candle to {hhmm(h.at)}
            </div>
            {h.spot !== null && (
              <div className="flex justify-between gap-3"><span className="text-text-secondary">NIFTY</span><span className="font-financial">{h.spot.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</span></div>
            )}
            <div className="flex justify-between gap-3"><span className="text-text-secondary">Put ΔOI</span><span className="font-financial">{lakh(h.put_change)}</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-secondary">Call ΔOI</span><span className="font-financial">{lakh(h.call_change)}</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-secondary">Put average</span><span className="font-financial">{lakh(h.put_avg)}</span></div>
            <div className="flex justify-between gap-3"><span className="text-text-secondary">Call average</span><span className="font-financial">{lakh(h.call_avg)}</span></div>
            <div className="mt-1 flex justify-between gap-3"><span className="text-text-secondary">Side</span><span className="font-semibold">{h.side ?? (h.session !== session ? "the day before" : hhmm(h.at) === "15:30" ? "no decision; held overnight" : "no change")}</span></div>
          </div>
        )}
      </div>
      {data && n > 0 && (
        <p className="text-xs text-text-muted">
          At each close from 09:30 to 15:15 the strategy holds the CE while the put average is higher and the PE while the call average is; the shaded candles are the session before, which the first averages use.
        </p>
      )}
    </div>
  );
}
