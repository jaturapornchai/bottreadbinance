"""BotTrade: a 7-member crypto team (4 bots + 3 DeepSeek API agents) that picks Binance futures shorts (1x); they talk only via Kafka (Redpanda).

  python team.py once         # one round (ClickHouse screen -> AI: risks, charts, summary -> short plan -> server), then exit; the app runs it hourly
  python team.py run          # 24/7: every member in its own thread, restarted if it crashes
  python team.py dca          # ask the team for a short plan now
  python team.py chart SOL    # ask the Chart Analyst to look at one coin
  python team.py close bto-…  # ลุงจืด's ปิดไม้ button in the console (runner.py runs it between rounds)
  python team.py watch        # the exit rule alone, between rounds (runner.py runs it every 15 min)
"""
import contextlib
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, ".env"), encoding="utf-8") as _f:
    for _line in _f:
        if "=" in _line and not _line.lstrip().startswith("#"):
            _k, _v = _line.strip().split("=", 1)
            os.environ.setdefault(_k, _v)

import binance  # noqa: E402
import bus  # noqa: E402  (env must be loaded first)
import chart  # noqa: E402
import market  # noqa: E402
import ollama  # noqa: E402

CACHE, REPORTS = os.path.join(ROOT, "cache"), os.path.join(ROOT, "reports")
MODELS = {  # picked per job: cheap+fast for volume, vision for charts, strongest reasoning only once a day
    "chart": os.environ.get("MODEL_CHART", "deepseek-flash"),      # DeepSeek-V4.1-Flash-1017, reads images
    "risk": os.environ.get("MODEL_RISK", "deepseek-flash"),
    "cio": os.environ.get("MODEL_CIO", "deepseek-v4-pro"),       # DeepSeek-V4-Pro-0813
}
MODELS_TH = " · ".join(f"{k}={v}" for k, v in MODELS.items())   # the chat shows "chart=… · risk=… · cio=…", not a Python dict
DCA_AT_UTC = os.environ.get("DCA_AT_UTC", "00:30")      # 07:30 Bangkok, after the daily candle closes
MAX_CHARTS = 5   # the plan names every passing coin (the buyer opens until the money runs out); the AI reads only the first 5 charts
TH = timezone(timedelta(hours=7))
os.makedirs(CACHE, exist_ok=True)
os.makedirs(REPORTS, exist_ok=True)
logging.basicConfig(filename=os.path.join(CACHE, "team.log"), level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
log = logging.getLogger("team")
# a crash in the background runner has no one watching stderr, so it goes to team.log (the console lists ERROR lines)
sys.excepthook = lambda *e: log.error("team.py died", exc_info=e)
threading.excepthook = lambda a: log.error("%s died", a.thread.name if a.thread else "thread",
                                           exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def now():
    return datetime.now(timezone.utc)


def say(who, text):
    """Team chat: every member narrates what it did, the Secretary writes it to reports/team-chat/."""
    bus.send("bt.team.chat", {"at": now().isoformat(), "who": who, "text": text})
    log.info("%s: %s", who, text)


def cached(fn, ttl):
    box = {"t": 0, "v": None}

    def get():
        if time.time() - box["t"] > ttl:
            box["v"], box["t"] = fn(), time.time()
        return box["v"]
    return get


SHORT_COOLDOWN_DAYS = 1      # ลุงจืด 2026-10-26: a coin whose last bot close lost money rests 1 day (CT was reopened 2 h after -10%)
SHORT_REST_HOURS = 4         # ลุงจืด 2026-11-09 "พักเหรียญ แค่ 4 ชม.": every last close (profit, loss, by hand) rests 4 h too
REST_TH = f"ทุกไม้ {SHORT_REST_HOURS} ชม. · ปิดขาดทุน {SHORT_COOLDOWN_DAYS} วัน"
LEVERAGE = 1                 # 1x isolated only (server MAX_LEVERAGE = 1)
OPEN_NEW = True              # ลุงจืด 2026-11-14 "1 ทำเลย เงินจริง": 1d | LOWVOL_BREAKDOWN | H_DELTA0 (BTC_001_MATCHED)
BTC_HEDGE_ON = not market.LV_SHORT_ONLY   # False since ลุงจืด 2026-11-22 "เล่นเฉพาะ short"; True = H_DELTA0: every alt SHORT opens paired 1:1 with a BTCUSDT LONG hedge leg
LOWVOL_EXIT_ON = True        # Low-Vol Breakdown exit on market.LV_TF bars: 3xATR20 stop [10%..35%], -30% TP, a bar closing +15% -> +4% ratchet,
                             # max hold 168 h ("1h") or 21 days ("1d")
BTC_UNIT_QTY = Decimal("0.001")     # Binance BTCUSDT Futures minQty / stepSize
MIN_BTC_LEG_USDT = Decimal("52")    # >= $50 Binance BTCUSDT MIN_NOTIONAL + tick cushion
MAX_PAIRS = 6                # max concurrent (Alt SHORT + BTC LONG) pairs on ~$864 wallet (~$134/pair = ~$804 total)
PAUSED_TH = "⛔ หยุดเปิดไม้ใหม่ทั้ง LONG/SHORT (ลุงจืดสั่ง 2026-11-05) ไม้ที่ถือยังตรวจกติกาออกทุกรอบ"
MARGIN_USDT = Decimal("100") # server per-leg cap (SHORT_LOT_USDT = 100); paired legs size dynamically from 0.001 BTC (~$67 <= $100)
MAX_OPEN = 8                 # ลุงจืด 2026-11-22 "8 ไม้": SHORT-only holds at most 8 positions at once (his own counted)
LOT_USDT = MARGIN_USDT * LEVERAGE
FEE = Decimal("0.0005")      # Binance USDⓈ-M taker (VIP0)
MAX_PLAN, MAX_CLOSE = 20, 50
# server /open refusals that hold for every coin (binance.gate/_open_one): the rest of the plan would only be refused too
ACCOUNT_WIDE = ("live_off", "setup", "old_run", "cap", "daily_cap", "hedge", "multi", "unclear")


def cached_portfolio():
    """The app's cache/portfolio.json (refreshed after every round and every 5 min), or None."""
    with contextlib.suppress(OSError, ValueError), open(binance.PORTFOLIO, encoding="utf-8") as f:
        return json.load(f)
    return None


def held_shorts(p=None):
    """Symbols with a position on Binance now (the bot's and ลุงจืด's own: never a second one on a coin), from `p` (a
    fresh /portfolio) or else the app's cache. None = unknown (no cache, or its last refresh failed)."""
    p = cached_portfolio() if p is None else p
    with contextlib.suppress(KeyError, TypeError, AttributeError):
        return None if "error" in p else {x["symbol"] for x in p["shorts"]} | set(p.get("held_symbols") or ())
    return None


def cooldown_shorts(t=None, p=None):
    """Symbols whose LAST close was less than SHORT_REST_HOURS ago, or lost money (the unrounded net pnl < 0) less than
    SHORT_COOLDOWN_DAYS ago, from `p`
    (a fresh /portfolio: what the buyer opens on) or else the app's cache (the quant's preview)."""
    if p is None:
        with contextlib.suppress(KeyError, TypeError, ValueError, AttributeError):
            return cooldown_shorts(t, cached_portfolio() or {})
        return set()
    t = t or now()
    last = {}
    for x in sorted(p.get("closed") or [], key=lambda x: datetime.fromisoformat(x["closed_at"])):
        last[x["symbol"]] = x   # oldest first: a coin's newest close wins
    rest, loss = t - timedelta(hours=SHORT_REST_HOURS), t - timedelta(days=SHORT_COOLDOWN_DAYS)
    return {sym for sym, x in last.items()
            if (at := datetime.fromisoformat(x["closed_at"])) > rest
            or (binance.dec(x.get("pnl") or x.get("pnl_usdt") or "0") < 0 and at > loss)}


# the rule the bot trades: entry = market.short_screen (research/long_short_trend_backtest.py config 4 + 3 filters ลุงจืด kept),
# exits = the backtest's exit rule (rule_exit), checked every round
TF1H = market.LV_TF == "1h"   # ลุงจืด 2026-11-22 "สัญญาณแท่ง 1 ชม.": the breakdown and the exit levels are read on 1h bars, entries any hour
SHORT_RULE_TH = (
    ("กลยุทธ์ 1D Low-Vol Support Breakdown + BTC Hedge 1:1 (1d | LOWVOL_BREAKDOWN | H_DELTA0 · ผ่านเกณฑ์สถิติ TEST p=0.0086): " if BTC_HEDGE_ON else
     "กลยุทธ์ 1H Low-Vol Support Breakdown SHORT ล้วน ไม่มีขา BTC LONG เปิดได้ทุกชั่วโมง (1h | LOWVOL_BREAKDOWN | G_BREADTH · ผลย้อนหลัง TEST -0.96%/เดือน p=0.8277 ไม่ผ่านเกณฑ์สถิติ): " if TF1H else
     "กลยุทธ์ 1D Low-Vol Support Breakdown SHORT ล้วน ไม่มีขา BTC LONG (1d | LOWVOL_BREAKDOWN | G_BREADTH · ผลย้อนหลัง TEST +1.01%/เดือน p=0.1151 ไม่ผ่านเกณฑ์สถิติ): ")
    + f"สแกนสัญญา USDT perpetual อายุ ≥ {market.LV_MIN_AGE_DAYS} วัน "
    + (f"(มีแท่ง 1 ชม. ≥ {market.LV_MIN_BARS_1H} แท่ง)" if TF1H else f"(มีแท่ง Day ต่อเนื่อง ≥ {market.LV_MIN_BARS_1D} วัน)") + " ไม่ใช่ stablecoin/ทอง/ดัชนี · "
    f"volume เฉลี่ย 7 วัน ≥ ${market.LV_MIN_VOL_1H * 24 / 1e6:.1f}M/วัน · กรองกันโดนบีบ (Anti-Squeeze" + (" จากแท่ง Day ของเมื่อวาน" if TF1H else "") + "): ความผันผวนรายวัน 14 วันอยู่ใน 2 ใน 3 ล่างของตลาด, "
    f"7 วันไม่มีแท่งพุ่งเกิน +{market.LV_MAX_DR7 * 100:.0f}%, funding 24 ชม. ≥ {market.LV_MIN_F24 * 100:+.2f}% และ 7 วัน ≥ {market.LV_MIN_F7D * 100:+.2f}% · "
    + ("สัญญาณเข้าเมื่อปิดแท่ง 1 ชม. (เปิดได้ทุกชั่วโมงตลอดวัน): โครงสร้างขาลงของแท่ง 1 ชม. (Close < SMA50, EMA20 < SMA50) และราคาปิดเมื่อวานอยู่ใต้ SMA20 ของแท่ง Day, " if TF1H else
       "สัญญาณเข้าเมื่อปิดแท่ง Day (เปิดเฉพาะช่วง 00:00–01:59 UTC / 07:00–08:59 น. เวลาไทย): โครงสร้างขาลง (Close < SMA50, EMA20 < SMA50, Close < SMA20), ")
    + f"อ่อนแอเทียบ BTC 14 วัน ≤ {market.LV_MAX_REL14 * 100:+.0f}%, เพิ่งหลุดแนวรับ Low {'20 แท่ง 1 ชม.' if TF1H else '20 วัน'}เป็นแท่งแรก และแท่งที่หลุดลงไม่เกิน {market.LV_MAX_DROP_BAR * 100:.0f}% (เรียงลำดับเหรียญที่อ่อนกว่า BTC มากสุดก่อน) · "
    + ((f"เปิดคู่ Market-Neutral 1:1 ({LEVERAGE}x isolated ทั้งสองขา): เปิด LONG BTCUSDT ทีละ 0.001 BTC คู่กับ SHORT เหรียญเป้าหมายในมูลค่า USDT เท่ากัน (ไม่เกินเพดาน {MARGIN_USDT} USDT/ขา ถือพร้อมกันไม่เกิน {MAX_PAIRS} คู่) "
        f"ถ้าเปิดขา SHORT ไม่สำเร็จ ระบบปิดคืนขา BTC LONG ทันที · ") if BTC_HEDGE_ON else
       (f"ตัวกรองตลาด ({'แท่ง Day ของเมื่อวาน' if TF1H else 'วันของแท่งสัญญาณ'}): Base Universe ≥ {market.LV_GATE_MIN_BASE} ตัว, เหรียญที่ยืนเหนือ SMA20 ของตัวเอง < {market.LV_GATE_MAX_BREADTH * 100:.0f}% "
        f"และ BTC 7 วัน < +{market.LV_GATE_MAX_BTC_RET7 * 100:.0f}% ไม่ผ่าน = ไม่เปิดไม้วันนั้น · "
        f"เปิด SHORT เหรียญเป้าหมายอย่างเดียว ไม้ละ {LOT_USDT} USDT ({LEVERAGE}x isolated) ถือพร้อมกันไม่เกิน {MAX_OPEN} ไม้ · "))
    + f"พักเหรียญหลังปิด ({REST_TH}) · "
    f"กติกาออก (ตรวจทุก 15 นาทีด้วย watch + รอบรายชั่วโมง): ขา SHORT ตั้ง stop เริ่มต้น 3×ATR20{' ของแท่ง 1 ชม.' if TF1H else ''} (10%–35% เหนือราคาเข้า), TP เมื่อราคาลง -30%, "
    f"ถ้ามีแท่ง {'1 ชม.' if TF1H else 'Day'} ปิดกำไร ≥ +15% เลื่อน stop ลงมาล็อกกำไรที่ -4% จากราคาเข้า (+4%), "
    + (f"ถือไม่เกิน {market.LV_MAX_HOLD_HOURS} ชม. (7 วัน)" if TF1H else f"ถือไม่เกิน {market.LV_MAX_HOLD_DAYS} วัน")
    + (" และเมื่อขา SHORT ปิด ระบบปิดขา BTCUSDT LONG คู่กันพร้อมกันทันที" if BTC_HEDGE_ON else "")
)
NO_PICKS_TH = "รอบนี้ไม่มีเหรียญเข้ากฎ (LONG/SHORT) ที่ยังไม่ได้ถืออยู่ — ไม่เปิดไม้ใหม่ ส่วนไม้ที่เปิดอยู่ โค้ดตรวจกติกาออกทุกรอบ"


btc_days = cached(lambda: market.load_daily("BTCUSDT")["BTCUSDT"], 3600)


# ======================= BOTS (no LLM) =======================

def dca_due(t, asked):
    """Run id to request now, or None. A day is done only once its dca-plan.md exists."""
    today = t.strftime("%Y-%m-%d")
    if t.strftime("%H:%M") >= DCA_AT_UTC and today not in asked and not os.path.exists(os.path.join(REPORTS, today, "dca-plan.md")):
        return today
    return None


def clock():
    """Bot 5 นาฬิกา: once a day after the daily candle closes, asks the team for a short plan.
    'Done' = the plan file exists, so a restart mid-run (CIO keeps its wait list in memory) asks again."""
    asked = set()
    while True:
        today = dca_due(now(), asked)
        if today:
            asked.add(today)
            bus.send("bt.dca.request", {"run_id": today, "by": "นาฬิกา"})
            say("นาฬิกา", f"[{today}] ได้เวลาทำแผนเทรดตามเทรนด์ (LONG/SHORT) → ส่งงานให้ควอนต์")
        time.sleep(60)


def quant():
    """Bot 3 ควอนต์: Binance USDⓈ-M perps x ClickHouse futures candles -> Bi-Directional Trend rule -> candidates (code decides)."""
    def handle(msgs):
        for _, req in msgs:
            run = req["run_id"]
            say("ควอนต์", f"[{run}] สแกนทุกสัญญา USDT perpetual บน Binance Futures ตามเทรนด์ตลาด + เทรนด์เหรียญ (LONG & SHORT)")
            rows, meta = market.short_screen()
            while meta["late"] and not meta["entry_day"] and meta.get("waiting_ch", True):   # wait up to HH:15 ("1d": only in the entry window)
                time.sleep(30)                               # into ClickHouse over ~6 min (until HH:15 at most, see short_screen)
                rows, meta = market.short_screen()
            if not OPEN_NEW and meta.get("mkt_trend"):
                meta["mkt_trend"] = meta["mkt_trend"].split(" — ")[0].replace(" (เน้นเปิด SHORT)", "") + " — " + PAUSED_TH
            for r in rows:
                if not binance.PLAN_SYMBOL.fullmatch(r["symbol"]):
                    r["fail"].append("ชื่อสัญญาใช้เป็น order id บน Binance ไม่ได้")
            held = held_shorts()
            cooled = cooldown_shorts()
            passing = [r for r in rows if not r["fail"]]   # highest trend score first
            picks = [r for r in passing if r["symbol"] not in (held or ()) and r["symbol"] not in cooled]
            out_dir = os.path.join(REPORTS, run)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "screen.json"), "w", encoding="utf-8") as f:
                json.dump({"rows": rows, **meta}, f, ensure_ascii=False, indent=1, default=str)
            bus.send("bt.dca.candidates", {"run_id": run, "picks": picks, "universe": len(rows),
                                           "passing": len(passing), "passing_coins": [r["base"] for r in passing],
                                           "held": None if held is None else sorted(binance.coin(x) for x in held),
                                           "cooled": sorted(binance.coin(x) for x in cooled), **meta})
            when = ("" if meta["entry_day"] or not meta.get("allow_short", True) else
                    f" · ข้อมูล {meta['data_to']} UTC ใน ClickHouse ยังไม่ครบ รอรอบหน้า" if market.LV_TF == "1h" or meta.get("waiting_ch", bool(meta.get("late"))) else
                    " · นอกเวลาเปิดไม้รอบปิดแท่ง Day (07:00–08:59 น.) บันทึกเฝ้าดู")
            cd_txt = f" · พักเหรียญเพิ่งปิด ({REST_TH}): {', '.join(sorted(binance.coin(x) for x in cooled))}" if cooled else ""
            mkt_txt = f" · {meta['mkt_trend']}" if meta.get("mkt_trend") else ""
            say("ควอนต์", f"[{run}] สัญญา {len(rows)} ตัว เข้ากฎ {len(passing)} ตัว ({', '.join(r['base'] + '(' + r.get('side', 'SHORT') + ')' for r in passing) or '-'}){mkt_txt}{cd_txt}{when}"
                f" → ส่ง {', '.join(p['base'] for p in picks) or 'ไม่มี'} ให้ AI วิเคราะห์")

    bus.listen("quant", ["bt.dca.request"], handle)


