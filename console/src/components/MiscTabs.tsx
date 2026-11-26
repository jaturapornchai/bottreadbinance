"use client";
import { useState } from "react";
import type { AgyGuardRow, AgyRoundRow, AgyStatusView, ChatLine } from "@/lib/types";
import { DataTable, Panel, Tabs, type Col, type TabDef } from "./ui";

type RunSub = "auto" | "guard";

const AUTO_COLS: Col<AgyRoundRow>[] = [
  { key: "at", head: "เวลา (ไทย)", width: 14, minPx: 92, sortValue: (r) => r.at, cell: (r) => r.at },
  {
    key: "ok", head: "ผล", width: 7, minPx: 54, align: "center",
    sortValue: (r) => (r.ok ? 1 : 0),
    cell: (r) => <span className={`badge ${r.ok ? "badge-ok" : "badge-err"}`}>{r.ok ? "ปกติ" : "พลาด"}</span>,
  },
  { key: "regime", head: "สภาพตลาด (Regime)", width: 22, minPx: 110, sortValue: (r) => r.regime, cell: (r) => r.regime },
  { key: "c1h", head: "ครบ 1H", width: 7, minPx: 54, align: "right", sortValue: (r) => r.complete1h, cell: (r) => r.complete1h },
  { key: "cands", head: "คัดผ่าน", width: 7, minPx: 52, align: "right", sortValue: (r) => r.cands, cell: (r) => r.cands },
  { key: "opened", head: "เปิด", width: 7, minPx: 46, align: "right", sortValue: (r) => r.opened, cell: (r) => r.opened },
  { key: "closed", head: "ปิด", width: 7, minPx: 46, align: "right", sortValue: (r) => r.closed, cell: (r) => r.closed },
  { key: "heat", head: "Heat", width: 11, minPx: 72, align: "right", sortValue: (r) => r.heat, cell: (r) => r.heat },
  {
    key: "eq", head: "มูลค่ารวม / ไม้", width: 18, minPx: 100, align: "right",
    sortValue: (r) => r.equity,
    cell: (r) => `${r.equity} U (${r.openCount})`,
    title: (r) => `มูลค่ารวม ${r.equity} USDT · ถืออยู่ ${r.openCount} ไม้ · กลยุทธ์ ${r.strategy}`,
  },
];

const GUARD_COLS: Col<AgyGuardRow>[] = [
  { key: "at", head: "เวลา (ไทย)", width: 16, minPx: 96, sortValue: (r) => r.at, cell: (r) => r.at },
  {
    key: "ok", head: "ผล", width: 9, minPx: 56, align: "center",
    sortValue: (r) => (r.ok ? 1 : 0),
    cell: (r) => <span className={`badge ${r.ok ? "badge-ok" : "badge-err"}`}>{r.ok ? "ปกติ" : "พลาด"}</span>,
  },
  { key: "checked", head: "ตรวจไม้", width: 10, minPx: 60, align: "right", sortValue: (r) => r.checked, cell: (r) => r.checked },
  { key: "closedCount", head: "ปิด", width: 8, minPx: 48, align: "right", sortValue: (r) => r.closedCount, cell: (r) => r.closedCount },
  { key: "closedText", head: "รายละเอียดการปิด", width: 41, minPx: 150, wrap: true, sortValue: (r) => r.closedText, cell: (r) => r.closedText },
  { key: "equity", head: "มูลค่ารวม USDT", width: 16, minPx: 92, align: "right", sortValue: (r) => r.equity, cell: (r) => r.equity },
];

export function RunsTab({ agy, onReloadSnap }: { agy: AgyStatusView | null; onReloadSnap: () => void }) {
  const [sub, setSub] = useState<RunSub>("auto");
  const rounds = agy?.recentRounds ?? [];
  const guards = agy?.recentGuards ?? [];
  const tabs: TabDef<RunSub>[] = [
    { key: "auto", label: `รอบ Auto-Trade 15 นาที (${rounds.length})` },
    { key: "guard", label: `Fast-Guard 5 วิ (${guards.length})` },
  ];

  return (
    <>
      <div className="row">
        <div>
          <h2 className="section">ประวัติรอบเทรด 15 นาที และ Fast-Guard 5 วิ</h2>
          <p className="dim">
            กลยุทธ์ {agy?.strategy ?? "V23_EMA_RSI_MOOD"} · รอบ 15 นาทีล่าสุด {agy?.lastRoundAt ?? "-"} · Fast-Guard ล่าสุด {agy?.guardAt ?? "-"}
          </p>
        </div>
      </div>

      <Tabs tabs={tabs} value={sub} onChange={setSub} idBase="runs" label="ประวัติการทำงาน" seg />
      <Panel idBase="runs" k={sub}>
        {sub === "auto" && (
          <DataTable
            storageKey="v22_runs_auto"
            caption="รอบ Auto-Trade 15 นาที (ใหม่สุดอยู่บน)"
            cols={AUTO_COLS}
            rows={rounds}
            empty="ยังไม่มีประวัติรอบ Auto-Trade 15 นาที"
            rowKey={(r, i) => `${r.at}|${i}`}
            onHeaderRefresh={onReloadSnap}
          />
        )}
        {sub === "guard" && (
          <DataTable
            storageKey="v22_runs_guard"
            caption="รอบ Fast-Guard 5 วิ (ใหม่สุดอยู่บน)"
            cols={GUARD_COLS}
            rows={guards}
            empty="ยังไม่มีประวัติ Fast-Guard"
            rowKey={(r, i) => `${r.at}|${i}`}
            onHeaderRefresh={onReloadSnap}
          />
        )}
      </Panel>
    </>
  );
}

export function ChatTab({ chat }: { chat: ChatLine[] }) {
  if (chat.length === 0) return <p className="empty">ยังไม่มีบันทึกกิจกรรมวันนี้</p>;
  return (
    <ul className="feed" tabIndex={0} aria-label="บันทึกกิจกรรมล่าสุด (ใหม่สุดอยู่บน)">
      {chat.map((c, i) => (
        <li key={`${c.time}|${i}`}>
          <span className="t">{c.time}</span>
          <span className="who">{c.name}</span>
          <span>{c.text}</span>
        </li>
      ))}
    </ul>
  );
}
