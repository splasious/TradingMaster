"use client";

import { Download } from "lucide-react";
import { useMemo, useState } from "react";

import { PcrChartStack } from "@/components/options/pcr-chart-stack";
import {
  ALL_EXPIRIES,
  COI_PCR_FLOOR,
  DRIVERS,
  buildPoints,
  crore,
  dayIst,
  daySummary,
  lakhSigned,
  nextMarkLabel,
  num,
  signed,
  timeIst,
  toCsv,
  toneOf,
} from "@/components/options/pcr-model";
import { OiChangeTable, OverallOiTable, PositioningChip } from "@/components/options/pcr-tables";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Select } from "@/components/ui/select";
import { usePcrSnapshots } from "@/lib/hooks";
import type { PcrCaptureStatusOut, PcrSnapshotRowOut } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Options > PCR Analysis: the 15-minute NIFTY PCR records -- the latest
 * ROWS marks, continuous across sessions -- as KPI tiles, one chart stack
 * on a shared time axis, and the Overall OI / Change in OI tables. More
 * than ROWS are fetched so the first shown row still has its predecessor
 * and the day change its previous-session baseline. */

const ROWS = 25;
const FETCH = 60;
const expiryFmt = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", timeZone: "UTC" });

function Tile({ label, value, sub }: { label: string; value: React.ReactNode; sub?: React.ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
      <div className="truncate text-xs text-text-muted">{label}</div>
      <div className="mt-0.5 truncate font-financial text-lg font-semibold text-text-primary sm:text-xl">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-text-secondary">{sub}</div>}
    </div>
  );
}

/** `now`: when the records were last fetched (every 30s) -- not read from
 * the clock during render. */
function CaptureStatus({ latest, capture, now }: { latest: PcrSnapshotRowOut | null; capture: PcrCaptureStatusOut; now: number }) {
  const recentMs = latest ? now - new Date(latest.ts).getTime() : Infinity;
  const live = recentMs < 20 * 60_000;
  const problem = capture.last_error ?? capture.last_fill_error;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-secondary">
      <span className="inline-flex items-center gap-1.5">
        <span className={cn("h-2 w-2 rounded-full", problem ? "bg-warning" : live ? "bg-positive" : "bg-inactive")} aria-hidden />
        {problem ? "Attention" : live ? "Live" : "Waiting for the next mark"}
      </span>
      {latest && <span>last record {dayIst(latest.ts)} {timeIst(latest.ts)}</span>}
      <span>next {nextMarkLabel(new Date(now))}</span>
      {latest?.contracts_expected != null && (
        <span>{latest.contracts_with_oi}/{latest.contracts_expected} contracts</span>
      )}
      {capture.filling && <span>filling missed records from Kite&apos;s history…</span>}
      {problem && <span className="text-warning">{problem}</span>}
    </div>
  );
}

