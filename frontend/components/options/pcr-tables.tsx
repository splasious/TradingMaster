"use client";

import { Badge } from "@/components/ui/badge";
import {
  COI_PCR_FLOOR,
  DRIVERS,
  arrow,
  POSITIONING,
  crore,
  dayIst,
  lakhSigned,
  num,
  signed,
  timeIst,
  toneOf,
  type PcrPoint,
} from "@/components/options/pcr-model";
import { cn } from "@/lib/utils";

/** The Overall OI and Change in OI tables: the same marks, newest first,
 * row-aligned (side by side from 1800px wide), with a divider where the
 * session changes. Hovering a row moves the chart crosshair and back. */

const TH = "sticky top-0 z-[1] bg-surface px-2.5 py-2 text-right text-[10.5px] font-medium uppercase tracking-wide text-text-muted whitespace-nowrap";
const TD = "px-2.5 py-1.5 text-right font-financial whitespace-nowrap";

export function PositioningChip({ value }: { value: PcrPoint["positioning"] }) {
  if (!value) return <span className="text-text-muted">—</span>;
  const p = POSITIONING[value];
  return (
    <Badge tone={p.tone} className="px-2 py-0.5 text-[11px]">
      <span aria-hidden>{p.icon}</span>
      {p.label}
    </Badge>
  );
}

function coiPcrText(p: PcrPoint) {
  if (p.coiNm) return <span className="text-text-muted" title={`Call ΔOI under ${COI_PCR_FLOOR / 1e5} L -- the ratio would be meaningless`}>n/m</span>;
  if (p.coiPcr == null) return <span className="text-text-muted">—</span>;
  if ((p.callChg ?? 0) < 0) {
    return <span className="text-text-muted" title="Call ΔOI is negative (calls unwinding) -- see Positioning">{signed(p.coiPcr, 2)}*</span>;
  }
  return <>{p.coiPcr.toFixed(2)}</>;
}

function rowsWithDividers(points: PcrPoint[]) {
  const newestFirst = points.map((p, i) => ({ p, i })).reverse();
  const out: ({ kind: "row"; p: PcrPoint; i: number } | { kind: "day"; label: string })[] = [];
  newestFirst.forEach(({ p, i }, k) => {
    if (k > 0 && newestFirst[k - 1].p.session !== p.session) out.push({ kind: "day", label: `${dayIst(p.ts)} · previous session` });
    out.push({ kind: "row", p, i });
  });
  return out;
}

function NotRecorded({ p, span }: { p: PcrPoint; span: number }) {
  return (
    <td colSpan={span} className="px-2.5 py-1.5 text-left text-xs text-text-muted">
      {p.status === "pending" ? "Capturing…" : "Not recorded -- filled from Kite's history when possible"}
    </td>
  );
}

interface TableProps {
  points: PcrPoint[];
  hover: number | null;
  onHover: (i: number | null) => void;
}

export function OverallOiTable({ points, hover, onHover }: TableProps) {
  return (
    <table className="w-full border-collapse text-[13px]" onMouseLeave={() => onHover(null)}>
      <thead>
        <tr className="border-b border-border">
          <th className={cn(TH, "text-left")}>Time</th>
          <th className={TH}>NIFTY</th>
          <th className={TH}>Δ pts</th>
          <th className={TH}>Δ %</th>
          <th className={TH}>ATM</th>
          <th className={TH}>Shift</th>
          <th className={TH}>Call OI</th>
          <th className={TH}>Put OI</th>
          <th className={TH}>PCR</th>
          <th className={TH}>ΔPCR</th>
        </tr>
      </thead>
      <tbody>
        {rowsWithDividers(points).map((r) =>
          r.kind === "day" ? (
            <tr key={r.label} className="bg-surface-elevated">
              <td colSpan={10} className="px-2.5 py-1 text-xs font-semibold text-text-secondary">{r.label} ↓</td>
            </tr>
          ) : (
            <tr
              key={r.p.ts}
              onMouseEnter={() => onHover(r.i)}
              className={cn("border-b border-border/60", hover === r.i && "bg-active-soft")}
            >
              <td className="px-2.5 py-1.5 text-left font-financial text-text-primary">{timeIst(r.p.ts)}</td>
              {r.p.status !== "recorded" ? (
                <NotRecorded p={r.p} span={9} />
              ) : (
                <>
                  <td className={TD}>{num(r.p.spot, 1)}</td>
                  <td className={cn(TD, toneOf(r.p.spotChange))}>{signed(r.p.spotChange)}</td>
                  <td className={cn(TD, toneOf(r.p.spotChange))}>{signed(r.p.spotChangePct, 2)}</td>
                  <td className={TD}>{num(r.p.atm)}</td>
                  <td className={cn(TD, r.p.atmShift ? (r.p.atmShift > 0 ? "text-positive" : "text-negative") : "text-text-muted")}>
                    {r.p.atmShift ? `${r.p.atmShift > 0 ? "▲" : "▼"} ${signed(r.p.atmShift, 0)}` : "·"}
                  </td>
                  <td className={TD}>{crore(r.p.callOi)}</td>
                  <td className={TD}>{crore(r.p.putOi)}</td>
                  <td className={cn(TD, "font-semibold")}>{r.p.pcr?.toFixed(3) ?? "—"}</td>
                  <td className={cn(TD, toneOf(r.p.pcrChange, 3))}>
                    {r.p.pcrChange == null ? "—" : `${arrow(r.p.pcrChange, 3)} ${signed(r.p.pcrChange, 3)}`}
                  </td>
                </>
              )}
            </tr>
          ),
        )}
      </tbody>
    </table>
  );
}

