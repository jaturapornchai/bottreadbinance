import { readdir } from "node:fs/promises";
import path from "node:path";
import { logTzOffsetMin, root } from "./env.ts";
import { arr, dayDirs, isObj, MB, readCapped, readJson, statOrNull, str, type Obj } from "./files.ts";
import { LogTail } from "./logtail.ts";
import * as M from "./money.ts";
import { buildPlan } from "./plan.ts";
import { affordMore, readLimits } from "./limits.ts";
import { agyMetaById, buildPortfolio, screenByCoin } from "./portfolio.ts";
import { TEAM } from "./team.ts";
import { bkkDate, fmtDMS, parseIso } from "./time.ts";
import type {
  AgyGuardRow, AgyRoundRow, AgyStatusView, BotDetailView, ChatLine, CostRow,
  MemberView, PlanView, PnlSign, PortfolioView, RunnerView, Snapshot, Tone,
} from "./types.ts";

const RUNNER_FRESH_MS = 30_000;
const GUARD_FRESH_MS = 10 * 60_000;   // fast-guard writes heartbeat every 1 min and guard.log every 5 min

const g = globalThis as unknown as { __btLog?: LogTail };
const logTail = () => (g.__btLog ??= new LogTail(path.join(root(), "cache", "team.log"), logTzOffsetMin()));
const cut = (t: string, n: number) => { const c = Array.from(t); return c.length <= n ? t : c.slice(0, n).join("") + "…"; };

export async function loadRunner(r: string, now: number): Promise<RunnerView> {
  const down = (note: string): RunnerView => ({
    up: false, note, updatedAt: "", teamRunning: false, pid: null, nextAuto: null, waitS: 900, kafkaUp: false, status: "", portfolioAt: "", kafkaAt: "",
  });
  const j = await readJson(path.join(r, "cache", "runner.json"), 64 * 1024);
  if (!isObj(j)) return down("ใช้โหมด Monitor + Antigravity V23");
  const updated = parseIso(str(j.updated));
  const up = updated !== null && Math.abs(now - updated) <= RUNNER_FRESH_MS;
  const next = parseIso(str(j.next_auto));
  const wait = typeof j.wait_s === "number" && Number.isFinite(j.wait_s) ? Math.min(86400, Math.max(1, Math.round(j.wait_s))) : 900;
  const at = (v: unknown) => (str(v) ? fmtDMS(str(v)) : "");
  return {
    up, note: up ? "" : "",
    updatedAt: updated === null ? "" : fmtDMS(new Date(updated).toISOString()),
    teamRunning: up && j.team_running === true,
    pid: typeof j.team_pid === "number" && Number.isInteger(j.team_pid) ? j.team_pid : null,
    nextAuto: next === null ? null : new Date(next).toISOString(), waitS: wait,
    kafkaUp: up && j.kafka_up === true, status: str(j.status), portfolioAt: at(j.portfolio_at), kafkaAt: at(j.kafka_at),
  };
}

/** True when server `fast-guard` (which executes manual close/portfolio requests every 5s) is alive. */
export async function isGuardAlive(r: string, now: number): Promise<boolean> {
  const hb = await readJson(path.join(r, "cache", "agy-guard-heartbeat.json"), 16 * 1024);
  if (isObj(hb)) {
    const t = parseIso(str(hb.at));
    if (t !== null && Math.abs(now - t) <= 180_000) return true;
  }
  const stGuard = await statOrNull(path.join(r, "reports", "agy-guard.log"));
  if (stGuard?.isFile() && Math.abs(now - stGuard.mtimeMs) <= GUARD_FRESH_MS) return true;
  const stState = await statOrNull(path.join(r, "cache", "agy-state.json"));
  if (stState?.isFile() && Math.abs(now - stState.mtimeMs) <= GUARD_FRESH_MS) return true;
  return false;
}

function next15mIso(nowMs: number): string {
  const slotMs = 15 * 60_000;
  const settleMs = 5 * 60_000;   // aligned to :05, :20, :35, :50 after ClickHouse settles 15m bars
  const base = Math.floor(nowMs / slotMs) * slotMs + settleMs;
  const next = base > nowMs ? base : base + slotMs;
  return new Date(next).toISOString();
}

