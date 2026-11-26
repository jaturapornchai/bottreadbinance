// Pure TradingView indicator math, auto price-precision, and Binance klines validation/sanitization.
// Chart coordinates use JS numbers (required by lightweight-charts Canvas); accounting/money calculations remain in money.ts.

export const KLINE_INTERVALS = ["15m", "1h", "4h", "1d"] as const;
export type KlineInterval = (typeof KLINE_INTERVALS)[number];

export interface Candle {
  time: number;     // Unix seconds UTC (strictly ascending)
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

const SYMBOL_RE = /^[A-Z0-9\u4e00-\u9fff]{1,20}USDT$/;
const BKK_OFFSET_S = 7 * 3600;
const MONTHS_TH = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];
const p2 = (n: number) => String(n).padStart(2, "0");

/** Strict API validation: uppercase `[A-Z0-9\u4e00-\u9fff]{1,20}USDT`, null if invalid. */
export function normalizeSymbol(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const s = raw.trim().toUpperCase();
  return SYMBOL_RE.test(s) ? s : null;
}

/** UI helper: "ARIA" -> "ARIAUSDT", "BTCUSDT" -> "BTCUSDT", invalid -> null. */
export function toUsdtSymbol(coinOrSymbol: string | null | undefined): string | null {
  if (typeof coinOrSymbol !== "string") return null;
  const s = coinOrSymbol.trim().toUpperCase();
  if (!s) return null;
  const sym = s.endsWith("USDT") ? s : `${s}USDT`;
  return SYMBOL_RE.test(sym) ? sym : null;
}

/** "ARIAUSDT" -> "ARIA", "BTC" -> "BTC". */
export function coinFromSymbol(symbol: string): string {
  const s = symbol.trim().toUpperCase();
  return s.endsWith("USDT") && s.length > 4 ? s.slice(0, -4) : s;
}

/** Whitelist interval; missing/empty defaults to "15m", unknown returns null (400). */
export function parseInterval(raw: string | null | undefined): KlineInterval | null {
  if (raw === null || raw === undefined || raw.trim() === "") return "15m";
  const v = raw.trim().toLowerCase();
  return (KLINE_INTERVALS as readonly string[]).includes(v) ? (v as KlineInterval) : null;
}

/** Clamp candle limit into [60, 400], default 240. */
export function clampLimit(raw: string | null | undefined): number {
  if (raw === null || raw === undefined || raw.trim() === "") return 240;
  const n = Number.parseInt(raw.trim(), 10);
  if (!Number.isFinite(n)) return 240;
  return Math.min(400, Math.max(60, n));
}

/**
 * Sanitize raw Binance `/fapi/v1/klines` JSON rows into deduplicated, strictly ascending Unix-second candles.
 * Drops malformed / non-finite / non-positive price rows safely without throwing.
 */
export function parseBinanceKlines(raw: unknown): Candle[] {
  if (!Array.isArray(raw)) return [];
  const byTime = new Map<number, Candle>();
  for (const row of raw) {
    if (!Array.isArray(row) || row.length < 6) continue;
    const tMs = typeof row[0] === "number" ? row[0] : Number(row[0]);
    const open = Number(row[1]);
    const high = Number(row[2]);
    const low = Number(row[3]);
    const close = Number(row[4]);
    const volume = Number(row[5]);
    if (
      !Number.isFinite(tMs) || tMs <= 0
      || !Number.isFinite(open) || open <= 0
      || !Number.isFinite(high) || high <= 0
      || !Number.isFinite(low) || low <= 0
      || !Number.isFinite(close) || close <= 0
      || !Number.isFinite(volume) || volume < 0
      || high < low
    ) {
      continue;
    }
    const time = Math.floor(tMs / 1000);
    byTime.set(time, { time, open, high, low, close, volume });
  }
  return Array.from(byTime.values()).sort((a, b) => a.time - b.time);
}

/** Simple Moving Average (SMA). First `period - 1` slots are null. */
export function calcSma(values: readonly number[], period: number): (number | null)[] {
  const n = values.length;
  const out: (number | null)[] = new Array(n).fill(null);
  const p = Math.trunc(period);
  if (p < 1 || n < p) return out;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    sum += values[i];
    if (i >= p) sum -= values[i - p];
    if (i >= p - 1) out[i] = sum / p;
  }
  return out;
}

/** Exponential Moving Average (TradingView standard: seeded by SMA of first `period` closes, k = 2/(p+1)). */
export function calcEma(values: readonly number[], period: number): (number | null)[] {
  const n = values.length;
  const out: (number | null)[] = new Array(n).fill(null);
  const p = Math.trunc(period);
  if (p < 1 || n < p) return out;
  let sum = 0;
  for (let i = 0; i < p; i++) sum += values[i];
  let ema = sum / p;
  out[p - 1] = ema;
  const k = 2 / (p + 1);
  for (let i = p; i < n; i++) {
    ema = values[i] * k + ema * (1 - k);
    out[i] = ema;
  }
  return out;
}