def secretary():
    """Bot 4 เลขา: writes team chat / short plan to reports/ (the console shows its own notices: no desktop notification)."""
    def handle(msgs):
        for topic, m in msgs:
            if topic == "bt.team.chat":
                t = datetime.fromisoformat(m["at"]).astimezone(TH)
                append(os.path.join(REPORTS, "team-chat", f"{t:%Y-%m-%d}.md"), f"- {t:%H:%M} **{m['who']}**: {m['text']}")
            elif topic == "bt.dca.plan":
                path = write_plan(m)
                say("เลขา", f"[{m['run_id']}] บันทึกแผนเทรด LONG/SHORT แล้วที่ {os.path.relpath(path, ROOT)}")

    bus.listen("secretary", ["bt.team.chat", "bt.dca.plan"], handle, batch=50)


def append(path, line):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ======================= AGENTS (DeepSeek API) =======================

def chart_data(coin):
    """{symbol, ind, png, source} of a coin's ClickHouse daily chart, or None when ClickHouse has no live pair for it."""
    sym = coin + "USDT"
    days = []
    if binance.PLAN_SYMBOL.fullmatch(sym):   # the bot shorts perps (most have no spot pair): their own candles first
        d, c, _ = market.load_perp_daily([sym])[0].get(sym, ([], [], []))
        a = market.segment_start(d, c)
        days = [(x.isoformat(), y) for x, y in zip(d[a:], c[a:])] or market.load_daily(sym).get(sym) or []
    if not days or not days[-1][0] >= (now() - timedelta(days=3)).strftime("%Y-%m-%d"):
        return None
    ind = {k: v for k, v in market.indicators(days, btc_days()).items() if v is not None}   # only what the data has
    ind["days_of_data"] = len(days)
    png, source = chart.snapshot(sym, os.path.join(REPORTS, "charts"), days)
    return {"symbol": sym, "ind": ind, "png": png, "source": source}


def chartist():
    """Agent 2 ช่างกราฟ (vision): our ClickHouse daily-close chart + indicators -> trend and a note without numbers."""
    def handle(msgs):
        for _, req in msgs:
            coin = str(req.get("coin", "")).upper()
            d = chart_data(coin)
            if not d:
                bus.send("bt.chart.result", {**req, "ok": False, "why": f"ไม่มี {coin}USDT ที่ยังเทรดอยู่ใน ClickHouse"})
                say("ช่างกราฟ", f"{coin}: ไม่มี {coin}USDT ที่ยังเทรดบน Binance (spot หรือ futures) ข้าม")
                continue
            try:
                reading = chart.read(MODELS["chart"], d["symbol"], d["ind"], d["png"], d["source"])
            except Exception:  # noqa: BLE001 — no reading beats a made-up one; the CIO stops waiting for this chart
                log.exception("chartist: %s", d["symbol"])
                bus.send("bt.chart.result", {**req, "ok": False, "why": "AI อ่านกราฟไม่สำเร็จ", "png": d["png"]})
                say("ช่างกราฟ", f"{d['symbol']}: AI อ่านกราฟไม่สำเร็จ ข้าม")
                continue
            bus.send("bt.chart.result", {**req, "ok": True, **d, "chart": reading})
            say("ช่างกราฟ", f"{d['symbol']}: แนวโน้ม {reading['trend']}" + (f" — {reading['note_th']}" if reading["note_th"] else ""))

    bus.listen("chartist", ["bt.chart.request"], handle)


def facts(p):
    """A pick's numbers exactly as the plan table shows them. The AI agents get these strings, so every number they
    write can be checked against them (ollama.ungrounded)."""
    rsi = f"{p['rsi_prev']:.1f} → {p['rsi']:.1f}" if p.get("rsi_prev") is not None and p.get("rsi") is not None else "-"
    rsi4 = f"{p['rsi4_prev']:.1f} → {p['rsi4']:.1f}" if p.get("rsi4_prev") is not None and p.get("rsi4") is not None else "-"
    score = f"{p['score']:.1f}" if p.get("score") is not None else "-"
    sig = p.get("signal") or "RSI<70 (Overbought)"
    side = p.get("side") or ("LONG" if "LONG" in str(sig) else "SHORT")
    return {"coin": p["base"], "side": side, "signal": sig, "score": score, "price": f"{p['price']:.6g}",
            "rsi_1h": rsi, "rsi_4h": rsi4, "age_days": p["age_days"], "trend_1d": p.get("trend_1d") or "-",
            "dd_ath": pct(p.get("dd_ath")), "vol30": f"${p['vol30_usd'] / 1e6:,.1f}M", "r7": pct(p["r7"])}


def grounded_reviews(reviews, coins, shared):
    """The AI's risk lines whose numbers all come from that coin's own figures or the shared text; known coins only, once."""
    out, cut = {}, 0
    for r in reviews:
        coin = str(r.get("coin", "")).upper()
        if coin not in coins or coin in out:
            continue
        lines = [x for x in r.get("risks_th", []) if isinstance(x, str) and x.strip()]
        keep = [x for x in lines if not ollama.ungrounded(x, json.dumps(coins[coin], ensure_ascii=False) + shared)]
        cut += len(lines) - len(keep)
        if keep:
            out[coin] = {"coin": coin, "risks_th": keep}
    return list(out.values()), cut


RISK_SCHEMA = {"type": "object", "required": ["reviews"], "properties": {"reviews": {"type": "array", "items": {
    "type": "object", "required": ["coin", "risks_th"],
    "properties": {"coin": {"type": "string"}, "risks_th": {"type": "array", "items": {"type": "string"}}}}}}}
