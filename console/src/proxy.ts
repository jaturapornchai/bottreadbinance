import { NextResponse, type NextRequest } from "next/server";
import { COOKIE, fromLan, verifyToken } from "@/lib/auth";

/** First gate: outside the LAN and no valid session = 401 for the API, the login page for everything else (every route also re-checks the session itself). */
export function proxy(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (pathname === "/login" || pathname === "/api/login") return NextResponse.next();
  if (fromLan(req.headers) || verifyToken(req.cookies.get(COOKIE)?.value)) return NextResponse.next();
  if (pathname.startsWith("/api/")) return NextResponse.json({ error: "unauthorized" }, { status: 401, headers: { "Cache-Control": "no-store" } });
  // rewrite, not redirect: a redirect needs an absolute Location, which is wrong behind a reverse proxy that rewrites the Host header
  const login = req.nextUrl.clone();
  login.pathname = "/login";
  login.search = "";
  return NextResponse.rewrite(login, { headers: { "Cache-Control": "no-store" } });
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico|icon.svg).*)"] };
