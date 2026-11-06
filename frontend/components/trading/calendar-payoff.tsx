"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import type { PayoffOut } from "@/lib/types";

/** A calendar card's "if closed at 15:00 today" panel (5 Nov): the most it can
 * make and lose by its daily close, the NIFTY range it ends in profit and
 * where its take-profit would be reached, and the profit curve across NIFTY
 * +-1,000 with NIFTY now marked. Every point is the backend's
 * (services/options/calendar_payoff.py): each option repriced at its implied
 * volatility now, closed at bid/ask, after every charge. */

const H = 220;
const PAD = { top: 18, right: 12, bottom: 28, left: 64 };

function money(value: number, signed = false): string {
  const sign = value < 0 ? "-" : signed && value > 0 ? "+" : "";
  return `${sign}₹${Math.abs(Math.round(value)).toLocaleString("en-IN")}`;
}

function level(value: number): string {
  return Math.round(value).toLocaleString("en-IN");
}

function away(value: number, spot: number): string {
  const d = Math.round(value - spot);
  return `${d > 0 ? "+" : ""}${d.toLocaleString("en-IN")} pts`;
}

function shortMoney(value: number): string {
  const abs = Math.abs(value);
  const text = abs >= 100_000 ? `${(abs / 100_000).toFixed(abs >= 1_000_000 ? 0 : 1)}L` : abs >= 1000 ? `${Math.round(abs / 1000)}K` : `${Math.round(abs)}`;
  return `${value < 0 ? "-" : ""}₹${text}`;
}

/** The profit at `x` on the curve, between its two nearest points. */
function pnlAt(points: PayoffOut["points"], x: number): number {
  if (x <= points[0].spot) return points[0].pnl;
  for (let i = 1; i < points.length; i++) {
    if (points[i].spot >= x) {
      const a = points[i - 1];
      const b = points[i];
      return a.pnl + ((x - a.spot) * (b.pnl - a.pnl)) / (b.spot - a.spot);
    }
  }
  return points[points.length - 1].pnl;
}

function Field({ label, children, sub }: { label: string; children: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-0.5">
      <div className="text-xs font-medium uppercase tracking-wide text-text-muted">{label}</div>
      <div className="font-financial text-base">{children}</div>
      {sub && <div className="text-sm text-text-muted">{sub}</div>}
    </div>
  );
}

function niceTicks(lo: number, hi: number, count: number): number[] {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(v);
  return out;
}

