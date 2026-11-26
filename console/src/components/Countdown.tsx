"use client";
import { useEffect, useState } from "react";
import { bkkThaiDate } from "@/lib/clock";
import { bkkHM, bkkHMS, parseIso } from "@/lib/time";

/** Ticks every 1 second inside this tiny chip so the rest of the page does not re-render. */
function useNow(offsetMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);
  return now + offsetMs;
}

export function GuardChip({ guardUp, guardAt }: { guardUp: boolean; guardAt: string }) {
  return (
    <span
      className={`chip ${guardUp ? "ok" : "warn"}`}
      title={guardUp ? `Fast-Guard 5 วิ ทำงานปกติ (อัปเดตล่าสุด ${guardAt}) — รองรับปุ่มปิดไม้และตรวจ SL/TP ทุก 5 วิ` : `ไม่พบสัญญาณ Fast-Guard ล่าสุด (${guardAt})`}
    >
      <span className={`beacon${guardUp ? "" : " idle"}`} aria-hidden="true" />
      {guardUp ? "Fast-Guard 5 วิ ปกติ" : "Fast-Guard รอสัญญาณ"}
    </span>
  );
}

export function NextRoundChip({ nextRoundIso, offsetMs, lastRoundAt }: {
  nextRoundIso: string;
  offsetMs: number;
  lastRoundAt: string;
}) {
  const now = useNow(offsetMs);
  const next = parseIso(nextRoundIso);
  let text = "● รอบ 15 นาที (PC)";
  if (next !== null) {
    const left = next - now;
    if (left <= 0) {
      text = "● กำลังเข้ารอบ 15 นาที…";
    } else {
      const secs = Math.max(0, Math.ceil(left / 1000));
      const mmss = `${String(Math.floor(secs / 60)).padStart(2, "0")}:${String(secs % 60).padStart(2, "0")}`;
      text = `● รอบถัดไป ${bkkHM(next)} น. (อีก ${mmss})`;
    }
  }
  return (
    <span className="chip gold" title={`ตัวเทรด PC ทำงานอัตโนมัติทุก 15 นาที · รอบล่าสุด: ${lastRoundAt}`}>
      {text}
    </span>
  );
}

const RING_R = 6.5;
const RING_C = 2 * Math.PI * RING_R;
const REFRESH_MS = 10_000;   // shared server refresh slot, same as Console.tsx AUTO_REFRESH_MS
const REFRESH_S = REFRESH_MS / 1000;

/**
 * Content of the "↻ รีเฟรชพอร์ต (XX วิ)" button: a 10-second SVG ring + 1-second countdown to the next shared
 * :00/:10/:20/:30/:40/:50 server refresh slot (offsetMs = server wall clock, identical across all open screens).
 * spinKey > 0 replays a one-shot spin of the ↻ icon after a manual click.
 */
export function RefreshLabel({ offsetMs, spinKey }: { offsetMs: number; spinKey: number }) {
  const [mounted, setMounted] = useState(false);
  const now = useNow(offsetMs);
  useEffect(() => { setMounted(true); }, []);
  const remMs = REFRESH_MS - (((now % REFRESH_MS) + REFRESH_MS) % REFRESH_MS);
  const secs = Math.min(REFRESH_S, Math.max(1, Math.ceil(remMs / 1000)));
  const left = mounted ? secs / REFRESH_S : 1;
  // at a fresh slot (10) the arc refills instantly; every other second it glides 1/10 with a 1s linear transition
  const ringCls = `cd-ring${!mounted || secs >= REFRESH_S ? " cd-reset" : ""}${mounted && secs <= 2 ? " cd-soon" : ""}`;
  return (
    <>
      <span className={ringCls} aria-hidden="true">
        <svg viewBox="0 0 18 18" width="18" height="18" focusable="false">
          <circle className="cd-track" cx="9" cy="9" r={RING_R} />
          <circle className="cd-arc" cx="9" cy="9" r={RING_R} strokeDasharray={RING_C.toFixed(2)} strokeDashoffset={(RING_C * (1 - left)).toFixed(2)} />
        </svg>
        <span key={spinKey} className={`rf-ico${spinKey > 0 ? " spin-once" : ""}`}>↻</span>
      </span>
      <span>รีเฟรชพอร์ต</span>
      {mounted && (
        <span className="btn-cd" aria-hidden="true">
          ({String(secs).padStart(2, "0")} วิ)
        </span>
      )}
    </>
  );
}

/** FIDS footer clock: Thai time + short Thai date on the shared server wall clock (offsetMs). Blank until mounted = no hydration mismatch. */
export function TerminalClock({ offsetMs }: { offsetMs: number }) {
  const [mounted, setMounted] = useState(false);
  const now = useNow(offsetMs);
  useEffect(() => { setMounted(true); }, []);
  return (
    <div className="clock" title="เวลาไทยตามนาฬิกาเซิร์ฟเวอร์">
      <span className="clock-time">{mounted ? `${bkkHMS(now)} น.` : "--:--:-- น."}</span>
      <span className="clock-date">{mounted ? bkkThaiDate(now) : "\u00a0"}</span>
    </div>
  );
}
