import { requireSession } from "@/lib/auth";
import { root } from "@/lib/env";
import { openPng } from "@/lib/safepng";

export const dynamic = "force-dynamic";

/** GET /api/png?p=<path from the plan>  a chart PNG, only from reports/ (see safepng.ts). */
export async function GET(req: Request) {
  const denied = requireSession(req);
  if (denied) return denied;
  const p = new URL(req.url).searchParams.get("p");
  if (!p || p.length > 512) return Response.json({ error: "bad_path" }, { status: 400, headers: { "Cache-Control": "no-store" } });
  const png = await openPng(root(), p);
  if (!png) return Response.json({ error: "not_found" }, { status: 404, headers: { "Cache-Control": "no-store" } });
  return new Response(new Uint8Array(png), {
    headers: { "Content-Type": "image/png", "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline" },
  });
}
