import { requireSession } from "@/lib/auth";
import { loadSnapshot } from "@/lib/snapshot";
import { TEAM } from "@/lib/team";

export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };

/** GET /api/snapshot?sel=<thread>  everything the page shows; `sel` (optional) = the bot card whose work is open. */
export async function GET(req: Request) {
  const denied = requireSession(req);
  if (denied) return denied;
  const sel = new URL(req.url).searchParams.get("sel");
  const thread = sel && TEAM.some((t) => t.thread === sel) ? sel : null;   // whitelist, anything else is ignored
  try {
    return Response.json(await loadSnapshot(thread), { headers: NO_STORE });
  } catch (e) {
    console.error("snapshot failed:", e instanceof Error ? e.message : "unknown error");
    return Response.json({ error: "read_failed" }, { status: 500, headers: NO_STORE });
  }
}
