"use client";
import { useMemo, useState } from "react";
import type { AgyStatusView, PfClosedView, PfLogView, PfOrderView, PfShortView, PortfolioView } from "@/lib/types";
import { RefreshLabel } from "./Countdown";
import { SplitFlap } from "./motion";
import { DataTable, Modal, Panel, pnlClass, SideBadge, Tabs, type Col, type TabDef } from "./ui";

type Sub = "open" | "closed" | "orders" | "log";

const ORDER_COLS: Col<PfOrderView>[] = [
  { key: "when", head: "เวลา (ไทย)", width: 13, minPx: 88, sortValue: (r) => r.when, cell: (r) => r.when },
  { key: "kind", head: "ประเภท", width: 11, minPx: 72, sortValue: (r) => r.kind, cell: (r) => r.kind },
  { key: "symbol", head: "คู่เหรียญ", width: 12, minPx: 80, strong: true, sortValue: (r) => r.symbol, cell: (r) => r.symbol },
  { key: "qty", head: "จำนวน", width: 11, minPx: 68, align: "right", sortValue: (r) => r.qty, cell: (r) => r.qty },
  { key: "usdt", head: "USDT", width: 10, minPx: 64, align: "right", sortValue: (r) => r.usdt, cell: (r) => r.usdt },
  { key: "price", head: "ราคาเฉลี่ย", width: 13, minPx: 76, align: "right", sortValue: (r) => r.price, cell: (r) => r.price },
  { key: "status", head: "สถานะ", width: 30, minPx: 130, wrap: true, sortValue: (r) => r.status, cell: (r) => r.status },
];

const LOG_COLS: Col<PfLogView>[] = [
  { key: "when", head: "เวลา (ไทย)", width: 14, minPx: 92, sortValue: (r) => r.when, cell: (r) => r.when },
  { key: "run", head: "รอบ", width: 12, minPx: 80, sortValue: (r) => r.run, cell: (r) => r.run },
  { key: "text", head: "ข้อความจากเซิร์ฟเวอร์ (ใหม่สุดอยู่บน)", width: 74, minPx: 200, wrap: true, sortValue: (r) => r.text, cell: (r) => r.text },
];

/** "100.00" -> "100" for short labels (display only). */
const trimCents = (v: string) => v.replace(/\.00$/, "");

/** V23 rules line, built from the live limits agy_trader reports (leverage, margin, unlimited/capped opens, Auto CB). */
function rulesNote(agy: AgyStatusView | null): string {
  const lev = agy?.leverage ?? 2;
  const margin = trimCents(agy?.marginUsdt ?? "100");
  const lot = trimCents(agy?.lotUsdt ?? "200");
  const minAvail = trimCents(agy?.minAvailUsdt ?? "101");
  const opens = agy && agy.maxOpen !== null
    ? `ถือสูงสุด ${agy.maxOpen} ไม้พร้อมกัน`
    : `ไม่จำกัดจำนวนไม้ (ดูตามเงินว่าง ≥ ${minAvail} USDT)`;
  const cb = agy?.cbAuto
    ? `Auto Circuit Breaker -${agy.cbLimitUsdt} U / -${agy.cbLimitPct}% จาก Peak Equity`
    : `Circuit Breaker -${agy?.cbLimitUsdt ?? "45"} USDT / -${agy?.cbLimitPct ?? "5"}%`;
  return `กติกา V23: ไม้ละ ${lot} USDT (Margin ${margin} · ${lev}x isolated) · ${opens} · ไม่จำกัดฝั่ง LONG/SHORT · ไม่จำกัดไม้/วัน-ชม. · ไม่จำกัด Heat/Regime/Cooldown · ห้ามซ้ำเหรียญวัน UTC เดียวกัน · V23 ATR SL 3.5–6.5% / TP 30% (1H/2H SL 5.0–7.5% / TP 40–60%) + Progressive Trail + Fast-Guard 5 วิ · ${cb}`;
}

