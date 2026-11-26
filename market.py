"""Data Keeper + Auto Short screen + Indicator bots. Reads CoinLake's ClickHouse (altcoin db), never writes.

ponytail: prices become float here — analytics/backtest only, nothing here is a ledger (rule 20 allows
analytics floats). Anything that later moves real money must use Decimal.
"""
import bisect
import datetime as dt
import json
import math
import os
import statistics
import urllib.parse
import urllib.request

FEE = 0.001          # Binance spot taker fee per buy
WINDOW = 104         # weeks per DCA window (2 years)
STEP = 4             # a new window every 4 weeks
WRAPPED = {"WBTC", "WBETH", "BETH", "BNSOL", "PAXG", "XAUT"}  # duplicates of other coins / gold, not a DCA pick


def ch(sql, **params):
    """Run a query; values go in as ClickHouse server-side parameters ({name:Type} in the SQL), never string-built."""
    qs = urllib.parse.urlencode({"default_format": "TabSeparated", **{f"param_{k}": v for k, v in params.items()}})
    req = urllib.request.Request(
        f"http://{os.environ['CLICKHOUSE_HOST']}:{os.environ.get('CLICKHOUSE_PORT', '8123')}/?{qs}",
        sql.encode(),
        {"X-ClickHouse-User": os.environ.get("CLICKHOUSE_USER", "default"),
         "X-ClickHouse-Key": os.environ.get("CLICKHOUSE_PASSWORD", "")})
    with urllib.request.urlopen(req, timeout=300) as r:
        return [line.split("\t") for line in r.read().decode().splitlines()]


USDT_SPOT = "SELECT symbol FROM altcoin.symbols FINAL WHERE market='spot' AND quote_asset='USDT'"


def load_symbols():
    return {s: {"base": b, "status": st} for s, b, st in ch(
        "SELECT symbol, base_asset, status FROM altcoin.symbols FINAL WHERE market='spot' AND quote_asset='USDT'")}


def load_daily(symbol=None):
    """symbol -> [(date, close)] from 15m spot candles (last closed 15m bar of each UTC day). One symbol or all USDT."""
    where = "symbol = {sym:String}" if symbol else f"symbol IN ({USDT_SPOT})"
    out = {}
    for sym, d, c in ch(f"""SELECT symbol, toString(toDate(open_time)) d, toString(argMax(close, open_time))
        FROM altcoin.ohlcv WHERE market='spot' AND interval='15m' AND {where}
        GROUP BY symbol, d ORDER BY symbol, d""", **({"sym": symbol} if symbol else {})):
        out.setdefault(sym, []).append((d, float(c)))
    return out


def weekly(days):
    """Sample every 7th daily close counting back from the latest day, so all live coins share week ends.
    Returns (closes, mayer) where mayer = close / 200-day SMA (None before 200 days).
    ponytail: missing days in the source shift sampling slightly; fine for 2-year DCA windows."""
    closes = [c for _, c in days]
    idx = list(range(len(closes) - 1, -1, -7))[::-1]
    prefix = [0.0]
    for c in closes:
        prefix.append(prefix[-1] + c)
    mayer = [closes[i] / ((prefix[i + 1] - prefix[i - 199]) / 200) if i >= 199 else None for i in idx]
    return [closes[i] for i in idx], mayer


def dca_windows(w, mult=None):
    """Every WINDOW-week DCA ending at len(w), len(w)-STEP, ... (newest first).
    Returns [(roi, worst_underwater)] — roi after FEE, valued at the last buy week's close."""
    out = []
    for end in range(len(w), WINDOW - 1, -STEP):
        units = invested = worst = 0.0
        for i in range(end - WINDOW, end):
            a = mult[i] if mult else 1.0
            units += a * (1 - FEE) / w[i]
            invested += a
            worst = min(worst, units * w[i] / invested - 1)
        out.append((units * w[end - 1] / invested - 1, worst))
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    g, l = sum(gains[:n]) / n, sum(losses[:n]) / n
    for gi, li in zip(gains[n:], losses[n:]):
        g, l = (g * (n - 1) + gi) / n, (l * (n - 1) + li) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def is_pegged(days):
    """Stablecoins / fiat tokens: a year of prices stuck in a narrow band near 1."""
    last = [c for _, c in days[-365:]]
    return 0.8 <= statistics.median(last) <= 1.3 and max(last) / min(last) < 1.25


def indicators(days, btc_days=None):
    closes = [c for _, c in days]
    w, mayer = weekly(days)
    price = closes[-1]
    rets = [math.log(b / a) for a, b in zip(w[-53:], w[-52:])]
    ind = {
        "price": price,
        "mayer": mayer[-1],
        "dd_from_ath": price / max(closes) - 1,
        "rsi_w": rsi(w[-60:]),
        "ret_30d": price / closes[-31] - 1 if len(closes) > 30 else None,
        "ret_365d": price / closes[-366] - 1 if len(closes) > 365 else None,
        "vol_1y": statistics.pstdev(rets) * math.sqrt(52) if len(rets) > 10 else None,
    }
    if btc_days and len(closes) > 90:
        b = [c for _, c in btc_days]
        ind["rs_btc_90d"] = (price / closes[-91]) / (b[-1] / b[-91]) - 1
    return ind


def chunk_medians(xs, k=6):
    size = len(xs) / k
    return [statistics.median(xs[round(i * size):round((i + 1) * size)]) for i in range(k)]


# Bi-Directional Trend-Following rules (ลุงจืด 2026-10-16: BI-DIRECTIONAL MKT-SWITCH A — Short + Long with Market Trend Regime)
# SHORT 1H levels -5 = research/loosen-rules-backtest-2026-10-25.md S_RSI-5+L_DIST2 (ลุงจืด 2026-10-25; bleed age 90 lost money there)
SHORT_RSI_OVER = 65.0                  # top 1H RSI cross-down level of every tier
SHORT_RSI_DOWN = 60.0                  # young downtrend <=45d or post-peak bleed >45d: 1h RSI(14) crosses down from >= 60
SHORT_RSI_NEW = 55.0                   # new listing <=7d (rolled over) or young downtrend <=30d: 1h RSI(14) crosses down from >= 55
SHORT_RSI_4H = 55.0                    # 4H pullback exhaustion in strict aligned downtrend: 4h RSI(14) crosses down from >= 55
SHORT_NEW_AGE = 7                      # listing age (days) for new listing rollover fade
SHORT_NEW_DD = -0.08                   # new listing <=7d must have pulled back >= 8% from peak (or be in 1D downtrend) to avoid shorting ATH pumps
SHORT_YOUNG_AGE = 30                   # listing age (days) for young downtrend fade at 1h RSI 55
SHORT_DOWN_AGE = 45                    # listing age (days) for young downtrend fade at 1h RSI 60
SHORT_STRICT_DD = -0.15                # required drawdown for 4H RSI<55 confluence in aligned daily downtrend (Close < SMA7 < SMA20)
SHORT_BLEED_DD = -0.20                 # required drawdown from highest daily close since listing for age > 45d (no age cap since 2026-11-08: ลุงจืด "ยืนยัน ใช้ E3", research/short_wide; was <= 60d)
LONG_RSI_BREAK = 55.0                  # 1h RSI(14) crosses up >= 55 for ATH Breakout Long
LONG_RSI_YOUNG = 50.0                  # 1h RSI(14) crosses up >= 50 (or 55) for Young Uptrend <=30d Long
LONG_RSI_MAX = 72.0                    # avoid chasing extreme overbought 1h candles on Long entry
# LONG distances = backtest config 4 x2 (research/loosen-rules-backtest-2026-10-25.md S_RSI-5+L_DIST2, ลุงจืด 2026-10-25)
LONG_ATH_DD = -0.08                    # within 8% of all-time high daily close for ATH Price Discovery Long
LONG_YOUNG_DD = -0.12                  # within 12% of all-time high daily close for Young Uptrend <=30d Long
LONG_90D_DD = -0.06                    # 90d Breakout Long: within 6% of the 90-day high daily close ...
LONG_90D_ATH_DD = -0.30                # ... and within 30% of the all-time high daily close
LONG_ON = False                        # ลุงจืด 2026-11-08 "ยืนยัน": SHORT only (E0, D:/tread2/research/short_wide); LONGs already held still exit via team.rule_exit
GOLD_PERPS = {"PAXGUSDT", "XAUTUSDT"}  # tokenized gold contracts excluded from crypto trend trading
SHORT_MIN_FUNDING = -0.003             # minimum funding per 8h of holding (-0.30%): reject crowded shorts paying heavy negative funding
SHORT_MIN_FUNDING_DAY = SHORT_MIN_FUNDING * 3   # same gate per day (-0.90%) = backtest's daily-sum gate f_day >= -0.009
SHORT_MAX_R7_OLD = 0.30                # squeeze filter reference
SHORT_MIN_VOL = 5_000_000              # 30-day geometric-mean daily quote volume, USDT
FAPI_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FAPI_FUNDING = "https://fapi.binance.com/fapi/v1/premiumIndex"
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FAPI_FUNDING_INFO = "https://fapi.binance.com/fapi/v1/fundingInfo"   # lists only coins whose interval isn't the default 8h
FAPI_FUNDING_HIST = "https://fapi.binance.com/fapi/v1/fundingRate"

