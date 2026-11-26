import { NextResponse } from "next/server";
import { clientKey, configured, COOKIE, lockedFor, makeToken, passwordOk, recordFail, recordOk, SESSION_S } from "@/lib/auth";
import { forceSecureCookie, trustProxy } from "@/lib/env";

export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };

/** The only route open without a session. The password is compared in constant time and never logged or echoed. */
export async function POST(req: Request) {
  if (!configured()) return NextResponse.json({ error: "not_configured" }, { status: 503, headers: NO_STORE });
  const key = clientKey(req, trustProxy());
  const wait = lockedFor(key);
  if (wait > 0) return NextResponse.json({ error: "locked", retryAfterS: wait }, { status: 429, headers: { ...NO_STORE, "Retry-After": String(wait) } });

  if (Number(req.headers.get("content-length") ?? 0) > 2048) return NextResponse.json({ error: "too_large" }, { status: 413, headers: NO_STORE });
  let password = "";
  try {
    const body: unknown = JSON.parse((await req.text()).slice(0, 2048));
    if (typeof body === "object" && body !== null && "password" in body && typeof body.password === "string") password = body.password.slice(0, 256);
  } catch { /* not JSON: treated as a wrong password below */ }

  if (!passwordOk(password)) {
    const f = recordFail(key);
    return NextResponse.json({ error: "bad_password", left: f.left, lockedS: f.lockedS }, { status: 401, headers: NO_STORE });
  }
  recordOk(key);
  const res = NextResponse.json({ ok: true }, { headers: NO_STORE });
  const secure = forceSecureCookie() || req.headers.get("x-forwarded-proto") === "https" || new URL(req.url).protocol === "https:";
  res.cookies.set(COOKIE, makeToken(), { httpOnly: true, sameSite: "strict", path: "/", maxAge: SESSION_S, secure });
  return res;
}
