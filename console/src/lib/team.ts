/** The 7 team members, same names / emojis / wording as the WPF app (Snapshot.Team). group = Kafka consumer group (null = timer bot). */
export interface Member {
  thread: string; emoji: string; name: string; role: string; model: string; group: string | null; listens: string; sends: string;
}

export const TEAM: readonly Member[] = [
  { thread: "quant", emoji: "📊", name: "ควอนต์", role: "bot · ClickHouse futures + กฎเทรนด์ LONG/SHORT + เทรนด์ตลาด 1 ชม.", model: "ไม่ใช้ AI", group: "quant", listens: "bt.dca.request", sends: "bt.dca.candidates" },
  { thread: "secretary", emoji: "📝", name: "เลขา", role: "bot · เขียนรายงาน", model: "ไม่ใช้ AI", group: "secretary", listens: "bt.team.chat, bt.dca.plan", sends: "reports/" },
  { thread: "clock", emoji: "⏰", name: "นาฬิกา", role: "bot · เริ่มรอบ สั่งควอนต์คัดเหรียญ", model: "ไม่ใช้ AI", group: null, listens: "ปุ่ม ▶ ทำงาน / ทุก 1 ชม.", sends: "bt.dca.request" },
  { thread: "chartist", emoji: "📈", name: "ช่างกราฟ", role: "agent · อ่านกราฟ", model: "deepseek-flash", group: "chartist", listens: "bt.chart.request", sends: "bt.chart.result" },
  { thread: "risk_critic", emoji: "🛡", name: "ผู้คุมความเสี่ยง", role: "agent · ชี้ความเสี่ยงแต่ละเหรียญ (ไม่ตัดเหรียญ)", model: "deepseek-flash", group: "risk", listens: "bt.dca.candidates", sends: "bt.dca.review" },
  { thread: "cio", emoji: "👔", name: "หัวหน้าพอร์ต", role: "agent · เขียนสรุปแผนเทรด LONG/SHORT (ไม่ได้ตัดสินไม้ที่ถือ — ออกตามกติกาในโค้ด)", model: "deepseek-v4-pro", group: "cio", listens: "bt.dca.review, bt.chart.result", sends: "bt.chart.request, bt.dca.plan" },
  { thread: "buyer", emoji: "🛒", name: "คนซื้อขาย", role: "bot · Auto LONG/SHORT เปิดตามแผน (ถ้าแผนสั่งหยุดเปิด แท็บแผนเทรดจะแจ้ง) · ปิดตามกติกาออกในโค้ด ไม่ใช่ตาม AI", model: "ไม่ใช้ AI", group: "buyer", listens: "bt.dca.plan", sends: "server (DCA_PROXY_HOST) → Binance (key อยู่บน server)" },
];
