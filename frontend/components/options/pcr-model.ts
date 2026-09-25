/** View model for the PCR Analysis tab: the API's 15-minute records
 * (backend services/options/pcr_snapshots.py) resolved for one expiry scope
 * -- all 4 summed, or one expiry -- plus formatting and the CSV export. */

import type { PcrSnapshotRowOut } from "@/lib/types";

export const ALL_EXPIRIES = "all";
// Below this Call ΔOI (1 L) the ΔOI PCR explodes (e.g. 57x) -- shown as n/m.
export const COI_PCR_FLOOR = 100_000;
// Same rule and threshold as the backend's pcr_snapshots.classify.
const FLAT_FRACTION = 0.001;

export type Positioning = "bullish" | "bearish" | "divergence" | "unwinding" | "flat";

export interface PcrPoint {
  ts: string;
  session: string;
  status: PcrSnapshotRowOut["status"];
  source: string | null;
  spot: number | null;
  spotChange: number | null;
  spotChangePct: number | null;
  atm: number | null;
  atmShift: number | null;
  callOi: number | null;
  putOi: number | null;
  pcr: number | null;
  prevPcr: number | null;
  pcrChange: number | null;
  callChg: number | null;
  putChg: number | null;
  coiPcr: number | null;
  coiNm: boolean;
  callDay: number | null;
  putDay: number | null;
  positioning: Positioning | null;
  driver: string | null;
  expected: number | null;
  withOi: number | null;
  flags: string[];
  /** The record ΔOI/ΔPCR compare with, when it isn't the mark just before. */
  gapSince: string | null;
}

export function classify(
  put: number | null, call: number | null, spotChange: number | null, totalOi: number | null,
): [Positioning | null, string | null] {
  if (put == null || call == null) return [null, null];
  if (put < 0 && call < 0) return ["unwinding", "both_unwinding"];
  if (totalOi && Math.abs(put) + Math.abs(call) < FLAT_FRACTION * totalOi) return ["flat", "flat"];
  const net = put - call;
  if (net > 0) {
    const driver = call < 0 ? (put > 0 ? "put_buildup_call_unwinding" : "call_unwinding") : "put_led_buildup";
    return [spotChange == null || spotChange >= 0 ? "bullish" : "divergence", driver];
  }
  if (net < 0) {
    const driver = put < 0 ? (call > 0 ? "call_buildup_put_unwinding" : "put_unwinding") : "call_led_buildup";
    return [spotChange == null || spotChange <= 0 ? "bearish" : "divergence", driver];
  }
  return ["flat", "flat"];
}

export const POSITIONING: Record<Positioning, { label: string; icon: string; tone: "positive" | "negative" | "warning" | "neutral" }> = {
  bullish: { label: "Bullish", icon: "▲", tone: "positive" },
  bearish: { label: "Bearish", icon: "▼", tone: "negative" },
  divergence: { label: "Divergence", icon: "◆", tone: "warning" },
  unwinding: { label: "Unwinding", icon: "●", tone: "neutral" },
  flat: { label: "Flat", icon: "●", tone: "neutral" },
};

export const DRIVERS: Record<string, string> = {
  put_led_buildup: "Put-led build-up",
  put_buildup_call_unwinding: "Put build-up · call unwinding",
  call_unwinding: "Call unwinding",
  call_led_buildup: "Call-led build-up",
  call_buildup_put_unwinding: "Call build-up · put unwinding",
  put_unwinding: "Put unwinding",
  both_unwinding: "Both unwinding",
  flat: "Flat",
};

function ratio(num: number | null, den: number | null): number | null {
  return num == null || den == null || den === 0 ? null : num / den;
}

/** Oldest first. `rows` come newest first from the API. */
export function buildPoints(rows: PcrSnapshotRowOut[], scope: string): PcrPoint[] {
  const points: PcrPoint[] = [];
  let lastRecorded: PcrPoint | null = null;
  for (const row of [...rows].reverse()) {
    const base: PcrPoint = {
      ts: row.ts, session: row.session_date, status: row.status, source: row.source,
      spot: row.spot, spotChange: row.spot_change, spotChangePct: row.spot_change_pct,
      atm: row.atm_strike, atmShift: row.atm_shift,
      callOi: null, putOi: null, pcr: null, prevPcr: null, pcrChange: null,
      callChg: null, putChg: null, coiPcr: null, coiNm: false, callDay: null, putDay: null,
      positioning: null, driver: null, expected: null, withOi: null, flags: row.flags ?? [], gapSince: null,
    };
    if (row.status !== "recorded") {
      points.push(base);
      continue;
    }
    if (scope === ALL_EXPIRIES) {
      Object.assign(base, {
        callOi: row.total_call_oi, putOi: row.total_put_oi, pcr: row.pcr, prevPcr: row.prev_pcr, pcrChange: row.pcr_change,
        callChg: row.call_oi_change, putChg: row.put_oi_change, coiPcr: row.oi_change_pcr,
        callDay: row.call_oi_change_day, putDay: row.put_oi_change_day,
        positioning: row.positioning as Positioning | null, driver: row.oi_driver,
        expected: row.contracts_expected, withOi: row.contracts_with_oi,
      });
    } else {
      const e = row.expiry_rows?.find((x) => x.expiry === scope);
      if (!e) {
        points.push({ ...base, status: "missing" });
        continue;
      }
      const prevPcr = lastRecorded?.pcr ?? null;
      const [positioning, driver] = classify(
        e.put_oi_change, e.call_oi_change, row.spot_change, (e.total_call_oi ?? 0) + (e.total_put_oi ?? 0),
      );
      Object.assign(base, {
        callOi: e.total_call_oi, putOi: e.total_put_oi, pcr: e.pcr, prevPcr,
        pcrChange: e.pcr != null && prevPcr != null ? e.pcr - prevPcr : null,
        callChg: e.call_oi_change, putChg: e.put_oi_change, coiPcr: e.oi_change_pcr,
        callDay: e.call_oi_change_day, putDay: e.put_oi_change_day, positioning, driver,
        expected: e.contracts_expected, withOi: e.contracts_with_oi,
      });
    }
    const prevTs = scope === ALL_EXPIRIES ? row.prev_ts : lastRecorded?.ts ?? null;
    if (base.flags.includes("gap_before") && prevTs) base.gapSince = prevTs;
    base.coiNm = base.callChg != null && Math.abs(base.callChg) < COI_PCR_FLOOR;
    if (base.coiNm) base.coiPcr = null;
    points.push(base);
    lastRecorded = base;
  }
  return points;
}

