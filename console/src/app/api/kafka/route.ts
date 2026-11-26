import { requireSession } from "@/lib/auth";
import { loadKafka } from "@/lib/kafka";

export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };

/** GET /api/kafka?k=<key the browser already has>  cache/kafka.json parsed; same key = {unchanged:true}. */
export async function GET(req: Request) {
  const denied = requireSession(req);
  if (denied) return denied;
  const k = new URL(req.url).searchParams.get("k");
  if (k !== null && !/^[A-Za-z0-9|.+-]{1,60}$/.test(k)) return Response.json({ error: "bad_key" }, { status: 400, headers: NO_STORE });
  try {
    return Response.json(await loadKafka(k), { headers: NO_STORE });
  } catch (e) {
    console.error("kafka read failed:", e instanceof Error ? e.message : "unknown error");
    return Response.json({ error: "read_failed" }, { status: 500, headers: NO_STORE });
  }
}
