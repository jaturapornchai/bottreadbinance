import { arr, isObj, str, type Obj } from "./files.ts";
import * as M from "./money.ts";
import { fmtDM, fmtDMS } from "./time.ts";
import type { PfClosedView, PfLogView, PfOrderView, PfShortView, PortfolioView, SideCode } from "./types.ts";

/** A bot position the ปิดไม้ button may close: the server holds it, it is on Binance and no order of it is unclear.
 *  The /api/request route checks the same on cache/portfolio.json before it writes the close request. */
export const closable = (x: Obj): boolean => /^bto-[0-9A-Za-z-]{1,32}$/.test(str(x.id)) && x.on_binance === true && x.unclear !== true;

/** Per-coin extras from reports/<day>/screen.json (age, trend, drawdown, score, RSI). */
export interface Screen { side: SideCode; age: string; trend: string; dd: string; score: string; rsi1h: string; rsi4h: string }

/** Per-position live metadata from cache/agy-positions.json. */
export interface AgyPosMeta {
  setup: string;
  exitMode: string;
  stopPx: string;
  tpPx: string;
  stage: string;
  mfePct: string;
  maePct: string;
}

const prop = (o: Obj, k: string): unknown => (o[k] === null ? undefined : o[k]);
const D = (o: Obj, k: string) => M.parseDec(prop(o, k));
const bkk = (v: unknown) => fmtDM(str(v));
const sideOf = (o: Obj): SideCode => (str(o.side) === "LONG" ? "LONG" : "SHORT");
const badge = (c: SideCode) => (c === "LONG" ? "▲ LONG" : "▼ SHORT");

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);   // indicators, not money
const roundHalfAway = (x: number) => Math.sign(x) * Math.round(Math.abs(x));

/** screen.json rows -> per coin. dd_ath is a ratio (-0.37 = -37%), shown as a whole percent. */
export function screenByCoin(screen: unknown): Map<string, Screen> {
  const out = new Map<string, Screen>();
  for (const r of arr(isObj(screen) ? screen.rows : null)) {
    if (!isObj(r) || typeof r.base !== "string" || r.base === "") continue;
    const age = num(r.age_days), dd = num(r.dd_ath), sc = num(r.score);
    const pair = (a: unknown, b: unknown) => (num(a) !== null && num(b) !== null ? `${num(a)!.toFixed(1)} → ${num(b)!.toFixed(1)}` : "-");
    const n = dd === null ? 0 : roundHalfAway(dd * 100);
    out.set(r.base, {
      side: r.side === "LONG" || (r.side !== "SHORT" && /LONG/i.test(str(r.signal))) ? "LONG" : "SHORT",
      age: age === null ? "-" : String(age),
      trend: typeof r.trend_1d === "string" && r.trend_1d !== "" ? r.trend_1d : "-",
      dd: dd === null ? "-" : `${n > 0 ? "+" : ""}${n}%`,
      score: sc === null ? "-" : sc.toFixed(1),
      rsi1h: pair(r.rsi_prev, r.rsi), rsi4h: pair(r.rsi4_prev, r.rsi4),
    });
  }
  return out;
}

export function agyMetaById(raw: unknown): Map<string, AgyPosMeta> {
  const out = new Map<string, AgyPosMeta>();
  if (!isObj(raw)) return out;
  for (const [id, v] of Object.entries(raw)) {
    if (!isObj(v)) continue;
    out.set(id, {
      setup: str(v.setup) || "-",
      exitMode: str(v.exit_mode) || "-",
      stopPx: M.price(prop(v, "stop_px")),
      tpPx: M.price(prop(v, "tp_px")),
      stage: str(v.ratchet_stage) || "INIT",
      mfePct: M.ratioPct(prop(v, "mfe_pct")),
      maePct: M.ratioPct(prop(v, "mae_pct")),
    });
  }
  return out;
}

/** The screen score is per side (a coin has a LONG and a SHORT score): show it only when it belongs to the position's side. */
export const scoreFor = (s: Screen, side: SideCode) => (s.side === side ? s.score : "-");