export function PortfolioTab({
  pf,
  onRefresh,
  onReloadSnap,
  onClose,
  onChart,
  closing,
  offsetMs,
  agy,
}: {
  pf: PortfolioView | null;
  onRefresh: () => void;
  onReloadSnap: () => void;
  onClose: (ids: string[]) => void;
  onChart: (coin: string) => void;
  closing: Readonly<Record<string, number>>;
  offsetMs: number;
  agy: AgyStatusView | null;
}) {
  const [sub, setSub] = useState<Sub>("open");
  const [ask, setAsk] = useState<PfShortView[] | null>(null);
  const [spinKey, setSpinKey] = useState(0);
  const open = pf?.shorts ?? [];

  const closedCols: Col<PfClosedView>[] = useMemo(() => [
    { key: "side", head: "ฝั่ง", width: 7, minPx: 56, align: "center", sortValue: (r) => r.sideCode, cell: (r) => <SideBadge code={r.sideCode} text={r.side} /> },
    {
      key: "coin", head: "เหรียญ", width: 8, minPx: 62, strong: true, sortValue: (r) => r.coin,
      cell: (r) => (
        <button type="button" className="coin-btn" onClick={() => onChart(r.coin)} title={`เปิดกราฟ TradingView สด ${r.coin}/USDT`}>
          {r.coin}
        </button>
      ),
    },
    { key: "entry", head: "ราคาเข้า", width: 10, minPx: 72, align: "right", sortValue: (r) => r.entry, cell: (r) => r.entry },
    { key: "time", head: "เปิด → ปิด (ไทย)", width: 16, minPx: 110, sortValue: (r) => r.closed, cell: (r) => `${r.opened} → ${r.closed}`, title: (r) => `เปิด ${r.opened} · ปิด ${r.closed} · จำนวน ${r.qty}` },
    { key: "why", head: "เหตุผลที่ปิด", width: 22, minPx: 120, wrap: true, sortValue: (r) => r.why, cell: (r) => r.why },
    { key: "pnl", head: "PnL สุทธิ", width: 12, minPx: 84, align: "right", sortValue: (r) => r.pnl, cell: (r) => <span className={pnlClass(r.pnlSign)}><SplitFlap value={r.pnl} /></span> },
    { key: "funding", head: "Funding", width: 9, minPx: 64, align: "right", sortValue: (r) => r.funding, cell: (r) => r.funding },
    { key: "fees", head: "ค่าธรรมเนียม", width: 9, minPx: 68, align: "right", sortValue: (r) => r.fees, cell: (r) => r.fees },
    {
      key: "chart", head: "กราฟ", width: 7, minPx: 66, align: "center",
      cell: (r) => (
        <button type="button" className="btn btn-xs btn-chart" onClick={() => onChart(r.coin)} title={`เปิดกราฟ TradingView สด ${r.coin}/USDT`}>
          📈 กราฟ
        </button>
      ),
    },
  ], [onChart]);

  const openCols: Col<PfShortView>[] = useMemo(() => [
    {
      key: "side", head: "ฝั่ง", width: 7, minPx: 56, align: "center",
      sortValue: (r) => r.sideCode,
      cell: (r) => <SideBadge code={r.sideCode} text={r.side} />,
    },
    {
      key: "coin", head: "เหรียญ", width: 8, minPx: 60, strong: true,
      sortValue: (r) => r.coin,
      cell: (r) => (
        <button type="button" className="coin-btn" onClick={() => onChart(r.coin)} title={`เปิดกราฟ TradingView สด ${r.coin}/USDT`}>
          {r.coin}
        </button>
      ),
      title: (r) => `${r.side} ${r.coin} · จำนวน ${r.qty} · Margin ${r.margin} U · Liq ${r.liq} (คลิกเปิดกราฟ)`,
    },
    {
      key: "engine", head: "เอนจิน", width: 11, minPx: 84,
      sortValue: (r) => (r.setup !== "-" ? r.setup : r.signal),
      cell: (r) => (r.setup !== "-" ? r.setup : r.signal),
      title: (r) => `เอนจิน: ${r.setup !== "-" ? r.setup : r.signal} · โหมดออก: ${r.exitMode}`,
    },
    {
      key: "price", head: "เข้า → ตลาด", width: 15, minPx: 100, align: "right",
      sortValue: (r) => r.price,
      cell: (r) => r.price,
      title: (r) => `ราคาเข้า → ตลาด: ${r.price} · Liq: ${r.liq}`,
    },
    {
      key: "sltp", head: "SL / TP", width: 13, minPx: 92, align: "right",
      sortValue: (r) => r.stopPx,
      cell: (r) => (r.stopPx === "-" && r.tpPx === "-" ? "-" : `${r.stopPx} / ${r.tpPx}`),
      title: (r) => `SL: ${r.stopPx} · TP: ${r.tpPx}`,
    },
    {
      key: "stage", head: "Stage · MFE", width: 9, minPx: 72, align: "center",
      sortValue: (r) => r.mfePct,
      cell: (r) => (r.stage === "-" && r.mfePct === "-" ? "-" : `${r.stage}${r.mfePct !== "-" ? ` ${r.mfePct}` : ""}`),
      title: (r) => `Stage: ${r.stage} · MFE: ${r.mfePct} · MAE: ${r.maePct}`,
    },
    {
      key: "pnl", head: "PnL USDT", width: 10, minPx: 74, align: "right",
      sortValue: (r) => r.pnl,
      cell: (r) => <span className={pnlClass(r.pnlSign)}><SplitFlap value={r.pnl} /></span>,
      title: (r) => `PnL ยังไม่ปิด: ${r.pnl} USDT (ยังไม่รวมค่าธรรมเนียม/funding)`,
    },
    {
      key: "funding", head: "Funding", width: 7, minPx: 56, align: "right",
      sortValue: (r) => r.funding,
      cell: (r) => r.funding,
    },
    {
      key: "held", head: "เปิดเมื่อ · ถือ", width: 9, minPx: 72,
      sortValue: (r) => r.held,
      cell: (r) => r.held,
      title: (r) => `${r.held} · สถานะ: ${r.state}`,
    },
    {
      key: "action", head: "กราฟ / ปิด", width: 11, minPx: 98, align: "center",
      cell: (r) => (
        <span className="row-acts">
          <button
            className="btn btn-xs btn-chart"
            type="button"
            onClick={() => onChart(r.coin)}
            title={`เปิดกราฟ TradingView สด ${r.coin}/USDT`}
          >
            📈 กราฟ
          </button>
          {!r.closable ? (
            <span className="dim" title={r.state}>-</span>
          ) : closing[r.id] !== undefined ? (
            <button className="btn btn-xs" type="button" disabled aria-busy="true">
              <span className="spin" aria-hidden="true" />ปิด…
            </button>
          ) : (
            <button
              className="btn btn-xs"
              type="button"
              onClick={() => setAsk([r])}
              title={`ปิด ${r.side} ${r.coin} ทั้งไม้ที่ราคาตลาด`}
            >
              ปิดไม้
            </button>
          )}
        </span>
      ),
    },
  ], [closing, onChart]);

  const confirm = () => {
    if (ask) onClose(ask.map((x) => x.id));
    setAsk(null);
  };
  const closableRows = open.filter((x) => x.closable && closing[x.id] === undefined);
  const closingN = open.filter((x) => closing[x.id] !== undefined).length;
  const longN = open.filter((x) => x.sideCode === "LONG").length;
  const tabs: TabDef<Sub>[] = [
    { key: "open", label: open.length > 0 ? `Position เปิดอยู่ (${open.length} · ▲ ${longN} / ▼ ${open.length - longN})` : "Position เปิดอยู่ (0)" },
    { key: "closed", label: `ปิดแล้ว (${pf?.closed.length ?? 0})` },
    { key: "orders", label: `คำสั่งล่าสุด (${pf?.orders.length ?? 0})` },
    { key: "log", label: `Log เซิร์ฟเวอร์ (${pf?.log.length ?? 0})` },
  ];
  const header = !pf
    ? "ยังไม่มีข้อมูลพอร์ต (กด รีเฟรชพอร์ต หรือรอสักครู่)"
    : [pf.mode && `โหมด: ${pf.mode}`, pf.hasLot && `ไม้ละ ${pf.lot} USDT (Margin ${trimCents(agy?.marginUsdt ?? "100")} · ${agy?.leverage ?? 2}x isolated)`, pf.at && `อัปเดต ${pf.at} (เวลาไทย)`, "ดึงข้อมูลใหม่ทุก 10 วิ"].filter(Boolean).join(" · ");
  const problems = pf ? [pf.error, pf.spot].filter(Boolean).join("\n") : "";

  return (
    <>
      <div className="row">
        <div>
          <h2 className="section">พอร์ตและ Position (Binance USDⓈ-M Futures)</h2>
          <p className="dim">{header || "ยังไม่มีข้อมูลพอร์ต"}</p>
        </div>
        {closingN > 0 ? (
          <button className="btn row-end" type="button" disabled aria-busy="true">
            <span className="spin" aria-hidden="true" />กำลังปิด {closingN} ไม้…
          </button>
        ) : closableRows.length > 0 && (
          <button
            className="btn btn-gold row-end"
            type="button"
            onClick={() => setAsk(closableRows)}
            title="ปิดทุกไม้ที่เปิดอยู่ ทั้งไม้ที่ราคาตลาด (ถามยืนยันก่อน)"
          >
            ปิดทุกไม้ ({closableRows.length})
          </button>
        )}
        <button
          className={closingN > 0 || closableRows.length > 0 ? "btn" : "btn row-end"}
          type="button"
          onClick={() => { setSpinKey((n) => n + 1); onRefresh(); }}
          title="สั่งดึงพอร์ตล่าสุดจาก Binance ทันที (เซิร์ฟเวอร์ดึงอัตโนมัติทุก 10 วิ ตามเวลาเครื่องตรงกันทุกจอ)"
        >
          <RefreshLabel offsetMs={offsetMs} spinKey={spinKey} />
        </button>
      </div>

      {problems && <p className="warn" role="alert">{problems}</p>}

      <Tabs tabs={tabs} value={sub} onChange={setSub} idBase="pf" label="รายการพอร์ต" seg />
      <Panel idBase="pf" k={sub}>
        {sub === "open" && (
          <DataTable
            storageKey="v22_pf_open"
            caption="Position ที่เปิดอยู่"
            cols={openCols}
            rows={open}
            empty="ตอนนี้ยังไม่มี Position (LONG/SHORT) ที่เปิดอยู่"
            rowKey={(r, i) => `${r.id || r.coin}|${i}`}
            onHeaderRefresh={onReloadSnap}
          />
        )}
        {sub === "closed" && (
          <DataTable
            storageKey="v22_pf_closed"
            caption="Position ที่ปิดแล้ว"
            cols={closedCols}
            rows={pf?.closed ?? []}
            empty="ยังไม่มี Position ที่ปิดแล้ว"
            rowKey={(r, i) => `${r.coin}|${r.opened}|${r.closed}|${i}`}
            onHeaderRefresh={onReloadSnap}
          />
        )}
        {sub === "orders" && (
          <DataTable
            storageKey="v22_pf_orders"
            caption="คำสั่งล่าสุด"
            cols={ORDER_COLS}
            rows={pf?.orders ?? []}
            empty="ยังไม่มีคำสั่งล่าสุด"
            rowKey={(r, i) => `${r.when}|${r.symbol}|${i}`}
            onHeaderRefresh={onReloadSnap}
          />
        )}
        {sub === "log" && (
          <DataTable
            storageKey="v22_pf_log"
            caption="Log เซิร์ฟเวอร์"
            cols={LOG_COLS}
            rows={pf?.log ?? []}
            empty="ยังไม่มีบันทึกจากเซิร์ฟเวอร์"
            rowKey={(r, i) => `${r.when}|${i}`}
            onHeaderRefresh={onReloadSnap}
          />
        )}
      </Panel>

      <p className="note">
        {rulesNote(agy)}
      </p>
      <p className="disclaimer">
        ปุ่ม ปิดไม้ / ปิดทุกไม้ ส่งคำสั่งปิดทั้งไม้ที่ราคาตลาด (market · reduce-only) ผ่าน Fast-Guard 5 วิ บนเซิร์ฟเวอร์ทันที
      </p>

      <Modal
        open={ask !== null}
        onClose={() => setAsk(null)}
        title={ask?.length === 1 ? `ยืนยันปิดไม้ ${ask[0].side} ${ask[0].coin}?` : `ยืนยันปิดทุกไม้ (${ask?.length ?? 0} ไม้)?`}
        foot={
          <>
            <button className="btn" type="button" onClick={() => setAsk(null)}>ยกเลิก</button>
            <button className="btn btn-gold" type="button" onClick={confirm}>
              {ask && ask.length > 1 ? `ยืนยันปิด ${ask.length} ไม้` : "ยืนยันปิดไม้"}
            </button>
          </>
        }
      >
        {ask?.length === 1 && (
          <>
            <p>ปิดทั้งไม้ จำนวน {ask[0].qty} ด้วยคำสั่งราคาตลาด (market · reduce-only) — ส่งแล้วยกเลิกไม่ได้</p>
            <p>
              ราคาเข้า → ตลาด {ask[0].price} · กำไร/ขาดทุนตอนนี้{" "}
              <span className={pnlClass(ask[0].pnlSign)}>{ask[0].pnl}</span> USDT (ราคาปิดจริงอาจต่างจากนี้)
            </p>
          </>
        )}
        {ask && ask.length > 1 && (
          <>
            <p>ปิดทั้งไม้ทุกตัวข้างล่าง ด้วยคำสั่งราคาตลาด (market · reduce-only) — ส่งแล้วยกเลิกไม่ได้</p>
            <ul>
              {ask.map((x) => (
                <li key={x.id}>
                  {x.side} {x.coin} · จำนวน {x.qty} · กำไร/ขาดทุนตอนนี้{" "}
                  <span className={pnlClass(x.pnlSign)}>{x.pnl}</span> USDT
                </li>
              ))}
            </ul>
            <p>ราคาปิดจริงอาจต่างจากนี้</p>
          </>
        )}
        {ask && <p className="dim">Fast-Guard 5 วิ จะดำเนินการปิดให้ภายในไม่กี่วินาที แล้วอัปเดตตารางให้อัตโนมัติ</p>}
      </Modal>
    </>
  );
}
