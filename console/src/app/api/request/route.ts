import path from "node:path";
import { requireSession } from "@/lib/auth";
import { root } from "@/lib/env";
import { arr, isObj, readJson } from "@/lib/files";
import { closable } from "@/lib/portfolio";
import { writeClose, writeRequest } from "@/lib/requests";
import { isGuardAlive, loadRunner } from "@/lib/snapshot";
import { REQUEST_KINDS, type RequestKind } from "@/lib/types";

export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };

/** POST /api/request {"kind":"portfolio"} or {"kind":"close","id":"bto-..."} (the ปิดไม้ button)
 *  drops an empty file in cache/requests/ for server `fast-guard` (5s loop) to execute immediately.
 *  Legacy `run`/`stop`/`kafka` are refused in Monitor-Only mode (PC `agy_scheduler.py` owns trading rounds). */
export async function POST(req: Request) {
  if (!(req.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json"))
    return Response.json({ error: "json_only" }, { status: 415, headers: NO_STORE });
  const site = req.headers.get("sec-fetch-site");
  if (site && site !== "same-origin") return Response.json({ error: "cross_site" }, { status: 403, headers: NO_STORE });
  const denied = requireSession(req);
  if (denied) return denied;
  if (Number(req.headers.get("content-length") ?? 0) > 512) return Response.json({ error: "too_large" }, { status: 413, headers: NO_STORE });
  let kind: RequestKind | "close" | null = null;
  let id = "";
  try {
    const body: unknown = JSON.parse((await req.text()).slice(0, 512));
    const k = isObj(body) ? body.kind : null;
    kind = k === "close" ? "close" : REQUEST_KINDS.find((x) => x === k) ?? null;
    id = isObj(body) && typeof body.id === "string" ? body.id : "";
  } catch { /* falls through to 400 */ }
  if (!kind) return Response.json({ error: "bad_kind" }, { status: 400, headers: NO_STORE });

  try {
    const r = root();
    const now = Date.now();
    const alive = (await isGuardAlive(r, now)) || (await loadRunner(r, now)).up;

    if (kind === "close") {
      const pf = await readJson(path.join(r, "cache", "portfolio.json"), 5 * 1024 * 1024);
      const x = isObj(pf) ? arr(pf.shorts).find((s) => isObj(s) && s.id === id) : undefined;
      if (!isObj(x) || !closable(x)) return Response.json({ error: "not_closable" }, { status: 409, headers: NO_STORE });
      if (!alive) return Response.json({ error: "runner_down" }, { status: 503, headers: NO_STORE });
      return Response.json({ kind, ...(await writeClose(id)) }, { headers: NO_STORE });
    }
    if (kind === "portfolio") {
      if (!alive) return Response.json({ error: "runner_down" }, { status: 503, headers: NO_STORE });
      return Response.json({ kind, ...(await writeRequest("portfolio")) }, { headers: NO_STORE });
    }
    // Monitor-only console: never trigger legacy team.py/runner.py run/stop/kafka
    return Response.json({ error: "monitor_only" }, { status: 403, headers: NO_STORE });
  } catch (e) {
    console.error("request failed:", e instanceof Error ? e.message : "unknown error");
    return Response.json({ error: "write_failed" }, { status: 500, headers: NO_STORE });
  }
}
