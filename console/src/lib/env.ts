import path from "node:path";

/** Repo root (cache/ + reports/). Read lazily so `next build` does not need it. */
export function root(): string {
  const r = process.env.BOTTREAD_ROOT;
  if (!r) throw new Error("BOTTREAD_ROOT is not set");
  return path.resolve(r);
}

/** Minutes east of UTC of the host that writes cache/team.log (its asctime has no zone). */
export function logTzOffsetMin(): number {
  const n = parseInt(process.env.LOG_TZ_OFFSET_MIN ?? "", 10);
  return Number.isInteger(n) && n >= -720 && n <= 840 ? n : 420;
}

export const trustProxy = () => process.env.TRUST_PROXY === "1";
export const forceSecureCookie = () => process.env.COOKIE_SECURE === "1";