RISK_SYSTEM = """คุณคือผู้คุมความเสี่ยงของทีม BotTrade หน้าที่คือชี้ว่าแต่ละเหรียญที่เข้าแผนเทรดตามเทรนด์ (side = LONG ซื้อขึ้น หรือ SHORT ขายลง) อาจพลาดตรงไหน
- ข้อมูลทั้งหมดโค้ดคำนวณจาก ClickHouse ถึงเวลา data_to (UTC): side = ฝั่งที่เปิด (LONG หรือ SHORT), score = คะแนนความแข็งแกร่งของเทรนด์, price = ราคาปิดแท่งล่าสุด (USDT), rsi_1h = RSI(14) แท่ง 1 ชม. (แท่งก่อน → แท่งล่าสุด), rsi_4h = RSI(14) แท่ง 4 ชม. (แท่งก่อน → แท่งล่าสุด), age_days = จำนวนวันตั้งแต่สัญญาเข้า Binance Futures, trend_1d = แนวโน้มกราฟรายวันเทียบ SMA7/20, dd_ath = ราคาห่างจากยอดสูงสุดเดิม (ATH), vol30 = มูลค่าซื้อขายต่อวันเฉลี่ยเรขาคณิตไม่เกิน 30 วัน, r7 = ราคาเปลี่ยนใน 7 วัน, rule = กฎของทีม
- ใช้เฉพาะข้อมูลนี้ ห้ามแต่งหรือเดา ห้ามอ้างข่าว funding หรือเหตุการณ์ที่ไม่มีใน input
- ตัวเลขที่อ้างต้องคัดลอกตรงตัวจาก input ห้ามปัด ห้ามคำนวณใหม่ (ข้อที่มีตัวเลขไม่ตรง input จะถูกตัดทิ้ง)
- risks_th: ความเสี่ยงที่เจาะจงของแต่ละเหรียญ 1-3 ข้อ (เช่น สำหรับ SHORT ราคาอาจพุ่งสวนทาง, สำหรับ LONG ราคาใกล้ยอดอาจเกิดแรงเทขายทำกำไร/false breakout, volume บางราคากระชากง่าย, อายุน้อยข้อมูลน้อย)
- คุณไม่มีสิทธิ์ตัดเหรียญ (โค้ดคัดตามกฎที่ลุงจืดกำหนด) ความเห็นของคุณแสดงให้ลุงจืดอ่าน"""


def risk_critic():
    """Agent 3 ผู้คุมความเสี่ยง: points out where each trade could go wrong. Shown to ลุงจืด, never drops a coin.
    An AI failure means no comments this round, never a missing plan."""
    def handle(msgs):
        for _, c in msgs:
            coins = {p["base"]: facts(p) for p in c["picks"]}
            reviews, note = [], ""
            if coins:
                try:
                    got = ollama.chat(MODELS["risk"], RISK_SYSTEM, json.dumps(
                        {"data_to": c["data_to"], "rule": SHORT_RULE_TH, "coins": list(coins.values())}, ensure_ascii=False),
                        RISK_SCHEMA, max_wait_s=600)["reviews"]
                    reviews, cut = grounded_reviews(got, coins, SHORT_RULE_TH)
                    note = f" · ตัดความเห็น {cut} ข้อที่มีตัวเลขไม่ตรงข้อมูล" if cut else ""
                except Exception:  # noqa: BLE001
                    log.exception("risk: AI review failed")
                    note = " · AI ตอบไม่สำเร็จ แผนไปต่อโดยไม่มีความเห็นนี้"
            bus.send("bt.dca.review", {**c, "reviews": reviews})
            say("ผู้คุมความเสี่ยง", f"[{c['run_id']}] ตรวจ {len(coins)} เหรียญ{note}"
                + "".join(f" · {r['coin']}: {r['risks_th'][0]}" for r in reviews[:3]))

    bus.listen("risk", ["bt.dca.candidates"], handle)


CIO_SYSTEM = """คุณคือหัวหน้าพอร์ตของทีม BotTrade เขียนสรุปแผนเทรดตามเทรนด์ (SHORT อย่างเดียว บน Binance Futures 1x isolated) ภาษาไทยให้ลุงจืด (ผู้ใช้) อ่าน ระบบนี้ใช้เงินจริง
- ใช้เฉพาะข้อมูลใน input (โค้ดคำนวณจาก ClickHouse) ห้ามแต่ง ห้ามเดา ห้ามอ้างข่าว funding หรือข้อมูลที่ไม่มีใน input
- ตัวเลขทุกตัวต้องคัดลอกตรงตัวจาก input ห้ามปัด ห้ามคำนวณใหม่ (ระบบตรวจ: มีตัวเลขไม่ตรง input = สรุปนี้ถูกแทนด้วยข้อความจากโค้ด)
- ห้ามเปลี่ยนรายชื่อเหรียญหรือลำดับ (โค้ดคัดตามกฎที่ลุงจืดกำหนด)
- อธิบาย: เทรนด์ตลาดรวมตอนนี้เป็นอย่างไร (ถ้ามีใน input), ทำไมเหรียญเหล่านี้เข้ากฎฝั่ง SHORT (อ้างตัวเลขใน coins), กราฟที่ช่างกราฟอ่าน (charts) บอกอะไร
- ความเสี่ยงที่ผู้คุมความเสี่ยงยกมา (risk_reviews)
- data_to = เวลาปิดของแท่งล่าสุดที่ใช้ (UTC)
- open_new=false = ลุงจืดสั่งหยุดเปิดไม้ใหม่ทั้ง LONG/SHORT: ห้ามเขียนว่าจะเปิดไม้ เหรียญเหล่านี้คือรายการเฝ้าดู ไม้ที่ถือยังออกตามกติกา
- open_today=false = รอบนี้ยังไม่เปิดไม้ใหม่ (ข้อมูล ClickHouse ยังไม่ครบ หรืออยู่นอกช่วงเวลาเปิดไม้ของกฎ): เหรียญเหล่านี้คือรายการเฝ้าดู รอเปิดรอบเช้าถัดไปถ้ายังเข้ากฎ
- ตรงไปตรงมา ไม่อวย ถ้าข้อมูลบอกว่าเสี่ยง ให้พูดตรงๆ
- ปิดท้ายด้วย 1 บรรทัด: นี่คือผลคัดกรองจากข้อมูลย้อนหลัง ไม่ใช่คำแนะนำการลงทุน
- markdown ไม่เกิน 300 คำ ไม่ต้องใส่ตาราง (โค้ดใส่ให้) ใช้ - แทนรายการแบบมีเลข"""


def summarize(r, picks, short, cut, charts):
    """CIO narrative. Every number must be copied from the code-made facts (one retry naming the offenders), else the
    code's own summary: an AI failure or a made-up figure never blocks or decorates a real-money plan."""
    opens = short if OPEN_NEW else []   # paused: the buyer opens nothing (trade()), so the AI must not say it will
    data = {"data_to": r["data_to"], "open_today": r["entry_day"], "open_new": OPEN_NEW, "mkt_trend": r.get("mkt_trend") or "",
            "coins_in_order": [x["base"] for x in picks],
            "count": len(picks), "opens_today": [binance.coin(x) for x in opens], "opens_count": len(opens),
            "over_plan_limit": [binance.coin(x) for x in cut], "coins": [facts(x) for x in picks],
            "universe": r["universe"], "passing": r["passing"], "rule": SHORT_RULE_TH}
    source = json.dumps(data, ensure_ascii=False)
    user = json.dumps({**data, "charts": {k: v["chart"] for k, v in charts.items() if v["chart"]},
                       "risk_reviews": r["reviews"]}, ensure_ascii=False)
    try:
        for _ in range(2):
            text = ollama.chat(MODELS["cio"], CIO_SYSTEM, user, max_wait_s=600)
            bad = ollama.ungrounded(text, source)
            if not bad:
                return text
            log.warning("cio: numbers not in the data %s", bad)
            user += f"\n\nร่างก่อนหน้ามีตัวเลขที่ไม่อยู่ใน input: {', '.join(bad)} — เขียนใหม่ ใช้ตัวเลขที่คัดลอกตรงตัวจาก input เท่านั้น"
        why = f"มีตัวเลขที่ไม่อยู่ในข้อมูล: {', '.join(bad[:5])}"
    except Exception:  # noqa: BLE001
        log.exception("cio: AI summary failed")
        why = "เรียก AI ไม่สำเร็จ"
    ch_ready = str(r.get("data_to", "")).endswith(("23:59", "24:00"))
    wait_th = "รอบนี้ยังไม่เปิดไม้ใหม่ (อยู่นอกช่วงเปิดไม้ 07:00–08:59 น. เวลาไทย)" if ch_ready else "รอบนี้ยังไม่เปิดไม้ใหม่ (ข้อมูลใน ClickHouse ยังไม่ครบ)"
    return (f"AI สรุปไม่ผ่านการตรวจ ({why}) จึงไม่แสดง — ตัวเลขทั้งหมดดูตาราง (โค้ดคำนวณจาก ClickHouse) · "
            f"เข้ากฎ {len(picks)} เหรียญ: {', '.join(x['base'] for x in picks)} · "
            + (PAUSED_TH if not OPEN_NEW else "วันนี้ server เปิด position ตามลำดับในตาราง" if short else wait_th))


def cio():
    """Agent 4 หัวหน้าพอร์ต: asks the Chart Analyst about the first picks, then writes the trade plan."""
    pending = {}

    def handle(msgs):
        for topic, m in msgs:
            if topic == "bt.dca.review":
                keep = m["picks"]
                pending[m["run_id"]] = {"review": m, "keep": keep, "charts": {}, "t": time.time()}
                for p in keep[:MAX_CHARTS]:
                    bus.send("bt.chart.request", {"coin": p["base"], "purpose": "dca", "run_id": m["run_id"]})
                say("หัวหน้าพอร์ต", f"[{m['run_id']}] เข้ากฎ {len(keep)} เหรียญ ({', '.join(p['base'] for p in keep)}) · ขอช่างกราฟ (AI) อ่านกราฟ ClickHouse {min(len(keep), MAX_CHARTS)} ตัวแรกก่อนสรุป" if keep
                    else f"[{m['run_id']}] (โค้ด ไม่ได้เรียก AI) ไม่มีเหรียญเข้ากฎที่ยังไม่ได้ถือ/ไม่ติดพักรอบนี้ → สรุปแผนเลย")
            elif topic == "bt.chart.result" and m.get("purpose") == "dca" and m.get("run_id") in pending:
                pending[m["run_id"]]["charts"][m["coin"]] = m
        for run, p in list(pending.items()):
            if len(p["charts"]) >= len(p["keep"][:MAX_CHARTS]) or time.time() - p["t"] > 1200:  # don't wait forever for a chart
                try:
                    finish(run, p)
                    del pending[run]
                except Exception:  # noqa: BLE001 — keep the run: the next tick tries again (the bus retry can't, it's popped)
                    log.exception("cio: plan %s failed", run)
                    p["tries"] = p.get("tries", 0) + 1
                    if p["tries"] < 3:
                        time.sleep(20)
                        continue
                    del pending[run]
                    bus.dropped.add("cio")
                    say("หัวหน้าพอร์ต", f"[{run}] เขียนแผนไม่สำเร็จ 3 ครั้ง ยกเลิกแผนรอบนี้")

    def finish(run, p):
        r = p["review"]
        coins = [x["symbol"] for x in p["keep"]]   # the code's order: the buyer opens from the top
        # a watch list until yesterday's ClickHouse data is complete; a plan opens MAX_PLAN coins at most
        short = coins[:MAX_PLAN] if r["entry_day"] else []
        cut = coins[MAX_PLAN:] if r["entry_day"] else []
        sides = {x["symbol"]: x.get("side") or ("LONG" if "LONG" in str(x.get("signal", "")) else "SHORT")
                 for x in p["keep"][:MAX_PLAN]} if r["entry_day"] else {}
        charts = {k: {"chart": v.get("chart"), "png": v.get("png")} for k, v in p["charts"].items()}
        narrative = summarize(r, p["keep"], short, cut, charts) if coins else NO_PICKS_TH
        bus.send("bt.dca.plan", {"run_id": run, "short": short, "sides": sides, "narrative": narrative, "charts": charts, **r, "picks": p["keep"]})
        why_wait = "นอกช่วงเปิดไม้ 07:00–08:59 น." if str(r.get("data_to", "")).endswith(("23:59", "24:00")) else "ข้อมูล ClickHouse ยังไม่ครบ"
        say("หัวหน้าพอร์ต", f"[{run}] " + ("" if coins else "(โค้ด ไม่ได้เรียก AI) ") + "สรุปแผน: " + (", ".join(f"{binance.coin(x)}({sides.get(x, 'SHORT')})" for x in short) or "ไม่มี")
            + ("" if short or not coins else f" ({why_wait} · เฝ้าดู {', '.join(binance.coin(x) for x in coins)})")
            + (f" · แผนละไม่เกิน {MAX_PLAN} เหรียญ ไม่เปิด {', '.join(binance.coin(x) for x in cut)}" if cut else "")
            + (f" · {PAUSED_TH}" if short and not OPEN_NEW else ""))

    bus.listen("cio", ["bt.dca.review", "bt.chart.result"], handle, idle=lambda: handle([]))


