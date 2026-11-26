// node --test: the pieces where a silent mistake costs money or opens a hole (money text, Thai time, PNG path, login lockout).
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import * as M from "../src/lib/money.ts";
import { buildPlan } from "../src/lib/plan.ts";
import { agyMetaById, closable, scoreFor, screenByCoin } from "../src/lib/portfolio.ts";
import { normalizePercentages, redistributeColumnPercentages } from "../src/lib/colwidths.ts";
import { affordMore, readLimits } from "../src/lib/limits.ts";
import { writeClose } from "../src/lib/requests.ts";
import { openPng, pngRel, safePng } from "../src/lib/safepng.ts";
import { bkkHM, fmtDM, logStampMs, parseIso } from "../src/lib/time.ts";

test("money: strings only, no binary float drift", () => {
  assert.equal(M.kpi("0.30000000000000004"), "0.30");
  assert.equal(M.signed("0.1"), "+0.10");
  assert.equal(M.kpi("999.995"), "1,000.00");        // half away from zero at the boundary
  assert.equal(M.kpi("-999.995"), "-1,000.00");
  assert.equal(M.price("1E-7"), "0.0000001");        // Python Decimal exponent form
  assert.equal(M.price("12345678.123456789012345678"), "12,345,678.123456789012345678");
  assert.equal(M.kpiSigned("-0.004"), "0.00");       // rounds to zero: no "-0.00"
  assert.equal(M.kpi(null), "-");
  assert.equal(M.kpi("abc"), "-");
  assert.equal(M.pct("12.345"), "+12.35");           // no grouping on percent
});

test("money: AI cost is exact integer arithmetic", () => {
  // 1,000,000 tokens in + 1,000,000 out of kimi-k2.6 = 0.95 + 4.00 USD
  assert.equal(M.fmtE8(M.costE8("kimi-k2.6", 1_000_000, 1_000_000) ?? 0n, 2), "4.95");
  assert.equal(M.costE8("unknown-model", 1, 1), null);
  assert.equal(M.fmtE8(1n, 8), "0.00000001");
});

test("time: always Asia/Bangkok, Gregorian", () => {
  assert.equal(bkkHM(Date.parse("2026-10-18T17:30:00Z")), "00:30");   // crosses midnight
  assert.equal(fmtDM("2026-10-18T10:05:00Z"), "18/10 17:05");
  assert.equal(parseIso("2026-10-18 10:05:00"), Date.parse("2026-10-18T10:05:00Z"));   // no offset = UTC
  assert.equal(parseIso("not a time"), null);
  // a UTC+7 host writing 08:00 means 01:00 UTC
  assert.equal(logStampMs("2026-10-18", "08:00:00", 420), Date.parse("2026-10-18T01:00:00Z"));
});

