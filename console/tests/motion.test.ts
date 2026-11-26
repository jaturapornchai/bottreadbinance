import assert from "node:assert/strict";
import { test } from "node:test";
import { bkkThaiDate } from "../src/lib/clock.ts";
import { cbUsedPermille, decDir, flapTokens, gateSlots, graphemes, leadNumber, markPart, newRowIds, numericChars, regimePolarity, slotStates } from "../src/lib/motion.ts";

test("motion: tick direction compares decimal strings exactly (no float)", () => {
  assert.equal(decDir("1,234.50", "1,234.51"), 1);
  assert.equal(decDir("+0.10", "-0.05"), -1);
  assert.equal(decDir("0.30", "0.3"), 0);                 // same value, different formatting
  assert.equal(decDir("0.1", "0.10000000000000000001"), 1); // beyond float precision
  assert.equal(decDir("-", "12.00"), 0);                  // unreadable side = no flash
  assert.equal(decDir("5.00", "5.00"), 0);
  assert.equal(markPart("0.1234 → 0.1250"), "0.1250");
  assert.equal(markPart("98,000"), "98,000");
});

test("motion: circuit breaker gauge uses the closer of -45 USDT / -5%", () => {
  assert.equal(cbUsedPermille("+3.20", "+0.35%"), 0);
  assert.equal(cbUsedPermille("-22.50", "-2.00%"), 500);
  assert.equal(cbUsedPermille("-9.00", "-4.00%"), 800);
  assert.equal(cbUsedPermille("-60.00", "-6.00%"), 1000);
  assert.equal(cbUsedPermille("-", "-"), 0);
});

test("motion: regime polarity and slot bar", () => {
  assert.equal(regimePolarity("BULLISH_FAVORABLE"), "bull");
  assert.equal(regimePolarity("BULLISH_SQUEEZE_CAUTION"), "squeeze");
  assert.equal(regimePolarity("BEARISH_STRONG"), "bear");
  assert.equal(regimePolarity("CRASH_CAUTION"), "bear");
  assert.equal(regimePolarity("NEUTRAL_TREND"), "flat");
  assert.equal(regimePolarity(undefined), "flat");
  assert.deepEqual(slotStates(2, 1, 4), ["long", "long", "short", "free"]);
  assert.equal(slotStates(8, 1, 8).length, 9);
});

const flips = (prev: string, next: string) => numericChars(prev, next).filter((x) => x.flip).map((x) => x.text).join("");
const calm = (prev: string, next: string) => numericChars(prev, next).filter((x) => !x.flip).map((x) => x.text).join("");

test("split-flap: decimal-point aligned digit diff (only changed digits flip)", () => {
  assert.equal(flips("0.241", "0.24199518"), "99518");   // 0.241 stays calm
  assert.equal(calm("0.241", "0.24199518"), "0.241");
  assert.equal(flips("895.50", "896.41"), "641");        // 89 . stay calm
  assert.equal(flips("898.64", "899.12"), "912");
  assert.equal(flips("9.95", "10.05"), "100");           // integer right-aligned (1,0 new), fraction left-aligned (0 changed, 5 calm)
  assert.equal(calm("9.95", "10.05"), ".5");
  assert.equal(flips("999.99", "1,000.01"), "1,00001");
  assert.deepEqual(numericChars("0.2408", "0.2419").filter((x) => x.flip), [{ text: "1", flip: true, old: "0" }, { text: "9", flip: true, old: "8" }]);
  assert.equal(flips("12.00", "12.00"), "");
});