export function CalendarPayoff({ payoff }: { payoff: PayoffOut }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  // Drawn at the box's own pixel width, so text stays at its size on any screen.
  const [W, setW] = useState(640);
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const observer = new ResizeObserver(([entry]) => setW(Math.max(280, Math.round(entry.contentRect.width))));
    observer.observe(box);
    return () => observer.disconnect();
  }, []);
  const { points, spot } = payoff;
  const inProfit = payoff.max_profit >= 0;
  const now = pnlAt(points, spot);
  const first = points[0].spot;
  const last = points[points.length - 1].spot;

  const geo = useMemo(() => {
    const ys = points.map((p) => p.pnl).concat([0], payoff.target != null && payoff.target <= payoff.max_profit * 1.6 ? [payoff.target] : []);
    let lo = Math.min(...ys);
    let hi = Math.max(...ys);
    const pad = (hi - lo || 1) * 0.08;
    lo -= pad;
    hi += pad;
    const x = (v: number) => PAD.left + ((v - first) / (last - first)) * (W - PAD.left - PAD.right);
    const y = (v: number) => PAD.top + ((hi - v) / (hi - lo)) * (H - PAD.top - PAD.bottom);
    const line = points.map((p, i) => `${i ? "L" : "M"}${x(p.spot).toFixed(1)},${y(p.pnl).toFixed(1)}`).join("");
    const area = `${line}L${x(last).toFixed(1)},${y(0).toFixed(1)}L${x(first).toFixed(1)},${y(0).toFixed(1)}Z`;
    return { x, y, lo, hi, line, area, yTicks: niceTicks(lo, hi, 4), xTicks: niceTicks(first, last, 6) };
  }, [points, payoff.target, payoff.max_profit, first, last, W]);

  const onMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const box = svgRef.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((event.clientX - box.left) / box.width) * W;
    const v = first + ((px - PAD.left) / (W - PAD.left - PAD.right)) * (last - first);  // px is in the drawing's units
    setHover(v < first || v > last ? null : Math.round(v / 10) * 10);
  };

  const breakevenText = !inProfit
    ? "none -- no NIFTY level ends in profit"
    : `${payoff.breakeven_low != null ? level(payoff.breakeven_low) : `below ${level(first)}`} – ${payoff.breakeven_high != null ? level(payoff.breakeven_high) : `above ${level(last)}`}`;
  const breakevenSub = inProfit
    ? [payoff.breakeven_low != null ? away(payoff.breakeven_low, spot) : null, payoff.breakeven_high != null ? away(payoff.breakeven_high, spot) : null]
        .filter(Boolean)
        .join(" / ")
    : "at these volatilities, by 15:00";
  const targetReached = payoff.target != null && payoff.max_profit >= payoff.target;

  return (
    <div className="space-y-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="text-base font-semibold text-text-primary">If closed at 15:00 today</div>
        <div className="text-sm text-text-muted">each option at its implied volatility now · closed at bid/ask · after charges</div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Field label="NIFTY stays here" sub={`NIFTY ${level(spot)} at 15:00`}>
          <span className={now >= 0 ? "text-positive" : "text-negative"}>{money(now, true)}</span>
        </Field>
        <Field label="Max profit" sub={`at NIFTY ${level(payoff.max_profit_at)} (${away(payoff.max_profit_at, spot)})`}>
          <span className={payoff.max_profit >= 0 ? "text-positive" : "text-negative"}>{money(payoff.max_profit, true)}</span>
        </Field>
        <Field
          label="Max loss (NIFTY ±1,000)"
          sub={
            <>
              at NIFTY {level(payoff.max_loss_at)} ({away(payoff.max_loss_at, spot)})
              {payoff.loss_cap != null && <div>never more than {money(payoff.loss_cap)} (net debit + charges)</div>}
            </>
          }
        >
          <span className="text-negative">{money(payoff.max_loss, true)}</span>
        </Field>
        <Field label="Breakeven range" sub={breakevenSub}>
          {breakevenText}
        </Field>
        {payoff.target != null && (
          <Field label={`${money(payoff.target)} take-profit`} sub={targetReached ? "NIFTY range where 15:00 reaches it" : "the most by 15:00 falls short"}>
            {targetReached
              ? `${payoff.target_low != null ? level(payoff.target_low) : `below ${level(first)}`} – ${payoff.target_high != null ? level(payoff.target_high) : `above ${level(last)}`}`
              : "not reached at any level"}
          </Field>
        )}
      </div>

      <div ref={boxRef} className="relative">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          width={W}
          height={H}
          className="block w-full touch-none select-none"
          role="img"
          aria-label={`Profit at 15:00 against NIFTY: maximum ${money(payoff.max_profit, true)} at ${level(payoff.max_profit_at)}`}
          onPointerMove={onMove}
          onPointerLeave={() => setHover(null)}
        >
          <defs>
            <clipPath id="payoff-above">
              <rect x={0} y={0} width={W} height={geo.y(0)} />
            </clipPath>
            <clipPath id="payoff-below">
              <rect x={0} y={geo.y(0)} width={W} height={H} />
            </clipPath>
          </defs>
          {geo.yTicks.map((t) => (
            <g key={`y${t}`}>
              <line x1={PAD.left} x2={W - PAD.right} y1={geo.y(t)} y2={geo.y(t)} stroke="var(--border)" strokeWidth={t === 0 ? 0 : 1} />
              <text x={PAD.left - 6} y={geo.y(t) + 4} textAnchor="end" fontSize={12} fill="var(--text-muted)">
                {shortMoney(t)}
              </text>
            </g>
          ))}
          {geo.xTicks.map((t) => (
            <text
              key={`x${t}`}
              x={geo.x(t)}
              y={H - 6}
              textAnchor={geo.x(t) < PAD.left + 28 ? "start" : geo.x(t) > W - PAD.right - 28 ? "end" : "middle"}
              fontSize={12}
              fill="var(--text-muted)"
            >
              {level(t)}
            </text>
          ))}
          <path d={geo.area} fill="var(--positive)" opacity={0.14} clipPath="url(#payoff-above)" />
          <path d={geo.area} fill="var(--negative)" opacity={0.12} clipPath="url(#payoff-below)" />
          <line x1={PAD.left} x2={W - PAD.right} y1={geo.y(0)} y2={geo.y(0)} stroke="var(--text-muted)" strokeWidth={1} />
          {targetReached && payoff.target != null && (
            <g>
              <line x1={PAD.left} x2={W - PAD.right} y1={geo.y(payoff.target)} y2={geo.y(payoff.target)} stroke="var(--positive)" strokeDasharray="4 3" strokeWidth={1} />
              <text x={W - PAD.right} y={geo.y(payoff.target) - 4} textAnchor="end" fontSize={12} fill="var(--text-secondary)">
                take-profit {money(payoff.target)}
              </text>
            </g>
          )}
          {inProfit &&
            [payoff.breakeven_low, payoff.breakeven_high].map((b, i) =>
              b != null ? <line key={`be${i}`} x1={geo.x(b)} x2={geo.x(b)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--text-muted)" strokeDasharray="2 3" strokeWidth={1} /> : null,
            )}
          <path d={geo.line} fill="none" stroke="var(--positive)" strokeWidth={2} clipPath="url(#payoff-above)" strokeLinejoin="round" />
          <path d={geo.line} fill="none" stroke="var(--negative)" strokeWidth={2} clipPath="url(#payoff-below)" strokeLinejoin="round" />
          <line x1={geo.x(spot)} x2={geo.x(spot)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--oi-spot)" strokeWidth={1.5} />
          <circle cx={geo.x(spot)} cy={geo.y(now)} r={4} fill="var(--oi-spot)" stroke="var(--surface)" strokeWidth={2} />
          <text x={geo.x(spot) + 5} y={PAD.top + 11} fontSize={12} fill="var(--text-secondary)">
            NIFTY now
          </text>
          {hover != null && (
            <g>
              <line x1={geo.x(hover)} x2={geo.x(hover)} y1={PAD.top} y2={H - PAD.bottom} stroke="var(--text-secondary)" strokeWidth={1} />
              <circle cx={geo.x(hover)} cy={geo.y(pnlAt(points, hover))} r={4} fill="var(--text-primary)" stroke="var(--surface)" strokeWidth={2} />
            </g>
          )}
        </svg>
        {hover != null && (
          <div
            className="pointer-events-none absolute top-1 rounded-md border border-border bg-surface-elevated px-2 py-1 text-sm shadow"
            style={hover > (first + last) / 2 ? { left: 8 } : { right: 8 }}
          >
            <div className="text-text-secondary">
              NIFTY {level(hover)} <span className="text-text-muted">({away(hover, spot)})</span>
            </div>
            <div className={`font-financial font-semibold ${pnlAt(points, hover) >= 0 ? "text-positive" : "text-negative"}`}>
              {money(pnlAt(points, hover), true)} at 15:00
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
