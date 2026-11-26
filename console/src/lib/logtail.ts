import fs from "node:fs/promises";
import { bkkDate, bkkDateTime, bkkHMS, logStampMs } from "./time.ts";

/** One line of a bot's work. kind: said / llm / warn / error. `when` is Thai time "yyyy-MM-dd HH:mm:ss"; ts = epoch ms. */
export interface LogEntry { when: string; ts: number; icon: string; kind: "said" | "llm" | "warn" | "error"; text: string }
export interface TokenRow { day: string; model: string; thread: string; calls: number; tin: number; tout: number }

const LINE_RE = /^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d),\d+ (\S+) ([A-Z]+) (.*)$/;
const TOKEN_RE = /^(\S+) tokens in=(\d+|None) out=(\d+|None)$/;
const KEEP = 300;          // entries per bot
const MAX_PROBLEMS = 60;
const nz = (s: string) => (/^\d+$/.test(s) ? parseInt(s, 10) : 0);

/**
 * Reads cache/team.log incrementally (like LogTail.cs): token counts per (day, model, thread), per-thread timeline, errors, recent problems.
 * The first read takes at most the last `firstMax` bytes; every later read only the bytes appended since.
 */
export class LogTail {
  private offset = 0;
  private carry: Buffer = Buffer.alloc(0);   // bytes after the last newline: finished by the next read (keeps UTF-8 whole)
  private lastThread: string | null = null;
  private chain: Promise<void> = Promise.resolve();
  readonly tokens = new Map<string, TokenRow>();
  readonly lastError = new Map<string, number>();
  readonly errorsToday = new Map<string, number>();   // `${day}|${thread}`
  readonly entries = new Map<string, LogEntry[]>();
  problems: string[] = [];   // oldest first

  constructor(private readonly file: string, private readonly tzOffsetMin: number, private readonly firstMax = 8 * 1024 * 1024) {}

  /** Serialised: two requests at the same moment must not read the same bytes twice. */
  read(): Promise<void> {
    const run = this.chain.then(() => this.readOnce());
    this.chain = run.catch(() => undefined);
    return run;
  }

  private reset() {
    this.offset = 0; this.carry = Buffer.alloc(0); this.lastThread = null;
    this.tokens.clear(); this.lastError.clear(); this.errorsToday.clear(); this.entries.clear(); this.problems = [];
  }

  private async readOnce() {
    let fh;
    try { fh = await fs.open(this.file, "r"); } catch { return; }
    try {
      const size = (await fh.stat()).size;
      if (size < this.offset) this.reset();   // log was replaced
      let skipFirstLine = false;
      if (this.offset === 0 && size > this.firstMax) { this.offset = size - this.firstMax; skipFirstLine = true; }
      while (this.offset < size) {
        const buf = Buffer.alloc(Math.min(size - this.offset, 4 * 1024 * 1024));
        const { bytesRead } = await fh.read(buf, 0, buf.length, this.offset);
        if (bytesRead === 0) break;
        this.offset += bytesRead;
        let data = Buffer.concat([this.carry, buf.subarray(0, bytesRead)]);
        if (skipFirstLine) {   // we started mid-line
          const nl = data.indexOf(0x0a);
          if (nl < 0) { this.carry = Buffer.alloc(0); continue; }
          data = data.subarray(nl + 1); skipFirstLine = false;
        }
        const end = data.lastIndexOf(0x0a);
        this.carry = end < 0 ? data : Buffer.from(data.subarray(end + 1));
        if (end >= 0) for (const raw of data.subarray(0, end).toString("utf8").split("\n")) this.feed(raw.replace(/\r$/, ""));
      }
    } finally { await fh.close(); }
  }

  feed(l: string) {
    const m = LINE_RE.exec(l);
    if (!m) {   // traceback / multi-line message: belongs to the entry above it
      const prev = this.lastThread ? this.entries.get(this.lastThread) : undefined;
      const last = prev?.[prev.length - 1];
      if (last && last.text.length < 3000) last.text += "\n" + l;
      return;
    }
    const [, day, time, thread, level, raw] = m;
    this.lastThread = thread;
    const ts = logStampMs(day, time, this.tzOffsetMin);
    const bkkDay = bkkDate(ts);
    const when = bkkDateTime(ts);
    const msg = raw.replace(/\\u([0-9a-fA-F]{4})/g, (_, h: string) => String.fromCharCode(parseInt(h, 16)));   // logs from before the UTF-8 fix
    let entry: LogEntry;
    const t = TOKEN_RE.exec(msg);
    if (t) {
      const key = `${bkkDay}|${t[1]}|${thread}`;
      const v = this.tokens.get(key) ?? { day: bkkDay, model: t[1], thread, calls: 0, tin: 0, tout: 0 };
      v.calls += 1; v.tin += nz(t[2]); v.tout += nz(t[3]);
      this.tokens.set(key, v);
      entry = { when, ts, icon: "🤖", kind: "llm", text: `เรียก AI ${t[1]} · token เข้า ${nz(t[2]).toLocaleString("en-US")} ออก ${nz(t[3]).toLocaleString("en-US")}` };
    } else if (level === "WARNING" || level === "ERROR") {
      if (level === "ERROR") {
        this.lastError.set(thread, ts);
        const k = `${bkkDay}|${thread}`;
        this.errorsToday.set(k, (this.errorsToday.get(k) ?? 0) + 1);
      }
      const line = `${when} ${thread} ${level} ${msg}`;   // stamp shown in Thai time, not the host's
      this.problems.push(line.length > 400 ? line.slice(0, 400) + "…" : line);
      if (this.problems.length > MAX_PROBLEMS) this.problems.shift();
      entry = { when, ts, icon: level === "ERROR" ? "❌" : "⚠", kind: level === "ERROR" ? "error" : "warn", text: msg };
    } else {
      const colon = msg.indexOf(": ");   // say() logs "ชื่อ: ข้อความ"; the name is in the header already
      entry = { when, ts, icon: "💬", kind: "said", text: colon > 0 && colon < 20 ? msg.slice(colon + 2) : msg };
    }
    const list = this.entries.get(thread) ?? [];
    this.entries.set(thread, list);
    list.push(entry);
    if (list.length > KEEP) list.shift();
  }

  /** Newest first; when = time only for today (Thai day). */
  timeline(thread: string, today: string): LogEntry[] {
    return [...(this.entries.get(thread) ?? [])].reverse().map((e) => (e.when.startsWith(today) ? { ...e, when: bkkHMS(e.ts) } : e));
  }
}
