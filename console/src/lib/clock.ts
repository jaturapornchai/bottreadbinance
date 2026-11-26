// Airport FIDS footer clock: Thai short date, Gregorian year, Asia/Bangkok (UTC+7, no DST) — same rule as time.ts.
const BKK_MS = 7 * 3600_000;
const WEEKDAY_TH = ["อา.", "จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส."];
const MONTH_TH = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];

/** epoch ms -> "พฤ. 26 พ.ย. 2026" */
export function bkkThaiDate(ms: number): string {
  const d = new Date(ms + BKK_MS);
  return `${WEEKDAY_TH[d.getUTCDay()]} ${d.getUTCDate()} ${MONTH_TH[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}
