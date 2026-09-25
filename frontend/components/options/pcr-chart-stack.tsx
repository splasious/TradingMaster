"use client";

import { useEffect, useRef, useState } from "react";

import {
  COI_PCR_FLOOR,
  DRIVERS,
  POSITIONING,
  crore,
  dayIst,
  lakhSigned,
  num,
  signed,
  timeIst,
  type PcrPoint,
} from "@/components/options/pcr-model";

/** Five panes on one shared time axis -- NIFTY, OI PCR, ΔOI PCR, Put vs
 * Call OI, Put vs Call ΔOI -- instead of NIFTY on a second y-axis (two
 * scales on one chart make the lines look related or not depending only
 * on how each axis happens to be scaled). One crosshair across all panes,
 * shared with the tables through `hover`/`onHover`. Points are evenly
 * spaced, so the overnight gap between sessions takes no room. */

const PAD_L = 58;
const PAD_R = 14;
const AXIS_H = 34;
const GAP = 12;
const COI_CLIP = 3;

type Series = { key: keyof PcrPoint; color: string; kind: "line" | "bar" | "gbar"; offset?: -1 | 1 };
interface Pane {
  title: string;
  height: number;
  series: Series[];
  /** `step`: the tick spacing, so neighbouring labels never read the same. */
  fmt: (v: number, step: number) => string;
  zero?: boolean;
  clip?: number;
  refLine?: number;
  endLabels?: string[];
}

const INK = "var(--text-primary)";
const PUT = "var(--series-put)";
const CALL = "var(--series-call)";
const PCR = "var(--series-pcr)";

const dec = (step: number) => Math.max(0, -Math.floor(Math.log10(step) + 1e-9));