# Live Strategy #1 (ลุงจืด 2026-11-14 "1 ทำเลย เงินจริง"): 1d | LOWVOL_BREAKDOWN | H_DELTA0 (d:/tread2/research/short_v2/lowvol_breakdown.py)
BLACKLIST_PERPS = {
    "USDCUSDT", "BUSDUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT",
    "DAIUPERP", "DAIUSDT", "EURUSDT", "PAXGUSDT", "XAUTUSDT",
    "BTCDOMUSDT", "DEFIUSDT", "USDEUSDT", "USD1USDT",
}
LV_MIN_AGE_DAYS = 30                   # >= 30 full UTC days listed before t_eval
LV_MIN_BARS_1D = 55                    # >= 55 consecutive daily bars for SMA50 + 20-bar support lookback
LV_MIN_VOL_1H = 200_000.0              # >= $200k/h mean quote volume over past 168h ($4.8M/day)
LV_VOL_PCTL = 66.67                    # bottom 2/3 of 14-day daily return volatility across Base_Universe
LV_MAX_DR7 = 0.15                      # max single daily return in past 7 days <= +15% (anti-pump)
LV_MIN_F24 = -0.0003                   # past 24h funding sum >= -0.03% (anti-crowded-short)
LV_MIN_F7D = -0.0010                   # past 7d funding sum >= -0.10%
LV_MAX_REL14 = -0.05                   # 14d return minus BTC 14d return <= -5% (true laggard)
LV_MAX_DROP_BAR = 0.08                 # breakdown bar drop <= 8% ((c[-2] - c[-1]) / c[-2] <= 0.08)
LV_STOP_MIN, LV_STOP_MAX = 0.10, 0.35  # initial stop = clip(3 * ATR20 / entry, 10%, 35%)
LV_TP_DROP = 0.30                      # TP at -30% (entry * 0.70)
LV_RATCHET_PROFIT = 0.15               # 1d close profit >= +15% ratchets stop to entry * 0.96 (+4% locked)
LV_RATCHET_STOP = 0.96
LV_MAX_HOLD_DAYS = 21                  # LV_TF "1d": max hold 21 days
DAILY_ENTRY_HOURS_UTC = (0, 1)         # LV_TF "1d": 1d close trades enter in the 00:00-01:59 UTC window (07:00-08:59 TH)
LV_TF = "1h"                           # ลุงจืด 2026-11-22 "ทำงานทุก ชม. หาเหรียญที่ short ได้ เล่น 24/7" -> "สัญญาณแท่ง 1 ชม.": research cell
                                       # 1h | LOWVOL_BREAKDOWN (TF_CFG["1h"] = 3600 s, 168 bars): the breakdown is read on the 1h bar that
                                       # just closed and opens any hour; "1d" = the daily-close rule (H_DELTA0 ran live on "1d" only)
LV_MIN_BARS_1H = 55                    # LV_TF "1h": >= 55 1h bars (research `n < 55`), the last two back to back with all 4 candles
LV_MAX_HOLD_HOURS = 168                # LV_TF "1h": max hold 168 1h bars (7 days)
LV_SHORT_ONLY = True                   # ลุงจืด 2026-11-22 "เล่นเฉพาะ short": research mode G_BREADTH — no BTC LONG hedge leg, opens on gated days only
LV_GATE_MIN_BASE = 25                  # G_BREADTH gate on the signal day (lowvol_basket.py:206): Base_Universe >= 25 coins,
LV_GATE_MAX_BREADTH = 0.45             # < 45% of them closing above their own SMA20,
LV_GATE_MAX_BTC_RET7 = 0.10            # and BTC 7d return < +10%


def lv_signal():
    """Signal label of a live setup = the research mode it trades."""
    return f"{LV_TF.upper()}-LowVolBrk ({'G_BREADTH' if LV_SHORT_ONLY else 'H_DELTA0'})"


def ema(xs, period):
    """Exponential moving average of `xs` with given `period`, or None if fewer than `period` bars."""
    if len(xs) < period:
        return None
    k = 2.0 / (period + 1.0)
    val = sum(xs[:period]) / period
    for x in xs[period:]:
        val = x * k + val * (1.0 - k)
    return val


def market_regime(hourly, daily=None):
    """Real-time Market Trend Regime from BTCUSDT 1H EMA(21)/EMA(50), Daily SMA20, and 24h Hourly Market Breadth."""
    _, btc_hc = hourly.get("BTCUSDT", ([], []))
    e21, e50 = ema(btc_hc, 21), ema(btc_hc, 50)
    up_cnt, tot_cnt = 0, 0
    for _, hc in hourly.values():
        if len(hc) >= 25 and hc[-25] > 0:
            tot_cnt += 1
            if hc[-1] > hc[-25]:
                up_cnt += 1
    breadth = (up_cnt / tot_cnt) if tot_cnt > 0 else 0.0
    btc_d_up = True
    if daily and "BTCUSDT" in daily and len(daily["BTCUSDT"][1]) >= 20:
        btc_dc = daily["BTCUSDT"][1]
        btc_sma20 = sum(btc_dc[-20:]) / 20
        btc_d_up = bool(btc_dc[-1] > btc_sma20 and (not btc_hc or btc_hc[-1] > btc_sma20))
    elif len(btc_hc) >= 168:
        btc_d_up = bool(btc_hc[-1] > sum(btc_hc[-168:]) / 168)
    up_fast = bool(e21 is not None and e50 is not None and btc_hc[-1] > e21 > e50)
    btc_up = up_fast and btc_d_up
    if not btc_hc:
        return {"btc_up_1h": False, "breadth_1h": 0.0, "allow_long": False, "allow_short": True,
                "mkt_trend": "ขาลง/พักตัว (เน้นเปิด SHORT)"}
    allow_long = bool(btc_up and breadth >= 0.50)            # backtest config 4 + BTC Daily > SMA20 (kept by ลุงจืด)
    allow_short = not bool(up_fast and breadth > 0.55)       # backtest config 4: BTC 1H only
    # 2026-11-10 research/loss_study/regime: E3 shorts opened while BTC's last daily close sat under its 200-day mean lost
    # in TRAIN and TEST (-1.5% / -2.1% a trade; 13 of 39 months positive), so no new SHORT there. Under 200 days of data: no gate.
    btc_dc = daily["BTCUSDT"][1] if daily and "BTCUSDT" in daily else []
    under_200d = len(btc_dc) >= 200 and btc_dc[-1] < sum(btc_dc[-200:]) / 200
    short_pause = ("BTC ปิดรายวันต่ำกว่าค่าเฉลี่ย 200 วัน (สถิติย้อนหลัง SHORT ช่วงนี้ขาดทุน)" if under_200d else
                   "" if allow_short else "เทรนด์ตลาดเป็นขาขึ้นแข็งแกร่ง (พักเปิด SHORT สวนตลาด)")
    allow_short = allow_short and not under_200d
    if allow_long and not allow_short:
        trend_th = f"🟢 ตลาดขาขึ้นแข็งแกร่ง (BTC 1H EMA21>50 · ตลาดบวก {breadth * 100:.0f}%) — เปิด LONG / พักเปิด SHORT ใหม่"
    elif allow_long:
        trend_th = f"🟢 ตลาดขาขึ้น (BTC 1H EMA21>50 · ตลาดบวก {breadth * 100:.0f}%) — เปิดได้ทั้ง LONG และ SHORT ตามเทรนด์"
    elif not allow_short:
        trend_th = f"🟡 BTC 1H ขาขึ้นแรงแต่ Daily ต่ำกว่า SMA20 (ตลาดบวก {breadth * 100:.0f}%) — พักเปิดทั้ง LONG และ SHORT ใหม่"
    else:
        btc_th = "BTC 1H ขาขึ้น" if up_fast else "BTC 1H พักตัว/ขาลง"
        trend_th = f"🔴 ตลาดขาลง/พักตัว ({btc_th} · ตลาดบวก {breadth * 100:.0f}%) — เปิด SHORT / งดเปิด LONG ใหม่"
    if under_200d:
        trend_th = f"🟡 {short_pause} — พักเปิด SHORT ใหม่" + (" / เปิด LONG ได้" if allow_long else "")
    if not LONG_ON:   # SHORT only: the switch only allows or pauses new SHORTs
        trend_th = trend_th.split(" — ")[0] + (" — เปิด SHORT ได้ (ปิดฝั่ง LONG)" if allow_short else " — พักเปิด SHORT ใหม่ (ปิดฝั่ง LONG)")
    return {"btc_up_1h": btc_up, "breadth_1h": round(breadth, 4), "allow_long": allow_long,
            "allow_short": allow_short, "short_pause": short_pause, "mkt_trend": trend_th}


