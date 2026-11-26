import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

export const COOKIE = "bt_session";
export const SESSION_S = 7 * 24 * 3600;
const MAX_FAILS = 5;
const LOCK_MS = 5 * 60_000;

/** Both env vars are required; a short secret is refused so a typo cannot leave the console guarded by "abc". */
export function configured(): boolean {
  return (process.env.CONSOLE_PASSWORD ?? "").length > 0 && (process.env.CONSOLE_SECRET ?? "").length >= 16;
}

const mac = (data: string) => createHmac("sha256", process.env.CONSOLE_SECRET ?? "").update(data).digest();

/** Constant time: both sides are HMAC'd to 32 bytes first, so length and content leak nothing through timing. */
export function passwordOk(input: string): boolean {
  if (!configured()) return false;
  return timingSafeEqual(mac("pw:" + input), mac("pw:" + (process.env.CONSOLE_PASSWORD ?? "")));
}

/** v1.<expiry epoch s>.<nonce>.<hmac>; the HMAC covers the first three parts. */
export function makeToken(nowS = Math.floor(Date.now() / 1000)): string {
  const body = `v1.${nowS + SESSION_S}.${randomBytes(12).toString("base64url")}`;
  return `${body}.${mac(body).toString("base64url")}`;
}

export function verifyToken(token: string | undefined | null, nowS = Math.floor(Date.now() / 1000)): boolean {
  if (!token || !configured() || token.length > 200) return false;
  const p = token.split(".");
  if (p.length !== 4 || p[0] !== "v1" || !/^\d{1,12}$/.test(p[1])) return false;
  const given = Buffer.from(p[3], "base64url");
  const want = mac(p.slice(0, 3).join("."));
  return given.length === want.length && timingSafeEqual(given, want) && parseInt(p[1], 10) > nowS;
}

export function tokenFromCookieHeader(header: string | null): string | undefined {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === COOKIE) return part.slice(i + 1).trim();
  }
  return undefined;
}

export const SOCKET_IP = "x-bt-socket-ip";
const LAN_SOCKET = /^(::ffff:)?192\.168\.\d{1,3}\.\d{1,3}$/;
const LAN_HOST = /^192\.168\.\d{1,3}\.\d{1,3}(:\d{1,5})?$/;

/**
 * ลุงจืด 2026-11-13: a browser inside the home LAN (192.168.x.x) needs no password; anything else still logs in.
 * The address is the connection's own, written by socket-ip.cjs. No such header (next dev, a forgotten preload) or a reverse proxy in
 * front (TRUST_PROXY=1: the socket is then the proxy's, not the browser's) = not LAN. The console must also have been opened by its
 * LAN IP: a web page that re-points its own name at this server (DNS rebinding) would otherwise act for the browser it runs in.
 */
export function fromLan(headers: { get(name: string): string | null }): boolean {
  return process.env.TRUST_PROXY !== "1" && LAN_SOCKET.test(headers.get(SOCKET_IP) ?? "") && LAN_HOST.test(headers.get("host") ?? "");
}

/** For route handlers: null = signed in or inside the LAN, otherwise the 401 to return. (The proxy checks too; this is the second lock.) */
export function requireSession(req: Request): Response | null {
  if (fromLan(req.headers) || verifyToken(tokenFromCookieHeader(req.headers.get("cookie")))) return null;
  return Response.json({ error: "unauthorized" }, { status: 401, headers: { "Cache-Control": "no-store" } });
}

// ---- lockout: 5 wrong passwords from one key = 5 minutes locked (memory only, resets on restart) ----
interface Tries { fails: number; lockedUntil: number }
const g = globalThis as unknown as { __btTries?: Map<string, Tries> };
const tries = (g.__btTries ??= new Map<string, Tries>());

/** Seconds still locked, 0 = free to try. */
export function lockedFor(key: string, now = Date.now()): number {
  for (const [k, t] of tries) if (t.lockedUntil && t.lockedUntil <= now) tries.delete(k);   // also keeps the map small
  const t = tries.get(key);
  return t && t.lockedUntil > now ? Math.ceil((t.lockedUntil - now) / 1000) : 0;
}

export function recordFail(key: string, now = Date.now()): { left: number; lockedS: number } {
  if (tries.size > 5000 && !tries.has(key)) tries.clear();   // a flood of spoofed keys must not grow memory without bound
  const t = tries.get(key) ?? { fails: 0, lockedUntil: 0 };
  t.fails += 1;
  if (t.fails >= MAX_FAILS) { t.lockedUntil = now + LOCK_MS; t.fails = 0; }
  tries.set(key, t);
  return { left: MAX_FAILS - t.fails, lockedS: t.lockedUntil > now ? Math.ceil(LOCK_MS / 1000) : 0 };
}

export const recordOk = (key: string) => void tries.delete(key);

/**
 * Lockout key. Behind exactly one trusted reverse proxy (TRUST_PROXY=1) it is the client IP that proxy appended (right-most
 * X-Forwarded-For entry); otherwise those headers are spoofable, so every client shares one bucket.
 */
export function clientKey(req: Request, trustProxy: boolean): string {
  if (!trustProxy) return "all";
  const xff = req.headers.get("x-forwarded-for")?.split(",").map((s) => s.trim()).filter(Boolean).pop();
  return (xff ?? req.headers.get("x-real-ip") ?? "all").slice(0, 64);
}