function niceTicks(lo: number, hi: number, count: number): number[] {
  if (lo === hi) {
    const pad = Math.abs(lo) * 0.01 || 1;
    lo -= pad;
    hi += pad;
  }
  const raw = (hi - lo) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  const ticks: number[] = [];
  for (let v = Math.floor(lo / step) * step; v <= Math.ceil(hi / step) * step + step / 2; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

function panesFor(compact: boolean): Pane[] {
  const h = compact ? 104 : 124;
  return [
    { title: "NIFTY 50", height: h, series: [{ key: "spot", color: INK, kind: "line" }], fmt: (v, st) => num(v, dec(st)) },
    { title: "Overall OI PCR", height: h, series: [{ key: "pcr", color: PCR, kind: "line" }], fmt: (v, st) => v.toFixed(Math.max(2, dec(st))) },
    {
      title: "ΔOI PCR (15 min)", height: compact ? 96 : 110, series: [{ key: "coiPcr", color: PCR, kind: "bar" }],
      fmt: (v) => v.toFixed(0), zero: true, clip: COI_CLIP, refLine: 1,
    },
    {
      title: "Total OI: Put vs Call", height: h,
      series: [{ key: "putOi", color: PUT, kind: "line" }, { key: "callOi", color: CALL, kind: "line" }],
      fmt: (v, st) => `${(v / 1e7).toFixed(dec(st / 1e7))} Cr`, endLabels: ["Put", "Call"],
    },
    {
      title: "ΔOI (15 min): Put vs Call", height: h,
      series: [{ key: "putChg", color: PUT, kind: "gbar", offset: -1 }, { key: "callChg", color: CALL, kind: "gbar", offset: 1 }],
      fmt: (v, st) => `${(v / 1e5).toFixed(dec(st / 1e5))} L`, zero: true,
    },
  ];
}

export function PcrChartStack({
  points,
  hover,
  onHover,
}: {
  points: PcrPoint[];
  hover: number | null;
  onHover: (i: number | null) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const n = points.length;
  const compact = width > 0 && width < 640;
  const panes = panesFor(compact);
  const plotW = Math.max(1, width - PAD_L - PAD_R);
  const band = plotW / Math.max(1, n);
  const x = (i: number) => PAD_L + band * (i + 0.5);

  const pick = (clientX: number) => {
    const el = ref.current;
    if (!el || n === 0) return;
    const mx = clientX - el.getBoundingClientRect().left;
    onHover(Math.max(0, Math.min(n - 1, Math.floor((mx - PAD_L) / band))));
  };

  let y0 = 0;
  const paneEls: React.ReactNode[] = [];
  for (const [pi, pane] of panes.entries()) {
    const top = y0 + 20;
    const bot = y0 + pane.height;
    const vals: number[] = [];
    for (const s of pane.series) for (const p of points) {
      const v = p[s.key] as number | null;
      if (v != null) vals.push(pane.clip ? Math.max(-pane.clip, Math.min(pane.clip, v)) : v);
    }
    let lo = vals.length ? Math.min(...vals) : 0;
    let hi = vals.length ? Math.max(...vals) : 1;
    if (pane.zero) {
      lo = Math.min(lo, 0);
      hi = Math.max(hi, 0);
    }
    if (pane.refLine != null) hi = Math.max(hi, pane.refLine);
    const ticks = pane.clip ? [-pane.clip, 0, pane.clip] : niceTicks(lo, hi, 3);
    lo = ticks[0];
    hi = ticks[ticks.length - 1];
    const ys = (v: number) => bot - ((bot - top) * (v - lo)) / (hi - lo || 1);
    const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;

    const els: React.ReactNode[] = [
      <text key="t" x={PAD_L} y={y0 + 12} style={{ fill: "var(--text-secondary)" }} fontSize={12} fontWeight={600}>{pane.title}</text>,
    ];
    ticks.forEach((v, ti) => {
      const y = ys(v);
      els.push(
        <g key={`tk${ti}`}>
          <line x1={PAD_L} x2={width - PAD_R} y1={y} y2={y} style={{ stroke: "var(--border)" }} strokeWidth={1} />
          <text x={PAD_L - 6} y={y + 3.5} textAnchor="end" style={{ fill: "var(--text-muted)" }} fontSize={10.5}>{pane.fmt(v, step)}</text>
        </g>,
      );
    });
    if (pane.zero) els.push(<line key="z" x1={PAD_L} x2={width - PAD_R} y1={ys(0)} y2={ys(0)} style={{ stroke: "var(--border-strong)" }} />);
    if (pane.refLine != null) {
      els.push(<line key="ref" x1={PAD_L} x2={width - PAD_R} y1={ys(pane.refLine)} y2={ys(pane.refLine)} style={{ stroke: "var(--text-muted)" }} strokeDasharray="2 4" />);
    }

    for (const [si, s] of pane.series.entries()) {
      if (s.kind === "line") {
        let d = "";
        let pen = false;
        points.forEach((p, i) => {
          const v = p[s.key] as number | null;
          if (v == null) {
            pen = false;
            return;
          }
          d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${ys(v).toFixed(1)}`;
          pen = true;
        });
        els.push(<path key={`l${si}`} d={d} fill="none" style={{ stroke: s.color }} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />);
        const lastIdx = points.findLastIndex((p) => p[s.key] != null);
        if (lastIdx >= 0) {
          const ly = ys(points[lastIdx][s.key] as number);
          els.push(<circle key={`d${si}`} cx={x(lastIdx)} cy={ly} r={4} style={{ fill: s.color, stroke: "var(--surface)" }} strokeWidth={2} />);
          if (pane.endLabels) {
            els.push(
              <text key={`el${si}`} x={x(lastIdx) - 8} y={ly + (si === 0 ? 15 : -8)} textAnchor="end" style={{ fill: "var(--text-secondary)" }} fontSize={11}>
                {pane.endLabels[si]}
              </text>,
            );
          }
        }
      } else {
        const bw = s.kind === "gbar" ? Math.max(2, Math.min(11, band * 0.36)) : Math.max(3, Math.min(16, band * 0.56));
        points.forEach((p, i) => {
          const raw = p[s.key] as number | null;
          if (raw == null) return;
          const v = pane.clip ? Math.max(-pane.clip, Math.min(pane.clip, raw)) : raw;
          const bx = x(i) + (s.offset ?? 0) * (bw / 2 + 1) - bw / 2;
          const y1 = ys(Math.max(v, 0));
          const y2 = ys(Math.min(v, 0));
          const faded = s.key === "coiPcr" && (p.callChg ?? 0) < 0;
          els.push(
            <rect key={`b${si}-${i}`} x={bx} y={y1} width={bw} height={Math.max(1, y2 - y1)} rx={2} style={{ fill: s.color }} opacity={faded ? 0.4 : 1} />,
          );
          if (pane.clip && Math.abs(raw) > pane.clip) {
            els.push(
              <text key={`c${i}`} x={x(i)} y={raw > 0 ? y1 - 3 : y2 + 10} textAnchor="middle" style={{ fill: "var(--text-secondary)" }} fontSize={9}>
                {raw > 0 ? "▲" : "▼"}
              </text>,
            );
          }
        });
      }
    }
    paneEls.push(<g key={`p${pi}`}>{els}</g>);
    y0 = bot + GAP + 12;
  }

  const plotBottom = y0 - GAP;
  const totalH = y0 + AXIS_H - 8;
  const labelEvery = Math.max(1, Math.ceil(44 / band));

  const overlays: React.ReactNode[] = [];
  points.forEach((p, i) => {
    if (i > 0 && p.session !== points[i - 1].session) {
      const dx = PAD_L + band * i;
      overlays.push(
        <g key={`s${i}`}>
          <line x1={dx} x2={dx} y1={0} y2={plotBottom} style={{ stroke: "var(--text-muted)" }} strokeDasharray="3 4" />
          <text x={dx + 5} y={plotBottom - 2} style={{ fill: "var(--text-muted)" }} fontSize={10.5}>{dayIst(p.ts)} ▸</text>
        </g>,
      );
    }
    if (p.atmShift) {
      overlays.push(
        <text key={`a${i}`} x={x(i)} y={plotBottom + 8} textAnchor="middle" style={{ fill: p.atmShift > 0 ? "var(--positive)" : "var(--negative)" }} fontSize={9.5}>
          {p.atmShift > 0 ? "▲" : "▼"}
        </text>,
      );
    }
    if ((n - 1 - i) % labelEvery === 0) {
      overlays.push(
        <text key={`x${i}`} x={x(i)} y={plotBottom + 22} textAnchor="middle" style={{ fill: "var(--text-muted)" }} fontSize={10.5}>{timeIst(p.ts)}</text>,
      );
    }
  });
  overlays.push(
    <text key="atm" x={PAD_L - 6} y={plotBottom + 8} textAnchor="end" style={{ fill: "var(--text-muted)" }} fontSize={9.5}>ATM</text>,
  );

  const hp = hover != null ? points[hover] : null;
  return (
    <div
      ref={ref}
      className="relative w-full touch-pan-y select-none"
      onPointerMove={(e) => pick(e.clientX)}
      onPointerDown={(e) => pick(e.clientX)}
      onPointerLeave={(e) => e.pointerType === "mouse" && onHover(null)}
    >
      {width > 0 && (
        <svg width={width} height={totalH} role="img" aria-label="NIFTY, OI PCR, ΔOI PCR, Put and Call OI and ΔOI every 15 minutes">
          {paneEls}
          {overlays}
          {hover != null && (
            <line x1={x(hover)} x2={x(hover)} y1={0} y2={plotBottom} style={{ stroke: "var(--text-secondary)" }} strokeWidth={1} />
          )}
        </svg>
      )}
      {hp && hover != null && <Tooltip point={hp} left={x(hover)} width={width} />}
    </div>
  );
}

function Tooltip({ point: p, left, width }: { point: PcrPoint; left: number; width: number }) {
  const w = 230;
  const flip = left + 14 + w > width;
  const rows: [string, string][] =
    p.status !== "recorded"
      ? [["", p.status === "pending" ? "Capturing…" : "Not recorded"]]
      : [
          ["NIFTY", `${num(p.spot, 1)} (${signed(p.spotChange)})`],
          ["ATM", `${num(p.atm)}${p.atmShift ? ` ${p.atmShift > 0 ? "▲" : "▼"} ${signed(p.atmShift, 0)}` : ""}`],
          ["PCR", `${p.prevPcr != null ? `${p.prevPcr.toFixed(3)} → ` : ""}${p.pcr?.toFixed(3) ?? "—"}`],
          ["ΔOI PCR", p.coiNm ? `n/m (Call ΔOI < ${COI_PCR_FLOOR / 1e5} L)` : p.coiPcr != null ? `${p.coiPcr.toFixed(2)}${(p.callChg ?? 0) < 0 ? "*" : ""}` : "—"],
          ["Put ΔOI", lakhSigned(p.putChg)],
          ["Call ΔOI", lakhSigned(p.callChg)],
          ["Put / Call OI", `${crore(p.putOi)} / ${crore(p.callOi)}`],
          ["Positioning", p.positioning ? `${POSITIONING[p.positioning].icon} ${POSITIONING[p.positioning].label}` : "—"],
        ];
  return (
    <div
      className="pointer-events-none absolute top-24 z-10 rounded-lg border border-border bg-surface-elevated px-3 py-2 text-xs shadow-lg"
      style={{ width: w, left: flip ? left - 14 - w : left + 14 }}
    >
      <div className="mb-1 font-semibold text-text-primary">
        {dayIst(p.ts)} · {timeIst(p.ts)}
        {p.source === "historical_fill" && <span className="ml-1 font-normal text-text-muted">(from history)</span>}
      </div>
      {rows.map(([k, v]) => (
        <div key={k || v} className="flex justify-between gap-3">
          <span className="text-text-muted">{k}</span>
          <span className="font-financial text-text-primary">{v}</span>
        </div>
      ))}
      {p.driver && <div className="mt-1 text-text-muted">{DRIVERS[p.driver] ?? p.driver}</div>}
    </div>
  );
}
