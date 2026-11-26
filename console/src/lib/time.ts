// Everything the user sees is Thai time (Asia/Bangkok = UTC+7, no DST), whatever the server timezone. Gregorian year always.
const BKK_MS = 7 * 3600_000;
const p2 = (n: number) => String(n).padStart(2, "0");

const ISO_RE = /^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d(?::\d\d(?:\.\d+)?)?)\s*(Z|[+-]\d\d(?::?\d\d)?)?$/i;

/** Epoch ms of an ISO timestamp; no offset in the text = UTC (like AssumeUniversal in the WPF app). null = not a timestamp. */
export function parseIso(s: string | null | undefined): number | null {
  const m = s ? ISO_RE.exec(s.trim()) : null;
  if (!m) return null;
  let tz = m[3] ?? "Z";
  if (/^[+-]\d\d$/.test(tz)) tz += ":00";
  else if (/^[+-]\d\d\d\d$/.test(tz)) tz = tz.slice(0, 3) + ":" + tz.slice(3);
  const hms = m[2].length === 5 ? m[2] + ":00" : m[2];
  const t = Date.parse(`${m[1]}T${hms}${tz.toUpperCase()}`);
  return Number.isNaN(t) ? null : t;
}

const parts = (ms: number) => { const d = new Date(ms + BKK_MS); return { y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds() }; };

export const bkkDate = (ms: number) => { const t = parts(ms); return `${t.y}-${p2(t.mo)}-${p2(t.d)}`; };
export const bkkHM = (ms: number) => { const t = parts(ms); return `${p2(t.h)}:${p2(t.mi)}`; };
export const bkkHMS = (ms: number) => { const t = parts(ms); return `${p2(t.h)}:${p2(t.mi)}:${p2(t.s)}`; };
export const bkkDateTime = (ms: number) => `${bkkDate(ms)} ${bkkHMS(ms)}`;

/** "dd/MM HH:mm" (Thai time); text that is not a timestamp comes back unchanged. */
export function fmtDM(iso: string): string {
  const ms = parseIso(iso);
  if (ms === null) return iso;
  const t = parts(ms);
  return `${p2(t.d)}/${p2(t.mo)} ${p2(t.h)}:${p2(t.mi)}`;
}
export function fmtDMS(iso: string): string {
  const ms = parseIso(iso);
  return ms === null ? iso : `${fmtDM(iso)}:${p2(parts(ms).s)}`;
}

/** team.log stamps are the Python host's local time; offsetMin = minutes east of UTC of that host. Returns the true epoch ms. */
export function logStampMs(day: string, time: string, offsetMin: number): number {
  const [y, mo, d] = day.split("-").map((x) => parseInt(x, 10));
  const [h, mi, s] = time.split(":").map((x) => parseInt(x, 10));
  return Date.UTC(y, mo - 1, d, h, mi, s) - offsetMin * 60_000;
}
