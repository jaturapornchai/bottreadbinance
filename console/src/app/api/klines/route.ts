import { requireSession } from "@/lib/auth";
import { clampLimit, normalizeSymbol, parseBinanceKlines, parseInterval } from "@/lib/indicators";

export const dynamic = "force-dynamic";
const NO_STORE = { "Cache-Control": "no-store" };
const BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines";
const FETCH_TIMEOUT_MS = 10_000;

/**
 * GET /api/klines?symbol=BTCUSDT&interval=15m&limit=240
 * Proxies public Binance USDⓈ-M Futures klines for the interactive TradingView chart.
 */
export async function GET(req: Request) {
  const denied = requireSession(req);
  if (denied) return denied;

  const sp = new URL(req.url).searchParams;
  const symbol = normalizeSymbol(sp.get("symbol"));
  if (!symbol) {
    return Response.json({ error: "bad_symbol" }, { status: 400, headers: NO_STORE });
  }
  const interval = parseInterval(sp.get("interval"));
  if (!interval) {
    return Response.json({ error: "bad_interval" }, { status: 400, headers: NO_STORE });
  }
  const limit = clampLimit(sp.get("limit"));

  const url = `${BINANCE_FAPI_KLINES}?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=${limit}`;
  try {
    const res = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      headers: { Accept: "application/json" },
    });
    if (!res.ok) {
      const status = res.status === 400 ? 404 : 502;
      return Response.json({ error: "upstream_failed", upstreamStatus: res.status }, { status, headers: NO_STORE });
    }
    const raw: unknown = await res.json();
    const candles = parseBinanceKlines(raw);
    return Response.json({ symbol, interval, candles }, { headers: NO_STORE });
  } catch (e) {
    const msg = e instanceof Error ? e.name : "fetch_error";
    console.error("klines fetch failed:", symbol, interval, msg);
    return Response.json({ error: "upstream_unavailable" }, { status: 502, headers: NO_STORE });
  }
}