def usdt_perps():
    """{symbol: base} for Binance USDⓈ-M USDT perpetuals on crypto coins that trade now (no stock/gold/index contracts)."""
    with urllib.request.urlopen(FAPI_INFO, timeout=30) as r:
        info = json.load(r)
    return {s["symbol"]: s["baseAsset"] for s in info["symbols"] if s.get("underlyingType") == "COIN"
            and s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}


def perp_funding():
    """{symbol: funding per day} = lastFundingRate x 24 / the coin's funding interval hours (Binance public premiumIndex +
    fundingInfo, unlisted = 8h), the unit of the backtest's daily funding sum; empty dict on error so screening never crashes."""
    try:
        with urllib.request.urlopen(FAPI_FUNDING_INFO, timeout=20) as r:
            hours = {x["symbol"]: int(x["fundingIntervalHours"]) for x in json.load(r)
                     if isinstance(x, dict) and x.get("symbol") and x.get("fundingIntervalHours")}
        with urllib.request.urlopen(FAPI_FUNDING, timeout=20) as r:
            return {x["symbol"]: float(x["lastFundingRate"]) * 24 / hours.get(x["symbol"], 8) for x in json.load(r)
                    if isinstance(x, dict) and x.get("symbol") and x.get("lastFundingRate") is not None}
    except Exception:  # noqa: BLE001
        return {}


def load_perp_daily(symbols, days=450):
    """symbol -> (dates, closes, quote volumes) of completed UTC days from 15m futures candles, de-duplicated per bar,
    built like research/short_backtest.load(). Plus each symbol's newest bar time (is its yesterday complete?) and
    symbol -> daily highs (same days)."""
    out, newest, high = {}, {}, {}
    for sym, d, c, v, last, h in ch("""SELECT symbol, toString(d), toString(argMax(c, t)), toString(sum(qv)), toString(max(t)),
        toString(max(hi)) FROM (
        SELECT symbol, open_time t, toDate(open_time) d, any(close) c, any(quote_volume) qv, any(high) hi FROM altcoin.ohlcv
        WHERE market='um_futures' AND interval='15m' AND symbol IN {syms:Array(String)}
          AND open_time >= toStartOfDay(now()) - INTERVAL {days:UInt32} DAY AND open_time < toStartOfDay(now())
        GROUP BY symbol, open_time) GROUP BY symbol, d ORDER BY symbol, d""", syms=str(sorted(symbols)), days=days):
        o = out.setdefault(sym, ([], [], []))
        o[0].append(dt.date.fromisoformat(d))
        o[1].append(float(c))
        o[2].append(float(v))
        high.setdefault(sym, []).append(float(h))
        newest[sym] = last   # days come oldest first: ends as the symbol's newest bar
    return out, newest, high


def load_perp_hourly(symbols, days=14):
    """symbol -> ([hour 'YYYY-MM-DD HH:00:00'], [closes]) of completed UTC hours from 15m futures candles, plus each
    symbol's newest 15m bar time (is its last hour complete?)."""
    out, newest = {}, {}
    for sym, h, c, last in ch("""SELECT symbol, toString(h), toString(argMax(c, t)), toString(max(t)) FROM (
        SELECT symbol, open_time t, toStartOfHour(open_time) h, any(close) c FROM altcoin.ohlcv
        WHERE market='um_futures' AND interval='15m' AND symbol IN {syms:Array(String)}
          AND open_time >= toStartOfHour(now()) - INTERVAL {days:UInt32} DAY AND open_time < toStartOfHour(now())
        GROUP BY symbol, open_time) GROUP BY symbol, h ORDER BY symbol, h""", syms=str(sorted(symbols)), days=days):
        o = out.setdefault(sym, ([], []))
        o[0].append(h)
        o[1].append(float(c))
        newest[sym] = last
    return out, newest


def segment_start(d, c):
    """Index where the current contract's history starts: a x20 jump / fall to 1/20 across a 3+ day gap is a relisted
    contract, not one series (research/short_backtest.segments)."""
    cut = [i for i in range(1, len(c)) if c[i] / c[i - 1] > 20 or (c[i] / c[i - 1] < 0.05 and (d[i] - d[i - 1]).days >= 3)]
    return cut[-1] if cut else 0


def day_trend(p, sma7, sma20):
    """Thai label of a price against its daily SMA7/SMA20."""
    if p < sma7 and p < sma20:
        return "ขาลง (ต่ำกว่า SMA7/20)"
    if p < sma20:
        return "ย่อตัว (ต่ำกว่า SMA20)"
    if p < sma7:
        return "ย่อสั้น (ต่ำกว่า SMA7)"
    return "ขาขึ้น (เหนือ SMA7/20)"


