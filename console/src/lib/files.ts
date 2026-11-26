import fs from "node:fs/promises";

export const MB = 1024 * 1024;

/**
 * Text of a file, never more than `max` bytes. tail=true keeps the END of an oversize file (the first, cut line is dropped);
 * otherwise an oversize file is refused (null) so a runaway JSON cannot eat the server's memory.
 */
export async function readCapped(file: string, max: number, tail = false): Promise<string | null> {
  let fh;
  try { fh = await fs.open(file, "r"); } catch { return null; }
  try {
    const st = await fh.stat();
    if (!st.isFile()) return null;
    if (st.size > max && !tail) return null;
    const len = Math.min(st.size, max);
    const buf = Buffer.alloc(len);
    const { bytesRead } = await fh.read(buf, 0, len, st.size - len);
    let text = buf.subarray(0, bytesRead).toString("utf8");
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
    return len < st.size ? text.slice(text.indexOf("\n") + 1) : text;
  } catch { return null; } finally { await fh.close(); }
}

export async function readJson(file: string, max: number): Promise<unknown> {
  const text = await readCapped(file, max);
  if (text === null) return null;
  try { return JSON.parse(text); } catch { return null; }   // half-written / bad file = "no data"
}

export async function statOrNull(file: string) {
  try { return await fs.stat(file); } catch { return null; }
}

/** YYYY-MM-DD sub-folders of reports/, newest first. */
export async function dayDirs(reports: string): Promise<string[]> {
  try {
    return (await fs.readdir(reports, { withFileTypes: true }))
      .filter((d) => d.isDirectory() && /^\d{4}-\d\d-\d\d$/.test(d.name)).map((d) => d.name).sort().reverse();
  } catch { return []; }
}

// ---- small helpers to read untyped JSON without `any` ----
export type Obj = Record<string, unknown>;
export const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
export const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
/** Like JsonElement.ToString(): null/missing = "", strings as they are, numbers/bools as text, anything else as JSON. */
export function str(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  return JSON.stringify(v);
}