test("png: only real PNGs under reports/, never traversal", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "bt-png-"));
  await fs.mkdir(path.join(root, "reports", "charts"), { recursive: true });
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from("data")]);
  await fs.writeFile(path.join(root, "reports", "charts", "a.png"), png);
  await fs.writeFile(path.join(root, "reports", "charts", "fake.png"), "not a png");
  await fs.writeFile(path.join(root, "secret.png"), png);
  try {
    assert.ok(await openPng(root, "reports/charts/a.png"));
    assert.ok(await openPng(root, "D:\\bottreadbinance\\reports\\charts\\a.png"));   // writer's Windows path is re-rooted
    assert.ok(await openPng(root, "/app/reports/charts/a.png"));                      // and so is the team container's
    assert.equal(await openPng(root, "reports/charts/fake.png"), null);               // wrong signature
    assert.equal(await openPng(root, "reports/../secret.png"), null);                 // traversal
    assert.equal(await openPng(root, "reports/charts/../../secret.png"), null);
    assert.equal(await openPng(root, "/etc/passwd"), null);
    assert.equal(await openPng(root, "reports/charts/a.txt"), null);
    assert.equal(await openPng(root, "reports/charts/a.png\0.txt"), null);
    assert.equal(safePng(root, root, "../x.png"), null);
    assert.equal(pngRel("nothing/here.png"), null);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("auth: token verifies, tampering and expiry fail, lockout after 5 wrong tries", async () => {
  process.env.CONSOLE_PASSWORD = "test-only-password";
  process.env.CONSOLE_SECRET = "test-only-secret-0123456789abcdef";
  const A = await import("../src/lib/auth.ts");
  assert.ok(A.passwordOk("test-only-password"));
  assert.ok(!A.passwordOk("test-only-passwor"));
  assert.ok(!A.passwordOk(""));
  const t = A.makeToken(1_000_000);
  assert.ok(A.verifyToken(t, 1_000_100));
  assert.ok(!A.verifyToken(t, 1_000_000 + A.SESSION_S + 1));        // expired
  assert.ok(!A.verifyToken(t.slice(0, -2) + "xx", 1_000_100));      // bad signature
  assert.ok(!A.verifyToken(undefined));
  const ip = "test-ip";
  for (let i = 0; i < 4; i++) assert.equal(A.recordFail(ip, 1_000).lockedS, 0);
  assert.ok(A.recordFail(ip, 1_000).lockedS > 0);                   // 5th wrong try locks
  assert.ok(A.lockedFor(ip, 2_000) > 0);
  assert.equal(A.lockedFor(ip, 1_000 + 301_000), 0);                // 5 min later it is open again
});

test("auth: only a 192.168 socket address on a console opened by its LAN IP skips the login", async () => {
  const A = await import("../src/lib/auth.ts");
  const h = (ip: string | null, host = "192.168.1.10:3300") => ({ get: (n: string) => (n === A.SOCKET_IP ? ip : n === "host" ? host : null) });
  assert.ok(A.fromLan(h("192.168.1.50")));
  assert.ok(A.fromLan(h("::ffff:192.168.1.50", "192.168.1.10")));
  assert.ok(!A.fromLan(h(null)));                                   // no preload = no header
  assert.ok(!A.fromLan(h("127.0.0.1")));                            // a tunnel or a proxy on the same host
  assert.ok(!A.fromLan(h("10.0.0.5")));
  assert.ok(!A.fromLan(h("203.0.113.9")));
  assert.ok(!A.fromLan(h("192.168.1.50, 203.0.113.9")));
  assert.ok(!A.fromLan(h("192.168.1.50", "evil.example:3300")));    // DNS rebinding
  process.env.TRUST_PROXY = "1";
  try { assert.ok(!A.fromLan(h("192.168.1.50"))); } finally { delete process.env.TRUST_PROXY; }
  const req = (ip: string) => new Request("http://192.168.1.10:3300/api/snapshot", { headers: { [A.SOCKET_IP]: ip, host: "192.168.1.10:3300" } });
  assert.equal(A.requireSession(req("192.168.1.50")), null);
  assert.equal(A.requireSession(req("203.0.113.9"))?.status, 401);
});

test("portfolio: screen score shown only for the matching side", () => {
  const m = screenByCoin({ rows: [{ base: "XMR", side: "SHORT", score: -7.9 }] });
  assert.equal(scoreFor(m.get("XMR")!, "SHORT"), "-7.9");
  assert.equal(scoreFor(m.get("XMR")!, "LONG"), "-");
});

test("plan: open_new=false pauses the day note and the new-plan alert; missing = open", () => {
  const base = { run_id: "2026-11-06", entry_day: true, short: ["AAAUSDT"], rows: [{ coin: "AAA" }], watchlist: [] };
  const paused = buildPlan({ ...base, open_new: false }, "k", 0);
  assert.match(paused.day, /หยุดเปิดไม้ใหม่/);
  assert.deepEqual(paused.shortCoins, []);
  const old = buildPlan(base, "k", 0);
  assert.match(old.day, /เปิดไม้ได้/);
  assert.deepEqual(old.shortCoins, ["AAA"]);
  assert.match(old.header, /\(วัน UTC\)/);
});

test("plan: watchlist note uses the real count", () => {
  const w = [{ coin: "A" }, { coin: "B" }, { coin: "C" }];
  assert.match(buildPlan({ rows: [], watchlist: w }, "k", 0).day, /3 เหรียญ/);
  assert.doesNotMatch(buildPlan({ rows: [], watchlist: w }, "k", 0).day, /ช่องเกณฑ์/);
});

test("close button: only an open, clear bot position; the request file name is never a path", async () => {
  const pos = { id: "bto-2026-11-09-CT", on_binance: true, unclear: false };
  assert.equal(closable(pos), true);
  assert.equal(closable({ ...pos, unclear: true }), false);           // an order of unknown outcome: a human checks Binance first
  assert.equal(closable({ ...pos, on_binance: false }), false);       // already gone: the next sync books it
  assert.equal(closable({ ...pos, id: "bto-../../x" }), false);
  assert.equal(closable({ on_binance: true }), false);
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "bt-close-"));
  process.env.BOTTREAD_ROOT = dir;
  assert.deepEqual(await writeClose(pos.id), { written: true });
  assert.deepEqual(await writeClose(pos.id), { written: false, reason: "pending", waitS: 0 });   // pressed twice: one file
  assert.deepEqual(await fs.readdir(path.join(dir, "cache", "requests")), ["close-bto-2026-11-09-CT"]);
  for (const bad of ["bto-../x", "x", "bto-a/b", "bto-" + "a".repeat(33), "bto-a\nb"]) await assert.rejects(writeClose(bad));
  await fs.rm(dir, { recursive: true, force: true });
});