def short_row(sym, base, d, c, v, hc=(), funding=None, mkt_regime=None, h4_close=True, hi=None):
    """One coin's signals on the last completed 1h/4h bar under research/long_short_trend_backtest.py config 4 (LONG or
    SHORT). Entry tiers judge the day by yesterday's close c[-1] and the highest daily high `hi` (closes if missing), like
    the backtest; trend_1d / dd_ath / downtrend_1d / r7 stay real-time from the last 1h close for the AI exit team.
    h4_close: hc[-1] is the last hour of a UTC 4H bar (00/04/08.. +3h); the 4H RSI only counts then, like the backtest."""
    a = segment_start(d, c)
    d, c, v = d[a:], c[a:], v[a:]
    hi = hi[a:] if hi else c
    i = len(c) - 1
    w = min(30, i + 1)
    w20 = min(20, i + 1)
    w7 = min(7, i + 1)
    sma20 = sum(c[-w20:]) / w20
    sma7 = sum(c[-w7:]) / w7
    live_hc = bool(hc and c and len(hc) >= 30 and 0.4 * c[i] <= hc[-1] <= 2.5 * c[i])
    cur_p = hc[-1] if live_hc else c[i]
    downtrend_1d = cur_p < sma7 and cur_p < sma20
    peak_c = max(max(c), max(hc)) if live_hc else (max(c) if c else 0.0)
    dd_ath = cur_p / peak_c - 1.0 if peak_c > 0 else 0.0
    r7 = cur_p / c[i - 7] - 1 if i >= 7 else None
    p = c[i]                                  # backtest day context: yesterday's close
    down_g, up_g = p < sma7 and p < sma20, p > sma7 and p > sma20
    dd_hi, dd_c, dd_c90 = p / max(hi) - 1.0, p / max(c) - 1.0, p / max(c[-90:]) - 1.0
    age_days = (d[i] - d[0]).days + 1   # counts today, like research/long_short_trend_backtest.py (d[i] is yesterday)
    rsi_prev, rsi_now = rsi(hc[:-1]), rsi(hc)
    hc4 = [hc[j] for j in range((len(hc) - 1) % 4, len(hc), 4)] if len(hc) >= 61 else []
    rsi4_prev, rsi4_now = (rsi(hc4[:-1]), rsi(hc4)) if len(hc4) >= 16 else (None, None)
    cross4h_55 = h4_close and rsi4_prev is not None and rsi4_now is not None and rsi4_prev >= SHORT_RSI_4H and rsi4_now < SHORT_RSI_4H
    e21_1h, e50_1h = ema(hc, 21), ema(hc, 50)
    up_1h = bool(e21_1h is not None and e50_1h is not None and hc[-1] > e21_1h > e50_1h)
    pegged = is_pegged(list(zip(d, c)))

    def new_row(side, signal, score):
        row = {"symbol": sym, "base": base, "side": side, "price": hc[-1] if hc else c[i], "age_days": age_days,
               "vol30_usd": math.exp(sum(math.log(x + 1) for x in v[-w:]) / w) - 1,
               "r7": r7, "dd_ath": dd_ath, "trend_1d": day_trend(cur_p, sma7, sma20), "downtrend_1d": downtrend_1d,
               "rsi_prev": rsi_prev, "rsi": rsi_now, "rsi4_prev": rsi4_prev, "rsi4": rsi4_now,
               "funding": funding, "score": round(score, 4), "signal": signal, "fail": []}
        if pegged:
            row["fail"].append("stablecoin")
        if row["vol30_usd"] < SHORT_MIN_VOL:
            row["fail"].append(f"volume 30 วัน ${row['vol30_usd'] / 1e6:.1f}M < ${SHORT_MIN_VOL / 1e6:.0f}M")
        return row

    # LONG (backtest: after 24 hourly bars, 1H RSI crosses up from above 25; kept: RSI <= 72)
    long_row = None
    if LONG_ON and mkt_regime is not None and sym not in GOLD_PERPS and len(hc) >= 25 and rsi_prev is not None:
        x55 = 25.0 < rsi_prev <= LONG_RSI_BREAK < rsi_now <= LONG_RSI_MAX
        x50 = 25.0 < rsi_prev <= LONG_RSI_YOUNG < rsi_now <= LONG_RSI_MAX
        if x55 and up_g and dd_c >= LONG_ATH_DD:
            long_tier, long_target, long_bonus = "ATH-Breakout", LONG_RSI_BREAK, 95.0
        elif x55 and p > sma7 >= sma20 and dd_c90 >= LONG_90D_DD and dd_c >= LONG_90D_ATH_DD:
            long_tier, long_target, long_bonus = "90d-Breakout", LONG_RSI_BREAK, 80.0
        elif (x50 or x55) and age_days <= SHORT_YOUNG_AGE and dd_c >= LONG_YOUNG_DD and (up_g or up_1h):
            long_tier, long_target, long_bonus = "YoungUptrend≤30d", LONG_RSI_YOUNG, 85.0
        else:
            long_tier = None
        if long_tier:
            r7_g = p / c[i - 7] - 1 if i >= 7 else 0.0
            long_row = new_row("LONG", f"{long_tier} (LONG RSI>{long_target:.0f})", long_bonus + rsi_now + r7_g * 30.0)
            if not mkt_regime.get("allow_long", False) and not pegged:
                long_row["fail"].append("เทรนด์ตลาดรวมยังไม่เป็นขาขึ้น (งดเปิด LONG สวนตลาด)")

    # SHORT: Bear Trend Pullback Exhaustion (any age; > 45d only past a -20% drawdown)
    target = tier = None
    if age_days <= SHORT_NEW_AGE and (down_g or dd_hi <= SHORT_NEW_DD):
        target, tier = SHORT_RSI_NEW, "NewListing≤7d"
    elif age_days <= SHORT_YOUNG_AGE and down_g:
        target, tier = SHORT_RSI_NEW, "YoungDown≤30d"
    elif age_days <= SHORT_DOWN_AGE and down_g:
        target, tier = SHORT_RSI_DOWN, "YoungDown≤45d"
    elif down_g and dd_hi <= SHORT_BLEED_DD:
        target, tier = SHORT_RSI_DOWN, "PostPeakBleed"

    targets = [t for t in (SHORT_RSI_OVER, SHORT_RSI_DOWN, SHORT_RSI_NEW) if target is not None and t >= target]
    crossed_1h = [t for t in targets if rsi_prev is not None and rsi_now is not None and rsi_prev >= t and rsi_now < t]
    crossed_4h = (tier not in (None, "NewListing≤7d") and cross4h_55 and p < sma7 <= sma20  # kept: dd <= -15%
                  and dd_hi <= SHORT_STRICT_DD)
    if tier is None:
        signal = f"นอกเกณฑ์อายุ/เทรนด์ ({age_days}d)"
    elif crossed_4h and not crossed_1h:
        signal = f"{tier} (4H-RSI<{SHORT_RSI_4H:.0f})"
    else:
        signal = f"{tier} (RSI<{target:.0f})"
    r4p = rsi4_prev if h4_close and rsi4_prev is not None else 0.0
    score = (100.0 if age_days <= SHORT_NEW_AGE else 50.0) - min(age_days, 60) * 1.5 - dd_hi * 20.0 + max(rsi_prev or 0.0, r4p)

    row = new_row("SHORT", signal, score)
    if sym in GOLD_PERPS:   # the backtest universe never had them (lrb.load); the 60-day cap hid them until 2026-11-08
        row["fail"].append("สัญญาทองคำ (ไม่เทรดตามเทรนด์คริปโต)")
    if funding is not None and funding < SHORT_MIN_FUNDING_DAY and not pegged:
        row["fail"].append(f"funding {funding * 100:+.2f}%/วัน < {SHORT_MIN_FUNDING_DAY * 100:+.2f}%/วัน (คนรุม short แน่น เสี่ยง squeeze)")
    if mkt_regime is not None and not mkt_regime.get("allow_short", True) and not pegged:
        row["fail"].append(mkt_regime.get("short_pause") or "เทรนด์ตลาดเป็นขาขึ้นแข็งแกร่ง (พักเปิด SHORT สวนตลาด)")
    if tier is None and not pegged:
        if age_days <= SHORT_NEW_AGE:
            row["fail"].append(f"เหรียญใหม่ {age_days} วัน ยังไม่เริ่มขาลง (ย่อจากยอด {dd_hi * 100:+.0f}% ต้อง ≤ -8% หรือต่ำกว่า SMA7/20)")
        elif not down_g:
            row["fail"].append(f"กราฟ Day (ราคาปิดเมื่อวาน) ยังไม่เป็นขาลงเต็มตัว ({day_trend(p, sma7, sma20)})")
        else:
            row["fail"].append(f"อายุ {age_days} วัน (> {SHORT_DOWN_AGE}) ยังย่อจากยอด {dd_hi * 100:+.0f}% ไม่ถึง -20%")
    if rsi_prev is None or rsi_now is None:
        row["fail"].append("แท่ง 1 ชม. ไม่พอคำนวณ RSI(14)")
    elif age_days <= 2 and len(hc) < 25:   # backtest bar_i >= 24: never short the first day's wild hours
        row["fail"].append(f"เข้าซื้อขายได้ {len(hc)} ชม. (SHORT ต้องรอครบ 24 ชม.)")
    elif tier is not None and not crossed_1h and not crossed_4h:
        if rsi_prev < target:
            row["fail"].append(f"RSI(14) 1 ชม. แท่งก่อน {rsi_prev:.1f} < {target:.0f}")
        else:
            t_hi = max(t for t in targets if rsi_prev >= t)
            row["fail"].append(f"RSI(14) 1 ชม. ล่าสุด {rsi_now:.1f} ≥ {t_hi:.0f} (ยังไม่ตัดลง)")
    if long_row and (row["fail"] or (not long_row["fail"] and long_row["score"] >= row["score"])):
        return long_row
    return row


def ever_liquid(d, c, v):
    """In the backtest's universe (liquid_syms): not pegged, and the current contract's 30-day geometric-mean daily volume
    reached $5M on some day."""
    if len(c) < 5 or is_pegged(list(zip(d, c))):
        return False
    lv = [0.0]
    for x in v[segment_start(d, c):]:
        lv.append(lv[-1] + math.log(x + 1))
    return any(math.exp((lv[j] - lv[max(0, j - 30)]) / min(30, j)) - 1 >= SHORT_MIN_VOL for j in range(1, len(lv)))


def legacy_short_screen(now=None):
    """Every tradable USDT perp scored by the Bi-Directional Trend-Following rule (LONG + SHORT + Market Regime) on the last
    completed UTC hour, ranked by composite Trend Score (then youngest age, highest previous RSI). A perp whose last hour
    isn't complete in ClickHouse (last 15m bar < HH:45) fails. Each hour is judged by the day before its UTC day, like the
    backtest (so 23:00 still uses the day before yesterday's close at 00:05).
    meta["entry_day"] waits for all live perps until 15 min past the hour, then goes on without the late ones."""
    now = now or dt.datetime.now(dt.timezone.utc)
    perps = usdt_perps()
    daily, _, highs = load_perp_daily(perps)
    hourly, newest = load_perp_hourly(perps)
    fund = perp_funding()
    last_h = now.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    last_hour, day_before = f"{last_h:%Y-%m-%d %H:00:00}", last_h.date() - dt.timedelta(days=1)
    data_to = last_hour[:16]
    daily = {s: tuple(x[:bisect.bisect_right(d, day_before)] for x in (d, c, v)) for s, (d, c, v) in daily.items()}
    breadth_set = {s: hourly[s] for s in hourly if hourly[s][0][-1] == last_hour and s not in GOLD_PERPS
                   and s in daily and ever_liquid(*daily[s])}
    regime = market_regime(breadth_set, daily)
    if not fund:   # Binance funding unreadable: the funding gate would pass every coin, so no new SHORT this round
        regime.update(allow_short=False, short_pause="อ่าน funding จาก Binance ไม่ได้ (รอบนี้พักเปิด SHORT)",
                      mkt_trend=regime["mkt_trend"].split(" — ")[0] + " — พักเปิด SHORT รอบนี้ (อ่าน funding จาก Binance ไม่ได้)")
    live = [s for s in daily if daily[s][0] and daily[s][0][-1] == day_before and s in hourly and hourly[s][0][-1] == last_hour]
    late = {s for s in live if newest[s] < f"{last_hour[:13]}:45"}
    rows = []
    for s in live:
        d, c, v = daily[s]
        start = str(d[segment_start(d, c)])
        hc = [px for h, px in zip(*hourly[s]) if h[:10] >= start]
        hi = highs.get(s)
        rows.append(short_row(s, perps[s], d, c, v, hc, fund.get(s), regime, int(last_hour[11:13]) % 4 == 3, hi and hi[:len(d)]))
    rows.sort(key=lambda r: (-r.get("score", 0.0), r["age_days"], -(r["rsi_prev"] or 0.0), r["symbol"]))
    for r in rows:
        if r["symbol"] in late:
            r["fail"].append(f"ข้อมูลชั่วโมง {data_to} UTC ใน ClickHouse ยังไม่ครบ (ถึง {newest[r['symbol']][11:16]} UTC)")
    return rows, {"universe": len(rows), "data_to": data_to, "late": sorted(late),
                  "entry_day": bool(live) and (not late or now.minute >= 15), **regime}