export interface DaySummary {
  spotChange: number | null;
  atmShift: number | null;
  pcrChange: number | null;
  putDay: number | null;
  callDay: number | null;
  coiPcrDay: number | null;
  positioning: Positioning | null;
  driver: string | null;
}

/** Since the previous session's last record (the ΔOI day baseline). */
export function daySummary(points: PcrPoint[], latest: PcrPoint | null): DaySummary | null {
  if (!latest) return null;
  const base = [...points].reverse().find((p) => p.status === "recorded" && p.session < latest.session) ?? null;
  const spotChange = base?.spot != null && latest.spot != null ? latest.spot - base.spot : null;
  const [positioning, driver] = classify(latest.putDay, latest.callDay, spotChange, (latest.callOi ?? 0) + (latest.putOi ?? 0));
  return {
    spotChange,
    atmShift: base?.atm != null && latest.atm != null ? latest.atm - base.atm : null,
    pcrChange: base?.pcr != null && latest.pcr != null ? latest.pcr - base.pcr : null,
    putDay: latest.putDay,
    callDay: latest.callDay,
    coiPcrDay: latest.callDay != null && Math.abs(latest.callDay) >= COI_PCR_FLOOR ? ratio(latest.putDay, latest.callDay) : null,
    positioning,
    driver,
  };
}

// ------------------------------------------------------------ formatting

const timeFmt = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
const dayFmt = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", weekday: "short", day: "numeric", month: "short" });

export const timeIst = (ts: string) => timeFmt.format(new Date(ts));
export const dayIst = (ts: string) => dayFmt.format(new Date(ts));

export function num(v: number | null | undefined, digits = 0): string {
  return v == null ? "—" : v.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Signed, with no sign on a value that rounds to zero ("0.000", never "−0.000"). */
export function signed(v: number | null | undefined, digits = 1): string {
  if (v == null) return "—";
  const s = Math.abs(v).toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  if (Number(Math.abs(v).toFixed(digits)) === 0) return s;
  return v > 0 ? `+${s}` : `−${s}`;
}

/** Up/down arrow for a change, none when it rounds to zero at `digits`. */
export function arrow(v: number | null | undefined, digits: number): string {
  if (v == null || Number(Math.abs(v).toFixed(digits)) === 0) return "";
  return v > 0 ? "▲" : "▼";
}

export const crore = (v: number | null | undefined) => (v == null ? "—" : `${(v / 1e7).toFixed(2)} Cr`);
export const lakhSigned = (v: number | null | undefined) => (v == null ? "—" : `${signed(v / 1e5, 1)} L`);

export function toneOf(v: number | null | undefined, digits = 6): string {
  return v == null || Number(Math.abs(v).toFixed(digits)) === 0 ? "text-text-muted" : v > 0 ? "text-positive" : "text-negative";
}

/** The next mark (09:00-15:30 IST every 15 minutes, weekdays), as a label.
 * Holidays aren't known here -- after the close it just says "09:00". */
export function nextMarkLabel(now: Date): string {
  const ist = new Date(now.getTime() + 5.5 * 3600_000);
  const minutes = ist.getUTCHours() * 60 + ist.getUTCMinutes();
  const weekday = ist.getUTCDay();
  if (weekday >= 1 && weekday <= 5 && minutes < 15 * 60 + 30) {
    const next = minutes < 9 * 60 ? 9 * 60 : Math.floor(minutes / 15) * 15 + 15;
    return `${String(Math.floor(next / 60)).padStart(2, "0")}:${String(next % 60).padStart(2, "0")}`;
  }
  return weekday === 5 || weekday === 6 ? "09:00 Mon" : "09:00";
}

// ------------------------------------------------------------ CSV

export function toCsv(points: PcrPoint[], scopeLabel: string): string {
  const header = [
    "date", "time_ist", "status", "source", "scope", "nifty", "nifty_change_pts", "nifty_change_pct", "atm_strike", "atm_shift",
    "call_oi", "put_oi", "pcr", "pcr_prev", "pcr_change", "call_oi_change_15m", "put_oi_change_15m", "oi_change_pcr_15m",
    "call_oi_change_day", "put_oi_change_day", "positioning", "oi_driver", "contracts_with_oi", "contracts_expected", "flags",
  ];
  const cell = (v: unknown) => {
    if (v == null) return "";
    const s = String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = points.map((p) =>
    [
      p.session, timeIst(p.ts), p.status, p.source, scopeLabel, p.spot, p.spotChange, p.spotChangePct, p.atm, p.atmShift,
      p.callOi, p.putOi, p.pcr, p.prevPcr, p.pcrChange, p.callChg, p.putChg, p.coiPcr, p.callDay, p.putDay,
      p.positioning, p.driver, p.withOi, p.expected, p.flags.join(" "),
    ].map(cell).join(","),
  );
  return [header.join(","), ...lines].join("\n");
}