function parseJsonLines(text: string | null, limit: number): Obj[] {
  if (!text) return [];
  const out: Obj[] = [];
  const lines = text.split("\n");
  for (let i = lines.length - 1; i >= 0 && out.length < limit; i--) {
    const s = lines[i].trim();
    if (!s.startsWith("{") || !s.endsWith("}")) continue;
    try {
      const obj: unknown = JSON.parse(s);
      if (isObj(obj)) out.push(obj);
    } catch { /* ignore partial line */ }
  }
  return out;
}

function countOpenedTodayUtc(pfRaw: unknown, nowMs: number): number {
  if (!isObj(pfRaw)) return 0;
  const todayUtc = new Date(nowMs).toISOString().slice(0, 10);
  const syms = new Set<string>();
  for (const o of arr(pfRaw.orders)) {
    if (isObj(o) && str(o.kind) === "open" && str(o.how) !== "rejected" && str(o.at).startsWith(todayUtc) && str(o.symbol)) {
      syms.add(str(o.symbol));
    }
  }
  for (const p of arr(pfRaw.shorts)) {
    if (isObj(p) && (str(p.run_id) === todayUtc || str(p.opened_at).startsWith(todayUtc)) && str(p.symbol)) {
      syms.add(str(p.symbol));
    }
  }
  for (const c of arr(pfRaw.closed)) {
    if (isObj(c) && str(c.opened_at).startsWith(todayUtc) && str(c.symbol)) {
      syms.add(str(c.symbol));
    }
  }
  return syms.size;
}