def percentile_linear(xs, q):
    """Exact match for numpy.percentile(xs, q, method='linear') without requiring numpy."""
    s = sorted(xs)
    if not s:
        return 0.0
    if len(s) == 1:
        return float(s[0])
    pos = (len(s) - 1) * (q / 100.0)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    return float(s[lo] + (pos - lo) * (s[hi] - s[lo]))


def causal_ema_series(xs, period=20):
    """Causal EMA series seeded with SMA(period) at index period-1, matching rw_hedge.causal_ema."""
    n = len(xs)
    out = [None] * n
    if n < period:
        return out
    prev = sum(xs[:period]) / period
    out[period - 1] = prev
    alpha = 2.0 / (period + 1.0)
    for i in range(period, n):
        prev = xs[i] * alpha + prev * (1.0 - alpha)
        out[i] = prev
    return out


def daily_tf_indicators(hi, lo, c):
    """Causal 1d indicators matching lowvol_breakdown.compute_tf_indicators: (sma50, ema20, atr20, sup20_now, sup20_prev)."""
    n = len(c)
    sma50 = sum(c[-50:]) / 50.0 if n >= 50 else None
    ema20 = causal_ema_series(c, 20)[-1] if n >= 20 else None
    atr20 = None
    if n >= 20:
        trs = [hi[0] - lo[0]]
        for k in range(1, n):
            trs.append(max(hi[k] - lo[k], abs(hi[k] - c[k - 1]), abs(lo[k] - c[k - 1])))
        atr20 = sum(trs[-20:]) / 20.0
    sup20_now = min(lo[-21:-1]) if n >= 21 else None
    sup20_prev = min(lo[-22:-2]) if n >= 22 else None
    return sma50, ema20, atr20, sup20_now, sup20_prev


def load_perp_daily_ohlcv(symbols, days=450, before_day=None):
    """symbol -> (dates, opens, highs, lows, closes, quote_vols, bar_counts) of completed UTC days from 15m futures candles,
    plus newest 15m open_time string per symbol."""
    end_expr = "toDateTime({before:String}, 'UTC')" if before_day else "toStartOfDay(now())"
    params = {"syms": str(sorted(symbols)), "days": days}
    if before_day:
        params["before"] = f"{before_day.isoformat()} 00:00:00"
    out, newest = {}, {}
    for sym, d, o, hi, lo, c, v, last, cnt in ch(f"""SELECT symbol, toString(d), toString(argMin(o, t)), toString(max(hi)),
        toString(min(lo)), toString(argMax(c, t)), toString(sum(qv)), toString(max(t)), toString(count()) FROM (
        SELECT symbol, open_time t, toDate(open_time) d, any(open) o, any(high) hi, any(low) lo, any(close) c, any(quote_volume) qv
        FROM altcoin.ohlcv
        WHERE market='um_futures' AND interval='15m' AND symbol IN {{syms:Array(String)}}
          AND open_time >= {end_expr} - INTERVAL {{days:UInt32}} DAY AND open_time < {end_expr}
        GROUP BY symbol, open_time) GROUP BY symbol, d ORDER BY symbol, d""", **params):
        rec = out.setdefault(sym, ([], [], [], [], [], [], []))
        rec[0].append(dt.date.fromisoformat(d))
        rec[1].append(float(o))
        rec[2].append(float(hi))
        rec[3].append(float(lo))
        rec[4].append(float(c))
        rec[5].append(float(v))
        rec[6].append(int(cnt))
        newest[sym] = last
    return out, newest


def load_perp_hourly_qv(symbols, days=10):
    """symbol -> ([hour 'YYYY-MM-DD HH:00:00'], [closes], [quote_volumes], [highs], [lows], [15m candle counts]) of completed UTC
    hours from 15m futures candles, plus newest 15m open_time string per symbol."""
    out, newest = {}, {}
    for sym, h, c, qv, last, hi, lo, cnt in ch("""SELECT symbol, toString(h), toString(argMax(c, t)), toString(sum(qv)), toString(max(t)),
        toString(max(hi)), toString(min(lo)), toString(count()) FROM (
        SELECT symbol, open_time t, toStartOfHour(open_time) h, any(close) c, any(quote_volume) qv, any(high) hi, any(low) lo
        FROM altcoin.ohlcv
        WHERE market='um_futures' AND interval='15m' AND symbol IN {syms:Array(String)}
          AND open_time >= toStartOfHour(now()) - INTERVAL {days:UInt32} DAY AND open_time < toStartOfHour(now())
        GROUP BY symbol, open_time) GROUP BY symbol, h ORDER BY symbol, h""", syms=str(sorted(symbols)), days=days):
        rec = out.setdefault(sym, ([], [], [], [], [], []))
        rec[0].append(h)
        rec[1].append(float(c))
        rec[2].append(float(qv))
        rec[3].append(float(hi))
        rec[4].append(float(lo))
        rec[5].append(int(cnt))
        newest[sym] = last
    return out, newest


def _funding_pages(start_ms, end_ms):
    """Every Binance USDT-perp funding record with start_ms <= fundingTime <= end_ms as (symbol, time_ms, rate), 1000 a call.
    The next page starts AT the last record's time and repeats are dropped: hundreds of perps settle in the same
    millisecond, so starting 1 ms later would lose the ones a page cut off. A full page with nothing new (1000+ records
    in one millisecond) or 80 pages without reaching the end raises: a short list would read as "funding is fine"."""
    cur, seen, out = start_ms, set(), []
    for _ in range(80):
        q = urllib.parse.urlencode({"startTime": cur, "endTime": end_ms, "limit": 1000})
        req = urllib.request.Request(f"{FAPI_FUNDING_HIST}?{q}", headers={"User-Agent": "bottread-short"})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.load(r)
        new = 0
        for x in batch or ():
            key = (x.get("symbol"), int(x.get("fundingTime", 0)))
            if key[0] and key[1] <= end_ms and key not in seen:
                seen.add(key)
                out.append((key[0], key[1], float(x.get("fundingRate", 0.0))))
                new += 1
        if len(batch or ()) < 1000:
            return out
        if not new:
            break
        cur = max(cur, int(batch[-1]["fundingTime"]))
    raise RuntimeError("funding history: paging did not reach the end")


FUND_REREAD_MS = 120_000
_FUND = {}   # {"lo", "hi", "rec"}: the funding records this process has read, complete for lo..hi (ms)


def funding_history(start_ms, end_ms):
    """_funding_pages(start_ms, end_ms), read once per process. A 7-day read is ~42 calls and a round screens again every
    30 s while ClickHouse lacks the hour's last candle, against Binance's 500 calls per 5 min per IP (shared with
    fundingInfo): the records are kept and a later read asks only for what follows them, plus their last 2 minutes again
    (a settlement's records can show up a few seconds late). A read that fails leaves what is kept as it was."""
    known = bool(_FUND) and _FUND["lo"] <= start_ms <= _FUND["hi"]
    frm = max(start_ms, _FUND["hi"] - FUND_REREAD_MS) if known else start_ms
    fresh = _funding_pages(frm, end_ms) if frm <= end_ms else []
    if not known:
        _FUND.update(lo=start_ms, hi=end_ms, rec={})
    _FUND["rec"].update(((s, t), r) for s, t, r in fresh)
    _FUND["hi"] = max(_FUND["hi"], end_ms)
    return [(s, t, r) for (s, t), r in _FUND["rec"].items() if start_ms <= t <= end_ms]


def perp_funding_7d(t_eval_sec):
    """Fetch exact 7-day funding history up to t_eval_sec across all Binance USDT perps.
    Returns ({sym: (f24, f7d)}, ok_bool), matching rw_hedge.funding_window & lowvol_basket (t0_ms+60_000, t1_ms+60_000]."""
    end_ms = int(t_eval_sec) * 1000 + 60_000
    start_7d_ms = (int(t_eval_sec) - 7 * 86400) * 1000 + 60_001
    start_24h_ms = (int(t_eval_sec) - 86400) * 1000 + 60_000
    f24, f7d = {}, {}
    try:
        for sym, ts, rate in funding_history(start_7d_ms, end_ms):
            f7d[sym] = f7d.get(sym, 0.0) + rate
            if ts > start_24h_ms:
                f24[sym] = f24.get(sym, 0.0) + rate
        return {s: (f24.get(s, 0.0), f7d[s]) for s in f7d}, True
    except Exception:  # noqa: BLE001
        return {}, False


