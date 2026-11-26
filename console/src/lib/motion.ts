// Pure helpers behind the console motion graphics (tick flash, slot bar, CB gauge, regime wave).
// Money stays a decimal STRING (IRON RULE 20): comparisons go through money.ts BigInt math, never parseFloat/Number.
import { diffAndPct, parseDec, type Dec } from "./money.ts";

/** "1,234.50 USDT" / "-1.37%" -> "1234.50" / "-1.37" so parseDec can read a formatted display string. */
const clean = (v: string) => v.replace(/[,\s]/g, "").replace(/(USDT|U|%)$/i, "");

/** Direction of a formatted decimal-string change: 1 up, -1 down, 0 same or unreadable ("-"). */
export function decDir(prev: string, next: string): -1 | 0 | 1 {
  if (prev === next) return 0;
  return diffAndPct(parseDec(clean(next)), parseDec(clean(prev))).sign;
}

/** "0.1234 → 0.1250" -> "0.1250": the market side of the entry → mark cell. */
export function markPart(v: string): string {
  const i = v.lastIndexOf("→");
  return (i >= 0 ? v.slice(i + 1) : v).trim();
}

const scaled = (d: Dec, scale: number): bigint => BigInt((d.int || "0") + d.frac.padEnd(scale, "0").slice(0, scale));

function usedPermille(dd: string, limit: string): bigint {
  const d = parseDec(clean(dd));
  const l = parseDec(clean(limit));
  if (!d || !l || !d.neg) return 0n;
  const ln = scaled(l, 4);
  if (ln <= 0n) return 0n;
  const p = (scaled(d, 4) * 1000n) / ln;
  return p > 1000n ? 1000n : p;
}

/** Share of the Circuit Breaker budget already used, in permille 0..1000: the larger of DD vs the USDT limit and DD% vs the % limit
 * (legacy -45 USDT / -5%, or the live Auto CB limits from agy_trader). A gain = 0. */
export function cbUsedPermille(ddUsdt: string, ddPct: string, limitUsdt = "45", limitPct = "5"): number {
  const a = usedPermille(ddUsdt, limitUsdt);
  const b = usedPermille(ddPct, limitPct);
  return Number(a > b ? a : b);   // a 0..1000 gauge ratio, not a money value
}

export type Polarity = "bull" | "bear" | "squeeze" | "flat";

/** Regime code from agy_trader -> wave polarity. */
export function regimePolarity(code: string | undefined): Polarity {
  const c = (code ?? "").toUpperCase();
  if (c.includes("SQUEEZE")) return "squeeze";
  if (c.startsWith("BULL")) return "bull";
  if (c.startsWith("BEAR") || c.startsWith("CRASH")) return "bear";
  return "flat";
}

export type SlotState = "long" | "short" | "free";

/** LONG slots first, then SHORT, then free; never fewer slots than open positions. */
export function slotStates(longN: number, shortN: number, max: number): SlotState[] {
  const total = Math.max(max, longN + shortN, 1);
  return Array.from({ length: total }, (_, i) => (i < longN ? "long" : i < longN + shortN ? "short" : "free"));
}

// ---- Airport split-flap: which characters / tokens actually changed ----
export interface FlapSeg { text: string; flip: boolean; old: string }
export type FlapToken =
  | { kind: "space"; text: string }
  | { kind: "num"; text: string; chars: FlapSeg[] }                 // monospace digits, each changed char = one equal-width slot
  | { kind: "word"; text: string; flip: boolean; old: string };     // any other word flips as one tile

/** A token whose characters flip one by one (prices, PnL, %, counts, times). Anything else flips as one whole word. */
const NUMERIC_TOKEN = /^[+\-]?[\d.,:/%]*\d[\d.,:/%]*$/;
const GRAPHEMES = typeof Intl !== "undefined" && "Segmenter" in Intl ? new Intl.Segmenter("th", { granularity: "grapheme" }) : null;

/** Grapheme clusters, so Thai vowels / tone marks never split from their base letter. */
export function graphemes(s: string): string[] {
  return GRAPHEMES ? Array.from(GRAPHEMES.segment(s), (x) => x.segment) : Array.from(s);
}