test("resizable columns: percentages always sum to 100% and obey min bounds without overflow", () => {
  const norm = normalizePercentages([7, 8, 13, 15, 15, 10, 10, 7, 8, 7]);
  assert.equal(norm.length, 10);
  const sum = Math.round(norm.reduce((a, b) => a + b, 0) * 100) / 100;
  assert.equal(sum, 100);
  const mins = norm.map(() => 4);
  const grown = redistributeColumnPercentages(norm, 2, 12, mins);
  const sumGrown = Math.round(grown.reduce((a, b) => a + b, 0) * 100) / 100;
  assert.equal(sumGrown, 100);
  assert.ok(grown[2] > norm[2]);
  assert.ok(grown.every((v) => v >= 3.9));
  const shrunk = redistributeColumnPercentages(norm, 2, -50, mins);
  assert.equal(Math.round(shrunk.reduce((a, b) => a + b, 0) * 100) / 100, 100);
  assert.ok(shrunk[2] >= 3.99);
});

test("portfolio: agyMetaById extracts V22 setup, stop/tp, stage and MFE/MAE cleanly", () => {
  const m = agyMetaById({
    "bto-2026-11-25-AA": {
      setup: "L_SQUEEZE_1H",
      exit_mode: "V22_LONG_SQZ96",
      stop_px: "95.5",
      tp_px: "160",
      ratchet_stage: "LOCK8",
      mfe_pct: "0.095",
      mae_pct: "-0.012",
    },
  });
  const hit = m.get("bto-2026-11-25-AA");
  assert.ok(hit);
  assert.equal(hit.setup, "L_SQUEEZE_1H");
  assert.equal(hit.exitMode, "V22_LONG_SQZ96");
  assert.equal(hit.stopPx, "95.5");
  assert.equal(hit.tpPx, "160");
  assert.equal(hit.stage, "LOCK8");
  assert.equal(hit.mfePct, "+9.50%");
  assert.equal(hit.maePct, "-1.20%");
  const cb = M.diffAndPct(M.parseDec("885.50"), M.parseDec("900.00"));
  assert.equal(M.fmtDec(cb.diff, { min: 2, max: 2, plus: true }), "-14.50");
  assert.equal(cb.pctStr, "-1.61%");
  assert.equal(cb.sign, -1);
});

test("limits: max_open 0 = unlimited (null, never 8), Auto CB from heartbeat, afford count is exact", () => {
  const hb = { leverage: 2, margin_usdt: "100", lot_usdt: "200", min_avail_usdt: "101", max_open: 0, max_long_open: 0,
    cb_auto: true, cb_ref_equity_usdt: "916.80", cb_limit_usdt: "68.76", cb_limit_pct: "7.50" };
  const lim = readLimits(hb, null, { baseline_equity_usdt: "900", peak_equity_usdt: "916.80" });
  assert.equal(lim.maxOpen, null);
  assert.equal(lim.maxLong, null);
  assert.equal(lim.leverage, 2);
  assert.equal(lim.lotUsdt, "200.00");
  assert.equal(lim.cbAuto, true);
  assert.equal(lim.cbRefEquity, "916.80");
  assert.equal(lim.cbLimitUsdt, "68.76");
  assert.equal(lim.cbLimitPct, "7.50");
  // heartbeat missing -> last auto round; capped value kept; legacy file without max_open -> 8
  assert.equal(readLimits(null, { max_open: 5 }, null).maxOpen, 5);
  assert.equal(readLimits(null, null, null).maxOpen, 8);
  // Auto CB without an explicit ref -> peak; manual CB -> baseline
  assert.equal(readLimits({ cb_auto: true }, null, { baseline_equity_usdt: "900", peak_equity_usdt: "950" }).cbRefEquity, "950");
  assert.equal(readLimits(null, null, { baseline_equity_usdt: "900", peak_equity_usdt: "950" }).cbRefEquity, "900");
  // floor(available / 101) with BigInt, never float
  assert.equal(affordMore("505.00", "101.00"), 5);
  assert.equal(affordMore("504.99", "101"), 4);
  assert.equal(affordMore("1,010.00", "101.00"), 10);
  assert.equal(affordMore("100.99", "101"), 0);
  assert.equal(affordMore("-5", "101"), 0);
  assert.equal(affordMore(null, "101"), null);
});