/** Relative Strength Index (Wilder smoothing / TradingView `ta.rma`). First `period` slots are null. */
export function calcRsi(values: readonly number[], period = 14): (number | null)[] {
  const n = values.length;
  const out: (number | null)[] = new Array(n).fill(null);
  const p = Math.trunc(period);
  if (p < 1 || n <= p) return out;
  let gainSum = 0;
  let lossSum = 0;
  for (let i = 1; i <= p; i++) {
    const d = values[i] - values[i - 1];
    if (d > 0) gainSum += d;
    else if (d < 0) lossSum -= d;
  }
  let avgGain = gainSum / p;
  let avgLoss = lossSum / p;
  const toRsi = (g: number, l: number) => (l === 0 ? (g === 0 ? 50 : 100) : 100 - 100 / (1 + g / l));
  out[p] = toRsi(avgGain, avgLoss);
  for (let i = p + 1; i < n; i++) {
    const d = values[i] - values[i - 1];
    const g = d > 0 ? d : 0;
    const l = d < 0 ? -d : 0;
    avgGain = (avgGain * (p - 1) + g) / p;
    avgLoss = (avgLoss * (p - 1) + l) / p;
    out[i] = toRsi(avgGain, avgLoss);
  }
  return out;
}

function observedDecimals(v: number): number {
  const s = v.toFixed(8).replace(/0+$/, "");
  const dot = s.indexOf(".");
  return dot < 0 ? 0 : s.length - dot - 1;
}

/**
 * Automatic price precision & minMove for lightweight-charts axis/crosshair.
 * Ensures low-priced altcoins (e.g. 0.004895 -> precision 6) are never rounded down to 0.00.
 */
export function calcPricePrecision(prices: readonly number[]): { precision: number; minMove: number } {
  const valid = prices.filter((v) => Number.isFinite(v) && v > 0);
  if (valid.length === 0) return { precision: 2, minMove: 0.01 };
  const ref = valid[valid.length - 1];
  let floor = 2;
  if (ref < 0.0001) floor = 8;
  else if (ref < 0.001) floor = 7;
  else if (ref < 0.01) floor = 6;
  else if (ref < 0.1) floor = 5;
  else if (ref < 10) floor = 4;
  else if (ref < 100) floor = 3;
  else floor = 2;

  let maxObs = 0;
  // inspect up to last 60 prices for actual tick decimals
  const start = Math.max(0, valid.length - 60);
  for (let i = start; i < valid.length; i++) {
    const d = observedDecimals(valid[i]);
    if (d > maxObs) maxObs = d;
  }
  const precision = Math.min(8, Math.max(floor, maxObs));
  const minMove = Number((10 ** -precision).toFixed(precision));
  return { precision, minMove };
}

/** Format chart price with adaptive grouping (commas for >= 1000) and exact decimal places. */
export function formatChartPrice(v: number | null | undefined, precision: number): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  const p = Math.min(8, Math.max(0, Math.trunc(precision)));
  if (Math.abs(v) >= 1000) {
    return v.toLocaleString("en-US", { minimumFractionDigits: p, maximumFractionDigits: p });
  }
  return v.toFixed(p);
}

/** Compact volume formatter (K / M / B). */
export function formatVolume(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "-";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(2);
}

/** Parse a display price string ("0.092885", "89,450.20") into a finite positive number for Canvas price lines. */
export function parseChartPrice(s: string | null | undefined): number | null {
  if (!s) return null;
  const cleaned = s.trim().replace(/,/g, "");
  if (!cleaned || cleaned === "-") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Extract entry price (left of "→") from a formatted "entry → mark" cell string. */
export function extractEntryPrice(rangeStr: string | null | undefined): number | null {
  if (!rangeStr) return null;
  const i = rangeStr.indexOf("→");
  return parseChartPrice(i >= 0 ? rangeStr.slice(0, i) : rangeStr);
}

/** Format Unix seconds as Thai time (Asia/Bangkok = UTC+7) for crosshair tooltip: "26 พ.ย. 13:45". */
export function bkkCrosshairTime(unixSec: number): string {
  const d = new Date((unixSec + BKK_OFFSET_S) * 1000);
  return `${d.getUTCDate()} ${MONTHS_TH[d.getUTCMonth()]} ${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
}

/** Format Unix seconds for the bottom time axis in Asia/Bangkok (UTC+7). */
export function bkkTickLabel(unixSec: number, isDayOrAbove: boolean): string {
  const d = new Date((unixSec + BKK_OFFSET_S) * 1000);
  if (isDayOrAbove) return `${d.getUTCDate()} ${MONTHS_TH[d.getUTCMonth()]}`;
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}`;
}
