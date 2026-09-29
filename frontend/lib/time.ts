/** Every trading time on the site is shown in India time -- the market's
 * own clock -- whatever time zone the viewer's device is set to. Formatting
 * in the device's zone showed a 10:31 IST entry as 09:01 on a laptop set to
 * Dubai time. Dates read day first ("29 Sep 2026"), which no reader can take
 * for a different day; built from "en-US" parts because en-GB and en-IN
 * abbreviate September "Sept". */
const IST = "Asia/Kolkata";

const partsFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: IST, year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
});

type When = string | number | Date;

function istParts(value: When): Record<string, string> {
  return Object.fromEntries(partsFmt.formatToParts(new Date(value)).map((p) => [p.type, p.value]));
}

/** "29 Sep 2026, 10:31:12 AM" (IST) */
export function istDateTime(value: When): string {
  const p = istParts(value);
  return `${p.day} ${p.month} ${p.year}, ${p.hour}:${p.minute}:${p.second} ${p.dayPeriod}`;
}

/** "29 Sep 2026" (IST) */
export function istDate(value: When): string {
  const p = istParts(value);
  return `${p.day} ${p.month} ${p.year}`;
}

/** "10:31:12 AM" (IST) */
export function istTime(value: When): string {
  const p = istParts(value);
  return `${p.hour}:${p.minute}:${p.second} ${p.dayPeriod}`;
}

/** "10:31 AM" (IST) */
export function istShortTime(value: When): string {
  const p = istParts(value);
  return `${p.hour}:${p.minute} ${p.dayPeriod}`;
}

/** "29 Sep, 10:31 AM" (IST) */
export function istShortDateTime(value: When): string {
  const p = istParts(value);
  return `${p.day} ${p.month}, ${p.hour}:${p.minute} ${p.dayPeriod}`;
}

/** A contract's expiry day, "06 Oct 26" (IST). */
export function expiryDay(value: When): string {
  const p = istParts(value);
  return `${p.day.padStart(2, "0")} ${p.month} ${p.year.slice(-2)}`;
}
