"use client";
import { useMemo } from "react";
import type { PlanRowView, PlanView } from "@/lib/types";
import { DataTable, SideBadge, type Col } from "./ui";

export function PlanTab({
  plan,
  onPng,
  onChart,
  onReloadSnap,
}: {
  plan: PlanView | null;
  onPng: (path: string, title: string) => void;
  onChart: (coin: string) => void;
  onReloadSnap: () => void;
}) {
  const cols: Col<PlanRowView>[] = useMemo(() => [
    { key: "n", head: "#", width: 4, minPx: 34, align: "center", sortValue: (r) => r.n, cell: (r) => r.n },
    { key: "side", head: "ฝั่ง", width: 8, minPx: 56, align: "center", sortValue: (r) => r.sideCode, cell: (r) => <SideBadge code={r.sideCode} text={r.side} /> },
    {
      key: "coin", head: "เหรียญ", width: 9, minPx: 64, strong: true, sortValue: (r) => r.coin,
      cell: (r) => (
        <button type="button" className="coin-btn" onClick={() => onChart(r.coin)} title={`เปิดกราฟ TradingView สด ${r.coin}/USDT`}>
          {r.coin}
        </button>
      ),
    },
    { key: "signal", head: "เอนจิน", width: 15, minPx: 92, sortValue: (r) => r.signal, cell: (r) => r.signal },
    { key: "score", head: "คะแนน", width: 7, minPx: 54, align: "right", sortValue: (r) => r.score, cell: (r) => r.score },
    { key: "price", head: "ราคาปิดล่าสุด", width: 11, minPx: 76, align: "right", sortValue: (r) => r.price, cell: (r) => r.price },
    { key: "rsi1h", head: "RSI 1H", width: 8, minPx: 54, align: "right", sortValue: (r) => r.rsi1h, cell: (r) => r.rsi1h },
    { key: "vol30", head: "Vol 24h", width: 9, minPx: 64, align: "right", sortValue: (r) => r.vol30, cell: (r) => r.vol30 },
    {
      key: "trend", head: "7D · สภาพตลาด / หมายเหตุ", width: 22, minPx: 120, wrap: true,
      sortValue: (r) => r.trend,
      cell: (r) => (
        <span className="row">
          <span>{r.r7 !== "-" ? `${r.r7} · ${r.trend}` : r.trend}</span>
          {r.png && (
            <button
              className="btn btn-link"
              type="button"
              aria-label={`เปิดภาพกราฟ ${r.coin}`}
              onClick={() => onPng(r.png ?? "", `กราฟ ${r.coin}`)}
            >
              PNG
            </button>
          )}
        </span>
      ),
    },
    {
      key: "chart", head: "กราฟ", width: 7, minPx: 66, align: "center",
      cell: (r) => (
        <button
          className="btn btn-xs btn-chart"
          type="button"
          aria-label={`เปิดกราฟ TradingView ${r.coin}`}
          title={`เปิดกราฟ TradingView สด ${r.coin}/USDT`}
          onClick={() => onChart(r.coin)}
        >
          📈 กราฟ
        </button>
      ),
    },
  ], [onPng, onChart]);

  return (
    <>
      <h2 className="section">
        {plan ? plan.header : "ยังไม่มีแผนเทรดวันนี้ — รอตัวเทรด PC V23 ทำงานรอบ 15 นาทีถัดไป"}
      </h2>
      {plan && (
        <>
          {plan.day && <p className="dim">{plan.day}</p>}
          <DataTable
            storageKey="v22_plan"
            caption="แผนเทรด LONG/SHORT รอบล่าสุด"
            cols={cols}
            rows={plan.rows}
            empty="รอบล่าสุดยังไม่มีเหรียญผ่านเกณฑ์เปิดไม้"
            rowKey={(r) => `${r.n}|${r.coin}`}
            onHeaderRefresh={onReloadSnap}
          />
          {plan.info && <p className="note">{plan.info}</p>}
          {plan.narrative && (
            <div className="card narrative" tabIndex={0} role="region" aria-label="สรุปรอบล่าสุดจาก V23">
              {plan.narrative}
            </div>
          )}
        </>
      )}
      <p className="disclaimer">
        แผนคัดกรองและเทรดอัตโนมัติทำงานจากเครื่อง PC ทุก 15 นาที (:05/:20/:35/:50) ด้วยกฎโค้ด V23 ล้วน · หน้าเว็บนี้แสดงผล Monitor อย่างเดียว
      </p>
    </>
  );
}