def perp_funding_24h(t_sec):
    """({sym: funding sum of the 24 h up to t_sec}, ok_bool): the real-time check of lowvol_breakdown.scan_symbol_tf_trades
    (`f24_rt`), same window (t0_ms+60_000, t1_ms+60_000]."""
    out = {}
    try:
        for sym, _, rate in funding_history((int(t_sec) - 86400) * 1000 + 60_001, int(t_sec) * 1000 + 60_000):
            out[sym] = out.get(sym, 0.0) + rate
        return out, True
    except Exception:  # noqa: BLE001
        return {}, False


def _dense_cf(days_grid, d_seg, c_seg):
    """Forward-filled daily closes on `days_grid` starting from d_seg[0], matching lowvol_basket Cf_day."""
    by_d = dict(zip(d_seg, c_seg))
    first_d = d_seg[0]
    out, prev = [], None
    for gd in days_grid:
        if gd < first_d:
            out.append(None)
            continue
        v = by_d.get(gd)
        if v is not None and math.isfinite(v):
            prev = v
        out.append(prev)
    return out


def lv_hourly_signal(hq, start_iso, vol_from_h, prev_h, sig_h):
    """LV_TF "1h": what lowvol_breakdown.scan_symbol_tf_trades reads on a coin's 1h bars up to the signal bar `sig_h`:
    n = bars of the current listing (from `start_iso`), ok = the signal bar and the one before are both there with all 4
    candles, c / c_prev = their closes, ind = daily_tf_indicators of the bars, vol_rt = mean hourly quote volume of the
    168 h ending with the signal bar (hours missing count as 0, like the research's cumulative sum)."""
    if len(hq) < 6:
        return {"n": 0, "ok": False, "c": None, "c_prev": None, "ind": (None,) * 5, "vol_rt": 0.0}
    bars = [x for x in zip(*hq[:6]) if start_iso <= x[0][:10] and x[0] <= sig_h]
    ok = (len(bars) >= 2 and bars[-1][0] == sig_h and bars[-2][0] == prev_h and bars[-1][5] == 4 and bars[-2][5] == 4)
    closes = [x[1] for x in bars]
    return {"n": len(bars), "ok": ok, "c": closes[-1] if bars else None, "c_prev": closes[-2] if len(bars) >= 2 else None,
            "ind": daily_tf_indicators([x[3] for x in bars], [x[4] for x in bars], closes),
            "vol_rt": sum(qv for h, qv in zip(hq[0], hq[2]) if vol_from_h <= h <= sig_h) / 168.0}