# ======================= exits: the backtest's rule (ลุงจืด 2026-10-28 "กลับ 1x + ออกตามกติกา"), checked every round

ENTRY_SIGNALS = os.path.join(CACHE, "entry-signals.json") # each coin's entry signal string (read by console snapshot.ts too)
ENTRY_STOPS = os.path.join(CACHE, "entry-stops.json")     # each coin's initial stop_dist / atr20 from 1d breakdown


def remember_signals(picks):
    """Save each picked coin's entry signal (string) and 1d ATR20 stop distance so exit_review has exact entry levels."""
    if not picks:
        return
    saved, stops = {}, {}
    with contextlib.suppress(OSError, ValueError, TypeError), open(ENTRY_SIGNALS, encoding="utf-8") as f:
        saved = json.load(f)
    with contextlib.suppress(OSError, ValueError, TypeError), open(ENTRY_STOPS, encoding="utf-8") as f:
        stops = json.load(f)
    for p in picks:
        c = p.get("coin") or p.get("base")
        if c:
            saved[c] = p.get("signal") or market.lv_signal()
            if p.get("stop_dist") is not None or p.get("atr20") is not None:
                stops[c] = {"stop_dist": p.get("stop_dist"), "atr20": p.get("atr20")}
    with contextlib.suppress(OSError), open(ENTRY_SIGNALS, "w", encoding="utf-8") as f:
        json.dump(saved, f, ensure_ascii=False)
    if stops:
        with contextlib.suppress(OSError), open(ENTRY_STOPS, "w", encoding="utf-8") as f:
            json.dump(stops, f, ensure_ascii=False)


def entry_signal(coin):
    with contextlib.suppress(OSError, ValueError, TypeError), open(ENTRY_SIGNALS, encoding="utf-8") as f:
        return json.load(f).get(coin) or market.lv_signal()
    return market.lv_signal()


def entry_stop_meta(coin):
    with contextlib.suppress(OSError, ValueError, TypeError), open(ENTRY_STOPS, encoding="utf-8") as f:
        v = json.load(f).get(coin)
        return v if isinstance(v, dict) else {}
    return {}


def position_facts(x):
    """An open position as the server sent it (Binance's own strings), + side, entry signal, and distance to liquidation."""
    sig = x.get("entry_signal") or entry_signal(x["coin"])
    side = x.get("side") or ("LONG" if "LONG" in str(sig) else "SHORT")
    f = {"coin": x["coin"], "side": side, "entry_signal": sig,
         "entry": x["entry"], "mark": x["mark"], "days": x.get("days"), "funding_usdt": x.get("funding_usdt")}
    if x.get("pnl_pct") is not None:
        f["pnl_pct"] = f"{x['pnl_pct']}%"
    mark, liq = binance.dec(x["mark"]), binance.dec(x.get("liq_price") or "0")
    if mark > 0 and liq > 0:
        f["to_liquidation"] = f"{abs(liq / mark - 1) * 100:.1f}%"
    return f


def hourly_bars(symbols, days=45):
    """symbol -> [(hour 'YYYY-MM-DD HH:00:00', high, low, close)] of completed UTC hours from 15m futures candles, oldest
    first (the bars research/loosen_rules_backtest.py resamples). An hour short of its 4 candles is left out: ClickHouse
    gets a candle 3-6 min late, and 3 candles would give the RSI exit a wrong close. Indicators only: floats."""
    out = {}
    for sym, h, hi, lo, c in market.ch("""SELECT symbol, toString(h), toString(max(hi)), toString(min(lo)), toString(argMax(c, t)) FROM (
        SELECT symbol, open_time t, toStartOfHour(open_time) h, any(high) hi, any(low) lo, any(close) c FROM altcoin.ohlcv
        WHERE market='um_futures' AND interval='15m' AND symbol IN {syms:Array(String)}
          AND open_time >= toStartOfHour(now()) - INTERVAL {days:UInt32} DAY AND open_time < toStartOfHour(now())
        GROUP BY symbol, open_time) GROUP BY symbol, h HAVING count() = 4 ORDER BY symbol, h""", syms=str(sorted(symbols)), days=days):
        out.setdefault(sym, []).append((h, float(hi), float(lo), float(c)))
    return out


def recent_range(positions, now_):
    """symbol -> (high, low) of Binance's own 15m futures candles (the running one too) that opened since the later of
    opened_at and the start of last hour. WHY not ClickHouse: it gets a candle 3-6 min after the candle closes (measured
    2026-11-10), so hourly_bars alone sees a stop touched in a round's last minutes an hour late. A symbol whose read
    fails is left out: its mark is still checked. Indicators only: floats."""
    out, floor = {}, now_.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    for x in positions:
        try:
            since = max(floor, datetime.fromisoformat(x["opened_at"])) if x.get("opened_at") else floor
            q = urllib.parse.urlencode({"symbol": x["symbol"], "interval": "15m", "startTime": int(since.timestamp() * 1000), "limit": 10})
            with urllib.request.urlopen(f"{market.FAPI_KLINES}?{q}", timeout=10) as r:
                ks = json.load(r)
            if ks:
                out[x["symbol"]] = (max(float(k[2]) for k in ks), min(float(k[3]) for k in ks))
        except Exception:  # noqa: BLE001
            log.warning("exit: 15m candles of %s failed", x.get("symbol"), exc_info=True)
    return out


def _agg_utc_days(bars):
    """Aggregate completed 1h bars [(h, hi, lo, c)] into completed 24-bar UTC days [(day_str, hi, lo, close_23h)]."""
    by_day = {}
    for h, hi, lo, c in bars:
        d = h[:10]
        rec = by_day.get(d)
        if rec is None:
            by_day[d] = [1, hi, lo, c, h[11:13]]
        else:
            rec[0] += 1
            if hi > rec[1]:
                rec[1] = hi
            if lo < rec[2]:
                rec[2] = lo
            rec[3] = c
            rec[4] = h[11:13]
    return [(d, rec[1], rec[2], rec[3]) for d, rec in sorted(by_day.items()) if rec[0] >= 24 and rec[4] == "23"]


def _atr20_stop_from_days(days_before, entry):
    """Compute clip(3 * ATR20 / entry, 0.10, 0.35) from >=20 completed UTC daily bars before entry."""
    if len(days_before) < 20 or entry <= 0:
        return None
    trs = [days_before[0][1] - days_before[0][2]]
    for k in range(1, len(days_before)):
        hi, lo, prev_c = days_before[k][1], days_before[k][2], days_before[k - 1][3]
        trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
    atr20 = sum(trs[-20:]) / 20.0
    return min(max(3.0 * atr20 / entry, market.LV_STOP_MIN), market.LV_STOP_MAX)


def rule_exit(side, entry, mark, opened_at, bars, now_, recent=None, stop_dist=None):
    """Exit rule checked every 15m (watch) and every hourly round.
    When LOWVOL_EXIT_ON is True (live Strategy #1 `LOWVOL_BREAKDOWN`), SHORT positions use exact backtest levels from
    d:/tread2/research/short_v2/lowvol_breakdown.py (run_tf_exit) on market.LV_TF bars:
      - Initial SL = entry * (1 + clip(3 * ATR20 / entry, 10%, 35%)), ATR20 of the 20 bars up to the signal bar
      - TP = entry * 0.70 (-30%)
      - A completed bar closing with profit >= +15% ratchets SL to entry * 0.96 (+4% locked profit): every 1h bar on "1h",
        the 23:00 UTC bar (the day's close) on "1d"
      - Max hold = 168 h on "1h", 21 days on "1d"."""
    sig = opened_at and f"{opened_at.astimezone(timezone.utc) - timedelta(hours=1):%Y-%m-%d %H}:00:00"
    at_sl, at_tp = " · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop", " · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"
    closes = [b[3] for b in bars]
    after = [(k, b) for k, b in enumerate(bars) if sig and b[0] > sig]
    held = now_ - opened_at.replace(minute=0, second=0, microsecond=0) if opened_at else timedelta(0)
    top, low, bar15 = max(mark, (recent or (mark,))[0]), min(mark, (recent or (mark, mark))[1]), "แท่ง 15 นาทีล่าสุดแตะ"
    if side == "LONG":
        sl, tp, peak = entry * 0.88, entry * 1.35, 0.0
        for k, (_, hi, lo, c) in after:
            if lo <= sl:
                return "หลุด stop -12%" + at_sl
            if hi >= tp:
                return "ถึง TP +35%" + at_tp
            peak, cr = max(peak, (hi - entry) / entry), (c - entry) / entry
            if peak >= 0.15 and peak - cr >= 0.08:
                return f"กำไรเคยถึง {peak * 100:.1f}% แล้วย่อเหลือ {cr * 100:.1f}%"
            r = market.rsi(closes[:k + 1])
            if cr >= 0.18 and r is not None and r > 72:
                return f"RSI 1 ชม. {r:.1f} > 72 ตอนกำไร {cr * 100:.1f}%"
        if low <= sl:
            return ("ราคา mark หลุด" if mark <= sl else bar15) + " stop -12%" + at_sl
        if top >= tp:
            return ("ราคา mark ถึง" if mark >= tp else bar15) + " TP +35%" + at_tp
        return "ถือครบ 7 วัน" if held >= timedelta(days=7) else None

    if LOWVOL_EXIT_ON:
        all_days = _agg_utc_days(bars) if bars else []
        signal_day_iso = (opened_at.astimezone(timezone.utc) - timedelta(days=1)).date().isoformat() if opened_at else None
        days_before = [d for d in all_days if signal_day_iso and d[0] <= signal_day_iso]
        tf1h = market.LV_TF == "1h"
        sd = stop_dist
        if sd is not None:
            sd = min(max(float(sd), market.LV_STOP_MIN), market.LV_STOP_MAX)
        else:   # a position without a saved stop (opened by hand): ATR20 of the bars up to the one before it opened
            sd = _atr20_stop_from_days([b for b in bars if sig and b[0] <= sig] if tf1h else days_before, entry)
        if sd is None:
            sd = market.LV_STOP_MAX
        sl = entry * (1.0 + sd)
        tp = entry * (1.0 - market.LV_TP_DROP)
        ratchet_sl = entry * market.LV_RATCHET_STOP
        now_hour = f"{now_.astimezone(timezone.utc):%Y-%m-%d %H}:00:00" if now_ else None

        def _sl_txt(prefix):
            pct_s = (sl / entry - 1.0) * 100.0
            kind = "trailing stop ล็อกกำไร" if sl < entry else "stop"
            return f"{prefix} {kind} {sl:.6g} ({pct_s:+.1f}%)" + at_sl

        for _, (h_iso, hi, lo, c) in after:
            if hi >= sl:
                return _sl_txt("หลุด")
            if lo <= tp:
                return f"ถึง TP -{market.LV_TP_DROP * 100:.0f}%" + at_tp
            if (tf1h or h_iso[11:13] == "23") and (not now_hour or now_hour > h_iso) and (entry - c) / entry >= market.LV_RATCHET_PROFIT:
                sl = min(sl, ratchet_sl)

        if top >= sl:
            return _sl_txt("ราคา mark หลุด" if mark >= sl else bar15)
        if low <= tp:
            return ("ราคา mark ถึง" if mark <= tp else bar15) + f" TP -{market.LV_TP_DROP * 100:.0f}%" + at_tp
        if tf1h:
            return f"ถือครบ {market.LV_MAX_HOLD_HOURS} ชม." if held >= timedelta(hours=market.LV_MAX_HOLD_HOURS) else None
        return f"ถือครบ {market.LV_MAX_HOLD_DAYS} วัน" if held >= timedelta(days=market.LV_MAX_HOLD_DAYS) else None

    first = sig and f"{datetime.fromisoformat(sig) - timedelta(hours=168):%Y-%m-%d %H}:00:00"
    swing = max((b[1] for b in bars if sig and first <= b[0] <= sig), default=None)
    sl = entry * (1 + (min(max(swing / entry - 1 + 0.05, 0.08), 0.45) if swing else 0.45))
    tp = entry * 0.65
    for k, (_, hi, lo, c) in after:
        if hi >= sl:
            return f"หลุด stop {sl:.6g} (+{(sl / entry - 1) * 100:.1f}%)" + at_sl
        if lo <= tp:
            return "ถึง TP -35%" + at_tp
        r = market.rsi(closes[:k + 1])
        if r is not None and r < 30 and (entry - c) / entry >= 0.20:
            return f"RSI 1 ชม. {r:.1f} < 30 ตอนกำไร {(entry - c) / entry * 100:.1f}%"
    if top >= sl:
        return f"{'ราคา mark หลุด' if mark >= sl else bar15} stop {sl:.6g} (+{(sl / entry - 1) * 100:.1f}%)" + at_sl
    if low <= tp:
        return ("ราคา mark ถึง" if mark <= tp else bar15) + " TP -35%" + at_tp
    return "ถือครบ 21 วัน" if held >= timedelta(days=21) else None


