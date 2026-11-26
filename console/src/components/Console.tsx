"use client";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toUsdtSymbol } from "@/lib/indicators";
import { regimePolarity } from "@/lib/motion";
import { bkkHM, parseIso } from "@/lib/time";
import type { PfClosedView, PlanView, PortfolioView, Snapshot } from "@/lib/types";
import { GuardChip, NextRoundChip, TerminalClock } from "./Countdown";
import { PlaneGlyph, StationEmblem } from "./fids";
import { ChatTab, RunsTab } from "./MiscTabs";
import { CbGauge, FlapReady, RegimeWave, SlotBar, SplitFlap } from "./motion";
import { PlanTab } from "./PlanTab";
import { PortfolioTab } from "./PortfolioTab";
import { ThemeToggle } from "./ThemeToggle";
import { TradingChartModal, type ChartCoinOption } from "./TradingChartModal";
import { Modal, Panel, pnlClass, Tabs, type TabDef } from "./ui";

type TabKey = "pf" | "plan" | "runs" | "chat";
const TABS: readonly TabDef<TabKey>[] = [
  { key: "pf", label: "พอร์ต & Position" },
  { key: "plan", label: "แผนเทรด (LONG/SHORT)" },
  { key: "runs", label: "รอบ 15 นาที / Fast-Guard" },
  { key: "chat", label: "บันทึกกิจกรรม" },
];
const POLL_MS = 5_000;
const AUTO_REFRESH_MS = 10_000;
const SETTLE_MS = 2_000;   // after each :00/:10/…/:50 slot the server fast-guard finishes writing portfolio.json
const CLOSING_POLL_MS = 1_000;
const CLOSING_MS = 60_000;

const hm = () => bkkHM(Date.now());
const closedKey = (x: PfClosedView) => `${x.coin}|${x.opened}|${x.closed}`;

function regimeLabelTh(code: string | undefined): string {
  switch (code) {
    case "BEARISH_FAVORABLE": return "ขาลง (เอื้อ SHORT)";
    case "BEARISH_STRONG": return "ขาลงแรง (เอื้อ SHORT)";
    case "BULLISH_SQUEEZE_CAUTION": return "ระวัง Squeeze (ขาขึ้น)";
    case "BULLISH_FAVORABLE": return "ขาขึ้น (เอื้อ LONG)";
    case "NEUTRAL_TREND": return "ไซด์เวย์ / กลาง";
    case "CRASH_CAUTION": return "ตลาดดิ่งแรง (ระวัง LONG)";
    default: return code && code !== "-" ? code.replace(/_/g, " ") : "-";
  }
}

/** "101.00" -> "101" for short labels (display only). */
const trimCents = (v: string) => v.replace(/\.00$/, "");

function compactInt(v: string | undefined): string {
  if (!v || v === "-") return "-";
  return v.replace(/\.\d+$/, "");
}

function compactPct(v: string | undefined): string {
  if (!v || v === "-") return "-";
  return v.replace(/\.0+%$/, "%").replace(/(\.\d)\d+%$/, "$1%");
}

interface Seen {
  planKey: string | null;
  pfKey: string | null;
  planSeen: string | null;
  pfOpen: Set<string> | null;
  pfClosed: Set<string> | null;
}

function KpiCard({
  label,
  value,
  unit,
  sub,
  cls,
  title,
  viz,
  action,
}: {
  label: string;
  value: string;
  unit?: string;
  /** an array wraps between tokens instead of clipping with "…" */
  sub?: string | string[];
  cls?: string;
  title?: string;
  viz?: ReactNode;
  action?: ReactNode;
}) {
  const vizCls = viz ? " has-viz" : "";
  return (
    <div className={`kpi${vizCls}`} title={title}>
      {action ? (
        <div className="kpi-head">
          <span className="kpi-label" title={label}>{label}</span>
          {action}
        </div>
      ) : (
        <span className="kpi-label" title={label}>{label}</span>
      )}
      <span className={`kpi-value ${cls ?? ""}`} title={title ?? value}>
        <SplitFlap value={value} />
        {unit && <small> {unit}</small>}
      </span>
      {Array.isArray(sub) ? (
        <span className="kpi-sub multi" title={sub.join(" · ")}>
          {sub.map((t, i) => (
            // the space between tokens lives outside the nowrap spans so the line can wrap there, never with "…"
            <Fragment key={i}>{i > 0 && " "}<span>{i > 0 && "· "}<SplitFlap value={t} /></span></Fragment>
          ))}
        </span>
      ) : sub && <span className="kpi-sub" title={sub}><SplitFlap value={sub} /></span>}
      {viz}
    </div>
  );
}