def lowvol_breakdown_eval(perps, daily_ohlcv, hourly_qv, fund_map, today_utc, newest_daily=None, fund_ok=True, now_utc=None, fund_rt=None):
    """Pure causal evaluator for `LOWVOL_BREAKDOWN`: mode G_BREADTH (SHORT only, opens on breadth-gated days) when
    LV_SHORT_ONLY, else H_DELTA0 (every SHORT hedged with BTC). The daily context (Base_Universe, anti-squeeze, rel14, the
    gate) is always the completed UTC day `today_utc - 1 day`. LV_TF "1d" reads the breakdown on that day's bar and opens in
    DAILY_ENTRY_HOURS_UTC. LV_TF "1h" reads it on the 1h bar that closed at the top of `now_utc`'s hour and opens any hour:
    `hourly_qv` then needs highs/lows/candle counts, `newest_daily` is the newest 15m candle per symbol and `fund_rt` the
    24h funding sum up to that hour. Matches d:/tread2/research/short_v2/lowvol_basket.py + lowvol_breakdown.py exactly."""
    tf1h = LV_TF == "1h"
    day_eval = today_utc - dt.timedelta(days=1)
    t_eval_dt = dt.datetime.combine(today_utc, dt.time(0, 0), tzinfo=dt.timezone.utc)
    # LV_TF "1h": the signal bar closed at t_close (research `t_close`); its daily context `d_prev` is day_eval for every hour of today
    t_close_dt = (now_utc.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
                  if tf1h and now_utc is not None else t_eval_dt)
    sig_h, prev_h = (f"{t_close_dt - dt.timedelta(hours=k):%Y-%m-%d %H}:00:00" for k in (1, 2))
    rt_from_h = f"{t_close_dt - dt.timedelta(hours=168):%Y-%m-%d %H}:00:00"
    want_bar = f"{t_close_dt - dt.timedelta(minutes=15):%Y-%m-%d %H:%M}" if tf1h else f"{day_eval.isoformat()} 23:45"
    h_end_str = f"{day_eval.isoformat()} 23:00:00"
    h_start_168_str = f"{(t_eval_dt - dt.timedelta(hours=168)):%Y-%m-%d %H:00:00}"
    days_grid = [day_eval - dt.timedelta(days=k) for k in range(40, -1, -1)]   # 41 days: [-1]=day_eval, [-15]=d-14, [-31]=d-30

    # BTC daily series for relative return (ret14 - btc_ret14) & breadth info
    btc_rec = daily_ohlcv.get("BTCUSDT")
    btc_ret14 = btc_ret7 = None
    if btc_rec and btc_rec[0]:
        bd, _, _, _, bc_all, _, _ = btc_rec
        cut_b = bisect.bisect_right(bd, day_eval)
        if cut_b > 0:
            bd_s, bc_s = bd[:cut_b], bc_all[:cut_b]
            a_b = segment_start(bd_s, bc_s)
            b_cf = _dense_cf(days_grid, bd_s[a_b:], bc_s[a_b:])
            if b_cf[-1] and b_cf[-15] and b_cf[-15] > 0:
                btc_ret14 = b_cf[-1] / b_cf[-15] - 1.0
            if b_cf[-1] and b_cf[-8] and b_cf[-8] > 0:
                btc_ret7 = b_cf[-1] / b_cf[-8] - 1.0

    # Pass 1: per-coin features & Base_Universe membership at day_eval
    feat = {}
    for sym, base in sorted(perps.items()):
        if sym == "BTCUSDT":
            continue
        rec = daily_ohlcv.get(sym)
        if not rec or not rec[0]:
            continue
        d_all, o_all, hi_all, lo_all, c_all, v_all, cnt_all = rec
        cut = bisect.bisect_right(d_all, day_eval)
        if cut == 0:
            continue
        d, o, hi, lo, c, v, cnt = (x[:cut] for x in (d_all, o_all, hi_all, lo_all, c_all, v_all, cnt_all))
        a = segment_start(d, c)
        d, o, hi, lo, c, v, cnt = d[a:], o[a:], hi[a:], lo[a:], c[a:], v[a:], cnt[a:]
        if not d:
            continue
        pegged = is_pegged(list(zip(d, c)))
        blacklisted = sym in BLACKLIST_PERPS or sym in GOLD_PERPS

        cf = _dense_cf(days_grid, d, c)
        dr = [0.0] * len(cf)
        for k in range(1, len(cf)):
            if cf[k - 1] is not None and cf[k - 1] > 0 and cf[k] is not None:
                dr[k] = cf[k] / cf[k - 1] - 1.0
        sma20_d = (sum(cf[-20:]) / 20.0) if all(x is not None for x in cf[-20:]) else None
        vol14 = statistics.pstdev(dr[-14:]) if all(x is not None for x in cf[-15:]) else None
        max_dr7 = max(dr[-7:]) if all(x is not None for x in cf[-8:]) else None
        ret14 = (cf[-1] / cf[-15] - 1.0) if (cf[-1] is not None and cf[-15] is not None and cf[-15] > 0) else None
        r7 = (cf[-1] / cf[-8] - 1.0) if (cf[-1] is not None and cf[-8] is not None and cf[-8] > 0) else None

        hq = hourly_qv.get(sym) or ((), (), ())
        hrs, hc_all, hqv_all = hq[0], hq[1], hq[2]
        start_iso = d[0].isoformat()
        hc = [px for h, px in zip(hrs, hc_all) if h[:10] >= start_iso]
        qv_168 = sum(qv for h, qv in zip(hrs, hqv_all) if h_start_168_str <= h <= h_end_str)
        # Fallback if hourly table holds < 7d (e.g. unit test fixture): use last 7 daily quote volumes / 168
        vol_168h = (qv_168 / 168.0) if hrs else (sum(v[-7:]) / 168.0 if len(v) >= 7 else 0.0)
        h23_ok = (any(h == h_end_str for h in hrs) if hrs else (d[-1] == day_eval))

        age_days = (today_utc - d[0]).days
        f24, f7d = fund_map.get(sym, (0.0, 0.0))
        bm_d = (
            not pegged
            and not blacklisted
            and d[-1] == day_eval
            and h23_ok
            and age_days >= LV_MIN_AGE_DAYS
            and vol_168h >= LV_MIN_VOL_1H
            and cf[-31] is not None
            and cf[-31] > 0.0
            and sma20_d is not None
            and vol14 is not None
        )
        feat[sym] = {
            "base": base, "d": d, "o": o, "hi": hi, "lo": lo, "c": c, "v": v, "cnt": cnt, "hc": hc,
            "pegged": pegged, "blacklisted": blacklisted, "cf": cf, "sma20_d": sma20_d,
            "vol14": vol14, "max_dr7": max_dr7, "ret14": ret14, "r7": r7, "vol_168h": vol_168h,
            "age_days": age_days, "f24": f24, "f7d": f7d, "bm_d": bm_d, "hq": hq, "start_iso": start_iso,
        }

    base_syms = [s for s, f in feat.items() if f["bm_d"]]
    vol_cut = percentile_linear([feat[s]["vol14"] for s in base_syms], LV_VOL_PCTL) if base_syms else 0.0
    breadth = (sum(1 for s in base_syms if feat[s]["cf"][-1] > feat[s]["sma20_d"]) / len(base_syms)) if base_syms else 0.0
    gate_ok = not LV_SHORT_ONLY or (len(base_syms) >= LV_GATE_MIN_BASE and breadth < LV_GATE_MAX_BREADTH
                                    and btc_ret7 is not None and btc_ret7 < LV_GATE_MAX_BTC_RET7)

    # Check ClickHouse completeness of the signal bar's last 15m candle (`want_bar`) for live coins
    late = set()
    if newest_daily:
        for s, f in feat.items():   # "1h": live = traded the hour before, so a coin delisted earlier today is not waited for every round
            live = (prev_h in f["hq"][0][-3:]) if tf1h else (f["d"][-1] == day_eval)
            if live and newest_daily.get(s, "") < want_bar:
                late.add(s)
        if btc_rec and newest_daily.get("BTCUSDT", "") < want_bar:
            late.add("BTCUSDT")

    now_h = now_utc.hour if now_utc is not None else 0
    now_m = now_utc.minute if now_utc is not None else 30
    in_window = tf1h or now_h in DAILY_ENTRY_HOURS_UTC
    ch_ready = bool(feat) and ("BTCUSDT" not in late) and (not late or now_m >= 15 or (now_h > 0 and not tf1h))
    waiting_ch = bool(late and now_m < 15) if tf1h else bool(in_window and late and not ch_ready)
    allow_short = bool(fund_ok and btc_ret14 is not None and gate_ok)
    entry_day = bool(in_window and ch_ready and allow_short)
    name_th = f"{LV_TF.upper()} Low-Vol Breakdown SHORT ล้วน" if LV_SHORT_ONLY else f"{LV_TF.upper()} Low-Vol Breakdown + BTC Hedge 1:1"
    open_th = "เปิด SHORT" if LV_SHORT_ONLY else "เปิดคู่ SHORT+BTC LONG"

    if not fund_ok:
        mkt_trend = "🟡 พักเปิดไม้รอบนี้ (อ่านประวัติ funding 7 วันจาก Binance ไม่ได้)"
    elif btc_ret14 is None:
        mkt_trend = "🟡 พักเปิดไม้รอบนี้ (ข้อมูลแท่งรายวัน BTCUSDT ใน ClickHouse ไม่ครบ)"
    elif entry_day:
        mkt_trend = (f"🟢 กลยุทธ์ {name_th} (ตลาดยืนเหนือ SMA20 {breadth * 100:.0f}% · BTC 14d {btc_ret14 * 100:+.1f}%) — "
                     + (f"{open_th} ได้ทุกชั่วโมง (สัญญาณจากแท่ง 1 ชม. ที่ปิด {t_close_dt:%H:%M} UTC)" if tf1h else f"รอบปิดแท่ง Day {open_th} ได้"))
    elif waiting_ch:
        mkt_trend = f"🟡 รอ ClickHouse เก็บแท่ง {want_bar} UTC ให้ครบก่อนเปิดไม้"
    elif not gate_ok:
        btc7_th = "ไม่มีข้อมูล" if btc_ret7 is None else f"{btc_ret7 * 100:+.1f}%"
        mkt_trend = (f"🟡 พักเปิดไม้ (ตัวกรองตลาด SHORT ล้วนไม่ผ่าน: ตลาดยืนเหนือ SMA20 {breadth * 100:.0f}% ต้อง < {LV_GATE_MAX_BREADTH * 100:.0f}% · "
                     f"BTC 7d {btc7_th} ต้อง < +{LV_GATE_MAX_BTC_RET7 * 100:.0f}% · Base Universe {len(base_syms)} ตัว ต้อง ≥ {LV_GATE_MIN_BASE})")
    elif tf1h:   # past HH:15 and BTCUSDT's last candle of the signal bar is still not in ClickHouse
        mkt_trend = f"🟡 พักเปิดไม้รอบนี้ (ClickHouse ยังไม่มีแท่ง {want_bar} UTC ของ BTCUSDT)"
    else:
        mkt_trend = (f"🔵 กลยุทธ์ {name_th} (ตลาดยืนเหนือ SMA20 {breadth * 100:.0f}% · "
                     f"BTC 14d {btc_ret14 * 100:+.1f}%) — เฝ้าดู/ตรวจออกทุก 15 นาที "
                     f"({'เปิดไม้ใหม่' if LV_SHORT_ONLY else 'เปิดคู่ใหม่'}เฉพาะรอบ 07:00–08:59 น. เวลาไทย)")

    rows = []
    for sym, f in feat.items():
        d, hi, lo, c, v, hc = f["d"], f["hi"], f["lo"], f["c"], f["v"], f["hc"]
        d_sma50, _, _, d_sup20, _ = d_ind = daily_tf_indicators(hi, lo, c)
        sg = lv_hourly_signal(f["hq"], f["start_iso"], rt_from_h, prev_h, sig_h) if tf1h else None
        # the bar the breakdown is read on: its close, the close before it, its indicators
        s_c, s_prev = (sg["c"], sg["c_prev"]) if tf1h else (c[-1], c[-2] if len(c) >= 2 else None)
        sma50, ema20, atr20, sup20, sup20_prev = sg["ind"] if tf1h else d_ind
        sup_th = "20 ชม." if tf1h else "20 วัน"
        f24_rt = (fund_rt or {}).get(sym, 0.0)
        cur_p = hc[-1] if (hc and 0.4 * c[-1] <= hc[-1] <= 2.5 * c[-1]) else c[-1]
        sma7 = sum(c[-min(7, len(c)):]) / min(7, len(c))
        sma20_cur = f["sma20_d"] if f["sma20_d"] is not None else sum(c[-min(20, len(c)):]) / min(20, len(c))
        peak_c = max(max(c), max(hc)) if hc else max(c)
        dd_ath = cur_p / peak_c - 1.0 if peak_c > 0 else 0.0
        rsi_prev, rsi_now = rsi(hc[:-1]), rsi(hc)
        hc4 = [hc[j] for j in range((len(hc) - 1) % 4, len(hc), 4)] if len(hc) >= 61 else []
        rsi4_prev, rsi4_now = (rsi(hc4[:-1]), rsi(hc4)) if len(hc4) >= 16 else (None, None)

        rel14 = (f["ret14"] - btc_ret14) if (f["ret14"] is not None and btc_ret14 is not None) else None
        consec_1d = len(d) >= 2 and (d[-1] - d[-2]).days == 1 and d[-1] == day_eval
        full_bars = len(f["cnt"]) >= 2 and f["cnt"][-1] >= 92 and f["cnt"][-2] >= 92

        fail = []
        if f["pegged"]:
            fail.append("stablecoin")
        if sym in GOLD_PERPS:
            fail.append("สัญญาทองคำ (ไม่เทรดตามเทรนด์คริปโต)")
        elif f["blacklisted"]:
            fail.append("สัญญาดัชนี/เงินตรา (อยู่นอกจักรวาลเหรียญ)")
        if not fund_ok:
            fail.append("อ่านประวัติ funding จาก Binance ไม่ได้")
        if btc_ret14 is None:
            fail.append("ข้อมูล BTCUSDT 14 วันไม่ครบ")
        if d[-1] != day_eval:
            fail.append(f"ไม่มีแท่ง Day {day_eval.isoformat()}")
        if f["age_days"] < LV_MIN_AGE_DAYS:
            fail.append(f"อายุสัญญา {f['age_days']} วัน < {LV_MIN_AGE_DAYS} วัน")
        if tf1h:
            if sg["n"] < LV_MIN_BARS_1H or not sg["ok"]:
                fail.append(f"แท่ง 1 ชม. ไม่ครบ ({sg['n']} แท่ง ต้อง ≥ {LV_MIN_BARS_1H} แท่ง และ 2 แท่งล่าสุดถึง {t_close_dt:%H:%M} UTC ครบ)")
        elif len(c) < LV_MIN_BARS_1D or not consec_1d or not full_bars:
            fail.append(f"แท่ง Day ต่อเนื่อง {len(c)} วัน (ต้อง ≥ {LV_MIN_BARS_1D} วันและเมื่อวานครบแท่ง)")
        if f["vol_168h"] < LV_MIN_VOL_1H:
            fail.append(f"volume เฉลี่ย 7 วัน ${f['vol_168h'] * 24 / 1e6:.1f}M/วัน < ${LV_MIN_VOL_1H * 24 / 1e6:.1f}M/วัน")
        elif tf1h and sg["vol_rt"] < LV_MIN_VOL_1H:
            fail.append(f"volume เฉลี่ย 168 ชม. ถึง {t_close_dt:%H:%M} UTC ${sg['vol_rt'] * 24 / 1e6:.1f}M/วัน < ${LV_MIN_VOL_1H * 24 / 1e6:.1f}M/วัน")
        if not f["bm_d"] and not fail:
            fail.append("ไม่ผ่านเกณฑ์ Base Universe")
        if f["bm_d"]:
            if f["vol14"] > vol_cut:
                fail.append(f"ความผันผวน 14 วัน ({f['vol14'] * 100:.1f}%) สูงเกินเกณฑ์ 2 ใน 3 ล่าง ({vol_cut * 100:.1f}%) เสี่ยงถูกบีบ")
            if f["max_dr7"] is None or f["max_dr7"] > LV_MAX_DR7:
                fail.append(f"7 วันล่าสุดมีแท่งพุ่งแรง {(f['max_dr7'] or 0) * 100:+.1f}% > +15% (เสี่ยง squeeze)")
            if f["f24"] < LV_MIN_F24:
                fail.append(f"funding 24 ชม. {f['f24'] * 100:+.3f}% < -0.030% (คนรุม short แน่น)")
            elif tf1h and f24_rt < LV_MIN_F24:
                fail.append(f"funding 24 ชม. ถึง {t_close_dt:%H:%M} UTC {f24_rt * 100:+.3f}% < -0.030% (คนรุม short แน่น)")
            if f["f7d"] < LV_MIN_F7D:
                fail.append(f"funding 7 วัน {f['f7d'] * 100:+.3f}% < -0.100% (คนรุม short สะสม)")
        if sma50 is None or ema20 is None or atr20 is None or sup20 is None or sup20_prev is None or s_prev is None:
            if not fail:
                fail.append("ข้อมูลคำนวณ SMA50/EMA20/ATR20 ไม่พอ")
        else:
            if not (s_c < sma50 and ema20 < sma50):
                fail.append("ยังไม่เป็นขาลงระดับโครงสร้าง (ต้อง Close < SMA50 และ EMA20 < SMA50" + (" ของแท่ง 1 ชม.)" if tf1h else ")"))
            if f["sma20_d"] is None or not (f["cf"][-1] < f["sma20_d"]):
                fail.append("ราคาปิดเมื่อวานยังไม่อยู่ใต้ SMA20")
            if rel14 is None or rel14 > LV_MAX_REL14:
                fail.append(f"ความอ่อนแอเทียบ BTC 14 วัน {(rel14 or 0) * 100:+.1f}% > -5.0%")
            if not (s_c < sup20):
                fail.append(f"ราคาปิด {s_c:.6g} ยังไม่หลุดแนวรับ {sup_th} ({sup20:.6g})")
            elif not (s_prev >= sup20_prev):
                fail.append("หลุดแนวรับไปตั้งแต่แท่งก่อนแล้ว (ไม่ไล่ราคา)")
            elif s_prev <= 0 or (s_prev - s_c) / s_prev > LV_MAX_DROP_BAR:
                drop_pct = ((s_prev - s_c) / s_prev * 100) if s_prev > 0 else 0.0
                fail.append(f"แท่งที่หลุดลงแรงเกินไป (-{drop_pct:.1f}% > 8%) เสี่ยงเด้งแรง")

        if sym in late:
            fail.append(f"ข้อมูลใน ClickHouse ยังไม่ครบถึง {want_bar[11:]} UTC" if tf1h else
                        f"ข้อมูลวัน {day_eval.isoformat()} ใน ClickHouse ยังไม่ครบถึง 23:45 UTC")
        if not gate_ok:
            fail.append("ตัวกรองตลาด SHORT ล้วนไม่ผ่าน")

        stop_dist = min(max(3.0 * atr20 / s_c, LV_STOP_MIN), LV_STOP_MAX) if (atr20 is not None and s_c and s_c > 0) else LV_STOP_MAX
        score = round((-rel14 * 100.0) if rel14 is not None else -999.0, 4)
        is_setup = not fail
        near_setup = (f["bm_d"] and sma50 is not None and ema20 is not None and s_c is not None and s_c < sma50 and ema20 < sma50
                      and (rel14 is not None and rel14 <= LV_MAX_REL14))
        sig_label = (lv_signal() if is_setup else
                     f"จ่อหลุดแนวรับ {sup_th} (LowVol Laggard)" if near_setup else
                     f"นอกเกณฑ์ {LV_TF.upper()} Breakdown ({f['age_days']}d)")
        trend_th = ("ขาลงหลุดแนวรับ 20 วัน" if (d_sup20 is not None and c[-1] < d_sup20 and d_sma50 is not None and c[-1] < d_sma50)
                    else day_trend(cur_p, sma7, sma20_cur))

        rows.append({
            "symbol": sym, "base": f["base"], "side": "SHORT", "price": s_c if is_setup else cur_p,
            "live_price": cur_p, "entry_close_1d": c[-1], "age_days": f["age_days"],
            "vol30_usd": f["vol_168h"] * 24.0, "vol_168h_usd": f["vol_168h"],
            "r7": f["r7"], "rel14": rel14, "dd_ath": dd_ath,
            "trend_1d": trend_th, "downtrend_1d": bool(d_sma50 is not None and c[-1] < d_sma50 and cur_p < sma20_cur),
            "rsi_prev": rsi_prev, "rsi": rsi_now, "rsi4_prev": rsi4_prev, "rsi4": rsi4_now,
            "funding": f24_rt if tf1h else f["f24"], "funding_7d": f["f7d"],
            "atr20": atr20, "stop_dist": round(stop_dist, 6), "sup20": sup20,
            "score": score, "signal": sig_label, "fail": fail,
        })

    rows.sort(key=lambda r: (bool(r["fail"]), -r.get("score", -999.0), r["symbol"]))
    return rows, {
        "universe": len(rows), "base_universe": len(base_syms), "vol_cut": round(vol_cut, 6),
        "data_to": f"{t_close_dt:%Y-%m-%d %H:%M}" if tf1h else f"{day_eval.isoformat()} 24:00", "late": sorted(late),
        "waiting_ch": waiting_ch, "entry_day": entry_day,
        "btc_up_1h": False, "breadth_1h": round(breadth, 4),
        "allow_long": False, "allow_short": allow_short,
        "short_pause": "" if allow_short else mkt_trend, "mkt_trend": mkt_trend,
    }


