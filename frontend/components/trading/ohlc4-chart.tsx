"use client";

/**
 * OHLC4 Buy 5 MIN's chart under its card (27 Nov): the latest session's
 * 5-minute NIFTY candles, the level each close was checked against -- the
 * previous 15-minute candle's (O+H+L+C)/4, as a step line -- with that
 * candle's low-high as a band (where the stops sit), and the side each
 * check picks as a strip. While the market is open, the forming slot shows
 * the level now and live NIFTY. Read-only; re-read every 30 s.
 */

import { useEffect, useRef, useState } from "react";

import { useOhlc4Chart } from "@/lib/hooks";
import type { Ohlc4CandleOut } from "@/lib/types";

const UP = "var(--positive)";
const DOWN = "var(--negative)";
const LEVEL = "var(--series-pcr)";
const CALL = "var(--series-call)";
const PUT = "var(--series-put)";
const PAD_L = 56;
const PAD_R = 12;

const hhmm = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const plus5 = (iso: string) => new Date(new Date(iso).getTime() + 5 * 60_000).toISOString();
const dayLabel = (d: string) => new Date(`${d}T00:00:00+05:30`).toLocaleDateString("en-GB", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short" });
const sameDay = (iso: string, d: string | null) =>
  d !== null && new Date(iso).toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" }) === d;
const pts = (v: number) => v.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function niceTicks(lo: number, hi: number, count: number): number[] {
  const span = hi - lo || 1;
  const mag = Math.pow(10, Math.floor(Math.log10(span / count)));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => span / s <= count) ?? 10 * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

export function Ohlc4Chart({ deploymentId }: { deploymentId: string }) {
  const { data, isLoading, isError } = useOhlc4Chart(deploymentId, true);
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

  const candles: Ohlc4CandleOut[] = data?.candles ?? [];
  const n = candles.length;
  const session = data?.session ?? null;
  const now = data?.level_now ?? null;
  const spot = data?.spot ?? null;
  const slots = n + (now || spot !== null ? 1 : 0); // the forming candle's slot while the market is open
  const W = width;
  const H_PRICE = W < 520 ? 190 : 230;
  const STRIP_Y = 8 + H_PRICE + 12;
  const H = STRIP_Y + 16 + 24;
  const band = slots ? (W - PAD_L - PAD_R) / slots : 1;
  const left = (i: number) => PAD_L + band * i;
  const x = (i: number) => left(i) + band / 2;
  const values = [
    ...candles.flatMap((c) => [c.high, c.low, c.level, c.level_low, c.level_high]),
    now?.level, now?.level_low, now?.level_high, spot,
  ].filter((v): v is number => typeof v === "number");
  const lo0 = values.length ? Math.min(...values) : 0;
  const hi0 = values.length ? Math.max(...values) : 1;
  const pad = (hi0 - lo0) * 0.06 || 5;
  const lo = lo0 - pad;
  const hi = hi0 + pad;
  const y = (v: number) => 8 + H_PRICE * (1 - (v - lo) / (hi - lo));
  const body = Math.max(1.5, Math.min(10, band * 0.62));
  const levelPath = candles
    .map((c, i) => (c.level === null ? "" : `${i === 0 || candles[i - 1].level === null ? "M" : "L"}${left(i).toFixed(1)},${y(c.level).toFixed(1)}H${(left(i) + band).toFixed(1)}`))
    .join("");
  const labelEvery = W < 520 ? 12 : 6; // every hour / half hour
  const h = hover !== null && hover < n ? candles[hover] : null;
  const levelName = (iso: string | null) =>
    iso === null ? "" : sameDay(iso, session) ? `the ${hhmm(iso)} 15-min candle` : `the ${dayLabel(iso.slice(0, 10))} ${hhmm(iso)} 15-min candle`;

  return (
    <div className="space-y-2" aria-label="NIFTY 5-minute candles and the level">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h4 className="text-sm font-semibold text-text-primary">NIFTY 5-minute candles and the level</h4>
        {data && (
          <span className="text-xs text-text-secondary">
            level: the previous 15-minute candle&apos;s (O+H+L+C)/4{session ? ` · ${dayLabel(session)}` : ""}
            {spot !== null && <> · NIFTY now <span className="font-financial text-text-primary">{pts(spot)}</span></>}
            {now && <> · level now <span className="font-financial" style={{ color: LEVEL }}>{pts(now.level)}</span></>}
          </span>
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-secondary">
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-2 rounded-sm" style={{ background: UP }} />Up candle</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-2 rounded-sm" style={{ background: DOWN }} />Down candle</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-0.5 w-4 rounded" style={{ background: LEVEL }} />Level (O+H+L+C)/4</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-3.5 rounded-sm opacity-20" style={{ background: LEVEL }} />15-min low–high (the stops)</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-3.5 rounded-sm opacity-40" style={{ background: CALL }} />CE</span>
        <span className="inline-flex items-center gap-1.5"><i className="inline-block h-3 w-3.5 rounded-sm opacity-40" style={{ background: PUT }} />PE</span>
      </div>
      {isLoading && <p className="text-xs text-text-muted">Reading NIFTY&apos;s 5-minute candles…</p>}
      {isError && <p className="text-xs text-negative">NIFTY&apos;s candles couldn&apos;t be read just now; trying again in 30 seconds.</p>}
      {data && n === 0 && <p className="text-xs text-text-muted">No 5-minute NIFTY candle yet.</p>}
      <div ref={box} className="relative w-full">
        {n > 0 && (
          <svg
            viewBox={`0 0 ${W} ${H}`}
            className="block h-auto w-full overflow-visible"
            role="img"
            aria-label="NIFTY 5-minute candles, the 15-minute level each close was checked against with its low-high band, and the side each check picks"
            onMouseLeave={() => setHover(null)}
          >
            {niceTicks(lo, hi, 5).map((v) => (
              <g key={v}>
                <line x1={PAD_L} x2={W - PAD_R} y1={y(v)} y2={y(v)} stroke="var(--border)" strokeWidth={1} />
                <text x={PAD_L - 6} y={y(v) + 3.5} textAnchor="end" fontSize={10.5} fill="var(--text-muted)" className="font-financial">
                  {v.toLocaleString("en-IN")}
                </text>
              </g>
            ))}
            {candles.map((c, i) =>
              c.level_low !== null && c.level_high !== null ? (
                <rect key={`b${c.at}`} x={left(i)} y={y(c.level_high)} width={band} height={Math.max(0.5, y(c.level_low) - y(c.level_high))}
                  fill={LEVEL} fillOpacity={0.09} />
              ) : null,
            )}
            {now && (
              <rect x={left(n)} y={y(now.level_high)} width={band} height={Math.max(0.5, y(now.level_low) - y(now.level_high))} fill={LEVEL} fillOpacity={0.09} />
            )}
            {candles.map((c, i) => {
              const up = c.close >= c.open;
              const color = up ? UP : DOWN;
              const top = y(Math.max(c.open, c.close));
              return (
                <g key={c.at}>
                  <line x1={x(i)} x2={x(i)} y1={y(c.high)} y2={y(c.low)} stroke={color} strokeWidth={1} />
                  <rect x={x(i) - body / 2} y={top} width={body} height={Math.max(1, y(Math.min(c.open, c.close)) - top)} rx={Math.min(1.5, body / 4)}
                    fill={color} stroke="var(--surface)" strokeWidth={0.5} />
                </g>
              );
            })}
            <path d={levelPath} fill="none" stroke="var(--surface)" strokeWidth={4.5} strokeLinejoin="round" />
            <path d={levelPath} fill="none" stroke={LEVEL} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {now && (
              <line x1={left(n)} x2={left(n) + band} y1={y(now.level)} y2={y(now.level)} stroke={LEVEL} strokeWidth={2} strokeDasharray="3 2" />
            )}
            {spot !== null && <circle cx={x(n)} cy={y(spot)} r={4} fill="var(--text-primary)" stroke="var(--surface)" strokeWidth={2} />}
            {candles.map((c, i) =>
              c.side ? (
                <g key={`s${c.at}`}>
                  <rect x={left(i) + 0.5} y={STRIP_Y} width={Math.max(1, band - 1)} height={14} rx={1.5} fill={c.side === "CE" ? CALL : PUT} fillOpacity={0.38} />
                  {band >= 9 && (i === 0 || candles[i - 1].side !== c.side) && (
                    <text x={x(i)} y={STRIP_Y + 10.5} textAnchor="middle" fontSize={9.5} fill="var(--text-primary)">{c.side === "CE" ? "C" : "P"}</text>
                  )}
                </g>
              ) : null,
            )}
            <text x={PAD_L - 6} y={STRIP_Y + 10.5} textAnchor="end" fontSize={10.5} fill="var(--text-secondary)">side</text>
            {candles.map((c, i) =>
              i % labelEvery === 0 ? (
                <text key={`t${c.at}`} x={x(i)} y={H - 6} textAnchor="middle" fontSize={10.5} fill="var(--text-muted)" className="font-financial">
                  {hhmm(c.at)}
                </text>
              ) : null,
            )}
            {hover !== null && hover < n && (
              <line x1={x(hover)} x2={x(hover)} y1={4} y2={STRIP_Y + 16} stroke="var(--border-strong)" strokeWidth={1} />
            )}
            {candles.map((c, i) => (
              <rect key={`h${c.at}`} x={left(i)} y={0} width={band} height={H} fill="transparent" onMouseEnter={() => setHover(i)} onClick={() => setHover(i)} />
            ))}
          </svg>
        )}
        {h && hover !== null && (
          <div
            className="pointer-events-none absolute top-2 z-10 min-w-[210px] rounded-md border border-border bg-surface-elevated p-2 text-xs shadow-md"
            style={
              W < 520
                ? x(hover) / W > 0.5 ? { left: 0 } : { right: 0 }
                : x(hover) / W > 0.6 ? { right: `${(1 - x(hover) / W) * 100 + 2}%` } : { left: `${(x(hover) / W) * 100 + 2}%` }
            }
          >
            <div className="mb-1 font-semibold text-text-primary">{hhmm(h.at)}–{hhmm(plus5(h.at))}</div>
            {([["Open", h.open], ["High", h.high], ["Low", h.low], ["Close", h.close]] as const).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-3"><span className="text-text-secondary">{k}</span><span className="font-financial">{pts(v)}</span></div>
            ))}
            {h.level !== null && (
              <>
                <div className="mt-1 flex justify-between gap-3"><span className="text-text-secondary">Level</span><span className="font-financial">{pts(h.level)}</span></div>
                <div className="text-[11px] text-text-muted">{levelName(h.level_candle)}</div>
                {h.level_low !== null && h.level_high !== null && (
                  <div className="flex justify-between gap-3"><span className="text-text-secondary">Its low / high</span><span className="font-financial">{pts(h.level_low)} / {pts(h.level_high)}</span></div>
                )}
              </>
            )}
            <div className="mt-1 flex justify-between gap-3">
              <span className="text-text-secondary">Check at {hhmm(plus5(h.at))}</span>
              <span className="font-semibold">{!h.checked ? "none (after 15:25)" : h.side === "CE" ? "call" : h.side === "PE" ? "put" : h.level === null ? "no level" : "at the level: no change"}</span>
            </div>
          </div>
        )}
      </div>
      {data && n > 0 && (
        <p className="text-xs text-text-muted">
          Checked at the 09:15 open on live NIFTY, then at each 5-minute close from 09:20 to 15:25 on that candle&apos;s close: the CE
          above the level, the PE below it, flipped at once. The level is the latest finished 15-minute candle&apos;s (O+H+L+C)/4 and the band its
          low–high: a call&apos;s stop is the low, a put&apos;s the high. The strip shows the side each 5-minute check picks
          {now ? "; the dashed level and the dot are the forming candle's level and NIFTY now" : ""}.
        </p>
      )}
    </div>
  );
}