const pnlMood = (s: number | undefined) => (s === undefined || s === 0 ? "flat" : s > 0 ? "pos" : "neg");

export function Console() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [offsetMs, setOffsetMs] = useState(0);
  const [loadError, setLoadError] = useState(false);
  const [tab, setTab] = useState<TabKey>("pf");
  const [alerts, setAlerts] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const [closing, setClosing] = useState<Record<string, number>>({});
  const busy = Object.keys(closing).length > 0;
  const [png, setPng] = useState<{ path: string; title: string } | null>(null);
  const [pngFailed, setPngFailed] = useState(false);
  const [chartSymbol, setChartSymbol] = useState<string | null>(null);
  // split-flap stays calm for the first snapshot (page load) and flips only on later live updates
  const [flapReady, setFlapReady] = useState(false);
  const hasSnap = snap !== null;
  useEffect(() => { if (hasSnap) setFlapReady(true); }, [hasSnap]);

  const inflight = useRef<AbortController | null>(null);
  const offsetRef = useRef(0);
  offsetRef.current = offsetMs;
  const seen = useRef<Seen>({ planKey: null, pfKey: null, planSeen: null, pfOpen: null, pfClosed: null });

  const notify = useCallback((text: string) => setAlerts((a) => [`🔔 ${hm()}  ${text}`, ...a].slice(0, 5)), []);

  const alertPlan = useCallback((p: PlanView) => {
    const s = seen.current;
    const coins = p.shortCoins.join(", ");
    const key = `${p.runId}|${coins}`;
    if (s.planSeen !== null && coins.length > 0 && key !== s.planSeen) notify(`แผนเทรดรอบใหม่: ${coins}`);
    s.planSeen = key;
  }, [notify]);

  const alertPortfolio = useCallback((p: PortfolioView) => {
    if (!p.hasGood) return;
    const s = seen.current;
    if (s.pfOpen && s.pfClosed) {
      const opened = p.shorts.filter((x) => !s.pfOpen?.has(x.coin));
      if (opened.length > 0) {
        notify("เปิด Position แล้ว: " + opened.map((x) => (x.stateOk ? `${x.side} ${x.coin}` : `${x.side} ${x.coin} (${x.state})`)).join(", "));
      }
      const done = p.closed.filter((x) => !s.pfClosed?.has(closedKey(x)));
      if (done.length > 0) {
        notify("ปิด Position แล้ว: " + done.map((x) => `${x.side} ${x.coin} (${x.why}${x.alertPnl ? ` ${x.alertPnl} USDT` : ""})`).join(", "));
      }
    }
    s.pfOpen = new Set(p.shorts.map((x) => x.coin));
    s.pfClosed = new Set(p.closed.map(closedKey));
  }, [notify]);

  const apply = useCallback((s: Snapshot) => {
    const seenNow = seen.current;
    const serverNow = parseIso(s.serverNow) ?? Date.now();
    setOffsetMs(serverNow - Date.now());
    if (seenNow.planKey === null && !s.plan) seenNow.planSeen = "";
    if (s.plan && s.plan.key !== seenNow.planKey) alertPlan(s.plan);
    seenNow.planKey = s.plan?.key ?? seenNow.planKey ?? "";
    if (s.pf && s.pf.key !== seenNow.pfKey) alertPortfolio(s.pf);
    seenNow.pfKey = s.pf?.key ?? seenNow.pfKey;
    setSnap(s);
  }, [alertPlan, alertPortfolio]);

  const load = useCallback(async (force: boolean) => {
    if (inflight.current && !force) return;
    inflight.current?.abort();
    const ac = new AbortController();
    inflight.current = ac;
    try {
      const res = await fetch("/api/snapshot", { cache: "no-store", signal: ac.signal });
      if (res.status === 401) { window.location.assign("/login"); return; }
      if (!res.ok) throw new Error("bad status");
      apply((await res.json()) as Snapshot);
      setLoadError(false);
    } catch {
      if (!ac.signal.aborted) setLoadError(true);
    } finally {
      if (inflight.current === ac) inflight.current = null;
    }
  }, [apply]);

  useEffect(() => {
    void load(false);
    const id = setInterval(() => void load(false), busy ? CLOSING_POLL_MS : POLL_MS);
    return () => { clearInterval(id); inflight.current?.abort(); };
  }, [load, busy]);

  useEffect(() => {
    const ids = Object.keys(closing);
    const pf = snap?.pf;
    if (!ids.length || !pf) return;
    const now = Date.now();
    const late = ids.filter((id) => now - closing[id] >= CLOSING_MS && pf.shorts.some((x) => x.id === id));
    const keep = ids.filter((id) => now - closing[id] < CLOSING_MS && pf.shorts.some((x) => x.id === id));
    if (keep.length === ids.length) return;
    setClosing(Object.fromEntries(keep.map((id) => [id, closing[id]])));
    if (late.length) setStatus(`${hm()} ยังไม่เห็นผลปิด ${late.length} ไม้ — ตรวจแท็บ พอร์ต หรือกด ปิดไม้ ใหม่อีกครั้ง`);
  }, [snap, closing]);

  const sendRequest = useCallback(async (kind: "portfolio" | "close", quiet: boolean, id?: string): Promise<boolean> => {
    try {
      const res = await fetch("/api/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, id }),
      });
      if (res.status === 401) { window.location.assign("/login"); return false; }
      const j = (await res.json().catch(() => ({}))) as { written?: boolean; reason?: string; waitS?: number; error?: string };
      if (res.status === 503 && j.error === "runner_down") {
        setStatus(`${hm()} Fast-Guard บนเซิร์ฟเวอร์ยังไม่ตอบ — ส่งคำสั่งไม่ได้`);
        return false;
      }
      if (res.status === 409 && j.error === "not_closable") {
        setStatus(`${hm()} ไม่ส่งคำสั่งปิด: ไม้นี้ไม่อยู่ในสถานะเปิดบน Binance แล้ว — รอพอร์ตอัปเดต`);
        return false;
      }
      if (!res.ok) {
        if (!quiet) setStatus(`${hm()} ส่งคำสั่งไม่สำเร็จ (${res.status})`);
        return false;
      }
      if (j.written) return true;
      if (!quiet) {
        setStatus(j.reason === "pending" ? `${hm()} มีคำสั่งรอ Fast-Guard อยู่แล้ว` : `${hm()} สั่งถี่เกินไป — รออีก ${j.waitS ?? 0} วินาที`);
      }
    } catch {
      if (!quiet) setStatus(`${hm()} ส่งคำสั่งไม่สำเร็จ — เชื่อมต่อเซิร์ฟเวอร์ไม่ได้`);
    }
    return false;
  }, []);

  const refreshPortfolio = useCallback(async () => {
    const ok = await sendRequest("portfolio", false);
    if (ok) {
      setStatus(`${hm()} ส่งคำสั่งดึงพอร์ตล่าสุดจาก Binance แล้ว — ตารางจะอัปเดตภายใน 5 วินาที`);
      setTimeout(() => void load(true), 2_500);
      setTimeout(() => void load(true), 5_500);
    }
    void load(true);
  }, [sendRequest, load]);

  // Sync UI reload to the shared server wall-clock 10s slot (:00/:10/:20/:30/:40/:50 + 2s settle after server fast-guard fetches Binance).
  // Multiple open screens read the same server refresh without colliding or spamming Proxy requests.
  useEffect(() => {
    let tid: ReturnType<typeof setTimeout> | null = null;
    const scheduleNext = () => {
      const serverNow = Date.now() + offsetRef.current;
      const remMs = AUTO_REFRESH_MS - (((serverNow % AUTO_REFRESH_MS) + AUTO_REFRESH_MS) % AUTO_REFRESH_MS);
      tid = setTimeout(() => {
        if (document.visibilityState === "visible") void load(true);
        scheduleNext();
      }, remMs + SETTLE_MS);
    };
    scheduleNext();
    const onVisible = () => {
      if (document.visibilityState === "visible") void load(true);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      if (tid) clearTimeout(tid);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const closePositions = useCallback(async (ids: string[]) => {
    const at = Date.now();
    setClosing((c) => ({ ...c, ...Object.fromEntries(ids.map((id) => [id, at])) }));
    const failed: string[] = [];
    for (const id of ids) {
      if (!(await sendRequest("close", ids.length > 1, id))) failed.push(id);
    }
    const sent = ids.length - failed.length;
    if (failed.length) {
      setClosing((c) => Object.fromEntries(Object.entries(c).filter(([id]) => !failed.includes(id))));
    }
    if (sent === ids.length) {
      setStatus(`${hm()} ส่งคำสั่งปิด ${sent} ไม้ให้ Fast-Guard 5 วิ แล้ว — ปิดภายในไม่กี่วินาที`);
    } else if (ids.length > 1) {
      setStatus(`${hm()} ส่งคำสั่งปิดได้ ${sent} จาก ${ids.length} ไม้ — ตรวจไม้ที่เหลือแล้วกด ปิดไม้ รายตัว`);
    }
  }, [sendRequest]);

  const reloadNow = useCallback(() => {
    void load(true);
    setStatus(`${hm()} รีเฟรชข้อมูลหน้าจอแล้ว`);
  }, [load]);

  const openPng = useCallback((path: string, title: string) => {
    setPngFailed(false);
    setPng({ path, title });
  }, []);

  const openChart = useCallback((coinOrSym: string) => {
    const sym = toUsdtSymbol(coinOrSym);
    if (sym) setChartSymbol(sym);
  }, []);

  async function logout() {
    await fetch("/api/logout", { method: "POST" }).catch(() => undefined);
    window.location.assign("/login");
  }

  const agy = snap?.agy ?? null;
  const pf = snap?.pf ?? null;
  const open = pf?.shorts ?? [];
  const planRows = snap?.plan?.rows ?? [];

  const chartCoins: ChartCoinOption[] = useMemo(() => {
    const seenSym = new Set<string>();
    const list: ChartCoinOption[] = [];
    for (const p of open) {
      const sym = toUsdtSymbol(p.coin);
      if (sym && !seenSym.has(sym)) {
        seenSym.add(sym);
        list.push({ symbol: sym, coin: p.coin, group: "open", sideCode: p.sideCode, pnlSign: p.pnlSign });
      }
    }
    if (!seenSym.has("BTCUSDT")) {
      seenSym.add("BTCUSDT");
      list.push({ symbol: "BTCUSDT", coin: "BTC", group: "btc" });
    }
    for (const r of planRows) {
      const sym = toUsdtSymbol(r.coin);
      if (sym && !seenSym.has(sym)) {
        seenSym.add(sym);
        list.push({ symbol: sym, coin: r.coin, group: "plan", sideCode: r.sideCode });
      }
    }
    return list;
  }, [open, planRows]);

  const activeChartPos = chartSymbol ? (open.find((x) => toUsdtSymbol(x.coin) === chartSymbol) ?? null) : null;
  const activeChartClosed = chartSymbol && !activeChartPos
    ? (pf?.closed.find((x) => toUsdtSymbol(x.coin) === chartSymbol) ?? null)
    : null;
  const longN = open.filter((x) => x.sideCode === "LONG").length;
  const shortN = open.length - longN;
  const capOpen = agy ? agy.maxOpen : 8;   // null = unlimited (opens bounded by free margin)
  const capLong = agy ? agy.maxLong : 8;
  const afford = agy?.affordMore ?? null;
  const minAvail = trimCents(agy?.minAvailUsdt ?? "101");
  const cbRule = agy ? `-${agy.cbLimitUsdt} USDT / -${agy.cbLimitPct}%${agy.cbAuto ? " (อัตโนมัติจาก Peak Equity)" : ""}` : "-45 USDT / -5%";
  const posSub = capOpen === null
    ? [`▲ LONG ${longN}`, `▼ SHORT ${shortN}`, afford === null ? "เปิดเพิ่มได้ตามเงินว่าง" : afford > 0 ? `เปิดเพิ่มได้ +${afford} ไม้` : `รอเงินว่าง ≥${minAvail}U`]
    : capLong === null || capLong >= capOpen
      ? [`▲ LONG ${longN}`, `▼ SHORT ${shortN}`, "ไม่จำกัด"]
      : [`▲ LONG ${longN}/${capLong}`, `▼ SHORT ${shortN}`];

  return (
    <FlapReady.Provider value={flapReady}>
    <div className="app">
      <header className="topbar" data-pnl={pnlMood(pf?.unrealizedSign)}>
        <div className="brand">
          <PlaneGlyph live={agy?.guardUp ?? false} />
          <div className="brand-text">
            <div className="brand-head">
              <h1>BOTTRADE TERMINAL</h1>
            </div>
            <p className="sub">กระดานพอร์ตและ Position สด · <span className="sub-strat">{agy?.strategy ?? "V23_EMA_RSI_MOOD"}</span> · Binance Futures</p>
          </div>
        </div>

        <StationEmblem />

        <div className="bar-info">
          {agy && <GuardChip guardUp={agy.guardUp} guardAt={agy.guardAt} />}
          {agy && <NextRoundChip nextRoundIso={agy.nextRoundIso} offsetMs={offsetMs} lastRoundAt={agy.lastRoundAt} />}
          {agy && (
            <span className={`chip ${agy.cbTripped ? "err" : "ok"}`} title={agy.cbReason || `Circuit Breaker ${cbRule}`}>
              {agy.cbTripped ? "⛔ CB หยุดเปิดไม้" : "● CB ปกติ"}
            </span>
          )}
        </div>

        <div className="bar-actions">
          <ThemeToggle />
          <button className="btn btn-sm" type="button" onClick={reloadNow} title="โหลดข้อมูลล่าสุดเข้าหน้าจอเดี๋ยวนี้">
            ↻ โหลด
          </button>
          <button className="btn btn-sm" type="button" onClick={() => void logout()} title="ออกจากระบบ">
            ออก
          </button>
        </div>
      </header>

      {alerts.length > 0 && (
        <div className="alert" role="status">
          <ul>{alerts.map((a, i) => <li key={`${i}|${a}`}>{a}</li>)}</ul>
          <button className="btn btn-x" type="button" aria-label="ปิดแจ้งเตือน" title="ปิดแจ้งเตือน" onClick={() => setAlerts([])}>✕</button>
        </div>
      )}

      {agy?.cbTripped && (
        <p className="warn" role="alert">
          ⛔ Circuit Breaker ทำงาน: {agy.cbReason || `ขาดทุนถึงเกณฑ์ ${cbRule}`} — ระบบหยุดเปิดไม้ใหม่ (ยังปิดไม้ที่มีอยู่ได้)
        </p>
      )}
      {agy && !agy.guardUp && (
        <p className="warn" role="alert">
          Fast-Guard 5 วิ ยังไม่อัปเดตสัญญาณล่าสุด ({agy.guardAt}) — ปุ่มปิดไม้และรีเฟรชพอร์ตอาจรอสัญญาณจากเซิร์ฟเวอร์
        </p>
      )}
      {loadError && (
        <p className="warn" role="alert">อ่านข้อมูลจากเซิร์ฟเวอร์ไม่ได้ — ลองใหม่อัตโนมัติทุก 5 วินาที</p>
      )}

      <section className="kpis" aria-label="สรุปพอร์ตและสถานะระบบ">
        <KpiCard
          label="มูลค่ารวม (Equity)"
          value={pf?.equity ?? "-"}
          unit="USDT"
          sub={`ว่างใช้ได้ ${pf?.available ?? "-"} USDT`}
          cls="gold"
        />
        <KpiCard
          label="PnL ยังไม่ปิด"
          value={pf?.unrealized ?? "-"}
          unit="USDT"
          sub={`ปิดแล้วสุทธิ ${pf?.realized ?? "-"} USDT`}
          cls={pnlClass(pf?.unrealizedSign ?? 0)}
        />
        <KpiCard
          label="Position เปิดอยู่"
          value={capOpen === null ? `${open.length}` : `${open.length} / ${capOpen}`}
          unit="ไม้"
          sub={posSub}
          title={capOpen === null
            ? `ไม่จำกัดจำนวนไม้ · เปิดใหม่ได้เมื่อเงินว่าง ≥ ${minAvail} USDT ต่อไม้ (Margin ${trimCents(agy?.marginUsdt ?? "100")} × ${agy?.leverage ?? 2}x) · ไม่จำกัดฝั่ง LONG/SHORT`
            : `ถือได้สูงสุด ${capOpen} ไม้`}
          viz={<SlotBar longN={longN} shortN={shortN} maxOpen={capOpen} afford={afford} />}
        />
        <KpiCard
          label="เปิดวันนี้ (UTC)"
          value={agy?.dailyCap ? `${agy.dailyUsed} / ${agy.dailyCap}` : `${agy?.dailyUsed ?? 0}`}
          unit="ไม้"
          sub={`${agy?.dailyCap ? "" : "ไม่จำกัด/วัน · "}Heat ${agy?.openHeat ?? "0.00"}${agy?.maxHeat ? ` / ${agy.maxHeat}` : ""} U`}
        />
        <KpiCard
          label="Circuit Breaker (DD)"
          value={agy?.cbDdUsdt ?? "-"}
          unit={agy && agy.cbDdPct !== "-" ? `U (${agy.cbDdPct})` : "USDT"}
          sub={agy?.cbAuto
            ? [`ฐานพีค ${agy.cbRefEquity} U`, `อัตโนมัติ -${agy.cbLimitUsdt} U / -${agy.cbLimitPct}%`]
            : `ฐาน ${agy?.cbBaseline ?? "-"} U · ตัด -${agy?.cbLimitUsdt ?? "45"} U / -${agy?.cbLimitPct ?? "5"}%`}
          cls={pnlClass(agy?.cbDdSign ?? 0)}
          viz={agy ? <CbGauge ddUsdt={agy.cbDdUsdt} ddPct={agy.cbDdPct} tripped={agy.cbTripped} limitUsdt={agy.cbLimitUsdt} limitPct={agy.cbLimitPct} /> : undefined}
        />
        <KpiCard
          label="สภาพตลาด (Regime)"
          value={regimeLabelTh(agy?.regime)}
          cls="kpi-text"
          title={agy?.regime ?? "-"}
          sub={`BTC ${compactInt(agy?.btcPrice)} · 1D ${compactPct(agy?.b1d)} · 1H ${compactPct(agy?.b1h)}`}
          viz={<RegimeWave polarity={regimePolarity(agy?.regime)} />}
          action={(
            <button
              type="button"
              className="kpi-act"
              onClick={() => openChart("BTCUSDT")}
              title="เปิดกราฟ TradingView สด BTCUSDT"
            >
              📈 BTC
            </button>
          )}
        />
      </section>

      <main className="main-single">
        <section className="content" aria-label="รายละเอียด Monitor">
          <Tabs tabs={TABS} value={tab} onChange={setTab} idBase="main" label="หน้าจอ Monitor" />
          <Panel key={tab} idBase="main" k={tab}>
            {tab === "pf" && (
              <PortfolioTab
                pf={pf}
                onRefresh={() => void refreshPortfolio()}
                onReloadSnap={() => void load(true)}
                onClose={(ids) => void closePositions(ids)}
                onChart={openChart}
                closing={closing}
                offsetMs={offsetMs}
                agy={agy}
              />
            )}
            {tab === "plan" && (
              <PlanTab
                plan={snap?.plan ?? null}
                onPng={openPng}
                onChart={openChart}
                onReloadSnap={() => void load(true)}
              />
            )}
            {tab === "runs" && (
              <RunsTab
                agy={agy}
                onReloadSnap={() => void load(true)}
              />
            )}
            {tab === "chat" && <ChatTab chat={snap?.chat ?? []} />}
          </Panel>
        </section>
      </main>

      <p className="disclaimer">
        หน้าจอนี้สำหรับ Monitor และสั่งปิด Position เท่านั้น · ตัวเทรด V23 ทำงานจากเครื่อง PC ทุก 15 นาที (:05/:20/:35/:50) · Fast-Guard ตรวจ SL/TP ทุก 5 วิ · ไม่ใช่คำแนะนำการลงทุน
      </p>

      <footer className="fids-footer">
        <p className="status-line" role="status" aria-live="polite">{status}</p>
        <TerminalClock offsetMs={offsetMs} />
      </footer>

      <Modal open={png !== null} onClose={() => setPng(null)} title={png?.title ?? "กราฟ"}>
        {png && (pngFailed ? <p className="warn">ไม่พบไฟล์กราฟ</p> : (
          <img src={`/api/png?p=${encodeURIComponent(png.path)}`} alt={png.title} onError={() => setPngFailed(true)} />
        ))}
      </Modal>

      <TradingChartModal
        symbol={chartSymbol}
        coins={chartCoins}
        position={activeChartPos}
        closedPosition={activeChartClosed}
        onSelectSymbol={setChartSymbol}
        onClose={() => setChartSymbol(null)}
      />
    </div>
    </FlapReady.Provider>
  );
}