async function loadAgyStatus(r: string, now: number, pfRaw: unknown, plan: PlanView | null): Promise<AgyStatusView> {
  const [stateJson, autoTxt, guardTxt, hbJson, guardSt] = await Promise.all([
    readJson(path.join(r, "cache", "agy-state.json"), 64 * 1024),
    readCapped(path.join(r, "reports", "agy-autotrade.log"), 512 * 1024, true),
    readCapped(path.join(r, "reports", "agy-guard.log"), 512 * 1024, true),
    readJson(path.join(r, "cache", "agy-guard-heartbeat.json"), 16 * 1024),
    statOrNull(path.join(r, "reports", "agy-guard.log")),
  ]);

  const autoObjs = parseJsonLines(autoTxt, 60);
  const guardObjs = parseJsonLines(guardTxt, 60);

  const recentRounds: AgyRoundRow[] = autoObjs.map((o) => {
    const opened = arr(o.opened).filter((x) => isObj(x) && (x.ok === true || x.dry_run === true)).length;
    const closed = arr(o.closed).filter((x) => isObj(x) && (x.ok === true || x.dry_run === true)).length;
    const comp = typeof o.complete_1h_pct === "number" ? `${o.complete_1h_pct.toFixed(0)}%` : "-";
    const hOpen = str(o.open_heat_usdt) || "0.00";
    const hMaxRaw = str(o.max_heat_usdt);
    const heat = M.signOf(M.parseDec(hMaxRaw)) > 0 ? `${hOpen}/${hMaxRaw}U` : `${hOpen}U`;   // decimal compare, no float
    return {
      at: str(o.at_th) || "-",
      ok: o.ok === true,
      strategy: str(o.strategy) || "-",
      regime: str(o.regime) || "-",
      complete1h: comp,
      cands: typeof o.candidates_found === "number" ? o.candidates_found : 0,
      opened,
      closed,
      heat,
      equity: M.kpi(o.equity_usdt),
      openCount: typeof o.open_count === "number" ? o.open_count : 0,
    };
  });

  const recentGuards: AgyGuardRow[] = guardObjs.map((o) => {
    const closedArr = arr(o.closed).filter(isObj);
    const closedText = closedArr.length > 0
      ? closedArr.map((c) => `${str(c.symbol) || str(c.id)} (${str(c.pnl_usdt) || "n/a"}U)`).join(", ")
      : "-";
    return {
      at: str(o.at_th) || "-",
      ok: o.ok === true,
      checked: typeof o.checked === "number" ? o.checked : 0,
      closedCount: closedArr.length,
      closedText,
      cbTripped: o.cb_tripped === true,
      equity: M.kpi(o.equity_usdt),
    };
  });

  const lastAuto = autoObjs[0] ?? null;
  const lastGuard = guardObjs[0] ?? null;
  let guardUp = false;
  let guardAt = lastGuard ? str(lastGuard.at_th) : "";
  if (isObj(hbJson)) {
    const hbMs = parseIso(str(hbJson.at));
    if (hbMs !== null && Math.abs(now - hbMs) <= 180_000) {
      guardUp = true;
      guardAt = fmtDMS(new Date(hbMs).toISOString());
    }
  }
  if (!guardUp && guardSt?.isFile() && Math.abs(now - guardSt.mtimeMs) <= GUARD_FRESH_MS) {
    guardUp = true;
  }

  const stObj = isObj(stateJson) ? stateJson : {};
  const lim = readLimits(isObj(hbJson) ? hbJson : null, lastAuto, stObj);
  const baseline = M.parseDec(stObj.baseline_equity_usdt);
  const curEq = M.parseDec((isObj(pfRaw) ? pfRaw.equity_usdt : null) ?? stObj.last_equity_usdt);
  // drawdown reference = agy_trader's own: cb_ref_equity_usdt (peak when Auto CB), else baseline
  const { diff, pctStr, sign } = M.diffAndPct(curEq, M.parseDec(lim.cbRefEquity) ?? baseline);

  // Extract BTC / B1D / B1H from latest plan narrative (e.g., "BTC=59967.8 B1D=16.1% B1H=22.4%")
  const nar = plan?.narrative ?? "";
  const btcM = /BTC=([0-9.,]+)/.exec(nar);
  const b1dM = /B1D=([0-9.]+%)/.exec(nar);
  const b1hM = /B1H=([0-9.]+%)/.exec(nar);

  const lastMaxHeatRaw = lastAuto && str(lastAuto.max_heat_usdt);
  const maxHeat = lastMaxHeatRaw && M.signOf(M.parseDec(lastMaxHeatRaw)) > 0 ? lastMaxHeatRaw : null;
  const lastDailyCap = lastAuto && typeof lastAuto.daily_max_opens === "number" ? lastAuto.daily_max_opens : 0;
  const hourlyMaxOpens = lastAuto && typeof lastAuto.hourly_max_opens === "number" ? lastAuto.hourly_max_opens : 0;
  const enforceRegimeGate = lastAuto && typeof lastAuto.enforce_regime_gate === "boolean" ? lastAuto.enforce_regime_gate : false;
  const enforceCooldown = lastAuto && typeof lastAuto.enforce_cooldown === "boolean" ? lastAuto.enforce_cooldown : false;

  // Live BTC price & open heat from 30s Fast-Guard heartbeat (falls back to 15m plan/auto log)
  const hbBtc = isObj(hbJson) ? str(hbJson.btc_price) : "";
  const hbHeat = isObj(hbJson) ? str(hbJson.open_heat_usdt) : "";

  return {
    guardUp,
    guardAt: guardAt || "-",
    strategy: (lastAuto && str(lastAuto.strategy)) || "V23_EMA_RSI_MOOD",
    regime: (lastAuto && str(lastAuto.regime)) || "-",
    btcPrice: hbBtc ? M.kpi(hbBtc) : (btcM ? M.kpi(btcM[1]) : "-"),
    b1d: b1dM ? b1dM[1] : "-",
    b1h: b1hM ? b1hM[1] : "-",
    lastRoundAt: (lastAuto && str(lastAuto.at_th)) || "-",
    nextRoundIso: next15mIso(now),
    cbTripped: stObj.circuit_breaker_tripped === true,
    cbBaseline: baseline ? M.kpi(stObj.baseline_equity_usdt) : "-",
    cbDdUsdt: diff ? M.fmtDec(diff, { min: 2, max: 2, plus: true }) : "-",
    cbDdPct: pctStr,
    cbDdSign: sign,
    cbReason: str(stObj.tripped_reason),
    openHeat: hbHeat || (lastAuto && str(lastAuto.open_heat_usdt)) || "0.00",
    maxHeat,
    dailyUsed: countOpenedTodayUtc(pfRaw, now),
    dailyCap: lastDailyCap > 0 ? lastDailyCap : null,
    maxOpen: lim.maxOpen,
    maxLong: lim.maxLong,
    leverage: lim.leverage,
    marginUsdt: lim.marginUsdt,
    lotUsdt: lim.lotUsdt,
    minAvailUsdt: lim.minAvailUsdt,
    affordMore: affordMore(isObj(pfRaw) ? pfRaw.available_usdt : null, lim.minAvailUsdt),
    cbAuto: lim.cbAuto,
    cbRefEquity: lim.cbRefEquity ? M.kpi(lim.cbRefEquity) : "-",
    cbLimitUsdt: lim.cbLimitUsdt,
    cbLimitPct: lim.cbLimitPct,
    hourlyMaxOpens,
    enforceRegimeGate,
    enforceCooldown,
    recentRounds,
    recentGuards,
  };
}