export function OiChangeTable({ points, hover, onHover }: TableProps) {
  return (
    <table className="w-full border-collapse text-[13px]" onMouseLeave={() => onHover(null)}>
      <thead>
        <tr className="border-b border-border">
          <th className={cn(TH, "text-left")}>Time</th>
          <th className={TH}>Call ΔOI</th>
          <th className={TH}>Put ΔOI</th>
          <th className={TH}>ΔOI PCR</th>
          <th className={TH}>PCR prev → now</th>
          <th className={TH}>NIFTY Δ</th>
          <th className={cn(TH, "text-left")}>Positioning</th>
        </tr>
      </thead>
      <tbody>
        {rowsWithDividers(points).map((r) =>
          r.kind === "day" ? (
            <tr key={r.label} className="bg-surface-elevated">
              <td colSpan={7} className="px-2.5 py-1 text-xs font-semibold text-text-secondary">{r.label} ↓</td>
            </tr>
          ) : (
            <tr
              key={r.p.ts}
              onMouseEnter={() => onHover(r.i)}
              className={cn("border-b border-border/60", hover === r.i && "bg-active-soft")}
            >
              <td className="px-2.5 py-1.5 text-left font-financial text-text-primary">
                {timeIst(r.p.ts)}
                {r.p.gapSince && <span className="ml-1 text-[10.5px] text-text-muted">since {timeIst(r.p.gapSince)}</span>}
              </td>
              {r.p.status !== "recorded" ? (
                <NotRecorded p={r.p} span={6} />
              ) : (
                <>
                  <td className={cn(TD, toneOf(r.p.callChg))}>{lakhSigned(r.p.callChg)}</td>
                  <td className={cn(TD, toneOf(r.p.putChg))}>{lakhSigned(r.p.putChg)}</td>
                  <td className={TD}>{coiPcrText(r.p)}</td>
                  <td className={TD}>
                    {r.p.prevPcr == null ? (
                      "—"
                    ) : (
                      <>
                        {r.p.prevPcr.toFixed(3)} → <b>{r.p.pcr?.toFixed(3) ?? "—"}</b>{" "}
                        {arrow(r.p.pcrChange, 3) ? (
                          <span className={toneOf(r.p.pcrChange, 3)} title={r.p.pcrChange! > 0 ? "Expansion" : "Contraction"}>
                            {arrow(r.p.pcrChange, 3)}
                          </span>
                        ) : null}
                      </>
                    )}
                  </td>
                  <td className={cn(TD, toneOf(r.p.spotChange))}>{signed(r.p.spotChange)}</td>
                  <td className="px-2.5 py-1.5 whitespace-nowrap">
                    <div className="flex items-center gap-2">
                      <PositioningChip value={r.p.positioning} />
                      {r.p.driver && <span className="text-[11px] text-text-muted">{DRIVERS[r.p.driver] ?? r.p.driver}</span>}
                    </div>
                  </td>
                </>
              )}
            </tr>
          ),
        )}
      </tbody>
    </table>
  );
}
