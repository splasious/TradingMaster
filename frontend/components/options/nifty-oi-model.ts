/** The live NIFTY 50 chart's numbers (nifty-oi-chart.tsx): the top three call
 * OI strikes above NIFTY and put OI strikes below it -- ranked the way the
 * Telegram OI-levels picture ranks them (services/options/oi_levels.py
 * top_levels: out of the money, most OI first, the nearer strike on a tie)
 * -- from live OI while NSE trades, and the 15-minute candle forming now. */

import type { OiChartContractOut, OiChartOut } from "@/lib/types";

export const LAKH = 100_000;
export const TOP_N = 3;
export const MA_CANDLES = 8;
export const BAR_MS = 15 * 60_000;
const IST_MS = 5.5 * 3_600_000;
const DAY_MS = 86_400_000;
const OPEN_MIN = 9 * 60 + 15;
const CLOSE_MIN = 15 * 60 + 30;

export interface Candle {
  start: number; // ms, UTC
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface OiLevel {
  tag: string; // "R1" .. "R3", "S1" .. "S3"
  rank: number; // 1 = the most OI
  side: "CE" | "PE";
  strike: number;
  oi: number; // lakh: live, or at the record
  ref: number | null; // lakh: what the dotted outline shows (the record when live, the record before it otherwise)
  change: number | null; // lakh, oi - ref
  day: number | null; // lakh, since the previous session's last record
  live: boolean; // oi is a live tick
}

export interface Levels {
  live: boolean; // live ticks (NSE open), else the latest record
  spot: number;
  resistance: OiLevel[]; // most OI first
  support: OiLevel[];
  liveContracts: number; // contracts with a live OI tick
}

export interface TickLike {
  open_interest: number | null;
  source: string;
}

function lakh(value: number | null | undefined): number | null {
  return value == null ? null : value / LAKH;
}

/** `liveSpot`: NIFTY's live price, null when there's none (closed, no tick). */
export function buildLevels(data: OiChartOut, ticks: Record<string, TickLike>, liveSpot: number | null): Levels | null {
  const live = data.market_open && liveSpot != null;
  const spot = live ? liveSpot : data.record_spot ?? data.candles.at(-1)?.close ?? null;
  if (spot == null || !data.contracts.length) return null;
  let liveContracts = 0;
  const rows = data.contracts.map((c: OiChartContractOut) => {
    const tick = live && c.instrument_id ? ticks[c.instrument_id] : undefined;
    const liveOi = tick && tick.source === "kite" && tick.open_interest != null ? tick.open_interest : null;
    if (liveOi != null) liveContracts += 1;
    const oi = liveOi ?? c.oi;
    const ref = live ? c.oi : c.prev_oi;
    return { c, oi, ref, live: liveOi != null };
  });
  const pick = (side: "CE" | "PE"): OiLevel[] =>
    rows
      .filter((r) => r.c.option_type === side && r.oi != null && (side === "CE" ? r.c.strike > spot : r.c.strike < spot))
      .sort((a, b) => b.oi! - a.oi! || Math.abs(a.c.strike - spot) - Math.abs(b.c.strike - spot))
      .slice(0, TOP_N)
      .map((r, i) => ({
        tag: `${side === "CE" ? "R" : "S"}${i + 1}`,
        rank: i + 1,
        side,
        strike: r.c.strike,
        oi: r.oi! / LAKH,
        ref: lakh(r.ref),
        change: r.ref == null ? null : (r.oi! - r.ref) / LAKH,
        day: r.c.day_oi == null ? null : (r.oi! - r.c.day_oi) / LAKH,
        live: r.live,
      }));
  return { live, spot, resistance: pick("CE"), support: pick("PE"), liveContracts };
}

export function ms(ts: string): number {
  return new Date(ts).getTime();
}

/** The start (ms, UTC) of the 15-minute candle `now` falls in, on the
 * 09:15 grid; null outside 09:15-15:30 IST. */
export function periodStart(now: number): number | null {
  const ist = now + IST_MS;
  const minutes = Math.floor((ist % DAY_MS) / 60_000);
  if (minutes < OPEN_MIN || minutes >= CLOSE_MIN) return null;
  const open = ist - (ist % DAY_MS) + OPEN_MIN * 60_000;
  return open + Math.floor((ist - open) / BAR_MS) * BAR_MS - IST_MS;
}

export function istDay(t: number): number {
  return Math.floor((t + IST_MS) / DAY_MS);
}

export function toCandle(c: { ts: string; open: number; high: number; low: number; close: number }): Candle {
  return { start: ms(c.ts), open: c.open, high: c.high, low: c.low, close: c.close };
}

export interface Forming {
  forming: Candle | null;
  done: Candle[]; // finished on the page, not yet among the saved candles
}

/** The candle forming now, moved by the live price -- opened at the previous
 * candle's close when it follows it (as the Telegram picture opens it),
 * its high and low the extremes of every tick seen. A forming candle the
 * clock has passed is kept as finished until the saved one arrives. */
export function nextForming(prev: Forming, data: OiChartOut, live: number | null, start: number | null): Forming {
  const saved = data.candles.map(toCandle);
  const lastSaved = saved.at(-1)?.start ?? -Infinity;
  let done = prev.done.filter((c) => c.start > lastSaved);
  let cur = prev.forming;
  if (cur && cur.start !== start) {
    if (cur.start > lastSaved && !done.some((c) => c.start === cur!.start)) done = [...done, cur];
    cur = null;
  }
  if (start == null || !data.market_open) return { forming: null, done };
  const server = data.forming && ms(data.forming.ts) === start ? toCandle(data.forming) : null;
  if (!cur) {
    const before = [...saved, ...done].at(-1);
    const follows = before != null && before.start + BAR_MS === start;
    const open = server?.open ?? (follows ? before.close : live);
    if (open == null) return { forming: null, done };
    cur = { start, open, high: server?.high ?? open, low: server?.low ?? open, close: server?.close ?? open };
  } else {
    cur = { ...cur };
    if (server) {
      cur.high = Math.max(cur.high, server.high);
      cur.low = Math.min(cur.low, server.low);
    }
  }
  if (live != null) {
    cur.close = live;
    cur.high = Math.max(cur.high, live);
    cur.low = Math.min(cur.low, live);
  }
  return { forming: cur, done };
}

/** The 8-candle MA of (O+H+L+C)/4 at each candle (null until there are 8). */
export function movingAverage(candles: Candle[]): (number | null)[] {
  const typical = candles.map((c) => (c.open + c.high + c.low + c.close) / 4);
  return typical.map((_, i) =>
    i < MA_CANDLES - 1 ? null : typical.slice(i - MA_CANDLES + 1, i + 1).reduce((a, b) => a + b, 0) / MA_CANDLES,
  );
}

/** The previous session's last close: the last candle of a day before the newest candle's. */
export function previousClose(candles: Candle[]): number | null {
  const newest = candles.at(-1);
  if (!newest) return null;
  const today = istDay(newest.start);
  for (let i = candles.length - 1; i >= 0; i--) if (istDay(candles[i].start) < today) return candles[i].close;
  return null;
}

export function fmtStrike(v: number): string {
  return v.toLocaleString("en-IN", { maximumFractionDigits: 0 });
}

export function arrow(v: number | null, digits = 1): string {
  if (v == null) return "—";
  const r = Number(v.toFixed(digits));
  return `${r > 0 ? "▲" : r < 0 ? "▼" : "•"}${Math.abs(r).toFixed(digits)}`;
}