async function loadChat(r: string): Promise<ChatLine[]> {
  const dir = path.join(r, "reports", "team-chat");
  let files: string[] = [];
  try { files = (await readdir(dir)).filter((f) => f.endsWith(".md")).sort().reverse(); } catch { /* no folder yet */ }
  const out: ChatLine[] = [];
  if (files[0]) {
    const text = await readCapped(path.join(dir, files[0]), 2 * MB, true);
    for (const l of (text ?? "").split("\n")) {
      const m = /^- (\d\d:\d\d) \*\*(.+?)\*\*: (.*)$/.exec(l.replace(/\r$/, ""));
      if (m) out.push({ time: m[1], name: m[2], text: m[3] });
    }
  }
  return out;
}

async function loadPlan(r: string): Promise<PlanView | null> {
  const reports = path.join(r, "reports");
  for (const d of await dayDirs(reports)) {
    const f = path.join(reports, d, "dca-plan.json");
    const st = await statOrNull(f);
    if (!st?.isFile()) continue;
    const j = await readJson(f, 2 * MB);
    return isObj(j) ? buildPlan(j, `${f}|${st.mtimeMs}`, st.mtimeMs) : null;
  }
  return null;
}

async function loadPortfolio(r: string): Promise<{ view: PortfolioView | null; raw: unknown }> {
  const f = path.join(r, "cache", "portfolio.json");
  const st = await statOrNull(f);
  if (!st?.isFile()) return { view: null, raw: null };
  const sigFile = path.join(r, "cache", "entry-signals.json");
  const posMetaFile = path.join(r, "cache", "agy-positions.json");
  const reports = path.join(r, "reports");
  let scrFile: string | null = null;
  for (const d of await dayDirs(reports)) if ((await statOrNull(path.join(reports, d, "screen.json")))?.isFile()) { scrFile = path.join(reports, d, "screen.json"); break; }
  const [pf, sigJson, posMetaJson, scrJson, sigSt, pmSt, scrSt] = await Promise.all([
    readJson(f, 5 * MB), readJson(sigFile, MB), readJson(posMetaFile, MB), scrFile ? readJson(scrFile, 8 * MB) : null,
    statOrNull(sigFile), statOrNull(posMetaFile), scrFile ? statOrNull(scrFile) : null,
  ]);
  if (!isObj(pf)) return { view: null, raw: pf };
  const signals = new Map<string, string>();
  if (isObj(sigJson)) for (const [k, v] of Object.entries(sigJson)) if (typeof v === "string") signals.set(k, v);
  const key = [f, st.size, st.mtimeMs, sigSt?.mtimeMs ?? 0, pmSt?.mtimeMs ?? 0, scrSt?.mtimeMs ?? 0].join("|");
  return { view: buildPortfolio(pf, key, signals, screenByCoin(scrJson), agyMetaById(posMetaJson)), raw: pf };
}