function pushSeg(out: FlapSeg[], text: string, flip: boolean, old: string) {
  const last = out[out.length - 1];
  if (!flip && last && !last.flip) last.text += text;
  else out.push({ text, flip, old: flip ? old : "" });
}

/**
 * Decimal-point aligned char diff: integer digits right-aligned, "." to ".", fraction digits left-aligned.
 * "0.241 -> 0.24199518" keeps 0.241 calm and flips only 99518; "895.50 -> 896.41" flips only 6, 4, 1.
 */
export function numericChars(prev: string | undefined, next: string): FlapSeg[] {
  const out: FlapSeg[] = [];
  const [ni, nf] = splitDot(next);
  const [pi, pf] = prev === undefined ? ["", null] : splitDot(prev);
  const a = graphemes(pi);
  const b = graphemes(ni);
  const off = a.length - b.length;
  b.forEach((ch, j) => pushSeg(out, ch, prev === undefined || a[j + off] !== ch, a[j + off] ?? ""));
  if (nf !== null) {
    pushSeg(out, ".", prev === undefined || pf === null, pf === null ? "" : ".");
    const fa = graphemes(pf ?? "");
    graphemes(nf).forEach((ch, j) => pushSeg(out, ch, prev === undefined || fa[j] !== ch, fa[j] ?? ""));
  }
  return out;
}

/** "-0.29 (-0.29%)" -> ["-0.29", " ", "(", "-0.29%", ")"] so a bracketed % still flips per digit. */
const tokenize = (s: string) => s.split(/(\s+|[()])/).filter((t) => t !== "");

function splitDot(s: string): [string, string | null] {
  const i = s.indexOf(".");
  return i < 0 ? [s, null] : [s.slice(0, i), s.slice(i + 1)];
}

/** Tokenise next (whitespace and ( ) kept as their own calm tokens) and diff it against prev; prev === next = everything calm. */
export function flapTokens(prev: string, next: string): FlapToken[] {
  const nt = tokenize(next);
  const ptRaw = tokenize(prev);
  const aligned = prev === next || ptRaw.length === nt.length;
  return nt.map((tok, i): FlapToken => {
    if (/^\s+$/.test(tok)) return { kind: "space", text: tok };
    const old = aligned ? ptRaw[i] : undefined;
    if (NUMERIC_TOKEN.test(tok)) {
      if (old === tok) return { kind: "num", text: tok, chars: [{ text: tok, flip: false, old: "" }] };
      return { kind: "num", text: tok, chars: numericChars(old !== undefined && NUMERIC_TOKEN.test(old) ? old : undefined, tok) };
    }
    return { kind: "word", text: tok, flip: old !== tok, old: old ?? "" };
  });
}

/** First numeric token of the market side ("0.12 → 0.13" -> "0.13", "3 / 8" -> "3", "ว่าง 1,234.50 USDT" -> "1,234.50"): what decides up / down. */
export function leadNumber(v: string): string {
  return markPart(v).split(/\s+/).find((t) => NUMERIC_TOKEN.test(t)) ?? "";
}

/** Ids present now but not in the previous render = brand-new rows (null previous = first render, nothing is new). */
export function newRowIds(prev: ReadonlySet<string> | null, ids: readonly string[]): Set<string> {
  const fresh = new Set<string>();
  if (prev) for (const id of ids) if (!prev.has(id)) fresh.add(id);
  return fresh;
}

export type GateState = SlotState | "more";
export interface Gate { state: GateState; label: string }

/**
 * Airport gate slots for the Position KPI. Capped mode: MAX_OPEN gates. Unlimited mode (maxOpen null): the gates in use
 * plus the gates the free margin can still open. More than `cap` gates folds the rest into one "+N" gate.
 */
export function gateSlots(longN: number, shortN: number, maxOpen: number | null, afford: number | null, cap = 10): Gate[] {
  const used = longN + shortN;
  const total = maxOpen !== null ? Math.max(maxOpen, used, 1) : Math.max(used + Math.max(0, afford ?? 0), 1);
  const gates: Gate[] = slotStates(longN, shortN, total).map((s, i) => ({ state: s, label: `G${i + 1}` }));
  if (gates.length <= cap) return gates;
  return [...gates.slice(0, cap - 1), { state: "more", label: `+${gates.length - cap + 1}` }];
}