_DAY_CTX = {}   # today_utc -> (daily_ohlcv, newest_daily, fund_map, fund_ok)


def lv_day_context(syms, today_utc, now):
    """Yesterday's daily bars + the day-boundary funding sums: the same all UTC day once yesterday's last candle is in. A
    round re-screens every 30 s while ClickHouse lacks the hour's last candle, so the 450-day read is kept from 01:00 UTC
    on (the 00:00 hour reads again: 23:45 of yesterday may still be arriving)."""
    if today_utc in _DAY_CTX:
        return _DAY_CTX[today_utc]
    daily_ohlcv, newest_daily = load_perp_daily_ohlcv(syms, days=450, before_day=today_utc)
    fund_map, fund_ok = perp_funding_7d(int(dt.datetime.combine(today_utc, dt.time(0, 0), tzinfo=dt.timezone.utc).timestamp()))
    ctx = (daily_ohlcv, newest_daily, fund_map, fund_ok)
    if fund_ok and now.hour >= 1:
        _DAY_CTX.clear()
        _DAY_CTX[today_utc] = ctx
    return ctx


def lowvol_breakdown_screen(now=None):
    """Live screen for Strategy #1: `LV_TF | LOWVOL_BREAKDOWN` (mode per LV_SHORT_ONLY)."""
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    today_utc = now.date()
    perps = usdt_perps()
    syms = set(perps) | {"BTCUSDT"}
    daily_ohlcv, newest_daily, fund_map, fund_ok = lv_day_context(syms, today_utc, now)
    hourly_qv, newest_15m = load_perp_hourly_qv(syms, days=10)
    if LV_TF != "1h":
        return lowvol_breakdown_eval(perps, daily_ohlcv, hourly_qv, fund_map, today_utc, newest_daily, fund_ok, now_utc=now)
    fund_rt, rt_ok = perp_funding_24h(int(now.replace(minute=0, second=0, microsecond=0).timestamp()))
    return lowvol_breakdown_eval(perps, daily_ohlcv, hourly_qv, fund_map, today_utc, newest_15m, fund_ok and rt_ok, now_utc=now, fund_rt=fund_rt)


short_screen = lowvol_breakdown_screen
