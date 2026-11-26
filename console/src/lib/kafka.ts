import path from "node:path";
import { arr, isObj, MB, readJson, statOrNull, str } from "./files.ts";
import { root } from "./env.ts";
import { loadRunner } from "./snapshot.ts";
import { fmtDMS, parseIso } from "./time.ts";
import type { KafkaRow, KafkaView } from "./types.ts";

const EMPTY = (key: string, error: string): KafkaView => ({ key, at: "", atMs: null, error, newest: "", topics: [], rows: [], runnerAt: "" });

/**
 * cache/kafka.json (written by `bus.py tail` through the runner): the last messages of every team topic.
 * `known` = the key the browser already has: same key means nothing new, so only the key goes back.
 */
export async function loadKafka(known: string | null): Promise<KafkaView | { key: string; unchanged: true }> {
  const r = root();
  const f = path.join(r, "cache", "kafka.json");
  const st = await statOrNull(f);
  if (!st?.isFile()) return EMPTY("none", "ยังไม่มีไฟล์ cache/kafka.json — รอสักครู่หรือกดรีเฟรช");
  const key = `${st.size}|${st.mtimeMs}`;
  if (known === key) return { key, unchanged: true };
  const j = await readJson(f, 8 * MB);
  if (!isObj(j)) return EMPTY("bad", "อ่าน cache/kafka.json ไม่ได้ (ไฟล์กำลังถูกเขียนหรือเสีย) — รอรอบถัดไป");
  const rows: KafkaRow[] = arr(j.messages).filter(isObj).map((x) => ({
    when: fmtDMS(str(x.at)), topic: str(x.topic), offset: typeof x.offset === "number" && Number.isInteger(x.offset) ? x.offset : -1,
    summary: str(x.summary).replaceAll("\n", " "), json: str(x.json),
  }));
  const runner = await loadRunner(r, Date.now());
  return {
    key, at: str(j.at) ? fmtDMS(str(j.at)) : "", atMs: parseIso(str(j.at)), error: str(j.error), runnerAt: runner.kafkaAt,
    newest: rows.length > 0 ? `${rows.length}/${rows[0].topic}/${rows[0].offset}` : "",   // unchanged = keep the table (and the row being read) as it is
    topics: arr(j.topics).filter(isObj).map((t) => ({ topic: str(t.topic), count: str(t.count) })), rows,
  };
}
