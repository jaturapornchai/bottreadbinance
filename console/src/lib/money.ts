// Money is a decimal STRING end to end (IRON RULE 20). Nothing here uses parseFloat/Number on a money value:
// strings are split into digit runs, rounded half away from zero with BigInt, and re-joined.

export interface Dec { neg: boolean; int: string; frac: string }
export interface Fmt { min: number; max: number; plus?: boolean; group?: boolean }

const DEC_RE = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d{1,3}))?$/;

/** "1E-7" (Python Decimal), "-0.05", "1234.50"; a JSON number is accepted as a fallback via String(). Empty / garbage = null. */
export function parseDec(v: unknown): Dec | null {
  const s = typeof v === "string" ? v.trim() : typeof v === "number" && Number.isFinite(v) ? String(v) : "";
  const m = s.length > 0 && s.length <= 80 ? DEC_RE.exec(s) : null;
  if (!m) return null;
  const [, sign, i = "", f = "", e = "0"] = m;
  if (i === "" && f === "") return null;
  const digits = i + f;
  const point = i.length + parseInt(e, 10);   // exponent = a digit shift count, not money
  let int: string, frac: string;
  if (point <= 0) { int = "0"; frac = "0".repeat(-point) + digits; }
  else if (point >= digits.length) { int = digits + "0".repeat(point - digits.length); frac = ""; }
  else { int = digits.slice(0, point); frac = digits.slice(point); }
  int = int.replace(/^0+(?=\d)/, "");
  return { neg: sign === "-" && /[1-9]/.test(int + frac), int, frac };
}

export const signOf = (d: Dec | null): -1 | 0 | 1 => (!d || !/[1-9]/.test(d.int + d.frac) ? 0 : d.neg ? -1 : 1);

/** Round the magnitude to `scale` decimals, half away from zero (what .NET decimal.ToString does). */
function round(d: Dec, scale: number): Dec {
  if (d.frac.length <= scale) return d;
  const head = d.int + d.frac.slice(0, scale);
  let n = head;
  if (d.frac.charCodeAt(scale) >= 53) n = (BigInt(head) + 1n).toString().padStart(head.length, "0");   // next digit >= '5'
  return { neg: d.neg, int: n.slice(0, n.length - scale) || "0", frac: n.slice(n.length - scale) };
}

const grp = (int: string) => int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

/** .NET style "#,##0.00##" = {min:2,max:4}; "#,##0.############################" = {min:0,max:28}; plus = "+" on positives. null = "-". */
export function fmtDec(d: Dec | null, o: Fmt): string {
  if (!d) return "-";
  const r = round(d, o.max);
  let frac = r.frac.replace(/0+$/, "");
  if (frac.length < o.min) frac = frac.padEnd(o.min, "0");
  const zero = !/[1-9]/.test(r.int + r.frac);
  const sign = zero ? "" : r.neg ? "-" : o.plus ? "+" : "";
  return sign + (o.group === false ? r.int : grp(r.int)) + (frac ? "." + frac : "");
}

export const price = (v: unknown) => fmtDec(parseDec(v), { min: 0, max: 28 });             // qty, entry, mark, liquidation
export const amount = (v: unknown) => fmtDec(parseDec(v), { min: 2, max: 4 });             // margin, USDT, fees
export const signed = (v: unknown) => fmtDec(parseDec(v), { min: 2, max: 4, plus: true }); // pnl, funding
export const kpi = (v: unknown) => fmtDec(parseDec(v), { min: 2, max: 2 });
export const kpiSigned = (v: unknown) => fmtDec(parseDec(v), { min: 2, max: 2, plus: true });
export const pct = (v: unknown) => fmtDec(parseDec(v), { min: 2, max: 2, plus: true, group: false });
export const alertPnl = kpiSigned;