/** Port of Portfolio.Parse. Every field is optional: after a failed refresh the file may hold only "error", or the previous snapshot plus "error". */
export function buildPortfolio(
  r: Obj,
  key: string,
  signals: Map<string, string>,
  screen: Map<string, Screen>,
  agyMeta: Map<string, AgyPosMeta> = new Map(),
): PortfolioView {
  const at = str(r.at);
  const err = str(r.error);
  const spot = D(r, "spot_usdt");
  const dash: Screen = { side: "SHORT", age: "-", trend: "-", dd: "-", score: "-", rsi1h: "-", rsi4h: "-" };
  const emptyMeta: AgyPosMeta = { setup: "-", exitMode: "-", stopPx: "-", tpPx: "-", stage: "INIT", mfePct: "-", maePct: "-" };

  const shorts: PfShortView[] = arr(r.shorts).filter(isObj).map((x) => {
    const id = str(x.id);
    const coin = str(x.coin);
    const am = agyMeta.get(id) ?? emptyMeta;
    const sig = (am.setup !== "-" ? am.setup : "") || signals.get(coin) || str(x.entry_signal) || "-";
    const code: SideCode = str(x.side) === "LONG" || /LONG/i.test(sig) ? "LONG" : "SHORT";
    const s = screen.get(coin) ?? dash;
    const opened = bkk(x.opened_at);
    const days = prop(x, "days") === undefined ? "-" : str(x.days);
    const state = x.unclear === true ? "มีคำสั่งไม่ชัด ตรวจใน Binance"
      : x.on_binance !== true ? "รอซิงก์ปิด"
      : "เปิดอยู่ · Fast-Guard 5 วิ";
    const pnl = D(x, "pnl_usdt"), q = D(x, "pnl_pct");
    return {
      sideCode: code, side: badge(code), coin, signal: sig, score: scoreFor(s, code), rsi1h: s.rsi1h, rsi4h: s.rsi4h, ageDays: s.age, trend1d: s.trend, ddAth: s.dd,
      qty: M.price(prop(x, "qty")),
      price: `${M.price(prop(x, "entry"))} → ${M.price(prop(x, "mark"))}`,
      liq: M.price(prop(x, "liq_price")), margin: M.amount(prop(x, "margin_usdt")),
      pnl: pnl ? M.fmtDec(pnl, { min: 2, max: 4, plus: true }) + (q ? ` (${M.pct(prop(x, "pnl_pct"))}%)` : "") : "-",
      pnlSign: M.signOf(pnl), funding: M.signed(prop(x, "funding_usdt")),
      held: days === "-" ? opened : `${opened} (${days}d)`, state, stateOk: state.startsWith("เปิดอยู่"),
      id, closable: closable(x),
      setup: am.setup !== "-" ? am.setup : sig,
      exitMode: am.exitMode,
      stopPx: am.stopPx,
      tpPx: am.tpPx,
      stage: am.stage,
      mfePct: am.mfePct,
      maePct: am.maePct,
    };
  });

  const closed: PfClosedView[] = arr(r.closed).filter(isObj).map((x) => {
    const code = sideOf(x), pnl = D(x, "pnl_usdt");
    return {
      sideCode: code, side: badge(code), coin: str(x.coin), qty: M.price(prop(x, "qty")), entry: M.price(prop(x, "entry")),
      opened: bkk(x.opened_at), closed: bkk(x.closed_at), why: str(x.why),
      pnl: M.signed(prop(x, "pnl_usdt")), pnlSign: M.signOf(pnl), funding: M.signed(prop(x, "funding_usdt")), fees: M.amount(prop(x, "fees_usdt")),
      alertPnl: pnl ? M.alertPnl(prop(x, "pnl_usdt")) : "",
    };
  });

  const orders: PfOrderView[] = arr(r.orders).filter(isObj).map((x) => {
    const long = str(x.side) === "LONG", kind = str(x.kind);
    const label = kind === "open" ? (long ? "เปิด long" : "เปิด short") : kind === "close" ? (long ? "ปิด long" : "ปิด short")
      : kind === "closed" ? "บันทึกปิด" : kind === "stop" ? "stop-loss" : kind === "tp" ? "take-profit" : kind;
    const status = [str(x.status), str(x.how)].filter((w) => w.length > 0).join(" · ");
    return {
      when: bkk(x.at), kind: label, symbol: str(x.symbol), qty: M.price(prop(x, "qty")), usdt: M.amount(prop(x, "quote_qty")),
      price: M.price(prop(x, "avg_price")), status: status || "-",
    };
  });

  const log: PfLogView[] = arr(r.log).filter(isObj).map((x) => ({ when: bkk(x.at), run: str(x.run_id), text: str(x.text) }));

  const unrl = D(r, "unrealized_usdt"), real = D(r, "realized_usdt"), lot = D(r, "lot_usdt");
  return {
    key, hasGood: at.length > 0, mode: str(r.mode), at: at ? fmtDMS(at) : "", rule: str(r.rule),
    error: err ? "⚠ อัปเดตพอร์ตล่าสุดไม่สำเร็จ: " + err + (str(r.error_at) ? ` (${bkk(r.error_at)})` : "") : "",
    spot: spot && M.signOf(spot) > 0 ? `USDT ใน Spot ${M.amount(prop(r, "spot_usdt"))} (ต้องโอนเข้า USDⓈ-M Futures ก่อนบอทใช้ได้)` : "",
    equity: M.kpi(prop(r, "equity_usdt")), available: M.kpi(prop(r, "available_usdt")),
    unrealized: M.kpiSigned(prop(r, "unrealized_usdt")), unrealizedSign: M.signOf(unrl),
    realized: M.kpiSigned(prop(r, "realized_usdt")), realizedSign: M.signOf(real),
    lot: lot ? M.kpi(prop(r, "lot_usdt")) : "ยังไม่ได้ตั้ง", hasLot: lot !== null,
    shorts, closed, orders, log,
  };
}
