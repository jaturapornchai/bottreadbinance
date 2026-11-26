import { arr, isObj, str, type Obj } from "./files.ts";
import type { PlanRowView, PlanView, SideCode } from "./types.ts";

const names = (v: unknown) => (arr(v).length > 0 ? arr(v).map(str).join(", ") : "-");

/** Port of MainWindow.ShowPlan: dca-plan.json -> header, day note, table rows, info lines, narrative. */
export function buildPlan(p: Obj, key: string, at: number): PlanView {
  const mkt = str(p.mkt_trend);
  const header = `แผนเทรด ${str(p.run_id)} (วัน UTC) · สร้าง ${str(p.created)} (เวลาไทย) · สแกน ${str(p.universe)} สัญญา · เข้ากฎ ${str(p.passing)}` + (mkt ? ` · ${mkt}` : "");
  const active = arr(p.rows).filter(isObj), watch = arr(p.watchlist).filter(isObj);
  const openNew = p.open_new !== false;   // missing = true: plans written before the pause switch existed
  const day = !openNew ? "⛔ หยุดเปิดไม้ใหม่ (ลุงจืดสั่ง) — ไม้ที่ถือยังตรวจกติกาออกทุกรอบ"
    : active.length === 0 && watch.length > 0
    ? `รอบนี้ยังไม่มีเหรียญเข้าจุดเปิดไม้ — ตารางด้านล่างคือ ${watch.length} เหรียญตามเทรนด์ตลาดคะแนนสูงสุด (Watchlist) ดูป้าย [ถืออยู่แล้ว]/[เฝ้าดู]/[พร้อมเปิด…] ในช่องสุดท้าย (หมายเหตุโค้ด)`
    : !("entry_day" in p) ? ""
    : p.entry_day === true
      ? (watch.length > 0 ? "รอบนี้เปิดไม้ได้ (แถวเลข 1.. คือเหรียญที่เปิดไม้ · แถว W1.. คือ Watchlist ดูป้ายในช่องสุดท้าย)"
        : "รอบนี้เปิดไม้ได้ (ตัวเทรด PC V23 เปิด LONG/SHORT อัตโนมัติตามสัญญาณ ไม่จำกัดจำนวนไม้ ตามเงินว่าง)")
      : "รอบนี้ยังไม่เปิดไม้ใหม่ — ข้อมูลใน ClickHouse ยังไม่ครบ รอรอบถัดไป";

  // a key that is absent shows blank, a key that is present but null shows blank; only these six default to "-"
  const rows: PlanRowView[] = [...active, ...watch].map((r, i) => {
    const s = (k: string, dflt = "") => (k in r ? str(r[k]) : dflt);
    const signal = s("signal", "-");
    const isLong = /LONG/i.test(s("side")) || /LONG/i.test(signal);
    const code: SideCode = isLong ? "LONG" : "SHORT";
    return {
      n: i < active.length ? String(i + 1) : `W${i - active.length + 1}`,
      sideCode: code, side: isLong ? "▲ LONG" : "▼ SHORT", coin: s("coin"), signal, score: s("score", "-"), price: s("price"),
      rsi1h: s("rsi_1h", "-"), rsi4h: s("rsi_4h", "-"), ageDays: s("age_days"), trend1d: s("trend_1d", "-"), ddAth: s("dd_ath", "-"),
      vol30: s("vol30"), r7: s("r7"), trend: s("trend"), png: typeof r.png === "string" && r.png !== "" ? r.png : null,
    };
  });

  const sigByCoin = new Map<string, string>();
  for (const r of rows) if (r.coin && r.signal !== "-" && r.signal !== "" && !sigByCoin.has(r.coin)) sigByCoin.set(r.coin, `${r.side} · ${r.signal}`);
  const passing = arr(p.passing_coins).length > 0
    ? arr(p.passing_coins).map((x) => { const c = str(x); const sg = sigByCoin.get(c); return sg ? `${c} [${sg}]` : c; }).join(", ") : "-";
  const cooled = names(p.cooled);
  const info = `เข้ากฎ: ${passing}\nมี Position อยู่แล้ว (ไม่เปิดซ้ำ): ${p.held === null ? "ไม่ทราบ (อ่านพอร์ตไม่ได้)" : names(p.held)}`
    + (cooled !== "-" ? ` · พักเหรียญ (Cooldown): ${cooled}` : "") + (str(p.rule) ? "\nกฎ: " + str(p.rule) : "");

  return {
    key, at, header, day, paused: !openNew, rows, info, runId: str(p.run_id),
    narrative: str(p.narrative).replaceAll("**", ""),   // the CIO writes markdown bold; the page shows plain text
    shortCoins: !openNew ? [] : arr(p.short).map((x) => { const c = str(x); return c.endsWith("USDT") ? c.slice(0, -4) : c; }),
  };
}