/** Shift a parsed decimal ratio x100 using exact string arithmetic (0.095 -> 9.5, -0.012 -> -1.2). */
export function mul100(d: Dec | null): Dec | null {
  if (!d) return null;
  const f = d.frac.padEnd(2, "0");
  const int = (d.int + f.slice(0, 2)).replace(/^0+(?=\d)/, "");
  const frac = f.slice(2);
  return { neg: d.neg && /[1-9]/.test(int + frac), int, frac };
}

/** Format a decimal ratio (e.g. "0.095") as a signed percentage string ("+9.50%"). */
export function ratioPct(v: unknown): string {
  const m = mul100(parseDec(v));
  return m ? `${fmtDec(m, { min: 2, max: 2, plus: true, group: false })}%` : "-";
}

function fromScaled(n: bigint, scale: number): Dec {
  const neg = n < 0n;
  const abs = neg ? -n : n;
  const raw = abs.toString().padStart(scale + 1, "0");
  const int = scale === 0 ? raw : raw.slice(0, -scale);
  const frac = scale === 0 ? "" : raw.slice(-scale);
  return { neg: neg && /[1-9]/.test(int + frac), int, frac };
}

/** Exact BigInt difference `(a - b)` plus percentage `((a - b) / b) * 100` rounded half away from zero to 2 decimals. */
export function diffAndPct(a: Dec | null, b: Dec | null): { diff: Dec | null; pctStr: string; sign: -1 | 0 | 1 } {
  if (!a || !b) return { diff: null, pctStr: "-", sign: 0 };
  const scale = Math.max(a.frac.length, b.frac.length);
  const an = BigInt(a.int + a.frac.padEnd(scale, "0")) * (a.neg ? -1n : 1n);
  const bn = BigInt(b.int + b.frac.padEnd(scale, "0")) * (b.neg ? -1n : 1n);
  const dn = an - bn;
  const diff = fromScaled(dn, scale);
  const sign = signOf(diff);
  if (bn <= 0n) return { diff, pctStr: "-", sign };
  const absNum = (dn < 0n ? -dn : dn) * 100000n;
  const q10 = absNum / bn;
  const rounded = (q10 / 10n) + (q10 % 10n >= 5n ? 1n : 0n);
  const pctDec = fromScaled(dn < 0n ? -rounded : rounded, 2);
  return { diff, pctStr: `${fmtDec(pctDec, { min: 2, max: 2, plus: true, group: false })}%`, sign };
}

/** Whole-number counts (calls, tokens): digit grouping only. */
export const count = (n: number) => grp(String(Math.trunc(n)));

// ---- AI cost: tokens x USD per 1M tokens, kept as integers (1e-8 USD) so sums are exact ----
// Prices in US cents per 1M tokens (in, out); uncached peak input = upper bound. DeepSeek API: api-docs.deepseek.com/quick_start/pricing
// read 2026-11-06 (team switched to it that day); the rest are Ollama Cloud names in older log lines (ollama.com/pricing, 2026-10-11).
const PRICES: readonly (readonly [prefix: string, inCents: bigint, outCents: bigint])[] = [
  ["gpt-oss:120b", 15n, 60n], ["kimi-k2.6", 95n, 400n],
  ["deepseek-v4.1-flash", 30n, 120n], ["deepseek-v4-pro", 132n, 396n], ["deepseek-flash", 30n, 120n],
];

/** Cost in 1e-8 USD; null = model has no known price. */
export function costE8(model: string, tokensIn: number, tokensOut: number): bigint | null {
  const p = PRICES.find((x) => model.startsWith(x[0]));
  return p ? BigInt(tokensIn) * p[1] + BigInt(tokensOut) * p[2] : null;
}

export function fmtE8(e8: bigint, decimals: number): string {
  const neg = e8 < 0n;
  const s = (neg ? -e8 : e8).toString().padStart(9, "0");
  return fmtDec({ neg, int: s.slice(0, -8), frac: s.slice(-8) }, { min: decimals, max: decimals });
}