def _pos_label(x):
    c = x["coin"].upper()
    return f"{c}({x['pair'].upper()})" if x.get("pair") else c


def exit_review(run_id, info=None, quiet=False):
    """Close every open bot position the backtested exit rule (rule_exit) says to close, every round, plus automatically
    close any paired BTCUSDT LONG hedge leg whenever its partner alt SHORT closes or is already gone on Binance."""
    try:
        p = binance.fetch_portfolio()
        if "error" in p:
            say("คนซื้อขาย", f"[{run_id}] ดู position ที่เปิดอยู่ไม่ได้ ({p['error'][:150]}) — รอบนี้ถือทุกไม้")
            return []
        all_shorts = p.get("shorts") or []
        held = {_pos_label(x): x for x in all_shorts if x.get("on_binance") and not x.get("unclear") and x.get("mark") and x.get("entry")}
        if not held:
            return []
        facts_map = {k: position_facts(x) for k, x in held.items()}
        alt_syms = sorted({x["symbol"] for x in held.values() if not x.get("pair")})
        try:
            bars, note = (hourly_bars(alt_syms) if alt_syms else {}), ""
        except Exception:  # noqa: BLE001 — the mark still meets the widest stop/TP levels
            log.exception("exit: ClickHouse bars failed")
            bars, note = {}, " · อ่านแท่ง 1 ชม. จาก ClickHouse ไม่ได้ รอบนี้เทียบแค่ราคา mark กับ stop กว้างสุด"
        t = binance.utcnow()
        non_hedge = [x for x in held.values() if not x.get("pair")]
        recent = recent_range(non_hedge, t) if non_hedge else {}
        why = {}
        for k, x in held.items():
            if x.get("pair"):
                continue
            c = x["coin"].upper()
            meta_s = entry_stop_meta(c)
            sd = x.get("stop_dist")
            if sd is None and meta_s.get("stop_dist") is not None:
                sd = meta_s["stop_dist"]
            elif sd is None and meta_s.get("atr20") is not None and float(x["entry"]) > 0:
                sd = 3.0 * float(meta_s["atr20"]) / float(x["entry"])
            why[k] = rule_exit(facts_map[k]["side"], float(x["entry"]), float(x["mark"]),
                               datetime.fromisoformat(x["opened_at"]) if x.get("opened_at") else None,
                               bars.get(x["symbol"], []), t, recent.get(x["symbol"]), stop_dist=sd)
        # Paired BTCUSDT LONG hedge legs follow their partner alt SHORT 1:1 (never standalone 7d/-12% LONG rules)
        unclear_alts = {x["coin"].upper() for x in all_shorts if not x.get("pair") and x.get("unclear")}
        for k, x in held.items():
            if not x.get("pair"):
                continue
            pair_c = x["pair"].upper()
            if pair_c in unclear_alts:
                why[k] = None
            elif pair_c in held and not held[pair_c].get("pair"):
                why[k] = f"ปิด hedge คู่ {pair_c} พร้อมขา short" if why.get(pair_c) else None
            else:
                why[k] = f"ปิด hedge คู่ {pair_c} (ขา short ปิดไปแล้ว)"
    except Exception as e:  # noqa: BLE001
        log.exception("exit: review failed")
        say("คนซื้อขาย", f"[{run_id}] ตรวจกติกาออกไม่สำเร็จ ({str(e)[:150]}) — รอบนี้ถือทุกไม้")
        return []
    targets = {k: (x["id"] if x.get("pair") else x["symbol"]) for k, x in held.items()}
    if isinstance(info, dict):
        info.update(ids={targets[k]: x["id"] for k, x in held.items() if x.get("id")},
                    whys={targets[k]: w for k, w in why.items() if w},
                    positions=list(facts_map.values()),
                    decisions={k: {"action": "close" if w else "hold", "reason_th": w or "ยังไม่ถึงกติกาออก"} for k, w in why.items()})
    log.info("exit: %d positions checked, %d to close, 15m candles of %d%s", len(held), sum(map(bool, why.values())), len(recent), note)
    if not quiet or any(why.values()):
        say("คนซื้อขาย", f"[{run_id}] ตรวจกติกาออก {len(held)} ไม้{note}: " + " · ".join(
            f"{k}: {'ปิด — ' + w if w else 'ถือต่อ'} ({facts_map[k].get('pnl_pct') or '-'})" for k, w in why.items()) + " · ไม่ใช่คำแนะนำการลงทุน")
    return [targets[k] for k, w in why.items() if w][:MAX_CLOSE]


def server_sync(run_id, lines):
    """POST /sync: the server settles orders of unknown outcome and records what Binance closed (bookkeeping only)."""
    try:
        lines += binance.post("/sync", {"run_id": run_id})["lines"]
    except Exception as e:  # noqa: BLE001 — the server's own guards still refuse an unclear coin
        log.exception("buyer: sync %s failed", run_id)
        lines.append(f"⚠️ ตรวจบัญชีที่ server ไม่สำเร็จ: {str(e)[:200]}")


def _target_name(target):
    return target[15:] if target.startswith("bto-") and len(target) > 15 else binance.coin(target)


def close_positions(run_id, close, ids, lines, whys=None, retry="รอบหน้าตรวจกติกาใหม่"):
    """POST /close for each target (`symbol` for single-leg coins, `bto-...` id for paired BTCUSDT hedge legs), using
    `ids[target]` -> `bto-...` position id. One failure never stops the others. Returns True when a second /sync is needed."""
    unsure = False
    for sym in close:
        name = _target_name(sym)
        try:
            pid = ids.get(sym) or (sym if sym.startswith("bto-") and sym in ids.values() else None)
            if not pid:
                raise RuntimeError("ไม่มีไม้นี้ในพอร์ตของบอท")
            r = binance.post("/close", {"id": pid, "why": ((whys or {}).get(sym) or (whys or {}).get(pid) or "ปิดตามกติกา")[:200]})
        except Exception as e:  # noqa: BLE001
            log.exception("buyer: close %s failed", sym)
            unsure = True
            lines.append(f"⚠️ ปิด {name} ไม่สำเร็จหรือไม่ได้คำตอบ ({str(e)[:150]}) — {retry}")
            say("คนซื้อขาย", f"[{run_id}] {lines[-1]}")
            continue
        lines.append(r["line"])
        unsure |= r["status"] == "unclear" or r["why"] == "gone"
        if r["status"] in ("refused", "rejected") and r["why"] != "gone":
            say("คนซื้อขาย", f"[{run_id}] ⚠️ ปิด {name} ไม่ได้ ({r['why'] or r['status']}): {r['line']}")
    return unsure


def close_by_hand(pids, in_round=False):
    """`team.py close <id>...`: close these bot positions now, whole qty at market, AND automatically include any live
    paired partner leg (BTCUSDT LONG <-> Alt SHORT) so pressing ปิดไม้ never leaves an unhedged leg behind."""
    run_id, lines = now().strftime("%Y-%m-%d"), []
    log.info("close by hand starts: %s", " ".join(pids))
    try:
        p = binance.fetch_portfolio()
        if "error" in p:
            raise RuntimeError(p["error"])
        shorts = p.get("shorts") or []
        by_id = {x["id"]: x for x in shorts if x.get("id") and x.get("symbol")}
        chosen = [pid for pid in pids if pid in by_id]
        lines += [f"⚠️ ไม่ปิด {i}: ไม่มีไม้นี้ในพอร์ตของบอทแล้ว" for i in pids if i not in by_id]
        # Auto-include live partner leg if this position is part of an active H_DELTA0 pair
        extra = []
        for pid in chosen:
            x = by_id[pid]
            if x.get("pair"):
                partner_coin = x["pair"].upper()
                for y in shorts:
                    if not y.get("pair") and (y.get("coin") or "").upper() == partner_coin and y.get("on_binance") and not y.get("unclear"):
                        if y["id"] not in chosen and y["id"] not in extra:
                            extra.append(y["id"])
            else:
                c_up = (x.get("coin") or binance.coin(x["symbol"])).upper()
                for y in shorts:
                    if (y.get("pair") or "").upper() == c_up and y.get("on_binance") and not y.get("unclear"):
                        if y["id"] not in chosen and y["id"] not in extra:
                            extra.append(y["id"])
        all_pids = chosen + extra
        ids = {pid: pid for pid in all_pids}
        whys = {pid: ("ลุงจืดกดปิดจากหน้า console" if pid in chosen else "ปิดคู่ hedge ตามไม้ที่ลุงจืดกดปิด") for pid in all_pids}
        close_positions(run_id, all_pids, ids, lines, whys, "ดูแท็บพอร์ต ถ้ายังเปิดอยู่ กดปิดไม้ใหม่")
    except Exception as e:  # noqa: BLE001
        log.exception("close by hand %s failed", pids)
        lines.append(f"⚠️ ปิดไม้ที่กดไม่สำเร็จ: {str(e)[:200]} — ยังไม่ได้ส่งคำสั่ง กดใหม่ได้")
    between_rounds(run_id, "ลุงจืดกดปิดไม้จากหน้า console", lines, in_round)


def watch():
    """`team.py watch`: the exit rule between rounds, every 15 min (runner.py, never beside a round or a hand close).
    Silent unless a position closes. Never raises."""
    run_id, lines, info = now().strftime("%Y-%m-%d"), [], {}
    close = exit_review(run_id, info, quiet=True)
    if close:
        close_positions(run_id, close, info.get("ids") or {}, lines, info.get("whys"), "อีก 15 นาทีตรวจกติกาใหม่")
        between_rounds(run_id, "ปิดตามกติกาออก (ตรวจทุก 15 นาที)", lines)


def between_rounds(run_id, head, lines, in_round=False):
    """After closes outside a round's own buyer: book them, tell the team chat, refresh the app's portfolio. Never raises."""
    server_sync(run_id, lines)
    text = f"[{run_id}] {head}: " + (" | ".join(lines) or "ไม่มีคำสั่ง")
    if in_round:
        log.info("คนซื้อขาย: %s", text)
    else:
        say("คนซื้อขาย", text)
    t = now().astimezone(TH)
    with contextlib.suppress(Exception):
        append(os.path.join(REPORTS, "team-chat", f"{t:%Y-%m-%d}.md"), f"- {t:%H:%M} **คนซื้อขาย**: {text}")
    with contextlib.suppress(Exception):
        binance.save_portfolio(binance.fetch_portfolio())


def btc_book_ask():
    """Read live BTCUSDT Futures ask price as Decimal (verifying spread <= 0.5%) for matched 0.001 BTC hedge sizing."""
    q = urllib.parse.urlencode({"symbol": "BTCUSDT"})
    with urllib.request.urlopen(f"https://{binance.FAPI}/fapi/v1/ticker/bookTicker?{q}", timeout=10) as r:
        book = json.loads(r.read().decode(), parse_float=Decimal)
    bid, ask = binance.dec(book["bidPrice"]), binance.dec(book["askPrice"])
    if bid <= 0 or ask <= 0 or (ask - bid) / bid > binance.MAX_SPREAD:
        raise RuntimeError(f"ราคาเสนอซื้อขาย BTCUSDT ถ่างผิดปกติ ({bid} / {ask})")
    return ask


