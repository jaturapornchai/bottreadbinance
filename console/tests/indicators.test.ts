import assert from "node:assert/strict";
import { test } from "node:test";
import {
  bkkCrosshairTime,
  bkkTickLabel,
  calcEma,
  calcPricePrecision,
  calcRsi,
  calcSma,
  clampLimit,
  coinFromSymbol,
  extractEntryPrice,
  formatChartPrice,
  formatVolume,
  normalizeSymbol,
  parseBinanceKlines,
  parseChartPrice,
  parseInterval,
  toUsdtSymbol,
} from "../src/lib/indicators.ts";

test("indicators: SMA and EMA match standard TradingView seeding", () => {
  const closes = [10, 12, 14, 16, 18, 20];
  const sma3 = calcSma(closes, 3);
  assert.deepEqual(sma3, [null, null, 12, 14, 16, 18]);

  // EMA(3): seed = SMA([10,12,14]) = 12, k = 2/(3+1) = 0.5
  // i=3: 16*0.5 + 12*0.5 = 14
  // i=4: 18*0.5 + 14*0.5 = 16
  // i=5: 20*0.5 + 16*0.5 = 18
  const ema3 = calcEma(closes, 3);
  assert.deepEqual(ema3, [null, null, 12, 14, 16, 18]);

  // Non-linear series verifies exponential weighting vs SMA
  const step = [10, 10, 10, 20, 20];
  const emaStep = calcEma(step, 3);
  assert.equal(emaStep[2], 10);
  assert.equal(emaStep[3], 15);
  assert.equal(emaStep[4], 17.5);
  assert.deepEqual(calcEma([1, 2], 5), [null, null]);
});

test("indicators: Wilder RSI(14) matches TradingView ta.rma smoothing", () => {
  // Classic Wilder textbook / reference rising+pullback series
  const closes = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
  ];
  const rsi = calcRsi(closes, 14);
  for (let i = 0; i < 14; i++) assert.equal(rsi[i], null);
  assert.ok(rsi[14] !== null && Math.abs(rsi[14] - 70.464) < 0.05, `expected ~70.46, got ${rsi[14]}`);
  assert.ok(rsi[15] !== null && Math.abs(rsi[15] - 66.25) < 0.1, `expected ~66.25, got ${rsi[15]}`);
  assert.ok(rsi[19] !== null && rsi[19] > 50 && rsi[19] < 65);

  // Purely rising = 100, flat = 50, purely falling = 0
  assert.equal(calcRsi([1, 2, 3, 4, 5], 3)[3], 100);
  assert.equal(calcRsi([5, 5, 5, 5, 5], 3)[3], 50);
  assert.equal(calcRsi([5, 4, 3, 2, 1], 3)[3], 0);
});

test("indicators: adaptive price precision handles BTC to micro-cap coins cleanly", () => {
  assert.deepEqual(calcPricePrecision([60570.8, 60657.1]), { precision: 2, minMove: 0.01 });
  assert.deepEqual(calcPricePrecision([0.2408, 0.2419]), { precision: 4, minMove: 0.0001 });
  assert.deepEqual(calcPricePrecision([0.004895, 0.004912]), { precision: 6, minMove: 0.000001 });
  assert.deepEqual(calcPricePrecision([0.0928855, 0.0933438]), { precision: 7, minMove: 0.0000001 });
  assert.deepEqual(calcPricePrecision([0.00001234]), { precision: 8, minMove: 0.00000001 });
  assert.deepEqual(calcPricePrecision([]), { precision: 2, minMove: 0.01 });

  assert.equal(formatChartPrice(60570.8, 2), "60,570.80");
  assert.equal(formatChartPrice(0.004895, 6), "0.004895");
  assert.equal(formatVolume(1_520_000_000), "1.52B");
  assert.equal(formatVolume(12_340_000), "12.34M");
  assert.equal(formatVolume(45_670), "45.7K");
  assert.equal(parseChartPrice("89,450.20"), 89450.2);
  assert.equal(parseChartPrice("-"), null);
  assert.equal(extractEntryPrice("0.09288551 → 0.09334387"), 0.09288551);
});

test("indicators: klines validation & Binance payload sanitization", () => {
  // Symbol whitelist (incl. Chinese CJK Binance Futures symbols)
  assert.equal(normalizeSymbol("btcusdt"), "BTCUSDT");
  assert.equal(normalizeSymbol(" 1000PEPEUSDT "), "1000PEPEUSDT");
  assert.equal(normalizeSymbol("币安人生USDT"), "币安人生USDT");
  assert.equal(normalizeSymbol("BTC"), null);              // API requires USDT suffix
  assert.equal(normalizeSymbol("BTC/USDT"), null);         // injection/punctuation rejected
  assert.equal(normalizeSymbol("BTCUSDT?foo=1"), null);
  assert.equal(toUsdtSymbol("aria"), "ARIAUSDT");
  assert.equal(toUsdtSymbol("BTCUSDT"), "BTCUSDT");
  assert.equal(coinFromSymbol("ARIAUSDT"), "ARIA");

  // Interval whitelist & limit clamp
  assert.equal(parseInterval(null), "15m");
  assert.equal(parseInterval("1H"), "1h");
  assert.equal(parseInterval("4h"), "4h");
  assert.equal(parseInterval("1d"), "1d");
  assert.equal(parseInterval("5m"), null);
  assert.equal(clampLimit(null), 240);
  assert.equal(clampLimit("10"), 60);
  assert.equal(clampLimit("9999"), 400);
  assert.equal(clampLimit("abc"), 240);

  // Sanitization: drops bad rows, deduplicates by timestamp, sorts ascending
  const raw = [
    [1700000900000, "10.5", "11.0", "10.0", "10.8", "500"],
    [1700000000000, "10.0", "10.6", "9.9", "10.5", "300"],
    [1700000900000, "10.5", "11.2", "10.0", "11.1", "650"], // newer duplicate wins
    [1700001800000, "bad", "12.0", "10.0", "11.0", "100"],  // malformed dropped
    [1700002700000, "11.0", "9.0", "12.0", "10.0", "100"],  // high < low dropped
    "not-an-array",
  ];
  assert.deepEqual(parseBinanceKlines(raw), [
    { time: 1700000000, open: 10, high: 10.6, low: 9.9, close: 10.5, volume: 300 },
    { time: 1700000900, open: 10.5, high: 11.2, low: 10, close: 11.1, volume: 650 },
  ]);

  // Thai UTC+7 axis/crosshair time
  const sec = Math.floor(Date.parse("2026-11-26T06:45:00Z") / 1000);
  assert.equal(bkkCrosshairTime(sec), "26 พ.ย. 13:45");
  assert.equal(bkkTickLabel(sec, false), "13:45");
  assert.equal(bkkTickLabel(sec, true), "26 พ.ย.");
});
