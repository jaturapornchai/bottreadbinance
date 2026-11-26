import fs from "node:fs/promises";
import path from "node:path";
import { root } from "./env.ts";
import type { RequestKind } from "./types.ts";

// Minimum gap between two files of the same kind. kafka: the app ran `bus.py tail` at most every 30 s while its tab was open.
const MIN_GAP_MS: Record<RequestKind, number> = { run: 0, stop: 0, portfolio: 10_000, kafka: 30_000 };

const g = globalThis as unknown as { __btReq?: Map<RequestKind, number> };
const last = (g.__btReq ??= new Map<RequestKind, number>());

export type RequestResult = { written: true } | { written: false; reason: "pending" | "throttled"; waitS: number };

/**
 * Ask the runner for something by creating an EMPTY file cache/requests/<kind>; the runner deletes it once handled.
 * This is the only place the console writes, and the file name is one of four constants (never user text) or close-<checked id>.
 */
export async function writeRequest(kind: RequestKind, now = Date.now()): Promise<RequestResult> {
  const gap = MIN_GAP_MS[kind] - (now - (last.get(kind) ?? 0));
  if (gap > 0) return { written: false, reason: "throttled", waitS: Math.ceil(gap / 1000) };
  if (!(await touch(kind))) return { written: false, reason: "pending", waitS: 0 };
  last.set(kind, now);
  return { written: true };
}

/** The ปิดไม้ button: cache/requests/close-<id>; runner.py runs `team.py close <id>` within one tick, beside a round too. The id is checked
 *  here again (letters, digits, "-" only: never a path) and once more by runner.py and the order server. */
export async function writeClose(id: string): Promise<RequestResult> {
  if (!/^bto-[0-9A-Za-z-]{1,32}$/.test(id)) throw new Error("bad position id");
  return (await touch(`close-${id}`)) ? { written: true } : { written: false, reason: "pending", waitS: 0 };
}

/** false = the same request already waits for the runner (wx), nothing to add. */
async function touch(name: string): Promise<boolean> {
  const dir = path.join(root(), "cache", "requests");
  await fs.mkdir(dir, { recursive: true, mode: 0o775 });
  try {
    await fs.writeFile(path.join(dir, name), "", { flag: "wx", mode: 0o664 });
    return true;
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "EEXIST") return false;
    throw e;
  }
}
