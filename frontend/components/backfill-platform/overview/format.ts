/** Formatting for the Data Backfill screens -- every time is shown in IST,
 * the market's own clock, whatever the browser's timezone ("en-US" parts, laid out
 * as "Thu 24 Sep" -- en-IN and en-GB abbreviate September "Sept"). */

const IST = "Asia/Kolkata";

const dayFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, weekday: "short", day: "numeric", month: "short" });
const shortDayFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, day: "numeric", month: "short" });
const yearDayFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, day: "numeric", month: "short", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });

function parts(fmt: Intl.DateTimeFormat, iso: string): Record<string, string> {
  return Object.fromEntries(fmt.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
}

/** "Thu 24 Sep" */
export function fmtDay(iso: string): string {
  const p = parts(dayFmt, iso);
  return `${p.weekday} ${p.day} ${p.month}`;
}

/** "24 Sep" */
export function fmtShortDay(iso: string): string {
  const p = parts(shortDayFmt, iso);
  return `${p.day} ${p.month}`;
}

/** "16 Mar 2021" */
export function fmtLongDay(iso: string): string {
  const p = parts(yearDayFmt, iso);
  return `${p.day} ${p.month} ${p.year}`;
}

/** "15:30" */
export function fmtTime(iso: string): string {
  return timeFmt.format(new Date(iso));
}

/** "Thu 24 Sep 15:30" -- a daily timeframe shows the day only. */
export function fmtSavedUpTo(iso: string, timeframe?: string): string {
  return timeframe === "1d" ? fmtDay(iso) : `${fmtDay(iso)} ${fmtTime(iso)}`;
}

export function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1_000)}K`;
  return n.toLocaleString("en-IN");
}

export function fmtBytes(n: number): string {
  const gb = n / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(gb >= 10 ? 0 : 1)} GB` : `${Math.round(n / 1024 ** 2)} MB`;
}

export function fmtEta(seconds: number | null): string | null {
  if (seconds == null) return null;
  if (seconds < 90) return "about a minute left";
  const minutes = Math.round(seconds / 60);
  return minutes < 90 ? `about ${minutes} min left` : `about ${Math.round(minutes / 60)} h left`;
}

export const TIMEFRAME_NAMES: Record<string, string> = {
  "1m": "1 minute",
  "5m": "5 minute",
  "15m": "15 minute",
  "30m": "30 minute",
  "60m": "60 minute",
  "1d": "Daily",
};

export const TIMEFRAME_SHORT: Record<string, string> = { "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m", "1d": "Daily" };