export function PcrAnalysisView() {
  const { data, isLoading, isError, dataUpdatedAt } = usePcrSnapshots("NIFTY", FETCH);
  const [scope, setScope] = useState(ALL_EXPIRIES);
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState<"overall" | "change">("overall");

  const latestRecorded = useMemo(() => data?.rows.find((r) => r.status === "recorded") ?? null, [data]);
  const expiries = latestRecorded?.expiries ?? [];
  const effectiveScope = scope === ALL_EXPIRIES || expiries.includes(scope) ? scope : ALL_EXPIRIES;
  const allPoints = useMemo(() => buildPoints(data?.rows ?? [], effectiveScope), [data, effectiveScope]);
  const points = useMemo(() => allPoints.slice(-ROWS), [allPoints]);
  const latest = useMemo(() => [...points].reverse().find((p) => p.status === "recorded") ?? null, [points]);
  const day = useMemo(() => daySummary(allPoints, latest), [allPoints, latest]);

  const scopeLabel =
    effectiveScope === ALL_EXPIRIES
      ? `Next ${data?.expiries_summed ?? 4} weeklies (summed)`
      : `${expiryFmt.format(new Date(effectiveScope))} expiry`;

  const downloadCsv = () => {
    const blob = new Blob([toCsv(points, scopeLabel)], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `nifty-pcr-15m-${latest?.session ?? "records"}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (isLoading) return <LoadingState />;
  if (isError || !data) return <ErrorState description="Could not load the PCR records." />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Select value={effectiveScope} onChange={(e) => setScope(e.target.value)} className="w-auto min-w-52" aria-label="Expiries">
          <option value={ALL_EXPIRIES}>Next {data.expiries_summed} weeklies (summed)</option>
          {expiries.map((e, i) => (
            <option key={e} value={e}>{expiryFmt.format(new Date(e))}{i === 0 ? " (current week)" : ""}</option>
          ))}
        </Select>
        <span className="rounded-md border border-border px-2.5 py-1.5 text-xs text-text-secondary">
          ATM ±{data.strike_window} strikes · 15 min · {ROWS} rows
        </span>
        <Button size="sm" variant="secondary" onClick={downloadCsv} disabled={!points.length}>
          <Download className="h-3.5 w-3.5" /> CSV
        </Button>
        <div className="basis-full lg:ml-auto lg:basis-auto">
          <CaptureStatus latest={latestRecorded} capture={data.capture} now={dataUpdatedAt} />
        </div>
      </div>

      {!points.length ? (
        <Card>
          <CardContent>
            <EmptyState
              title="No PCR records yet"
              description="A record is saved every 15 minutes from 09:00 to 15:30 IST once Zerodha is logged in. Marks missed in the last few sessions are rebuilt from Kite's history."
            />
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
            <Tile
              label="NIFTY 50"
              value={num(latest?.spot, 1)}
              sub={
                <>
                  <span className={toneOf(latest?.spotChange)}>{signed(latest?.spotChange)} pts · {signed(latest?.spotChangePct, 2)}%</span>
                  {day?.spotChange != null && <> · day <span className={toneOf(day.spotChange)}>{signed(day.spotChange, 0)}</span></>}
                </>
              }
            />
            <Tile
              label="ATM strike"
              value={num(latest?.atm)}
              sub={<>shift {latest?.atmShift ? signed(latest.atmShift, 0) : "0"}{day?.atmShift != null && <> · day {signed(day.atmShift, 0)}</>}</>}
            />
            <Tile
              label="Overall OI PCR"
              value={latest?.pcr?.toFixed(3) ?? "—"}
              sub={
                <>
                  <span className={toneOf(latest?.pcrChange, 3)}>{signed(latest?.pcrChange, 3)}</span> vs prev
                  {day?.pcrChange != null && <> · day <span className={toneOf(day.pcrChange, 3)}>{signed(day.pcrChange, 3)}</span></>}
                </>
              }
            />
            <Tile
              label="ΔOI PCR · 15 min / day"
              value={
                <>
                  {latest?.coiNm ? "n/m" : latest?.coiPcr?.toFixed(2) ?? "—"}
                  <span className="text-sm font-normal text-text-muted"> / {day?.coiPcrDay?.toFixed(2) ?? "—"}</span>
                </>
              }
              sub={<>Put {lakhSigned(latest?.putChg)} · Call {lakhSigned(latest?.callChg)}</>}
            />
            <Tile
              label="Put OI / Call OI"
              value={<span className="text-base sm:text-lg">{crore(latest?.putOi)} / {crore(latest?.callOi)}</span>}
              sub={<>day: Put {lakhSigned(day?.putDay)} · Call {lakhSigned(day?.callDay)}</>}
            />
            <Tile
              label="Positioning (day)"
              value={<PositioningChip value={day?.positioning ?? null} />}
              sub={day?.driver ? DRIVERS[day.driver] : "—"}
            />
          </div>

          <Card>
            <CardHeader className="flex-wrap gap-2">
              <CardTitle>Every 15 minutes · {scopeLabel}</CardTitle>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-text-secondary">
                <Legend color="var(--series-put)" label="Put" />
                <Legend color="var(--series-call)" label="Call" />
                <Legend color="var(--series-pcr)" label="PCR" />
                <Legend color="var(--text-primary)" label="NIFTY" />
                <span className="text-text-muted">▲▼ ATM shift · ┆ new session</span>
              </div>
            </CardHeader>
            <CardContent className="px-2 pt-1 sm:px-4">
              <PcrChartStack points={points} hover={hover} onHover={setHover} />
              <p className="mt-1 px-2 text-[11px] text-text-muted">
                ΔOI PCR: dotted line = 1.0 (equal build-up) · faded = calls unwinding · clipped at ±3 (▲▼ beyond) · gap = n/m (Call ΔOI under {COI_PCR_FLOOR / 1e5} L)
              </p>
            </CardContent>
          </Card>

          <div className="flex gap-1 min-[1800px]:hidden" role="tablist" aria-label="Table">
            {(["overall", "change"] as const).map((t) => (
              <button
                key={t}
                role="tab"
                aria-selected={table === t}
                onClick={() => setTable(t)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium",
                  table === t ? "bg-active-soft text-active" : "text-text-muted hover:text-text-secondary",
                )}
              >
                {t === "overall" ? "Overall OI" : "Change in OI"}
              </button>
            ))}
          </div>
          <div className="grid grid-cols-1 gap-4 min-[1800px]:grid-cols-2">
            <Card className={cn(table !== "overall" && "hidden min-[1800px]:block")}>
              <CardHeader>
                <CardTitle>Overall OI · {points.length} records</CardTitle>
                <span className="text-xs text-text-muted">newest first</span>
              </CardHeader>
              <CardContent className="overflow-x-auto p-0">
                <OverallOiTable points={points} hover={hover} onHover={setHover} />
              </CardContent>
            </Card>
            <Card className={cn(table !== "change" && "hidden min-[1800px]:block")}>
              <CardHeader>
                <CardTitle>Change in OI (ΔOI) · {points.length} records</CardTitle>
                <span className="text-xs text-text-muted">same contracts at both times</span>
              </CardHeader>
              <CardContent className="overflow-x-auto p-0">
                <OiChangeTable points={points} hover={hover} onHover={setHover} />
              </CardContent>
            </Card>
          </div>

          <Definitions />
        </>
      )}
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block h-[3px] w-3.5 rounded-full" style={{ background: color }} aria-hidden />
      {label}
    </span>
  );
}

function Definitions() {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>How each number is defined</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="list-disc space-y-1 pl-4 text-xs text-text-secondary">
            <li><b>Time</b>: the moment the numbers were true -- a record every 15 minutes, 09:00 to 15:30 IST.</li>
            <li><b>Overall PCR</b> = Σ Put OI ÷ Σ Call OI over ATM ±40 strikes of the selected expiries.</li>
            <li><b>ΔOI</b> = OI now − OI at the previous record, summed over the <i>same</i> contracts -- an ATM shift never shows up as build-up.</li>
            <li><b>ΔOI PCR</b> = Σ Put ΔOI ÷ Σ Call ΔOI. <b>*</b> = calls unwinding (the ratio no longer reads like a PCR); <b>n/m</b> = Call ΔOI under {COI_PCR_FLOOR / 1e5} L.</li>
            <li><b>ΔPCR</b> = PCR now − previous PCR: ▲ expansion, ▼ contraction. <b>Day</b> = since the previous session&apos;s last record.</li>
            <li><b>ATM</b> = strike nearest to NIFTY at that time; <b>Shift</b> = change since the previous record.</li>
          </ul>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Positioning</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="space-y-1.5 text-xs text-text-secondary">
            <li className="flex items-center gap-2"><PositioningChip value="bullish" /> put build-up larger, or calls unwinding -- and NIFTY rises</li>
            <li className="flex items-center gap-2"><PositioningChip value="bearish" /> call build-up larger, or puts unwinding -- and NIFTY falls</li>
            <li className="flex items-center gap-2"><PositioningChip value="divergence" /> the OI lean and the NIFTY move disagree</li>
            <li className="flex items-center gap-2"><PositioningChip value="unwinding" /> both sides falling · <PositioningChip value="flat" /> change under 0.1% of OI</li>
          </ul>
          <p className="mt-2 text-[11px] text-text-muted">OI can&apos;t tell writers from buyers; this reads it the usual way for index options, as mostly written.</p>
        </CardContent>
      </Card>
    </div>
  );
}