test("split-flap: tokens — numbers per digit, words whole, idle = calm", () => {
  const idle = flapTokens("ว่างใช้ได้ 10.50 USDT", "ว่างใช้ได้ 10.50 USDT");
  assert.deepEqual(idle.map((t) => t.kind), ["word", "space", "num", "space", "word"]);
  assert.ok(idle.every((t) => (t.kind === "word" ? !t.flip : t.kind === "num" ? t.chars.every((c) => !c.flip) : true)));
  const num = flapTokens("ว่างใช้ได้ 10.50 USDT", "ว่างใช้ได้ 10.75 USDT")[2];
  assert.ok(num.kind === "num" && num.chars.filter((c) => c.flip).map((c) => c.text).join("") === "75");
  const words = flapTokens("ขาลง (เอื้อ SHORT)", "ขาขึ้น (เอื้อ LONG)").filter((t) => t.kind === "word" && t.flip).map((t) => t.text);
  assert.deepEqual(words, ["ขาขึ้น", "LONG"]);   // "(" and ")" are their own calm tokens
  // bracketed percentage flips per digit, brackets stay calm
  const pnl = flapTokens("-0.29 (-0.29%)", "-0.30 (-0.30%)");
  assert.deepEqual(pnl.map((t) => t.text), ["-0.30", " ", "(", "-0.30%", ")"]);
  assert.deepEqual(pnl.map((t) => t.kind), ["num", "space", "word", "num", "word"]);
  assert.ok(pnl[2].kind === "word" && !pnl[2].flip && pnl[4].kind === "word" && !pnl[4].flip);
  assert.ok(pnl[3].kind === "num" && pnl[3].chars.filter((c) => c.flip).map((c) => c.text).join("") === "30");
  const price = flapTokens("0.2408 → 0.2408", "0.2408 → 0.24199518");
  assert.deepEqual(price.map((t) => t.kind), ["num", "space", "word", "space", "num"]);
  assert.ok(price[0].kind === "num" && price[0].chars.every((c) => !c.flip));   // entry price untouched
  assert.deepEqual(graphemes("ไม่"), ["ไ", "ม่"]);   // Thai tone marks stay with their base letter
  assert.equal(leadNumber("0.12 → 0.13"), "0.13");
  assert.equal(leadNumber("3 / 8"), "3");
  assert.equal(leadNumber("ว่างใช้ได้ 1,234.50 USDT"), "1,234.50");
  assert.equal(leadNumber("-"), "");
});

test("split-flap: brand-new rows are detected only after the first render", () => {
  assert.deepEqual([...newRowIds(null, ["a", "b"])], []);
  assert.deepEqual([...newRowIds(new Set(["a", "b"]), ["c", "a", "b"])], ["c"]);
  assert.deepEqual([...newRowIds(new Set(["a", "b"]), ["b"])], []);
});

test("fids clock: Thai short date, Gregorian year, Asia/Bangkok", () => {
  assert.equal(bkkThaiDate(Date.parse("2026-11-26T00:58:51Z")), "พฤ. 26 พ.ย. 2026");
  assert.equal(bkkThaiDate(Date.parse("2026-12-31T17:30:00Z")), "ศ. 1 ม.ค. 2027");   // UTC 31 Dec 17:30 = BKK 1 Jan 00:30
});

test("2x / unlimited: CB gauge with the live Auto CB limits, dynamic airport gates", () => {
  // Auto CB 2x: -68.76 USDT / -7.50% from peak
  assert.equal(cbUsedPermille("-34.38", "-3.75%", "68.76", "7.50"), 500);
  assert.equal(cbUsedPermille("-10.00", "-6.00%", "68.76", "7.50"), 800);
  assert.equal(cbUsedPermille("-80.00", "-8.00%", "68.76", "7.50"), 1000);
  assert.equal(cbUsedPermille("-1,000.00", "-1.00%", "1,000.00", "50"), 1000);   // grouped limit is read too
  // unlimited: gates in use + affordable
  assert.deepEqual(gateSlots(2, 1, null, 2).map((g) => g.state), ["long", "long", "short", "free", "free"]);
  assert.deepEqual(gateSlots(0, 0, null, 0).map((g) => g.label), ["G1"]);
  const many = gateSlots(9, 3, null, 4);   // 16 gates folded to 10
  assert.equal(many.length, 10);
  assert.deepEqual(many[9], { state: "more", label: "+7" });
  // capped (legacy MAX_OPEN 8) unchanged
  assert.equal(gateSlots(1, 1, 8, 99).length, 8);
});