def pair_lot(btc_ask):
    """Compute (btc_qty, leg_usdt) for BTC_001_MATCHED: smallest k * 0.001 BTC >= $52 USDT, matched 1:1 on the alt SHORT."""
    k = 1
    while k * BTC_UNIT_QTY * btc_ask < MIN_BTC_LEG_USDT:
        k += 1
    qty = k * BTC_UNIT_QTY
    usdt = (qty * btc_ask).quantize(Decimal("0.01"))
    return qty, usdt


def _open_new_paired(run_id, short, sides, lines, p, held, cooled, avail):
    """H_DELTA0 (`BTC_001_MATCHED`) paired opener:
    1) Sizes each pair from live BTCUSDT ask (`k * 0.001 BTC`, ~$67 at $67k BTC)
    2) Opens `BTCUSDT LONG` (`pair=coin(sym)`, `qty=btc_qty`) first
    3) Opens `sym SHORT` (`usdt=leg_usdt`) second
    4) If `sym SHORT` is refused/rejected, immediately settles + closes the `BTCUSDT LONG` leg so no naked hedge remains."""
    shorts_list = p.get("shorts") or []
    bot_live_syms = {x["symbol"] for x in shorts_list if x.get("on_binance")}
    manual_syms = set(p.get("held_symbols") or ()) - bot_live_syms
    alt_open = {x["symbol"] for x in shorts_list if not x.get("pair") and (x.get("on_binance") or not x.get("entry"))}
    btc_hedges = [x for x in shorts_list if x.get("pair") and (x.get("on_binance") or not x.get("entry"))]
    active_pairs = max(len(alt_open), len(btc_hedges)) + len(manual_syms)
    room = MAX_PAIRS - active_pairs
    try:
        btc_ask = btc_book_ask()
    except Exception as e:  # noqa: BLE001
        log.exception("buyer: btc_book_ask failed")
        lines.append(f"⚠️ อ่านราคา BTCUSDT เพื่อคำนวณขนาดคู่ hedge ไม่ได้ ({str(e)[:150]}) — หยุดเปิดไม้ใหม่รอบนี้")
        return False
    btc_qty, leg_usdt = pair_lot(btc_ask)
    if leg_usdt > MARGIN_USDT:
        lines.append(f"⚠️ มูลค่า 1 หน่วย hedge ({btc_qty} BTC ≈ {binance.money(leg_usdt)} USDT) เกินเพดานไม้ละ {binance.money(MARGIN_USDT)} USDT — หยุดเปิดใหม่")
        return False
    pair_need = (leg_usdt * 2) * Decimal("1.02") + Decimal("5")
    unsure = False
    held_alts = (held - {"BTCUSDT"}) if ("BTCUSDT" not in manual_syms) else held
    active_pair_tags = {(x.get("pair") or "").upper() for x in btc_hedges}

    for sym in short[:MAX_PLAN]:
        name, side = binance.coin(sym), sides.get(sym)
        why = ("กลยุทธ์ H_DELTA0 เปิดเฉพาะเหรียญฝั่ง SHORT คู่กับ BTC LONG" if side == "LONG" and not market.LONG_ON else
               "แผนไม่ได้บอกฝั่ง LONG/SHORT" if side not in ("LONG", "SHORT") else
               "มี position อยู่แล้ว" if (sym in held_alts or name.upper() in active_pair_tags) else
               f"ไม้ล่าสุดเพิ่งปิด ยังไม่ครบเวลาพัก ({REST_TH})" if sym in cooled else None)
        if why:
            lines.append(f"ข้าม {name}: {why}")
            continue
        if room <= 0:
            lines.append(f"ถือครบเพดาน {MAX_PAIRS} คู่แล้ว — หยุดเปิดคู่ใหม่")
            break
        if avail < pair_need:
            lines.append(f"USDT ใน Futures เหลือ {binance.money(avail)} ไม่พอเปิดคู่ {name}+BTC (~{binance.money(pair_need)} USDT) — หยุดเปิดใหม่")
            break

        # Leg 1: BTCUSDT LONG hedge
        btc_cid = binance.order_id("open", run_id, "BTCUSDT", pair=name)
        try:
            r_btc = binance.post("/open", {"run_id": run_id, "symbol": "BTCUSDT", "side": "LONG",
                                           "usdt": str(leg_usdt), "leverage": LEVERAGE, "pair": name, "qty": str(btc_qty)})
        except Exception as e:  # noqa: BLE001
            log.exception("buyer: open BTC hedge for %s failed", sym)
            lines.append(f"⚠️ เปิดขา hedge BTC คู่ {name} ไม่สำเร็จหรือไม่ได้คำตอบ ({str(e)[:150]}) — หยุดเปิดไม้ใหม่รอบนี้")
            return True
        lines.append(r_btc["line"])
        if r_btc["status"] in ("refused", "rejected"):
            if r_btc["status"] == "refused" and r_btc["why"] in ACCOUNT_WIDE + ("held",):
                say("คนซื้อขาย", f"[{run_id}] ⚠️ หยุดเปิดคู่ใหม่รอบนี้ ({r_btc['why']}): {r_btc['line']}")
                break
            continue
        if r_btc["status"] == "unclear":
            if r_btc.get("order_status") != "FILLED":
                lines.append(f"⚠️ ขา hedge BTC คู่ {name} ยังไม่รู้ผลคำสั่ง — หยุดเปิดขา short เพื่อไม่ให้เปิดค้างข้างเดียว")
                return True
            # Routine Binance cumQuote=0 on FILLED market order: settle immediately so Leg 2 isn't blocked by 'unclear'
            server_sync(run_id, lines)

        # Leg 2: Alt SHORT matched USDT notional
        try:
            r_alt = binance.post("/open", {"run_id": run_id, "symbol": sym, "side": "SHORT",
                                           "usdt": str(leg_usdt), "leverage": LEVERAGE})
        except Exception as e:  # noqa: BLE001
            log.exception("buyer: open %s failed after BTC hedge", sym)
            lines.append(f"⚠️ เปิด {name} ไม่ได้คำตอบหลังเปิด BTC hedge ({str(e)[:150]}) — หยุดเปิดไม้ใหม่รอบนี้")
            return True
        lines.append(r_alt["line"])

        # Atomic rollback: if Alt SHORT was refused/rejected (nothing placed on sym), close the BTC hedge immediately!
        if r_alt["status"] in ("refused", "rejected"):
            if r_btc["status"] != "tested":
                server_sync(run_id, lines)
                try:
                    rc = binance.post("/close", {"id": r_btc.get("cid") or btc_cid,
                                                 "why": f"ยกเลิก hedge เพราะเปิด short {name} ไม่สำเร็จ ({r_alt['why'] or r_alt['status']})"})
                    lines.append(rc["line"])
                    unsure = True
                except Exception as e:  # noqa: BLE001
                    log.exception("buyer: rollback BTC hedge %s failed", btc_cid)
                    lines.append(f"⚠️ ปิดคืนขา hedge {btc_cid} ไม่สำเร็จ ({str(e)[:150]}) — รอบถัดไป exit_review จะปิดซ้ำ")
                    unsure = True
            if r_alt["status"] == "refused" and r_alt["why"] in ACCOUNT_WIDE:
                say("คนซื้อขาย", f"[{run_id}] ⚠️ หยุดเปิดคู่ใหม่รอบนี้ ({r_alt['why']}): {r_alt['line']}")
                break
            continue

        avail -= (leg_usdt * 2) + (leg_usdt * 2) * FEE
        room -= 1
        active_pair_tags.add(name.upper())
        held_alts.add(sym)
        unsure |= r_alt["status"] == "unclear" or r_btc["status"] == "unclear"
    return unsure


def open_new(run_id, short, sides, lines):
    """POST /open for the plan's coins in its order (the code ranked them). When BTC_HEDGE_ON is True (live Strategy #1),
    opens matched (BTCUSDT LONG + Alt SHORT) pairs via _open_new_paired."""
    p = binance.fetch_portfolio()
    held = held_shorts(p)
    if held is None or not p.get("available_usdt"):
        lines.append(f"ไม่เปิดไม้ใหม่รอบนี้: อ่านพอร์ตจาก server ไม่ได้ ({str(p.get('error') or 'ไม่มียอด USDT ว่างหรือรายการ position')[:150]})")
        return False
    bare = [x["coin"] for x in p.get("shorts") or [] if x.get("unclear") and x.get("on_binance")]
    if bare:
        lines.append(f"ไม่เปิดไม้ใหม่รอบนี้: {', '.join(dict.fromkeys(bare))} มีคำสั่งที่ยังไม่รู้ผล — ตรวจประวัติคำสั่งใน Binance")
        return False
    cooled, avail = cooldown_shorts(p=p), binance.dec(p["available_usdt"])
    if BTC_HEDGE_ON:
        return _open_new_paired(run_id, short, sides, lines, p, held, cooled, avail)
    open_now = set(p.get("held_symbols") or ()) | {x["symbol"] for x in p["shorts"] if x.get("on_binance") or not x.get("entry")}
    room = MAX_OPEN - len(open_now)
    unsure = False
    for sym in short[:MAX_PLAN]:
        name, side = binance.coin(sym), sides.get(sym)
        why = ("ปิดฝั่ง LONG (SHORT อย่างเดียว)" if side == "LONG" and not market.LONG_ON else
               "แผนไม่ได้บอกฝั่ง LONG/SHORT" if side not in ("LONG", "SHORT") else "มี position อยู่แล้ว" if sym in held
               else f"ไม้ล่าสุดเพิ่งปิด ยังไม่ครบเวลาพัก ({REST_TH})" if sym in cooled else None)
        if why:
            lines.append(f"ข้าม {name}: {why}")
            continue
        if room <= 0:
            lines.append(f"ถือครบ {MAX_OPEN} ไม้แล้ว (นับรวมไม้ที่ลุงจืดเปิดเอง) — หยุดเปิดใหม่")
            break
        if avail < MARGIN_USDT * Decimal("1.01"):
            lines.append(f"USDT ใน Futures เหลือ {binance.money(avail)} ไม่พอ margin ไม้ละ {binance.money(MARGIN_USDT)} — หยุดเปิดใหม่"
                         " (โอน USDT เข้า USDⓈ-M Futures ในแอป Binance)")
            break
        try:
            r = binance.post("/open", {"run_id": run_id, "symbol": sym, "side": side, "usdt": str(LOT_USDT), "leverage": LEVERAGE})
        except Exception as e:  # noqa: BLE001
            log.exception("buyer: open %s failed", sym)
            lines.append(f"⚠️ เปิด {name} ไม่สำเร็จหรือไม่ได้คำตอบ ({str(e)[:150]}) — หยุดเปิดไม้ใหม่รอบนี้")
            return True
        lines.append(r["line"])
        if r["status"] in ("placed", "found", "unclear", "tested"):
            avail -= MARGIN_USDT + LOT_USDT * FEE
            room -= 1
        unsure |= r["status"] == "unclear"
        if r["status"] == "refused" and r["why"] in ACCOUNT_WIDE:
            say("คนซื้อขาย", f"[{run_id}] ⚠️ หยุดเปิดไม้ใหม่รอบนี้ ({r['why']}): {r['line']}")
            break
    return unsure


def trade(run_id, short, plan=None):
    """One round's orders through the proxy server, which only sends them (every decision is made here): /sync, the
    rule's exits, then the plan's new positions (short [] = exits and upkeep only), /sync again when an answer is
    unclear or the round broke off. Never raises: a bus retry must not run a round twice."""
    if plan:
        remember_signals(plan.get("picks", []))
    lines, exit_info, close, unsure = [], {}, [], False
    try:
        server_sync(run_id, lines)
        close = exit_review(run_id, exit_info)
        unsure = close_positions(run_id, close, exit_info.get("ids") or {}, lines, exit_info.get("whys")) if close else False
        if short and not OPEN_NEW:
            lines.append("ไม่เปิดไม้ใหม่: ลุงจืดสั่งหยุดเปิดไม้ใหม่ทั้ง LONG/SHORT (2026-11-05) รอกติกาใหม่ผ่าน backtest")
        elif short:
            unsure = open_new(run_id, short, (plan or {}).get("sides") or {}, lines) or unsure
    except Exception as e:  # noqa: BLE001
        log.exception("buyer: round %s failed", run_id)
        lines.append(f"⚠️ ส่งคำสั่งรอบนี้ไม่สำเร็จ: {str(e)[:200]} — server กันเปิดซ้ำเองจาก ledger ของมัน")
        unsure = True   # it may have stopped after an order went out
    if unsure:
        server_sync(run_id, lines)   # never raises
    say("คนซื้อขาย", f"[{run_id}] " + (" | ".join(lines) or "ไม่มีคำสั่ง"))
    pf = None
    with contextlib.suppress(Exception):   # the app's portfolio tab (and its open/closed notices); it also refreshes every 1 min on its own
        pf = binance.save_portfolio(binance.fetch_portfolio())
    with contextlib.suppress(Exception):
        write_analysis_log(run_id, short, close, lines, pf, plan, exit_info)


