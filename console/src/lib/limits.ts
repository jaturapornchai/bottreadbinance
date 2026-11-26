// Live trade limits for the console (pure: no fs), read from agy-guard-heartbeat.json first (fresh every 10 s),
// then the last agy-autotrade.log round, then agy-state.json. Money stays a decimal STRING (IRON RULE 20).
import * as M from "./money.ts";

type Src = Record<string, unknown> | null;

export interface TradeLimits {
  leverage: number;
  marginUsdt: string;
  lotUsdt: string;
  minAvailUsdt: string;
  maxOpen: number | null;      // null = unlimited (max_open 0): opens are bounded by available >= minAvailUsdt
  maxLong: number | null;
  cbAuto: boolean;
  cbRefEquity: string;         // raw decimal string the drawdown is measured from ("" = unknown)
  cbLimitUsdt: string;         // "45.00" / auto "68.76"
  cbLimitPct: string;          // "5.00" / auto "7.50"
}

const present = (v: unknown) => v !== undefined && v !== null && v !== "";
function pick(srcs: readonly Src[], key: string): unknown {
  for (const s of srcs) if (s && present(s[key])) return s[key];
  return undefined;
}
const decStr = (v: unknown): string => (typeof v === "string" ? v.trim() : typeof v === "number" && Number.isFinite(v) ? String(v) : "");

/** max_open style count: 0 = unlimited (null), > 0 = that cap, missing = legacy fallback. */
function capOf(v: unknown, legacy: number | null): number | null {
  if (typeof v !== "number" || !Number.isFinite(v)) return legacy;
  return v > 0 ? Math.trunc(v) : null;
}

export function readLimits(hb: Src, lastAuto: Src, state: Src): TradeLimits {
  const live = [hb, lastAuto];
  const lev = pick(live, "leverage");
  const maxOpen = capOf(pick(live, "max_open"), 8);
  const maxLong = capOf(pick(live, "max_long_open"), maxOpen);
  const cbAuto = pick([hb, state, lastAuto], "cb_auto") === true;
  const ref = decStr(pick([hb, state], "cb_ref_equity_usdt"))
    || decStr(state?.[cbAuto ? "peak_equity_usdt" : "baseline_equity_usdt"])
    || decStr(state?.baseline_equity_usdt);
  return {
    leverage: typeof lev === "number" && lev > 0 ? Math.trunc(lev) : 2,
    marginUsdt: M.kpi(pick(live, "margin_usdt") ?? "100"),
    lotUsdt: M.kpi(pick(live, "lot_usdt") ?? "200"),
    minAvailUsdt: M.kpi(pick(live, "min_avail_usdt") ?? "101"),
    maxOpen,
    maxLong,
    cbAuto,
    cbRefEquity: ref,
    cbLimitUsdt: M.kpi(pick([hb, state, lastAuto], "cb_limit_usdt") ?? "45"),
    cbLimitPct: M.kpi(pick([hb, state, lastAuto], "cb_limit_pct") ?? "5"),
  };
}

/** How many more positions the free margin can open: floor(available / minAvail), exact BigInt; null = unknown. */
export function affordMore(available: unknown, minAvail: unknown): number | null {
  const a = M.parseDec(typeof available === "string" ? available.replace(/,/g, "") : available);
  const m = M.parseDec(typeof minAvail === "string" ? minAvail.replace(/,/g, "") : minAvail);
  if (!a || !m || M.signOf(m) <= 0) return null;
  if (M.signOf(a) <= 0) return 0;
  const scale = Math.max(a.frac.length, m.frac.length);
  const an = BigInt(a.int + a.frac.padEnd(scale, "0"));
  const mn = BigInt(m.int + m.frac.padEnd(scale, "0"));
  return Number(an / mn);   // a whole position count, not money
}