function buildCost(log: LogTail, today: string) {
  const month = today.slice(0, 7);
  let todayE8 = 0n, monthE8 = 0n, unknownModel = false;
  const rows = new Map<string, { calls: number; tin: number; tout: number; e8: bigint; known: boolean }>();
  for (const v of log.tokens.values()) {
    const e8 = M.costE8(v.model, v.tin, v.tout);
    if (v.day === today) todayE8 += e8 ?? 0n;
    if (!v.day.startsWith(month)) continue;
    monthE8 += e8 ?? 0n;
    unknownModel ||= e8 === null;
    const x = rows.get(v.model) ?? { calls: 0, tin: 0, tout: 0, e8: 0n, known: false };
    rows.set(v.model, { calls: x.calls + v.calls, tin: x.tin + v.tin, tout: x.tout + v.tout, e8: x.e8 + (e8 ?? 0n), known: e8 !== null });
  }
  const list: CostRow[] = [...rows.entries()].sort((a, b) => (a[1].e8 < b[1].e8 ? 1 : a[1].e8 > b[1].e8 ? -1 : 0))
    .map(([model, x]) => ({ model, calls: x.calls, tin: M.count(x.tin), tout: M.count(x.tout), usd: x.known ? "$" + M.fmtE8(x.e8, 4) : "ไม่ทราบราคา" }));
  return { today: M.fmtE8(todayE8, 2), month: M.fmtE8(monthE8, 2), rows: list, unknownModel };
}

function buildDetail(thread: string, log: LogTail, chat: ChatLine[], members: MemberView[], runner: RunnerView, today: string): BotDetailView | null {
  const m = TEAM.find((t) => t.thread === thread);
  if (!m) return null;
  const timeline = log.timeline(m.thread, today);
  const me = members.find((x) => x.thread === m.thread);
  const errors = log.errorsToday.get(`${today}|${m.thread}`) ?? 0;
  const [status, tone]: [string, Tone] = !runner.up ? ["● ปิดใช้งาน (ใช้ V23 แทน)", "dim"] : me?.active ? ["● กำลังทำงาน", "ok"] : ["● ว่าง", "dim"];
  return {
    thread: m.thread, emoji: m.emoji, name: m.name, role: m.role, model: m.model, listens: m.listens, sends: m.sends, status, tone,
    stats: `error ${errors} ครั้ง`, timeline: timeline.map(({ when, icon, kind, text }) => ({ when, icon, kind, text })),
  };
}

/** Everything the monitor console shows, read from cache/ and reports/. */
export async function loadSnapshot(sel: string | null): Promise<Snapshot> {
  const r = root();
  const now = Date.now();
  const log = logTail();
  const [runner, chatOldest, plan, pfLoaded] = await Promise.all([loadRunner(r, now), loadChat(r), loadPlan(r), loadPortfolio(r), log.read()]);
  const agy = await loadAgyStatus(r, now, pfLoaded.raw, plan);
  const today = bkkDate(now);
  const cutoff = now - 30 * 60_000;
  const members: MemberView[] = TEAM.map((t) => {
    const last = chatOldest.findLast((c) => c.name === t.name);
    const newest = log.entries.get(t.thread)?.at(-1);
    return {
      thread: t.thread, emoji: t.emoji, name: t.name, role: t.role, model: t.model, error: (log.lastError.get(t.thread) ?? 0) > cutoff,
      active: runner.up && runner.teamRunning && !!newest && now - newest.ts < 90_000,
      last: last ? `${last.time} ${cut(last.text, 70)}` : "ยังไม่พูดวันนี้",
    };
  });
  const chat = chatOldest.slice(-400).reverse();
  return {
    serverNow: new Date(now).toISOString(), runner, agy, members, chat, cost: buildCost(log, today),
    problems: [...log.problems].reverse(), plan, pf: pfLoaded.view, detail: sel ? buildDetail(sel, log, chat, members, runner, today) : null,
  };
}
