/** Every trading time on the site is shown in India time -- the market's
 * own clock -- whatever time zone the viewer's device is set to. Formatting
 * in the device's zone showed a 10:31 IST entry as 09:01 on a laptop set to
 * Dubai time. Same "en-US" style the pages already used. */
const IST = "Asia/Kolkata";

const dateTimeFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: IST, month: "numeric", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
});
const dateFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, month: "numeric", day: "numeric", year: "numeric" });
const timeFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, hour: "numeric", minute: "2-digit", second: "2-digit" });
const shortDateTimeFmt = new Intl.DateTimeFormat("en-US", { timeZone: IST, month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });

type When = string | number | Date;

/** "9/29/2026, 10:31:12 AM" (IST) */
export function istDateTime(value: When): string {
  return dateTimeFmt.format(new Date(value));
}

/** "9/29/2026" (IST) */
export function istDate(value: When): string {
  return dateFmt.format(new Date(value));
}

/** "10:31:12 AM" (IST) */
export function istTime(value: When): string {
  return timeFmt.format(new Date(value));
}

/** "Sep 29, 10:31 AM" (IST) */
export function istShortDateTime(value: When): string {
  return shortDateTimeFmt.format(new Date(value));
}