def buyer():
    """Bot 5 คนซื้อขาย (Auto Trade, ลุงจืด 2026-10-11): turns each plan into orders (trade). The Binance key, SHORT_LIVE
    switch, lot cap and order ledger live only on the server (binance.py serve), which only sends."""
    def handle(msgs):
        for _, m in msgs:
            trade(m["run_id"], m.get("short", []), m)

    bus.listen("buyer", ["bt.dca.plan"], handle)


# ======================= report =======================

def pct(x):
    return "-" if x is None else f"{x * 100:+.0f}%"


def share(x):
    return "-" if x is None else f"{x:.0%}"


def write_plan(m):
    out_dir = os.path.join(REPORTS, m["run_id"])
    os.makedirs(out_dir, exist_ok=True)
    rev = {r["coin"]: r for r in m["reviews"]}
    mkt_trend = m.get("mkt_trend") or ""
    L = [f"# แผนเทรดตามเทรนด์ (LONG / SHORT) {m['run_id']}", "", f"สร้างโดยทีม BotTrade {now().astimezone(TH):%Y-%m-%d %H:%M} (เวลาไทย) · ข้อมูล: ClickHouse altcoin "
         f"(Binance USDⓈ-M futures 15m ถึง {m['data_to']} UTC)" + (f" · {mkt_trend}" if mkt_trend else ""), "", *(["## สรุป (โค้ด ไม่ได้เรียก AI)", ""] if m["narrative"] == NO_PICKS_TH else
           ["## สรุปจาก AI (หัวหน้าพอร์ต)", "", "_ตัวเลขในสรุปนี้ผ่านการเทียบกับข้อมูลที่โค้ดคำนวณจาก ClickHouse แล้ว · ตัวเลขจริงดูตาราง_", ""]), m["narrative"], ""]
    rows = []
    for p in m["picks"]:
        ch = m["charts"].get(p["base"]) or {}
        c = ch.get("chart") or {}
        rows.append({**facts(p), "symbol": p["symbol"], "trend": c.get("trend") or "ไม่ได้อ่าน", "png": ch.get("png")})
    remember_signals(rows)
    watchlist = []
    held_set, cooled_set, picked_set = set(m.get("held") or []), set(m.get("cooled") or []), {r["coin"] for r in rows}
    with contextlib.suppress(OSError, ValueError, TypeError), open(os.path.join(out_dir, "screen.json"), encoding="utf-8") as sf:
        s_rows = [r for r in json.load(sf).get("rows", [])
                  if r.get("base") not in picked_set
                  and "stablecoin" not in (r.get("fail") or [])]
        s_rows.sort(key=lambda r: (str(r.get("signal", "")).startswith("นอกเกณฑ์"),
                                   (r.get("vol30_usd") or 0) < market.SHORT_MIN_VOL,
                                   r["base"] in held_set or r["base"] in cooled_set,
                                   -(r.get("score") or 0.0), r.get("age_days") or 999))
        for r in s_rows[:5]:
            why = " · ".join(r.get("fail") or []) or "รอจังหวะเข้าเกณฑ์"
            is_long = (r.get("side") or ("LONG" if "LONG" in str(r.get("signal", "")) else "SHORT")) == "LONG"
            if r["base"] in held_set:
                tag = "ถืออยู่แล้ว"
            elif r["base"] in cooled_set:
                tag = f"ติดพักหลังปิด ({REST_TH})"
            elif str(r.get("signal", "")).startswith(("1D-LowVolBrk", "1H-LowVolBrk", "จ่อหลุดแนวรับ")):
                tag = f"เฝ้าดู Low-Vol {market.LV_TF.upper()}" + ("" if OPEN_NEW else " · หยุดเปิดไม้ใหม่อยู่")
            elif (r.get("vol30_usd") or 0) >= market.SHORT_MIN_VOL and not str(r.get("signal", "")).startswith("นอกเกณฑ์"):
                tag = (("พร้อมเปิด LONG เมื่อ RSI ตัดขึ้น" if is_long else "พร้อมเปิด SHORT เมื่อ RSI ตัดลง") if OPEN_NEW
                       else f"รอ RSI ตัด{'ขึ้น (LONG)' if is_long else 'ลง (SHORT)'} · หยุดเปิดไม้ใหม่อยู่")
            else:
                tag = "เฝ้าดู"
            watchlist.append({**facts(r), "symbol": r.get("symbol", r["base"] + "USDT"), "trend": f"[{tag}] {why}", "png": None})
    outside_1d_window = not m["entry_day"] and str(m.get("data_to", "")).endswith(("23:59", "24:00"))
    if rows:
        L += ["## เหรียญที่เข้ากฎ (เฝ้าดู — หยุดเปิดไม้ใหม่อยู่ รอบนี้ไม่เปิด)" if not OPEN_NEW else
              f"## เหรียญที่จะเปิดสถานะ (เปิดจากบนลงล่าง {f'รอบละไม่เกิน {MAX_PAIRS} คู่' if BTC_HEDGE_ON else f'ถือพร้อมกันไม่เกิน {MAX_OPEN} ไม้'} · server วันละไม่เกิน {binance.DAILY_OPEN_CAP} ไม้)" if m["entry_day"] else
              "## เหรียญที่เข้ากฎแท่ง 1D ล่าสุด (เฝ้าดู — นอกช่วงเปิดไม้ 07:00–08:59 น. เวลาไทย)" if outside_1d_window else
              f"## เหรียญที่เข้ากฎ (เฝ้าดู — ข้อมูล {m['data_to']} UTC ใน ClickHouse ยังไม่ครบ รอบนี้ยังไม่เปิด)", "",
              f"| # | ฝั่ง | เหรียญ | เกณฑ์ที่เข้า | คะแนน | ราคาปิด {m['data_to']} | RSI(14) 1 ชม. | RSI(14) 4 ชม. | อยู่บน Futures (วัน) | เทรนด์ Day | ห่างยอด ATH | volume/วัน (เฉลี่ยเรขาคณิต ≤30 วัน) | 7 วัน | AI อ่านกราฟ |",
              "|---:|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|"]
        for i, r in enumerate(rows, 1):
            img = f"[{r['trend']}]({os.path.relpath(r['png'], out_dir).replace(os.sep, '/')})" if r["png"] else r["trend"]
            L.append(f"| {i} | {r['side']} | {r['coin']} | {r['signal']} | {r['score']} | {r['price']} | {r['rsi_1h']} | {r['rsi_4h']} | {r['age_days']} | {r['trend_1d']} | {r['dd_ath']} | {r['vol30']} | {r['r7']} | {img} |")
        if len(m["picks"]) > len(m["short"]) and m["entry_day"]:
            L.append(f"\nแผนละไม่เกิน {MAX_PLAN} เหรียญ: รอบนี้ไม่เปิด {', '.join(r['coin'] for r in rows[len(m['short']):])}")
        L.append("")
    if watchlist:
        L += [f"## Watchlist {len(watchlist)} เหรียญคะแนนสูงสุดตามเทรนด์ (ดูสถานะในช่องสุดท้าย)", "",
              f"| # | ฝั่ง | เหรียญ | เกณฑ์/กลุ่ม | คะแนน | ราคาปิด {m['data_to']} | RSI(14) 1 ชม. | RSI(14) 4 ชม. | อยู่บน Futures (วัน) | เทรนด์ Day | ห่างยอด ATH | volume/วัน (เฉลี่ยเรขาคณิต ≤30 วัน) | 7 วัน | สถานะที่รอ |",
              "|---:|---|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|"]
        for i, r in enumerate(watchlist, 1):
            L.append(f"| {i} | {r['side']} | {r['coin']} | {r['signal']} | {r['score']} | {r['price']} | {r['rsi_1h']} | {r['rsi_4h']} | {r['age_days']} | {r['trend_1d']} | {r['dd_ath']} | {r['vol30']} | {r['r7']} | {r['trend']} |")
        L.append("")
    if rev:
        L += ["## ความเสี่ยงที่ AI ผู้คุมความเสี่ยงยกมา (ตัวเลขผ่านการเทียบกับข้อมูลแล้ว)", ""]
        L += [f"- **{k}**: {' / '.join(r['risks_th'])}" for k, r in rev.items()]
        L.append("")
    L += ["## สถานะวันนี้", "",
          *( [f"- เทรนด์ตลาดรวม: {mkt_trend}"] if mkt_trend else [] ),
          f"- สัญญาที่สแกน {m['universe']} ตัว · เข้ากฎ {m['passing']} ตัว: {', '.join(m['passing_coins']) or '-'}",
          "- มี position อยู่แล้ว (ไม่เปิดซ้ำ): " + ("ไม่ทราบ (อ่านพอร์ตจาก server ไม่ได้ — server กันเปิดซ้ำเอง)" if m["held"] is None
                                                else ", ".join(m["held"]) or "-"),
          f"- พักเหรียญเพิ่งปิด ({REST_TH}): {', '.join(m.get('cooled') or []) or '-'}",
          f"- {PAUSED_TH}" if not OPEN_NEW else "- รอบนี้เปิดไม้ได้" if m["entry_day"] else "- รอบนี้ไม่เปิดไม้ใหม่ (ตัวกรองตลาดหรือข้อมูลไม่ผ่าน ดูบรรทัดเทรนด์ตลาดด้านบน)" if not m.get("allow_short", True) else f"- รอบนี้เฝ้าดู/ตรวจออกเท่านั้น ({'เปิดคู่ใหม่' if BTC_HEDGE_ON else 'เปิดไม้ใหม่'}เฉพาะช่วง 07:00–08:59 น. เวลาไทย หลังปิดแท่ง 1D)" if outside_1d_window else f"- รอบนี้ยังไม่เปิดไม้ใหม่ (ข้อมูล {m['data_to']} UTC ใน ClickHouse ยังไม่ครบ รอรอบหน้า)", "",
          "## กฎ / ข้อจำกัด", "", f"- กฎ: {SHORT_RULE_TH}",
          "- ไม่มีคำสั่ง stop รอบน Binance: โค้ดตรวจกติกาออกทุกรอบ (ทุก 1 ชม.) ระหว่างรอบหรือตอน PC ล่ม ราคาอาจวิ่งเลย stop ไปก่อนถูกปิด"
          " (1x isolated ราคาต้องวิ่งสวนราวเท่าตัวจึงถูก liquidate)",
          f"- เปิดได้ทุกวัน (รอบละไม่เกิน {MAX_PLAN}, วันละไม่เกิน {binance.DAILY_OPEN_CAP} ตามเวลา UTC) จนถือครบ {MAX_OPEN} ไม้หรือ USDT หมด: เงินอาจเข้าหลายเหรียญในช่วงเดียวกัน",
          "- อดีตไม่รับประกันอนาคต · ไม่ใช่คำแนะนำการลงทุน", ""]
    # display-ready copy for the monitor console (console/); written before dca-plan.md, which marks the day as done
    with open(os.path.join(out_dir, "dca-plan.json"), "w", encoding="utf-8") as f:
        json.dump({"run_id": m["run_id"], "short": m["short"], "sides": m.get("sides", {}),
                   "mkt_trend": mkt_trend, "created": f"{now().astimezone(TH):%Y-%m-%d %H:%M}", "rows": rows,
                   "watchlist": watchlist, "narrative": m["narrative"], "universe": m["universe"],
                   "passing": m["passing"], "passing_coins": m["passing_coins"], "held": m["held"],
                   "cooled": m.get("cooled", []), "entry_day": m["entry_day"],
                   "data_to": m["data_to"], "rule": SHORT_RULE_TH, "open_new": OPEN_NEW}, f, ensure_ascii=False, indent=1)
    path = os.path.join(out_dir, "dca-plan.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    return path


def write_analysis_log(run_id, short, close, lines, pf, plan=None, exit_info=None):
    """Append-only structured round log (reports/analysis-log.jsonl + .md) so Gemini / AI can inspect every hourly round."""
    screen = {}
    with contextlib.suppress(OSError, ValueError), open(os.path.join(REPORTS, run_id, "screen.json"), encoding="utf-8") as f:
        screen = json.load(f)
    s_rows = screen.get("rows", [])
    liquid = [r for r in s_rows if (r.get("vol30_usd") or 0) >= market.SHORT_MIN_VOL and r.get("rsi_prev") is not None and r.get("rsi") is not None]
    liquid.sort(key=lambda r: (-max(r["rsi_prev"], r["rsi"]), r["symbol"]))
    top_rsi = [{"coin": r["base"], "symbol": r["symbol"], "side": r.get("side") or "SHORT", "signal": r.get("signal") or "RSI<70 (Overbought)",
                "price": f"{r['price']:.6g}", "rsi_prev": round(r["rsi_prev"], 1),
                "rsi": round(r["rsi"], 1), "vol30": f"${r['vol30_usd'] / 1e6:,.1f}M", "r7": pct(r.get("r7"))} for r in liquid[:10]]
    passing = [facts(r) for r in s_rows if not r.get("fail")]
    picks = [facts(p) for p in (plan.get("picks", []) if plan else [])]
    t, src = now(), plan or screen
    ex = exit_info or {}
    pf_ok = pf and "error" not in pf
    row = {
        "at": t.isoformat(timespec="seconds"), "at_th": f"{t.astimezone(TH):%Y-%m-%d %H:%M:%S}", "run_id": run_id,
        "data_to": src.get("data_to"), "entry_day": src.get("entry_day"), "universe": src.get("universe"), "late": screen.get("late", []),
        "mkt_trend": src.get("mkt_trend"),
        "passing": passing, "picks": picks, "overbought_top": top_rsi,
        "entry_ai": {"short_plan": list(short), "sides": plan.get("sides", {}) if plan else {},
                     "charts": {k: v.get("chart") for k, v in (plan.get("charts") or {}).items()} if plan else {},
                     "risk_reviews": plan.get("reviews", []) if plan else [], "narrative": plan.get("narrative") if plan else None},
        "exit_ai": {"close_plan": list(close), "positions": ex.get("positions", []), "charts": ex.get("charts", {}),
                    "risk_reviews": ex.get("risk_reviews", []), "decisions": ex.get("decisions", {})},
        "execution": list(lines),
        "portfolio": {"mode": pf.get("mode"), "equity_usdt": pf.get("equity_usdt"), "wallet_usdt": pf.get("wallet_usdt"),
                      "available_usdt": pf.get("available_usdt"), "unrealized_usdt": pf.get("unrealized_usdt"),
                      "realized_usdt": pf.get("realized_usdt"), "shorts": pf.get("shorts", []),
                      "closed_latest": pf.get("closed", [])[:5]} if pf_ok else None,
    }
    append(os.path.join(REPORTS, "analysis-log.jsonl"), json.dumps(row, ensure_ascii=False))
    md_path = os.path.join(REPORTS, "analysis-log.md")
    head = [] if os.path.exists(md_path) else ["# ประวัติการทำงานรายรอบ BotTrade (สำหรับ Gemini / AI วิเคราะห์)", ""]
    top_txt = ", ".join(f"{r['coin']} ({r['rsi_prev']:.1f}→{r['rsi']:.1f}, Vol {r['vol30']}, 7d {r['r7']})" for r in top_rsi[:5]) or "-"
    pass_txt = ", ".join(f"{x['coin']} ({x['side']}) [{x['signal']}]" for x in passing) or "-"
    dec_txt = " · ".join(f"{c}: {d['action']} ({d['reason_th']})" for c, d in ex.get("decisions", {}).items()) or "ไม่มีไม้ที่ถืออยู่"
    pf_txt = (f"Equity {pf.get('equity_usdt')} USDT · ว่าง {pf.get('available_usdt')} USDT · Unrealized {pf.get('unrealized_usdt')} USDT · "
              f"Realized {pf.get('realized_usdt')} USDT · ถือ {len(pf.get('shorts', []))} ไม้ "
              f"({', '.join(s['coin'] + '(' + (s.get('side') or 'SHORT') + ') ' + str(s.get('pnl_pct') or '0') + '%' for s in pf.get('shorts', [])) or '-'})"
              if pf_ok else "อ่านพอร์ตไม่ได้")
    md = head + [
        f"## [{row['at_th']} TH] รอบ `{run_id}` (แท่ง 1H ปิดถึง `{row['data_to'] or '-'}` UTC)",
        f"- **เทรนด์ตลาด:** {row.get('mkt_trend') or '-'} · **สแกน:** {row['universe'] or 0} สัญญา (`entry_day={row['entry_day']}`, ช้า {len(row['late'])} ตัว) · "
        f"**เข้ากฎ:** {len(passing)} ตัว ({pass_txt}) · "
        f"**ส่งเปิดไม้:** {', '.join(binance.coin(s) for s in short) or '-'}",
        f"- **Top 5 เหรียญสภาพคล่องสูง (Vol30 ≥ ${market.SHORT_MIN_VOL / 1e6:.0f}M) ที่ RSI(14) 1H สูงสุด:** {top_txt}",
        f"- **กติกาออกของไม้ที่ถืออยู่:** {dec_txt} → **สั่งปิด:** {', '.join(_target_name(s) for s in close) or '-'}",
        f"- **ผลคำสั่ง Server:** {' | '.join(lines)}",
        f"- **พอร์ตหลังจบรอบ:** {pf_txt}", "",
    ]
    append(md_path, "\n".join(md))


# ======================= runtime =======================

MEMBERS = [quant, secretary, clock, chartist, risk_critic, cio, buyer]
LISTENERS = [quant, secretary, chartist, risk_critic, cio, buyer]   # once() does the clock's job itself


def supervise(fn):
    def loop():
        while not bus.stop.is_set():
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — a crashed member restarts, the rest keep working
                log.exception("%s crashed", fn.__name__)
                try:
                    say("ระบบ", f"{fn.__name__} ล้ม ({str(e)[:120]}) — เริ่มใหม่ใน 30 วินาที")
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(30)
    t = threading.Thread(target=loop, name=fn.__name__, daemon=True)
    t.start()
    return t


def settle(done=lambda: True, max_s=3600, every_s=5):
    """Wait until no listener has queued or in-hand work and done() holds, twice in a row (one look can land
    between a commit and the next send). False on timeout."""
    t0, calm = time.time(), 0
    while time.time() - t0 < max_s:
        bus.flush()   # a handler's last send must reach Kafka before lag can see it
        try:
            quiet = not bus.working and done() and all(bus.lag(g, t) == 0 for g, t in list(bus.groups.items()))
        except Exception:  # noqa: BLE001 — broker blip (lag times out after 10 s): not quiet yet, look again
            log.warning("settle: lag check failed", exc_info=True)
            quiet = False
        calm = calm + 1 if quiet else 0
        if calm >= 2:
            return True
        time.sleep(every_s)
    return False


def once():
    """One round on demand: ClickHouse screen -> AI risks + charts + summary -> short plan -> stop."""
    with contextlib.suppress(Exception):
        watch()   # exits first: the buyer's own check comes only after the screen and the AIs (6-20 min, up to an hour)
    bus.start_at_end = True   # a round handles only what it produces itself (leftovers of a stopped round are dropped)
    threads, t0 = [supervise(m) for m in LISTENERS], time.time()
    while len(bus.groups) < len(LISTENERS):   # settle() only sees groups that have started listening
        if time.time() - t0 > 120:   # a broker that answers the port but not the consumers: end the round, or runner.py sees it running forever and no exit is checked again
            raise RuntimeError("Kafka listeners did not start in 120 s")
        time.sleep(0.2)
    say("ระบบ", f"เริ่มรอบ (อัตโนมัติทุกชั่วโมง หรือลุงจืดกด ▶): ClickHouse คัดเหรียญ → AI วิเคราะห์ (ถ้ามีเหรียญใหม่เข้ากฎ) → ทำแผนเทรด LONG/SHORT แล้วหยุด"
        + ("" if OPEN_NEW else " · ⛔ หยุดเปิดไม้ใหม่อยู่") + f" · โมเดล {MODELS_TH}")
    run, t0 = now().strftime("%Y-%m-%d"), time.time()
    plan = os.path.join(REPORTS, run, "dca-plan.md")
    threading.current_thread().name = "clock"   # the app files log lines under member cards by thread name
    bus.send("bt.dca.request", {"run_id": run, "by": "ลุงจืด"})
    say("นาฬิกา", f"[{run}] ส่งงานให้ควอนต์คัดเหรียญจาก ClickHouse")
    threading.current_thread().name = "MainThread"

    def planned():
        return os.path.exists(plan) and os.path.getmtime(plan) >= t0
    # the CIO writes the plan anyway after waiting 20 min for a missing chart, so 60 min covers a slow round;
    # a link that gave up (quant/risk/cio) can't produce a plan any more: stop now instead of waiting it out
    finished = settle(lambda: planned() or bool(bus.dropped & {"quant", "risk", "cio"}), max_s=3600)
    plan_ok = planned()
    if not plan_ok:   # no plan = no new shorts, but exits and stop-loss upkeep must not depend on ClickHouse/LLMs
        threading.current_thread().name = "buyer"
        trade(run, [])
        threading.current_thread().name = "MainThread"
    say("ระบบ", ("ทำงานครบรอบแล้ว ทีมหยุด" if finished else "แผนเสร็จแต่งานต่อจากแผนเกิน 60 นาที ถูกหยุดกลางทาง"
                 " (คนซื้อขายอาจยังไม่ได้ส่งแผน/คำสั่งปิด) ทีมหยุด") + " — รอบถัดไประบบเริ่มเองต้นชั่วโมงหน้า (หรือกด ▶ ใน console)" if plan_ok else
        f"ไม่ได้แผนเทรด LONG/SHORT รอบนี้ ({'ขั้น ' + ', '.join(sorted(bus.dropped)) + ' ล้ม' if bus.dropped else 'เกิน 60 นาที'}) ทีมหยุด — ดู error ในแท็บ ค่าใช้จ่าย/Log")
    settle(max_s=120)   # let the secretary write that last line
    bus.stop.set()
    for t in threads:
        t.join(timeout=90)
    return plan_ok


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("run", "once"):
        lock = open(os.path.join(CACHE, "team.lock"), "w")
        try:   # single instance; the OS drops the lock if we die
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:   # Linux server
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("BotTrade team is already running (cache/team.lock is held)")
        with open(os.path.join(CACHE, "team.pid"), "w") as f:  # runner.py checks/stops the team by this pid
            f.write(str(os.getpid()))
        bus.ensure_topics()
        if cmd == "once":
            ok = False
            try:
                ok = once()
            except Exception:  # noqa: BLE001
                log.exception("round failed")
            bus.flush()
            logging.shutdown()
            os._exit(0 if ok else 1)   # a normal exit would wait on a member stuck in an Ollama retry (ThreadPoolExecutor's exit hook)
        for m in MEMBERS:
            supervise(m)
        say("ระบบ", f"ทีม BotTrade เริ่มงาน {len(MEMBERS)} สมาชิก · โมเดล {MODELS_TH}")
        while True:
            time.sleep(3600)
    elif cmd == "dca":
        bus.send("bt.dca.request", {"run_id": now().strftime("%Y-%m-%d"), "by": "ลุงจืด"})
    elif cmd in ("close", "close-in-round") and len(sys.argv) > 2:   # never team.lock/team.pid: it may run beside a round
        close_by_hand(sys.argv[2:], cmd == "close-in-round")
    elif cmd == "watch":   # like close: no team.lock/team.pid
        watch()
    elif cmd == "chart" and len(sys.argv) > 2:
        bus.send("bt.chart.request", {"coin": sys.argv[2].upper(), "purpose": "manual"})
    else:
        sys.exit(__doc__)
    bus.flush()


if __name__ == "__main__":
    main()
