"""Antigravity autonomous Dual-Side SHORT/LONG Intraday trader CLI (V22 — 25 V21 SHORT + 5 V22 LONG Engines).

Runs on <LAN_SERVER_IP>:~/bottread and talks to Order Proxy via binance.py.

Hard safety rails (enforced in code with Decimal money/price arithmetic):
  - Dual-Side SHORT & LONG (R0 unlocked 2026-11-26 by user; unlimited LONG/SHORT and open count, constrained by available_usdt >= 101)
  - Margin 100 USDT x 2x isolated = 200 USDT notional (`LOT_USDT`), min 101 USDT available margin
  - Sticky Auto Safe Circuit Breaker: peak-trailing HWM drawdown, leverage-scaled (1x=5.0%, +2.5%/+1x -> 2x=7.50%, cap 10.0%, floor 20 USDT; blocks new opens, closes allowed)
  - 72h (3-day) per-symbol loss cooldown (max-until across history) + max 2 losses/symbol in 7 days
  - V18 Asymmetric Exit, 3-Stage Progressive Runner Trail & Quad-Channel Unprotected Squeeze Defense (`SQZ_STG1` + `BTC15_18` + `ETH15_20`):
      * All-Age Engines (`V18_AA_SQZ96`): MFE >= +5.0% tightens SL to -1.0% (`HALF50`, cutting risk 80% earlier),
        >= +8.0% locks +2.5% (`LOCK8`), >= +13.0% trails MFE-4.5% (`TRAIL13`), >= +22.0% trails MFE-3.5% (`TRAIL22`),
        >= +30.0% trails MFE-2.5% (`TRAIL30`), 24h < +5% closes, >= +5% runner up to 96h (right-tail uncapped TP 60%);
        ALL unprotected losing positions (`INIT` & `HALF50`/`HALF55` with `pnl < 0`) cut immediately on ANY of:
        BTC 15m Flash Squeeze (`BTC 15m >= +1.8%`), ETH 15m Altcoin-Beta Flash Squeeze (`ETH 15m >= +2.0%`),
        BTC Short Squeeze (`BTC 1h >= +1.8%` or `BTC 4h >= +3.2%`),
        OR Broad Altcoin 1H Breadth Squeeze (`b1h >= 85%` of liquid alts > 1H EMA20).
      * New-Listing Cohort (`V21_NEW7_SQZ96`): Soft-Half Anti-Roundtrip (`MFE >= +6.5%` -> SL `-2.0%` `HALF65`,
        surviving new-listing noise) + `LOCK8`/`TRAIL13`/`TRAIL22`/`TRAIL30`/TP 60% + noise-tolerant Quint-Channel Squeeze Cut
        (`BTC 15m >= +2.2% | ETH 15m >= +2.5% | BTC 1h >= +2.2% | BTC 4h >= +4.0% | Alt B1H >= 90%`)
        covering BOTH `INIT` and `HALF65` losing positions (`pnl < 0`).
  - V21 Production Execution & Reliability Engine (built on V16.1–V20):
      * Thread-local HTTPS Keep-Alive pool, parallel top-of-scan bootstrap, 24-worker 1H REST patch, parallel dual-host
        (`fapi` + `api`) 15m patch + 8h/12h gap repair, concurrent 4H/2H/1H Spot REST fallback, parallel spread prefetch,
        parallel BTC+ETH+mark position-eval bootstrap, and 1-retry transient scan fault tolerance in `auto-trade`.
      * 5-second `fast-guard` mark-price & quad-channel (`BTC 15m/1h/4h` + `ETH 15m` + `B1H >= 85%`) squeeze defense + Parallel Emergency Batch Close.
      * Portfolio-fetch-error guard, persistent cross-process exponential close-failure backoff, pending-settlement reopen guard,
        tentative `entry_est` stop fallback (cleared once real fill arrives), stale `ema20_1h_map` CH auto-refresh,
        and explicit `active_squeeze` separation so Neutral/Bull overheat shorts fire cleanly in `65% < BD <= 85%` without relaxing BTC/1H squeeze blocks.
  - Never imports `team` (which installs a global excepthook logging to cache/team.log); never leaks public IPs/secrets.

Validated V21 25-Engine Institutional Spot Upper-Wick Rejection + NEW7 Quint-Channel Squeeze Defense System (`V21_25ENG_SPUW_N7SQZ`, V134-V140 7-layer audit):
  23 V20 Core engines + `SP_UWICK_1H` (1H Institutional Spot OHLC Upper-Wick Fresh-EMA10 Rejection, 24/7 incl. odd hours, `sp_red`, `sp_uwick >= 0.30`, `bas8h >= -0.05%`, `spot_tb_1h < 0.40`, `tb_1h < 0.43`, `spot_vrat_1h >= 1.3x`, `lwick_1h < 0.28`, SL 5.0%, TP 60%, max 2)
  + `SP_UWICK_2H` (2H Institutional Spot OHLC Upper-Wick Rejection below EMA10, SL 5.0%, TP 60%, max 2)
  + `V21_NEW7_SQZ96` (NEW7 Noise-Tolerant Quint-Channel Squeeze Defense: BTC 15m >= +2.2%, ETH 15m >= +2.5%, BTC 1h >= +2.2%, BTC 4h >= +4.0%, Alt B1H >= 90%):
  Full TRAIN n=1,107 +110.0% (+7.6% vs V20) MTM -8.6% MCal 12.79 Surv 80.5% (+4.9%) /
  Blind TEST (2023-2026): n=2,695 (2.04/d, +89 vs V20), WR 41.8%, Payoff 2.10:1,
  Base +280.0% (+6.48%/mo), PF 1.505, FundAdj +292.7%, Stress50 +204.7%,
  +15m Latency +243.3% (+3.3% vs V20) / FA +256.5% (+4.1%) / S50 +168.7% (+1.1%),
  CB Surv 62.8% (+4.7% vs V20), 5/6 30d, Q 12/15, 7/7 years (2020-2026), Top1 3.4% (-0.3%), p=0.0000;
  All-Age Core (0 New Listings): Blind TEST n=1,888 (1.43/d, +108 vs V20), Base +227.6% (+14.1% vs V20), PF 1.726,
  FundAdj +238.7% (+14.0%), Stress50 +175.1% (+11.0%), +15m Latency +224.4% (+5.7%) / FA +236.2% / S50 +171.8%,
  MTM Calmar 20.14 (+0.18), CB Surv 69.8%, 7/7 years, Top1 4.0%, p=0.0000.
"""
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import http.client
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

try:
    import fcntl
except ImportError:  # Windows offline tests
    fcntl = None

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
REPORTS = os.path.join(ROOT, "reports")
STATE_PATH = os.path.join(CACHE, "agy-state.json")
POS_META_PATH = os.path.join(CACHE, "agy-positions.json")
BTC_REF_PATH = os.path.join(CACHE, "agy-btc-ref.json")
CLOSE_BACKOFF_PATH = os.path.join(CACHE, "agy-close-backoff.json")
FUNDING_CACHE_PATH = os.path.join(CACHE, "agy-funding-cache.json")
FUNDING_CACHE_MAX_AGE_SEC = 1800
ENTRY_SIGNALS_PATH = os.path.join(CACHE, "entry-signals.json")
REQUESTS_DIR = os.path.join(CACHE, "requests")
GUARD_HEARTBEAT_PATH = os.path.join(CACHE, "agy-guard-heartbeat.json")
LOCK_PATH = os.path.join(CACHE, "agy-guard.lock")
FAST_GUARD_LOCK_PATH = os.path.join(CACHE, "agy-fast-guard.lock")
JOURNAL_PATH = os.path.join(REPORTS, "agy-journal.jsonl")
_CLOSE_REQ_RE = re.compile(r"^close-(bto-[0-9A-Za-z-]{1,32})$")
TH = dt.timezone(dt.timedelta(hours=7))

# Hard-locked R0 risk & trading constants (NEVER alter without explicit R0 order)
ALLOWED_SIDES = frozenset({"SHORT", "LONG"})
ALLOWED_SIDE = "SHORT"                 # default side for backward-compatible callers/CLI
MARGIN_USDT = Decimal("100")           # Margin ต่อไม้ (ลุงจืดอนุมัติ R0 2026-11-26: Margin 100 × 2x = Notional 200 USDT)
LEVERAGE = 2                           # 2x isolated (ลุงจืดอนุมัติ R0 2026-11-26)
LOT_USDT = MARGIN_USDT * Decimal(LEVERAGE)  # Notional ต่อไม้ (200 USDT)
MAX_OPEN = 0                           # 0 = ไม่จำกัดจำนวนไม้ ดูตาม available_usdt >= MIN_AVAIL_USDT (ลุงจืดอนุมัติ R0 2026-11-26)
MAX_SHORT_OPEN = 0                     # 0 = ไม่จำกัดฝั่ง SHORT (ลุงจืดอนุมัติ R0 2026-11-26)
MAX_LONG_OPEN = 0                      # 0 = ไม่จำกัดฝั่ง LONG (ลุงจืดอนุมัติ R0 2026-11-26)
MIN_AVAIL_USDT = Decimal("101")        # Margin × 1.01 buffer
CB_AUTO_ENABLED = True                 # Auto Safe Circuit Breaker: Peak-trailing + Leverage-scaled (ลุงจืดอนุมัติ R0 2026-11-26)
CB_BASE_PCT = Decimal("0.05")          # 1x = 5.0%
CB_PCT_PER_EXTRA_LEV = Decimal("0.025")  # +2.5% per +1x -> 2x = 7.50%
CB_MAX_PCT_CAP = Decimal("0.10")       # เพดานปลอดภัยสูงสุด 10.0%
CB_MIN_USDT_FLOOR = Decimal("20.00")   # ขั้นต่ำ USDT สำหรับพอร์ตขนาดเล็ก
CB_MAX_LOSS_USDT = Decimal("45.00")    # legacy fixed fallback when CB_AUTO_ENABLED=False
CB_MAX_LOSS_PCT = Decimal("0.05")      # legacy fixed fallback when CB_AUTO_ENABLED=False
DAILY_MAX_OPENS = 0                    # 0 = ไม่จำกัดจำนวนไม้/วัน UTC (ปลดล็อก R0 10/10 โดยลุงจืด 2026-11-26)
HOURLY_MAX_OPENS = 0                   # 0 = ไม่จำกัดจำนวนเปิดต่อชั่วโมง/รอบ (ปลดล็อกโดยลุงจืด 2026-11-26)
ENFORCE_REGIME_OPEN_GATE = False       # False = ไม่บล็อกเปิด SHORT/LONG ตาม regime/squeeze/crash (ปลดล็อกโดยลุงจืด 2026-11-26)
ENFORCE_OPEN_COOLDOWN = False          # False = ไม่พัก cooldown รายเหรียญหลังปิด/ขาดทุน (ปลดล็อกโดยลุงจืด 2026-11-26)

# V23 Dual-Side EMA+RSI Mood + V21/V22 Institutional Engines (runs every 15m after ClickHouse settle at :04/:19/:34/:49)
STRATEGY_VERSION = "V23_EMA_RSI_MOOD"
PENDING_OPEN_META_TTL_SEC = 600        # Keep pre-open pending meta 10m if /open times out (prevents LEGACY downgrade)
FAST_CLOSE_FAIL_BACKOFF_SEC = 45.0     # Base backoff after /close failure to prevent Proxy spam
CLOSE_FAIL_BACKOFF_STEPS_SEC = (45.0, 120.0, 300.0)  # Cross-process exponential backoff on repeated /close refusal
STOP_PCT_MIN = Decimal("0.025")
STOP_PCT_MAX = Decimal("0.075")
DEFAULT_STOP_PCT = Decimal("0.060")
TP_PCT_MIN = Decimal("0.07")
TP_PCT_MAX = Decimal("0.60")
DEFAULT_TP_PCT = Decimal("0.60")
DEFAULT_EXIT_MODE = "V18_AA_SQZ96"
NEW7_EXIT_MODE = "V21_NEW7_SQZ96"
LONG_EXIT_MODE = "V22_LONG_SQZ96"

# V23 Dual-Side Portfolio Controls (0 / empty = unlimited per user unlock 2026-11-26; constrained by available_usdt >= 101 & Auto Safe CB)
MAX_OPEN_HEAT_USDT = Decimal("0")      # 0 = ไม่จำกัด Heat รวม (ปลดล็อกโดยลุงจืด 2026-11-26)
ENGINE_MAX_OPEN = {}                   # ว่าง = ไม่จำกัดจำนวนไม้ต่อเอนจิน (ปลดล็อกโดยลุงจืด 2026-11-26)
BULL_REGIME_SETUPS = frozenset({"BULL_SPOTFLOW_2H"})
ENGINE_HEAT_CAP = {}
MIN_ATR14_1H_PCT = 0.008               # filter out dead/flat coins (< 0.8%/h ATR14)
MAX_ENTRY_CHASE_DROP = 0.030           # guard against chasing >3% flash dump below SHORT signal close
MAX_ENTRY_CHASE_PUMP = 0.030           # guard against chasing >3% flash pump above LONG signal close

# V12 Hybrid Anti-Roundtrip, 3-Stage Progressive Runner Trail & Dual-Channel BTC + Altcoin-Breadth Squeeze Defense
V11_AA_HALF_MFE = Decimal("0.050")     # V11/V12 All-Age: MFE >= +5.0% -> tighten SL to -1.0% (`HALF50`, earlier 80% risk cut)
V7_HALF_MFE = Decimal("0.055")         # V7-V10 All-Age: MFE >= +5.5% -> tighten SL (V7-V9: -1.5%, V10: -1.0%)
V7_HALF_SL = Decimal("0.015")          # V7-V9 All-Age HALF55 loss cap (-1.5%)
V10_AA_HALF_SL = Decimal("0.010")      # V10-V12 All-Age HALF50/HALF55 tightened loss cap (-1.0%, cuts residual risk 80%)
V9_NEW7_HALF_MFE = Decimal("0.065")    # V9-V12 NEW7: MFE >= +6.5% -> tighten SL to -2.0% (survives listing noise)
V9_NEW7_HALF_SL = Decimal("0.020")     # entry * (1 + 0.020) = -2.0% loss cap
V4_LOCK_MFE = Decimal("0.080")         # MFE >= +8.0% -> lock stop at +2.5%
V4_LOCK_PROFIT = Decimal("0.025")
V4_TRAIL_MFE = Decimal("0.130")        # MFE >= +13.0% -> trail MFE - 4.5%
V4_TRAIL_DIST = Decimal("0.045")
V8_TR2_MFE = Decimal("0.220")          # V8-V12: MFE >= +22.0% -> tighten runner trail to MFE - 3.5%
V8_TR2_DIST = Decimal("0.035")
V9_TR3_MFE = Decimal("0.300")          # V9-V12: MFE >= +30.0% -> lock mega-dump tail at MFE - 2.5%
V9_TR3_DIST = Decimal("0.025")
V8_SQZ_BTC_1H = Decimal("0.022")       # V8/V9 & V11/V12 NEW7 BTC squeeze exit (+2.2%/1h | +4.0%/4h)
V8_SQZ_BTC_4H = Decimal("0.040")
V10_SQZ_BTC_1H = Decimal("0.020")      # V10 legacy BTC squeeze exit (+2.0%/1h | +3.5%/4h)
V10_SQZ_BTC_4H = Decimal("0.035")
V11_AA_SQZ_BTC_1H = Decimal("0.018")   # V11/V17 All-Age sharpened BTC squeeze exit (+1.8%/1h | +3.2%/4h, wide plateau V44/V50)
V11_AA_SQZ_BTC_4H = Decimal("0.032")
V17_AA_SQZ_BTC_15M = Decimal("0.018")  # V17/V18 All-Age 15m BTC flash-squeeze emergency cut (+1.8%/15m, strict Pareto V102-V106)
V18_AA_SQZ_ETH_15M = Decimal("0.020")  # V18 All-Age 15m ETH altcoin-beta flash-squeeze cut (+2.0%/15m, 18/18 strict Pareto V108-V115)
V11_NEW7_SQZ_BTC_1H = Decimal("0.022") # V11/V21 NEW7 noise-tolerant BTC squeeze exit (+2.2%/1h | +4.0%/4h)
V11_NEW7_SQZ_BTC_4H = Decimal("0.040")
V21_NEW7_SQZ_BTC_15M = Decimal("0.022")# V21 NEW7 noise-tolerant BTC 15m flash-squeeze cut (+2.2%/15m, V137B/V140)
V21_NEW7_SQZ_ETH_15M = Decimal("0.025")# V21 NEW7 noise-tolerant ETH 15m alt-beta flash-squeeze cut (+2.5%/15m, V137B/V140)
V21_NEW7_SQZ_B1H = Decimal("0.90")     # V21 NEW7 noise-tolerant Broad Altcoin 1H Breadth Squeeze Cut (>= 90%, V137B/V140)
V22_LONG_CRASH_BTC_15M = Decimal("-0.018")  # V22 LONG unprotected BTC 15m flash-crash emergency cut (<= -1.8%)
V22_LONG_CRASH_BTC_1H = Decimal("-0.018")   # V22 LONG unprotected BTC 1h crash emergency cut (<= -1.8%)
V22_LONG_CRASH_BTC_4H = Decimal("-0.032")   # V22 LONG unprotected BTC 4h crash emergency cut (<= -3.2%)
V22_LONG_CRASH_ETH_15M = Decimal("-0.020")  # V22 LONG unprotected ETH 15m alt-beta crash cut (<= -2.0%)
V12_AA_SQZ_B1H = Decimal("0.85")       # V12 All-Age Broad Altcoin 1H Breadth Squeeze Cut (>= 85% liquid alts > 1H EMA20)
B1H_EMA_REF_MAX_AGE_SEC = 5400         # Max age (90m) of cached ema20_1h_map for live 15s fast-guard breadth
B1H_EMA_MIN_SYMS = 20                  # Min valid symbols required before live breadth squeeze can trigger
BTC_REF_MAX_AGE_SEC = 600              # Max age (10m) of cached BTC 1h/4h reference closes for live fast-guard
V4_RUNNER_MIN_PNL = Decimal("0.050")   # At 24h: cur_pnl >= +5.0% unlocks risk-free runner
V4_RUNNER_LOCK = Decimal("0.025")      # Minimum profit locked when runner unlocks at 24h
V4_RUNNER_MAX_HOURS = 72
V6_RUNNER_MAX_HOURS = 96
FIRST_BAR_GRACE_MS = 180_000           # Include opening 15m bar when sanitized post-entry or entry <=3m after bar open

SHORT_REST_HOURS = 4
SHORT_COOLDOWN_DAYS = 3          # 72h loss cooldown (blocks repeated squeeze re-entries)
MAX_LOSSES_PER_SYM_7D = 2        # hard cap: max 2 losing trades per symbol in rolling 7d
MAX_SPREAD = Decimal("0.005")
STAGNANT_HOLD_HOURS = 8          # LEGACY exit_mode only (disabled in V9/V8/V7/V6/V4/V3)
STAGNANT_MIN_PROFIT = Decimal("0.015")
STAGNANT_MAX_MFE = Decimal("0.025")
MAX_HOLD_HOURS = 24

MIN_VOL_24H_USD = 5_000_000.0
MIN_SPOT_VOL_24H_USD = 500_000.0
MIN_FUNDING_DAY = -0.0009
MIN_EXHAUST_FUNDING_DAY = -0.0012
BULL_BREADTH_MAX = 0.50
BTC_RET7D_MAX = 0.08
BASIS_NOT_CROWDED_MIN = -0.0015
BASIS_CLEAN_MIN = -0.0005
BASCRUSH_PEAK12H_MIN = 0.0020
BASCRUSH_DROP_MIN = 0.0010
BASCRUSH_BD_MAX = 0.42           # V11 Bull-Transition guard on BASCRUSH_4H (cuts CB clusters; NEW7 stays <= 0.50)
BASCRUSH_BTC_RET4H_MIN = -0.040  # V13 Flash-Crash Capitulation guard: skip BASCRUSH_4H after >=4% 4H BTC crash (V63 7-layer)
SPOTDMP_BD_MAX = 0.40
SPOTDMP_STB_MAX = 0.40
SPOTDMP_PTB_MAX = 0.44
SPOTDMP_MIN_PS_VRAT = 2.0        # Unified Perp/Spot 24h vol ratio >= 2.0x for SPOTDMP_4H & SPOTDMP_1H
SPOTDMP4H_LWICK_MAX = 0.35       # V10/V11 SPOTDMP_4H clean-close guard (rejects >= 35% lower-wick dip absorption)
SPOTDMP1H_BD_MAX = 0.40          # V9-V11 SPOTDMP_1H sniper thresholds (wide plateau verified in V31)
SPOTDMP1H_STB_MAX = 0.35
SPOTDMP1H_PTB_MAX = 0.42
SPOTDMP1H_SVRAT_MIN = 1.5
SPOTDMP1H_LWICK_MAX = 0.30
SPOTDMP1H_BAS8H_MIN = 0.0
SPLEAD4H_BD_MAX = 0.45           # V10/V11 SP_LEAD_4H Institutional Spot-Leads-Perp Divergence (wide plateau V36/V38)
SPLEAD4H_BAS8H_MIN = 0.0
SPLEAD4H_STB_MAX = 0.38
SPLEAD4H_DIV_MIN = 0.06
SPLEAD4H_SVRAT_MIN = 1.2
SPLEAD4H_LWICK_MAX = 0.30
BEARBRK1H_BD_MAX = 0.35          # V11 BEAR_BRK_1H Deep-Bear Compression/Flag Breakdown (wide plateau V42/V44)
BEARBRK1H_BAS8H_MIN = 0.0
BEARBRK1H_TB_MAX = 0.38
BEARBRK1H_VRAT_MIN = 1.6
BEARBRK1H_EXT_E10_MIN = -0.035
BEARBRK1H_LWICK_MAX = 0.25
POST30D4H_AGE_MIN = 7.0          # V11/V12 POST30D_4H Post-Listing Phase-2 Distribution (wide plateau V42/V44)
POST30D4H_AGE_MAX = 30.0
POST30D4H_BD_MAX = 0.45
POST30D4H_TB_MAX = 0.44
POST30D4H_VRAT_MIN = 1.2
POST30D4H_LWICK_MAX = 0.30
BASOVH1H_BD_MAX = 0.45           # V12 BASOVH_1H Crowded-Long Perp Basis Overheat Unwind (wide plateau V47/V50)
BASOVH1H_BAS8H_MIN = 0.0010
BASOVH1H_TB_MAX = 0.42
BASOVH1H_STB_MAX = 0.42
BASOVH1H_LWICK_MAX = 0.30
WHALESP1H_BD_MAX = 0.45          # V12 WHALESP_1H Institutional Spot Whale Distribution (wide plateau V47/V50)
WHALESP1H_BAS8H_MIN = 0.0
WHALESP1H_STB_MAX = 0.40
WHALESP1H_PTB_MAX = 0.44
WHALESP1H_TSIZE_MIN = 1.20
WHALESP1H_SVRAT_MIN = 1.2
WHALESP1H_LWICK_MAX = 0.30
SPOTFLOW1H_BD_MAX = 0.45         # V15 SPOTFLOW_POS_1H Hourly Positive-Basis Spot-Flow Continuation (V76/V77 7-layer)
SPOTFLOW1H_BAS8H_MIN = 0.0
SPOTFLOW1H_STB_MAX = 0.36
SPOTFLOW1H_PTB_MAX = 0.42
SPOTFLOW1H_SVRAT_MIN = 1.5
SPOTFLOW1H_EXT_E10_MIN = -0.040
SPOTFLOW1H_LWICK_MAX = 0.30
# V16 Strict Positive-Basis 2H/4H Institutional Moat (V83-V85 7-layer; subordinated priority 221-224)
WHALESP2H_BD_MAX = 0.45
WHALESP2H_BAS8H_MIN = 0.0
WHALESP2H_STB_MAX = 0.40
WHALESP2H_PTB_MAX = 0.44
WHALESP2H_TSIZE_MIN = 1.20
WHALESP2H_SVRAT_MIN = 1.2
WHALESP2H_LWICK_MAX = 0.30
SPOTFLOW2H_BD_MAX = 0.45
SPOTFLOW2H_BAS8H_MIN = 0.0
SPOTFLOW2H_STB_MAX = 0.38
SPOTFLOW2H_PTB_MAX = 0.42
SPOTFLOW2H_SVRAT_MIN = 1.4
SPOTFLOW2H_EXT_E10_MIN = -0.045
SPOTFLOW2H_LWICK_MAX = 0.30
SPOTDMP2H_BD_MAX = 0.40
SPOTDMP2H_BAS8H_MIN = 0.0
SPOTDMP2H_STB_MAX = 0.38
SPOTDMP2H_PTB_MAX = 0.43
SPOTDMP2H_SVRAT_MIN = 1.3
SPOTDMP2H_LWICK_MAX = 0.32
SPLEAD2H_BD_MAX = 0.45
SPLEAD2H_BAS8H_MIN = 0.0
SPLEAD2H_STB_MAX = 0.38
SPLEAD2H_DIV_MIN = 0.06
SPLEAD2H_SVRAT_MIN = 1.2
SPLEAD2H_LWICK_MAX = 0.30
# V17 Basis-Impulse 1H & 2H Post-Squeeze Blow-Off Reversal (V103-V106 strict 7-layer Pareto dominance)
BASIMP1H_BD_MAX = 0.45
BASIMP1H_BAS1H_MIN = 0.0003
BASIMP1H_JUMP_MIN = 0.0005
BASIMP1H_BAS8H_MIN = -0.0005
BASIMP1H_STB_MAX = 0.38
BASIMP1H_PTB_MAX = 0.43
BASIMP1H_SVRAT_MIN = 1.2
BASIMP1H_LWICK_MAX = 0.30
BLOWOFF2H_BD_MAX = 0.45
BLOWOFF2H_PREV_RET_MIN = 0.045
BLOWOFF2H_PREV_VRAT_MIN = 1.5
BLOWOFF2H_TB_MAX = 0.42
BLOWOFF2H_LWICK_MAX = 0.28
# V18 1H Accelerating Perp Retail FOMO into Spot Distribution Continuation (V112-V115 7-layer audit)
BASFOMO1H_BD_MAX = 0.45
BASFOMO1H_BAS15_MIN = 0.0003
BASFOMO1H_B1_JUMP_MIN = 0.0003
BASFOMO1H_BAS8H_MIN = -0.0005
BASFOMO1H_EXT_E10_MIN = -0.040
BASFOMO1H_STB_MAX = 0.36
BASFOMO1H_PTB_MAX = 0.42
BASFOMO1H_SVRAT_MIN = 1.5
BASFOMO1H_LWICK_MAX = 0.30
# V19 2H Pullback-to-EMA10 Kiss Rejection in Established Downtrend (Perp-Only & Deep-Bear safe, V118-V126 7-layer audit)
EMAREJ2H_BD_MAX = 0.45
EMAREJ2H_KISS_RATIO = 0.997
EMAREJ2H_UWICK_MIN = 0.25
EMAREJ2H_LWICK_MAX = 0.25
EMAREJ2H_TB_MAX = 0.42
EMAREJ2H_VRAT_MIN = 1.3
# V20 2H Neutral/Bull Overheat Institutional Spot-Flow Continuation (45% < BD <= 85%, b1h <= 72%, V127-V133 7-layer audit)
BULLSF2H_BD_MIN = 0.45
BULLSF2H_BD_MAX = 0.85
BULLSF2H_B1H_MAX = 0.72
BULLSF2H_BAS8H_MIN = 0.0010
BULLSF2H_EXT_E10_MIN = -0.040
BULLSF2H_STB_MAX = 0.36
BULLSF2H_PTB_MAX = 0.41
BULLSF2H_SVRAT_MIN = 1.5
BULLSF2H_LWICK_MAX = 0.28
# V21 Institutional Spot OHLC Upper-Wick Rejection (1H 24/7 + 2H even UTC, V135-V140 7-layer audit)
SPUW1H_BD_MAX = 0.45
SPUW1H_BAS8H_MIN = -0.0005
SPUW1H_UWICK_MIN = 0.30
SPUW1H_LWICK_MAX = 0.28
SPUW1H_PTB_MAX = 0.43
SPUW1H_STB_MAX = 0.40
SPUW1H_SVRAT_MIN = 1.3
SPUW2H_BD_MAX = 0.45
SPUW2H_BAS8H_MIN = -0.0005
SPUW2H_UWICK_MIN = 0.30
SPUW2H_LWICK_MAX = 0.28
SPUW2H_PTB_MAX = 0.43
SPUW2H_STB_MAX = 0.40
SPUW2H_SVRAT_MIN = 1.3
# V22 Dual-Side Intraday LONG engines (5 disciplined microstructure & trend setups; SL 5.0-5.5%, TP 40-50%, V22_LONG_SQZ96)
LONG_MAX_FUNDING_DAY = 0.0006          # skip crowded longs paying > +0.06%/day funding
LONG_MAX_BAS8H = 0.0010                # skip crowded positive-basis FOMO longs (> +0.10%)
LSQZ1H_BAS8H_MAX = -0.0005             # crowded shorts: bas8h <= -0.05% (or funding <= -0.03%/d)
LSQZ1H_FUND_MAX = -0.0003
LSQZ1H_TB_MIN = 0.56
LSQZ1H_STB_MIN = 0.54
LSQZ1H_SVRAT_MIN = 1.2
LSQZ1H_VRAT_MIN = 1.5
LSQZ1H_UWICK_MAX = 0.30
LSQZ1H_EXT_E10_MAX = 0.045
LSQZ1H_MAX_RET24 = 0.08
LSPOTACC1H_B1H_MIN = 0.20
LSPOTACC1H_STB_MIN = 0.58
LSPOTACC1H_PTB_MIN = 0.55
LSPOTACC1H_SVRAT_MIN = 1.5
LSPOTACC1H_UWICK_MAX = 0.30
LSPOTACC1H_EXT_E10_MAX = 0.040
LSPLWICK1H_LWICK_MIN = 0.30
LSPLWICK1H_UWICK_MAX = 0.28
LSPLWICK1H_STB_MIN = 0.55
LSPLWICK1H_PTB_MIN = 0.54
LSPLWICK1H_SVRAT_MIN = 1.3
LSPLWICK1H_BAS8H_MAX = 0.0008
LTREND1H_B1H_MIN = 0.45
LTREND1H_B1D_MIN = 0.40
LTREND1H_TB_MIN = 0.56
LTREND1H_VRAT_MIN = 1.4
LTREND1H_UWICK_MAX = 0.28
LTREND1H_RSI_MIN = 55.0
LTREND1H_RSI_MAX = 74.0
LTREND1H_EXT_E10_MAX = 0.045
LEMABNC2H_KISS_RATIO = 1.003
LEMABNC2H_LWICK_MIN = 0.25
LEMABNC2H_UWICK_MAX = 0.25
LEMABNC2H_TB_MIN = 0.56
LEMABNC2H_VRAT_MIN = 1.3
# V22 15-Minute Intraday Scalping Engines (fire on completed 15m bars every :00/:15/:30/:45; subordinated below 1H/2H/4H)
M15_MIN_ATR_PCT = 0.005
M15_MIN_VRAT = 2.0
M15_MAX_WICK = 0.25
M15_MAX_CHASE = 0.015
M15_SHORT_BD_MAX = 0.45
M15_SHORT_RSI_MAX = 45.0
M15_SHORT_STB_MAX = 0.45
M15_LONG_RSI_MIN = 52.0
M15_LONG_RSI_MAX = 74.0
M15_LONG_STB_MIN = 0.54
# V23 Active EMA + RSI Mood Engines (15m + 1H aligned; ClickHouse-first at :05/:20/:35/:50; ATR SL 3.5-6.5%, TP 30%, Fast-Guard 5s)
EMA_RSI_MIN_VRAT = 1.2
EMA_RSI_MAX_CHASE = 0.025
EMA_RSI_MAX_EXT_EMA21 = 0.025          # block entries stretched > 2.5% away from 15m EMA21 (anti-chase / anti-wick)
EMA_RSI_SHORT_MIN_FUND = -0.0015       # skip SHORT if daily funding < -0.15%/day (avoid paying heavy negative funding)
EMA_RSI_LONG_MAX_FUND = 0.0010         # skip LONG if daily funding > +0.10%/day (avoid crowded leveraged longs)
EMA_RSI_PULLBACK_TOL = 0.002           # 0.2% touch tolerance around 15m EMA9 for pullback resumption entries
EMA_RSI_SL_ATR_MULT = 2.5              # ATR(14) 1H stop multiplier
EMA_RSI_SL_MIN = 0.035
EMA_RSI_SL_MAX = 0.065
EMA_RSI_SL_DEFAULT = 0.045
EMA_RSI_SHORT_RSI15_MIN = 25.0
EMA_RSI_SHORT_RSI15_MAX = 45.0
EMA_RSI_SHORT_RSI1H_MIN = 25.0
EMA_RSI_SHORT_RSI1H_MAX = 46.0
EMA_RSI_SHORT_RET24_MAX = -0.01
EMA_RSI_LONG_RSI15_MIN = 54.0
EMA_RSI_LONG_RSI15_MAX = 74.0
EMA_RSI_LONG_RSI1H_MIN = 52.0
EMA_RSI_LONG_RSI1H_MAX = 74.0
EMA_RSI_LONG_RET24_MIN = 0.01
NEW7_MIN_BARS_1H = 22
MAX_LIVE_15M_FALLBACK = 20

FAPI_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
FAPI_TICKER_24H = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FAPI_BOOK_TICKER = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
FAPI_PREMIUM_INDEX = "https://fapi.binance.com/fapi/v1/premiumIndex"
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
SPOT_KLINES = "https://api.binance.com/api/v3/klines"
SPOT_TICKER_24H = "https://api.binance.com/api/v3/ticker/24hr?type=MINI"

import binance  # noqa: E402
import market  # noqa: E402

_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_SYMBOL_RE = re.compile(r"^[A-Z0-9\u4e00-\u9fff]{1,20}USDT$")


def scrub_ips(text):
    """Mask public IPv4 addresses in text with `<ip>`, keeping loopback and RFC1918 LAN addresses intact."""
    if not text:
        return ""

    def _repl(m):
        a, b, c, d = (int(m.group(i)) for i in range(1, 5))
        if any(x > 255 for x in (a, b, c, d)):
            return m.group(0)
        if a == 127 or a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return m.group(0)
        return "<ip>"

    return _IPV4_RE.sub(_repl, str(text))


def safe_err(err, limit=240):
    return scrub_ips(str(err))[:limit]


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def init_env():
    """Prefer the host proxy key under ~/.bottread/ssh and load defaults from .env."""
    key_path = os.path.expanduser("~/.bottread/ssh/bottread_proxy_ed25519")
    if os.path.isfile(key_path):
        os.environ["DCA_PROXY_KEY"] = key_path
    binance.load_env(os.path.join(ROOT, ".env"))
    os.environ.setdefault("CLICKHOUSE_HOST", "127.0.0.1")


def dec(val, default=None):
    """Parse value to Decimal safely; return `default` on None/empty/invalid."""
    if val is None or val == "":
        return default
    if isinstance(val, Decimal):
        return val if val.is_finite() else default
    try:
        out = Decimal(str(val).strip())
        return out if out.is_finite() else default
    except (InvalidOperation, ValueError):
        return default


def q_str(val, quantum="0.0001"):
    d = dec(val)
    if d is None:
        return None
    return str(d.quantize(Decimal(quantum), rounding=ROUND_HALF_UP))


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS = {}


def _thread_lock_for(path):
    key = os.path.abspath(path)
    with _THREAD_LOCKS_GUARD:
        lk = _THREAD_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _THREAD_LOCKS[key] = lk
        return lk


def write_json_atomic(path, obj):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def append_line(path, line):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line.rstrip("\r\n") + "\n")


@contextlib.contextmanager
def file_lock(path=LOCK_PATH, blocking=False):
    """Advisory POSIX + intra-process thread lock preventing overlapping guard/auto-trade/scan mutations."""
    tlk = _thread_lock_for(path)
    acquired_t = tlk.acquire(blocking=blocking)
    if not acquired_t:
        yield False
        return
    fd = None
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = open(path, "a+", encoding="utf-8")
        if fcntl is not None:
            flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd.fileno(), flags)
            except BlockingIOError:
                fd.close()
                fd = None
                yield False
                return
        yield True
    finally:
        if fd is not None:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                fd.close()
        tlk.release()


def normalize_symbol(sym):
    s = (sym or "").strip().upper()
    if not s:
        return ""
    return s if s.endswith("USDT") else f"{s}USDT"


def parse_iso_utc(s):
    d = dt.datetime.fromisoformat(s)
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def compute_auto_cb_limits(ref_equity_usdt, leverage=None):
    """Compute leverage-aware safe Circuit Breaker limits (cb_usdt, cb_pct_ratio) using Decimal arithmetic.

    1x = 5.0%, +2.5% per +1x leverage (2x = 7.50%), capped at 10.0%, minimum floor 20.00 USDT.
    """
    lev = LEVERAGE if leverage is None else leverage
    cb_pct = min(CB_MAX_PCT_CAP, CB_BASE_PCT + CB_PCT_PER_EXTRA_LEV * Decimal(max(0, int(lev) - 1)))
    ref_eq = dec(ref_equity_usdt, Decimal("0"))
    if ref_eq is None or ref_eq < Decimal("0"):
        ref_eq = Decimal("0")
    cb_usdt = max(CB_MIN_USDT_FLOOR, (ref_eq * cb_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return cb_usdt, cb_pct


def _config_cb_fields(cb_summary=None, state=None):
    """Build live config + Auto Safe CB metadata shared by heartbeat, auto-trade, status, and scan."""
    src = cb_summary if isinstance(cb_summary, dict) else {}
    st = state if isinstance(state, dict) else {}
    ref_eq = src.get("cb_ref_equity_usdt") or st.get("cb_ref_equity_usdt")
    lim_u = src.get("cb_limit_usdt") or st.get("cb_limit_usdt")
    lim_p = src.get("cb_limit_pct") or st.get("cb_limit_pct")
    if ref_eq is None:
        base_eq = dec(st.get("peak_equity_usdt") if CB_AUTO_ENABLED else st.get("baseline_equity_usdt")) or dec(st.get("baseline_equity_usdt"))
        if base_eq is not None and base_eq > Decimal("0"):
            ref_eq = str(base_eq.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if lim_u is None or lim_p is None:
        if CB_AUTO_ENABLED:
            c_u, c_p = compute_auto_cb_limits(ref_eq or Decimal("0"), LEVERAGE)
            lim_u = lim_u or str(c_u.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            lim_p = lim_p or str((c_p * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        else:
            lim_u = lim_u or str(CB_MAX_LOSS_USDT.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            lim_p = lim_p or str((CB_MAX_LOSS_PCT * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    out = {
        "leverage": int(LEVERAGE),
        "margin_usdt": str(MARGIN_USDT),
        "lot_usdt": str(LOT_USDT),
        "min_avail_usdt": str(MIN_AVAIL_USDT),
        "max_open": int(MAX_OPEN),
        "max_long_open": int(MAX_LONG_OPEN),
        "max_short_open": int(MAX_SHORT_OPEN),
        "daily_max_opens": int(DAILY_MAX_OPENS),
        "hourly_max_opens": int(HOURLY_MAX_OPENS),
        "cb_auto": bool(src.get("cb_auto", CB_AUTO_ENABLED)),
        "cb_limit_usdt": lim_u,
        "cb_limit_pct": lim_p,
    }
    if ref_eq is not None:
        out["cb_ref_equity_usdt"] = ref_eq
    return out


def evaluate_circuit_breaker(state, equity_usdt, now_iso):
    """Update sticky equity baseline/peak/Auto Safe Circuit Breaker state using Decimal math.

    When CB_AUTO_ENABLED is True: ratchets peak = max(peak, baseline, equity) (High-Water Mark) and
    trips when drawdown vs peak reaches <= -cb_usdt or <= -cb_pct from `compute_auto_cb_limits(peak, LEVERAGE)`.
    When CB_AUTO_ENABLED is False: legacy fixed -CB_MAX_LOSS_USDT / -CB_MAX_LOSS_PCT vs baseline.
    Once tripped, stays tripped until explicit `reset-cb --confirm-reset`.
    """
    st = dict(state or {})
    eq = dec(equity_usdt)
    if eq is None or eq <= Decimal("0"):
        meta = _config_cb_fields(state=st)
        summary = {
            "baseline_usdt": st.get("baseline_equity_usdt"),
            "peak_usdt": st.get("peak_equity_usdt"),
            "equity_usdt": None,
            "dd_usdt": None,
            "dd_pct": None,
            "cb_auto": bool(CB_AUTO_ENABLED),
            "cb_leverage": int(LEVERAGE),
            "cb_ref_equity_usdt": meta.get("cb_ref_equity_usdt"),
            "cb_limit_usdt": meta.get("cb_limit_usdt"),
            "cb_limit_pct": meta.get("cb_limit_pct"),
            "cb_dd_usdt": st.get("cb_dd_usdt"),
            "cb_dd_pct": st.get("cb_dd_pct"),
            "tripped": bool(st.get("circuit_breaker_tripped", False)),
            "tripped_at": st.get("tripped_at"),
            "tripped_reason": st.get("tripped_reason"),
        }
        return st, summary

    baseline = dec(st.get("baseline_equity_usdt"))
    if baseline is None or baseline <= Decimal("0"):
        baseline = eq
        st["baseline_equity_usdt"] = str(baseline.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        st["baseline_set_at"] = now_iso

    peak_prev = dec(st.get("peak_equity_usdt")) or eq
    peak = max(peak_prev, baseline, eq)
    st["peak_equity_usdt"] = str(peak.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    st["last_equity_usdt"] = str(eq.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    st["last_checked_at"] = now_iso

    if CB_AUTO_ENABLED:
        ref_eq = peak
        cb_usdt, cb_pct = compute_auto_cb_limits(ref_eq, LEVERAGE)
    else:
        ref_eq = baseline
        cb_usdt, cb_pct = CB_MAX_LOSS_USDT, CB_MAX_LOSS_PCT

    dd_usdt = eq - ref_eq
    dd_pct = (dd_usdt / ref_eq) if ref_eq > Decimal("0") else Decimal("0")
    ref_eq_str = str(ref_eq.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    cb_usdt_str = str(cb_usdt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    cb_pct_str = str((cb_pct * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    dd_usdt_str = str(dd_usdt.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    dd_pct_str = str((dd_pct * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    st["cb_auto"] = bool(CB_AUTO_ENABLED)
    st["cb_leverage"] = int(LEVERAGE)
    st["cb_ref_equity_usdt"] = ref_eq_str
    st["cb_limit_usdt"] = cb_usdt_str
    st["cb_limit_pct"] = cb_pct_str
    st["cb_dd_usdt"] = dd_usdt_str
    st["cb_dd_pct"] = dd_pct_str

    was_tripped = bool(st.get("circuit_breaker_tripped", False))
    trip_now = (dd_usdt <= -cb_usdt) or (dd_pct <= -cb_pct)
    if trip_now and not was_tripped:
        st["circuit_breaker_tripped"] = True
        st["tripped_at"] = now_iso
        ref_lbl = "peak" if CB_AUTO_ENABLED else "baseline"
        st["tripped_reason"] = (
            f"Equity {eq.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} vs {ref_lbl} {ref_eq_str}: "
            f"{dd_usdt.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+} USDT ({(dd_pct * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+}%, limit -{cb_usdt_str}U / -{cb_pct_str}%)"
        )
    elif "circuit_breaker_tripped" not in st:
        st["circuit_breaker_tripped"] = False

    summary = {
        "baseline_usdt": st.get("baseline_equity_usdt"),
        "peak_usdt": st.get("peak_equity_usdt"),
        "equity_usdt": st.get("last_equity_usdt"),
        "dd_usdt": dd_usdt_str,
        "dd_pct": dd_pct_str,
        "cb_auto": bool(CB_AUTO_ENABLED),
        "cb_leverage": int(LEVERAGE),
        "cb_ref_equity_usdt": ref_eq_str,
        "cb_limit_usdt": cb_usdt_str,
        "cb_limit_pct": cb_pct_str,
        "cb_dd_usdt": dd_usdt_str,
        "cb_dd_pct": dd_pct_str,
        "tripped": bool(st.get("circuit_breaker_tripped", False)),
        "tripped_at": st.get("tripped_at"),
        "tripped_reason": st.get("tripped_reason"),
    }
    return st, summary


def _is_v22_long_mode(exit_mode):
    return exit_mode == "V22_LONG_SQZ96"


def _is_v21_new7_mode(exit_mode):
    return exit_mode == "V21_NEW7_SQZ96"


def _is_v18_aa_mode(exit_mode):
    return exit_mode == "V18_AA_SQZ96"


def _is_v17_aa_mode(exit_mode):
    return exit_mode in ("V18_AA_SQZ96", "V17_AA_SQZ96")


def _is_v14_mode(exit_mode):
    return exit_mode in ("V22_LONG_SQZ96", "V21_NEW7_SQZ96", "V18_AA_SQZ96", "V17_AA_SQZ96", "V14_AA_SQZ96", "V14_NEW7_SQZ96")


def _is_unprotected_sqz_stage(exit_mode, stage):
    """V14/V17/V18/V22 `SQZ_STG1`: cut ALL unprotected losing stages (`INIT` + `HALF50`/`HALF55`/`HALF65`); older modes keep `INIT`."""
    if _is_v14_mode(exit_mode):
        return stage in ("INIT", "HALF50", "HALF55", "HALF65")
    return stage == "INIT"


def _is_v12_aa_mode(exit_mode):
    return exit_mode in ("V18_AA_SQZ96", "V17_AA_SQZ96", "V14_AA_SQZ96", "V12_AA_SQZ96")


def _is_v11_aa_mode(exit_mode):
    return exit_mode in ("V18_AA_SQZ96", "V17_AA_SQZ96", "V14_AA_SQZ96", "V12_AA_SQZ96", "V11_AA_SQZ96")


def _is_v11_mode(exit_mode):
    return exit_mode in (
        "V22_LONG_SQZ96",
        "V21_NEW7_SQZ96",
        "V18_AA_SQZ96", "V17_AA_SQZ96",
        "V14_AA_SQZ96", "V14_NEW7_SQZ96",
        "V12_AA_SQZ96", "V12_NEW7_SQZ96",
        "V11_AA_SQZ96", "V11_NEW7_SQZ96",
    )


def _is_v10_mode(exit_mode):
    return exit_mode in ("V10_AA_SQZ96", "V10_NEW7_SQZ96")


def _is_v9_mode(exit_mode):
    return exit_mode in (
        "V22_LONG_SQZ96",
        "V21_NEW7_SQZ96",
        "V18_AA_SQZ96", "V17_AA_SQZ96",
        "V14_AA_SQZ96", "V14_NEW7_SQZ96",
        "V12_AA_SQZ96", "V12_NEW7_SQZ96",
        "V11_AA_SQZ96", "V11_NEW7_SQZ96",
        "V10_AA_SQZ96", "V10_NEW7_SQZ96",
        "V9_AA_SQZ96", "V9_NEW7_SQZ96",
    )


def _is_sqz_mode(exit_mode):
    return exit_mode in (
        "V22_LONG_SQZ96",
        "V21_NEW7_SQZ96",
        "V18_AA_SQZ96", "V17_AA_SQZ96",
        "V14_AA_SQZ96", "V14_NEW7_SQZ96",
        "V12_AA_SQZ96", "V12_NEW7_SQZ96",
        "V11_AA_SQZ96", "V11_NEW7_SQZ96",
        "V10_AA_SQZ96", "V10_NEW7_SQZ96",
        "V9_AA_SQZ96", "V9_NEW7_SQZ96",
        "V8_AA_SQZ96", "V8_NEW7_SQZ96",
    )


def _sqz_thresholds(exit_mode):
    if _is_v11_aa_mode(exit_mode):
        return V11_AA_SQZ_BTC_1H, V11_AA_SQZ_BTC_4H
    if exit_mode in ("V21_NEW7_SQZ96", "V14_NEW7_SQZ96", "V12_NEW7_SQZ96", "V11_NEW7_SQZ96"):
        return V11_NEW7_SQZ_BTC_1H, V11_NEW7_SQZ_BTC_4H
    if _is_v10_mode(exit_mode):
        return V10_SQZ_BTC_1H, V10_SQZ_BTC_4H
    return V8_SQZ_BTC_1H, V8_SQZ_BTC_4H


def _sqz_btc_15m_threshold(exit_mode):
    if _is_v17_aa_mode(exit_mode):
        return V17_AA_SQZ_BTC_15M
    if _is_v21_new7_mode(exit_mode):
        return V21_NEW7_SQZ_BTC_15M
    return None


def _sqz_eth_15m_threshold(exit_mode):
    if _is_v18_aa_mode(exit_mode) or _is_v22_long_mode(exit_mode):
        return V18_AA_SQZ_ETH_15M
    if _is_v21_new7_mode(exit_mode):
        return V21_NEW7_SQZ_ETH_15M
    return None


def _sqz_b1h_threshold(exit_mode):
    if _is_v12_aa_mode(exit_mode):
        return V12_AA_SQZ_B1H
    if _is_v21_new7_mode(exit_mode):
        return V21_NEW7_SQZ_B1H
    return None


def _is_runner_mode(exit_mode):
    return exit_mode in (
        "V22_LONG_SQZ96",
        "V21_NEW7_SQZ96",
        "V18_AA_SQZ96", "V17_AA_SQZ96",
        "V14_AA_SQZ96", "V14_NEW7_SQZ96",
        "V12_AA_SQZ96", "V12_NEW7_SQZ96",
        "V11_AA_SQZ96", "V11_NEW7_SQZ96",
        "V10_AA_SQZ96", "V10_NEW7_SQZ96",
        "V9_AA_SQZ96", "V9_NEW7_SQZ96",
        "V8_AA_SQZ96", "V8_NEW7_SQZ96",
        "V7_AA_HYB96", "V6_LT8_96", "V4_LT8_72",
    )


def _runner_max_hours(exit_mode):
    if exit_mode in (
        "V22_LONG_SQZ96",
        "V21_NEW7_SQZ96",
        "V18_AA_SQZ96", "V17_AA_SQZ96",
        "V14_AA_SQZ96", "V14_NEW7_SQZ96",
        "V12_AA_SQZ96", "V12_NEW7_SQZ96",
        "V11_AA_SQZ96", "V11_NEW7_SQZ96",
        "V10_AA_SQZ96", "V10_NEW7_SQZ96",
        "V9_AA_SQZ96", "V9_NEW7_SQZ96",
        "V8_AA_SQZ96", "V8_NEW7_SQZ96",
        "V7_AA_HYB96", "V6_LT8_96",
    ):
        return V6_RUNNER_MAX_HOURS
    return V4_RUNNER_MAX_HOURS


def _runner_stage_name(exit_mode):
    return "RUNNER96" if _runner_max_hours(exit_mode) == V6_RUNNER_MAX_HOURS else "RUNNER72"


def _ratchet_stop_v4(entry_px, mfe, cur_stop, cur_stage):
    """V4/V6 late trailing ratchet (never chokes early winners; protects >= +8% / >= +13% dumps)."""
    new_stop = cur_stop
    stage = cur_stage
    if mfe >= V4_TRAIL_MFE:
        lock = mfe - V4_TRAIL_DIST
        cand = entry_px * (Decimal("1") - lock)
        if cand < new_stop:
            new_stop = cand
        if stage not in ("RUNNER72", "RUNNER96"):
            stage = "TRAIL13"
    elif mfe >= V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") - V4_LOCK_PROFIT)
        if cand < new_stop:
            new_stop = cand
        if stage in ("INIT", "HALF50", "HALF55", "HALF65"):
            stage = "LOCK8"
    return new_stop, stage


def _ratchet_stop_v7_aa(entry_px, mfe, cur_stop, cur_stage):
    """V7 All-Age hybrid anti-roundtrip ratchet: MFE >= +5.5% -> tighten SL to -1.5%, >= +8% -> +2.5%, >= +13% -> trail."""
    new_stop, stage = _ratchet_stop_v4(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V7_HALF_MFE and mfe < V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V7_HALF_SL)
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF55"
    return new_stop, stage


def _ratchet_stop_v8_new7(entry_px, mfe, cur_stop, cur_stage):
    """V8 NEW7 ratchet: adds 2nd-stage tighter runner trail at MFE >= +22% (MFE - 3.5%) on top of V6_LT8_96."""
    new_stop, stage = _ratchet_stop_v4(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V8_TR2_MFE:
        lock = mfe - V8_TR2_DIST
        cand = entry_px * (Decimal("1") - lock)
        if cand < new_stop:
            new_stop = cand
        if stage not in ("RUNNER72", "RUNNER96"):
            stage = "TRAIL22"
    return new_stop, stage


def _ratchet_stop_v8_aa(entry_px, mfe, cur_stop, cur_stage):
    """V8 All-Age ratchet: HALF55 (-1.5% @ +5.5%), LOCK8 (+2.5% @ +8%), TRAIL13 (-4.5% @ +13%), TRAIL22 (-3.5% @ +22%)."""
    new_stop, stage = _ratchet_stop_v8_new7(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V7_HALF_MFE and mfe < V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V7_HALF_SL)
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF55"
    return new_stop, stage


def _ratchet_stop_v9_base(entry_px, mfe, cur_stop, cur_stage):
    """V9 3-stage progressive runner trail: LOCK8 (+2.5%), TRAIL13 (-4.5%), TRAIL22 (-3.5%), TRAIL30 (-2.5%)."""
    new_stop, stage = _ratchet_stop_v8_new7(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V9_TR3_MFE:
        lock = mfe - V9_TR3_DIST
        cand = entry_px * (Decimal("1") - lock)
        if cand < new_stop:
            new_stop = cand
        if stage not in ("RUNNER72", "RUNNER96"):
            stage = "TRAIL30"
    return new_stop, stage


def _ratchet_stop_v9_new7(entry_px, mfe, cur_stop, cur_stage):
    """V9 NEW7 ratchet: HALF65 (-2.0% @ +6.5%) + LOCK8 + TRAIL13 + TRAIL22 + TRAIL30."""
    new_stop, stage = _ratchet_stop_v9_base(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V9_NEW7_HALF_MFE and mfe < V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V9_NEW7_HALF_SL)
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF65"
    return new_stop, stage


def _ratchet_stop_v9_aa(entry_px, mfe, cur_stop, cur_stage):
    """V9 All-Age ratchet: HALF55 (-1.5% @ +5.5%) + LOCK8 + TRAIL13 + TRAIL22 + TRAIL30."""
    new_stop, stage = _ratchet_stop_v9_base(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V7_HALF_MFE and mfe < V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V7_HALF_SL)
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF55"
    return new_stop, stage


def _ratchet_stop_v10_aa(entry_px, mfe, cur_stop, cur_stage):
    """V10 All-Age ratchet: tightened HALF55 (-1.0% @ +5.5%) + LOCK8 + TRAIL13 + TRAIL22 + TRAIL30."""
    new_stop, stage = _ratchet_stop_v9_base(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V7_HALF_MFE and mfe < V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V10_AA_HALF_SL)
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF55"
    return new_stop, stage


def _ratchet_stop_v11_aa(entry_px, mfe, cur_stop, cur_stage):
    """V11 All-Age ratchet: earlier HALF50 (-1.0% @ +5.0%) + LOCK8 + TRAIL13 + TRAIL22 + TRAIL30."""
    new_stop, stage = _ratchet_stop_v9_base(entry_px, mfe, cur_stop, cur_stage)
    if mfe >= V11_AA_HALF_MFE and mfe < V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V10_AA_HALF_SL)
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF50"
    return new_stop, stage


def _ratchet_stop_v22_long(entry_px, mfe, cur_stop, cur_stage):
    """V22 LONG progressive ratchet (pure Decimal mirror of V11_AA): HALF50 (-1.0% @ +5%), LOCK8 (+2.5% @ +8%), TRAIL13/22/30."""
    new_stop = cur_stop
    stage = cur_stage
    if mfe >= V9_TR3_MFE:
        lock = mfe - V9_TR3_DIST
        cand = entry_px * (Decimal("1") + lock)
        if cand > new_stop:
            new_stop = cand
        if stage not in ("RUNNER72", "RUNNER96"):
            stage = "TRAIL30"
    elif mfe >= V8_TR2_MFE:
        lock = mfe - V8_TR2_DIST
        cand = entry_px * (Decimal("1") + lock)
        if cand > new_stop:
            new_stop = cand
        if stage not in ("RUNNER72", "RUNNER96"):
            stage = "TRAIL22"
    elif mfe >= V4_TRAIL_MFE:
        lock = mfe - V4_TRAIL_DIST
        cand = entry_px * (Decimal("1") + lock)
        if cand > new_stop:
            new_stop = cand
        if stage not in ("RUNNER72", "RUNNER96"):
            stage = "TRAIL13"
    elif mfe >= V4_LOCK_MFE:
        cand = entry_px * (Decimal("1") + V4_LOCK_PROFIT)
        if cand > new_stop:
            new_stop = cand
        if stage in ("INIT", "HALF50", "HALF55", "HALF65"):
            stage = "LOCK8"
    elif mfe >= V11_AA_HALF_MFE:
        cand = entry_px * (Decimal("1") - V10_AA_HALF_SL)
        if cand > new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "HALF50"
    return new_stop, stage


def _btc_squeeze_returns_from_bars(btc_bars_15m, now_utc):
    """Build `(close_by_open_ms, latest_btc_ref_dict)` from completed BTCUSDT 15m bars using Decimal math."""
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    close_map = {}
    for t, _hi, _lo, cl in (btc_bars_15m or ()):
        t_ms = int(t)
        c_d = dec(cl)
        if c_d is not None and c_d > Decimal("0") and t_ms + 900_000 <= now_ms:
            close_map[t_ms] = c_d
    if not close_map:
        return close_map, None
    last_t = max(close_map)
    ref_1h = close_map.get(last_t - 3_600_000)
    ref_4h = close_map.get(last_t - 14_400_000)
    recent_closes = {str(t): str(c) for t, c in sorted(close_map.items())[-20:]}
    ref_info = {
        "at": now_utc.isoformat(timespec="seconds"),
        "last_bar_ms": last_t,
        "last_close": str(close_map[last_t]),
        "ref_1h_close": str(ref_1h) if ref_1h is not None else None,
        "ref_4h_close": str(ref_4h) if ref_4h is not None else None,
        "closes_15m": recent_closes,
    }
    return close_map, ref_info


def _check_btc_squeeze_at(close_map, ts_ms, live_btc_px=None, sqz_1h=None, sqz_4h=None, sqz_15m=None):
    """Return `(triggered, r1h_dec, r4h_dec)` for BTC short-squeeze emergency exit (with optional V17 15m flash channel)."""
    if not close_map:
        return False, None, None
    cur_c = dec(live_btc_px) if live_btc_px is not None else close_map.get(ts_ms)
    if cur_c is None or cur_c <= Decimal("0"):
        return False, None, None
    thr_1h = V8_SQZ_BTC_1H if sqz_1h is None else sqz_1h
    thr_4h = V8_SQZ_BTC_4H if sqz_4h is None else sqz_4h
    c_1h = close_map.get(ts_ms - 3_600_000)
    c_4h = close_map.get(ts_ms - 14_400_000)
    r1h = ((cur_c - c_1h) / c_1h) if (c_1h is not None and c_1h > Decimal("0")) else None
    r4h = ((cur_c - c_4h) / c_4h) if (c_4h is not None and c_4h > Decimal("0")) else None
    r15m = None
    if sqz_15m is not None:
        c_15m = close_map.get(ts_ms - 900_000)
        r15m = ((cur_c - c_15m) / c_15m) if (c_15m is not None and c_15m > Decimal("0")) else None
    hit = bool(
        (r1h is not None and r1h >= thr_1h)
        or (r4h is not None and r4h >= thr_4h)
        or (sqz_15m is not None and r15m is not None and r15m >= sqz_15m)
    )
    return hit, r1h, r4h


def _eval_live_btc_squeeze_from_map(close_map, live_btc_px, now_utc, sqz_1h=None, sqz_4h=None, sqz_15m=None):
    """Exact rolling-window (±7.5m) live BTC squeeze evaluation from completed 15m close_map + live mark."""
    if not close_map:
        return False, None, None
    cur_c = dec(live_btc_px)
    if cur_c is None or cur_c <= Decimal("0"):
        return False, None, None
    thr_1h = V8_SQZ_BTC_1H if sqz_1h is None else sqz_1h
    thr_4h = V8_SQZ_BTC_4H if sqz_4h is None else sqz_4h
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    parsed = [(int(t_ms) + 900_000, c_d) for t_ms, c_d in close_map.items() if c_d is not None and c_d > Decimal("0")]
    if not parsed:
        return False, None, None
    ref_1h = None
    ref_4h = None
    best_1h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 3_600_000)))
    if abs(best_1h[0] - (now_ms - 3_600_000)) <= 1_800_000:
        ref_1h = best_1h[1]
    best_4h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 14_400_000)))
    if abs(best_4h[0] - (now_ms - 14_400_000)) <= 1_800_000:
        ref_4h = best_4h[1]
    r1h = ((cur_c - ref_1h) / ref_1h) if (ref_1h is not None and ref_1h > Decimal("0")) else None
    r4h = ((cur_c - ref_4h) / ref_4h) if (ref_4h is not None and ref_4h > Decimal("0")) else None
    r15m = None
    if sqz_15m is not None:
        # Exact live parity with backtest `btc_ret15m = c[i]/c[i-1] - 1`: compare live mark vs latest completed 15m close
        past = [x for x in parsed if x[0] <= now_ms]
        if past:
            latest_c = max(past, key=lambda x: x[0])
            if (now_ms - latest_c[0]) <= 1_200_000 and latest_c[1] > Decimal("0"):
                r15m = (cur_c - latest_c[1]) / latest_c[1]
    hit = bool(
        (r1h is not None and r1h >= thr_1h)
        or (r4h is not None and r4h >= thr_4h)
        or (sqz_15m is not None and r15m is not None and r15m >= sqz_15m)
    )
    return hit, r1h, r4h


def _eth_15m_ref_from_bars(eth_bars_15m, now_utc):
    """Build `(eth_close_by_open_ms, eth_ref_updates_dict)` from completed ETHUSDT 15m bars using Decimal math."""
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    close_map = {}
    for t, _hi, _lo, cl in (eth_bars_15m or ()):
        t_ms = int(t)
        c_d = dec(cl)
        if c_d is not None and c_d > Decimal("0") and t_ms + 900_000 <= now_ms:
            close_map[t_ms] = c_d
    if not close_map:
        return close_map, None
    last_t = max(close_map)
    recent_closes = {str(t): str(c) for t, c in sorted(close_map.items())[-12:]}
    ref_info = {
        "eth_at": now_utc.isoformat(timespec="seconds"),
        "eth_last_bar_ms": last_t,
        "eth_last_close": str(close_map[last_t]),
        "eth_closes_15m": recent_closes,
    }
    return close_map, ref_info


def _check_eth_15m_squeeze_at(eth_close_map, ts_ms, sqz_15m=V18_AA_SQZ_ETH_15M):
    """Return `(triggered, r15m_dec)` for completed 15m bar ETH flash-squeeze (`c[t]/c[t-15m] - 1 >= +2.0%`)."""
    if not eth_close_map or sqz_15m is None:
        return False, None
    cur_c = eth_close_map.get(ts_ms)
    prev_c = eth_close_map.get(ts_ms - 900_000)
    if cur_c is None or cur_c <= Decimal("0") or prev_c is None or prev_c <= Decimal("0"):
        return False, None
    r15m = (cur_c - prev_c) / prev_c
    return bool(r15m >= sqz_15m), r15m


def _eval_live_eth_15m_from_map(eth_close_map, live_eth_px, now_utc, sqz_15m=V18_AA_SQZ_ETH_15M):
    """Exact live parity with backtest `eth_ret15m = c[i]/c[i-1] - 1`: compare live ETH mark vs latest completed 15m close."""
    if not eth_close_map or sqz_15m is None:
        return False, None
    cur_c = dec(live_eth_px)
    if cur_c is None or cur_c <= Decimal("0"):
        return False, None
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    past = [
        (int(t_ms) + 900_000, c_d)
        for t_ms, c_d in eth_close_map.items()
        if c_d is not None and c_d > Decimal("0") and int(t_ms) + 900_000 <= now_ms
    ]
    if not past:
        return False, None
    latest_c = max(past, key=lambda x: x[0])
    if (now_ms - latest_c[0]) > 1_200_000:
        return False, None
    r15m = (cur_c - latest_c[1]) / latest_c[1]
    return bool(r15m >= sqz_15m), r15m


def _check_btc_crash_at(close_map, ts_ms, live_btc_px=None, cr_1h=V22_LONG_CRASH_BTC_1H, cr_4h=V22_LONG_CRASH_BTC_4H, cr_15m=V22_LONG_CRASH_BTC_15M):
    """Return `(triggered, r1h_dec, r4h_dec)` for BTC downward crash emergency exit on unprotected losing LONGs."""
    if not close_map:
        return False, None, None
    cur_c = dec(live_btc_px) if live_btc_px is not None else close_map.get(ts_ms)
    if cur_c is None or cur_c <= Decimal("0"):
        return False, None, None
    c_1h = close_map.get(ts_ms - 3_600_000)
    c_4h = close_map.get(ts_ms - 14_400_000)
    r1h = ((cur_c - c_1h) / c_1h) if (c_1h is not None and c_1h > Decimal("0")) else None
    r4h = ((cur_c - c_4h) / c_4h) if (c_4h is not None and c_4h > Decimal("0")) else None
    r15m = None
    if cr_15m is not None:
        c_15m = close_map.get(ts_ms - 900_000)
        r15m = ((cur_c - c_15m) / c_15m) if (c_15m is not None and c_15m > Decimal("0")) else None
    hit = bool(
        (r1h is not None and r1h <= cr_1h)
        or (r4h is not None and r4h <= cr_4h)
        or (cr_15m is not None and r15m is not None and r15m <= cr_15m)
    )
    return hit, r1h, r4h


def _eval_live_btc_crash_from_map(close_map, live_btc_px, now_utc, cr_1h=V22_LONG_CRASH_BTC_1H, cr_4h=V22_LONG_CRASH_BTC_4H, cr_15m=V22_LONG_CRASH_BTC_15M):
    """Exact rolling-window (±7.5m) live BTC crash evaluation from completed 15m close_map + live mark."""
    if not close_map:
        return False, None, None
    cur_c = dec(live_btc_px)
    if cur_c is None or cur_c <= Decimal("0"):
        return False, None, None
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    parsed = [(int(t_ms) + 900_000, c_d) for t_ms, c_d in close_map.items() if c_d is not None and c_d > Decimal("0")]
    if not parsed:
        return False, None, None
    ref_1h = ref_4h = None
    best_1h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 3_600_000)))
    if abs(best_1h[0] - (now_ms - 3_600_000)) <= 1_800_000:
        ref_1h = best_1h[1]
    best_4h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 14_400_000)))
    if abs(best_4h[0] - (now_ms - 14_400_000)) <= 1_800_000:
        ref_4h = best_4h[1]
    r1h = ((cur_c - ref_1h) / ref_1h) if (ref_1h is not None and ref_1h > Decimal("0")) else None
    r4h = ((cur_c - ref_4h) / ref_4h) if (ref_4h is not None and ref_4h > Decimal("0")) else None
    r15m = None
    if cr_15m is not None:
        past = [x for x in parsed if x[0] <= now_ms]
        if past:
            latest_c = max(past, key=lambda x: x[0])
            if (now_ms - latest_c[0]) <= 1_200_000 and latest_c[1] > Decimal("0"):
                r15m = (cur_c - latest_c[1]) / latest_c[1]
    hit = bool(
        (r1h is not None and r1h <= cr_1h)
        or (r4h is not None and r4h <= cr_4h)
        or (cr_15m is not None and r15m is not None and r15m <= cr_15m)
    )
    return hit, r1h, r4h


def _check_eth_15m_crash_at(eth_close_map, ts_ms, cr_15m=V22_LONG_CRASH_ETH_15M):
    """Return `(triggered, r15m_dec)` for completed 15m bar ETH flash-crash (`c[t]/c[t-15m] - 1 <= -2.0%`)."""
    if not eth_close_map or cr_15m is None:
        return False, None
    cur_c = eth_close_map.get(ts_ms)
    prev_c = eth_close_map.get(ts_ms - 900_000)
    if cur_c is None or cur_c <= Decimal("0") or prev_c is None or prev_c <= Decimal("0"):
        return False, None
    r15m = (cur_c - prev_c) / prev_c
    return bool(r15m <= cr_15m), r15m


def _eval_live_eth_15m_crash_from_map(eth_close_map, live_eth_px, now_utc, cr_15m=V22_LONG_CRASH_ETH_15M):
    """Compare live ETH mark vs latest completed 15m close for LONG flash-crash emergency cut."""
    if not eth_close_map or cr_15m is None:
        return False, None
    cur_c = dec(live_eth_px)
    if cur_c is None or cur_c <= Decimal("0"):
        return False, None
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    past = [
        (int(t_ms) + 900_000, c_d)
        for t_ms, c_d in eth_close_map.items()
        if c_d is not None and c_d > Decimal("0") and int(t_ms) + 900_000 <= now_ms
    ]
    if not past:
        return False, None
    latest_c = max(past, key=lambda x: x[0])
    if (now_ms - latest_c[0]) > 1_200_000:
        return False, None
    r15m = (cur_c - latest_c[1]) / latest_c[1]
    return bool(r15m <= cr_15m), r15m


def _save_btc_ref_merged(updates):
    """Atomically merge BTC/breadth reference fields into BTC_REF_PATH under a dedicated lock without wiping sibling keys."""
    if not isinstance(updates, dict) or not updates:
        return None
    with file_lock(f"{BTC_REF_PATH}.lock", blocking=True):
        cur = read_json(BTC_REF_PATH, default={})
        if not isinstance(cur, dict):
            cur = {}
        merged = {**cur, **updates}
        write_json_atomic(BTC_REF_PATH, merged)
        return merged


def _eval_live_b1h_squeeze(mark_map, btc_ref, now_utc, b1h_thr=None):
    """Compute live Altcoin 1H Breadth (`pct_above_1h_ema20`) from 1 HTTP `premiumIndex` mark_map + cached `ema20_1h_map`.

    Returns `(triggered, live_b1h_dec, valid_count)` using pure Decimal comparisons.
    Requires fresh `ema20_1h_at` (<= 90m) and >= B1H_EMA_MIN_SYMS (20) valid symbols to prevent false triggers.
    """
    if not btc_ref or not mark_map:
        return False, None, 0
    ema_map = btc_ref.get("ema20_1h_map")
    if not isinstance(ema_map, dict) or len(ema_map) < B1H_EMA_MIN_SYMS:
        return False, None, 0
    at_s = btc_ref.get("ema20_1h_at") or btc_ref.get("at")
    if at_s:
        try:
            age_s = abs((now_utc.astimezone(dt.timezone.utc) - parse_iso_utc(at_s)).total_seconds())
            if age_s > B1H_EMA_REF_MAX_AGE_SEC:
                return False, None, 0
        except (TypeError, ValueError):
            return False, None, 0
    thr = V12_AA_SQZ_B1H if b1h_thr is None else b1h_thr
    valid = 0
    above = 0
    for sym, ema_s in ema_map.items():
        if sym == "BTCUSDT":
            continue
        mk = dec(mark_map.get(sym))
        em = dec(ema_s)
        if mk is None or mk <= Decimal("0") or em is None or em <= Decimal("0"):
            continue
        valid += 1
        if mk > em:
            above += 1
    if valid < B1H_EMA_MIN_SYMS:
        return False, None, valid
    b1h_val = Decimal(above) / Decimal(valid)
    return bool(b1h_val >= thr), b1h_val, valid


def _ratchet_stop_legacy(entry_px, mfe, cur_stop, cur_stage):
    """Legacy trailing ratchet kept only for pre-V3 positions opened before V3/V4 deployment."""
    new_stop = cur_stop
    stage = cur_stage
    if mfe >= Decimal("0.080"):
        lock = mfe - Decimal("0.020")
        cand = entry_px * (Decimal("1") - lock)
        if cand < new_stop:
            new_stop = cand
        stage = "TRAIL8"
    elif mfe >= Decimal("0.050"):
        lock = max(Decimal("0.025"), mfe - Decimal("0.025"))
        cand = entry_px * (Decimal("1") - lock)
        if cand < new_stop:
            new_stop = cand
        if stage in ("INIT", "BE", "TRAIL5"):
            stage = "TRAIL5"
    elif mfe >= Decimal("0.025"):
        cand = entry_px * Decimal("0.996")
        if cand < new_stop:
            new_stop = cand
        if stage == "INIT":
            stage = "BE"
    return new_stop, stage


def _stop_exit_tag(stage, suffix):
    if stage == "INIT":
        return f"STOP_LOSS_{suffix}"
    if stage in ("HALF50", "HALF55", "HALF65"):
        return f"HALF_STOP_{suffix}"
    return f"TRAILING_STOP_{suffix}"


def evaluate_position_exit(
    pos_meta,
    entry_px,
    mark_px,
    bars_15m,
    opened_at,
    now_utc,
    btc_bars_15m=None,
    btc_mark_px=None,
    first_bar_sanitized=False,
    first_bar_1m_failed=False,
    live_b1h=None,
    eth_bars_15m=None,
    eth_mark_px=None,
):
    """Causal SHORT exit evaluation over completed 15m bars plus live mark price and hold duration.

    Supports nine exit modes stored per position in `pos_meta["exit_mode"]`:
      - `V9_AA_SQZ96` (V9 default for All-Age 5 engines): HALF55 (-1.5% @ +5.5%) + LOCK8 + TRAIL13 (-4.5%) +
        TRAIL22 (-3.5%) + TRAIL30 (-2.5% @ +30%) + TP 60% + RUNNER96 + BTC Squeeze Emergency Cut.
      - `V9_NEW7_SQZ96` (V9 default for NEW7 cohort): HALF65 (-2.0% @ +6.5%) + LOCK8 + TRAIL13 + TRAIL22 +
        TRAIL30 + TP 60% + RUNNER96 + BTC Squeeze Emergency Cut.
      - `V8_AA_SQZ96` / `V8_NEW7_SQZ96`: V8 2-stage trail (TRAIL22) + BTC Squeeze Cut (backward-compatible).
      - `V7_AA_HYB96`: causal SL/TP (TP 40%) + HALF55 + LOCK8 + TRAIL13 + RUNNER96.
      - `V6_LT8_96` / `V4_LT8_72` / `V3_PURE24` / `LEGACY`: preserved for older open positions.
    """
    meta = dict(pos_meta or {})
    exit_mode = meta.get("exit_mode") or "LEGACY"
    side_u = str(meta.get("side") or ("LONG" if _is_v22_long_mode(exit_mode) else "SHORT")).upper()
    is_long = (side_u == "LONG")
    is_v9_mode = _is_v9_mode(exit_mode)
    is_sqz_mode = _is_sqz_mode(exit_mode)
    is_runner_mode = _is_runner_mode(exit_mode)
    runner_max_h = _runner_max_hours(exit_mode)
    runner_stage_name = _runner_stage_name(exit_mode)

    entry = dec(entry_px)
    mark = dec(mark_px)
    if entry is None or entry <= Decimal("0") or mark is None or mark <= Decimal("0"):
        return {
            "action": "HOLD",
            "exit_reason": None,
            "side": side_u,
            "cur_pnl_pct": None,
            "mfe_pct": meta.get("mfe_pct", "0"),
            "mae_pct": meta.get("mae_pct", "0"),
            "stop_px": meta.get("stop_px"),
            "tp_px": meta.get("tp_px"),
            "ratchet_stage": meta.get("ratchet_stage", "INIT"),
            "exit_mode": exit_mode,
            "held_hours": 0.0,
            "updated_meta": meta,
        }

    stop_pct = dec(meta.get("stop_pct"), DEFAULT_STOP_PCT)
    stop_pct = min(max(stop_pct, STOP_PCT_MIN), STOP_PCT_MAX)
    tp_pct = dec(meta.get("tp_pct"), DEFAULT_TP_PCT)
    tp_pct = min(max(tp_pct, TP_PCT_MIN), TP_PCT_MAX)

    init_stop = entry * (Decimal("1") - stop_pct) if is_long else entry * (Decimal("1") + stop_pct)
    prev_stop = None if meta.get("tentative_stops") else dec(meta.get("stop_px"))
    if prev_stop is not None:
        stop_px = max(prev_stop, init_stop) if is_long else min(prev_stop, init_stop)
    else:
        stop_px = init_stop
    tp_px = entry * (Decimal("1") + tp_pct) if is_long else entry * (Decimal("1") - tp_pct)

    mfe = dec(meta.get("mfe_pct"), Decimal("0"))
    mae = dec(meta.get("mae_pct"), Decimal("0"))
    stage = meta.get("ratchet_stage") or "INIT"
    runner_unlocked = bool(meta.get("runner_unlocked", stage in ("RUNNER72", "RUNNER96")))
    last_bar_ms = int(meta.get("last_bar_ms") or 0)

    opened_dt = parse_iso_utc(opened_at)
    opened_ms = int(opened_dt.timestamp() * 1000)
    open_bar_start_ms = (opened_ms // 900_000) * 900_000
    # V14 Fix: include sanitized opening 15m bar; if 1m fetch failed, keep opening bar when entered within <=15s of bar open
    if first_bar_sanitized:
        bar_floor_ms = open_bar_start_ms
    elif first_bar_1m_failed and (opened_ms - open_bar_start_ms) > 15_000:
        bar_floor_ms = open_bar_start_ms + 900_000
    elif first_bar_1m_failed:
        bar_floor_ms = open_bar_start_ms
    else:
        bar_floor_ms = opened_ms - FIRST_BAR_GRACE_MS
    checkpoint_24h_ms = opened_ms + MAX_HOLD_HOURS * 3_600_000
    hard_end_h = runner_max_h if is_runner_mode else MAX_HOLD_HOURS
    hard_end_ms = opened_ms + hard_end_h * 3_600_000
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    held_hours = max(0.0, (now_utc.astimezone(dt.timezone.utc) - opened_dt).total_seconds() / 3600.0)
    btc_close_map, _ = _btc_squeeze_returns_from_bars(btc_bars_15m, now_utc) if (is_sqz_mode and btc_bars_15m) else ({}, None)
    eth_close_map, _ = _eth_15m_ref_from_bars(eth_bars_15m, now_utc) if (_sqz_eth_15m_threshold(exit_mode) is not None and eth_bars_15m) else ({}, None)

    action = "HOLD"
    exit_reason = None

    sorted_bars = sorted(
        (int(t), dec(hi), dec(lo), dec(cl))
        for t, hi, lo, cl in (bars_15m or ())
        if int(t) >= bar_floor_ms and int(t) > last_bar_ms and int(t) + 900_000 <= now_ms
    )

    for ts_ms, hi, lo, cl in sorted_bars:
        if hi is None or lo is None:
            continue
        # Hard horizon cap: never process bars starting at/after max hold horizon before triggering MAX_HOLD
        if ts_ms >= hard_end_ms:
            action = "MUST_CLOSE"
            exit_reason = f"MAX_HOLD_{hard_end_h}H (bar horizon {hard_end_h}h reached)"
            break
        # 1. Conservative SL/Trailing check BEFORE TP and BEFORE updating MFE inside the same bar
        if is_long:
            if lo <= stop_px:
                action = "MUST_CLOSE"
                exit_reason = f"{_stop_exit_tag(stage, '15M')} (15m low {lo:.6g} <= stop {stop_px:.6g})"
                last_bar_ms = ts_ms
                break
            if hi >= tp_px:
                action = "MUST_CLOSE"
                exit_reason = f"TAKE_PROFIT_15M (15m high {hi:.6g} >= tp {tp_px:.6g})"
                last_bar_ms = ts_ms
                break
        else:
            if hi >= stop_px:
                action = "MUST_CLOSE"
                exit_reason = f"{_stop_exit_tag(stage, '15M')} (15m high {hi:.6g} >= stop {stop_px:.6g})"
                last_bar_ms = ts_ms
                break
            if lo <= tp_px:
                action = "MUST_CLOSE"
                exit_reason = f"TAKE_PROFIT_15M (15m low {lo:.6g} <= tp {tp_px:.6g})"
                last_bar_ms = ts_ms
                break
        # 2. Bar survived: update MFE/MAE
        bar_mfe = ((hi - entry) / entry) if is_long else ((entry - lo) / entry)
        bar_mae = ((lo - entry) / entry) if is_long else ((entry - hi) / entry)
        if bar_mfe > mfe:
            mfe = bar_mfe
        if bar_mae < mae:
            mae = bar_mae
        if is_long:
            bar_pnl = ((cl - entry) / entry) if cl is not None else ((mark - entry) / entry)
        else:
            bar_pnl = ((entry - cl) / entry) if cl is not None else ((entry - mark) / entry)

        # 2b. V8-V22 BTC & ETH Emergency Cut on unprotected losers (SHORT upward squeeze / LONG downward crash)
        if is_sqz_mode and _is_unprotected_sqz_stage(exit_mode, stage) and bar_pnl < Decimal("0") and ts_ms > open_bar_start_ms:
            if is_long:
                if btc_close_map:
                    cr_hit, r1h, r4h = _check_btc_crash_at(btc_close_map, ts_ms)
                    if cr_hit:
                        r1_s = f"{(r1h * 100):+.2f}%" if r1h is not None else "n/a"
                        r4_s = f"{(r4h * 100):+.2f}%" if r4h is not None else "n/a"
                        action = "MUST_CLOSE"
                        exit_reason = f"BTC_CRASH_CUT_15M (btc 1h {r1_s} / 4h {r4_s}, pnl {(bar_pnl * 100):+.2f}%)"
                        last_bar_ms = ts_ms
                        break
                if eth_close_map:
                    eth_hit, eth_r15 = _check_eth_15m_crash_at(eth_close_map, ts_ms)
                    if eth_hit:
                        e15_s = f"{(eth_r15 * 100):+.2f}%" if eth_r15 is not None else "n/a"
                        action = "MUST_CLOSE"
                        exit_reason = f"ETH_CRASH_CUT_15M (eth 15m {e15_s}, pnl {(bar_pnl * 100):+.2f}%)"
                        last_bar_ms = ts_ms
                        break
            else:
                if btc_close_map:
                    sqz_1h_thr, sqz_4h_thr = _sqz_thresholds(exit_mode)
                    sqz_15m_thr = _sqz_btc_15m_threshold(exit_mode)
                    sqz_hit, r1h, r4h = _check_btc_squeeze_at(
                        btc_close_map, ts_ms, sqz_1h=sqz_1h_thr, sqz_4h=sqz_4h_thr, sqz_15m=sqz_15m_thr
                    )
                    if sqz_hit:
                        r1_s = f"{(r1h * 100):+.2f}%" if r1h is not None else "n/a"
                        r4_s = f"{(r4h * 100):+.2f}%" if r4h is not None else "n/a"
                        action = "MUST_CLOSE"
                        exit_reason = f"BTC_SQUEEZE_CUT_15M (btc 1h {r1_s} / 4h {r4_s}, pnl {(bar_pnl * 100):+.2f}%)"
                        last_bar_ms = ts_ms
                        break
                eth_15m_thr = _sqz_eth_15m_threshold(exit_mode)
                if eth_15m_thr is not None and eth_close_map:
                    eth_hit, eth_r15 = _check_eth_15m_squeeze_at(eth_close_map, ts_ms, sqz_15m=eth_15m_thr)
                    if eth_hit:
                        e15_s = f"{(eth_r15 * 100):+.2f}%" if eth_r15 is not None else "n/a"
                        action = "MUST_CLOSE"
                        exit_reason = f"ETH_SQUEEZE_CUT_15M (eth 15m {e15_s}, pnl {(bar_pnl * 100):+.2f}%)"
                        last_bar_ms = ts_ms
                        break

        # 3. 24h causal checkpoint: close losers/flat (<+5%) at 24h; promote >=+5% to profit-locked runner
        if is_runner_mode and not runner_unlocked and (ts_ms + 900_000) >= checkpoint_24h_ms:
            if bar_pnl < V4_RUNNER_MIN_PNL:
                action = "MUST_CLOSE"
                exit_reason = f"MAX_HOLD_{MAX_HOLD_HOURS}H (24h close pnl {(bar_pnl * 100):.2f}% < +{V4_RUNNER_MIN_PNL * 100:.0f}% runner gate)"
                last_bar_ms = ts_ms
                break
            eff_td = (
                V9_TR3_DIST if (is_v9_mode and mfe >= V9_TR3_MFE)
                else (V8_TR2_DIST if (is_sqz_mode and mfe >= V8_TR2_MFE) else V4_TRAIL_DIST)
            )
            lock = max(V4_RUNNER_LOCK, mfe - eff_td)
            if is_long:
                cand = entry * (Decimal("1") + lock)
                if cand > stop_px:
                    stop_px = cand
            else:
                cand = entry * (Decimal("1") - lock)
                if cand < stop_px:
                    stop_px = cand
            runner_unlocked = True
            stage = runner_stage_name
            last_bar_ms = ts_ms
            continue

        # 4. Ratchet trailing stop for NEXT bar only
        if is_long:
            stop_px, stage = _ratchet_stop_v22_long(entry, mfe, stop_px, stage)
        elif exit_mode in ("V18_AA_SQZ96", "V17_AA_SQZ96", "V14_AA_SQZ96", "V12_AA_SQZ96", "V11_AA_SQZ96"):
            stop_px, stage = _ratchet_stop_v11_aa(entry, mfe, stop_px, stage)
        elif exit_mode in ("V21_NEW7_SQZ96", "V14_NEW7_SQZ96", "V12_NEW7_SQZ96", "V11_NEW7_SQZ96", "V10_NEW7_SQZ96", "V9_NEW7_SQZ96"):
            stop_px, stage = _ratchet_stop_v9_new7(entry, mfe, stop_px, stage)
        elif exit_mode == "V10_AA_SQZ96":
            stop_px, stage = _ratchet_stop_v10_aa(entry, mfe, stop_px, stage)
        elif exit_mode == "V9_AA_SQZ96":
            stop_px, stage = _ratchet_stop_v9_aa(entry, mfe, stop_px, stage)
        elif exit_mode == "V8_AA_SQZ96":
            stop_px, stage = _ratchet_stop_v8_aa(entry, mfe, stop_px, stage)
        elif exit_mode == "V8_NEW7_SQZ96":
            stop_px, stage = _ratchet_stop_v8_new7(entry, mfe, stop_px, stage)
        elif exit_mode == "V7_AA_HYB96":
            stop_px, stage = _ratchet_stop_v7_aa(entry, mfe, stop_px, stage)
        elif is_runner_mode:
            stop_px, stage = _ratchet_stop_v4(entry, mfe, stop_px, stage)
        elif exit_mode == "LEGACY":
            stop_px, stage = _ratchet_stop_legacy(entry, mfe, stop_px, stage)
        last_bar_ms = ts_ms

    cur_pnl = ((mark - entry) / entry) if is_long else ((entry - mark) / entry)

    if action == "HOLD":
        if is_long:
            if mark <= stop_px:
                action = "MUST_CLOSE"
                exit_reason = f"{_stop_exit_tag(stage, 'MARK')} (mark {mark:.6g} <= stop {stop_px:.6g})"
            elif mark >= tp_px:
                action = "MUST_CLOSE"
                exit_reason = f"TAKE_PROFIT_MARK (mark {mark:.6g} >= tp {tp_px:.6g})"
        else:
            if mark >= stop_px:
                action = "MUST_CLOSE"
                exit_reason = f"{_stop_exit_tag(stage, 'MARK')} (mark {mark:.6g} >= stop {stop_px:.6g})"
            elif mark <= tp_px:
                action = "MUST_CLOSE"
                exit_reason = f"TAKE_PROFIT_MARK (mark {mark:.6g} <= tp {tp_px:.6g})"
        eth_15m_thr_live = _sqz_eth_15m_threshold(exit_mode)
        b1h_thr_live = _sqz_b1h_threshold(exit_mode)
        if is_sqz_mode and action == "HOLD" and _is_unprotected_sqz_stage(exit_mode, stage) and cur_pnl < Decimal("0") and held_hours >= 0.25 and (
            (btc_close_map and btc_mark_px is not None)
            or (eth_15m_thr_live is not None and eth_close_map and eth_mark_px is not None)
            or (not is_long and b1h_thr_live is not None and dec(live_b1h) is not None)
        ):
            if is_long:
                cr_live = False
                r1_l = r4_l = None
                if btc_close_map and btc_mark_px is not None:
                    cr_live, r1_l, r4_l = _eval_live_btc_crash_from_map(btc_close_map, btc_mark_px, now_utc)
                eth_cr_live = False
                eth_r15_l = None
                if eth_close_map and eth_mark_px is not None:
                    eth_cr_live, eth_r15_l = _eval_live_eth_15m_crash_from_map(eth_close_map, eth_mark_px, now_utc)
                if cr_live:
                    r1_s = f"{(r1_l * 100):+.2f}%" if r1_l is not None else "n/a"
                    r4_s = f"{(r4_l * 100):+.2f}%" if r4_l is not None else "n/a"
                    action = "MUST_CLOSE"
                    exit_reason = f"BTC_CRASH_CUT_MARK (btc 1h {r1_s} / 4h {r4_s}, pnl {(cur_pnl * 100):+.2f}%)"
                elif eth_cr_live:
                    e15_s = f"{(eth_r15_l * 100):+.2f}%" if eth_r15_l is not None else "n/a"
                    action = "MUST_CLOSE"
                    exit_reason = f"ETH_CRASH_CUT_MARK (eth 15m {e15_s}, pnl {(cur_pnl * 100):+.2f}%)"
            else:
                # V10-V21 SHORT: rolling-window BTC 15m/1h/4h + ETH 15m + Broad Altcoin 1H Breadth Squeeze
                sqz_live = False
                r1_l = r4_l = None
                if btc_close_map and btc_mark_px is not None:
                    sqz_1h_thr, sqz_4h_thr = _sqz_thresholds(exit_mode)
                    sqz_15m_thr = _sqz_btc_15m_threshold(exit_mode)
                    sqz_live, r1_l, r4_l = _eval_live_btc_squeeze_from_map(
                        btc_close_map, btc_mark_px, now_utc, sqz_1h=sqz_1h_thr, sqz_4h=sqz_4h_thr, sqz_15m=sqz_15m_thr
                    )
                eth_live = False
                eth_r15_l = None
                if eth_15m_thr_live is not None and eth_close_map and eth_mark_px is not None:
                    eth_live, eth_r15_l = _eval_live_eth_15m_from_map(
                        eth_close_map, eth_mark_px, now_utc, sqz_15m=eth_15m_thr_live
                    )
                b1h_d = dec(live_b1h) if b1h_thr_live is not None else None
                if sqz_live:
                    r1_s = f"{(r1_l * 100):+.2f}%" if r1_l is not None else "n/a"
                    r4_s = f"{(r4_l * 100):+.2f}%" if r4_l is not None else "n/a"
                    action = "MUST_CLOSE"
                    exit_reason = f"BTC_SQUEEZE_CUT_MARK (btc 1h {r1_s} / 4h {r4_s}, pnl {(cur_pnl * 100):+.2f}%)"
                elif eth_live:
                    e15_s = f"{(eth_r15_l * 100):+.2f}%" if eth_r15_l is not None else "n/a"
                    action = "MUST_CLOSE"
                    exit_reason = f"ETH_SQUEEZE_CUT_MARK (eth 15m {e15_s}, pnl {(cur_pnl * 100):+.2f}%)"
                elif b1h_d is not None and b1h_thr_live is not None and b1h_d >= b1h_thr_live:
                    action = "MUST_CLOSE"
                    exit_reason = f"B1H_SQUEEZE_CUT_MARK (b1h {(b1h_d * 100):.1f}% >= {b1h_thr_live * 100:.0f}%, pnl {(cur_pnl * 100):+.2f}%)"
        if action == "HOLD":
            if is_runner_mode:
                if held_hours >= runner_max_h:
                    action = "MUST_CLOSE"
                    exit_reason = f"MAX_HOLD_{runner_max_h}H (runner held {held_hours:.1f}h)"
                elif held_hours >= MAX_HOLD_HOURS and not runner_unlocked:
                    if cur_pnl >= V4_RUNNER_MIN_PNL:
                        eff_td = (
                            V9_TR3_DIST if (is_v9_mode and mfe >= V9_TR3_MFE)
                            else (V8_TR2_DIST if (is_sqz_mode and mfe >= V8_TR2_MFE) else V4_TRAIL_DIST)
                        )
                        lock = max(V4_RUNNER_LOCK, mfe - eff_td)
                        if is_long:
                            cand = entry * (Decimal("1") + lock)
                            if cand > stop_px:
                                stop_px = cand
                        else:
                            cand = entry * (Decimal("1") - lock)
                            if cand < stop_px:
                                stop_px = cand
                        runner_unlocked = True
                        stage = runner_stage_name
                    else:
                        action = "MUST_CLOSE"
                        exit_reason = f"MAX_HOLD_{MAX_HOLD_HOURS}H (held {held_hours:.1f}h, pnl {(cur_pnl * 100):.2f}% < +{V4_RUNNER_MIN_PNL * 100:.0f}%)"
            elif held_hours >= MAX_HOLD_HOURS:
                action = "MUST_CLOSE"
                exit_reason = f"MAX_HOLD_{MAX_HOLD_HOURS}H (held {held_hours:.1f}h)"
            elif (
                exit_mode == "LEGACY"
                and held_hours >= STAGNANT_HOLD_HOURS
                and cur_pnl < STAGNANT_MIN_PROFIT
                and mfe < STAGNANT_MAX_MFE
            ):
                action = "MUST_CLOSE"
                exit_reason = f"STAGNANT_{STAGNANT_HOLD_HOURS}H (held {held_hours:.1f}h, pnl {(cur_pnl * 100):.2f}%, mfe {(mfe * 100):.2f}%)"

    updated_meta = {
        **meta,
        "side": side_u,
        "exit_mode": exit_mode,
        "entry": str(entry),
        "stop_pct": str(stop_pct),
        "tp_pct": str(tp_pct),
        "stop_px": str(stop_px),
        "tp_px": str(tp_px),
        "mfe_pct": str(mfe.quantize(Decimal("0.000001"))),
        "mae_pct": str(mae.quantize(Decimal("0.000001"))),
        "ratchet_stage": stage,
        "runner_unlocked": runner_unlocked,
        "last_bar_ms": last_bar_ms,
    }
    updated_meta.pop("tentative_stops", None)
    return {
        "action": action,
        "exit_reason": exit_reason,
        "side": side_u,
        "cur_pnl_pct": str((cur_pnl * Decimal("100")).quantize(Decimal("0.01"))),
        "mfe_pct": str((mfe * Decimal("100")).quantize(Decimal("0.01"))),
        "mae_pct": str((mae * Decimal("100")).quantize(Decimal("0.01"))),
        "stop_px": str(stop_px.quantize(Decimal("0.00000001")).normalize()),
        "tp_px": str(tp_px.quantize(Decimal("0.00000001")).normalize()),
        "ratchet_stage": stage,
        "runner_unlocked": runner_unlocked,
        "exit_mode": exit_mode,
        "held_hours": round(held_hours, 2),
        "updated_meta": updated_meta,
    }


def cooldown_symbols(portfolio, now_utc=None):
    """Symbols blocked by cooldown:
      - Closed < 4h ago (any outcome)
      - Closed at net loss < 72h (3 days) ago
      - Hit >= 2 net losing closes within rolling 7 days (anti-revenge squeeze guard)
    """
    now_dt = (now_utc or utcnow()).astimezone(dt.timezone.utc)
    rest_cut = now_dt - dt.timedelta(hours=SHORT_REST_HOURS)
    loss_cut = now_dt - dt.timedelta(days=SHORT_COOLDOWN_DAYS)
    week_cut = now_dt - dt.timedelta(days=7)

    cooldown_until = {}
    loss_cnt_7d = {}
    rest_delta = dt.timedelta(hours=SHORT_REST_HOURS)
    loss_delta = dt.timedelta(days=SHORT_COOLDOWN_DAYS)
    for row in sorted((portfolio or {}).get("closed") or [], key=lambda x: x.get("closed_at") or ""):
        sym = row.get("symbol")
        if not sym:
            continue
        try:
            closed_at = parse_iso_utc(row["closed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        pnl = dec(row.get("pnl") if row.get("pnl") is not None else row.get("pnl_usdt"), Decimal("0"))
        until = closed_at + (loss_delta if pnl < Decimal("0") else rest_delta)
        prev_u = cooldown_until.get(sym)
        if prev_u is None or until > prev_u:
            cooldown_until[sym] = until
        if pnl < Decimal("0") and closed_at > week_cut:
            loss_cnt_7d[sym] = loss_cnt_7d.get(sym, 0) + 1

    out = set()
    for sym, until in cooldown_until.items():
        if now_dt < until:
            out.add(sym)
    for sym, cnt in loss_cnt_7d.items():
        if cnt >= MAX_LOSSES_PER_SYM_7D:
            out.add(sym)
    return out


def opened_today_utc(portfolio, today_iso):
    """Symbols already sent as non-rejected opens on `today_iso` UTC (checks orders, shorts & closed to beat 30-order cap)."""
    out = set()
    for o in (portfolio or {}).get("orders") or []:
        if o.get("kind") == "open" and o.get("how") != "rejected" and str(o.get("at") or "").startswith(today_iso):
            if o.get("symbol"):
                out.add(o["symbol"])
    for p in (portfolio or {}).get("shorts") or []:
        if (str(p.get("run_id") or "") == today_iso or str(p.get("opened_at") or "").startswith(today_iso)) and p.get("symbol"):
            out.add(p["symbol"])
    for c in (portfolio or {}).get("closed") or []:
        if str(c.get("opened_at") or "").startswith(today_iso) and c.get("symbol"):
            out.add(c["symbol"])
    return out


def opened_this_hour_utc(portfolio, now_utc):
    """Symbols already sent as non-rejected opens in the current UTC hour (enforces sim_v31 max_per_hour persistence)."""
    now_u = now_utc.astimezone(dt.timezone.utc)
    hr_prefix_t = f"{now_u:%Y-%m-%dT%H}"
    hr_prefix_s = f"{now_u:%Y-%m-%d %H}"
    out = set()
    for o in (portfolio or {}).get("orders") or []:
        at_s = str(o.get("at") or "")
        if o.get("kind") == "open" and o.get("how") != "rejected" and (at_s.startswith(hr_prefix_t) or at_s.startswith(hr_prefix_s)):
            if o.get("symbol"):
                out.add(o["symbol"])
    for p in (portfolio or {}).get("shorts") or []:
        op_s = str(p.get("opened_at") or "")
        if (op_s.startswith(hr_prefix_t) or op_s.startswith(hr_prefix_s)) and p.get("symbol"):
            out.add(p["symbol"])
    for c in (portfolio or {}).get("closed") or []:
        op_s = str(c.get("opened_at") or "")
        if (op_s.startswith(hr_prefix_t) or op_s.startswith(hr_prefix_s)) and c.get("symbol"):
            out.add(c["symbol"])
    return out


def count_open_by_side(portfolio, pos_meta_all=None):
    """Count active bot positions split by side (`SHORT` vs `LONG`)."""
    counts = {"SHORT": 0, "LONG": 0}
    meta_map = pos_meta_all or {}
    for p in (portfolio or {}).get("shorts") or []:
        if not (p.get("on_binance") or not p.get("entry") or p.get("unclear")):
            continue
        pid = p.get("id")
        meta = meta_map.get(pid) or {}
        side_u = str(p.get("side") or meta.get("side") or "SHORT").upper()
        if side_u == "LONG":
            counts["LONG"] += 1
        else:
            counts["SHORT"] += 1
    return counts


def compute_open_heat_and_counts(portfolio, pos_meta_all):
    """Compute unprotected open SL risk in USDT across SHORT+LONG (profit-locked stops count as 0) and active counts per setup."""
    total_heat = Decimal("0")
    setup_counts = {}
    meta_map = pos_meta_all or {}
    for p in (portfolio or {}).get("shorts") or []:
        if not (p.get("on_binance") or not p.get("entry") or p.get("unclear")):
            continue
        pid = p.get("id")
        meta = meta_map.get(pid) or {}
        setup = meta.get("setup") or "LEGACY"
        setup_counts[setup] = setup_counts.get(setup, 0) + 1
        side_u = str(p.get("side") or meta.get("side") or "SHORT").upper()
        is_long = (side_u == "LONG")
        entry = dec(p.get("entry") or meta.get("entry"))
        qty = dec(p.get("qty"))
        notional = abs(qty * entry) if (qty is not None and qty != Decimal("0") and entry is not None and entry > Decimal("0")) else LOT_USDT
        stop_px = dec(meta.get("stop_px"))
        stop_pct = min(max(dec(meta.get("stop_pct"), DEFAULT_STOP_PCT), STOP_PCT_MIN), STOP_PCT_MAX)
        if entry is not None and entry > Decimal("0") and stop_px is not None:
            if is_long:
                risk_u = Decimal("0") if stop_px >= entry else (notional * (entry - stop_px) / entry)
            else:
                risk_u = Decimal("0") if stop_px <= entry else (notional * (stop_px - entry) / entry)
        else:
            risk_u = notional * stop_pct
        if risk_u > Decimal("0"):
            total_heat += risk_u
    return total_heat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), setup_counts


def validate_open_guardrails(symbol, side, stop_pct, tp_pct, portfolio, state, now_utc, spread=None):
    """Hard guardrail check before sending POST /open. Returns (ok, error_message_or_None)."""
    sym = normalize_symbol(symbol)
    if not _SYMBOL_RE.fullmatch(sym):
        return False, f"ชื่อสัญญาไม่ถูกต้อง: {symbol!r}"
    side_u = str(side or "").upper()
    if side_u not in ALLOWED_SIDES:
        return False, f"ระบบนี้อนุญาตเฉพาะ SHORT หรือ LONG เท่านั้น (ได้รับ {side!r})"
    if sym == "BTCUSDT" or sym in market.BLACKLIST_PERPS or sym in market.GOLD_PERPS:
        return False, f"ห้ามเทรดสัญญาต้องห้าม/ทอง/สเตเบิลคอยน์/BTC: {sym}"

    sp = dec(stop_pct)
    tp = dec(tp_pct)
    if sp is None or not (STOP_PCT_MIN <= sp <= STOP_PCT_MAX):
        return False, f"stop_pct ต้องอยู่ในช่วง {STOP_PCT_MIN}..{STOP_PCT_MAX} (ได้รับ {stop_pct})"
    if tp is None or not (TP_PCT_MIN <= tp <= TP_PCT_MAX):
        return False, f"tp_pct ต้องอยู่ในช่วง {TP_PCT_MIN}..{TP_PCT_MAX} (ได้รับ {tp_pct})"

    if (state or {}).get("circuit_breaker_tripped"):
        return False, f"Circuit Breaker ทำงานอยู่ ห้ามเปิดไม้ใหม่ ({state.get('tripped_reason') or 'ขาดทุนเกินเกณฑ์'})"

    pf = portfolio or {}
    if not pf or pf.get("error"):
        return False, f"อ่านพอร์ตจาก Proxy ไม่ได้: {safe_err(pf.get('error') or 'empty portfolio')}"

    unclear_coins = [x.get("coin") or x.get("symbol") for x in pf.get("shorts") or [] if x.get("unclear")]
    if unclear_coins:
        return False, f"มีคำสั่งที่ยังไม่รู้ผลค้างอยู่ ({', '.join(unclear_coins)}) — ห้ามเปิดไม้ใหม่จนกว่าจะ sync/ตรวจเสร็จ"

    shorts_active = [x for x in pf.get("shorts") or [] if x.get("on_binance") or not x.get("entry")]
    held = set(pf.get("held_symbols") or ()) | {x["symbol"] for x in shorts_active if x.get("symbol")}
    pending_settle_syms = {x["symbol"] for x in pf.get("shorts") or [] if x.get("symbol") and not x.get("on_binance") and x.get("entry")}
    if sym in held:
        return False, f"{sym} มี position อยู่แล้วบน Binance หรือบอทถืออยู่"
    if sym in pending_settle_syms:
        return False, f"{sym} เพิ่งปิดและกำลังรอ Binance ยืนยัน REALIZED_PNL (ห้ามเปิดซ้ำทันที)"

    if MAX_OPEN > 0 and len(held) >= MAX_OPEN:
        return False, f"ถือครบเพดาน {MAX_OPEN} ไม้แล้ว (นับรวม position ทั้งหมดบน Binance)"
    side_counts = count_open_by_side(pf)
    if MAX_LONG_OPEN > 0 and side_u == "LONG" and side_counts["LONG"] >= MAX_LONG_OPEN:
        return False, f"ถือฝั่ง LONG ครบเพดาน {MAX_LONG_OPEN} ไม้แล้ว" + (f" (รวม SHORT+LONG ≤ {MAX_OPEN})" if MAX_OPEN > 0 else "")
    if MAX_SHORT_OPEN > 0 and side_u == "SHORT" and side_counts["SHORT"] >= MAX_SHORT_OPEN:
        return False, f"ถือฝั่ง SHORT ครบเพดาน {MAX_SHORT_OPEN} ไม้แล้ว"

    avail = dec(pf.get("available_usdt"))
    if avail is None or avail < MIN_AVAIL_USDT:
        return False, f"available_usdt เหลือ {pf.get('available_usdt')} < ขั้นต่ำ {MIN_AVAIL_USDT} USDT"

    now_dt = now_utc.astimezone(dt.timezone.utc)
    if ENFORCE_OPEN_COOLDOWN and sym in cooldown_symbols(pf, now_dt):
        return False, f"{sym} ยังติด cooldown หลังปิด ({SHORT_REST_HOURS} ชม. / ขาดทุน {SHORT_COOLDOWN_DAYS} วัน / แพ้ครบ {MAX_LOSSES_PER_SYM_7D} ครั้งใน 7 วัน)"

    today_iso = now_dt.date().isoformat()
    opened_today = opened_today_utc(pf, today_iso)
    if sym in opened_today:
        return False, f"{sym} เปิดของแผนวันที่ {today_iso} (UTC) ไปแล้วในวันนี้ (Proxy ห้าม id ซ้ำ)"
    if DAILY_MAX_OPENS > 0 and len(opened_today) >= DAILY_MAX_OPENS:
        return False, f"วันนี้ (UTC) เปิดครบเพดานเล่นสั้น {DAILY_MAX_OPENS} ไม้แล้ว — พักเพื่อกัน overtrade"

    if spread is not None:
        spr = dec(spread)
        if spr is None or spr < Decimal("0") or spr > MAX_SPREAD:
            return False, f"spread ของ {sym} สูงเกินเพดาน ({spr} > {MAX_SPREAD})"

    return True, None


_HTTPS_POOL_HOSTS = frozenset({"fapi.binance.com", "api.binance.com"})
_HTTPS_TLS = threading.local()


def _close_thread_https_conn(host):
    conns = getattr(_HTTPS_TLS, "conns", None)
    if not conns:
        return
    conn = conns.pop(host, None)
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()


def _http_json(url, timeout=20):
    """Fetch JSON over HTTPS using a thread-local Keep-Alive pool for Binance hosts (cuts 200+ TLS handshakes/hr)."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme == "https" and host in _HTTPS_POOL_HOSTS and parsed.port in (None, 443):
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conns = getattr(_HTTPS_TLS, "conns", None)
        if conns is None:
            conns = {}
            _HTTPS_TLS.conns = conns
        headers = {"User-Agent": "bottread-agy/2.0", "Connection": "keep-alive"}
        for attempt in range(2):
            conn = conns.get(host)
            if conn is None:
                conn = http.client.HTTPSConnection(host, timeout=timeout)
                conns[host] = conn
            else:
                conn.timeout = timeout
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status >= 400:
                    raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
                return json.loads(body.decode("utf-8"))
            except (
                http.client.HTTPException,
                BrokenPipeError,
                ConnectionResetError,
                TimeoutError,
                OSError,
            ):
                _close_thread_https_conn(host)
                if attempt == 1:
                    raise
    req = urllib.request.Request(url, headers={"User-Agent": "bottread-agy/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_usdt_perps_with_onboard():
    """Return `(perps_map, onboard_ms_map)` for live Binance COIN USDT perpetuals in a single API call."""
    info = _http_json(FAPI_INFO, timeout=25)
    perps = {}
    onboard_ms = {}
    for s in info.get("symbols") or []:
        if (
            s.get("underlyingType") == "COIN"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        ):
            sym = s["symbol"]
            perps[sym] = s["baseAsset"]
            if s.get("onboardDate"):
                with contextlib.suppress(TypeError, ValueError):
                    onboard_ms[sym] = int(s["onboardDate"])
    return perps, onboard_ms


def _summarize_last_15m_rows(rows, n=3):
    """Return compact last `n` 15m bars with OHLC close/high/low and taker-buy ratio (0 extra API calls)."""
    out = []
    for r in (rows or [])[-n:]:
        try:
            t_hhmm = dt.datetime.fromtimestamp(int(r[0]) / 1000.0, tz=dt.timezone.utc).strftime("%H:%M")
            vol = float(r[5])
            tb = round(float(r[9]) / vol, 2) if vol > 0 else None
            out.append({
                "t": t_hhmm,
                "h": round(float(r[2]), 8),
                "l": round(float(r[3]), 8),
                "c": round(float(r[4]), 8),
                "tb": tb,
            })
        except (IndexError, TypeError, ValueError):
            continue
    return out


def fetch_klines_15m_raw(symbol, limit=400):
    q = urllib.parse.urlencode({"symbol": symbol, "interval": "15m", "limit": limit})
    rows = _http_json(f"{FAPI_KLINES}?{q}", timeout=20)
    bars = [(int(r[0]), dec(r[2]), dec(r[3]), dec(r[4])) for r in rows]
    return bars, _summarize_last_15m_rows(rows, n=3)


def fetch_klines_15m(symbol, limit=400):
    bars, _ = fetch_klines_15m_raw(symbol, limit=limit)
    return bars


def fetch_klines_1m(symbol, limit=25, start_time_ms=None):
    params = {"symbol": symbol, "interval": "1m", "limit": limit}
    if start_time_ms is not None:
        params["startTime"] = int(start_time_ms)
    q = urllib.parse.urlencode(params)
    rows = _http_json(f"{FAPI_KLINES}?{q}", timeout=15)
    return [(int(r[0]), dec(r[2]), dec(r[3]), dec(r[4])) for r in rows]


def sanitize_first_bar_1m(bars_15m, bars_1m, opened_at, now_utc):
    """Replace the opening 15m bar's OHLC with post-entry completed 1m bars when entry occurs mid-bar.

    Eliminates phantom pre-entry SL hits or phantom MFE ratchets caused by wicks in the seconds
    before order execution (e.g. HH:00:00..HH:00:15 before an HH:00:15 fill).
    """
    if not bars_15m or not opened_at:
        return bars_15m
    try:
        opened_ms = int(parse_iso_utc(opened_at).timestamp() * 1000)
    except (TypeError, ValueError):
        return bars_15m
    bar_start = (opened_ms // 900_000) * 900_000
    if opened_ms == bar_start:
        return bars_15m
    bar_end = bar_start + 900_000
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    if bar_end > now_ms:
        return bars_15m

    # Post-entry 1m bars starting at the first full minute >= opened_ms inside [bar_start, bar_end)
    first_1m_ms = ((opened_ms + 59_999) // 60_000) * 60_000
    post_1m = sorted(
        (int(t), dec(hi), dec(lo), dec(cl))
        for t, hi, lo, cl in (bars_1m or ())
        if first_1m_ms <= int(t) < bar_end and int(t) + 60_000 <= now_ms
        and dec(hi) is not None and dec(lo) is not None and dec(cl) is not None
    )
    out = []
    for b in bars_15m:
        if int(b[0]) == bar_start:
            if post_1m:
                out.append((
                    bar_start,
                    max(x[1] for x in post_1m),
                    min(x[2] for x in post_1m),
                    post_1m[-1][3],
                ))
            # If no full post-entry 1m bars exist (e.g. entry at :14:30), omit the pre-entry 15m bar;
            # live mark-price fast-guard protects the entry seconds directly.
        else:
            out.append(b)
    return out


def _live_bars_to_m15_series(bars_15m, now_utc, start_iso=None):
    """Convert Binance 15m klines `(open_ms, hi, lo, cl)` to completed-bar UTC series `(t, cl, hi, lo)`."""
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    rows = sorted(
        (int(t), float(hi), float(lo), float(cl))
        for t, hi, lo, cl in (bars_15m or ())
        if hi is not None and lo is not None and cl is not None and int(t) + 900_000 <= now_ms
    )
    m15_t, m15_cl, m15_hi, m15_lo = [], [], [], []
    for t_ms, hi, lo, cl in rows:
        ts = dt.datetime.fromtimestamp(t_ms / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if start_iso and ts[:10] < start_iso:
            continue
        m15_t.append(ts)
        m15_cl.append(cl)
        m15_hi.append(hi)
        m15_lo.append(lo)
    return m15_t, m15_cl, m15_hi, m15_lo


def _compute_m15_stats(m15_cl, m15_hi, m15_lo, cur_px):
    """Compute 15m EMA20/SMA50/RSI/ATR/16-bar swing levels from completed 15m bars."""
    if len(m15_cl) < 55:
        return (None,) * 12
    ema20_15m = market.ema(m15_cl, 20)
    sma50_15m = _sma(m15_cl, 50)
    rsi15m_prev = market.rsi(m15_cl[:-1], 14)
    rsi15m_now = market.rsi(m15_cl, 14)
    atr14_15m = _atr(m15_hi, m15_lo, m15_cl, 14)
    atr14_15m_pct = (atr14_15m / cur_px) if (atr14_15m is not None and cur_px > 0) else None
    low16_15m = min(m15_lo[-17:-1])
    low16_15m_prev = min(m15_lo[-18:-2])
    res16_15m = max(m15_hi[-17:-1])
    return (
        ema20_15m, sma50_15m, rsi15m_prev, rsi15m_now, atr14_15m_pct,
        low16_15m, low16_15m_prev, res16_15m,
        m15_cl[-1], m15_cl[-2], m15_hi[-1], m15_hi[-2],
    )


def fetch_live_spread(symbol):
    q = urllib.parse.urlencode({"symbol": symbol})
    b = _http_json(f"{FAPI_BOOK_TICKER}?{q}", timeout=15)
    bid, ask = dec(b.get("bidPrice")), dec(b.get("askPrice"))
    if bid is None or ask is None or bid <= Decimal("0"):
        return None
    return (ask - bid) / bid


def fetch_mark_prices():
    """Fetch live mark prices for all Binance USDT perpetuals in a single lightweight API call."""
    rows = _http_json(FAPI_PREMIUM_INDEX, timeout=10)
    out = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        mp = dec(row.get("markPrice"))
        if sym and mp is not None and mp > Decimal("0"):
            out[sym] = mp
    return out


def _atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return sum(trs[-period:]) / float(period) if len(trs) >= period else None


def _sma(xs, period):
    return (sum(xs[-period:]) / float(period)) if len(xs) >= period else None


def _hourly_to_4h(hrs, h_cl):
    """Aggregate 1h closes into completed 4h UTC bars (oldest -> newest)."""
    buckets = {}
    for h_str, c in zip(hrs, h_cl):
        hr = int(h_str[11:13])
        key = f"{h_str[:10]} {hr // 4 * 4:02d}"
        buckets[key] = buckets.get(key, []) + [c]
    return [vals[-1] for _, vals in sorted(buckets.items()) if len(vals) == 4]


def _hourly_to_2h_flow(hrs, h_op, h_hi, h_lo, h_cl, h_qv, h_tb, h_cnt):
    """Aggregate completed 1h flow series into completed 2h UTC bars matching backtest `aggregate_tf(..., 7200)`.

    Returns list of tuples `(key_utc_start, open, high, low, close, qv, tb_ratio, complete_15m_cnt)` sorted oldest->newest.
    Only includes buckets with both hourly rows present.
    """
    buckets = {}
    for h_str, op, hi, lo, cl, qv, tb, cnt in zip(hrs, h_op, h_hi, h_lo, h_cl, h_qv, h_tb, h_cnt):
        hr = int(h_str[11:13])
        key = f"{h_str[:10]} {hr // 2 * 2:02d}:00:00"
        buckets.setdefault(key, []).append((h_str, op, hi, lo, cl, qv, tb, cnt))
    out = []
    for key, rows in sorted(buckets.items()):
        if len(rows) != 2:
            continue
        rows.sort(key=lambda x: x[0])
        tot_qv = sum(r[5] for r in rows)
        tot_tb_qv = sum(r[5] * r[6] for r in rows)
        tb_2h = (tot_tb_qv / tot_qv) if tot_qv > 0 else 0.5
        out.append((
            key,
            rows[0][1],
            max(r[2] for r in rows),
            min(r[3] for r in rows),
            rows[-1][4],
            tot_qv,
            tb_2h,
            sum(r[7] for r in rows),
        ))
    return out


def _hourly_to_4h_flow(hrs, h_op, h_hi, h_lo, h_cl, h_qv, h_tb, h_cnt):
    """Aggregate completed 1h flow series into completed 4h UTC bars matching backtest `aggregate_tf(..., 14400)`.

    Returns list of tuples `(key_utc_start, open, high, low, close, qv, tb_ratio, complete_15m_cnt)` sorted oldest->newest.
    Only includes buckets with all 4 hourly rows present.
    """
    buckets = {}
    for h_str, op, hi, lo, cl, qv, tb, cnt in zip(hrs, h_op, h_hi, h_lo, h_cl, h_qv, h_tb, h_cnt):
        hr = int(h_str[11:13])
        key = f"{h_str[:10]} {hr // 4 * 4:02d}:00:00"
        buckets.setdefault(key, []).append((h_str, op, hi, lo, cl, qv, tb, cnt))
    out = []
    for key, rows in sorted(buckets.items()):
        if len(rows) != 4:
            continue
        rows.sort(key=lambda x: x[0])
        tot_qv = sum(r[5] for r in rows)
        tot_tb_qv = sum(r[5] * r[6] for r in rows)
        tb_4h = (tot_tb_qv / tot_qv) if tot_qv > 0 else 0.5
        out.append((
            key,
            rows[0][1],
            max(r[2] for r in rows),
            min(r[3] for r in rows),
            rows[-1][4],
            tot_qv,
            tb_4h,
            sum(r[7] for r in rows),
        ))
    return out


def fetch_spot_24h_qv():
    """Fetch live Binance Spot 24h quoteVolume for all USDT pairs in one lightweight MINI ticker call.

    Prevents ClickHouse Spot 15m ingestion gaps from understating `spot_qv_24h` and falsely inflating `ps_vrat`.
    """
    rows = _http_json(SPOT_TICKER_24H, timeout=15)
    out = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol")
        if not sym or not sym.endswith("USDT"):
            continue
        with contextlib.suppress(TypeError, ValueError):
            qv = float(row.get("quoteVolume") or 0.0)
            if qv > 0.0:
                out[sym] = qv
    return out


def _spot_15m_to_1h_flow(spot_map, target_hour_str, end_ts_str=None):
    """Aggregate Spot 15m map `{t_str: (close, qv, tb_qv[, tc[, open, high, low]])}` into completed 1H UTC bars.

    Exact parity with backtest `_agg_spot_v3(..., 3600)` + `_agg_extra_phase(..., 3600)`:
    requires 4/4 completed 15m bars in `target_hour_str` and 20 prior completed 1H bars for `spot_vrat_1h` & `spot_tsize_r_1h`,
    plus Spot OHLC upper-wick (`spot_uwick_1h`) and red-bar (`spot_red_1h`) for V21 `SP_UWICK_1H`.
    """
    empty = {
        "spot_1h_complete": False,
        "spot_tb_1h": None,
        "spot_qv_1h": None,
        "spot_qv_avg20_1h": None,
        "spot_vrat_1h": None,
        "spot_tc_1h": None,
        "spot_tsize_r_1h": None,
        "spot_red_1h": None,
        "spot_green_1h": None,
        "spot_uwick_1h": None,
        "spot_lwick_1h": None,
    }
    if not spot_map or not target_hour_str:
        return empty
    buckets = {}
    for t_str in sorted(spot_map.keys()):
        if end_ts_str and t_str > end_ts_str:
            continue
        sp = spot_map[t_str]
        if not sp or len(sp) < 2:
            continue
        sc, sqv = sp[0], sp[1]
        has_tb_col = len(sp) >= 3 and sp[2] is not None
        stb_qv = sp[2] if has_tb_col else 0.0
        stc = sp[3] if (len(sp) >= 4 and sp[3] is not None) else 0.0
        has_ohlc = len(sp) >= 7 and sp[4] is not None and sp[5] is not None and sp[6] is not None and sp[4] > 0
        if sc <= 0:
            continue
        hr_key = f"{t_str[:13]}:00:00"
        rec = buckets.setdefault(hr_key, [0, 0.0, 0.0, False, 0.0, 0, None, None, None, None])
        rec[0] += 1
        rec[1] += sqv
        rec[2] += stb_qv
        if has_tb_col and sqv > 0.0:
            rec[3] = True
        rec[4] += stc
        if has_ohlc:
            so, sh, sl = float(sp[4]), float(sp[5]), float(sp[6])
            rec[5] += 1
            if rec[6] is None:
                rec[6] = so
            rec[7] = sh if rec[7] is None else max(rec[7], sh)
            rec[8] = sl if rec[8] is None else min(rec[8], sl)
            rec[9] = float(sc)
    complete_hrs = [(h, v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9]) for h, v in sorted(buckets.items()) if v[0] == 4]
    if not complete_hrs or complete_hrs[-1][0] != target_hour_str:
        return empty
    _, qv_now, tb_qv_now, has_tb, tc_now, ohlc_cnt, sp_o, sp_h, sp_l, sp_c = complete_hrs[-1]
    spot_tb_1h = (tb_qv_now / qv_now) if (qv_now > 0.0 and has_tb) else None
    spot_red_1h = None
    spot_green_1h = None
    spot_uwick_1h = None
    spot_lwick_1h = None
    if ohlc_cnt == 4 and sp_o is not None and sp_h is not None and sp_l is not None and sp_c is not None and sp_h > sp_l:
        spot_red_1h = (sp_c < sp_o)
        spot_green_1h = (sp_c > sp_o)
        spot_uwick_1h = (sp_h - max(sp_o, sp_c)) / (sp_h - sp_l)
        spot_lwick_1h = (min(sp_o, sp_c) - sp_l) / (sp_h - sp_l)
    contig_21 = False
    if len(complete_hrs) >= 21:
        try:
            t_first = dt.datetime.strptime(complete_hrs[-21][0], "%Y-%m-%d %H:%M:%S")
            t_last = dt.datetime.strptime(complete_hrs[-1][0], "%Y-%m-%d %H:%M:%S")
            contig_21 = int((t_last - t_first).total_seconds()) == 20 * 3600
        except ValueError:
            contig_21 = False
    qv_avg20 = (sum(r[1] for r in complete_hrs[-21:-1]) / 20.0) if contig_21 else None
    spot_vrat_1h = (qv_now / qv_avg20) if (qv_avg20 is not None and qv_avg20 > 0.0) else None
    spot_tsize_r_1h = None
    if tc_now > 0.0 and qv_now > 0.0 and contig_21:
        ts_now = qv_now / tc_now
        prior_ts = [(r[1] / r[4]) if (r[4] > 0.0 and r[1] > 0.0) else 0.0 for r in complete_hrs[-21:-1]]
        ts_avg20 = sum(prior_ts) / 20.0
        if ts_avg20 > 0.0:
            spot_tsize_r_1h = ts_now / ts_avg20
    return {
        "spot_1h_complete": True,
        "spot_tb_1h": spot_tb_1h,
        "spot_qv_1h": qv_now,
        "spot_qv_avg20_1h": qv_avg20,
        "spot_vrat_1h": spot_vrat_1h,
        "spot_tc_1h": tc_now if tc_now > 0.0 else None,
        "spot_tsize_r_1h": spot_tsize_r_1h,
        "spot_red_1h": spot_red_1h,
        "spot_green_1h": spot_green_1h,
        "spot_uwick_1h": spot_uwick_1h,
        "spot_lwick_1h": spot_lwick_1h,
    }


def _spot_15m_to_2h_flow(spot_map, target_2h_str, end_ts_str=None):
    """Aggregate Spot 15m map `{t_str: (close, qv, tb_qv[, tc[, open, high, low]])}` into completed 2H UTC bars.

    Exact parity with backtest `_agg_spot_v3(..., 7200)` + `_agg_extra_phase(..., 7200)`:
    requires 8/8 completed 15m bars in `target_2h_str` and 20 prior completed 2H bars (40h+) for `spot_vrat_2h` & `spot_tsize_r_2h`,
    plus Spot OHLC upper-wick (`spot_uwick_2h`) and red-bar (`spot_red_2h`) for V21 `SP_UWICK_2H`.
    """
    empty = {
        "spot_2h_complete": False,
        "spot_tb_2h": None,
        "spot_qv_2h": None,
        "spot_qv_avg20_2h": None,
        "spot_vrat_2h": None,
        "spot_tc_2h": None,
        "spot_tsize_r_2h": None,
        "spot_red_2h": None,
        "spot_green_2h": None,
        "spot_uwick_2h": None,
        "spot_lwick_2h": None,
    }
    if not spot_map or not target_2h_str:
        return empty
    buckets = {}
    for t_str in sorted(spot_map.keys()):
        if end_ts_str and t_str > end_ts_str:
            continue
        sp = spot_map[t_str]
        if not sp or len(sp) < 2:
            continue
        sc, sqv = sp[0], sp[1]
        has_tb_col = len(sp) >= 3 and sp[2] is not None
        stb_qv = sp[2] if has_tb_col else 0.0
        stc = sp[3] if (len(sp) >= 4 and sp[3] is not None) else 0.0
        has_ohlc = len(sp) >= 7 and sp[4] is not None and sp[5] is not None and sp[6] is not None and sp[4] > 0
        if sc <= 0:
            continue
        try:
            hr = int(t_str[11:13])
        except (IndexError, ValueError):
            continue
        h2_key = f"{t_str[:10]} {(hr // 2) * 2:02d}:00:00"
        rec = buckets.setdefault(h2_key, [0, 0.0, 0.0, False, 0.0, 0, None, None, None, None])
        rec[0] += 1
        rec[1] += sqv
        rec[2] += stb_qv
        if has_tb_col and sqv > 0.0:
            rec[3] = True
        rec[4] += stc
        if has_ohlc:
            so, sh, sl = float(sp[4]), float(sp[5]), float(sp[6])
            rec[5] += 1
            if rec[6] is None:
                rec[6] = so
            rec[7] = sh if rec[7] is None else max(rec[7], sh)
            rec[8] = sl if rec[8] is None else min(rec[8], sl)
            rec[9] = float(sc)
    complete_2h = [(h, v[1], v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9]) for h, v in sorted(buckets.items()) if v[0] == 8]
    if not complete_2h or complete_2h[-1][0] != target_2h_str:
        return empty
    _, qv_now, tb_qv_now, has_tb, tc_now, ohlc_cnt, sp_o, sp_h, sp_l, sp_c = complete_2h[-1]
    spot_tb_2h = (tb_qv_now / qv_now) if (qv_now > 0.0 and has_tb) else None
    spot_red_2h = None
    spot_green_2h = None
    spot_uwick_2h = None
    spot_lwick_2h = None
    if ohlc_cnt == 8 and sp_o is not None and sp_h is not None and sp_l is not None and sp_c is not None and sp_h > sp_l:
        spot_red_2h = (sp_c < sp_o)
        spot_green_2h = (sp_c > sp_o)
        spot_uwick_2h = (sp_h - max(sp_o, sp_c)) / (sp_h - sp_l)
        spot_lwick_2h = (min(sp_o, sp_c) - sp_l) / (sp_h - sp_l)
    contig_21 = False
    if len(complete_2h) >= 21:
        try:
            t_first = dt.datetime.strptime(complete_2h[-21][0], "%Y-%m-%d %H:%M:%S")
            t_last = dt.datetime.strptime(complete_2h[-1][0], "%Y-%m-%d %H:%M:%S")
            contig_21 = int((t_last - t_first).total_seconds()) == 20 * 7200
        except ValueError:
            contig_21 = False
    qv_avg20 = (sum(r[1] for r in complete_2h[-21:-1]) / 20.0) if contig_21 else None
    spot_vrat_2h = (qv_now / qv_avg20) if (qv_avg20 is not None and qv_avg20 > 0.0) else None
    spot_tsize_r_2h = None
    if tc_now > 0.0 and qv_now > 0.0 and contig_21:
        ts_now = qv_now / tc_now
        prior_ts = [(r[1] / r[4]) if (r[4] > 0.0 and r[1] > 0.0) else 0.0 for r in complete_2h[-21:-1]]
        ts_avg20 = sum(prior_ts) / 20.0
        if ts_avg20 > 0.0:
            spot_tsize_r_2h = ts_now / ts_avg20
    return {
        "spot_2h_complete": True,
        "spot_tb_2h": spot_tb_2h,
        "spot_qv_2h": qv_now,
        "spot_qv_avg20_2h": qv_avg20,
        "spot_vrat_2h": spot_vrat_2h,
        "spot_tc_2h": tc_now if tc_now > 0.0 else None,
        "spot_tsize_r_2h": spot_tsize_r_2h,
        "spot_red_2h": spot_red_2h,
        "spot_green_2h": spot_green_2h,
        "spot_uwick_2h": spot_uwick_2h,
        "spot_lwick_2h": spot_lwick_2h,
    }


def _spot_15m_to_4h_flow(spot_map, target_4h_str, end_ts_str=None):
    """Aggregate Spot 15m map `{t_str: (close, qv, tb_qv[, tc])}` into completed 4H UTC bars for `SP_LEAD_4H`/`WHALESP_4H`/`SPOTFLOW_POS_4H`.

    Exact parity with backtest `_agg_spot_v3(..., 14400)` + `sma_np(b_sqv, 20)` + `sma_np(stsz, 20)`: requires 16/16 completed 15m bars
    in `target_4h_str` and 20 prior completed 4H bars (80h+) for `spot_vrat_4h` & `spot_tsize_r_4h`.
    """
    empty = {
        "spot_4h_complete": False,
        "spot_tb_4h_flow": None,
        "spot_qv_4h": None,
        "spot_qv_avg20_4h": None,
        "spot_vrat_4h": None,
        "spot_tc_4h": None,
        "spot_tsize_r_4h": None,
    }
    if not spot_map or not target_4h_str:
        return empty
    buckets = {}
    for t_str, sp in spot_map.items():
        if end_ts_str and t_str > end_ts_str:
            continue
        if not sp or len(sp) < 2:
            continue
        sc, sqv = sp[0], sp[1]
        has_tb_col = len(sp) >= 3 and sp[2] is not None
        stb_qv = sp[2] if has_tb_col else 0.0
        stc = sp[3] if len(sp) >= 4 else 0.0
        if sc <= 0:
            continue
        try:
            hr = int(t_str[11:13])
        except (IndexError, ValueError):
            continue
        h4_key = f"{t_str[:10]} {(hr // 4) * 4:02d}:00:00"
        rec = buckets.setdefault(h4_key, [0, 0.0, 0.0, False, 0.0])
        rec[0] += 1
        rec[1] += sqv
        rec[2] += stb_qv
        if has_tb_col and sqv > 0.0:
            rec[3] = True
        rec[4] += stc
    complete_4h = [(h, v[1], v[2], v[3], v[4]) for h, v in sorted(buckets.items()) if v[0] == 16]
    if not complete_4h or complete_4h[-1][0] != target_4h_str:
        return empty
    _, qv_now, tb_qv_now, has_tb, tc_now = complete_4h[-1]
    spot_tb_4h = (tb_qv_now / qv_now) if (qv_now > 0.0 and has_tb) else None
    contig_21 = False
    if len(complete_4h) >= 21:
        try:
            t_first = dt.datetime.strptime(complete_4h[-21][0], "%Y-%m-%d %H:%M:%S")
            t_last = dt.datetime.strptime(complete_4h[-1][0], "%Y-%m-%d %H:%M:%S")
            contig_21 = int((t_last - t_first).total_seconds()) == 20 * 14400
        except ValueError:
            contig_21 = False
    qv_avg20 = (sum(r[1] for r in complete_4h[-21:-1]) / 20.0) if contig_21 else None
    spot_vrat_4h = (qv_now / qv_avg20) if (qv_avg20 is not None and qv_avg20 > 0.0) else None
    spot_tsize_r_4h = None
    if tc_now > 0.0 and qv_now > 0.0 and contig_21:
        ts_now = qv_now / tc_now
        prior_ts = [(r[1] / r[4]) if (r[4] > 0.0 and r[1] > 0.0) else 0.0 for r in complete_4h[-21:-1]]
        ts_avg20 = sum(prior_ts) / 20.0
        if ts_avg20 > 0.0:
            spot_tsize_r_4h = ts_now / ts_avg20
    return {
        "spot_4h_complete": True,
        "spot_tb_4h_flow": spot_tb_4h,
        "spot_qv_4h": qv_now,
        "spot_qv_avg20_4h": qv_avg20,
        "spot_vrat_4h": spot_vrat_4h,
        "spot_tc_4h": tc_now if tc_now > 0.0 else None,
        "spot_tsize_r_4h": spot_tsize_r_4h,
    }


def load_spot_15m(symbols, hours=108):
    """symbol -> dict[time_str] = (close, qv, tb_qv_or_None, tc, open, high, low) for Spot 15m.

    V21 Fix: preserves NULL `taker_buy_quote_volume` as `None` (never spoofs `spot_tb=0.0` on ClickHouse nulls)
    and includes Spot OHLC (`open, high, low`) with zero extra ClickHouse queries for `SP_UWICK_1H/2H`.
    """
    if not symbols:
        return {}
    out = {}
    for sym, t_str, c, qv, tb_qv_s, tc, op, hi, lo in market.ch(
        """SELECT symbol, toString(open_time) t, toString(any(close)),
                  toString(coalesce(any(quote_volume), 0)),
                  if(isNull(any(taker_buy_quote_volume)), '', toString(any(taker_buy_quote_volume))),
                  toString(coalesce(any(trade_count), 0)),
                  toString(any(open)), toString(any(high)), toString(any(low))
           FROM altcoin.ohlcv
           WHERE market='spot' AND interval='15m' AND symbol IN {syms:Array(String)}
             AND open_time >= toStartOfFifteenMinutes(now()) - INTERVAL {hours:UInt32} HOUR
             AND open_time < toStartOfFifteenMinutes(now())
           GROUP BY symbol, open_time
           ORDER BY symbol, open_time""",
        syms=str(sorted(symbols)),
        hours=hours,
    ):
        tb_val = float(tb_qv_s) if tb_qv_s != "" else None
        out.setdefault(sym, {})[t_str[:19]] = (
            float(c), float(qv), tb_val, float(tc), float(op), float(hi), float(lo)
        )
    return out


def compute_spot_basis_stats(
    m15_t,
    m15_cl,
    spot_map,
    target_4h_last_15m_str=None,
    end_ts_str=None,
    target_4h_start_str=None,
    spot_qv_24h_override=None,
):
    """Compute causal Perp-Spot basis & 4H Spot Taker-Buy metrics matching V7/V8/V9 backtest exactly.

    Supports both 2-tuple `(close, qv)` and 3-tuple `(close, qv, tb_qv)` values in `spot_map`.
    If `end_ts_str` is provided, restricts 8h/12h rolling windows to bars `<= end_ts_str` (prevents lookahead).
    If `spot_qv_24h_override` (live Binance Spot 24h quoteVolume) is provided, takes max(CH 24h sum, live 24h)
    so ClickHouse ingestion gaps cannot falsely deflate `spot_qv_24h` and inflate `ps_vrat`.
    Returns dict `{has_spot_24h, spot_qv_24h, bas8h, bas12h_max, bas_4h_close, spot_tb_4h}`.
    """
    empty = {
        "has_spot_24h": False,
        "spot_qv_24h": 0.0,
        "bas15": None,
        "bas1h": None,
        "bas8h": None,
        "bas12h_max": None,
        "bas_4h_close": None,
        "spot_tb_4h": None,
    }
    if not m15_t or not spot_map:
        return empty

    aligned = []
    for t_str, pc in zip(m15_t, m15_cl):
        if end_ts_str and t_str > end_ts_str:
            continue
        sp = spot_map.get(t_str)
        if not sp:
            aligned.append((t_str, None, 0.0, 0.0))
            continue
        sc = sp[0]
        sqv = sp[1]
        has_tb_col = len(sp) >= 3 and sp[2] is not None
        stb_qv = sp[2] if has_tb_col else None
        if sc <= 0 or pc <= 0:
            aligned.append((t_str, None, 0.0, None))
            continue
        ratio = pc / sc
        if ratio > 1.5 or ratio < 0.67:
            aligned.append((t_str, None, 0.0, None))
            continue
        bas = max(-0.08, min(0.08, ratio - 1.0))
        aligned.append((t_str, bas, sqv, stb_qv))

    if not aligned:
        return empty

    tail_24h = aligned[-96:]
    spot_24h_bars = sum(1 for x in tail_24h if x[1] is not None)
    spot_qv_24h_ch = sum(x[2] for x in tail_24h)
    if spot_qv_24h_override is not None and spot_qv_24h_override > 0.0:
        spot_qv_24h = max(spot_qv_24h_ch, float(spot_qv_24h_override))
    else:
        spot_qv_24h = spot_qv_24h_ch
    has_spot_24h = spot_qv_24h >= MIN_SPOT_VOL_24H_USD

    bas15 = None
    if end_ts_str:
        for t_str, bas, _, _ in reversed(aligned):
            if t_str == end_ts_str:
                bas15 = bas
                break
    elif aligned:
        bas15 = aligned[-1][1]

    tail_1h = [x[1] for x in aligned[-4:] if x[1] is not None]
    bas1h = (sum(tail_1h) / len(tail_1h)) if len(tail_1h) >= 2 else None

    tail_8h = [x[1] for x in aligned[-32:] if x[1] is not None]
    bas8h = (sum(tail_8h) / len(tail_8h)) if len(tail_8h) >= 8 else None

    tail_12h = [x[1] for x in aligned[-48:] if x[1] is not None]
    bas12h_max = max(tail_12h) if tail_12h else None

    bas_4h_close = None
    if target_4h_last_15m_str:
        for t_str, bas, _, _ in reversed(aligned):
            if t_str == target_4h_last_15m_str:
                bas_4h_close = bas
                break

    spot_tb_4h = None
    if target_4h_start_str and target_4h_last_15m_str:
        tot_sqv_4h = 0.0
        tot_stb_4h = 0.0
        has_tb_data = False
        for t_str, bas, sqv, stb_qv in aligned:
            if target_4h_start_str <= t_str <= target_4h_last_15m_str and bas is not None and sqv > 0:
                tot_sqv_4h += sqv
                if stb_qv is not None:
                    tot_stb_4h += stb_qv
                    has_tb_data = True
        if tot_sqv_4h > 0 and has_tb_data:
            spot_tb_4h = tot_stb_4h / tot_sqv_4h

    return {
        "has_spot_24h": has_spot_24h,
        "spot_qv_24h": spot_qv_24h,
        "spot_24h_bars": spot_24h_bars,
        "bas15": bas15,
        "bas1h": bas1h,
        "bas8h": bas8h,
        "bas12h_max": bas12h_max,
        "bas_4h_close": bas_4h_close,
        "spot_tb_4h": spot_tb_4h,
    }


def load_perp_hourly_flow(symbols, days=10):
    """symbol -> ([hr], [open], [high], [low], [close], [qv], [tb_ratio], [cnt]) of completed 1h UTC bars."""
    out, newest = {}, {}
    for sym, h_str, op, hi, lo, cl, qv, tb_qv, cnt in market.ch(
        """SELECT symbol, toString(h),
                  toString(argMin(o, t)), toString(max(hi)), toString(min(lo)), toString(argMax(c, t)),
                  toString(sum(qv)), toString(sum(tb_qv)), toString(count())
           FROM (
               SELECT symbol, open_time t, toStartOfHour(open_time) h,
                      any(open) o, any(high) hi, any(low) lo, any(close) c,
                      coalesce(any(quote_volume), 0) qv,
                      coalesce(any(taker_buy_quote_volume), 0) tb_qv
               FROM altcoin.ohlcv
               WHERE market='um_futures' AND interval='15m' AND symbol IN {syms:Array(String)}
                 AND open_time >= toStartOfHour(now()) - INTERVAL {days:UInt32} DAY
                 AND open_time < toStartOfHour(now())
               GROUP BY symbol, open_time
           )
           GROUP BY symbol, h
           ORDER BY symbol, h""",
        syms=str(sorted(symbols)),
        days=days,
    ):
        rec = out.setdefault(sym, ([], [], [], [], [], [], [], []))
        qvf = float(qv)
        tb = (float(tb_qv) / qvf) if qvf > 0 else 0.5
        rec[0].append(h_str[:19])
        rec[1].append(float(op))
        rec[2].append(float(hi))
        rec[3].append(float(lo))
        rec[4].append(float(cl))
        rec[5].append(qvf)
        rec[6].append(tb)
        rec[7].append(int(cnt))
        newest[sym] = h_str[:19]
    return out, newest


def load_perp_15m(symbols, hours=28):
    """symbol -> ([time 'YYYY-MM-DD HH:MM:SS'], [closes], [quote_volumes], [highs], [lows]) of completed 15m UTC bars."""
    out, newest = {}, {}
    for sym, t_str, c, qv, hi, lo in market.ch(
        """SELECT symbol, toString(open_time) t, toString(any(close)), toString(any(quote_volume)),
               toString(any(high)), toString(any(low))
           FROM altcoin.ohlcv
           WHERE market='um_futures' AND interval='15m' AND symbol IN {syms:Array(String)}
             AND open_time >= toStartOfFifteenMinutes(now()) - INTERVAL {hours:UInt32} HOUR
             AND open_time < toStartOfFifteenMinutes(now())
           GROUP BY symbol, open_time
           ORDER BY symbol, open_time""",
        syms=str(sorted(symbols)),
        hours=hours,
    ):
        rec = out.setdefault(sym, ([], [], [], [], []))
        ts = t_str[:19]
        rec[0].append(ts)
        rec[1].append(float(c))
        rec[2].append(float(qv))
        rec[3].append(float(hi))
        rec[4].append(float(lo))
        newest[sym] = ts
    return out, newest


def _fetch_rest_klines(url, sym, interval, limit=3, timeout=10):
    q = urllib.parse.urlencode({"symbol": sym, "interval": interval, "limit": limit})
    return sym, _http_json(f"{url}?{q}", timeout=timeout)


def _patch_live_1h_from_binance(hourly_flow, symbols, target_hour_str, now_utc, max_workers=24, check_hours=8):
    """Patch missing or incomplete 1H UTC bars (last `check_hours` window) directly from Binance Futures REST.

    V12.1: inspects the last 8 hours (2 full 4H buckets) with `limit=max(10, check_hours + 2)` (same weight=1),
    so any earlier incomplete hour in ClickHouse never blinds 4H or 1H setups.
    """
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    tgt_dt = dt.datetime.strptime(target_hour_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    n_chk = max(1, int(check_hours))
    required_hrs = [
        (tgt_dt - dt.timedelta(hours=h)).strftime("%Y-%m-%d %H:00:00")
        for h in range(n_chk)
    ]
    missing = []
    tail_n = max(8, n_chk + 4)
    for sym in sorted(symbols):
        rec = hourly_flow.get(sym)
        if not rec or not rec[0]:
            missing.append(sym)
            continue
        cnt_by_hr = {h: cnt for h, cnt in zip(rec[0][-tail_n:], rec[7][-tail_n:])}
        if any(cnt_by_hr.get(hr, 0) < 4 for hr in required_hrs):
            missing.append(sym)
    if not missing:
        return 0

    req_limit = max(10, n_chk + 2)

    def _get(sym):
        try:
            return _fetch_rest_klines(FAPI_KLINES, sym, "1h", limit=req_limit, timeout=10)
        except Exception:  # noqa: BLE001
            return sym, None

    patched = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(missing)))) as ex:
        for sym, rows in ex.map(_get, missing):
            if not rows:
                continue
            rec = hourly_flow.setdefault(sym, ([], [], [], [], [], [], [], []))
            by_hr = {
                rec[0][i]: (rec[1][i], rec[2][i], rec[3][i], rec[4][i], rec[5][i], rec[6][i], rec[7][i])
                for i in range(len(rec[0]))
            }
            changed = False
            for r in rows:
                try:
                    t_open = int(r[0])
                    if t_open + 3_600_000 > now_ms:
                        continue  # skip still-open current hour candle
                    h_str = dt.datetime.fromtimestamp(t_open / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:00:00")
                    op, hi, lo, cl = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                    qv = float(r[7])
                    tb_qv = float(r[10])
                    tb = (tb_qv / qv) if qv > 0 else 0.5
                except (IndexError, TypeError, ValueError):
                    continue
                prev = by_hr.get(h_str)
                if prev is None or prev[6] < 4:
                    by_hr[h_str] = (op, hi, lo, cl, qv, tb, 4)
                    if h_str in required_hrs:
                        changed = True
            if changed:
                ordered = sorted(by_hr.items())
                rec[0][:] = [k for k, _ in ordered]
                rec[1][:] = [v[0] for _, v in ordered]
                rec[2][:] = [v[1] for _, v in ordered]
                rec[3][:] = [v[2] for _, v in ordered]
                rec[4][:] = [v[3] for _, v in ordered]
                rec[5][:] = [v[4] for _, v in ordered]
                rec[6][:] = [v[5] for _, v in ordered]
                rec[7][:] = [v[6] for _, v in ordered]
                patched += 1
    return patched


def _patch_shortlist_15m_from_binance(
    m15_ohlcv,
    spot_15m,
    symbols,
    target_last_15m_str,
    now_utc,
    max_workers=12,
    window_start_str=None,
    spot_live_syms=None,
    check_12h_gap=False,
):
    """Patch recent completed 15m Perp & Spot bars from Binance REST for 4H/2H/1H basis & spot-flow candidates.

    V16.1/V16.2 upgrades:
      - Repairs 8h basis gaps (< 24/32 bars) and 12h basis gaps (< 36/48 bars on 4H windows for `BASCRUSH_4H` `bas12h_max`).
      - Runs `fapi.binance.com` (Perp) and `api.binance.com` (Spot) batches IN PARALLEL across separate hosts.
    """
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    req_ts = [target_last_15m_str]
    e_dt = None
    with contextlib.suppress(ValueError):
        e_dt = dt.datetime.strptime(target_last_15m_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    if window_start_str and window_start_str <= target_last_15m_str and e_dt is not None:
        w_dt = dt.datetime.strptime(window_start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        req_ts = []
        cur_dt = w_dt
        while cur_dt <= e_dt:
            req_ts.append(cur_dt.strftime("%Y-%m-%d %H:%M:%S"))
            cur_dt += dt.timedelta(minutes=15)
    bas8h_start_str = (e_dt - dt.timedelta(minutes=15 * 31)).strftime("%Y-%m-%d %H:%M:%S") if e_dt is not None else None
    bas12h_start_str = (e_dt - dt.timedelta(minutes=15 * 47)).strftime("%Y-%m-%d %H:%M:%S") if e_dt is not None else None

    check_8h_gap = bool(window_start_str and bas8h_start_str)
    check_12h = bool(check_12h_gap and bas12h_start_str)
    live_sp = set(spot_live_syms or ())
    need_perp = []
    need_spot = []
    for sym in sorted(set(symbols)):
        mrec = m15_ohlcv.get(sym)
        m_ts = mrec[0] if (mrec and mrec[0]) else []
        m_seen = set(m_ts[-24:])
        perp_8h_ok = (not check_8h_gap) or (sum(1 for ts in m_ts[-48:] if bas8h_start_str <= ts <= target_last_15m_str) >= 24)
        perp_12h_ok = (not check_12h) or (sum(1 for ts in m_ts[-64:] if bas12h_start_str <= ts <= target_last_15m_str) >= 36)
        if any(ts not in m_seen for ts in req_ts) or not perp_8h_ok or not perp_12h_ok:
            need_perp.append(sym)
        smap = spot_15m.get(sym) or {}
        if smap or sym in live_sp:
            spot_8h_ok = (not check_8h_gap) or (sum(1 for ts in smap if bas8h_start_str <= ts <= target_last_15m_str) >= 24)
            spot_12h_ok = (not check_12h) or (sum(1 for ts in smap if bas12h_start_str <= ts <= target_last_15m_str) >= 36)
            if any(ts not in smap for ts in req_ts) or not spot_8h_ok or not spot_12h_ok:
                need_spot.append(sym)
    if not need_perp and not need_spot:
        return 0

    def _get_p(sym):
        try:
            # V12 Fix #1: limit=50 (12.5h, same 1 weight as limit=20) guarantees full bas8h & bas12h_max coverage
            return _fetch_rest_klines(FAPI_KLINES, sym, "15m", limit=50, timeout=10)
        except Exception:  # noqa: BLE001
            return sym, None

    def _get_s(sym):
        try:
            return _fetch_rest_klines(SPOT_KLINES, sym, "15m", limit=50, timeout=10)
        except Exception:  # noqa: BLE001
            return sym, None

    def _run_perp():
        if not need_perp:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(need_perp))) as ex:
            for sym, rows in ex.map(_get_p, need_perp):
                if not rows:
                    continue
                rec = m15_ohlcv.setdefault(sym, ([], [], [], [], []))
                by_t = {
                    rec[0][i]: (rec[1][i], rec[2][i], rec[3][i], rec[4][i])
                    for i in range(len(rec[0]))
                }
                changed = False
                for r in rows:
                    try:
                        t_open = int(r[0])
                        if t_open + 900_000 > now_ms:
                            continue
                        ts = dt.datetime.fromtimestamp(t_open / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        if ts not in by_t:
                            by_t[ts] = (float(r[4]), float(r[7]), float(r[2]), float(r[3]))
                            changed = True
                    except (IndexError, TypeError, ValueError):
                        continue
                if changed:
                    ordered = sorted(by_t.items())
                    rec[0][:] = [k for k, _ in ordered]
                    rec[1][:] = [v[0] for _, v in ordered]
                    rec[2][:] = [v[1] for _, v in ordered]
                    rec[3][:] = [v[2] for _, v in ordered]
                    rec[4][:] = [v[3] for _, v in ordered]

    def _run_spot():
        if not need_spot:
            return 0
        patched_s = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(need_spot))) as ex:
            for sym, rows in ex.map(_get_s, need_spot):
                if not rows:
                    continue
                smap = spot_15m.setdefault(sym, {})
                got = False
                for r in rows:
                    try:
                        t_open = int(r[0])
                        if t_open + 900_000 > now_ms:
                            continue
                        ts = dt.datetime.fromtimestamp(t_open / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        tc_val = float(r[8]) if len(r) > 8 and r[8] is not None else 0.0
                        smap[ts] = (float(r[4]), float(r[7]), float(r[10]), tc_val, float(r[1]), float(r[2]), float(r[3]))
                        if ts == target_last_15m_str:
                            got = True
                    except (IndexError, TypeError, ValueError):
                        continue
                if got:
                    patched_s += 1
        return patched_s

    if need_perp and need_spot:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pair_ex:
            f_p = pair_ex.submit(_run_perp)
            f_s = pair_ex.submit(_run_spot)
            f_p.result()
            return f_s.result()
    _run_perp()
    return _run_spot()


def _fetch_spot_flow_rest_fallback(symbols, interval, target_bar_str, now_utc, max_workers=12):
    """V11/V16 Fix: direct Binance Spot 4H/2H/1H REST fallback (`limit=25`) when ClickHouse spot_15m lacks 21-bar history.

    Guarantees exact 20-bar prior SMA `spot_vrat` & `spot_tsize_r` & `spot_tb` parity for 1H/2H/4H spot-flow engines
    even if ClickHouse has zero rows (new Spot pairs) or a single dropped 15m candle in the prior history window.
    """
    if not symbols or interval not in ("1h", "2h", "4h") or not target_bar_str:
        return {}
    bar_ms = {"1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000}[interval]
    now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
    uniq_syms = sorted(set(symbols))

    def _get(sym):
        try:
            return _fetch_rest_klines(SPOT_KLINES, sym, interval, limit=25, timeout=10)
        except Exception:  # noqa: BLE001
            return sym, None

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(uniq_syms))) as ex:
        for sym, rows in ex.map(_get, uniq_syms):
            if not rows:
                continue
            done = []
            for r in rows:
                try:
                    t_open = int(r[0])
                    if t_open + bar_ms > now_ms:
                        continue
                    k_str = dt.datetime.fromtimestamp(t_open / 1000.0, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:00:00")
                    qv = float(r[7])
                    tb_qv = float(r[10])
                    tc = float(r[8]) if len(r) > 8 and r[8] is not None else 0.0
                    so, sh, sl, sc = float(r[1]), float(r[2]), float(r[3]), float(r[4])
                    done.append((k_str, qv, tb_qv, tc, so, sh, sl, sc))
                except (IndexError, TypeError, ValueError):
                    continue
            done.sort(key=lambda x: x[0])
            if not done or done[-1][0] != target_bar_str:
                continue
            _, qv_now, tb_qv_now, tc_now, so_now, sh_now, sl_now, sc_now = done[-1]
            spot_tb = (tb_qv_now / qv_now) if (qv_now > 0.0 and tb_qv_now >= 0.0) else None
            spot_red = (sc_now < so_now) if (so_now > 0.0 and sc_now > 0.0) else None
            spot_green = (sc_now > so_now) if (so_now > 0.0 and sc_now > 0.0) else None
            spot_uwick = ((sh_now - max(so_now, sc_now)) / (sh_now - sl_now)) if (sh_now > sl_now and so_now > 0.0) else None
            spot_lwick = ((min(so_now, sc_now) - sl_now) / (sh_now - sl_now)) if (sh_now > sl_now and so_now > 0.0) else None
            qv_avg20 = (sum(x[1] for x in done[-21:-1]) / 20.0) if len(done) >= 21 else None
            spot_vrat = (qv_now / qv_avg20) if (qv_avg20 is not None and qv_avg20 > 0.0) else None
            spot_tsize_r = None
            if tc_now > 0.0 and qv_now > 0.0 and len(done) >= 21:
                ts_now = qv_now / tc_now
                prior_ts = [(x[1] / x[3]) if (x[3] > 0.0 and x[1] > 0.0) else 0.0 for x in done[-21:-1]]
                ts_avg20 = sum(prior_ts) / 20.0
                if ts_avg20 > 0.0:
                    spot_tsize_r = ts_now / ts_avg20
            if interval == "4h":
                out[sym] = {
                    "spot_4h_complete": True,
                    "spot_tb_4h_flow": spot_tb,
                    "spot_qv_4h": qv_now,
                    "spot_qv_avg20_4h": qv_avg20,
                    "spot_vrat_4h": spot_vrat,
                    "spot_tc_4h": tc_now if tc_now > 0.0 else None,
                    "spot_tsize_r_4h": spot_tsize_r,
                }
            elif interval == "2h":
                out[sym] = {
                    "spot_2h_complete": True,
                    "spot_tb_2h": spot_tb,
                    "spot_qv_2h": qv_now,
                    "spot_qv_avg20_2h": qv_avg20,
                    "spot_vrat_2h": spot_vrat,
                    "spot_tc_2h": tc_now if tc_now > 0.0 else None,
                    "spot_tsize_r_2h": spot_tsize_r,
                    "spot_red_2h": spot_red,
                    "spot_green_2h": spot_green,
                    "spot_uwick_2h": spot_uwick,
                    "spot_lwick_2h": spot_lwick,
                }
            else:
                out[sym] = {
                    "spot_1h_complete": True,
                    "spot_tb_1h": spot_tb,
                    "spot_qv_1h": qv_now,
                    "spot_qv_avg20_1h": qv_avg20,
                    "spot_vrat_1h": spot_vrat,
                    "spot_tc_1h": tc_now if tc_now > 0.0 else None,
                    "spot_tsize_r_1h": spot_tsize_r,
                    "spot_red_1h": spot_red,
                    "spot_green_1h": spot_green,
                    "spot_uwick_1h": spot_uwick,
                    "spot_lwick_1h": spot_lwick,
                }
    return out


def _append_chat_line(who, text, now_utc):
    th_now = now_utc.astimezone(TH)
    chat_path = os.path.join(REPORTS, "team-chat", f"{th_now:%Y-%m-%d}.md")
    if not os.path.exists(chat_path):
        append_line(chat_path, f"# แชตทีม BotTrade {th_now:%Y-%m-%d}\n")
    append_line(chat_path, f"- {th_now:%H:%M} **{who}**: {text}")


def _append_journal(event_type, payload, now_utc):
    row = {
        "at": now_utc.isoformat(timespec="seconds"),
        "at_th": f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M:%S}",
        "event": event_type,
        **payload,
    }
    append_line(JOURNAL_PATH, json.dumps(row, ensure_ascii=False))


def _sync_and_portfolio(now_utc, do_sync=True):
    today_iso = now_utc.astimezone(dt.timezone.utc).date().isoformat()
    sync_out = None
    if do_sync:
        try:
            sync_out = binance.post("/sync", {"run_id": today_iso})
        except Exception as e:  # noqa: BLE001
            sync_out = {"status": "error", "lines": [safe_err(e)]}
    pf = binance.save_portfolio(binance.fetch_portfolio())
    state = read_json(STATE_PATH, default={})
    equity = pf.get("equity_usdt") if not pf.get("error") else None
    new_state, cb_summary = evaluate_circuit_breaker(state, equity, now_utc.isoformat())
    write_json_atomic(STATE_PATH, new_state)
    return pf, sync_out, new_state, cb_summary


def _evaluate_open_positions(pf, now_utc, bars_out=None, flow_out=None):
    pos_meta_all = read_json(POS_META_PATH, default={})
    active_shorts = [p for p in (pf.get("shorts") or []) if p.get("on_binance")]
    btc_bars_15m = None
    btc_mark_px = None
    eth_bars_15m = None
    eth_mark_px = None
    live_b1h = None
    if active_shorts:
        btc_ref_merged = read_json(BTC_REF_PATH, default={})
        has_sqz = any(_is_sqz_mode((pos_meta_all.get(p.get("id")) or {}).get("exit_mode")) for p in active_shorts)
        has_v12_aa = any(_sqz_b1h_threshold((pos_meta_all.get(p.get("id")) or {}).get("exit_mode")) is not None for p in active_shorts)
        has_v18_aa = any(_sqz_eth_15m_threshold((pos_meta_all.get(p.get("id")) or {}).get("exit_mode")) is not None for p in active_shorts)
        # V18: fetch BTC 15m, ETH 15m (when V18 positions active), and live marks IN PARALLEL (~0s extra latency)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ref_ex:
            f_btc = ref_ex.submit(fetch_klines_15m, "BTCUSDT", 400)
            f_eth = ref_ex.submit(fetch_klines_15m, "ETHUSDT", 400) if has_v18_aa else None
            f_mks = ref_ex.submit(fetch_mark_prices) if has_sqz else None
            ref_updates = {}
            with contextlib.suppress(Exception):
                btc_bars_15m = f_btc.result()
                _, btc_ref = _btc_squeeze_returns_from_bars(btc_bars_15m, now_utc)
                if btc_ref:
                    ref_updates.update(btc_ref)
            if f_eth is not None:
                with contextlib.suppress(Exception):
                    eth_bars_15m = f_eth.result()
                    _, eth_ref = _eth_15m_ref_from_bars(eth_bars_15m, now_utc)
                    if eth_ref:
                        ref_updates.update(eth_ref)
            if ref_updates:
                with contextlib.suppress(Exception):
                    btc_ref_merged = _save_btc_ref_merged(ref_updates) or {**btc_ref_merged, **ref_updates}
            if has_sqz:
                if has_v12_aa and not _b1h_ref_is_fresh(btc_ref_merged, now_utc):
                    with contextlib.suppress(Exception):
                        refreshed_b1h = _refresh_b1h_ema_ref_from_ch(now_utc)
                        if refreshed_b1h:
                            btc_ref_merged = refreshed_b1h
                if f_mks is not None:
                    with contextlib.suppress(Exception):
                        marks_all = f_mks.result()
                        btc_mark_px = marks_all.get("BTCUSDT")
                        eth_mark_px = marks_all.get("ETHUSDT")
                        if has_v12_aa and btc_ref_merged:
                            _, b1h_d, _ = _eval_live_b1h_squeeze(marks_all, btc_ref_merged, now_utc)
                            live_b1h = b1h_d

    def _fetch_pos_klines(p_row):
        pid_i = p_row.get("id")
        sym_i = p_row.get("symbol")
        meta_i = pos_meta_all.get(pid_i) or {"symbol": sym_i, "side": p_row.get("side") or "SHORT"}
        bars_i, last3_i, kerr_i = [], [], None
        san_ok = False
        san_fail = False
        try:
            bars_i, last3_i = fetch_klines_15m_raw(sym_i, limit=400)
            # V14 Fix: fetch 1m klines ONLY once the opening 15m bar has actually closed (eliminates wasted 1m REST calls
            # during the first 15m of a trade), while still tolerating <=120s Proxy clock skew and guarding empty 1m replies.
            if bars_i and int(meta_i.get("last_bar_ms") or 0) == 0 and p_row.get("opened_at"):
                try:
                    op_dt = parse_iso_utc(p_row["opened_at"])
                    now_u_k = now_utc.astimezone(dt.timezone.utc)
                    age_sec = (now_u_k - op_dt).total_seconds()
                    op_ms = int(op_dt.timestamp() * 1000)
                    now_ms_k = int(now_u_k.timestamp() * 1000)
                    open_bar_start = (op_ms // 900_000) * 900_000
                    open_bar_closed = (open_bar_start + 900_000) <= (now_ms_k + 120_000) and any(
                        int(t) == open_bar_start for t, *_ in bars_i
                    )
                    if age_sec >= -120 and (op_ms % 900_000) != 0 and open_bar_closed:
                        if age_sec <= 3600:
                            bars_1m = fetch_klines_1m(sym_i, limit=70)
                        else:
                            bars_1m = fetch_klines_1m(sym_i, limit=16, start_time_ms=open_bar_start)
                        if bars_1m:
                            bars_i = sanitize_first_bar_1m(bars_i, bars_1m, p_row.get("opened_at"), now_utc)
                            san_ok = True
                        else:
                            san_fail = True
                except Exception:  # noqa: BLE001
                    san_fail = True
        except Exception as e:  # noqa: BLE001
            kerr_i = safe_err(e)
        return pid_i, sym_i, bars_i, last3_i, kerr_i, san_ok, san_fail

    fetched_map = {}
    if len(active_shorts) <= 1:
        for p in active_shorts:
            res_f = _fetch_pos_klines(p)
            fetched_map[res_f[0]] = res_f
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(active_shorts), 8)) as ex:
            for res_f in ex.map(_fetch_pos_klines, active_shorts):
                fetched_map[res_f[0]] = res_f

    active_ids = set()
    evaluated = []
    for p in active_shorts:
        pid = p.get("id")
        sym = p.get("symbol")
        active_ids.add(pid)
        meta = dict(pos_meta_all.get(pid) or {"symbol": sym, "side": p.get("side") or "SHORT"})
        meta["side"] = str(p.get("side") or meta.get("side") or "SHORT").upper()
        meta.pop("pending_open", None)
        _, _, bars, last3, kline_err, first_bar_sanitized, first_bar_1m_failed = fetched_map[pid]
        if bars_out is not None and bars:
            bars_out[sym] = bars
        if flow_out is not None and last3:
            flow_out[sym] = last3
        ev = evaluate_position_exit(
            meta,
            p.get("entry"),
            p.get("mark"),
            bars,
            p.get("opened_at"),
            now_utc,
            btc_bars_15m=btc_bars_15m,
            btc_mark_px=btc_mark_px,
            first_bar_sanitized=first_bar_sanitized,
            first_bar_1m_failed=first_bar_1m_failed,
            live_b1h=live_b1h,
            eth_bars_15m=eth_bars_15m,
            eth_mark_px=eth_mark_px,
        )
        upd_meta = dict(ev["updated_meta"])
        upd_meta.pop("pending_open", None)
        pos_meta_all[pid] = upd_meta
        evaluated.append({
            "id": pid,
            "symbol": sym,
            "coin": p.get("coin"),
            "side": p.get("side") or "SHORT",
            "setup": meta.get("setup"),
            "exit_mode": ev["exit_mode"],
            "entry": p.get("entry"),
            "mark": p.get("mark"),
            "pnl_usdt": p.get("pnl_usdt"),
            "pnl_pct": p.get("pnl_pct"),
            "funding_usdt": p.get("funding_usdt"),
            "opened_at": p.get("opened_at"),
            "unclear": bool(p.get("unclear")),
            "action": ev["action"],
            "exit_reason": ev["exit_reason"],
            "mfe_pct": ev["mfe_pct"],
            "mae_pct": ev["mae_pct"],
            "stop_px": ev["stop_px"],
            "tp_px": ev["tp_px"],
            "ratchet_stage": ev["ratchet_stage"],
            "held_hours": ev["held_hours"],
            **({"kline_error": kline_err} if kline_err else {}),
        })
    # V12.1/V13 Fix: preserve pos_meta for active/unclear shorts AND recent pre-open pending_open entries (TTL 10m)
    keep_ids = active_ids | {p.get("id") for p in (pf.get("shorts") or []) if p.get("id")}
    now_u_eval = now_utc.astimezone(dt.timezone.utc)
    cleaned_meta = {}
    for k, v in pos_meta_all.items():
        if k in keep_ids:
            cleaned_meta[k] = v
        elif isinstance(v, dict) and v.get("pending_open"):
            try:
                p_age = (now_u_eval - parse_iso_utc(v.get("opened_at") or "")).total_seconds()
                if -120 <= p_age <= PENDING_OPEN_META_TTL_SEC:
                    cleaned_meta[k] = v
            except (TypeError, ValueError):
                pass
    write_json_atomic(POS_META_PATH, cleaned_meta)
    return evaluated


def evaluate_v12_short_setups(
    *,
    age_days,
    breadth_1d,
    breadth_1h=None,
    btc_ret7d=None,
    btc_ret4h=None,
    bar_complete_1h=False,
    bars_1h_count=None,
    cur_px=None,
    o_1h=None,
    hi_1h=None,
    lo_1h=None,
    c_1h=None,
    c_1h_prev=None,
    qv_1h=None,
    qv_avg20_1h=None,
    tb_1h=None,
    ema10_1h=None,
    ema10_1h_prev=None,
    ema20_1h=None,
    lo16_1h_prev=None,
    atr14_1h_pct=None,
    hi24=None,
    hi7d=None,
    ret_4h=None,
    ret_12h=None,
    ret_24h=None,
    ret_7d=None,
    d_sma20=None,
    f_day=0.0,
    # Perp-Spot Basis & 1H/2H/4H Spot/Perp flow fields (V12-V19 Institutional Microstructure)
    has_spot_24h=False,
    bas15=None,
    bas1h=None,
    bas8h=None,
    bas12h_max=None,
    bas_4h_close=None,
    spot_tb_4h=None,
    spot_4h_complete=False,
    spot_vrat_4h=None,
    spot_tsize_r_4h=None,
    spot_1h_complete=False,
    spot_tb_1h=None,
    spot_vrat_1h=None,
    spot_tsize_r_1h=None,
    spot_red_1h=None,
    spot_uwick_1h=None,
    spot_2h_complete=False,
    spot_tb_2h=None,
    spot_vrat_2h=None,
    spot_tsize_r_2h=None,
    spot_red_2h=None,
    spot_uwick_2h=None,
    ps_vrat=None,
    is_fresh_2h_window=False,
    bar_complete_2h=False,
    o_2h=None,
    hi_2h=None,
    lo_2h=None,
    c_2h=None,
    o_2h_prev=None,
    c_2h_prev=None,
    qv_2h=None,
    qv_avg20_2h=None,
    qv_2h_prev=None,
    qv_avg20_2h_prev=None,
    tb_2h=None,
    ema10_2h=None,
    ema10_2h_prev=None,
    ema20_2h=None,
    is_fresh_4h_window=False,
    bar_complete_4h=False,
    o_4h=None,
    hi_4h=None,
    lo_4h=None,
    c_4h=None,
    c_4h_prev=None,
    qv_4h=None,
    qv_avg20_4h=None,
    tb_4h=None,
    ema10_4h=None,
    ema10_4h_prev=None,
    **_unused_legacy_kwargs,
):
    """Evaluate the 23 backtest-validated V20 Intraday SHORT engines (`V20_23ENG_BULLSF2H`).

    Priority & Exit Architecture (TP 60%, 3-stage trail TRAIL30, AA HALF50 -1.0%, Quad-Channel Unprotected Squeeze Cut AA BTC 15m>=1.8% | ETH 15m>=2.0% | BTC 1h>=1.8% | 4h>=3.2% | B1H>=85%):
      1–12. Proven V15 Core (score 225–368+): DCAT_WICK_1H, BASCRUSH_4H, PARA75_G_1H, NEW7_BAS_1H,
            BASOVH_1H, WHALESP_1H, POST30D_4H, SP_LEAD_4H, BEAR_BRK_1H, SPOTDMP_4H, SPOTDMP_1H, SPOTFLOW_POS_1H.
      13–18. V16 Strict Positive-Basis 2H/4H Institutional Moat (subordinated score 221.5–223.2):
            SP_LEAD_2H, WHALESP_2H, WHALESP_4H, SPOTDMP_POS_2H, SPOTFLOW_POS_2H, SPOTFLOW_POS_4H (`bas8h >= 0%`).
      19–20. V17 Structural Alpha Expansion (subordinated score 221.0–224+):
            BLOWOFF_2H (2H 2-bar post-squeeze blow-off reversal, Perp-Only & Deep-Bear safe, SL 5.5%) and
            BASIMP_1H (1H positive basis-impulse fresh breakdown `bas1h >= +0.03%`, `bas1h > bas8h + 0.05%`, `bas8h >= -0.05%`, SL 5.0%).
      21. V18 Accelerating Perp FOMO Continuation (V112–V115 7-layer audit, subordinated score 220.8+, max 2):
            BASFOMO_1H (`bas15 >= +0.03%`, `bas15 > bas1h > bas8h + 0.03%`, `bas8h >= -0.05%`, below EMA10 `ext >= -4%`,
            heavy Spot distribution `spot_tb_1h < 0.36`, `spot_vrat_1h >= 1.5x`, `tb_1h < 0.42`, `lwick_1h < 0.30`, SL 5.0%).
      22. V19 Deep-Bear & Perp-Only Pullback Rejection (V118–V126 7-layer audit, subordinated score 220.6+, max 2):
            EMAREJ_2H (2H Pullback-to-EMA10 Kiss Rejection in established downtrend `c_2h < ema10_2h < ema20_2h`,
            `c_2h_prev < ema10_2h_prev`, `c_2h < d_sma20`, `hi_2h >= ema10_2h * 0.997`, `uwick_2h >= 0.25`, `lwick_2h < 0.25`,
            `tb_2h < 0.42`, `vrat_2h >= 1.3x`, `clean_bas`, ZERO Spot dependency, SL 5.0%).

    Applies Volatility Floor (`atr14_1h_pct >= 0.8%` when available) and anti-chase guard (`cur_px >= sig_close * 0.97`).
    Returns list of dicts `{setup, score, stop_pct, tp_pct, exit_mode}` sorted by highest score first.
    """
    hits = []
    if cur_px is None or cur_px <= 0:
        return hits
    if atr14_1h_pct is not None and atr14_1h_pct < MIN_ATR14_1H_PCT:
        return hits

    bd_ok = (breadth_1d is None) or (breadth_1d <= BULL_BREADTH_MAX)
    btc_ok = (btc_ret7d is None) or (btc_ret7d < BTC_RET7D_MAX)
    not_crowded = (bas8h is None) or (bas8h >= BASIS_NOT_CROWDED_MIN)
    clean_bas = (bas8h is None) or (bas8h >= BASIS_CLEAN_MIN)
    red_1h = (o_1h is not None and c_1h is not None and c_1h < o_1h)
    no_chase_1h = (c_1h is not None and cur_px >= c_1h * (1.0 - MAX_ENTRY_CHASE_DROP))
    lwick_1h = (
        ((min(o_1h, c_1h) - lo_1h) / (hi_1h - lo_1h))
        if (o_1h is not None and c_1h is not None and hi_1h is not None and lo_1h is not None and hi_1h > lo_1h)
        else None
    )
    vrat_1h = (qv_1h / qv_avg20_1h) if (qv_1h is not None and qv_avg20_1h is not None and qv_avg20_1h > 0) else None
    fresh_1h_cross = (
        ema10_1h is not None
        and ema10_1h_prev is not None
        and c_1h is not None
        and c_1h_prev is not None
        and c_1h < ema10_1h
        and c_1h_prev >= ema10_1h_prev
        and cur_px < ema10_1h
    )

    # Priority #1: DCAT_WICK_1H — All-age Bear Dead-Cat Bounce blow-off wick sniper (SL 5.0%, TP 60%)
    if (
        bar_complete_1h
        and (breadth_1d is None or breadth_1d <= 0.45)
        and clean_bas
        and red_1h
        and no_chase_1h
        and tb_1h is not None
        and tb_1h < 0.45
        and d_sma20 is not None
        and c_1h is not None
        and c_1h < d_sma20
        and cur_px < d_sma20
        and hi_1h is not None
        and lo_1h is not None
        and hi_1h > lo_1h
        and vrat_1h is not None
        and vrat_1h >= 1.5
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        spiked = (
            (ret_4h is not None and ret_4h >= 0.06)
            or (ret_12h is not None and ret_12h >= 0.10)
            or (ret_24h is not None and ret_24h >= 0.12)
        )
        uwick = (hi_1h - max(o_1h, c_1h)) / (hi_1h - lo_1h)
        if spiked and uwick >= 0.30:
            sc = 368.0 + uwick * 50.0 + (0.45 - tb_1h) * 60.0
            hits.append({"setup": "DCAT_WICK_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Shared 4H Perp fresh red cross < EMA10 check (`fresh_4h_perp` works for all coins; `fresh_4h_base` adds has_spot_24h)
    red_4h = (o_4h is not None and c_4h is not None and c_4h < o_4h)
    no_chase_4h = (c_4h is not None and cur_px >= c_4h * (1.0 - MAX_ENTRY_CHASE_DROP))
    lwick_4h = (
        ((min(o_4h, c_4h) - lo_4h) / (hi_4h - lo_4h))
        if (o_4h is not None and c_4h is not None and hi_4h is not None and lo_4h is not None and hi_4h > lo_4h)
        else None
    )
    vrat_4h = (qv_4h / qv_avg20_4h) if (qv_4h is not None and qv_avg20_4h is not None and qv_avg20_4h > 0) else None
    fresh_4h_perp = (
        is_fresh_4h_window
        and bar_complete_4h
        and btc_ok
        and no_chase_4h
        and ema10_4h is not None
        and ema10_4h_prev is not None
        and c_4h is not None
        and c_4h_prev is not None
        and c_4h < ema10_4h
        and c_4h_prev >= ema10_4h_prev
        and cur_px < ema10_4h
        and red_4h
        and tb_4h is not None
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    )
    fresh_4h_base = fresh_4h_perp and has_spot_24h
    fresh_4h_cross = fresh_4h_base and tb_4h < SPOTDMP_PTB_MAX

    # Priority #2: BASCRUSH_4H — All-age 4H Perp-Spot Basis compression unwind (`BD <= 42%` V11 + `btc_ret4h > -4.0%` V13 Flash-Crash guard)
    if (
        fresh_4h_cross
        and (breadth_1d is None or breadth_1d <= BASCRUSH_BD_MAX)
        and (btc_ret4h is None or btc_ret4h > BASCRUSH_BTC_RET4H_MIN)
        and not_crowded
        and bas12h_max is not None
        and bas_4h_close is not None
        and bas12h_max >= BASCRUSH_PEAK12H_MIN
        and bas_4h_close < (bas12h_max - BASCRUSH_DROP_MIN)
        and bas_4h_close >= BASIS_NOT_CROWDED_MIN
    ):
        sc = 335.0 + bas12h_max * 4000.0 + (0.50 - tb_4h) * 40.0
        hits.append({"setup": "BASCRUSH_4H", "score": round(sc, 2), "stop_pct": 0.060, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #3: PARA75_G_1H — All-age Basis+Macro-guarded parabolic crack (SL 7.5%, TP 60%)
    if (
        bar_complete_1h
        and bd_ok
        and btc_ok
        and not_crowded
        and no_chase_1h
        and ret_7d is not None
        and ret_7d >= 0.75
        and hi7d is not None
        and hi7d > 0
        and c_1h is not None
        and -0.22 <= (c_1h / hi7d - 1.0) <= -0.06
        and cur_px <= hi7d * 0.94
        and fresh_1h_cross
        and red_1h
        and tb_1h is not None
        and tb_1h < 0.46
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 332.0 + ret_7d * 80.0 + (0.50 - tb_1h) * 40.0
        hits.append({"setup": "PARA75_G_1H", "score": round(sc, 2), "stop_pct": 0.075, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #4: NEW7_BAS_1H — Basis-guarded new listing structural sell flow (age <= 7d, SL 6.0%, TP 60%, V12_NEW7_SQZ96)
    if (
        bar_complete_1h
        and (bars_1h_count is None or bars_1h_count >= NEW7_MIN_BARS_1H)
        and bd_ok
        and not_crowded
        and no_chase_1h
        and age_days is not None
        and age_days <= 7.0
        and ema10_1h is not None
        and c_1h is not None
        and c_1h < ema10_1h
        and cur_px < ema10_1h
        and red_1h
        and tb_1h is not None
        and tb_1h < 0.48
        and (hi24 is None or hi24 <= 0 or (c_1h <= hi24 * 0.97 and cur_px <= hi24 * 0.97))
        and (ret_24h is None or ret_24h <= 0.10)
        and (ret_4h is None or ret_4h <= 0.02)
        and f_day >= MIN_FUNDING_DAY
    ):
        sc = 300.0 - age_days * 15.0 + (0.50 - tb_1h) * 50.0
        hits.append({"setup": "NEW7_BAS_1H", "score": round(sc, 2), "stop_pct": 0.060, "tp_pct": 0.600, "exit_mode": NEW7_EXIT_MODE})

    # Priority #5: BASOVH_1H — 24/7 Hourly All-Age Crowded-Long Perp Basis Overheat Unwind (SL 5.5%, TP 60%, max 1, heat <= 12U)
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= BASOVH1H_BD_MAX)
        and red_1h
        and no_chase_1h
        and fresh_1h_cross
        and lwick_1h is not None
        and lwick_1h < BASOVH1H_LWICK_MAX
        and bas8h is not None
        and bas8h >= BASOVH1H_BAS8H_MIN
        and tb_1h is not None
        and tb_1h < BASOVH1H_TB_MAX
        and spot_1h_complete
        and spot_tb_1h is not None
        and spot_tb_1h < BASOVH1H_STB_MAX
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 265.0 + bas8h * 5000.0 + (0.45 - tb_1h) * 50.0
        hits.append({"setup": "BASOVH_1H", "score": round(sc, 2), "stop_pct": 0.055, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #6: WHALESP_1H — 24/7 Hourly All-Age Institutional Spot Whale Distribution (SL 5.0%, TP 60%, max 1, heat <= 10U)
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= WHALESP1H_BD_MAX)
        and bas8h is not None
        and bas8h >= WHALESP1H_BAS8H_MIN
        and red_1h
        and no_chase_1h
        and fresh_1h_cross
        and lwick_1h is not None
        and lwick_1h < WHALESP1H_LWICK_MAX
        and tb_1h is not None
        and tb_1h < WHALESP1H_PTB_MAX
        and spot_1h_complete
        and spot_tb_1h is not None
        and spot_tb_1h < WHALESP1H_STB_MAX
        and spot_vrat_1h is not None
        and spot_vrat_1h >= WHALESP1H_SVRAT_MIN
        and spot_tsize_r_1h is not None
        and spot_tsize_r_1h >= WHALESP1H_TSIZE_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 262.0 + min(spot_tsize_r_1h, 3.0) * 15.0 + (0.45 - spot_tb_1h) * 80.0
        hits.append({"setup": "WHALESP_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #5: POST30D_4H — Post-Listing Phase-2 Distribution (7 < age <= 30d, works on all coins, SL 5.5%, TP 60%, heat <= 10U)
    if (
        fresh_4h_perp
        and age_days is not None
        and POST30D4H_AGE_MIN < age_days <= POST30D4H_AGE_MAX
        and (breadth_1d is None or breadth_1d <= POST30D4H_BD_MAX)
        and clean_bas
        and lwick_4h is not None
        and lwick_4h < POST30D4H_LWICK_MAX
        and tb_4h < POST30D4H_TB_MAX
        and vrat_4h is not None
        and vrat_4h >= POST30D4H_VRAT_MIN
    ):
        sc = 252.0 + (0.45 - tb_4h) * 60.0
        hits.append({"setup": "POST30D_4H", "score": round(sc, 2), "stop_pct": 0.055, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #6: SP_LEAD_4H — High-Purity All-Age 4H Institutional Spot-Leads-Perp Divergence (Idle-Heat <= 5U, SL 5.0%, TP 60%)
    if (
        fresh_4h_base
        and (breadth_1d is None or breadth_1d <= SPLEAD4H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPLEAD4H_BAS8H_MIN
        and lwick_4h is not None
        and lwick_4h < SPLEAD4H_LWICK_MAX
        and spot_4h_complete
        and spot_tb_4h is not None
        and spot_tb_4h < SPLEAD4H_STB_MAX
        and spot_tb_4h < (tb_4h - SPLEAD4H_DIV_MIN)
        and spot_vrat_4h is not None
        and spot_vrat_4h >= SPLEAD4H_SVRAT_MIN
    ):
        sc = 246.0 + (tb_4h - spot_tb_4h) * 100.0 + (0.42 - spot_tb_4h) * 80.0
        hits.append({"setup": "SP_LEAD_4H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #7: BEAR_BRK_1H — Deep-Bear Compression/Flag Support Breakdown (100% All-Age, Idle-Heat <= 5U, NO fresh_e10, SL 5.0%, TP 60%)
    if (
        bar_complete_1h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= BEARBRK1H_BD_MAX)
        and bas8h is not None
        and bas8h >= BEARBRK1H_BAS8H_MIN
        and red_1h
        and no_chase_1h
        and d_sma20 is not None
        and c_1h is not None
        and c_1h < d_sma20
        and cur_px < d_sma20
        and ema10_1h is not None
        and ema10_1h > 0
        and ema20_1h is not None
        and ema10_1h < ema20_1h
        and lo16_1h_prev is not None
        and c_1h_prev is not None
        and c_1h < lo16_1h_prev
        and c_1h_prev >= lo16_1h_prev
        and cur_px < lo16_1h_prev
        and (c_1h / ema10_1h - 1.0) >= BEARBRK1H_EXT_E10_MIN
        and lwick_1h is not None
        and lwick_1h < BEARBRK1H_LWICK_MAX
        and tb_1h is not None
        and tb_1h < BEARBRK1H_TB_MAX
        and vrat_1h is not None
        and vrat_1h >= BEARBRK1H_VRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 244.0 + (0.42 - tb_1h) * 80.0 + min(vrat_1h, 5.0) * 4.0
        hits.append({"setup": "BEAR_BRK_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #8: SPOTDMP_4H — Opportunistic All-Age 4H Perp-Heavy Spot Distribution (ps_vrat >= 2.0, lwick < 0.35, SL 5.0%, TP 60%)
    if (
        fresh_4h_cross
        and (breadth_1d is None or breadth_1d <= SPOTDMP_BD_MAX)
        and clean_bas
        and (lwick_4h is None or lwick_4h < SPOTDMP4H_LWICK_MAX)
        and spot_tb_4h is not None
        and spot_tb_4h < SPOTDMP_STB_MAX
        and ps_vrat is not None
        and ps_vrat >= SPOTDMP_MIN_PS_VRAT
    ):
        sc = 240.0 + (0.45 - spot_tb_4h) * 100.0 + (0.45 - tb_4h) * 60.0
        hits.append({"setup": "SPOTDMP_4H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #11: SPOTDMP_1H — High-Purity All-Age 1H Spot Distribution Surge (24/7 hourly, ps_vrat >= 2.0, bas8h >= 0%, SL 5.0%, TP 60%)
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= SPOTDMP1H_BD_MAX)
        and red_1h
        and no_chase_1h
        and fresh_1h_cross
        and lwick_1h is not None
        and lwick_1h < SPOTDMP1H_LWICK_MAX
        and bas8h is not None
        and bas8h >= SPOTDMP1H_BAS8H_MIN
        and ps_vrat is not None
        and ps_vrat >= SPOTDMP_MIN_PS_VRAT
        and spot_1h_complete
        and spot_tb_1h is not None
        and spot_tb_1h < SPOTDMP1H_STB_MAX
        and spot_vrat_1h is not None
        and spot_vrat_1h >= SPOTDMP1H_SVRAT_MIN
        and tb_1h is not None
        and tb_1h < SPOTDMP1H_PTB_MAX
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 238.0 + (0.45 - spot_tb_1h) * 100.0 + (0.45 - tb_1h) * 60.0
        hits.append({"setup": "SPOTDMP_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #12: SPOTFLOW_POS_1H — V15 24/7 Hourly All-Age Positive-Basis Spot-Flow Continuation (NO fresh_e10, SL 5.0%, TP 60%, max 2)
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= SPOTFLOW1H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPOTFLOW1H_BAS8H_MIN
        and red_1h
        and no_chase_1h
        and ema10_1h is not None
        and ema10_1h > 0
        and c_1h is not None
        and c_1h < ema10_1h
        and cur_px < ema10_1h
        and (c_1h / ema10_1h - 1.0) >= SPOTFLOW1H_EXT_E10_MIN
        and lwick_1h is not None
        and lwick_1h < SPOTFLOW1H_LWICK_MAX
        and tb_1h is not None
        and tb_1h < SPOTFLOW1H_PTB_MAX
        and spot_1h_complete
        and spot_tb_1h is not None
        and spot_tb_1h < SPOTFLOW1H_STB_MAX
        and spot_vrat_1h is not None
        and spot_vrat_1h >= SPOTFLOW1H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 225.0 + (0.45 - spot_tb_1h) * 80.0 + (0.45 - tb_1h) * 50.0
        hits.append({"setup": "SPOTFLOW_POS_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # === V16 Strict Positive-Basis 2H/4H Institutional Moat (Subordinated score 221.5–223.2; zero cannibalization of V15 Core) ===
    red_2h = (o_2h is not None and c_2h is not None and c_2h < o_2h)
    no_chase_2h = (c_2h is not None and cur_px >= c_2h * (1.0 - MAX_ENTRY_CHASE_DROP))
    lwick_2h = (
        ((min(o_2h, c_2h) - lo_2h) / (hi_2h - lo_2h))
        if (o_2h is not None and c_2h is not None and hi_2h is not None and lo_2h is not None and hi_2h > lo_2h)
        else None
    )
    uwick_2h = (
        ((hi_2h - max(o_2h, c_2h)) / (hi_2h - lo_2h))
        if (o_2h is not None and c_2h is not None and hi_2h is not None and lo_2h is not None and hi_2h > lo_2h)
        else None
    )
    vrat_2h = (qv_2h / qv_avg20_2h) if (qv_2h is not None and qv_avg20_2h is not None and qv_avg20_2h > 0) else None
    below_2h_e10 = (
        ema10_2h is not None
        and ema10_2h > 0
        and c_2h is not None
        and c_2h < ema10_2h
        and cur_px < ema10_2h
    )
    fresh_2h_base = (
        is_fresh_2h_window
        and bar_complete_2h
        and has_spot_24h
        and btc_ok
        and red_2h
        and no_chase_2h
        and below_2h_e10
        and ema10_2h_prev is not None
        and c_2h_prev is not None
        and c_2h_prev >= ema10_2h_prev
        and tb_2h is not None
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    )

    # Priority #13: SP_LEAD_2H — 2H Institutional Spot-Leads-Perp Divergence (strict bas8h >= 0%, SL 5.0%, TP 60%)
    if (
        fresh_2h_base
        and (breadth_1d is None or breadth_1d <= SPLEAD2H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPLEAD2H_BAS8H_MIN
        and lwick_2h is not None
        and lwick_2h < SPLEAD2H_LWICK_MAX
        and spot_2h_complete
        and spot_tb_2h is not None
        and spot_tb_2h < SPLEAD2H_STB_MAX
        and spot_tb_2h < (tb_2h - SPLEAD2H_DIV_MIN)
        and spot_vrat_2h is not None
        and spot_vrat_2h >= SPLEAD2H_SVRAT_MIN
    ):
        sc = 223.2 + min(0.25, tb_2h - spot_tb_2h) * 5.0
        hits.append({"setup": "SP_LEAD_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #14: WHALESP_2H — 2H Institutional Spot Whale Distribution (strict bas8h >= 0%, SL 5.0%, TP 60%)
    if (
        fresh_2h_base
        and (breadth_1d is None or breadth_1d <= WHALESP2H_BD_MAX)
        and bas8h is not None
        and bas8h >= WHALESP2H_BAS8H_MIN
        and lwick_2h is not None
        and lwick_2h < WHALESP2H_LWICK_MAX
        and tb_2h < WHALESP2H_PTB_MAX
        and spot_2h_complete
        and spot_tb_2h is not None
        and spot_tb_2h < WHALESP2H_STB_MAX
        and spot_vrat_2h is not None
        and spot_vrat_2h >= WHALESP2H_SVRAT_MIN
        and spot_tsize_r_2h is not None
        and spot_tsize_r_2h >= WHALESP2H_TSIZE_MIN
    ):
        sc = 223.0 + min(spot_tsize_r_2h, 3.0) * 0.5
        hits.append({"setup": "WHALESP_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #15: WHALESP_4H — 4H Institutional Spot Whale Distribution (strict bas8h >= 0%, SL 5.0%, TP 60%)
    if (
        fresh_4h_base
        and (breadth_1d is None or breadth_1d <= WHALESP2H_BD_MAX)
        and bas8h is not None
        and bas8h >= WHALESP2H_BAS8H_MIN
        and lwick_4h is not None
        and lwick_4h < WHALESP2H_LWICK_MAX
        and tb_4h < WHALESP2H_PTB_MAX
        and spot_4h_complete
        and spot_tb_4h is not None
        and spot_tb_4h < WHALESP2H_STB_MAX
        and spot_vrat_4h is not None
        and spot_vrat_4h >= WHALESP2H_SVRAT_MIN
        and spot_tsize_r_4h is not None
        and spot_tsize_r_4h >= WHALESP2H_TSIZE_MIN
    ):
        sc = 223.0 + min(spot_tsize_r_4h, 3.0) * 0.5
        hits.append({"setup": "WHALESP_4H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #16: SPOTDMP_POS_2H — 2H Perp-Heavy Spot Distribution Surge (strict bas8h >= 0%, ps_vrat >= 2.0x, SL 5.0%)
    if (
        fresh_2h_base
        and (breadth_1d is None or breadth_1d <= SPOTDMP2H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPOTDMP2H_BAS8H_MIN
        and lwick_2h is not None
        and lwick_2h < SPOTDMP2H_LWICK_MAX
        and ps_vrat is not None
        and ps_vrat >= SPOTDMP_MIN_PS_VRAT
        and tb_2h < SPOTDMP2H_PTB_MAX
        and spot_2h_complete
        and spot_tb_2h is not None
        and spot_tb_2h < SPOTDMP2H_STB_MAX
        and spot_vrat_2h is not None
        and spot_vrat_2h >= SPOTDMP2H_SVRAT_MIN
    ):
        sc = 222.5 + (0.45 - spot_tb_2h) * 5.0
        hits.append({"setup": "SPOTDMP_POS_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #17: SPOTFLOW_POS_2H — 2H Positive-Basis Spot-Flow Continuation (NO fresh_e10, strict bas8h >= 0%, SL 5.0%)
    if (
        is_fresh_2h_window
        and bar_complete_2h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= SPOTFLOW2H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPOTFLOW2H_BAS8H_MIN
        and red_2h
        and no_chase_2h
        and below_2h_e10
        and (c_2h / ema10_2h - 1.0) >= SPOTFLOW2H_EXT_E10_MIN
        and lwick_2h is not None
        and lwick_2h < SPOTFLOW2H_LWICK_MAX
        and tb_2h is not None
        and tb_2h < SPOTFLOW2H_PTB_MAX
        and spot_2h_complete
        and spot_tb_2h is not None
        and spot_tb_2h < SPOTFLOW2H_STB_MAX
        and spot_vrat_2h is not None
        and spot_vrat_2h >= SPOTFLOW2H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 221.5 + (0.45 - spot_tb_2h) * 5.0
        hits.append({"setup": "SPOTFLOW_POS_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #18: SPOTFLOW_POS_4H — 4H Positive-Basis Spot-Flow Continuation (NO fresh_e10, strict bas8h >= 0%, SL 5.0%)
    if (
        is_fresh_4h_window
        and bar_complete_4h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= SPOTFLOW2H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPOTFLOW2H_BAS8H_MIN
        and red_4h
        and no_chase_4h
        and ema10_4h is not None
        and ema10_4h > 0
        and c_4h is not None
        and c_4h < ema10_4h
        and cur_px < ema10_4h
        and (c_4h / ema10_4h - 1.0) >= SPOTFLOW2H_EXT_E10_MIN
        and lwick_4h is not None
        and lwick_4h < SPOTFLOW2H_LWICK_MAX
        and tb_4h is not None
        and tb_4h < SPOTFLOW2H_PTB_MAX
        and spot_4h_complete
        and spot_tb_4h is not None
        and spot_tb_4h < SPOTFLOW2H_STB_MAX
        and spot_vrat_4h is not None
        and spot_vrat_4h >= SPOTFLOW2H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 221.5 + (0.45 - spot_tb_4h) * 5.0
        hits.append({"setup": "SPOTFLOW_POS_4H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # === V17 Structural Alpha Expansion (V103–V106 7-layer strict Pareto dominance; subordinated 221.0–224+) ===
    # Priority #19: BLOWOFF_2H — 2H 2-Bar Post-Squeeze Blow-Off Reversal (Perp-Only & Deep-Bear safe, SL 5.5%, TP 60%)
    vrat_2h_prev = (
        (qv_2h_prev / qv_avg20_2h_prev)
        if (qv_2h_prev is not None and qv_avg20_2h_prev is not None and qv_avg20_2h_prev > 0)
        else None
    )
    if (
        is_fresh_2h_window
        and bar_complete_2h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= BLOWOFF2H_BD_MAX)
        and clean_bas
        and red_2h
        and no_chase_2h
        and d_sma20 is not None
        and c_2h is not None
        and c_2h < d_sma20
        and cur_px < d_sma20
        and o_2h_prev is not None
        and o_2h_prev > 0
        and c_2h_prev is not None
        and c_2h_prev > o_2h_prev
        and (c_2h_prev / o_2h_prev - 1.0) >= BLOWOFF2H_PREV_RET_MIN
        and vrat_2h_prev is not None
        and vrat_2h_prev >= BLOWOFF2H_PREV_VRAT_MIN
        and c_2h < (o_2h_prev + c_2h_prev) * 0.5
        and cur_px < (o_2h_prev + c_2h_prev) * 0.5
        and tb_2h is not None
        and tb_2h < BLOWOFF2H_TB_MAX
        and lwick_2h is not None
        and lwick_2h < BLOWOFF2H_LWICK_MAX
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        prev_ret_2h = c_2h_prev / o_2h_prev - 1.0
        sc = 223.8 + prev_ret_2h * 10.0
        hits.append({"setup": "BLOWOFF_2H", "score": round(sc, 2), "stop_pct": 0.055, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #20: BASIMP_1H — 1H Positive Basis-Impulse Fresh Breakdown (bas1h >= +0.03%, bas1h > bas8h + 0.05%, bas8h >= -0.05%, SL 5.0%, TP 60%)
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= BASIMP1H_BD_MAX)
        and red_1h
        and no_chase_1h
        and fresh_1h_cross
        and lwick_1h is not None
        and lwick_1h < BASIMP1H_LWICK_MAX
        and bas1h is not None
        and bas8h is not None
        and bas1h >= BASIMP1H_BAS1H_MIN
        and bas1h > (bas8h + BASIMP1H_JUMP_MIN)
        and bas8h >= BASIMP1H_BAS8H_MIN
        and spot_1h_complete
        and spot_tb_1h is not None
        and spot_tb_1h < BASIMP1H_STB_MAX
        and tb_1h is not None
        and tb_1h < BASIMP1H_PTB_MAX
        and spot_vrat_1h is not None
        and spot_vrat_1h >= BASIMP1H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 221.0 + (0.45 - spot_tb_1h) * 5.0
        hits.append({"setup": "BASIMP_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # === V18 Structural Alpha Expansion (V112–V115 7-layer audit; subordinated score 220.8+, max 2 concurrent) ===
    # Priority #21: BASFOMO_1H — 1H 3-Step Accelerating Perp Retail FOMO (bas15 > bas1h > bas8h + 0.03%) into Spot Distribution Continuation
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= BASFOMO1H_BD_MAX)
        and red_1h
        and no_chase_1h
        and ema10_1h is not None
        and ema10_1h > 0
        and c_1h is not None
        and c_1h < ema10_1h
        and cur_px < ema10_1h
        and (c_1h / ema10_1h - 1.0) >= BASFOMO1H_EXT_E10_MIN
        and lwick_1h is not None
        and lwick_1h < BASFOMO1H_LWICK_MAX
        and bas15 is not None
        and bas1h is not None
        and bas8h is not None
        and bas15 >= BASFOMO1H_BAS15_MIN
        and bas15 > bas1h
        and bas1h > (bas8h + BASFOMO1H_B1_JUMP_MIN)
        and bas8h >= BASFOMO1H_BAS8H_MIN
        and spot_1h_complete
        and spot_tb_1h is not None
        and spot_tb_1h < BASFOMO1H_STB_MAX
        and tb_1h is not None
        and tb_1h < BASFOMO1H_PTB_MAX
        and spot_vrat_1h is not None
        and spot_vrat_1h >= BASFOMO1H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 220.8 + (0.45 - spot_tb_1h) * 5.0
        hits.append({"setup": "BASFOMO_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # === V19 Deep-Bear & Perp-Only Structural Expansion (V118–V126 7-layer audit; subordinated score 220.6+, max 2 concurrent) ===
    # Priority #22: EMAREJ_2H — 2H Pullback-to-EMA10 Kiss Rejection in Established Downtrend (Perp-Only & Deep-Bear safe, SL 5.0%, TP 60%)
    if (
        is_fresh_2h_window
        and bar_complete_2h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= EMAREJ2H_BD_MAX)
        and clean_bas
        and red_2h
        and no_chase_2h
        and d_sma20 is not None
        and c_2h is not None
        and c_2h < d_sma20
        and cur_px < d_sma20
        and below_2h_e10
        and ema20_2h is not None
        and ema10_2h < ema20_2h
        and ema10_2h_prev is not None
        and c_2h_prev is not None
        and c_2h_prev < ema10_2h_prev
        and hi_2h is not None
        and hi_2h >= ema10_2h * EMAREJ2H_KISS_RATIO
        and uwick_2h is not None
        and uwick_2h >= EMAREJ2H_UWICK_MIN
        and lwick_2h is not None
        and lwick_2h < EMAREJ2H_LWICK_MAX
        and tb_2h is not None
        and tb_2h < EMAREJ2H_TB_MAX
        and vrat_2h is not None
        and vrat_2h >= EMAREJ2H_VRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 220.6 + uwick_2h * 0.5
        hits.append({"setup": "EMAREJ_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # === V20 Neutral/Bull 2H Institutional Overheat Moat (V127–V133 7-layer audit; 45% < BD <= 85%, b1h <= 72%, max 2 concurrent) ===
    # Priority #23: BULL_SPOTFLOW_2H — 2H Crowded-Long Basis Overheat Institutional Spot-Flow Continuation (SL 5.5%, TP 60%)
    if (
        is_fresh_2h_window
        and bar_complete_2h
        and has_spot_24h
        and breadth_1d is not None
        and BULLSF2H_BD_MIN < breadth_1d <= BULLSF2H_BD_MAX
        and (breadth_1h is None or breadth_1h <= BULLSF2H_B1H_MAX)
        and red_2h
        and no_chase_2h
        and below_2h_e10
        and (c_2h / ema10_2h - 1.0) >= BULLSF2H_EXT_E10_MIN
        and lwick_2h is not None
        and lwick_2h < BULLSF2H_LWICK_MAX
        and bas8h is not None
        and bas8h >= BULLSF2H_BAS8H_MIN
        and spot_2h_complete
        and spot_tb_2h is not None
        and spot_tb_2h < BULLSF2H_STB_MAX
        and tb_2h is not None
        and tb_2h < BULLSF2H_PTB_MAX
        and spot_vrat_2h is not None
        and spot_vrat_2h >= BULLSF2H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 220.4 + (0.45 - spot_tb_2h) * 5.0
        hits.append({"setup": "BULL_SPOTFLOW_2H", "score": round(sc, 2), "stop_pct": 0.055, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # === V21 Institutional Spot OHLC Upper-Wick Rejection Suite (V135–V140 7-layer audit; max 2 concurrent each) ===
    # Priority #24: SP_UWICK_1H — 1H Spot OHLC Upper-Wick Fresh-EMA10 Rejection (24/7 incl. odd UTC hours, SL 5.0%, TP 60%)
    if (
        bar_complete_1h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= SPUW1H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPUW1H_BAS8H_MIN
        and red_1h
        and no_chase_1h
        and fresh_1h_cross
        and lwick_1h is not None
        and lwick_1h < SPUW1H_LWICK_MAX
        and tb_1h is not None
        and tb_1h < SPUW1H_PTB_MAX
        and spot_1h_complete
        and spot_red_1h is True
        and spot_uwick_1h is not None
        and spot_uwick_1h >= SPUW1H_UWICK_MIN
        and spot_tb_1h is not None
        and spot_tb_1h < SPUW1H_STB_MAX
        and spot_vrat_1h is not None
        and spot_vrat_1h >= SPUW1H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 220.35 + spot_uwick_1h
        hits.append({"setup": "SP_UWICK_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    # Priority #25: SP_UWICK_2H — 2H Spot OHLC Upper-Wick Rejection below EMA10 (even UTC hours, SL 5.0%, TP 60%)
    if (
        is_fresh_2h_window
        and bar_complete_2h
        and has_spot_24h
        and btc_ok
        and (breadth_1d is None or breadth_1d <= SPUW2H_BD_MAX)
        and bas8h is not None
        and bas8h >= SPUW2H_BAS8H_MIN
        and red_2h
        and no_chase_2h
        and below_2h_e10
        and lwick_2h is not None
        and lwick_2h < SPUW2H_LWICK_MAX
        and tb_2h is not None
        and tb_2h < SPUW2H_PTB_MAX
        and spot_2h_complete
        and spot_red_2h is True
        and spot_uwick_2h is not None
        and spot_uwick_2h >= SPUW2H_UWICK_MIN
        and spot_tb_2h is not None
        and spot_tb_2h < SPUW2H_STB_MAX
        and spot_vrat_2h is not None
        and spot_vrat_2h >= SPUW2H_SVRAT_MIN
        and f_day >= MIN_EXHAUST_FUNDING_DAY
    ):
        sc = 220.45 + spot_uwick_2h
        hits.append({"setup": "SP_UWICK_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": DEFAULT_EXIT_MODE})

    hits.sort(key=lambda x: -x["score"])
    return hits


evaluate_v21_short_setups = evaluate_v12_short_setups
evaluate_v20_short_setups = evaluate_v12_short_setups
evaluate_v19_short_setups = evaluate_v12_short_setups
evaluate_v18_short_setups = evaluate_v12_short_setups
evaluate_v17_short_setups = evaluate_v12_short_setups
evaluate_v16_short_setups = evaluate_v12_short_setups
evaluate_v15_short_setups = evaluate_v12_short_setups
evaluate_v14_short_setups = evaluate_v12_short_setups
evaluate_v13_short_setups = evaluate_v12_short_setups
evaluate_v11_short_setups = evaluate_v12_short_setups
evaluate_v10_short_setups = evaluate_v12_short_setups
evaluate_v9_short_setups = evaluate_v12_short_setups
evaluate_v8_short_setups = evaluate_v12_short_setups
evaluate_v7_short_setups = evaluate_v12_short_setups
evaluate_v6_short_setups = evaluate_v12_short_setups
evaluate_v5_short_setups = evaluate_v12_short_setups
evaluate_v4_short_setups = evaluate_v12_short_setups
evaluate_v3_short_setups = evaluate_v12_short_setups


def evaluate_v22_long_setups(
    *,
    breadth_1d=None,
    breadth_1h=None,
    btc_px=None,
    btc_1h_ema20=None,
    btc_ret1h=None,
    bar_complete_1h=False,
    cur_px=None,
    o_1h=None,
    hi_1h=None,
    lo_1h=None,
    c_1h=None,
    c_1h_prev=None,
    qv_1h=None,
    qv_avg20_1h=None,
    tb_1h=None,
    ema10_1h=None,
    ema10_1h_prev=None,
    ema20_1h=None,
    hi16_1h_prev=None,
    rsi1h_now=None,
    atr14_1h_pct=None,
    ret_24h=None,
    d_sma20=None,
    f_day=0.0,
    has_spot_24h=False,
    bas8h=None,
    spot_1h_complete=False,
    spot_tb_1h=None,
    spot_vrat_1h=None,
    spot_green_1h=None,
    spot_lwick_1h=None,
    is_fresh_2h_window=False,
    bar_complete_2h=False,
    o_2h=None,
    hi_2h=None,
    lo_2h=None,
    c_2h=None,
    c_2h_prev=None,
    qv_2h=None,
    qv_avg20_2h=None,
    tb_2h=None,
    ema10_2h=None,
    ema10_2h_prev=None,
    ema20_2h=None,
):
    """Evaluate 5 disciplined V22 Intraday LONG engines (`V22_LONG_SQZ96` exit, SL 5.0-5.5%, TP 40-50%).

      1. L_SQUEEZE_1H: Crowded negative-basis / negative-funding short-squeeze reclaim above 1H EMA10 (works in deep bear).
      2. L_SPOTACC_1H: Institutional Spot-led accumulation breakout (`spot_green`, `spot_tb >= 0.58`, `spot_vrat >= 1.5x`).
      3. L_SP_LWICK_1H: Institutional Spot lower-wick dip absorption / spring (`spot_green`, `spot_lwick >= 0.30`).
      4. L_TREND_BRK_1H: Uptrend leader 16h-high or EMA10+RSI momentum breakout in recovery/bull regime.
      5. L_EMA_BOUNCE_2H: Established 2H uptrend pullback-to-EMA10 kiss bounce (zero Spot dependency).
    """
    hits = []
    if cur_px is None or cur_px <= 0:
        return hits
    if atr14_1h_pct is not None and atr14_1h_pct < MIN_ATR14_1H_PCT:
        return hits
    if f_day is not None and f_day > LONG_MAX_FUNDING_DAY:
        return hits

    clean_long_bas = (bas8h is None) or (bas8h <= LONG_MAX_BAS8H)
    green_1h = (o_1h is not None and c_1h is not None and c_1h > o_1h)
    no_chase_1h = (c_1h is not None and c_1h > 0 and cur_px <= c_1h * (1.0 + MAX_ENTRY_CHASE_PUMP))
    rng_1h = (hi_1h - lo_1h) if (hi_1h is not None and lo_1h is not None and hi_1h > lo_1h) else None
    uwick_1h = ((hi_1h - max(o_1h, c_1h)) / rng_1h) if (rng_1h and o_1h is not None and c_1h is not None) else None
    vrat_1h = (qv_1h / qv_avg20_1h) if (qv_1h is not None and qv_avg20_1h is not None and qv_avg20_1h > 0) else None
    ext_e10_1h = ((c_1h / ema10_1h) - 1.0) if (c_1h is not None and ema10_1h is not None and ema10_1h > 0) else None
    fresh_1h_long_cross = (
        ema10_1h is not None
        and ema10_1h_prev is not None
        and c_1h is not None
        and c_1h_prev is not None
        and c_1h > ema10_1h
        and c_1h_prev <= ema10_1h_prev
        and cur_px > ema10_1h
    )

    # 1. L_SQUEEZE_1H: Crowded Negative-Basis / Negative-Funding Short-Squeeze Reclaim (SL 5.0%, TP 40%)
    crowded_short = (
        (bas8h is not None and bas8h <= LSQZ1H_BAS8H_MAX)
        or (f_day is not None and f_day <= LSQZ1H_FUND_MAX)
    )
    if (
        bar_complete_1h
        and crowded_short
        and green_1h
        and no_chase_1h
        and ema10_1h is not None
        and c_1h > ema10_1h
        and cur_px > ema10_1h
        and ext_e10_1h is not None
        and ext_e10_1h <= LSQZ1H_EXT_E10_MAX
        and uwick_1h is not None
        and uwick_1h < LSQZ1H_UWICK_MAX
        and tb_1h is not None
        and tb_1h >= LSQZ1H_TB_MIN
        and (fresh_1h_long_cross or ret_24h is None or ret_24h < LSQZ1H_MAX_RET24)
    ):
        spot_ok_sqz = (
            has_spot_24h
            and spot_1h_complete
            and spot_tb_1h is not None
            and spot_tb_1h >= LSQZ1H_STB_MIN
            and spot_vrat_1h is not None
            and spot_vrat_1h >= LSQZ1H_SVRAT_MIN
        )
        perp_ok_sqz = (
            vrat_1h is not None
            and vrat_1h >= LSQZ1H_VRAT_MIN
            and (spot_tb_1h is None or spot_tb_1h >= 0.50)
        )
        if spot_ok_sqz or perp_ok_sqz:
            bas_bonus = min(max(-(bas8h or 0.0) * 8000.0, 0.0), 15.0)
            sc = 240.0 + (tb_1h - LSQZ1H_TB_MIN) * 60.0 + bas_bonus + (6.0 if spot_ok_sqz else 0.0)
            hits.append({"side": "LONG", "setup": "L_SQUEEZE_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.400, "exit_mode": LONG_EXIT_MODE})

    # 2. L_SPOTACC_1H: Institutional Spot-Led Accumulation Breakout (SL 5.0%, TP 45%)
    if (
        bar_complete_1h
        and clean_long_bas
        and green_1h
        and no_chase_1h
        and (breadth_1h is None or breadth_1h >= LSPOTACC1H_B1H_MIN)
        and (btc_ret1h is None or btc_ret1h > -0.010)
        and has_spot_24h
        and spot_1h_complete
        and spot_green_1h is True
        and spot_tb_1h is not None
        and spot_tb_1h >= LSPOTACC1H_STB_MIN
        and spot_vrat_1h is not None
        and spot_vrat_1h >= LSPOTACC1H_SVRAT_MIN
        and tb_1h is not None
        and tb_1h >= LSPOTACC1H_PTB_MIN
        and uwick_1h is not None
        and uwick_1h < LSPOTACC1H_UWICK_MAX
        and ema10_1h is not None
        and c_1h > ema10_1h
        and cur_px > ema10_1h
        and ext_e10_1h is not None
        and ext_e10_1h <= LSPOTACC1H_EXT_E10_MAX
    ):
        sc = 235.0 + (spot_tb_1h - LSPOTACC1H_STB_MIN) * 70.0 + min(spot_vrat_1h, 5.0) * 2.5
        hits.append({"side": "LONG", "setup": "L_SPOTACC_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.450, "exit_mode": LONG_EXIT_MODE})

    # 3. L_SP_LWICK_1H: Institutional Spot Lower-Wick Absorption / Spring (SL 5.0%, TP 45%)
    if (
        bar_complete_1h
        and (bas8h is None or bas8h <= LSPLWICK1H_BAS8H_MAX)
        and green_1h
        and no_chase_1h
        and has_spot_24h
        and spot_1h_complete
        and spot_green_1h is True
        and spot_lwick_1h is not None
        and spot_lwick_1h >= LSPLWICK1H_LWICK_MIN
        and spot_tb_1h is not None
        and spot_tb_1h >= LSPLWICK1H_STB_MIN
        and spot_vrat_1h is not None
        and spot_vrat_1h >= LSPLWICK1H_SVRAT_MIN
        and tb_1h is not None
        and tb_1h >= LSPLWICK1H_PTB_MIN
        and uwick_1h is not None
        and uwick_1h < LSPLWICK1H_UWICK_MAX
        and ema10_1h is not None
        and c_1h > ema10_1h
        and cur_px > ema10_1h
        and ext_e10_1h is not None
        and ext_e10_1h <= LSQZ1H_EXT_E10_MAX
    ):
        sc = 232.0 + (spot_lwick_1h - LSPLWICK1H_LWICK_MIN) * 35.0 + (spot_tb_1h - LSPLWICK1H_STB_MIN) * 50.0
        hits.append({"side": "LONG", "setup": "L_SP_LWICK_1H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.450, "exit_mode": LONG_EXIT_MODE})

    # 4. L_TREND_BRK_1H: Recovery/Bull Uptrend Leader Breakout (SL 5.5%, TP 50%)
    bull_or_recovery = (
        (breadth_1h is not None and breadth_1h >= LTREND1H_B1H_MIN)
        or (breadth_1d is not None and breadth_1d >= LTREND1H_B1D_MIN)
        or (btc_px is not None and btc_1h_ema20 is not None and btc_px > btc_1h_ema20)
    )
    if (
        bar_complete_1h
        and bull_or_recovery
        and clean_long_bas
        and green_1h
        and no_chase_1h
        and d_sma20 is not None
        and c_1h > d_sma20
        and cur_px > d_sma20
        and ema10_1h is not None
        and ema20_1h is not None
        and c_1h > ema10_1h
        and ema10_1h > ema20_1h
        and cur_px > ema10_1h
        and ext_e10_1h is not None
        and ext_e10_1h <= LTREND1H_EXT_E10_MAX
        and tb_1h is not None
        and tb_1h >= LTREND1H_TB_MIN
        and vrat_1h is not None
        and vrat_1h >= LTREND1H_VRAT_MIN
        and uwick_1h is not None
        and uwick_1h < LTREND1H_UWICK_MAX
    ):
        brk_16h = (hi16_1h_prev is not None and c_1h > hi16_1h_prev and c_1h_prev is not None and c_1h_prev <= hi16_1h_prev)
        rsi_momo = (fresh_1h_long_cross and rsi1h_now is not None and LTREND1H_RSI_MIN <= rsi1h_now <= LTREND1H_RSI_MAX)
        if brk_16h or rsi_momo:
            sc = 230.0 + (tb_1h - LTREND1H_TB_MIN) * 55.0 + min(vrat_1h, 5.0) * 2.0 + (4.0 if brk_16h else 0.0)
            hits.append({"side": "LONG", "setup": "L_TREND_BRK_1H", "score": round(sc, 2), "stop_pct": 0.055, "tp_pct": 0.500, "exit_mode": LONG_EXIT_MODE})

    # 5. L_EMA_BOUNCE_2H: Established 2H Uptrend Pullback-to-EMA10 Kiss Bounce (Zero Spot dependency; SL 5.0%, TP 50%)
    green_2h = (o_2h is not None and c_2h is not None and c_2h > o_2h)
    no_chase_2h = (c_2h is not None and c_2h > 0 and cur_px <= c_2h * (1.0 + MAX_ENTRY_CHASE_PUMP))
    rng_2h = (hi_2h - lo_2h) if (hi_2h is not None and lo_2h is not None and hi_2h > lo_2h) else None
    lwick_2h = ((min(o_2h, c_2h) - lo_2h) / rng_2h) if (rng_2h and o_2h is not None and c_2h is not None) else None
    uwick_2h = ((hi_2h - max(o_2h, c_2h)) / rng_2h) if (rng_2h and o_2h is not None and c_2h is not None) else None
    vrat_2h = (qv_2h / qv_avg20_2h) if (qv_2h is not None and qv_avg20_2h is not None and qv_avg20_2h > 0) else None
    if (
        is_fresh_2h_window
        and bar_complete_2h
        and clean_long_bas
        and green_2h
        and no_chase_2h
        and d_sma20 is not None
        and c_2h > d_sma20
        and cur_px > d_sma20
        and ema10_2h is not None
        and ema20_2h is not None
        and ema10_2h_prev is not None
        and c_2h_prev is not None
        and c_2h > ema10_2h
        and ema10_2h > ema20_2h
        and c_2h_prev > ema10_2h_prev
        and cur_px > ema10_2h
        and lo_2h is not None
        and lo_2h <= ema10_2h * LEMABNC2H_KISS_RATIO
        and lwick_2h is not None
        and lwick_2h >= LEMABNC2H_LWICK_MIN
        and uwick_2h is not None
        and uwick_2h < LEMABNC2H_UWICK_MAX
        and tb_2h is not None
        and tb_2h >= LEMABNC2H_TB_MIN
        and vrat_2h is not None
        and vrat_2h >= LEMABNC2H_VRAT_MIN
    ):
        sc = 228.0 + (lwick_2h - LEMABNC2H_LWICK_MIN) * 25.0 + (tb_2h - LEMABNC2H_TB_MIN) * 50.0
        hits.append({"side": "LONG", "setup": "L_EMA_BOUNCE_2H", "score": round(sc, 2), "stop_pct": 0.050, "tp_pct": 0.500, "exit_mode": LONG_EXIT_MODE})

    hits.sort(key=lambda h: -h["score"])
    return hits


def evaluate_v22_m15_setups(
    *,
    m15_complete=False,
    cur_px=None,
    c15_now=None,
    c15_prev=None,
    hi15_now=None,
    lo15_now=None,
    qv15_now=None,
    qv15_avg20=None,
    ema20_15m=None,
    sma50_15m=None,
    rsi15m_now=None,
    atr14_15m_pct=None,
    low16_15m=None,
    res16_15m=None,
    breadth_1d=None,
    bas8h=None,
    f_day=0.0,
    spot_tb_15m=None,
):
    """Evaluate 2 disciplined 15-minute breakout engines (`M15_BRK_SHORT` & `M15_BRK_LONG`, SL 4.0%, TP 30%).

    Subordinated in score (215+) below all 1H/2H/4H engines so top-of-hour validated setups always take priority,
    while :15/:30/:45 rounds capture clean high-volume 15m breakouts with strict wick, anti-chase, basis & Spot-TB guards.
    """
    hits = []
    if not m15_complete or cur_px is None or cur_px <= 0:
        return hits
    if c15_now is None or c15_prev is None or c15_now <= 0 or c15_prev <= 0:
        return hits
    if hi15_now is None or lo15_now is None or hi15_now <= lo15_now:
        return hits
    if atr14_15m_pct is None or atr14_15m_pct < M15_MIN_ATR_PCT:
        return hits
    if qv15_now is None or qv15_avg20 is None or qv15_avg20 <= 0:
        return hits
    vrat_15m = qv15_now / qv15_avg20
    if vrat_15m < M15_MIN_VRAT:
        return hits
    rng = hi15_now - lo15_now
    lwick = (min(c15_prev, c15_now) - lo15_now) / rng
    uwick = (hi15_now - max(c15_prev, c15_now)) / rng

    # 1. M15_BRK_SHORT: 15m volume-confirmed breakdown below 16-bar support in 15m downtrend
    if (
        (breadth_1d is None or breadth_1d <= M15_SHORT_BD_MAX)
        and (bas8h is None or bas8h >= BASIS_CLEAN_MIN)
        and (f_day is None or f_day >= MIN_EXHAUST_FUNDING_DAY)
        and c15_now < c15_prev
        and ema20_15m is not None
        and sma50_15m is not None
        and c15_now < ema20_15m
        and ema20_15m < sma50_15m
        and cur_px < ema20_15m
        and low16_15m is not None
        and c15_now < low16_15m
        and c15_prev >= low16_15m
        and lwick < M15_MAX_WICK
        and rsi15m_now is not None
        and rsi15m_now <= M15_SHORT_RSI_MAX
        and cur_px >= c15_now * (1.0 - M15_MAX_CHASE)
        and (spot_tb_15m is None or spot_tb_15m < M15_SHORT_STB_MAX)
    ):
        sc = 215.0 + min(vrat_15m - M15_MIN_VRAT, 4.0) * 1.2 + (M15_SHORT_RSI_MAX - rsi15m_now) * 0.1
        hits.append({"side": "SHORT", "setup": "M15_BRK_SHORT", "score": round(sc, 2), "stop_pct": 0.040, "tp_pct": 0.300, "exit_mode": DEFAULT_EXIT_MODE})

    # 2. M15_BRK_LONG: 15m volume-confirmed breakout above 16-bar resistance
    if (
        (bas8h is None or bas8h <= LSPLWICK1H_BAS8H_MAX)
        and (f_day is None or f_day <= LONG_MAX_FUNDING_DAY)
        and c15_now > c15_prev
        and ema20_15m is not None
        and c15_now > ema20_15m
        and cur_px > ema20_15m
        and res16_15m is not None
        and c15_now > res16_15m
        and c15_prev <= res16_15m
        and uwick < M15_MAX_WICK
        and rsi15m_now is not None
        and M15_LONG_RSI_MIN <= rsi15m_now <= M15_LONG_RSI_MAX
        and cur_px <= c15_now * (1.0 + M15_MAX_CHASE)
        and (spot_tb_15m is None or spot_tb_15m >= M15_LONG_STB_MIN)
    ):
        sc = 215.0 + min(vrat_15m - M15_MIN_VRAT, 4.0) * 1.2 + (rsi15m_now - M15_LONG_RSI_MIN) * 0.1
        hits.append({"side": "LONG", "setup": "M15_BRK_LONG", "score": round(sc, 2), "stop_pct": 0.040, "tp_pct": 0.300, "exit_mode": LONG_EXIT_MODE})

    hits.sort(key=lambda h: -h["score"])
    return hits


def evaluate_v23_ema_rsi_mood(
    *,
    m15_complete=False,
    cur_px=None,
    c15_now=None,
    c15_prev=None,
    hi15_now=None,
    lo15_now=None,
    qv15_now=None,
    qv15_avg20=None,
    ema9_15m=None,
    ema21_15m=None,
    sma50_15m=None,
    rsi15m_now=None,
    low16_15m=None,
    res16_15m=None,
    ema9_1h=None,
    ema21_1h=None,
    rsi1h_now=None,
    atr14_1h_pct=None,
    ret_24h=None,
    btc_ret24=None,
    f_day=0.0,
):
    """V23 Active EMA + RSI Trend-Following Dual-Side Engines (`EMA_RSI_SHORT` & `EMA_RSI_LONG`, ATR SL 3.5-6.5%, TP 30%).

    Upgraded with:
      1. Anti-extended guard (<= 2.5% from 15m EMA21) + Funding guard (SHORT >= -0.15%/d, LONG <= +0.10%/d).
      2. Pullback-to-EMA9/21 touch OR fresh 16-bar breakout trigger + Relative Strength vs BTC (`ret_24h - btc_ret24`) ranking.
      3. Volatility-adjusted SL = clip(2.5 * ATR14_1H, 3.5%, 6.5%), still under Fast-Guard 5s & R0 limits.
    """
    hits = []
    if not m15_complete or cur_px is None or cur_px <= 0:
        return hits
    if c15_now is None or c15_prev is None or c15_now <= 0 or c15_prev <= 0:
        return hits
    if qv15_now is None or qv15_avg20 is None or qv15_avg20 <= 0:
        return hits
    vrat_15m = qv15_now / qv15_avg20
    if vrat_15m < EMA_RSI_MIN_VRAT:
        return hits
    if any(v is None for v in (ema9_15m, ema21_15m, sma50_15m, rsi15m_now, ema9_1h, ema21_1h, rsi1h_now)):
        return hits
    if ema21_15m <= 0:
        return hits

    if atr14_1h_pct is not None and atr14_1h_pct > 0:
        sl_pct = round(min(max(atr14_1h_pct * EMA_RSI_SL_ATR_MULT, EMA_RSI_SL_MIN), EMA_RSI_SL_MAX), 4)
    else:
        sl_pct = EMA_RSI_SL_DEFAULT
    rs_24h = (ret_24h - btc_ret24) if (ret_24h is not None and btc_ret24 is not None) else (ret_24h or 0.0)

    # 1. EMA_RSI_SHORT: 15m+1H bearish EMA stack + anti-extended + funding ok + (EMA9 pullback OR fresh 16-bar breakdown)
    short_pullback = (hi15_now is not None and hi15_now >= ema9_15m * (1.0 - EMA_RSI_PULLBACK_TOL))
    short_fresh_brk = (low16_15m is not None and c15_now < low16_15m and c15_prev >= low16_15m)
    short_trigger_ok = short_pullback or short_fresh_brk
    if (
        c15_now < c15_prev
        and cur_px < ema9_15m < ema21_15m < sma50_15m
        and cur_px < ema9_1h < ema21_1h
        and cur_px >= ema21_15m * (1.0 - EMA_RSI_MAX_EXT_EMA21)
        and (f_day is None or f_day >= EMA_RSI_SHORT_MIN_FUND)
        and short_trigger_ok
        and EMA_RSI_SHORT_RSI15_MIN <= rsi15m_now <= EMA_RSI_SHORT_RSI15_MAX
        and EMA_RSI_SHORT_RSI1H_MIN <= rsi1h_now <= EMA_RSI_SHORT_RSI1H_MAX
        and (ret_24h is None or ret_24h <= EMA_RSI_SHORT_RET24_MAX)
        and cur_px >= c15_now * (1.0 - EMA_RSI_MAX_CHASE)
    ):
        rs_bonus = min(max(-rs_24h, 0.0) * 60.0, 12.0)
        trig = "PULLBACK" if short_pullback else "BREAKOUT"
        sc = (
            212.0
            + rs_bonus
            + (EMA_RSI_SHORT_RSI15_MAX - rsi15m_now) * 0.25
            + (EMA_RSI_SHORT_RSI1H_MAX - rsi1h_now) * 0.15
            + min(vrat_15m, 5.0) * 1.5
        )
        hits.append({
            "side": "SHORT",
            "setup": "EMA_RSI_SHORT",
            "trigger": trig,
            "score": round(sc, 2),
            "stop_pct": sl_pct,
            "tp_pct": 0.300,
            "exit_mode": DEFAULT_EXIT_MODE,
        })

    # 2. EMA_RSI_LONG: 15m+1H bullish EMA stack + anti-extended + funding ok + (EMA9 pullback OR fresh 16-bar breakout)
    long_pullback = (lo15_now is not None and lo15_now <= ema9_15m * (1.0 + EMA_RSI_PULLBACK_TOL))
    long_fresh_brk = (res16_15m is not None and c15_now > res16_15m and c15_prev <= res16_15m)
    long_trigger_ok = long_pullback or long_fresh_brk
    if (
        c15_now > c15_prev
        and cur_px > ema9_15m > ema21_15m > sma50_15m
        and cur_px > ema9_1h > ema21_1h
        and cur_px <= ema21_15m * (1.0 + EMA_RSI_MAX_EXT_EMA21)
        and (f_day is None or f_day <= EMA_RSI_LONG_MAX_FUND)
        and long_trigger_ok
        and EMA_RSI_LONG_RSI15_MIN <= rsi15m_now <= EMA_RSI_LONG_RSI15_MAX
        and EMA_RSI_LONG_RSI1H_MIN <= rsi1h_now <= EMA_RSI_LONG_RSI1H_MAX
        and (ret_24h is None or ret_24h >= EMA_RSI_LONG_RET24_MIN)
        and cur_px <= c15_now * (1.0 + EMA_RSI_MAX_CHASE)
    ):
        rs_bonus = min(max(rs_24h, 0.0) * 60.0, 12.0)
        trig = "PULLBACK" if long_pullback else "BREAKOUT"
        sc = (
            212.0
            + rs_bonus
            + (rsi15m_now - EMA_RSI_LONG_RSI15_MIN) * 0.25
            + (rsi1h_now - EMA_RSI_LONG_RSI1H_MIN) * 0.15
            + min(vrat_15m, 5.0) * 1.5
        )
        hits.append({
            "side": "LONG",
            "setup": "EMA_RSI_LONG",
            "trigger": trig,
            "score": round(sc, 2),
            "stop_pct": sl_pct,
            "tp_pct": 0.300,
            "exit_mode": LONG_EXIT_MODE,
        })

    hits.sort(key=lambda h: -h["score"])
    return hits


def is_active_squeeze(
    btc_ret24,
    btc_1h_ret1,
    btc_1h_ret4,
    pct_above_1h_ema20,
    btc_15m_ret1=None,
):
    """Return True when an active BTC velocity or 1H altcoin-breadth squeeze blocks ALL SHORT opens (incl. Bull-regime moat)."""
    return bool(
        (btc_ret24 is not None and btc_ret24 >= 0.04)
        or (btc_1h_ret1 is not None and btc_1h_ret1 >= 0.02)
        or (btc_1h_ret4 is not None and btc_1h_ret4 >= 0.035)
        or (btc_15m_ret1 is not None and btc_15m_ret1 >= 0.012)
        or (pct_above_1h_ema20 is not None and pct_above_1h_ema20 > 0.72)
    )


def is_active_crash(
    btc_ret24,
    btc_1h_ret1,
    btc_1h_ret4,
    pct_above_1h_ema20,
    btc_15m_ret1=None,
):
    """Return True when an active BTC downward crash or panic waterfall blocks ALL LONG opens."""
    return bool(
        (btc_ret24 is not None and btc_ret24 <= -0.05)
        or (btc_1h_ret1 is not None and btc_1h_ret1 <= -0.02)
        or (btc_1h_ret4 is not None and btc_1h_ret4 <= -0.035)
        or (btc_15m_ret1 is not None and btc_15m_ret1 <= -0.012)
        or (pct_above_1h_ema20 is not None and pct_above_1h_ema20 < 0.10)
    )


def classify_regime(
    btc_px,
    btc_sma20_d,
    btc_ret24,
    btc_1h_ret1,
    btc_1h_ret4,
    pct_above_d_sma20,
    pct_above_1h_ema20,
    btc_15m_ret1=None,
    btc_15m_ret4=None,
    pct_above_15m_ema20=None,
    btc_1h_ema20=None,
):
    """Classify market regime for SHORT-only entries.

    - BULLISH_SQUEEZE_CAUTION: short squeeze / extreme breadth (blocks ALL opens)
    - BULL_TREND_CAUTION: daily breadth > 50% (blocks NEW7/PARA75/YOUNG_1D; allows PUMP_MONSTER_1H only)
    - BEARISH_FAVORABLE: BTC < 1D SMA20 & <= 1H EMA20, low 1D/1H/15m breadth
    - NEUTRAL_SELECTIVE: breadth_1d <= 50% without squeeze
    """
    if (
        (btc_ret24 is not None and btc_ret24 >= 0.04)
        or (btc_1h_ret1 is not None and btc_1h_ret1 >= 0.02)
        or (btc_1h_ret4 is not None and btc_1h_ret4 >= 0.035)
        or (btc_15m_ret1 is not None and btc_15m_ret1 >= 0.012)
        or pct_above_d_sma20 > 0.65
        or pct_above_1h_ema20 > 0.72
    ):
        return "BULLISH_SQUEEZE_CAUTION"
    if pct_above_d_sma20 > BULL_BREADTH_MAX:
        return "BULL_TREND_CAUTION"
    if (
        btc_sma20_d is not None
        and btc_px < btc_sma20_d
        and pct_above_d_sma20 < 0.45
        and pct_above_1h_ema20 < 0.50
        and (pct_above_15m_ema20 is None or pct_above_15m_ema20 < 0.55)
        and (btc_1h_ema20 is None or btc_px <= btc_1h_ema20 * 1.005)
    ):
        return "BEARISH_FAVORABLE"
    return "NEUTRAL_SELECTIVE"


def _classify_1h_shortlist_tier(age_p, h_op_p, h_hi_p, h_lo_p, h_cl_p, h_qv_p, h_tb_p):
    """Classify a red completed 1H bar for Spot 15m/1H REST shortlist patching.

    Returns:
      1: Tier-1/2 core 1H engines (NEW7, DCAT_WICK, PARA75, BEAR_BRK, BASOVH, WHALESP, SPOTDMP_1H)
      2: V15 SPOTFLOW_POS_1H continuation candidate (below EMA10, ext >= -4%, tb < 0.42, lwick < 0.30)
      0: Not shortlisted
    """
    if age_p <= 7.0 and h_tb_p[-1] < 0.48:
        return 1
    if h_tb_p[-1] >= 0.46:
        return 0
    rng_p = h_hi_p[-1] - h_lo_p[-1]
    uwick_p = ((h_hi_p[-1] - max(h_op_p[-1], h_cl_p[-1])) / rng_p) if rng_p > 0 else 0.0
    lwick_p = ((min(h_op_p[-1], h_cl_p[-1]) - h_lo_p[-1]) / rng_p) if rng_p > 0 else 1.0
    qv20_p = (sum(h_qv_p[-21:-1]) / 20.0) if len(h_qv_p) >= 21 else 0.0
    if uwick_p >= 0.30 and qv20_p > 0 and (h_qv_p[-1] / qv20_p) >= 1.5:
        return 1
    if len(h_cl_p) >= 169 and h_cl_p[-169] > 0 and (h_cl_p[-1] / h_cl_p[-169] - 1.0) >= 0.75:
        return 1
    if (
        h_tb_p[-1] < BEARBRK1H_TB_MAX
        and lwick_p < BEARBRK1H_LWICK_MAX
        and qv20_p > 0
        and (h_qv_p[-1] / qv20_p) >= BEARBRK1H_VRAT_MIN
        and len(h_lo_p) >= 17
        and h_cl_p[-1] < min(h_lo_p[-17:-1])
        and h_cl_p[-2] >= min(h_lo_p[-17:-1])
    ):
        return 1
    if h_tb_p[-1] < WHALESP1H_PTB_MAX and lwick_p < WHALESP1H_LWICK_MAX:
        e1_p = market.ema(h_cl_p, 10)
        e1_pp = market.ema(h_cl_p[:-1], 10)
        if e1_p is not None and e1_pp is not None and h_cl_p[-1] < e1_p and h_cl_p[-2] >= e1_pp:
            return 1
        if (
            e1_p is not None
            and e1_p > 0
            and h_cl_p[-1] < e1_p
            and (h_cl_p[-1] / e1_p - 1.0) >= SPOTFLOW1H_EXT_E10_MIN
            and h_tb_p[-1] < SPOTFLOW1H_PTB_MAX
            and lwick_p < SPOTFLOW1H_LWICK_MAX
        ):
            return 2
    return 0


def _fetch_funding_with_cache(now_utc):
    """Fetch Binance funding map via `market.perp_funding()` with 1 transient retry and <=30m cache fallback."""
    fund = {}
    for attempt in range(2):
        with contextlib.suppress(Exception):
            res = market.perp_funding()
            if isinstance(res, dict) and res:
                fund = res
                break
        if attempt == 0:
            time.sleep(0.5)
    if fund:
        with contextlib.suppress(Exception):
            write_json_atomic(FUNDING_CACHE_PATH, {
                "updated_at": now_utc.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
                "funding": fund,
            })
        return fund
    cached = read_json(FUNDING_CACHE_PATH, default=None)
    if isinstance(cached, dict) and isinstance(cached.get("funding"), dict) and cached["funding"]:
        with contextlib.suppress(ValueError, TypeError):
            ts_c = parse_iso_utc(cached["updated_at"])
            if 0 <= (now_utc.astimezone(dt.timezone.utc) - ts_c).total_seconds() <= FUNDING_CACHE_MAX_AGE_SEC:
                return cached["funding"]
    return {}


def _scan_market_and_candidates(pf, now_utc, held_bars_15m=None):
    # V16.2/V21: parallelize 3 independent Binance/funding metadata fetches with funding retry+30m cache fallback
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as boot_ex:
        f_perps = boot_ex.submit(fetch_usdt_perps_with_onboard)
        f_fund = boot_ex.submit(_fetch_funding_with_cache, now_utc)
        f_tk = boot_ex.submit(_http_json, FAPI_TICKER_24H, 20)
        perps, onboard_ms = f_perps.result()
        funding_day = f_fund.result()
        tickers_raw = f_tk.result()
    ticker_map = {x["symbol"]: x for x in tickers_raw if isinstance(x, dict) and x.get("symbol")}

    held = set(pf.get("held_symbols") or ()) | {x["symbol"] for x in pf.get("shorts") or [] if x.get("on_binance")}
    liquid_syms = {"BTCUSDT"}
    for sym in perps:
        if sym in market.BLACKLIST_PERPS or sym in market.GOLD_PERPS or sym == "BTCUSDT":
            continue
        t = ticker_map.get(sym)
        if not t:
            continue
        if float(t.get("quoteVolume") or 0.0) >= MIN_VOL_24H_USD:
            liquid_syms.add(sym)

    query_syms = liquid_syms | {"ETHUSDT"} | {s for s in held if s and s != "BTCUSDT"}
    spot_15m = {}
    spot_24h_qv = {}
    spot_ok = True
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        f_daily = ex.submit(market.load_perp_daily_ohlcv, query_syms, 65)
        f_hourly = ex.submit(load_perp_hourly_flow, query_syms, 12)
        f_m15 = ex.submit(load_perp_15m, query_syms, 28)
        f_spot15 = ex.submit(load_spot_15m, query_syms, 108)
        f_spot24 = ex.submit(fetch_spot_24h_qv)
        daily_ohlcv, _ = f_daily.result()
        hourly_flow, _ = f_hourly.result()
        m15_ohlcv, _ = f_m15.result()
        try:
            spot_15m = f_spot15.result()
        except Exception:  # noqa: BLE001
            spot_ok = False
        with contextlib.suppress(Exception):
            spot_24h_qv = f_spot24.result()
    spot_live_syms = {s for s, q in spot_24h_qv.items() if q >= MIN_SPOT_VOL_24H_USD}

    now_u = now_utc.astimezone(dt.timezone.utc)
    now_ms = int(now_u.timestamp() * 1000)
    target_hour_dt = now_u.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=1)
    target_hour_str = f"{target_hour_dt:%Y-%m-%d %H}:00:00"
    target_1h_last_15m_str = f"{(target_hour_dt + dt.timedelta(minutes=45)):%Y-%m-%d %H:%M}:00"
    cur_4h_dt = now_u.replace(hour=(now_u.hour // 4) * 4, minute=0, second=0, microsecond=0)
    target_4h_dt = cur_4h_dt - dt.timedelta(hours=4)
    target_4h_str = f"{target_4h_dt:%Y-%m-%d %H}:00:00"
    target_4h_last_15m_str = f"{(cur_4h_dt - dt.timedelta(minutes=15)):%Y-%m-%d %H:%M}:00"
    is_fresh_4h_window = (now_u.hour % 4 == 0)
    cur_2h_dt = now_u.replace(hour=(now_u.hour // 2) * 2, minute=0, second=0, microsecond=0)
    target_2h_dt = cur_2h_dt - dt.timedelta(hours=2)
    target_2h_str = f"{target_2h_dt:%Y-%m-%d %H}:00:00"
    target_2h_last_15m_str = f"{(cur_2h_dt - dt.timedelta(minutes=15)):%Y-%m-%d %H:%M}:00"
    is_fresh_2h_window = (now_u.hour % 2 == 0)
    cur_15m_dt = now_u.replace(minute=(now_u.minute // 15) * 15, second=0, microsecond=0)
    target_15m_dt = cur_15m_dt - dt.timedelta(minutes=15)
    target_15m_str = f"{target_15m_dt:%Y-%m-%d %H:%M}:00"

    # Zero-lag Binance REST 1H patch when ClickHouse collector hasn't finished ingesting the just-closed hour
    patched_1h = _patch_live_1h_from_binance(hourly_flow, query_syms, target_hour_str, now_utc)
    patched_spot_15m = 0
    spot_flow_4h_fb = {}
    spot_flow_2h_fb = {}
    spot_flow_1h_fb = {}
    # V12.1/V16 Fix: run Spot REST shortlist patching even if ClickHouse spot_15m fails (`spot_ok=False`) & cover 4H/2H/1H
    if spot_ok or spot_live_syms:
        shortlist_4h = []
        shortlist_2h = []
        shortlist_1h = []
        shortlist_1h_sfp = []
        for sym in sorted(liquid_syms):
            if sym == "BTCUSDT" or sym in held or (not spot_15m.get(sym) and sym not in spot_live_syms):
                continue
            h_rec = hourly_flow.get(sym)
            if not h_rec or len(h_rec[0]) < 22:
                continue
            # Apply same segment_start cut as Pass 1 so relisted/redenominated symbols never distort EMA10
            d_rec_pre = daily_ohlcv.get(sym)
            start_iso_pre = None
            if d_rec_pre and d_rec_pre[0]:
                dd_p, _, _, _, dc_p, _, _ = d_rec_pre
                sa_p = market.segment_start(dd_p, dc_p)
                if sa_p < len(dd_p):
                    start_iso_pre = dd_p[sa_p].isoformat()
            hi_p = [i for i, h in enumerate(h_rec[0]) if (not start_iso_pre or h[:10] >= start_iso_pre)]
            if len(hi_p) < 22:
                continue
            h_hrs_p = [h_rec[0][i] for i in hi_p]
            h_op_p = [h_rec[1][i] for i in hi_p]
            h_hi_p = [h_rec[2][i] for i in hi_p]
            h_lo_p = [h_rec[3][i] for i in hi_p]
            h_cl_p = [h_rec[4][i] for i in hi_p]
            h_qv_p = [h_rec[5][i] for i in hi_p]
            h_tb_p = [h_rec[6][i] for i in hi_p]
            h_cnt_p = [h_rec[7][i] for i in hi_p]
            vol_sort = float((ticker_map.get(sym) or {}).get("quoteVolume") or 0.0)

            # 1) 4H shortlist (BASCRUSH_4H, POST30D_4H, SP_LEAD_4H, SPOTDMP_4H, WHALESP_4H, SPOTFLOW_POS_4H) — Priority #1
            if is_fresh_4h_window and len(h_hrs_p) >= 88:
                h4_tmp = _hourly_to_4h_flow(h_hrs_p, h_op_p, h_hi_p, h_lo_p, h_cl_p, h_qv_p, h_tb_p, h_cnt_p)
                if len(h4_tmp) >= 22 and h4_tmp[-1][0] == target_4h_str and h4_tmp[-1][7] == 16:
                    c4 = [r[4] for r in h4_tmp]
                    e4 = market.ema(c4, 10)
                    e4p = market.ema(c4[:-1], 10)
                    rng4 = h4_tmp[-1][2] - h4_tmp[-1][3]
                    lw4 = ((min(h4_tmp[-1][1], h4_tmp[-1][4]) - h4_tmp[-1][3]) / rng4) if rng4 > 0 else 1.0
                    is_fresh4 = (e4 is not None and e4p is not None and c4[-1] < e4 and c4[-2] >= e4p)
                    is_sfp4 = (
                        e4 is not None and e4 > 0 and c4[-1] < e4
                        and (c4[-1] / e4 - 1.0) >= SPOTFLOW2H_EXT_E10_MIN
                        and lw4 < SPOTFLOW2H_LWICK_MAX and h4_tmp[-1][6] < SPOTFLOW2H_PTB_MAX
                    )
                    if h4_tmp[-1][4] < h4_tmp[-1][1] and (
                        (is_fresh4 and (h4_tmp[-1][6] < SPOTDMP_PTB_MAX or lw4 < SPLEAD4H_LWICK_MAX))
                        or is_sfp4
                    ):
                        shortlist_4h.append((-vol_sort, sym))
            # 2) 2H shortlist (V16 2H Moat + V17 BLOWOFF_2H + V19 EMAREJ_2H) — Parity: >= 22 2H bars
            if is_fresh_2h_window and len(h_hrs_p) >= 44:
                h2_tmp = _hourly_to_2h_flow(h_hrs_p, h_op_p, h_hi_p, h_lo_p, h_cl_p, h_qv_p, h_tb_p, h_cnt_p)
                if len(h2_tmp) >= 22 and h2_tmp[-1][0] == target_2h_str and h2_tmp[-1][7] == 8 and h2_tmp[-1][4] < h2_tmp[-1][1]:
                    c2 = [r[4] for r in h2_tmp]
                    e2 = market.ema(c2, 10)
                    e2p = market.ema(c2[:-1], 10)
                    e2_20 = market.ema(c2, 20)
                    rng2 = h2_tmp[-1][2] - h2_tmp[-1][3]
                    lw2 = ((min(h2_tmp[-1][1], h2_tmp[-1][4]) - h2_tmp[-1][3]) / rng2) if rng2 > 0 else 1.0
                    uw2 = ((h2_tmp[-1][2] - max(h2_tmp[-1][1], h2_tmp[-1][4])) / rng2) if rng2 > 0 else 0.0
                    is_fresh2 = (e2 is not None and e2p is not None and c2[-1] < e2 and c2[-2] >= e2p and lw2 < SPOTDMP2H_LWICK_MAX)
                    is_sfp2 = (
                        e2 is not None and e2 > 0 and c2[-1] < e2
                        and (c2[-1] / e2 - 1.0) >= SPOTFLOW2H_EXT_E10_MIN
                        and lw2 < SPOTFLOW2H_LWICK_MAX and h2_tmp[-1][6] < SPOTFLOW2H_PTB_MAX
                    )
                    o2_prev, c2_prev = h2_tmp[-2][1], h2_tmp[-2][4]
                    is_blow2 = (
                        o2_prev > 0 and c2_prev > o2_prev
                        and (c2_prev / o2_prev - 1.0) >= BLOWOFF2H_PREV_RET_MIN
                        and c2[-1] < (o2_prev + c2_prev) * 0.5
                        and h2_tmp[-1][6] < BLOWOFF2H_TB_MAX
                        and lw2 < BLOWOFF2H_LWICK_MAX
                    )
                    is_emarej2 = (
                        e2 is not None and e2_20 is not None and e2p is not None
                        and c2[-1] < e2 and e2 < e2_20 and c2[-2] < e2p
                        and h2_tmp[-1][2] >= e2 * EMAREJ2H_KISS_RATIO
                        and uw2 >= EMAREJ2H_UWICK_MIN and lw2 < EMAREJ2H_LWICK_MAX
                        and h2_tmp[-1][6] < EMAREJ2H_TB_MAX
                    )
                    is_spuw2 = (
                        e2 is not None and c2[-1] < e2
                        and lw2 < SPUW2H_LWICK_MAX and h2_tmp[-1][6] < SPUW2H_PTB_MAX
                    )
                    if is_fresh2 or is_sfp2 or is_blow2 or is_emarej2 or is_spuw2:
                        shortlist_2h.append((-vol_sort, sym))
            # 3) 1H shortlist (SHORT red bars + V22 LONG green bars with strong taker buy)
            if h_hrs_p[-1] == target_hour_str and h_cnt_p[-1] == 4:
                if h_cl_p[-1] < h_op_p[-1]:
                    ob_p = onboard_ms.get(sym)
                    age_p = ((now_ms - ob_p) / 86_400_000.0) if ob_p else (len(h_cl_p) / 24.0)
                    tier_1h = _classify_1h_shortlist_tier(age_p, h_op_p, h_hi_p, h_lo_p, h_cl_p, h_qv_p, h_tb_p)
                    if tier_1h == 1:
                        shortlist_1h.append((-vol_sort, sym))
                    elif tier_1h == 2:
                        shortlist_1h_sfp.append((-vol_sort, sym))
                elif h_cl_p[-1] > h_op_p[-1] and h_tb_p[-1] >= LSPLWICK1H_PTB_MIN:
                    rng_lp = h_hi_p[-1] - h_lo_p[-1]
                    uw_lp = ((h_hi_p[-1] - max(h_op_p[-1], h_cl_p[-1])) / rng_lp) if rng_lp > 0 else 1.0
                    e1_lp = market.ema(h_cl_p, 10)
                    if uw_lp < LSQZ1H_UWICK_MAX and e1_lp is not None and e1_lp > 0 and h_cl_p[-1] > e1_lp and (h_cl_p[-1] / e1_lp - 1.0) <= LSQZ1H_EXT_E10_MAX:
                        shortlist_1h.append((-vol_sort, sym))
        shortlist_4h.sort()
        shortlist_2h.sort()
        shortlist_1h.sort()
        shortlist_1h_sfp.sort()
        s4_syms = [s for _, s in shortlist_4h]
        s2_syms = [s for _, s in shortlist_2h]
        s1_core_syms = [s for _, s in shortlist_1h]
        s1_sfp_syms = [s for _, s in shortlist_1h_sfp]
        s1_syms = s1_core_syms + s1_sfp_syms
        # V21 Fix: prioritize 4H, Tier-1 1H, and 2H ahead of broad Tier-2 1H SFP so 2H/4H engines are never starved
        shortlist_basis = list(dict.fromkeys(s4_syms + s1_core_syms + s2_syms + s1_sfp_syms))[:200]
        if shortlist_basis:
            patched_spot_15m = _patch_shortlist_15m_from_binance(
                m15_ohlcv,
                spot_15m,
                shortlist_basis,
                target_1h_last_15m_str,
                now_utc,
                window_start_str=(target_4h_str if is_fresh_4h_window else (target_2h_str if is_fresh_2h_window else target_hour_str)),
                spot_live_syms=spot_live_syms,
                check_12h_gap=is_fresh_4h_window,
            )
            # V16.1/V16.2: run 4H/2H/1H Spot REST fallbacks IN PARALLEL whenever CH spot_15m lacks 21-bar history or TB data
            need_4h_fb = []
            if is_fresh_4h_window and s4_syms:
                for s in s4_syms[:90]:
                    if s in spot_live_syms or spot_15m.get(s):
                        f4_chk = _spot_15m_to_4h_flow(spot_15m.get(s), target_4h_str, end_ts_str=target_4h_last_15m_str)
                        if f4_chk.get("spot_tb_4h_flow") is None or f4_chk.get("spot_vrat_4h") is None or f4_chk.get("spot_tsize_r_4h") is None:
                            need_4h_fb.append(s)
            need_2h_fb = []
            if is_fresh_2h_window and s2_syms:
                for s in s2_syms[:100]:
                    if s in spot_live_syms or spot_15m.get(s):
                        f2_chk = _spot_15m_to_2h_flow(spot_15m.get(s), target_2h_str, end_ts_str=target_2h_last_15m_str)
                        if (
                            f2_chk.get("spot_tb_2h") is None
                            or f2_chk.get("spot_vrat_2h") is None
                            or f2_chk.get("spot_tsize_r_2h") is None
                            or f2_chk.get("spot_uwick_2h") is None
                        ):
                            need_2h_fb.append(s)
            need_1h_fb = []
            if s1_syms:
                for s in s1_syms[:140]:
                    if s in spot_live_syms or spot_15m.get(s):
                        f1_chk = _spot_15m_to_1h_flow(spot_15m.get(s), target_hour_str, end_ts_str=target_1h_last_15m_str)
                        if (
                            f1_chk.get("spot_tb_1h") is None
                            or f1_chk.get("spot_vrat_1h") is None
                            or f1_chk.get("spot_tsize_r_1h") is None
                            or f1_chk.get("spot_uwick_1h") is None
                        ):
                            need_1h_fb.append(s)
            fb_jobs = []
            if need_4h_fb:
                fb_jobs.append(("4h", need_4h_fb, target_4h_str))
            if need_2h_fb:
                fb_jobs.append(("2h", need_2h_fb, target_2h_str))
            if need_1h_fb:
                fb_jobs.append(("1h", need_1h_fb, target_hour_str))
            if len(fb_jobs) == 1:
                iv, syms_fb, tgt_fb = fb_jobs[0]
                res_fb = _fetch_spot_flow_rest_fallback(syms_fb, iv, tgt_fb, now_utc)
                if iv == "4h":
                    spot_flow_4h_fb = res_fb
                elif iv == "2h":
                    spot_flow_2h_fb = res_fb
                else:
                    spot_flow_1h_fb = res_fb
            elif len(fb_jobs) > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(fb_jobs)) as fb_ex:
                    futs = {
                        iv: fb_ex.submit(_fetch_spot_flow_rest_fallback, syms_fb, iv, tgt_fb, now_utc)
                        for iv, syms_fb, tgt_fb in fb_jobs
                    }
                    if "4h" in futs:
                        spot_flow_4h_fb = futs["4h"].result()
                    if "2h" in futs:
                        spot_flow_2h_fb = futs["2h"].result()
                    if "1h" in futs:
                        spot_flow_1h_fb = futs["1h"].result()

    # BTC regime
    btc_d = daily_ohlcv.get("BTCUSDT")
    btc_h = hourly_flow.get("BTCUSDT")
    btc_m15 = m15_ohlcv.get("BTCUSDT")
    btc_tk = ticker_map.get("BTCUSDT") or {}
    btc_px = float(
        btc_tk.get("lastPrice")
        or (btc_m15[1][-1] if btc_m15 and btc_m15[1] else (btc_h[4][-1] if btc_h and btc_h[4] else 0.0))
    )
    btc_ret14 = btc_ret7 = btc_sma20_d = None
    if btc_d and len(btc_d[4]) >= 20:
        bd_dates = list(btc_d[0])
        bc = list(btc_d[4])
        # V13 Fix: synthesize BTCUSDT yesterday's UTC daily close at 00:00 UTC from patched 24/24 hourly_flow (same as altcoins)
        yday_btc_date = now_u.date() - dt.timedelta(days=1)
        yday_btc_iso = yday_btc_date.isoformat()
        if bd_dates and bd_dates[-1] < yday_btc_date and btc_h and len(btc_h[0]) >= 24:
            y_btc_rows = [(h_s, cl_f, cnt_i) for h_s, cl_f, cnt_i in zip(btc_h[0], btc_h[4], btc_h[7]) if h_s[:10] == yday_btc_iso and cnt_i == 4]
            if len(y_btc_rows) == 24 and y_btc_rows[-1][0] == f"{yday_btc_iso} 23:00:00":
                bd_dates.append(yday_btc_date)
                bc.append(y_btc_rows[-1][1])
        btc_sma20_d = _sma(bc, 20)
        btc_ret7 = (bc[-1] / bc[-8] - 1.0) if len(bc) >= 8 and bc[-8] > 0 else None
        btc_ret14 = (bc[-1] / bc[-15] - 1.0) if len(bc) >= 15 and bc[-15] > 0 else None

    btc_1h_ret1 = btc_1h_ret4 = btc_1h_ema20 = btc_1h_sma50 = None
    btc_complete = False
    if btc_h and len(btc_h[4]) >= 50:
        bh_hrs, _, _, _, bh_cl, _, _, bh_cnt = btc_h
        btc_1h_ema20 = market.ema(bh_cl, 20)
        btc_1h_sma50 = _sma(bh_cl, 50)
        btc_1h_ret1 = (bh_cl[-1] / bh_cl[-2] - 1.0) if bh_cl[-2] > 0 else None
        btc_1h_ret4 = (bh_cl[-1] / bh_cl[-5] - 1.0) if len(bh_cl) >= 5 and bh_cl[-5] > 0 else None
        if len(bh_cl) >= 169 and bh_cl[-169] > 0:
            btc_ret7 = bh_cl[-1] / bh_cl[-169] - 1.0
        btc_complete = bh_hrs[-1] == target_hour_str and bh_cnt[-1] == 4

    btc_15m_ret1 = btc_15m_ret4 = btc_15m_ema20 = btc_15m_sma50 = btc_15m_rsi = None
    btc_15m_complete = False
    if btc_m15 and len(btc_m15[1]) >= 50:
        bm_t, bm_cl, _, _, _ = btc_m15
        btc_15m_ema20 = market.ema(bm_cl, 20)
        btc_15m_sma50 = _sma(bm_cl, 50)
        btc_15m_rsi = market.rsi(bm_cl, 14)
        btc_15m_ret1 = (bm_cl[-1] / bm_cl[-2] - 1.0) if bm_cl[-2] > 0 else None
        btc_15m_ret4 = (bm_cl[-1] / bm_cl[-5] - 1.0) if len(bm_cl) >= 5 and bm_cl[-5] > 0 else None
        btc_15m_complete = bm_t[-1] == target_15m_str
    if not btc_15m_complete:
        with contextlib.suppress(Exception):
            btc_live_15m = fetch_klines_15m("BTCUSDT", limit=65)
            _, btc_ref_scan = _btc_squeeze_returns_from_bars(btc_live_15m, now_utc)
            if btc_ref_scan:
                _save_btc_ref_merged(btc_ref_scan)
            bm_t, bm_cl, _, _ = _live_bars_to_m15_series(btc_live_15m, now_utc)
            if len(bm_cl) >= 50 and bm_t and bm_t[-1] == target_15m_str:
                btc_15m_ema20 = market.ema(bm_cl, 20)
                btc_15m_sma50 = _sma(bm_cl, 50)
                btc_15m_rsi = market.rsi(bm_cl, 14)
                btc_15m_ret1 = (bm_cl[-1] / bm_cl[-2] - 1.0) if bm_cl[-2] > 0 else None
                btc_15m_ret4 = (bm_cl[-1] / bm_cl[-5] - 1.0) if len(bm_cl) >= 5 and bm_cl[-5] > 0 else None
                btc_15m_complete = True
    elif btc_m15 and len(btc_m15[0]) >= 20:
        # V9/V12: always keep agy-btc-ref.json fresh (preserving ema20_1h_map) even when opening from 0 positions
        with contextlib.suppress(Exception):
            ch_btc_bars = [
                (
                    int(dt.datetime.strptime(t_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
                    Decimal(str(cl_f)), Decimal(str(cl_f)), Decimal(str(cl_f)),
                )
                for t_s, cl_f in zip(btc_m15[0][-24:], btc_m15[1][-24:])
            ]
            _, btc_ref_scan = _btc_squeeze_returns_from_bars(ch_btc_bars, now_utc)
            if btc_ref_scan:
                _save_btc_ref_merged(btc_ref_scan)

    # V18: keep ETH 15m reference fresh in agy-btc-ref.json alongside BTC so ETH15_20 fast-guard is never blind
    eth_m15 = m15_ohlcv.get("ETHUSDT")
    eth_15m_complete = bool(eth_m15 and eth_m15[0] and eth_m15[0][-1] == target_15m_str)
    eth_ref_scan = None
    if eth_15m_complete and len(eth_m15[0]) >= 12:
        with contextlib.suppress(Exception):
            ch_eth_bars = [
                (
                    int(dt.datetime.strptime(t_s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
                    Decimal(str(cl_f)), Decimal(str(cl_f)), Decimal(str(cl_f)),
                )
                for t_s, cl_f in zip(eth_m15[0][-16:], eth_m15[1][-16:])
            ]
            _, eth_ref_scan = _eth_15m_ref_from_bars(ch_eth_bars, now_utc)
    else:
        with contextlib.suppress(Exception):
            eth_live_15m = fetch_klines_15m("ETHUSDT", limit=24)
            _, eth_ref_scan = _eth_15m_ref_from_bars(eth_live_15m, now_utc)
    if eth_ref_scan:
        with contextlib.suppress(Exception):
            _save_btc_ref_merged(eth_ref_scan)

    btc_ret24 = float(btc_tk.get("priceChangePercent") or 0.0) / 100.0 if btc_tk else None

    cooled = cooldown_symbols(pf, now_utc) if ENFORCE_OPEN_COOLDOWN else set()
    today_iso = now_u.date().isoformat()
    opened_today = opened_today_utc(pf, today_iso)

    if not funding_day:
        return {
            "regime_label": "DATA_INCOMPLETE_HOLD",
            "active_squeeze": False,
            "active_crash": False,
            "funding_ok": False,
            "spot_ok": spot_ok,
            "strategy": STRATEGY_VERSION,
            "target_1h_bar_utc": target_hour_str,
            "target_2h_bar_utc": target_2h_str,
            "target_4h_bar_utc": target_4h_str,
            "target_15m_bar_utc": target_15m_str,
            "is_fresh_2h_window": is_fresh_2h_window,
            "is_fresh_4h_window": is_fresh_4h_window,
            "btc_1h_complete": btc_complete,
            "btc_15m_complete": btc_15m_complete,
            "complete_1h_pct": 0.0,
            "patched_1h": patched_1h,
            "patched_spot_15m": patched_spot_15m,
            "liquid_universe": 0,
            "btc": {"price": round(btc_px, 2), "funding_day_pct": None},
            "breadth": {
                "pct_above_1d_sma20": None,
                "pct_above_1h_ema20": None,
                "pct_above_15m_ema20": None,
                "pct_24h_positive": None,
                "median_24h_ret_pct": None,
            },
        }, [], sorted(cooled), sorted(opened_today), {}

    # Pass 1: compute live breadth, completeness stats, Perp-Spot basis, and 1H/4H indicators
    n_1d_valid = n_1d_above_sma20 = 0
    n_1h_valid = n_1h_above_ema20 = 0
    n_1h_checked = n_1h_complete = 0
    n_15m_valid = n_15m_above_ema20 = 0
    n_15m_checked = n_15m_complete = 0
    rets_24h = []
    bas8h_vals = []
    held_tech = {}
    prepared = []
    ema20_1h_map = {}
    ema20_1h_map_complete = {}
    yday_date = now_u.date() - dt.timedelta(days=1)
    yday_iso = yday_date.isoformat()

    for sym in sorted(query_syms):
        if sym == "BTCUSDT":
            continue
        is_liquid = sym in liquid_syms
        d_rec = daily_ohlcv.get(sym)
        h_rec = hourly_flow.get(sym)
        m15_rec = m15_ohlcv.get(sym)
        tk = ticker_map.get(sym) or {}
        if not h_rec or len(h_rec[0]) < 16:
            continue

        d_dates, d_cl = [], []
        start_iso = None
        if d_rec and d_rec[0]:
            d_dates, _, _, _, d_cl, _, _ = d_rec
            seg_a = market.segment_start(d_dates, d_cl)
            d_dates, d_cl = list(d_dates[seg_a:]), list(d_cl[seg_a:])
            if len(d_cl) >= 21 and market.is_pegged(list(zip(d_dates, d_cl))):
                continue
            if d_dates:
                start_iso = d_dates[0].isoformat()

        h_idx = [i for i, h in enumerate(h_rec[0]) if (not start_iso or h[:10] >= start_iso)]
        if len(h_idx) < 16:
            continue
        h_hrs = [h_rec[0][i] for i in h_idx]
        h_op = [h_rec[1][i] for i in h_idx]
        h_hi = [h_rec[2][i] for i in h_idx]
        h_lo = [h_rec[3][i] for i in h_idx]
        h_cl = [h_rec[4][i] for i in h_idx]
        h_qv = [h_rec[5][i] for i in h_idx]
        h_tb = [h_rec[6][i] for i in h_idx]
        h_cnt = [h_rec[7][i] for i in h_idx]

        # V12 Fix #3: synthesize yesterday's UTC daily close from patched 24h hourly_flow at 00:00 UTC before CH ingests 23:45
        if d_dates and d_dates[-1] < yday_date:
            yday_rows = [(h, cl, cnt) for h, cl, cnt in zip(h_hrs, h_cl, h_cnt) if h[:10] == yday_iso and cnt == 4]
            if len(yday_rows) == 24 and yday_rows[-1][0] == f"{yday_iso} 23:00:00":
                d_dates.append(yday_date)
                d_cl.append(yday_rows[-1][1])

        # Exact contract age from Binance onboardDate (fallback to ClickHouse first bar)
        ob_ms = onboard_ms.get(sym)
        if ob_ms:
            age_days = max(0.0, (now_ms - ob_ms) / 86_400_000.0)
        elif d_dates:
            age_days = float((now_u.date() - d_dates[0]).days)
        else:
            age_days = len(h_cl) / 24.0

        m15_t = m15_cl = m15_qv = m15_hi = m15_lo = []
        if m15_rec:
            m15_idx = [i for i, t in enumerate(m15_rec[0]) if (not start_iso or t[:10] >= start_iso)]
            m15_t = [m15_rec[0][i] for i in m15_idx]
            m15_cl = [m15_rec[1][i] for i in m15_idx]
            m15_qv = [m15_rec[2][i] for i in m15_idx]
            m15_hi = [m15_rec[3][i] for i in m15_idx]
            m15_lo = [m15_rec[4][i] for i in m15_idx]
        m15_complete = bool(m15_t and m15_t[-1] == target_15m_str)

        cur_px = float(tk.get("lastPrice") or (m15_cl[-1] if m15_cl else h_cl[-1]))
        ret_24h = float(tk.get("priceChangePercent") or 0.0) / 100.0
        vol_24h = float(tk.get("quoteVolume") or 0.0)

        d_sma7 = _sma(d_cl, 7)
        d_sma20 = _sma(d_cl, 20)
        ema10_1h = market.ema(h_cl, 10) if len(h_cl) >= 10 else None
        ema10_1h_prev = market.ema(h_cl[:-1], 10) if len(h_cl) >= 11 else None
        ema20_1h = market.ema(h_cl, 20) if len(h_cl) >= 20 else None
        bar_complete_1h = h_hrs[-1] == target_hour_str and h_cnt[-1] == 4

        (
            ema20_15m, sma50_15m, _rsi15_p, rsi15m_now, atr14_15m_pct,
            low16_15m, _low16_p, res16_15m,
            _c15_n, _c15_p, _hi15_n, _hi15_p,
        ) = _compute_m15_stats(m15_cl, m15_hi, m15_lo, cur_px)

        if is_liquid:
            rets_24h.append(ret_24h)
            n_1h_checked += 1
            if bar_complete_1h:
                n_1h_complete += 1
            n_15m_checked += 1
            if m15_complete:
                n_15m_complete += 1
            if d_sma20 is not None:
                n_1d_valid += 1
                if cur_px > d_sma20:
                    n_1d_above_sma20 += 1
            if ema20_1h is not None:
                n_1h_valid += 1
                ema20_1h_map[sym] = f"{ema20_1h:.10g}"
                if bar_complete_1h:
                    ema20_1h_map_complete[sym] = f"{ema20_1h:.10g}"
                if cur_px > ema20_1h:
                    n_1h_above_ema20 += 1
            if ema20_15m is not None:
                n_15m_valid += 1
                if cur_px > ema20_15m:
                    n_15m_above_ema20 += 1

        if sym in held:
            if not m15_complete and held_bars_15m and sym in held_bars_15m:
                lt, lcl, lhi, llo = _live_bars_to_m15_series(held_bars_15m[sym], now_utc, start_iso=start_iso)
                if lt and (not m15_t or lt[-1] > m15_t[-1]):
                    m15_t, m15_cl, m15_hi, m15_lo = lt, lcl, lhi, llo
                    m15_complete = m15_t[-1] == target_15m_str
                    (
                        ema20_15m, sma50_15m, _rsi15_p, rsi15m_now, atr14_15m_pct,
                        low16_15m, _low16_p, res16_15m,
                        _c15_n, _c15_p, _hi15_n, _hi15_p,
                    ) = _compute_m15_stats(m15_cl, m15_hi, m15_lo, cur_px)
            f_day_h = funding_day.get(sym, 0.0)
            rsi1h_h = market.rsi(h_cl, 14) if len(h_cl) >= 16 else None
            hc4_h = _hourly_to_4h(h_hrs, h_cl)
            ema20_4h_h = market.ema(hc4_h, 20) if len(hc4_h) >= 20 else None
            held_tech[sym] = {
                "price": round(cur_px, 8),
                "ret_24h_pct": round(ret_24h * 100.0, 2),
                "vol_24h_m": round(vol_24h / 1e6, 2),
                "funding_day_pct": round(f_day_h * 100.0, 4),
                "age_days": round(age_days, 1),
                "d_sma20": round(d_sma20, 8) if d_sma20 is not None else None,
                "ema20_4h": round(ema20_4h_h, 8) if ema20_4h_h is not None else None,
                "ema20_1h": round(ema20_1h, 8) if ema20_1h is not None else None,
                "ema20_15m": round(ema20_15m, 8) if ema20_15m is not None else None,
                "res16_15m": round(res16_15m, 8) if res16_15m is not None else None,
                "low16_15m": round(low16_15m, 8) if low16_15m is not None else None,
                "rsi15m": round(rsi15m_now, 1) if rsi15m_now is not None else None,
                "rsi1h": round(rsi1h_h, 1) if rsi1h_h is not None else None,
                "above_ema20_15m": bool(ema20_15m is not None and cur_px > ema20_15m),
                "above_ema20_1h": bool(ema20_1h is not None and cur_px > ema20_1h),
                "above_ema20_4h": bool(ema20_4h_h is not None and cur_px > ema20_4h_h),
                "above_d_sma20": bool(d_sma20 is not None and cur_px > d_sma20),
                "complete_15m": m15_complete,
            }
            continue

        if not is_liquid or sym in cooled or sym in opened_today:
            continue

        # Build completed 2H & 4H flow & Perp-Spot basis metrics (exact parity with V85/V106 backtest: m >= 22 bars)
        h2_rows = _hourly_to_2h_flow(h_hrs, h_op, h_hi, h_lo, h_cl, h_qv, h_tb, h_cnt) if is_fresh_2h_window else []
        bar_complete_2h = bool(h2_rows and h2_rows[-1][0] == target_2h_str and h2_rows[-1][7] == 8)
        o_2h = hi_2h = lo_2h = c_2h = o_2h_prev = c_2h_prev = None
        qv_2h = qv_avg20_2h = qv_2h_prev = qv_avg20_2h_prev = tb_2h = ema10_2h = ema10_2h_prev = ema20_2h = None
        if len(h2_rows) >= 22:
            h2_cl = [r[4] for r in h2_rows]
            o_2h = h2_rows[-1][1]
            hi_2h = h2_rows[-1][2]
            lo_2h = h2_rows[-1][3]
            c_2h = h2_cl[-1]
            o_2h_prev = h2_rows[-2][1]
            c_2h_prev = h2_cl[-2]
            qv_2h = h2_rows[-1][5]
            qv_avg20_2h = (sum(r[5] for r in h2_rows[-21:-1]) / 20.0) if len(h2_rows) >= 21 else None
            qv_2h_prev = h2_rows[-2][5]
            qv_avg20_2h_prev = (sum(r[5] for r in h2_rows[-22:-2]) / 20.0) if len(h2_rows) >= 22 else None
            tb_2h = h2_rows[-1][6]
            ema10_2h = market.ema(h2_cl, 10)
            ema10_2h_prev = market.ema(h2_cl[:-1], 10)
            ema20_2h = market.ema(h2_cl, 20)

        h4_rows = _hourly_to_4h_flow(h_hrs, h_op, h_hi, h_lo, h_cl, h_qv, h_tb, h_cnt)
        bar_complete_4h = bool(h4_rows and h4_rows[-1][0] == target_4h_str and h4_rows[-1][7] == 16)
        o_4h = hi_4h = lo_4h = c_4h = c_4h_prev = qv_4h = qv_avg20_4h = tb_4h = ema10_4h = ema10_4h_prev = None
        if len(h4_rows) >= 22:
            h4_cl = [r[4] for r in h4_rows]
            o_4h = h4_rows[-1][1]
            hi_4h = h4_rows[-1][2]
            lo_4h = h4_rows[-1][3]
            c_4h = h4_cl[-1]
            c_4h_prev = h4_cl[-2]
            qv_4h = h4_rows[-1][5]
            qv_avg20_4h = (sum(r[5] for r in h4_rows[-21:-1]) / 20.0) if len(h4_rows) >= 21 else None
            tb_4h = h4_rows[-1][6]
            ema10_4h = market.ema(h4_cl, 10)
            ema10_4h_prev = market.ema(h4_cl[:-1], 10)

        bas_stats = compute_spot_basis_stats(
            m15_t,
            m15_cl,
            spot_15m.get(sym),
            target_4h_last_15m_str=target_4h_last_15m_str,
            end_ts_str=target_1h_last_15m_str,
            target_4h_start_str=target_4h_str,
            spot_qv_24h_override=spot_24h_qv.get(sym),
        )
        if bas_stats["bas8h"] is not None:
            bas8h_vals.append(bas_stats["bas8h"])
        sp1h_flow = _spot_15m_to_1h_flow(
            spot_15m.get(sym),
            target_hour_str,
            end_ts_str=target_1h_last_15m_str,
        )
        if (
            not sp1h_flow["spot_1h_complete"]
            or sp1h_flow.get("spot_tb_1h") is None
            or sp1h_flow["spot_vrat_1h"] is None
            or sp1h_flow.get("spot_tsize_r_1h") is None
            or sp1h_flow.get("spot_uwick_1h") is None
        ) and sym in spot_flow_1h_fb:
            sp1h_flow = spot_flow_1h_fb[sym]
        sp2h_flow = _spot_15m_to_2h_flow(
            spot_15m.get(sym),
            target_2h_str,
            end_ts_str=target_2h_last_15m_str,
        ) if is_fresh_2h_window else {"spot_2h_complete": False, "spot_tb_2h": None, "spot_vrat_2h": None, "spot_tsize_r_2h": None, "spot_red_2h": None, "spot_uwick_2h": None}
        if is_fresh_2h_window and (
            not sp2h_flow["spot_2h_complete"]
            or sp2h_flow.get("spot_tb_2h") is None
            or sp2h_flow["spot_vrat_2h"] is None
            or sp2h_flow.get("spot_tsize_r_2h") is None
            or sp2h_flow.get("spot_uwick_2h") is None
        ) and sym in spot_flow_2h_fb:
            sp2h_flow = spot_flow_2h_fb[sym]
        sp4h_flow = _spot_15m_to_4h_flow(
            spot_15m.get(sym),
            target_4h_str,
            end_ts_str=target_4h_last_15m_str,
        ) if is_fresh_4h_window else {"spot_4h_complete": False, "spot_tb_4h_flow": None, "spot_vrat_4h": None, "spot_tsize_r_4h": None}
        if is_fresh_4h_window and (
            not sp4h_flow["spot_4h_complete"]
            or sp4h_flow.get("spot_tb_4h_flow") is None
            or sp4h_flow["spot_vrat_4h"] is None
            or sp4h_flow.get("spot_tsize_r_4h") is None
        ) and sym in spot_flow_4h_fb:
            sp4h_flow = spot_flow_4h_fb[sym]

        atr14_1h = _atr(h_hi, h_lo, h_cl, 14)
        atr14_1h_pct = (atr14_1h / h_cl[-1]) if (atr14_1h is not None and h_cl[-1] > 0) else None
        # Exact parity with backtest ret24[96:]: None when < 25 1h bars (do NOT fall back to listing-open 24hr ticker spike!)
        ret_24h_bar = (h_cl[-1] / h_cl[-25] - 1.0) if len(h_cl) >= 25 and h_cl[-25] > 0 else None
        spot_qv_24h = bas_stats["spot_qv_24h"]
        # V12.1 HIGH Fix: prevent false ps_vrat spikes when SPOT_TICKER_24H is down and CH spot_15m has partial (<80/96) bars
        spot_qv_reliable = (spot_24h_qv.get(sym) is not None) or (bas_stats.get("spot_24h_bars", 0) >= 80)
        ps_vrat = (vol_24h / spot_qv_24h) if (bas_stats["has_spot_24h"] and spot_qv_24h > 0 and spot_qv_reliable) else None

        prepared.append({
            "sym": sym,
            "coin": perps[sym],
            "age_days": age_days,
            "bar_complete_1h": bar_complete_1h,
            "bars_1h_count": len(h_cl),
            "bar_complete_4h": bar_complete_4h,
            "cur_px": cur_px,
            "vol_24h": vol_24h,
            "f_day": funding_day.get(sym, 0.0),
            "o_1h": h_op[-1],
            "hi_1h": h_hi[-1],
            "lo_1h": h_lo[-1],
            "c_1h": h_cl[-1],
            "c_1h_prev": h_cl[-2],
            "qv_1h": h_qv[-1],
            "qv_avg20_1h": (sum(h_qv[-21:-1]) / 20.0) if len(h_qv) >= 21 else None,
            "tb_1h": h_tb[-1],
            "ema10_1h": ema10_1h,
            "ema10_1h_prev": ema10_1h_prev,
            "ema20_1h": ema20_1h,
            "lo16_1h_prev": min(h_lo[-17:-1]) if len(h_lo) >= 17 else None,
            "hi16_1h_prev": max(h_hi[-17:-1]) if len(h_hi) >= 17 else None,
            "atr14_1h_pct": atr14_1h_pct,
            "rsi1h_now": market.rsi(h_cl, 14) if len(h_cl) >= 16 else None,
            "hi24": max(h_hi[-24:]) if len(h_hi) >= 25 else None,
            "hi7d": max(h_hi[-min(168, len(h_hi)):]) if len(h_hi) >= 168 else None,
            "ret_4h": (h_cl[-1] / h_cl[-5] - 1.0) if len(h_cl) >= 5 and h_cl[-5] > 0 else None,
            "ret_12h": (h_cl[-1] / h_cl[-13] - 1.0) if len(h_cl) >= 13 and h_cl[-13] > 0 else None,
            "ret_24h": ret_24h_bar,
            "ret_24h_display": ret_24h_bar if ret_24h_bar is not None else ret_24h,
            "ret_7d": (h_cl[-1] / h_cl[-169] - 1.0) if len(h_cl) >= 169 and h_cl[-169] > 0 else None,
            "d_sma20": d_sma20,
            "has_spot_24h": bas_stats["has_spot_24h"],
            "bas15": bas_stats["bas15"],
            "bas1h": bas_stats["bas1h"],
            "bas8h": bas_stats["bas8h"],
            "bas12h_max": bas_stats["bas12h_max"],
            "bas_4h_close": bas_stats["bas_4h_close"],
            # V11 Fix #1: prefer exact 16-bar complete `sp4h_flow` over partial perp-aligned `bas_stats["spot_tb_4h"]`
            "spot_tb_4h": sp4h_flow["spot_tb_4h_flow"] if sp4h_flow["spot_tb_4h_flow"] is not None else bas_stats["spot_tb_4h"],
            "spot_4h_complete": sp4h_flow["spot_4h_complete"],
            "spot_vrat_4h": sp4h_flow["spot_vrat_4h"],
            "spot_tsize_r_4h": sp4h_flow.get("spot_tsize_r_4h"),
            "spot_1h_complete": sp1h_flow["spot_1h_complete"],
            "spot_tb_1h": sp1h_flow["spot_tb_1h"],
            "spot_vrat_1h": sp1h_flow["spot_vrat_1h"],
            "spot_tsize_r_1h": sp1h_flow.get("spot_tsize_r_1h"),
            "spot_red_1h": sp1h_flow.get("spot_red_1h"),
            "spot_green_1h": sp1h_flow.get("spot_green_1h"),
            "spot_uwick_1h": sp1h_flow.get("spot_uwick_1h"),
            "spot_lwick_1h": sp1h_flow.get("spot_lwick_1h"),
            "spot_2h_complete": sp2h_flow["spot_2h_complete"],
            "spot_tb_2h": sp2h_flow["spot_tb_2h"],
            "spot_vrat_2h": sp2h_flow["spot_vrat_2h"],
            "spot_tsize_r_2h": sp2h_flow.get("spot_tsize_r_2h"),
            "spot_red_2h": sp2h_flow.get("spot_red_2h"),
            "spot_green_2h": sp2h_flow.get("spot_green_2h"),
            "spot_uwick_2h": sp2h_flow.get("spot_uwick_2h"),
            "spot_lwick_2h": sp2h_flow.get("spot_lwick_2h"),
            "ps_vrat": ps_vrat,
            "bar_complete_2h": bar_complete_2h,
            "o_2h": o_2h,
            "hi_2h": hi_2h,
            "lo_2h": lo_2h,
            "c_2h": c_2h,
            "o_2h_prev": o_2h_prev,
            "c_2h_prev": c_2h_prev,
            "qv_2h": qv_2h,
            "qv_avg20_2h": qv_avg20_2h,
            "qv_2h_prev": qv_2h_prev,
            "qv_avg20_2h_prev": qv_avg20_2h_prev,
            "tb_2h": tb_2h,
            "ema10_2h": ema10_2h,
            "ema10_2h_prev": ema10_2h_prev,
            "ema20_2h": ema20_2h,
            "o_4h": o_4h,
            "hi_4h": hi_4h,
            "lo_4h": lo_4h,
            "c_4h": c_4h,
            "c_4h_prev": c_4h_prev,
            "qv_4h": qv_4h,
            "qv_avg20_4h": qv_avg20_4h,
            "tb_4h": tb_4h,
            "ema10_4h": ema10_4h,
            "ema10_4h_prev": ema10_4h_prev,
            "m15_complete": m15_complete,
            "c15_now": _c15_n,
            "c15_prev": _c15_p,
            "hi15_now": _hi15_n,
            "lo15_now": m15_lo[-1] if m15_lo else None,
            "qv15_now": m15_qv[-1] if m15_qv else None,
            "qv15_avg20": (sum(m15_qv[-21:-1]) / 20.0) if len(m15_qv) >= 21 else None,
            "ema20_15m": ema20_15m,
            "ema9_15m": market.ema(m15_cl, 9) if len(m15_cl) >= 9 else None,
            "ema21_15m": market.ema(m15_cl, 21) if len(m15_cl) >= 21 else None,
            "sma50_15m": sma50_15m,
            "ema9_1h": market.ema(h_cl, 9) if len(h_cl) >= 9 else None,
            "ema21_1h": market.ema(h_cl, 21) if len(h_cl) >= 21 else None,
            "rsi15m_now": rsi15m_now,
            "atr14_15m_pct": atr14_15m_pct,
            "low16_15m": low16_15m,
            "res16_15m": res16_15m,
            "spot_tb_15m": (
                (lambda r: (r[2] / r[1]) if (r and len(r) >= 3 and r[1] > 0 and r[2] is not None) else None)(
                    (spot_15m.get(sym) or {}).get(target_15m_str)
                )
            ),
        })

    pct_above_d_sma20 = (n_1d_above_sma20 / n_1d_valid) if n_1d_valid else 0.0
    pct_above_1h_ema20 = (n_1h_above_ema20 / n_1h_valid) if n_1h_valid else 0.0
    pct_above_15m_ema20 = (n_15m_above_ema20 / n_15m_valid) if n_15m_valid else 0.0
    pct_24h_pos = (sum(1 for r in rets_24h if r > 0) / len(rets_24h)) if rets_24h else 0.0
    med_24h = statistics.median(rets_24h) if rets_24h else 0.0
    complete_1h_pct = (n_1h_complete / n_1h_checked * 100.0) if n_1h_checked else 0.0
    complete_15m_pct = (n_15m_complete / n_15m_checked * 100.0) if n_15m_checked else 0.0

    active_squeeze = is_active_squeeze(
        btc_ret24,
        btc_1h_ret1,
        btc_1h_ret4,
        pct_above_1h_ema20,
        btc_15m_ret1=btc_15m_ret1,
    )
    active_crash = is_active_crash(
        btc_ret24,
        btc_1h_ret1,
        btc_1h_ret4,
        pct_above_1h_ema20,
        btc_15m_ret1=btc_15m_ret1,
    )
    regime_label = classify_regime(
        btc_px,
        btc_sma20_d,
        btc_ret24,
        btc_1h_ret1,
        btc_1h_ret4,
        pct_above_d_sma20,
        pct_above_1h_ema20,
        btc_15m_ret1=btc_15m_ret1,
        btc_15m_ret4=btc_15m_ret4,
        pct_above_15m_ema20=pct_above_15m_ema20 if n_15m_valid else None,
        btc_1h_ema20=btc_1h_ema20,
    )

    ema20_1h_save = ema20_1h_map_complete if len(ema20_1h_map_complete) >= B1H_EMA_MIN_SYMS else ema20_1h_map
    if len(ema20_1h_save) >= B1H_EMA_MIN_SYMS:
        with contextlib.suppress(Exception):
            _save_btc_ref_merged({
                "ema20_1h_map": ema20_1h_save,
                "ema20_1h_at": now_utc.isoformat(timespec="seconds"),
                "ema20_1h_target_hour": target_hour_str,
            })

    # Pass 2: Evaluate V21 25-Engine SHORT + V22 5-Engine LONG setups with exact live breadth, BTC, ATR floor, Spot+Perp flow & basis
    candidates = []
    for p in prepared:
        short_hits = [
            {**h, "side": "SHORT"}
            for h in evaluate_v12_short_setups(
            age_days=p["age_days"],
            breadth_1d=pct_above_d_sma20,
            breadth_1h=pct_above_1h_ema20,
            btc_ret7d=btc_ret7,
            btc_ret4h=btc_1h_ret4,
            bar_complete_1h=p["bar_complete_1h"],
            bars_1h_count=p["bars_1h_count"],
            cur_px=p["cur_px"],
            o_1h=p["o_1h"],
            hi_1h=p["hi_1h"],
            lo_1h=p["lo_1h"],
            c_1h=p["c_1h"],
            c_1h_prev=p["c_1h_prev"],
            qv_1h=p["qv_1h"],
            qv_avg20_1h=p["qv_avg20_1h"],
            tb_1h=p["tb_1h"],
            ema10_1h=p["ema10_1h"],
            ema10_1h_prev=p["ema10_1h_prev"],
            ema20_1h=p["ema20_1h"],
            lo16_1h_prev=p["lo16_1h_prev"],
            atr14_1h_pct=p["atr14_1h_pct"],
            hi24=p["hi24"],
            hi7d=p["hi7d"],
            ret_4h=p["ret_4h"],
            ret_12h=p["ret_12h"],
            ret_24h=p["ret_24h"],
            ret_7d=p["ret_7d"],
            d_sma20=p["d_sma20"],
            f_day=p["f_day"],
            has_spot_24h=p["has_spot_24h"],
            bas15=p["bas15"],
            bas1h=p["bas1h"],
            bas8h=p["bas8h"],
            bas12h_max=p["bas12h_max"],
            bas_4h_close=p["bas_4h_close"],
            spot_tb_4h=p["spot_tb_4h"],
            spot_4h_complete=p["spot_4h_complete"],
            spot_vrat_4h=p["spot_vrat_4h"],
            spot_tsize_r_4h=p["spot_tsize_r_4h"],
            spot_1h_complete=p["spot_1h_complete"],
            spot_tb_1h=p["spot_tb_1h"],
            spot_vrat_1h=p["spot_vrat_1h"],
            spot_tsize_r_1h=p["spot_tsize_r_1h"],
            spot_red_1h=p["spot_red_1h"],
            spot_uwick_1h=p["spot_uwick_1h"],
            spot_2h_complete=p["spot_2h_complete"],
            spot_tb_2h=p["spot_tb_2h"],
            spot_vrat_2h=p["spot_vrat_2h"],
            spot_tsize_r_2h=p["spot_tsize_r_2h"],
            spot_red_2h=p["spot_red_2h"],
            spot_uwick_2h=p["spot_uwick_2h"],
            ps_vrat=p["ps_vrat"],
            is_fresh_2h_window=is_fresh_2h_window,
            bar_complete_2h=p["bar_complete_2h"],
            o_2h=p["o_2h"],
            hi_2h=p["hi_2h"],
            lo_2h=p["lo_2h"],
            c_2h=p["c_2h"],
            o_2h_prev=p["o_2h_prev"],
            c_2h_prev=p["c_2h_prev"],
            qv_2h=p["qv_2h"],
            qv_avg20_2h=p["qv_avg20_2h"],
            qv_2h_prev=p["qv_2h_prev"],
            qv_avg20_2h_prev=p["qv_avg20_2h_prev"],
            tb_2h=p["tb_2h"],
            ema10_2h=p["ema10_2h"],
            ema10_2h_prev=p["ema10_2h_prev"],
            ema20_2h=p["ema20_2h"],
            is_fresh_4h_window=is_fresh_4h_window,
            bar_complete_4h=p["bar_complete_4h"],
            o_4h=p["o_4h"],
            hi_4h=p["hi_4h"],
            lo_4h=p["lo_4h"],
            c_4h=p["c_4h"],
            c_4h_prev=p["c_4h_prev"],
            qv_4h=p["qv_4h"],
            qv_avg20_4h=p["qv_avg20_4h"],
            tb_4h=p["tb_4h"],
            ema10_4h=p["ema10_4h"],
            ema10_4h_prev=p["ema10_4h_prev"],
        )
        ]
        long_hits = evaluate_v22_long_setups(
            breadth_1d=pct_above_d_sma20,
            breadth_1h=pct_above_1h_ema20,
            btc_px=btc_px,
            btc_1h_ema20=btc_1h_ema20,
            btc_ret1h=btc_1h_ret1,
            bar_complete_1h=p["bar_complete_1h"],
            cur_px=p["cur_px"],
            o_1h=p["o_1h"],
            hi_1h=p["hi_1h"],
            lo_1h=p["lo_1h"],
            c_1h=p["c_1h"],
            c_1h_prev=p["c_1h_prev"],
            qv_1h=p["qv_1h"],
            qv_avg20_1h=p["qv_avg20_1h"],
            tb_1h=p["tb_1h"],
            ema10_1h=p["ema10_1h"],
            ema10_1h_prev=p["ema10_1h_prev"],
            ema20_1h=p["ema20_1h"],
            hi16_1h_prev=p["hi16_1h_prev"],
            rsi1h_now=p["rsi1h_now"],
            atr14_1h_pct=p["atr14_1h_pct"],
            ret_24h=p["ret_24h"],
            d_sma20=p["d_sma20"],
            f_day=p["f_day"],
            has_spot_24h=p["has_spot_24h"],
            bas8h=p["bas8h"],
            spot_1h_complete=p["spot_1h_complete"],
            spot_tb_1h=p["spot_tb_1h"],
            spot_vrat_1h=p["spot_vrat_1h"],
            spot_green_1h=p["spot_green_1h"],
            spot_lwick_1h=p["spot_lwick_1h"],
            is_fresh_2h_window=is_fresh_2h_window,
            bar_complete_2h=p["bar_complete_2h"],
            o_2h=p["o_2h"],
            hi_2h=p["hi_2h"],
            lo_2h=p["lo_2h"],
            c_2h=p["c_2h"],
            c_2h_prev=p["c_2h_prev"],
            qv_2h=p["qv_2h"],
            qv_avg20_2h=p["qv_avg20_2h"],
            tb_2h=p["tb_2h"],
            ema10_2h=p["ema10_2h"],
            ema10_2h_prev=p["ema10_2h_prev"],
            ema20_2h=p["ema20_2h"],
        )
        mood_hits = evaluate_v23_ema_rsi_mood(
            m15_complete=p["m15_complete"],
            cur_px=p["cur_px"],
            c15_now=p["c15_now"],
            c15_prev=p["c15_prev"],
            hi15_now=p["hi15_now"],
            lo15_now=p["lo15_now"],
            qv15_now=p["qv15_now"],
            qv15_avg20=p["qv15_avg20"],
            ema9_15m=p["ema9_15m"],
            ema21_15m=p["ema21_15m"],
            sma50_15m=p["sma50_15m"],
            rsi15m_now=p["rsi15m_now"],
            low16_15m=p["low16_15m"],
            res16_15m=p["res16_15m"],
            ema9_1h=p["ema9_1h"],
            ema21_1h=p["ema21_1h"],
            rsi1h_now=p["rsi1h_now"],
            atr14_1h_pct=p["atr14_1h_pct"],
            ret_24h=p["ret_24h_display"],
            btc_ret24=btc_ret24,
            f_day=p["f_day"],
        )
        all_hits = short_hits + long_hits + mood_hits
        if not all_hits:
            continue
        all_hits.sort(key=lambda h: -h["score"])
        best_side = all_hits[0]["side"]
        hits = [h for h in all_hits if h["side"] == best_side]
        best = hits[0]
        dd_hi7 = ((p["c_1h"] / p["hi7d"] - 1.0) * 100.0) if (p["hi7d"] and p["hi7d"] > 0) else None
        dd_hi24 = ((p["c_1h"] / p["hi24"] - 1.0) * 100.0) if (p["hi24"] and p["hi24"] > 0) else None
        uwick_1h = (
            round((p["hi_1h"] - max(p["o_1h"], p["c_1h"])) / (p["hi_1h"] - p["lo_1h"]), 2)
            if (p["hi_1h"] > p["lo_1h"])
            else None
        )
        lwick_1h = (
            round((min(p["o_1h"], p["c_1h"]) - p["lo_1h"]) / (p["hi_1h"] - p["lo_1h"]), 2)
            if (p["hi_1h"] > p["lo_1h"])
            else None
        )
        uwick_2h = (
            round((p["hi_2h"] - max(p["o_2h"], p["c_2h"])) / (p["hi_2h"] - p["lo_2h"]), 2)
            if (p["o_2h"] is not None and p["c_2h"] is not None and p["hi_2h"] is not None and p["lo_2h"] is not None and p["hi_2h"] > p["lo_2h"])
            else None
        )
        vrat_1h = round(p["qv_1h"] / p["qv_avg20_1h"], 2) if (p["qv_avg20_1h"] and p["qv_avg20_1h"] > 0) else None
        vrat_2h = round(p["qv_2h"] / p["qv_avg20_2h"], 2) if (p["qv_2h"] is not None and p["qv_avg20_2h"] and p["qv_avg20_2h"] > 0) else None
        vrat_4h = round(p["qv_4h"] / p["qv_avg20_4h"], 2) if (p["qv_4h"] is not None and p["qv_avg20_4h"] and p["qv_avg20_4h"] > 0) else None
        ext_e10_1h = round((p["c_1h"] / p["ema10_1h"] - 1.0) * 100.0, 2) if (p["ema10_1h"] and p["ema10_1h"] > 0) else None
        candidates.append({
            "symbol": p["sym"],
            "coin": p["coin"],
            "side": best.get("side") or "SHORT",
            "setups": [h["setup"] for h in hits],
            "hit_specs": hits,
            "primary_setup": best["setup"],
            "score": best["score"],
            "age_days": round(p["age_days"], 1),
            "price": round(p["cur_px"], 8),
            "ema10_1h": round(p["ema10_1h"], 8) if p["ema10_1h"] is not None else None,
            "ema10_4h": round(p["ema10_4h"], 8) if p["ema10_4h"] is not None else None,
            "tb_1h": round(p["tb_1h"], 2),
            "tb_2h": round(p["tb_2h"], 2) if p["tb_2h"] is not None else None,
            "tb_4h": round(p["tb_4h"], 2) if p["tb_4h"] is not None else None,
            "spot_tb_1h": round(p["spot_tb_1h"], 2) if p["spot_tb_1h"] is not None else None,
            "spot_vrat_1h": round(p["spot_vrat_1h"], 2) if p["spot_vrat_1h"] is not None else None,
            "spot_tsize_r_1h": round(p["spot_tsize_r_1h"], 2) if p["spot_tsize_r_1h"] is not None else None,
            "spot_uwick_1h": round(p["spot_uwick_1h"], 2) if p.get("spot_uwick_1h") is not None else None,
            "spot_lwick_1h": round(p["spot_lwick_1h"], 2) if p.get("spot_lwick_1h") is not None else None,
            "spot_tb_2h": round(p["spot_tb_2h"], 2) if p["spot_tb_2h"] is not None else None,
            "spot_vrat_2h": round(p["spot_vrat_2h"], 2) if p["spot_vrat_2h"] is not None else None,
            "spot_tsize_r_2h": round(p["spot_tsize_r_2h"], 2) if p["spot_tsize_r_2h"] is not None else None,
            "spot_uwick_2h": round(p["spot_uwick_2h"], 2) if p.get("spot_uwick_2h") is not None else None,
            "spot_tb_4h": round(p["spot_tb_4h"], 2) if p["spot_tb_4h"] is not None else None,
            "spot_vrat_4h": round(p["spot_vrat_4h"], 2) if p["spot_vrat_4h"] is not None else None,
            "spot_tsize_r_4h": round(p["spot_tsize_r_4h"], 2) if p["spot_tsize_r_4h"] is not None else None,
            "ps_vrat": round(p["ps_vrat"], 2) if p["ps_vrat"] is not None else None,
            "uwick_1h": uwick_1h,
            "lwick_1h": lwick_1h,
            "uwick_2h": uwick_2h,
            "vrat_1h": vrat_1h,
            "vrat_2h": vrat_2h,
            "vrat_4h": vrat_4h,
            "ext_e10_1h_pct": ext_e10_1h,
            "bas15_pct": round(p["bas15"] * 100.0, 3) if p["bas15"] is not None else None,
            "bas1h_pct": round(p["bas1h"] * 100.0, 3) if p["bas1h"] is not None else None,
            "bas8h_pct": round(p["bas8h"] * 100.0, 3) if p["bas8h"] is not None else None,
            "bas12m_pct": round(p["bas12h_max"] * 100.0, 3) if p["bas12h_max"] is not None else None,
            "bas4h_pct": round(p["bas_4h_close"] * 100.0, 3) if p["bas_4h_close"] is not None else None,
            "prev_ret_2h_pct": round((p["c_2h_prev"] / p["o_2h_prev"] - 1.0) * 100.0, 2) if (p["o_2h_prev"] and p["c_2h_prev"] is not None and p["o_2h_prev"] > 0) else None,
            "vrat_2h_prev": round(p["qv_2h_prev"] / p["qv_avg20_2h_prev"], 2) if (p["qv_2h_prev"] is not None and p["qv_avg20_2h_prev"] and p["qv_avg20_2h_prev"] > 0) else None,
            "vol_24h_m": round(p["vol_24h"] / 1e6, 2),
            "funding_day_pct": round(p["f_day"] * 100.0, 4),
            "ret_4h_pct": round(p["ret_4h"] * 100.0, 2) if p["ret_4h"] is not None else None,
            "ret_12h_pct": round(p["ret_12h"] * 100.0, 2) if p["ret_12h"] is not None else None,
            "ret_24h_pct": round(p["ret_24h_display"] * 100.0, 2) if p["ret_24h_display"] is not None else None,
            "ret_7d_pct": round(p["ret_7d"] * 100.0, 2) if p["ret_7d"] is not None else None,
            "dd_hi24_pct": round(dd_hi24, 2) if dd_hi24 is not None else None,
            "dd_hi7d_pct": round(dd_hi7, 2) if dd_hi7 is not None else None,
            "rsi1h": round(p["rsi1h_now"], 1) if p["rsi1h_now"] is not None else None,
            "rsi15m": round(p["rsi15m_now"], 1) if p["rsi15m_now"] is not None else None,
            "vrat_15m": round(p["qv15_now"] / p["qv15_avg20"], 2) if (p["qv15_now"] is not None and p["qv15_avg20"] and p["qv15_avg20"] > 0) else None,
            "rs24_pct": round((p["ret_24h_display"] - btc_ret24) * 100.0, 2) if (p["ret_24h_display"] is not None and btc_ret24 is not None) else None,
            "atr1h_pct": round(p["atr14_1h_pct"] * 100.0, 2) if p["atr14_1h_pct"] is not None else None,
            "suggested_stop_pct": best["stop_pct"],
            "suggested_tp_pct": best["tp_pct"],
            "exit_mode": best.get("exit_mode") or DEFAULT_EXIT_MODE,
        })

    candidates.sort(key=lambda x: (-x["score"], x["symbol"]))

    regime = {
        "regime_label": regime_label,
        "active_squeeze": active_squeeze,
        "active_crash": active_crash,
        "funding_ok": True,
        "spot_ok": spot_ok,
        "strategy": STRATEGY_VERSION,
        "target_1h_bar_utc": target_hour_str,
        "target_2h_bar_utc": target_2h_str,
        "target_4h_bar_utc": target_4h_str,
        "target_15m_bar_utc": target_15m_str,
        "is_fresh_2h_window": is_fresh_2h_window,
        "is_fresh_4h_window": is_fresh_4h_window,
        "btc_1h_complete": btc_complete,
        "btc_15m_complete": btc_15m_complete,
        "complete_1h_pct": round(complete_1h_pct, 1),
        "complete_15m_pct": round(complete_15m_pct, 1),
        "patched_1h": patched_1h,
        "patched_spot_15m": patched_spot_15m,
        "liquid_universe": len(rets_24h),
        "btc": {
            "price": round(btc_px, 2),
            "sma20_1d": round(btc_sma20_d, 2) if btc_sma20_d is not None else None,
            "above_sma20_1d": bool(btc_sma20_d is not None and btc_px > btc_sma20_d),
            "ema20_1h": round(btc_1h_ema20, 2) if btc_1h_ema20 is not None else None,
            "sma50_1h": round(btc_1h_sma50, 2) if btc_1h_sma50 is not None else None,
            "ema20_15m": round(btc_15m_ema20, 2) if btc_15m_ema20 is not None else None,
            "sma50_15m": round(btc_15m_sma50, 2) if btc_15m_sma50 is not None else None,
            "rsi15m": round(btc_15m_rsi, 1) if btc_15m_rsi is not None else None,
            "ret_15m_pct": round(btc_15m_ret1 * 100.0, 2) if btc_15m_ret1 is not None else None,
            "ret_60m_15m_pct": round(btc_15m_ret4 * 100.0, 2) if btc_15m_ret4 is not None else None,
            "ret_1h_pct": round(btc_1h_ret1 * 100.0, 2) if btc_1h_ret1 is not None else None,
            "ret_4h_pct": round(btc_1h_ret4 * 100.0, 2) if btc_1h_ret4 is not None else None,
            "ret_24h_pct": round(btc_ret24 * 100.0, 2) if btc_ret24 is not None else None,
            "ret_7d_pct": round(btc_ret7 * 100.0, 2) if btc_ret7 is not None else None,
            "ret_14d_pct": round(btc_ret14 * 100.0, 2) if btc_ret14 is not None else None,
            "funding_day_pct": round(funding_day.get("BTCUSDT", 0.0) * 100.0, 4),
        },
        "breadth": {
            "pct_above_1d_sma20": round(pct_above_d_sma20 * 100.0, 1),
            "pct_above_1h_ema20": round(pct_above_1h_ema20 * 100.0, 1),
            "pct_above_15m_ema20": round(pct_above_15m_ema20 * 100.0, 1) if n_15m_valid else None,
            "pct_24h_positive": round(pct_24h_pos * 100.0, 1),
            "median_24h_ret_pct": round(med_24h * 100.0, 2),
            "median_bas8h_pct": round(statistics.median(bas8h_vals) * 100.0, 3) if bas8h_vals else None,
            "pos_bas8h_count": sum(1 for b in bas8h_vals if b >= 0.0),
            "valid_bas8h_count": len(bas8h_vals),
        },
    }
    return regime, candidates[:40], sorted(cooled), sorted(opened_today), held_tech


def _dump_json(obj, pretty=False):
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _recent_journal(limit=10):
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
        out = []
        for raw in lines[-limit:]:
            with contextlib.suppress(ValueError):
                out.append(json.loads(raw))
        return out
    except OSError:
        return []


def _extract_last_round_regime(entries):
    for e in reversed(entries or []):
        if e.get("event") == "round":
            m = re.match(r"^([A-Z0-9_]+)", str(e.get("regime") or "").strip())
            if m:
                return m.group(1)
    return None


def _compact_closed_recent(closed_list, limit=3):
    out = []
    for r in (closed_list or [])[:limit]:
        out.append({
            "symbol": r.get("symbol"),
            "pnl_usdt": r.get("pnl_usdt"),
            "closed_at": str(r.get("closed_at") or "")[:16],
        })
    return out


def _compact_journal_recent(entries, limit=2):
    out = []
    for e in (entries or [])[-limit:]:
        ev = e.get("event")
        item = {"at_th": e.get("at_th"), "event": ev}
        if ev == "round":
            m = re.match(r"^([A-Z0-9_]+)", str(e.get("regime") or "").strip())
            item["regime"] = m.group(1) if m else str(e.get("regime") or "")[:32]
            item["picks"] = e.get("picks") or []
        elif ev in ("open", "open_error"):
            item["symbol"] = e.get("symbol")
            if e.get("setup"):
                item["setup"] = e["setup"]
            if e.get("error"):
                item["error"] = str(e.get("error"))[:60]
        elif ev in ("close", "close_error"):
            item["id"] = e.get("id")
            if e.get("error"):
                item["error"] = str(e.get("error"))[:60]
            else:
                cr = e.get("closed_record") or {}
                pnl_u = e.get("pnl_usdt") if e.get("pnl_usdt") is not None else cr.get("pnl_usdt")
                if pnl_u is not None:
                    item["pnl_usdt"] = pnl_u
        elif ev == "reset_cb":
            item["new_baseline"] = e.get("new_baseline")
        out.append(item)
    return out


_COMPACT_POS_TECH_KEYS = (
    "price", "ema20_15m", "ema20_1h", "ema20_4h", "d_sma20",
    "res16_15m", "low16_15m", "rsi15m", "rsi1h", "funding_day_pct",
    "above_ema20_15m", "above_ema20_1h", "above_ema20_4h", "above_d_sma20",
    "last3_15m",
)
_COMPACT_CAND_KEYS = (
    "symbol", "side", "primary_setup", "setups", "score", "age_days", "price",
    "ema10_1h", "ema10_4h", "tb_1h", "tb_2h", "tb_4h",
    "spot_tb_1h", "spot_vrat_1h", "spot_tb_2h", "spot_vrat_2h", "spot_tsize_r_2h",
    "spot_tb_4h", "spot_vrat_4h", "spot_tsize_r_4h", "ps_vrat",
    "uwick_1h", "lwick_1h", "vrat_1h", "vrat_4h", "ext_e10_1h_pct",
    "bas15_pct", "bas1h_pct", "bas8h_pct", "bas12m_pct", "bas4h_pct", "vol_24h_m", "funding_day_pct",
    "ret_4h_pct", "ret_12h_pct", "ret_24h_pct", "ret_7d_pct",
    "dd_hi24_pct", "dd_hi7d_pct", "rsi1h", "atr1h_pct",
    "suggested_stop_pct", "suggested_tp_pct", "exit_mode",
)


def _compact_position(pos):
    out = {
        "id": pos.get("id"),
        "symbol": pos.get("symbol"),
        "side": pos.get("side") or "SHORT",
        "entry": pos.get("entry"),
        "mark": pos.get("mark"),
        "pnl_usdt": pos.get("pnl_usdt"),
        "pnl_pct": pos.get("pnl_pct"),
        "action": pos.get("action"),
        "mfe_pct": pos.get("mfe_pct"),
        "mae_pct": pos.get("mae_pct"),
        "stop_px": pos.get("stop_px"),
        "tp_px": pos.get("tp_px"),
        "ratchet_stage": pos.get("ratchet_stage"),
        "exit_mode": pos.get("exit_mode"),
        "held_hours": pos.get("held_hours"),
    }
    if pos.get("setup"):
        out["setup"] = pos["setup"]
    if pos.get("exit_reason"):
        out["exit_reason"] = pos["exit_reason"]
    if pos.get("unclear"):
        out["unclear"] = True
    if pos.get("kline_error"):
        out["kline_error"] = pos["kline_error"]
    tech = pos.get("tech")
    if isinstance(tech, dict):
        ct = {k: tech[k] for k in _COMPACT_POS_TECH_KEYS if k in tech}
        if tech.get("complete_15m") is False:
            ct["complete_15m"] = False
        out["tech"] = ct
    elif pos.get("last3_15m"):
        out["last3_15m"] = pos["last3_15m"]
    return out


def _compact_candidate(cand):
    return {k: cand[k] for k in _COMPACT_CAND_KEYS if k in cand}


def _execute_open(sym, stop_pct, tp_pct, reason, setup="V22", score=None, regime_label=None, btc_px=None, exit_mode=DEFAULT_EXIT_MODE, now_utc=None, entry_est=None, prefetched_spread=None, side=ALLOWED_SIDE):
    """Internal guardrail-verified SHORT/LONG open helper shared by `open` and `auto-trade`."""
    now_utc = now_utc or utcnow()
    sym = normalize_symbol(sym)
    side_u = str(side or ALLOWED_SIDE).upper()
    is_long = (side_u == "LONG")
    if is_long and exit_mode == DEFAULT_EXIT_MODE:
        exit_mode = LONG_EXIT_MODE
    pf = binance.save_portfolio(binance.fetch_portfolio())
    state = read_json(STATE_PATH, default={})
    equity = pf.get("equity_usdt") if not pf.get("error") else None
    state, cb = evaluate_circuit_breaker(state, equity, now_utc.isoformat())
    write_json_atomic(STATE_PATH, state)

    spread = prefetched_spread
    spread_err = None
    if spread is None:
        try:
            spread = fetch_live_spread(sym)
        except Exception as e:  # noqa: BLE001
            spread_err = safe_err(e)

    ok, err = validate_open_guardrails(sym, side_u, stop_pct, tp_pct, pf, state, now_utc, spread=spread)
    if not ok:
        return False, {"ok": False, "symbol": sym, "side": side_u, "error": err, "circuit_breaker": cb, "spread_err": spread_err}, 2

    today_iso = now_utc.astimezone(dt.timezone.utc).date().isoformat()
    expected_cid = binance.order_id("open", today_iso, sym)
    sp_d_pre = dec(stop_pct, DEFAULT_STOP_PCT)
    tp_d_pre = dec(tp_pct, DEFAULT_TP_PCT)
    est_d = dec(entry_est)
    # V13/V14 CRITICAL Fix: persist pending_open meta BEFORE POST /open so SSH/timeout after Binance fill NEVER degrades to LEGACY
    if expected_cid:
        pos_meta_pending = read_json(POS_META_PATH, default={})
        if expected_cid not in pos_meta_pending:
            pos_meta_pending[expected_cid] = {
                "symbol": sym,
                "side": side_u,
                "setup": setup,
                "score": score,
                "regime": regime_label,
                "btc_px": btc_px,
                "exit_mode": exit_mode,
                "opened_at": now_utc.isoformat(timespec="seconds"),
                "stop_pct": str(sp_d_pre),
                "tp_pct": str(tp_d_pre),
                "ratchet_stage": "INIT",
                "runner_unlocked": False,
                "mfe_pct": "0",
                "mae_pct": "0",
                "why": reason,
                "reason": reason,
                "opened_by": "Antigravity-V22",
                "pending_open": True,
            }
            write_json_atomic(POS_META_PATH, pos_meta_pending)

    try:
        res = binance.post("/open", {
            "run_id": today_iso,
            "symbol": sym,
            "side": side_u,
            "usdt": str(LOT_USDT),
            "leverage": LEVERAGE,
        })
    except Exception as e:  # noqa: BLE001
        msg = safe_err(e)
        _append_journal("open_error", {"id": expected_cid, "symbol": sym, "side": side_u, "setup": setup, "error": msg, "reason": reason}, now_utc)
        return False, {"ok": False, "id": expected_cid, "symbol": sym, "side": side_u, "error": msg}, 1

    cid = res.get("cid") or expected_cid
    accepted = res.get("status") in ("placed", "found", "tested", "unclear")
    if not accepted and expected_cid:
        pos_meta_clean = read_json(POS_META_PATH, default={})
        if (pos_meta_clean.get(expected_cid) or {}).get("pending_open"):
            pos_meta_clean.pop(expected_cid, None)
            write_json_atomic(POS_META_PATH, pos_meta_clean)
    # V12.1/V13/V14 Fix: finalize pre-sync pos_meta BEFORE saving portfolio.json so 5s fast-guard never sees a new fill as LEGACY
    if accepted and cid:
        pos_meta_pre = read_json(POS_META_PATH, default={})
        if expected_cid and expected_cid != cid and (pos_meta_pre.get(expected_cid) or {}).get("pending_open"):
            pos_meta_pre.pop(expected_cid, None)
        est_stop = None
        est_tp = None
        if est_d is not None and est_d > Decimal("0"):
            est_stop = est_d * (Decimal("1") - sp_d_pre) if is_long else est_d * (Decimal("1") + sp_d_pre)
            est_tp = est_d * (Decimal("1") + tp_d_pre) if is_long else est_d * (Decimal("1") - tp_d_pre)
        pos_meta_pre[cid] = {
            "symbol": sym,
            "side": side_u,
            "setup": setup,
            "score": score,
            "regime": regime_label,
            "btc_px": btc_px,
            "exit_mode": exit_mode,
            "opened_at": now_utc.isoformat(timespec="seconds"),
            "stop_pct": str(sp_d_pre),
            "tp_pct": str(tp_d_pre),
            **(
                {
                    "entry_est": str(est_d),
                    "stop_px": str(est_stop),
                    "tp_px": str(est_tp),
                    "tentative_stops": True,
                }
                if (est_stop is not None and est_tp is not None)
                else {}
            ),
            "ratchet_stage": "INIT",
            "runner_unlocked": False,
            "mfe_pct": "0",
            "mae_pct": "0",
            "why": reason,
            "reason": reason,
            "opened_by": "Antigravity-V22",
        }
        write_json_atomic(POS_META_PATH, pos_meta_pre)

    if res.get("status") == "unclear":
        time.sleep(1.5)
        with contextlib.suppress(Exception):
            binance.post("/sync", {"run_id": today_iso})

    pf_after = binance.save_portfolio(binance.fetch_portfolio())
    entry_filled = None
    for srow in pf_after.get("shorts") or []:
        if srow.get("id") == cid or (srow.get("symbol") == sym and srow.get("on_binance")):
            entry_filled = srow.get("entry")
            break
    # V10 Fix #5: if live fill entry isn't populated yet, sync once so fast-guard has immediate stop_px/tp_px
    if accepted and res.get("status") != "tested" and not dec(entry_filled):
        time.sleep(1.0)
        with contextlib.suppress(Exception):
            binance.post("/sync", {"run_id": today_iso})
            pf_after = binance.save_portfolio(binance.fetch_portfolio())
        for srow in pf_after.get("shorts") or []:
            if srow.get("id") == cid or (srow.get("symbol") == sym and srow.get("on_binance")):
                entry_filled = srow.get("entry")
                break

    if accepted and cid:
        pos_meta = read_json(POS_META_PATH, default={})
        entry_d = dec(entry_filled)
        sp_d = dec(stop_pct, DEFAULT_STOP_PCT)
        tp_d = dec(tp_pct, DEFAULT_TP_PCT)
        stop_fields = {}
        if entry_d and entry_d > Decimal("0"):
            stop_fields = {
                "stop_px": str(entry_d * (Decimal("1") - sp_d) if is_long else entry_d * (Decimal("1") + sp_d)),
                "tp_px": str(entry_d * (Decimal("1") + tp_d) if is_long else entry_d * (Decimal("1") - tp_d)),
            }
        elif est_d is not None and est_d > Decimal("0"):
            # V14 Fix: fallback tentative stop_px/tp_px when post-open /sync lags so 5s fast-guard is never blind
            stop_fields = {
                "entry_est": str(est_d),
                "stop_px": str(est_d * (Decimal("1") - sp_d) if is_long else est_d * (Decimal("1") + sp_d)),
                "tp_px": str(est_d * (Decimal("1") + tp_d) if is_long else est_d * (Decimal("1") - tp_d)),
                "tentative_stops": True,
            }
        pos_meta[cid] = {
            "symbol": sym,
            "side": side_u,
            "setup": setup,
            "score": score,
            "regime": regime_label,
            "btc_px": btc_px,
            "exit_mode": exit_mode,
            "entry": entry_filled,
            "opened_at": now_utc.isoformat(timespec="seconds"),
            "stop_pct": str(sp_d),
            "tp_pct": str(tp_d),
            **stop_fields,
            "ratchet_stage": "INIT",
            "runner_unlocked": False,
            "mfe_pct": "0",
            "mae_pct": "0",
            "why": reason,
            "reason": reason,
            "opened_by": "Antigravity-V22",
        }
        write_json_atomic(POS_META_PATH, pos_meta)
        sigs = read_json(ENTRY_SIGNALS_PATH, default={})
        sigs[binance.coin(sym)] = f"AGY-V22 {side_u} ({setup})"
        write_json_atomic(ENTRY_SIGNALS_PATH, sigs)

    _append_journal("open", {
        "id": cid,
        "symbol": sym,
        "side": side_u,
        "setup": setup,
        "score": score,
        "regime": regime_label,
        "btc_px": btc_px,
        "exit_mode": exit_mode,
        "entry": entry_filled,
        "stop_pct": str(stop_pct),
        "tp_pct": str(tp_pct),
        "why": reason,
        "reason": reason,
        "spread": str(spread) if spread is not None else None,
        "proxy": res,
    }, now_utc)
    _append_chat_line("Antigravity", f"[{today_iso}] เปิด {side_u} {binance.coin(sym)} ({setup}, {res.get('status')}): {reason} | {res.get('line')}", now_utc)

    refused_err = None if accepted else safe_err(res.get("why") or res.get("line") or res.get("status") or "open_refused")
    return accepted, {
        "ok": accepted,
        "id": cid,
        "symbol": sym,
        "side": side_u,
        "setup": setup,
        **({"error": refused_err} if refused_err else {}),
        "spread": str(spread) if spread is not None else None,
        "proxy": res,
        "available_usdt": pf_after.get("available_usdt"),
        "shorts_count": len(pf_after.get("shorts") or []),
    }, (0 if accepted else 1)


def _execute_close(pid, why, now_utc=None):
    """Internal close helper shared by `close`, `guard`, and `auto-trade`."""
    now_utc = now_utc or utcnow()
    pid = (pid or "").strip()
    why = (why or "").strip()[:180] or "Antigravity exit"
    today_iso = now_utc.astimezone(dt.timezone.utc).date().isoformat()

    pos_meta_before = read_json(POS_META_PATH, default={})
    meta_hit = pos_meta_before.get(pid) or {}
    setup_before = meta_hit.get("setup")
    mfe_before = meta_hit.get("mfe_pct")
    mae_before = meta_hit.get("mae_pct")
    exit_mode_before = meta_hit.get("exit_mode")
    stage_before = meta_hit.get("ratchet_stage")

    try:
        res = binance.post("/close", {"id": pid, "why": why})
    except Exception as e:  # noqa: BLE001
        msg = safe_err(e)
        _append_journal("close_error", {"id": pid, "why": why, "error": msg}, now_utc)
        return False, {"ok": False, "id": pid, "error": msg}, 1

    if res.get("status") == "unclear":
        time.sleep(1.5)
    with contextlib.suppress(Exception):
        binance.post("/sync", {"run_id": today_iso})

    pf_after = binance.save_portfolio(binance.fetch_portfolio())
    pf_ok = bool(pf_after) and not pf_after.get("error")
    raw_ok = res.get("status") in ("placed", "found", "tested", "unclear")
    still_on_binance = (
        True if not pf_ok else any(
            s.get("id") == pid and s.get("on_binance")
            for s in (pf_after.get("shorts") or [])
        )
    )
    accepted = raw_ok and (res.get("status") == "tested" or (pf_ok and not still_on_binance))
    closed_sym = meta_hit.get("symbol")
    # V10/V14 Fix: NEVER wipe pos_meta or count close if fetch_portfolio errored or position is still open on Binance
    if accepted:
        pos_meta = read_json(POS_META_PATH, default={})
        if pid in pos_meta:
            closed_sym = pos_meta[pid].get("symbol") or closed_sym
            pos_meta.pop(pid, None)
            write_json_atomic(POS_META_PATH, pos_meta)
    if not closed_sym and "-" in pid:
        closed_sym = normalize_symbol(pid.rsplit("-", 1)[-1])

    state = read_json(STATE_PATH, default={})
    equity = pf_after.get("equity_usdt") if pf_ok else None
    new_state, cb = evaluate_circuit_breaker(state, equity, now_utc.isoformat())
    write_json_atomic(STATE_PATH, new_state)

    closed_rec = None
    cutoff = now_utc - dt.timedelta(minutes=5)
    for row in pf_after.get("closed") or []:
        if closed_sym and row.get("symbol") != closed_sym:
            continue
        try:
            if parse_iso_utc(row.get("closed_at") or "") >= cutoff:
                closed_rec = row
                break
        except (TypeError, ValueError):
            continue

    pnl_usdt = closed_rec.get("pnl_usdt") if closed_rec else None
    close_err = None if accepted else safe_err(
        pf_after.get("error") if not pf_ok else (res.get("why") or res.get("line") or res.get("status") or "close_unconfirmed")
    )
    side_before = str(meta_hit.get("side") or "SHORT").upper()
    _append_journal("close" if accepted else "close_unclear", {
        "id": pid,
        "symbol": closed_sym,
        "side": side_before,
        "setup": setup_before,
        "exit_mode": exit_mode_before,
        "ratchet_stage": stage_before,
        "mfe_pct": mfe_before,
        "mae_pct": mae_before,
        "pnl_usdt": pnl_usdt,
        "still_on_binance": still_on_binance,
        "why": why,
        **({"error": close_err} if close_err else {}),
        "proxy": res,
        "closed_record": closed_rec,
    }, now_utc)
    _append_chat_line("Antigravity", f"[{today_iso}] ปิด {pid} ({res.get('status')}): {why} | {res.get('line')}", now_utc)
    return accepted, {
        "ok": accepted,
        "id": pid,
        "symbol": closed_sym,
        "setup": setup_before,
        "pnl_usdt": pnl_usdt,
        "still_on_binance": still_on_binance,
        **({"error": close_err} if close_err else {}),
        "proxy": res,
        "closed_record": closed_rec,
        "equity_usdt": pf_after.get("equity_usdt"),
        "available_usdt": pf_after.get("available_usdt"),
        "shorts_count": len(pf_after.get("shorts") or []),
        "circuit_breaker": cb,
    }, (0 if accepted else 1)


def _write_round_report(regime_str, summary, universe=0, picks=None, actions=None, now_utc=None, preserve_plan_if_no_picks=False):
    now_utc = now_utc or utcnow()
    today_iso = now_utc.astimezone(dt.timezone.utc).date().isoformat()
    th_str = f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M}"
    picks = picks or []
    actions = actions or []
    pf = read_json(binance.PORTFOLIO, default={})
    held_coins = [binance.coin(s) for s in (pf.get("held_symbols") or [])]
    cooled_coins = [binance.coin(s) for s in cooldown_symbols(pf, now_utc)] if ENFORCE_OPEN_COOLDOWN else []

    rows = []
    short_syms = []
    sides = {}
    passing_coins = []
    for p in picks:
        sym = normalize_symbol(p.get("symbol") or p.get("coin") or "")
        coin = p.get("coin") or binance.coin(sym)
        p_side = str(p.get("side") or ALLOWED_SIDE).upper()
        if sym:
            short_syms.append(sym)
            sides[sym] = p_side
        passing_coins.append(coin)
        rows.append({
            "side": p_side,
            "coin": coin,
            "symbol": sym,
            "signal": p.get("signal") or p.get("primary_setup") or "AGY-V22",
            "score": str(p.get("score") or "-"),
            "price": str(p.get("price") or "-"),
            "rsi_1h": str(p.get("rsi_1h") or p.get("rsi1h") or "-"),
            "rsi_4h": str(p.get("rsi_4h") or "-"),
            "age_days": str(p.get("age_days") or "-"),
            "trend_1d": str(p.get("trend_1d") or "-"),
            "dd_ath": str(p.get("dd_ath") or "-"),
            "vol30": str(p.get("vol30") or p.get("vol_24h_m") or "-"),
            "r7": str(p.get("r7") or p.get("ret_7d_pct") or "-"),
            "trend": str(p.get("trend") or regime_str or "-"),
            "png": None,
        })

    out_dir = os.path.join(REPORTS, today_iso)
    os.makedirs(out_dir, exist_ok=True)
    plan_path = os.path.join(out_dir, "dca-plan.json")
    if preserve_plan_if_no_picks and not picks:
        prev_plan = read_json(plan_path, default={})
        if isinstance(prev_plan, dict) and prev_plan.get("run_id") == today_iso:
            short_syms = list(prev_plan.get("short") or [])
            sides = dict(prev_plan.get("sides") or {})
            rows = list(prev_plan.get("rows") or [])
            passing_coins = list(prev_plan.get("passing_coins") or [])
            if not universe and prev_plan.get("universe"):
                universe = prev_plan["universe"]
    open_cap_th = f"ถือรวมสูงสุด {MAX_OPEN} ไม้" if MAX_OPEN > 0 else f"ไม่จำกัดจำนวนไม้ (ดูตามเงินว่าง ≥ {MIN_AVAIL_USDT} USDT)"
    side_cap_th = (
        "ไม่จำกัดฝั่ง LONG/SHORT"
        if (MAX_LONG_OPEN == 0 and MAX_SHORT_OPEN == 0) or (MAX_OPEN > 0 and MAX_LONG_OPEN >= MAX_OPEN and MAX_SHORT_OPEN >= MAX_OPEN)
        else f"LONG ≤{MAX_LONG_OPEN or '∞'} · SHORT ≤{MAX_SHORT_OPEN or '∞'}"
    )
    eng_cap_th = "ไม่จำกัดต่อเอนจิน" if not ENGINE_MAX_OPEN else f"จำกัดต่อเอนจิน {len(ENGINE_MAX_OPEN)} ชุด"
    hr_cap_th = f"เปิดสูงสุด {HOURLY_MAX_OPENS} ไม้/ชม." if HOURLY_MAX_OPENS > 0 else "ไม่จำกัดไม้/ชม."
    day_cap_th = f"วันละไม่เกิน {DAILY_MAX_OPENS} ไม้" if DAILY_MAX_OPENS > 0 else "ไม่จำกัดไม้/วัน UTC"
    heat_cap_th = f"Heat ≤ {MAX_OPEN_HEAT_USDT}U" if MAX_OPEN_HEAT_USDT > Decimal("0") else "ไม่จำกัด Heat"
    gate_th = "เปิดได้ทุกสภาวะตลาด" if not ENFORCE_REGIME_OPEN_GATE else "กรองตาม Regime/Squeeze/Crash"
    cd_th = "ไม่พัก Cooldown" if not ENFORCE_OPEN_COOLDOWN else f"พักหลังปิด {SHORT_REST_HOURS} ชม. / ขาดทุน {SHORT_COOLDOWN_DAYS} วัน"
    cb_meta_rr = _config_cb_fields(state=read_json(STATE_PATH, default={}))
    cb_th = (
        f"Auto Circuit Breaker -{cb_meta_rr['cb_limit_usdt']} USDT / -{cb_meta_rr['cb_limit_pct']}% จาก Peak Equity"
        if cb_meta_rr.get("cb_auto")
        else f"Circuit Breaker -{CB_MAX_LOSS_USDT} USDT / -{CB_MAX_LOSS_PCT * Decimal('100'):.0f}%"
    )
    rule_th = (
        f"Antigravity {STRATEGY_VERSION} (EMA+RSI 15m/1H Dual-Side PULLBACK/BREAKOUT + RS เทียบ BTC + V21/V22 1H/2H · รอบ :05/:20/:35/:50 หลัง ClickHouse 15m ครบ) · "
        f"ไม้ละ {LOT_USDT} USDT (Margin {MARGIN_USDT} · {LEVERAGE}x isolated) · {open_cap_th} ({side_cap_th} · {eng_cap_th} · {hr_cap_th} · {day_cap_th} · {heat_cap_th} · {gate_th} · {cd_th} · ห้ามซ้ำเหรียญวัน UTC เดียวกัน) · "
        f"V23 ATR SL {EMA_RSI_SL_MIN * 100:.1f}–{EMA_RSI_SL_MAX * 100:.1f}% / TP 30% (1H/2H SL 5.0–7.5% / TP 40–60%) + Progressive Trail (+5%→SL-1%, +8%→+2.5%, +13%→-4.5%, +22%→-3.5%, +30%→-2.5%) + Fast-Guard 5 วิ · "
        f"{cb_th}"
    )
    plan_json = {
        "run_id": today_iso,
        "short": short_syms,
        "sides": sides,
        "mkt_trend": regime_str,
        "created": th_str,
        "rows": rows,
        "watchlist": [],
        "narrative": summary,
        "universe": universe,
        "passing": len(rows),
        "passing_coins": passing_coins,
        "held": held_coins,
        "cooled": cooled_coins,
        "entry_day": True,
        "data_to": f"{now_utc:%Y-%m-%d %H}:00",
        "rule": rule_th,
        "open_new": True,
    }
    write_json_atomic(plan_path, plan_json)

    md_lines = [
        f"# แผนเทรด Antigravity {STRATEGY_VERSION} SHORT/LONG {today_iso}",
        "",
        f"อัปเดตล่าสุด {th_str} (เวลาไทย) · สภาพตลาด: {regime_str}",
        "",
        f"## สรุปจาก Antigravity {STRATEGY_VERSION}",
        "",
        summary,
        "",
        "## การดำเนินการรอบนี้",
        "",
        *([f"- {a}" for a in actions] if actions else ["- ไม่มีคำสั่งซื้อขายในรอบนี้"]),
        "",
        f"## กติกา: {rule_th}",
    ]
    with open(os.path.join(out_dir, "dca-plan.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md_lines) + "\n")

    _append_chat_line("Antigravity", f"[{today_iso}] ({regime_str}) {summary}", now_utc)
    _append_journal("round", {"regime": regime_str, "summary": summary, "picks": short_syms, "actions": actions}, now_utc)
    return {"ok": True, "plan_dir": out_dir, "created": th_str}


def cmd_status(full=False):
    now_utc = utcnow()
    pf, sync_out, _, cb = _sync_and_portfolio(now_utc, do_sync=False)
    out = {
        "ok": not bool(pf.get("error")),
        "now_utc": now_utc.isoformat(timespec="seconds"),
        "now_th": f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M:%S}",
        "strategy": STRATEGY_VERSION,
        "portfolio": {
            "mode": pf.get("mode"),
            "at": pf.get("at"),
            "wallet_usdt": pf.get("wallet_usdt"),
            "equity_usdt": pf.get("equity_usdt"),
            "available_usdt": pf.get("available_usdt"),
            "unrealized_usdt": pf.get("unrealized_usdt"),
            "realized_usdt": pf.get("realized_usdt"),
            "shorts_count": len(pf.get("shorts") or []),
            "held_symbols": pf.get("held_symbols") or [],
            "error": safe_err(pf.get("error")) if pf.get("error") else None,
        },
        "circuit_breaker": cb,
        "limits": {
            "side": "SHORT/LONG",
            "allowed_sides": sorted(ALLOWED_SIDES),
            "margin_usdt": str(MARGIN_USDT),
            "lot_usdt": str(LOT_USDT),
            "leverage": LEVERAGE,
            "max_open": MAX_OPEN,
            "max_long_open": MAX_LONG_OPEN,
            "max_short_open": MAX_SHORT_OPEN,
            "long_exit_mode": LONG_EXIT_MODE,
            "min_avail_usdt": str(MIN_AVAIL_USDT),
            "cb_auto": bool(cb.get("cb_auto", CB_AUTO_ENABLED)),
            "cb_ref_equity_usdt": cb.get("cb_ref_equity_usdt"),
            "cb_limit_usdt": cb.get("cb_limit_usdt"),
            "cb_limit_pct": cb.get("cb_limit_pct"),
            "cb_max_loss_usdt": cb.get("cb_limit_usdt") or str(CB_MAX_LOSS_USDT),
            "cb_max_loss_pct": cb.get("cb_limit_pct") or str(CB_MAX_LOSS_PCT * Decimal("100")),
            "stop_pct_range": f"{STOP_PCT_MIN * 100:.1f}%..{STOP_PCT_MAX * 100:.1f}%",
            "tp_pct_range": f"{TP_PCT_MIN * 100:.1f}%..{TP_PCT_MAX * 100:.1f}%",
            "exit_mode": DEFAULT_EXIT_MODE,
            "loss_cooldown_days": SHORT_COOLDOWN_DAYS,
            "max_losses_sym_7d": MAX_LOSSES_PER_SYM_7D,
            "max_hold_hours": MAX_HOLD_HOURS,
            "runner_max_hours": V6_RUNNER_MAX_HOURS,
            "max_open_heat_usdt": str(MAX_OPEN_HEAT_USDT),
            "engine_max_open": ENGINE_MAX_OPEN,
            "engine_heat_cap": {k: str(v) for k, v in ENGINE_HEAT_CAP.items()},
            "daily_max_opens": DAILY_MAX_OPENS,
            "hourly_max_opens": HOURLY_MAX_OPENS,
        },
    }
    if sync_out:
        out["sync"] = sync_out
    print(_dump_json(out, pretty=full))
    return 0 if out["ok"] else 1


def _build_scan_payload(now_utc, full=False):
    # V12.1 CRITICAL Fix: hold LOCK_PATH ONLY during sync & pos_meta/state mutation (~2s), release before slow market scan
    with file_lock(LOCK_PATH, blocking=True):
        pf, sync_out, state, cb = _sync_and_portfolio(now_utc, do_sync=True)
        if pf.get("error"):
            return None, {"ok": False, "error": safe_err(pf["error"]), "circuit_breaker": cb, "sync": sync_out}
        held_bars_15m = {}
        held_flow_15m = {}
        positions = _evaluate_open_positions(pf, now_utc, bars_out=held_bars_15m, flow_out=held_flow_15m)
    regime, candidates, cooled, opened_today, held_tech = _scan_market_and_candidates(pf, now_utc, held_bars_15m=held_bars_15m)
    for pos in positions:
        sym = pos.get("symbol")
        if sym in held_tech:
            pos["tech"] = held_tech[sym]
            if sym in held_flow_15m:
                pos["tech"]["last3_15m"] = held_flow_15m[sym]
        elif sym in held_flow_15m:
            pos["last3_15m"] = held_flow_15m[sym]

    journal_tail = _recent_journal(40)
    last_round_regime = _extract_last_round_regime(journal_tail)
    regime_changed = bool(last_round_regime and last_round_regime != regime.get("regime_label"))
    daily_remaining = max(0, DAILY_MAX_OPENS - len(opened_today)) if DAILY_MAX_OPENS > 0 else None

    cand_total = len(candidates)
    cand_omitted_reason = None
    if full:
        pos_out = positions
        cand_out = candidates
        closed_out = (pf.get("closed") or [])[:10]
        journal_out = journal_tail[-10:]
        regime_out = regime
    else:
        pos_out = [_compact_position(p) for p in positions]
        if cb.get("tripped"):
            cand_out = []
            cand_omitted_reason = "circuit_breaker_tripped"
        elif not regime.get("funding_ok"):
            cand_out = []
            cand_omitted_reason = "funding_not_ok"
        elif daily_remaining is not None and daily_remaining <= 0:
            cand_out = []
            cand_omitted_reason = "daily_quota_full"
        else:
            sqz_on = bool(regime.get("active_squeeze")) if "active_squeeze" in regime else (regime.get("regime_label") == "BULLISH_SQUEEZE_CAUTION")
            cr_on = bool(regime.get("active_crash"))
            kept_cands = []
            for c in candidates:
                c_side = str(c.get("side") or "SHORT").upper()
                if ENFORCE_REGIME_OPEN_GATE:
                    if c_side == "SHORT" and sqz_on:
                        continue
                    if c_side == "LONG" and cr_on:
                        continue
                kept_cands.append(c)
            cand_out = [_compact_candidate(c) for c in kept_cands[:5]]
            if not cand_out and candidates:
                if sqz_on and cr_on:
                    cand_omitted_reason = "market_shock_caution"
                elif sqz_on:
                    cand_omitted_reason = "bullish_squeeze_caution"
                elif cr_on:
                    cand_omitted_reason = "bearish_crash_caution"
        closed_out = _compact_closed_recent(pf.get("closed"), limit=3)
        journal_out = _compact_journal_recent(journal_tail, limit=2)
        regime_out = regime

    pos_meta_now = read_json(POS_META_PATH, default={})
    open_heat_u, setup_counts = compute_open_heat_and_counts(pf, pos_meta_now)
    out = {
        "ok": True,
        "now_utc": now_utc.isoformat(timespec="seconds"),
        "now_th": f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M:%S}",
        "sync": sync_out,
        "portfolio": {
            "mode": pf.get("mode"),
            "wallet_usdt": pf.get("wallet_usdt"),
            "equity_usdt": pf.get("equity_usdt"),
            "available_usdt": pf.get("available_usdt"),
            "unrealized_usdt": pf.get("unrealized_usdt"),
            "realized_usdt": pf.get("realized_usdt"),
            "open_count": len(set(pf.get("held_symbols") or ()) | {x["symbol"] for x in pf.get("shorts") or [] if x.get("on_binance") or not x.get("entry")}),
            "open_by_side": count_open_by_side(pf, pos_meta_now),
            "leverage": LEVERAGE,
            "margin_usdt": str(MARGIN_USDT),
            "lot_usdt": str(LOT_USDT),
            "min_avail_usdt": str(MIN_AVAIL_USDT),
            "max_open": MAX_OPEN,
            "max_long_open": MAX_LONG_OPEN,
            "max_short_open": MAX_SHORT_OPEN,
            "open_heat_usdt": str(open_heat_u),
            "max_heat_usdt": str(MAX_OPEN_HEAT_USDT),
            "setup_counts": setup_counts,
            "held_symbols": pf.get("held_symbols") or [],
        },
        "circuit_breaker": cb,
        "daily_quota": {"used": len(opened_today), "cap": DAILY_MAX_OPENS if DAILY_MAX_OPENS > 0 else None, "remaining": daily_remaining},
        "last_round_regime": last_round_regime,
        "regime_changed": regime_changed,
        "positions": pos_out,
        "regime": regime_out,
        "candidates_total": cand_total,
        **({"candidates_omitted_reason": cand_omitted_reason} if cand_omitted_reason else {}),
        "candidates": cand_out,
        "cooldown_symbols": cooled,
        "opened_today_utc": opened_today,
        "closed_recent": closed_out,
        "journal_recent": journal_out,
    }
    raw_ctx = {"pf": pf, "state": state, "cb": cb, "positions": positions, "regime": regime, "candidates": candidates, "regime_changed": regime_changed, "daily_remaining": daily_remaining, "open_heat_usdt": open_heat_u, "setup_counts": setup_counts}
    return raw_ctx, out


def cmd_scan(full=False):
    now_utc = utcnow()
    raw_ctx, out = _build_scan_payload(now_utc, full=full)
    print(_dump_json(out, pretty=full))
    return 0 if raw_ctx is not None else 1


def _load_close_backoff(now_utc):
    """Load active cross-process close-failure backoff map `{pid: until_epoch_sec}` (pruning expired entries)."""
    raw = read_json(CLOSE_BACKOFF_PATH, default={})
    if not isinstance(raw, dict):
        return {}
    now_ts = now_utc.astimezone(dt.timezone.utc).timestamp()
    active = {}
    for pid, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        until_ts = rec.get("until_ts")
        if isinstance(until_ts, (int, float)) and until_ts > now_ts:
            active[pid] = float(until_ts)
    return active


def _update_close_backoff(closed_results, now_utc):
    """Persist cross-process exponential backoff (45s -> 120s -> 300s) on failed closes; clear on ok=True."""
    if not closed_results:
        return
    now_ts = now_utc.astimezone(dt.timezone.utc).timestamp()
    with file_lock(f"{CLOSE_BACKOFF_PATH}.lock", blocking=True):
        raw = read_json(CLOSE_BACKOFF_PATH, default={})
        if not isinstance(raw, dict):
            raw = {}
        cleaned = {
            pid: rec for pid, rec in raw.items()
            if isinstance(rec, dict) and isinstance(rec.get("until_ts"), (int, float)) and rec["until_ts"] > now_ts
        }
        changed = len(cleaned) != len(raw)
        for item in closed_results:
            pid = item.get("id")
            if not pid:
                continue
            if item.get("ok") is True:
                if pid in cleaned:
                    cleaned.pop(pid, None)
                    changed = True
            elif item.get("ok") is False:
                prev_fails = int((cleaned.get(pid) or {}).get("fails") or 0)
                fails = prev_fails + 1
                step_idx = min(fails - 1, len(CLOSE_FAIL_BACKOFF_STEPS_SEC) - 1)
                wait_s = CLOSE_FAIL_BACKOFF_STEPS_SEC[step_idx]
                cleaned[pid] = {"fails": fails, "until_ts": round(now_ts + wait_s, 3)}
                changed = True
        if changed:
            write_json_atomic(CLOSE_BACKOFF_PATH, cleaned)


def _refresh_b1h_ema_ref_from_ch(now_utc):
    """Lightweight ClickHouse fallback to refresh `ema20_1h_map` in `fast-guard` if hourly scan missed >90m."""
    perps, _ = fetch_usdt_perps_with_onboard()
    tickers_raw = _http_json(FAPI_TICKER_24H, timeout=15)
    liq = {
        x["symbol"] for x in (tickers_raw or [])
        if isinstance(x, dict) and x.get("symbol") in perps
        and x["symbol"] != "BTCUSDT"
        and x["symbol"] not in market.BLACKLIST_PERPS
        and x["symbol"] not in market.GOLD_PERPS
        and float(x.get("quoteVolume") or 0.0) >= MIN_VOL_24H_USD
    }
    if len(liq) < B1H_EMA_MIN_SYMS:
        return None
    hflow, _ = load_perp_hourly_flow(liq, days=4)
    ema_map = {}
    for sym, rec in hflow.items():
        if rec and len(rec[4]) >= 20:
            em = market.ema(rec[4], 20)
            if em is not None and em > 0:
                ema_map[sym] = f"{em:.10g}"
    if len(ema_map) < B1H_EMA_MIN_SYMS:
        return None
    return _save_btc_ref_merged({
        "ema20_1h_map": ema_map,
        "ema20_1h_at": now_utc.isoformat(timespec="seconds"),
        "ema20_1h_source": "fast_guard_ch_fallback",
    })


def cmd_guard(dry_run=False):
    """Fast 24/7 exit guardian (for 5-min cron): checks ONLY open positions and closes any MUST_CLOSE immediately."""
    with file_lock(LOCK_PATH, blocking=False) as got:
        if not got:
            print(_dump_json({"ok": True, "skipped": "lock_busy"}))
            return 0
        now_utc = utcnow()
        pf, sync_out, _, cb = _sync_and_portfolio(now_utc, do_sync=True)
        if pf.get("error"):
            print(_dump_json({"ok": False, "error": safe_err(pf["error"]), "circuit_breaker": cb}))
            return 1
        positions = _evaluate_open_positions(pf, now_utc)
        backoff_map = _load_close_backoff(now_utc) if not dry_run else {}
        must_close = [
            pos for pos in positions
            if pos.get("action") == "MUST_CLOSE" and pos.get("id") not in backoff_map
        ]
        closed = []
        actions = []
        if must_close:
            if dry_run:
                for pos in must_close:
                    why = f"AUTO_GUARD: {pos.get('exit_reason') or 'MUST_CLOSE'}"
                    closed.append({"id": pos["id"], "symbol": pos["symbol"], "dry_run": True, "why": why})
            elif len(must_close) == 1:
                pos = must_close[0]
                why = f"AUTO_GUARD: {pos.get('exit_reason') or 'MUST_CLOSE'}"
                ok, res, _ = _execute_close(pos["id"], why, now_utc=now_utc)
                closed.append({"id": pos["id"], "symbol": pos["symbol"], "ok": ok, "pnl_usdt": res.get("pnl_usdt"), "why": why, **({"error": res.get("error")} if not ok and res.get("error") else {})})
                if ok:
                    actions.append(f"GUARD ปิด {pos['symbol']} ({res.get('pnl_usdt')} USDT): {why}")
                else:
                    actions.append(f"GUARD ปิดไม่สำเร็จ {pos['symbol']} (รอตรวจ): {why}")
            else:
                batch_items = [
                    {"id": pos["id"], "symbol": pos["symbol"], "why": f"AUTO_GUARD: {pos.get('exit_reason') or 'MUST_CLOSE'}"}
                    for pos in must_close
                ]
                for ok, res in _execute_close_batch(batch_items, now_utc=now_utc):
                    sym_i = res.get("symbol")
                    why_i = res.get("why")
                    closed.append({"id": res.get("id"), "symbol": sym_i, "ok": ok, "pnl_usdt": res.get("pnl_usdt"), "why": why_i, **({"error": res.get("error")} if not ok and res.get("error") else {})})
                    if ok:
                        actions.append(f"GUARD ปิด {sym_i} ({res.get('pnl_usdt')} USDT): {why_i}")
                    else:
                        actions.append(f"GUARD ปิดไม่สำเร็จ {sym_i} (รอตรวจ): {why_i}")
        if closed and not dry_run:
            _update_close_backoff(closed, now_utc)
            ok_cnt = sum(1 for c in closed if c.get("ok"))
            last_reg = _extract_last_round_regime(_recent_journal(40)) or "GUARD_EXIT"
            hdr = f"GUARD ปิดอัตโนมัติ {ok_cnt}/{len(closed)} ไม้" if ok_cnt else f"GUARD ปิดไม่สำเร็จ {len(closed)} ไม้"
            summary = f"{hdr}: " + "; ".join(actions)[:230]
            _write_round_report(last_reg, summary, actions=actions, now_utc=now_utc, preserve_plan_if_no_picks=True)
        out = {
            "ok": True,
            "at_th": f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M:%S}",
            "checked": len(positions),
            "closed": closed,
            "cb_tripped": bool(cb.get("tripped")),
            "equity_usdt": pf.get("equity_usdt"),
        }
        print(_dump_json(out))
        return 0


def check_fast_mark_breach(pos_meta_all, portfolio, mark_map, now_utc, btc_ref=None):
    """Pure Decimal check of active bot positions against live mark prices for 15-second SL/TP/time/BTC-squeeze guard.

    Returns list of breach dicts `{id, symbol, reason, mark, stop_px, tp_px}` (for immediate close).
    Also supports `check_fast_guard_events` which returns `(breaches, promote_ids)` when 24h winners need immediate runner lock.
    """
    breaches, _ = check_fast_guard_events(pos_meta_all, portfolio, mark_map, now_utc, btc_ref=btc_ref)
    return breaches


def _eval_live_btc_squeeze(mark_map, btc_ref, now_utc, sqz_1h=None, sqz_4h=None, sqz_15m=None):
    """Return `(triggered, r1h_dec, r4h_dec)` from live BTC mark vs fresh cached 15m/1h/4h reference closes.

    V9/V10/V17: when `closes_15m` (rolling 20-bar map) is present in `btc_ref`, selects the completed 15m bar
    whose close time (`t + 900s`) is closest to `now - 1h` and `now - 4h` (±7.5m precision), and when `sqz_15m`
    is provided, compares live BTC mark vs the latest completed 15m close (`<= 20m` old) for 5s flash-squeeze detection.
    """
    if not btc_ref or not mark_map:
        return False, None, None
    btc_mark = dec(mark_map.get("BTCUSDT"))
    if btc_mark is None or btc_mark <= Decimal("0"):
        return False, None, None
    at_s = btc_ref.get("at")
    if at_s:
        try:
            age_s = abs((now_utc.astimezone(dt.timezone.utc) - parse_iso_utc(at_s)).total_seconds())
            if age_s > BTC_REF_MAX_AGE_SEC:
                return False, None, None
        except (TypeError, ValueError):
            return False, None, None

    thr_1h = V8_SQZ_BTC_1H if sqz_1h is None else sqz_1h
    thr_4h = V8_SQZ_BTC_4H if sqz_4h is None else sqz_4h

    ref_1h = None
    ref_4h = None
    ref_15m = None
    c15 = btc_ref.get("closes_15m")
    if isinstance(c15, dict) and c15:
        now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
        parsed = []
        for k_ms, v_px in c15.items():
            with contextlib.suppress(TypeError, ValueError):
                c_d = dec(v_px)
                if c_d is not None and c_d > Decimal("0"):
                    parsed.append((int(k_ms) + 900_000, c_d))
        if parsed:
            best_1h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 3_600_000)))
            if abs(best_1h[0] - (now_ms - 3_600_000)) <= 1_800_000:
                ref_1h = best_1h[1]
            best_4h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 14_400_000)))
            if abs(best_4h[0] - (now_ms - 14_400_000)) <= 1_800_000:
                ref_4h = best_4h[1]
            if sqz_15m is not None:
                past = [x for x in parsed if x[0] <= now_ms]
                if past:
                    latest_c = max(past, key=lambda x: x[0])
                    if (now_ms - latest_c[0]) <= 1_200_000:
                        ref_15m = latest_c[1]

    if ref_1h is None:
        ref_1h = dec(btc_ref.get("ref_1h_close"))
    if ref_4h is None:
        ref_4h = dec(btc_ref.get("ref_4h_close"))

    r1h = ((btc_mark - ref_1h) / ref_1h) if (ref_1h is not None and ref_1h > Decimal("0")) else None
    r4h = ((btc_mark - ref_4h) / ref_4h) if (ref_4h is not None and ref_4h > Decimal("0")) else None
    r15m = ((btc_mark - ref_15m) / ref_15m) if (sqz_15m is not None and ref_15m is not None and ref_15m > Decimal("0")) else None
    hit = bool(
        (r1h is not None and r1h >= thr_1h)
        or (r4h is not None and r4h >= thr_4h)
        or (sqz_15m is not None and r15m is not None and r15m >= sqz_15m)
    )
    return hit, r1h, r4h


def _eval_live_eth_15m_squeeze(mark_map, btc_ref, now_utc, sqz_15m=V18_AA_SQZ_ETH_15M):
    """Return `(triggered, r15m_dec)` from live ETH mark vs fresh cached 15m reference closes (V18 5s fast-guard)."""
    if not btc_ref or not mark_map or sqz_15m is None:
        return False, None
    eth_mark = dec(mark_map.get("ETHUSDT"))
    if eth_mark is None or eth_mark <= Decimal("0"):
        return False, None
    at_s = btc_ref.get("eth_at")
    if not at_s:
        return False, None
    try:
        age_s = abs((now_utc.astimezone(dt.timezone.utc) - parse_iso_utc(at_s)).total_seconds())
        if age_s > BTC_REF_MAX_AGE_SEC:
            return False, None
    except (TypeError, ValueError):
        return False, None
    c15 = btc_ref.get("eth_closes_15m")
    if not isinstance(c15, dict) or not c15:
        return False, None
    eth_map = {}
    for k_ms, v_px in c15.items():
        with contextlib.suppress(TypeError, ValueError):
            c_d = dec(v_px)
            if c_d is not None and c_d > Decimal("0"):
                eth_map[int(k_ms)] = c_d
    return _eval_live_eth_15m_from_map(eth_map, eth_mark, now_utc, sqz_15m=sqz_15m)


def _eval_live_btc_crash(mark_map, btc_ref, now_utc, cr_1h=V22_LONG_CRASH_BTC_1H, cr_4h=V22_LONG_CRASH_BTC_4H, cr_15m=V22_LONG_CRASH_BTC_15M):
    """Return `(triggered, r1h_dec, r4h_dec)` from live BTC mark vs cached 15m/1h/4h closes for LONG 5s crash defense."""
    if not btc_ref or not mark_map:
        return False, None, None
    btc_mark = dec(mark_map.get("BTCUSDT"))
    if btc_mark is None or btc_mark <= Decimal("0"):
        return False, None, None
    at_s = btc_ref.get("at")
    if at_s:
        try:
            age_s = abs((now_utc.astimezone(dt.timezone.utc) - parse_iso_utc(at_s)).total_seconds())
            if age_s > BTC_REF_MAX_AGE_SEC:
                return False, None, None
        except (TypeError, ValueError):
            return False, None, None
    ref_1h = ref_4h = ref_15m = None
    c15 = btc_ref.get("closes_15m")
    if isinstance(c15, dict) and c15:
        now_ms = int(now_utc.astimezone(dt.timezone.utc).timestamp() * 1000)
        parsed = []
        for k_ms, v_px in c15.items():
            with contextlib.suppress(TypeError, ValueError):
                c_d = dec(v_px)
                if c_d is not None and c_d > Decimal("0"):
                    parsed.append((int(k_ms) + 900_000, c_d))
        if parsed:
            best_1h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 3_600_000)))
            if abs(best_1h[0] - (now_ms - 3_600_000)) <= 1_800_000:
                ref_1h = best_1h[1]
            best_4h = min(parsed, key=lambda x: abs(x[0] - (now_ms - 14_400_000)))
            if abs(best_4h[0] - (now_ms - 14_400_000)) <= 1_800_000:
                ref_4h = best_4h[1]
            if cr_15m is not None:
                past = [x for x in parsed if x[0] <= now_ms]
                if past:
                    latest_c = max(past, key=lambda x: x[0])
                    if (now_ms - latest_c[0]) <= 1_200_000:
                        ref_15m = latest_c[1]
    if ref_1h is None:
        ref_1h = dec(btc_ref.get("ref_1h_close"))
    if ref_4h is None:
        ref_4h = dec(btc_ref.get("ref_4h_close"))
    r1h = ((btc_mark - ref_1h) / ref_1h) if (ref_1h is not None and ref_1h > Decimal("0")) else None
    r4h = ((btc_mark - ref_4h) / ref_4h) if (ref_4h is not None and ref_4h > Decimal("0")) else None
    r15m = ((btc_mark - ref_15m) / ref_15m) if (cr_15m is not None and ref_15m is not None and ref_15m > Decimal("0")) else None
    hit = bool(
        (r1h is not None and r1h <= cr_1h)
        or (r4h is not None and r4h <= cr_4h)
        or (cr_15m is not None and r15m is not None and r15m <= cr_15m)
    )
    return hit, r1h, r4h


def _eval_live_eth_15m_crash(mark_map, btc_ref, now_utc, cr_15m=V22_LONG_CRASH_ETH_15M):
    """Return `(triggered, r15m_dec)` from live ETH mark vs cached 15m closes for LONG 5s crash defense."""
    if not btc_ref or not mark_map or cr_15m is None:
        return False, None
    eth_mark = dec(mark_map.get("ETHUSDT"))
    if eth_mark is None or eth_mark <= Decimal("0"):
        return False, None
    at_s = btc_ref.get("eth_at")
    if not at_s:
        return False, None
    try:
        age_s = abs((now_utc.astimezone(dt.timezone.utc) - parse_iso_utc(at_s)).total_seconds())
        if age_s > BTC_REF_MAX_AGE_SEC:
            return False, None
    except (TypeError, ValueError):
        return False, None
    c15 = btc_ref.get("eth_closes_15m")
    if not isinstance(c15, dict) or not c15:
        return False, None
    eth_map = {}
    for k_ms, v_px in c15.items():
        with contextlib.suppress(TypeError, ValueError):
            c_d = dec(v_px)
            if c_d is not None and c_d > Decimal("0"):
                eth_map[int(k_ms)] = c_d
    return _eval_live_eth_15m_crash_from_map(eth_map, eth_mark, now_utc, cr_15m=cr_15m)


def check_fast_guard_events(pos_meta_all, portfolio, mark_map, now_utc, btc_ref=None):
    """Pure Decimal check returning `(breaches_to_close, runner_promote_ids)` for 5-second fast-guard."""
    breaches = []
    promote_ids = []
    btc_sqz_v8, btc_r1h, btc_r4h = _eval_live_btc_squeeze(mark_map, btc_ref, now_utc, sqz_1h=V8_SQZ_BTC_1H, sqz_4h=V8_SQZ_BTC_4H)
    btc_sqz_v10, _, _ = _eval_live_btc_squeeze(mark_map, btc_ref, now_utc, sqz_1h=V10_SQZ_BTC_1H, sqz_4h=V10_SQZ_BTC_4H)
    btc_sqz_v11_aa, _, _ = _eval_live_btc_squeeze(mark_map, btc_ref, now_utc, sqz_1h=V11_AA_SQZ_BTC_1H, sqz_4h=V11_AA_SQZ_BTC_4H)
    btc_sqz_v17_aa, _, _ = _eval_live_btc_squeeze(
        mark_map, btc_ref, now_utc, sqz_1h=V11_AA_SQZ_BTC_1H, sqz_4h=V11_AA_SQZ_BTC_4H, sqz_15m=V17_AA_SQZ_BTC_15M
    )
    btc_sqz_v21_n7, _, _ = _eval_live_btc_squeeze(
        mark_map, btc_ref, now_utc, sqz_1h=V11_NEW7_SQZ_BTC_1H, sqz_4h=V11_NEW7_SQZ_BTC_4H, sqz_15m=V21_NEW7_SQZ_BTC_15M
    )
    eth_sqz_v18_aa, eth_r15m = _eval_live_eth_15m_squeeze(mark_map, btc_ref, now_utc, sqz_15m=V18_AA_SQZ_ETH_15M)
    eth_sqz_v21_n7, _ = _eval_live_eth_15m_squeeze(mark_map, btc_ref, now_utc, sqz_15m=V21_NEW7_SQZ_ETH_15M)
    b1h_sqz_v12_aa, live_b1h, b1h_valid = _eval_live_b1h_squeeze(mark_map, btc_ref, now_utc, b1h_thr=V12_AA_SQZ_B1H)
    b1h_sqz_v21_n7, _, _ = _eval_live_b1h_squeeze(mark_map, btc_ref, now_utc, b1h_thr=V21_NEW7_SQZ_B1H)
    btc_crash_long, btc_cr_r1h, btc_cr_r4h = _eval_live_btc_crash(mark_map, btc_ref, now_utc)
    eth_crash_long, eth_cr_r15m = _eval_live_eth_15m_crash(mark_map, btc_ref, now_utc)
    pf_shorts = {
        p.get("id"): p
        for p in ((portfolio or {}).get("shorts") or [])
        if p.get("on_binance") and p.get("id")
    }
    for pid, p in pf_shorts.items():
        sym = p.get("symbol")
        if not sym:
            continue
        mark = dec((mark_map or {}).get(sym))
        if mark is None or mark <= Decimal("0"):
            continue
        meta = (pos_meta_all or {}).get(pid) or {}
        exit_mode = meta.get("exit_mode") or "LEGACY"
        pos_side = str(p.get("side") or meta.get("side") or ("LONG" if _is_v22_long_mode(exit_mode) else "SHORT")).upper()
        is_long = (pos_side == "LONG")
        real_entry = dec(p.get("entry"))
        entry = real_entry or dec(meta.get("entry") or meta.get("entry_est"))
        stop_pct = min(max(dec(meta.get("stop_pct"), DEFAULT_STOP_PCT), STOP_PCT_MIN), STOP_PCT_MAX)
        tp_pct = min(max(dec(meta.get("tp_pct"), DEFAULT_TP_PCT), TP_PCT_MIN), TP_PCT_MAX)
        if entry and entry > Decimal("0"):
            init_stop = entry * (Decimal("1") - stop_pct) if is_long else entry * (Decimal("1") + stop_pct)
        else:
            init_stop = None
        # V16.2 Fix: once real Binance fill `p["entry"]` arrives, ignore stale tentative `stop_px`/`tp_px` from `entry_est`
        stale_tentative = bool(meta.get("tentative_stops")) and real_entry is not None and real_entry > Decimal("0")
        prev_stop = None if stale_tentative else dec(meta.get("stop_px"))
        if prev_stop is not None and init_stop is not None:
            stop_px = max(prev_stop, init_stop) if is_long else min(prev_stop, init_stop)
        else:
            stop_px = prev_stop or init_stop
        prev_tp = None if stale_tentative else dec(meta.get("tp_px"))
        if entry and entry > Decimal("0"):
            default_tp_px = entry * (Decimal("1") + tp_pct) if is_long else entry * (Decimal("1") - tp_pct)
        else:
            default_tp_px = None
        tp_px = prev_tp or default_tp_px
        is_sqz_mode = _is_sqz_mode(exit_mode)
        if _is_v17_aa_mode(exit_mode):
            btc_sqz_hit = btc_sqz_v17_aa
        elif _is_v21_new7_mode(exit_mode):
            btc_sqz_hit = btc_sqz_v21_n7
        elif _is_v11_aa_mode(exit_mode):
            btc_sqz_hit = btc_sqz_v11_aa
        elif _is_v10_mode(exit_mode):
            btc_sqz_hit = btc_sqz_v10
        else:
            btc_sqz_hit = btc_sqz_v8
        eth_sqz_hit = (eth_sqz_v18_aa and _is_v18_aa_mode(exit_mode)) or (eth_sqz_v21_n7 and _is_v21_new7_mode(exit_mode))
        b1h_sqz_hit = (b1h_sqz_v12_aa and _is_v12_aa_mode(exit_mode)) or (b1h_sqz_v21_n7 and _is_v21_new7_mode(exit_mode))
        is_runner_mode = _is_runner_mode(exit_mode)
        runner_max_h = _runner_max_hours(exit_mode)
        stage = meta.get("ratchet_stage") or "INIT"
        runner_unlocked = bool(meta.get("runner_unlocked", stage in ("RUNNER72", "RUNNER96")))

        held_h = None
        if p.get("opened_at"):
            with contextlib.suppress(ValueError, TypeError):
                opened_dt = parse_iso_utc(p["opened_at"])
                held_h = max(0.0, (now_utc.astimezone(dt.timezone.utc) - opened_dt).total_seconds() / 3600.0)

        cur_pnl = (((mark - entry) / entry) if is_long else ((entry - mark) / entry)) if (entry and entry > Decimal("0")) else None
        reason = None
        if is_long:
            if stop_px is not None and mark <= stop_px:
                reason = f"FAST_SL_MARK ({mark:.6g} <= {stop_px:.6g})"
            elif tp_px is not None and mark >= tp_px:
                reason = f"FAST_TP_MARK ({mark:.6g} >= {tp_px:.6g})"
            elif (
                btc_crash_long
                and is_sqz_mode
                and _is_unprotected_sqz_stage(exit_mode, stage)
                and held_h is not None
                and held_h >= 0.25
                and cur_pnl is not None
                and cur_pnl < Decimal("0")
            ):
                r1_s = f"{(btc_cr_r1h * 100):+.2f}%" if btc_cr_r1h is not None else "n/a"
                r4_s = f"{(btc_cr_r4h * 100):+.2f}%" if btc_cr_r4h is not None else "n/a"
                reason = f"FAST_BTC_CRASH_CUT (btc 1h {r1_s} / 4h {r4_s}, pnl {(cur_pnl * 100):+.2f}%)"
            elif (
                eth_crash_long
                and is_sqz_mode
                and _is_unprotected_sqz_stage(exit_mode, stage)
                and held_h is not None
                and held_h >= 0.25
                and cur_pnl is not None
                and cur_pnl < Decimal("0")
            ):
                e15_s = f"{(eth_cr_r15m * 100):+.2f}%" if eth_cr_r15m is not None else "n/a"
                reason = f"FAST_ETH_CRASH_CUT (eth 15m {e15_s} <= {V22_LONG_CRASH_ETH_15M * 100:.1f}%, pnl {(cur_pnl * 100):+.2f}%)"
        else:
            if stop_px is not None and mark >= stop_px:
                reason = f"FAST_SL_MARK ({mark:.6g} >= {stop_px:.6g})"
            elif tp_px is not None and mark <= tp_px:
                reason = f"FAST_TP_MARK ({mark:.6g} <= {tp_px:.6g})"
            elif (
                btc_sqz_hit
                and is_sqz_mode
                and _is_unprotected_sqz_stage(exit_mode, stage)
                and held_h is not None
                and held_h >= 0.25
                and cur_pnl is not None
                and cur_pnl < Decimal("0")
            ):
                r1_s = f"{(btc_r1h * 100):+.2f}%" if btc_r1h is not None else "n/a"
                r4_s = f"{(btc_r4h * 100):+.2f}%" if btc_r4h is not None else "n/a"
                reason = f"FAST_BTC_SQUEEZE_CUT (btc 1h {r1_s} / 4h {r4_s}, pnl {(cur_pnl * 100):+.2f}%)"
            elif (
                eth_sqz_hit
                and is_sqz_mode
                and _is_unprotected_sqz_stage(exit_mode, stage)
                and held_h is not None
                and held_h >= 0.25
                and cur_pnl is not None
                and cur_pnl < Decimal("0")
            ):
                e15_s = f"{(eth_r15m * 100):+.2f}%" if eth_r15m is not None else "n/a"
                e_thr_lbl = _sqz_eth_15m_threshold(exit_mode) or V18_AA_SQZ_ETH_15M
                reason = f"FAST_ETH_SQUEEZE_CUT (eth 15m {e15_s} >= +{e_thr_lbl * 100:.1f}%, pnl {(cur_pnl * 100):+.2f}%)"
            elif (
                b1h_sqz_hit
                and is_sqz_mode
                and _is_unprotected_sqz_stage(exit_mode, stage)
                and held_h is not None
                and held_h >= 0.25
                and cur_pnl is not None
                and cur_pnl < Decimal("0")
            ):
                b1h_s = f"{(live_b1h * 100):.1f}%" if live_b1h is not None else "n/a"
                b_thr_lbl = _sqz_b1h_threshold(exit_mode) or V12_AA_SQZ_B1H
                reason = f"FAST_B1H_SQUEEZE_CUT (b1h {b1h_s} >= {b_thr_lbl * 100:.0f}% [{b1h_valid} syms], pnl {(cur_pnl * 100):+.2f}%)"
        if reason is None and held_h is not None:
            if is_runner_mode:
                if held_h >= runner_max_h:
                    reason = f"FAST_MAX_HOLD_{runner_max_h}H ({held_h:.1f}h)"
                elif held_h >= MAX_HOLD_HOURS and not runner_unlocked and cur_pnl is not None:
                    if cur_pnl < V4_RUNNER_MIN_PNL:
                        reason = f"FAST_MAX_HOLD_{MAX_HOLD_HOURS}H ({held_h:.1f}h, pnl {(cur_pnl * 100):.2f}%)"
                    else:
                        promote_ids.append(pid)
            elif held_h >= MAX_HOLD_HOURS:
                reason = f"FAST_MAX_HOLD_{MAX_HOLD_HOURS}H ({held_h:.1f}h)"

        if reason:
            breaches.append({
                "id": pid,
                "symbol": sym,
                "reason": reason,
                "mark": str(mark),
                "stop_px": str(stop_px) if stop_px is not None else None,
                "tp_px": str(tp_px) if tp_px is not None else None,
            })
    return breaches, promote_ids


def _execute_close_batch(items, now_utc=None):
    """Parallel emergency close for >=2 simultaneous breaches (cuts squeeze exit latency from 40-90s to ~3s)."""
    now_utc = now_utc or utcnow()
    today_iso = now_utc.astimezone(dt.timezone.utc).date().isoformat()
    pos_meta_before = read_json(POS_META_PATH, default={})

    def _post_one(item):
        pid_i = (item["id"] or "").strip()
        why_i = (item["why"] or "").strip()[:180] or "Antigravity exit"
        try:
            res_i = binance.post("/close", {"id": pid_i, "why": why_i})
            return pid_i, why_i, res_i, None
        except Exception as e:  # noqa: BLE001
            return pid_i, why_i, None, safe_err(e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), 6)) as ex:
        raw_results = list(ex.map(_post_one, items))

    if any(r is not None and r.get("status") == "unclear" for _, _, r, _ in raw_results):
        time.sleep(1.5)
    with contextlib.suppress(Exception):
        binance.post("/sync", {"run_id": today_iso})
    pf_after = binance.save_portfolio(binance.fetch_portfolio())
    pf_ok = bool(pf_after) and not pf_after.get("error")

    still_open_ids = {s.get("id") for s in (pf_after.get("shorts") or []) if s.get("on_binance") and s.get("id")} if pf_ok else set()
    pos_meta_now = read_json(POS_META_PATH, default={})
    meta_dirty = False
    cutoff = now_utc - dt.timedelta(minutes=5)
    out_rows = []
    for item, (pid, why, res, err) in zip(items, raw_results):
        meta_hit = pos_meta_before.get(pid) or {}
        closed_sym = item.get("symbol") or meta_hit.get("symbol")
        if not closed_sym and "-" in pid:
            closed_sym = normalize_symbol(pid.rsplit("-", 1)[-1])
        if err is not None or res is None:
            _append_journal("close_error", {"id": pid, "symbol": closed_sym, "why": why, "error": err or "close_failed"}, now_utc)
            out_rows.append((False, {"ok": False, "id": pid, "symbol": closed_sym, "error": err or "close_failed", "why": why}))
            continue
        raw_ok = res.get("status") in ("placed", "found", "tested", "unclear")
        still_on = True if not pf_ok else (pid in still_open_ids)
        accepted = raw_ok and (res.get("status") == "tested" or (pf_ok and not still_on))
        if accepted and pid in pos_meta_now:
            closed_sym = pos_meta_now[pid].get("symbol") or closed_sym
            pos_meta_now.pop(pid, None)
            meta_dirty = True
        closed_rec = None
        for row in pf_after.get("closed") or []:
            if closed_sym and row.get("symbol") != closed_sym:
                continue
            try:
                if parse_iso_utc(row.get("closed_at") or "") >= cutoff:
                    closed_rec = row
                    break
            except (TypeError, ValueError):
                continue
        pnl_usdt = closed_rec.get("pnl_usdt") if closed_rec else None
        close_err = None if accepted else safe_err(
            pf_after.get("error") if not pf_ok else (res.get("why") or res.get("line") or res.get("status") or "close_unconfirmed")
        )
        side_b = str(meta_hit.get("side") or "SHORT").upper()
        _append_journal("close" if accepted else "close_unclear", {
            "id": pid,
            "symbol": closed_sym,
            "side": side_b,
            "setup": meta_hit.get("setup"),
            "exit_mode": meta_hit.get("exit_mode"),
            "ratchet_stage": meta_hit.get("ratchet_stage"),
            "mfe_pct": meta_hit.get("mfe_pct"),
            "mae_pct": meta_hit.get("mae_pct"),
            "pnl_usdt": pnl_usdt,
            "still_on_binance": still_on,
            "why": why,
            **({"error": close_err} if close_err else {}),
            "proxy": res,
            "closed_record": closed_rec,
        }, now_utc)
        _append_chat_line(
            "Antigravity",
            f"[{today_iso}] ปิด {side_b} {closed_sym or pid} ({res.get('status')}, {pnl_usdt if pnl_usdt is not None else 'n/a'} USDT): {why} | {res.get('line')}",
            now_utc,
        )
        out_rows.append((accepted, {
            "ok": accepted,
            "id": pid,
            "symbol": closed_sym,
            "pnl_usdt": pnl_usdt,
            "still_on_binance": still_on,
            **({"error": close_err} if close_err else {}),
            "why": why,
            "proxy": res,
        }))

    if meta_dirty:
        write_json_atomic(POS_META_PATH, pos_meta_now)
    state = read_json(STATE_PATH, default={})
    equity = pf_after.get("equity_usdt") if pf_ok else None
    new_state, _ = evaluate_circuit_breaker(state, equity, now_utc.isoformat())
    write_json_atomic(STATE_PATH, new_state)
    return out_rows


def _close_fast_breaches(breaches, now_utc, dry_run=False):
    """Close breached SHORT positions immediately under LOCK_PATH (parallel batch when >=2 simultaneous breaches)."""
    if not breaches:
        return []
    with file_lock(LOCK_PATH, blocking=True) as got:
        if not got:
            return [{"skipped": "lock_busy"}]
        pf = read_json(binance.PORTFOLIO, default={})
        active_ids = {p.get("id") for p in (pf.get("shorts") or []) if p.get("on_binance") and p.get("id")}
        targets = []
        for b in breaches:
            pid = b.get("id")
            if pid not in active_ids:
                continue
            targets.append({"id": pid, "symbol": b.get("symbol"), "why": f"FAST_GUARD: {b.get('reason') or 'BREACH'}"})
        if not targets:
            return []
        closed = []
        actions = []
        if dry_run:
            for t in targets:
                closed.append({"id": t["id"], "symbol": t["symbol"], "dry_run": True, "why": t["why"]})
            return closed
        if len(targets) == 1:
            t0 = targets[0]
            ok, res, _ = _execute_close(t0["id"], t0["why"], now_utc=now_utc)
            results = [(ok, {**res, "symbol": t0["symbol"], "why": t0["why"]})]
        else:
            results = _execute_close_batch(targets, now_utc=now_utc)
        for ok, res in results:
            sym = res.get("symbol")
            why = res.get("why")
            closed.append({
                "id": res.get("id"),
                "symbol": sym,
                "ok": ok,
                "pnl_usdt": res.get("pnl_usdt"),
                "why": why,
                **({"error": res.get("error")} if not ok and res.get("error") else {}),
            })
            if ok:
                actions.append(f"FAST_GUARD ปิด {sym} ({res.get('pnl_usdt')} USDT): {why}")
            else:
                actions.append(f"FAST_GUARD ปิดไม่สำเร็จ {sym} (รอตรวจ): {why}")
        if closed:
            ok_cnt = sum(1 for c in closed if c.get("ok"))
            last_reg = _extract_last_round_regime(_recent_journal(40)) or "FAST_GUARD_EXIT"
            hdr = f"FAST_GUARD ปิดทันที {ok_cnt}/{len(closed)} ไม้" if ok_cnt else f"FAST_GUARD ปิดไม่สำเร็จ {len(closed)} ไม้"
            summary = f"{hdr}: " + "; ".join(actions)[:230]
            _write_round_report(last_reg, summary, actions=actions, now_utc=now_utc, preserve_plan_if_no_picks=True)
        return closed


def _btc_ref_is_fresh(btc_ref, now_utc):
    if not isinstance(btc_ref, dict) or not btc_ref.get("at"):
        return False
    try:
        now_u = now_utc.astimezone(dt.timezone.utc)
        age_s = abs((now_u - parse_iso_utc(btc_ref["at"])).total_seconds())
        if age_s > BTC_REF_MAX_AGE_SEC:
            return False
        # V17: refresh right after a new 15m BTC bar closes (+15s buffer, 30s throttle) so BTC15_18 never goes blind
        last_bar_ms = btc_ref.get("last_bar_ms")
        if age_s >= 30.0 and last_bar_ms is not None:
            now_ms = int(now_u.timestamp() * 1000)
            if now_ms >= int(last_bar_ms) + 1_815_000:
                return False
        return True
    except (TypeError, ValueError):
        return False


def _eth_ref_is_fresh(btc_ref, now_utc):
    if not isinstance(btc_ref, dict) or not btc_ref.get("eth_at") or not isinstance(btc_ref.get("eth_closes_15m"), dict):
        return False
    try:
        now_u = now_utc.astimezone(dt.timezone.utc)
        age_s = abs((now_u - parse_iso_utc(btc_ref["eth_at"])).total_seconds())
        if age_s > BTC_REF_MAX_AGE_SEC:
            return False
        # V18: refresh right after a new 15m ETH bar closes (+15s buffer, 30s throttle) so ETH15_20 never goes blind
        last_bar_ms = btc_ref.get("eth_last_bar_ms")
        if age_s >= 30.0 and last_bar_ms is not None:
            now_ms = int(now_u.timestamp() * 1000)
            if now_ms >= int(last_bar_ms) + 1_815_000:
                return False
        return True
    except (TypeError, ValueError):
        return False


def _b1h_ref_is_fresh(btc_ref, now_utc):
    if not isinstance(btc_ref, dict):
        return False
    ema_map = btc_ref.get("ema20_1h_map")
    if not isinstance(ema_map, dict) or len(ema_map) < B1H_EMA_MIN_SYMS:
        return False
    at_s = btc_ref.get("ema20_1h_at") or btc_ref.get("at")
    if not at_s:
        return False
    try:
        age_s = abs((now_utc.astimezone(dt.timezone.utc) - parse_iso_utc(at_s)).total_seconds())
        return age_s <= B1H_EMA_REF_MAX_AGE_SEC
    except (TypeError, ValueError):
        return False


def _fast_guard_deadline_sec(duration, now_utc):
    """Compute wall-clock-aligned loop budget so 1-min cron never overruns into the next minute's lock."""
    dur = max(0.0, float(duration))
    if dur <= 60.0:
        sec_in_min = now_utc.second + now_utc.microsecond / 1_000_000.0
        rem_in_min = max(0.0, 55.5 - sec_in_min)
        return min(dur, rem_in_min)
    return dur


def _fast_guard_next_sleep(step_sec, rem_sec, now_ts=None):
    """Align Fast-Guard sleep to wall-clock `step_sec` boundaries (+0.05s) so 10s slots fire cleanly at :00/:10/:20/:30/:40/:50."""
    step_f = max(1.0, float(step_sec))
    ts = time.time() if now_ts is None else float(now_ts)
    wait = (step_f - (ts % step_f)) + 0.05
    if wait < 0.15:
        wait = 0.15
    return max(0.0, min(wait, float(rem_sec)))


def _write_guard_heartbeat(now_utc, btc_price=None, open_heat_usdt=None, cb_summary=None):
    """Write lightweight 5s/10s Fast-Guard heartbeat with live BTC mark, open heat, config & Auto Safe CB for the Monitor console."""
    prev = read_json(GUARD_HEARTBEAT_PATH, default={}) if (btc_price is None or open_heat_usdt is None) else {}
    st_cur = read_json(STATE_PATH, default={}) if cb_summary is None else {}
    payload = {
        "at": now_utc.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        "ok": True,
        "strategy": STRATEGY_VERSION,
        **_config_cb_fields(cb_summary=cb_summary, state=st_cur),
    }
    btc_val = str(btc_price) if btc_price is not None else prev.get("btc_price")
    heat_val = str(open_heat_usdt) if open_heat_usdt is not None else prev.get("open_heat_usdt")
    if btc_val is not None:
        payload["btc_price"] = btc_val
    if heat_val is not None:
        payload["open_heat_usdt"] = heat_val
    with contextlib.suppress(OSError):
        write_json_atomic(GUARD_HEARTBEAT_PATH, payload)


def _sync_live_pos_meta_and_heat(pf, marks_map=None):
    """Update live MFE/MAE/ratchet SL in POS_META_PATH from current marks (Decimal math) and return live open heat USDT."""
    pos_meta = read_json(POS_META_PATH, default={})
    dirty = False
    for p in (pf or {}).get("shorts") or []:
        if not p.get("on_binance"):
            continue
        pid = p.get("id")
        if not pid or pid not in pos_meta:
            continue
        meta = pos_meta[pid]
        entry = dec(p.get("entry"))
        sym = p.get("symbol")
        # Prefer exact Binance /fapi/v3/positionRisk markPrice from portfolio.json so MFE/MAE matches row display 1:1
        mark = dec(p.get("mark"))
        if (mark is None or mark <= Decimal("0")) and sym and marks_map:
            mark = dec(marks_map.get(sym))
        if entry is None or entry <= Decimal("0") or mark is None or mark <= Decimal("0"):
            continue
        is_long = str(p.get("side") or meta.get("side") or "SHORT").upper() == "LONG"
        cur_pnl = ((mark - entry) / entry) if is_long else ((entry - mark) / entry)
        prev_mfe = dec(meta.get("mfe_pct"), Decimal("0"))
        prev_mae = dec(meta.get("mae_pct"), Decimal("0"))
        mfe = cur_pnl if cur_pnl > prev_mfe else prev_mfe
        mae = cur_pnl if cur_pnl < prev_mae else prev_mae
        stop_pct = dec(meta.get("stop_pct"), DEFAULT_STOP_PCT)
        init_stop = entry * (Decimal("1") - stop_pct) if is_long else entry * (Decimal("1") + stop_pct)
        cur_stop = dec(meta.get("stop_px"), init_stop)
        cur_stage = meta.get("ratchet_stage") or "INIT"
        exit_mode = str(meta.get("exit_mode") or (LONG_EXIT_MODE if is_long else DEFAULT_EXIT_MODE))
        if is_long:
            new_stop, new_stage = _ratchet_stop_v22_long(entry, mfe, cur_stop, cur_stage)
        elif "NEW7" in exit_mode:
            new_stop, new_stage = _ratchet_stop_v9_new7(entry, mfe, cur_stop, cur_stage)
        elif exit_mode != "LEGACY":
            new_stop, new_stage = _ratchet_stop_v11_aa(entry, mfe, cur_stop, cur_stage)
        else:
            new_stop, new_stage = cur_stop, cur_stage
        if mfe != prev_mfe or mae != prev_mae or new_stop != cur_stop or new_stage != cur_stage:
            meta["mfe_pct"] = f"{mfe:.6f}"
            meta["mae_pct"] = f"{mae:.6f}"
            meta["stop_px"] = str(new_stop)
            meta["ratchet_stage"] = new_stage
            dirty = True
    if dirty:
        with contextlib.suppress(OSError):
            write_json_atomic(POS_META_PATH, pos_meta)
    open_heat_u, _ = compute_open_heat_and_counts(pf or {}, pos_meta)
    return open_heat_u


def _process_console_requests(now_utc, dry_run=False):
    """Consume manual close (`close-bto-...`) and `portfolio` refresh requests dropped by the Monitor console."""
    try:
        names = sorted(os.listdir(REQUESTS_DIR))
    except OSError:
        return {"closed": [], "portfolio_refreshed": False}
    if not names:
        return {"closed": [], "portfolio_refreshed": False}

    close_ids = []
    want_portfolio = False
    for name in names:
        fpath = os.path.join(REQUESTS_DIR, name)
        if not os.path.isfile(fpath):
            continue
        if name == "portfolio":
            if not dry_run:
                with contextlib.suppress(OSError):
                    os.unlink(fpath)
            want_portfolio = True
            continue
        m = _CLOSE_REQ_RE.match(name)
        if m:
            if not dry_run:
                with contextlib.suppress(OSError):
                    os.unlink(fpath)
            close_ids.append(m.group(1))

    if not close_ids and not want_portfolio:
        return {"closed": [], "portfolio_refreshed": False}

    closed_out = []
    pf_refreshed = False
    with file_lock(LOCK_PATH, blocking=True) as got:
        if not got:
            return {"closed": [], "portfolio_refreshed": False}
        if close_ids:
            pf = read_json(binance.PORTFOLIO, default={})
            by_id = {p.get("id"): p for p in (pf.get("shorts") or []) if p.get("on_binance") and p.get("id")}
            targets = [
                {"id": pid, "symbol": by_id[pid].get("symbol"), "why": "MANUAL_CLOSE: ปิดจากหน้าเว็บ Console"}
                for pid in close_ids
                if pid in by_id
            ]
            if targets:
                if dry_run:
                    closed_out = [{"id": t["id"], "symbol": t["symbol"], "dry_run": True, "why": t["why"]} for t in targets]
                else:
                    if len(targets) == 1:
                        t0 = targets[0]
                        ok, res, _ = _execute_close(t0["id"], t0["why"], now_utc=now_utc)
                        results = [(ok, {**res, "symbol": t0["symbol"], "why": t0["why"]})]
                    else:
                        results = _execute_close_batch(targets, now_utc=now_utc)
                    actions = []
                    for ok, res in results:
                        sym = res.get("symbol")
                        why = res.get("why")
                        closed_out.append({
                            "id": res.get("id"),
                            "symbol": sym,
                            "ok": ok,
                            "pnl_usdt": res.get("pnl_usdt"),
                            "why": why,
                            **({"error": res.get("error")} if not ok and res.get("error") else {}),
                        })
                        if ok:
                            actions.append(f"ปิดมือ {sym} ({res.get('pnl_usdt')} USDT)")
                        else:
                            actions.append(f"ปิดมือไม่สำเร็จ {sym}")
                    ok_cnt = sum(1 for c in closed_out if c.get("ok"))
                    last_reg = _extract_last_round_regime(_recent_journal(40)) or "MANUAL_CLOSE"
                    hdr = f"ปิดมือจากหน้าเว็บ {ok_cnt}/{len(closed_out)} ไม้"
                    _write_round_report(last_reg, f"{hdr}: " + "; ".join(actions)[:220], actions=actions, now_utc=now_utc, preserve_plan_if_no_picks=True)
                    pf_after_c = read_json(binance.PORTFOLIO, default={})
                    heat_after_c = _sync_live_pos_meta_and_heat(pf_after_c)
                    _write_guard_heartbeat(now_utc, open_heat_usdt=heat_after_c)
                    print(_dump_json({"ok": True, "event": "console_manual_close", "at_th": f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M:%S}", "closed": closed_out}))
        if want_portfolio and not closed_out:
            if dry_run:
                pf_refreshed = True
            else:
                with contextlib.suppress(Exception):
                    pf_now = binance.save_portfolio(binance.fetch_portfolio())
                    if pf_now and not pf_now.get("error"):
                        st = read_json(STATE_PATH, default={})
                        new_st, cb_now = evaluate_circuit_breaker(st, pf_now.get("equity_usdt"), now_utc.isoformat())
                        write_json_atomic(STATE_PATH, new_st)
                        marks_live = None
                        with contextlib.suppress(Exception):
                            marks_live = fetch_mark_prices()
                        live_heat = _sync_live_pos_meta_and_heat(pf_now, marks_map=marks_live)
                        _write_guard_heartbeat(now_utc, btc_price=(marks_live or {}).get("BTCUSDT"), open_heat_usdt=live_heat, cb_summary=cb_now)
                        pf_refreshed = True
    return {"closed": closed_out, "portfolio_refreshed": pf_refreshed}


def _refresh_portfolio_slot_10s(now_utc, last_slot, dry_run=False):
    """Wall-clock 10-second (:00/:10/:20/:30/:40/:50) portfolio + live MFE/SL/Heat/BTC/Auto-CB refresh on server so all Console screens stay synced."""
    slot_10 = int(now_utc.timestamp()) // 10
    if dry_run or slot_10 == last_slot:
        return last_slot, False
    try:
        if os.path.isfile(binance.PORTFOLIO) and (int(os.path.getmtime(binance.PORTFOLIO)) // 10) >= slot_10:
            pf_cur = read_json(binance.PORTFOLIO, default={})
            marks_cur = None
            with contextlib.suppress(Exception):
                marks_cur = fetch_mark_prices()
            live_heat = _sync_live_pos_meta_and_heat(pf_cur, marks_map=marks_cur)
            cb_cur = None
            if pf_cur and not pf_cur.get("error") and pf_cur.get("equity_usdt"):
                st_cur = read_json(STATE_PATH, default={})
                _, cb_cur = evaluate_circuit_breaker(st_cur, pf_cur.get("equity_usdt"), now_utc.isoformat())
            _write_guard_heartbeat(now_utc, btc_price=(marks_cur or {}).get("BTCUSDT"), open_heat_usdt=live_heat, cb_summary=cb_cur)
            return slot_10, False
    except OSError:
        pass
    with file_lock(LOCK_PATH, blocking=False) as got:
        if not got:
            return last_slot, False
        refreshed = False
        live_heat = None
        marks_live = None
        cb_now = None
        with contextlib.suppress(Exception):
            marks_live = fetch_mark_prices()
        with contextlib.suppress(Exception):
            pf_now = binance.save_portfolio(binance.fetch_portfolio())
            if pf_now and not pf_now.get("error"):
                st = read_json(STATE_PATH, default={})
                new_st, cb_now = evaluate_circuit_breaker(st, pf_now.get("equity_usdt"), now_utc.isoformat())
                write_json_atomic(STATE_PATH, new_st)
                live_heat = _sync_live_pos_meta_and_heat(pf_now, marks_map=marks_live)
                refreshed = True
        _write_guard_heartbeat(now_utc, btc_price=(marks_live or {}).get("BTCUSDT"), open_heat_usdt=live_heat, cb_summary=cb_now)
        return slot_10, refreshed


_refresh_portfolio_slot_30s = _refresh_portfolio_slot_10s


def cmd_fast_guard(duration=55, interval=5, dry_run=False):
    """Ultra-low-latency 5-second Wall-Clock-Aligned Mark-Price & Quad-Channel Squeeze Guardian (for `* * * * *` 1-min cron).

    - On every 5-min boundary except `:00` (where `auto-trade` runs Exit-First), runs full `cmd_guard`.
    - Every wall-clock 10 seconds (`:00/:10/:20/:30/:40/:50`), refreshes `cache/portfolio.json` once on the server so any number of
      open Monitor browser tabs receive fresh Binance prices/PnL without colliding or spamming Proxy requests.
    - Every `interval` seconds (default 5s), polls Binance `premiumIndex` (1 HTTP call) and immediately closes
      any SL/TP/time/BTC-squeeze/B1H-squeeze breach directly (parallel batch when >=2), using persistent
      cross-process exponential close-failure backoff (`agy-close-backoff.json`).
    - Also consumes manual close (`cache/requests/close-bto-...`) and on-demand portfolio refresh (`cache/requests/portfolio`)
      dropped by the Monitor web console every 5 seconds, and updates `cache/agy-guard-heartbeat.json`.
    """
    with file_lock(FAST_GUARD_LOCK_PATH, blocking=False) as got:
        if not got:
            return 0

        start_mono = time.monotonic()
        now0 = utcnow()
        if not dry_run:
            _write_guard_heartbeat(now0)
        end_mono = start_mono + _fast_guard_deadline_sec(duration, now0)
        if now0.minute % 5 == 0 and now0.minute != 0:
            if now0.minute % 15 == 0 and now0.second < 2 and not dry_run:
                time.sleep(2.0 - now0.second)
            cmd_guard(dry_run=dry_run)

        step = max(5, int(interval))
        # V13/V14 Fix: stay alive during BOTH :00 and :01 so positions opened after auto-trade wait-retry get immediate 15s guard
        keep_alive_open_window = (now0.minute in (0, 1))
        top_hour_backup_done = False
        missing_entry_synced = 0
        b1h_refresh_tried = False
        last_pf_slot_10 = None
        while True:
            now_u = utcnow()
            req_res = _process_console_requests(now_u, dry_run=dry_run)
            if req_res.get("portfolio_refreshed") or req_res.get("closed"):
                last_pf_slot_10 = int(now_u.timestamp()) // 10
            else:
                last_pf_slot_10, _ = _refresh_portfolio_slot_10s(now_u, last_pf_slot_10, dry_run=dry_run)
            pf = read_json(binance.PORTFOLIO, default={})
            if keep_alive_open_window and not top_hour_backup_done and not dry_run and now_u.second >= 45:
                top_hour_backup_done = True
                pf_stale = True
                with contextlib.suppress(OSError):
                    pf_stale = (time.time() - os.path.getmtime(binance.PORTFOLIO)) > 60.0
                if pf_stale:
                    cmd_guard(dry_run=False)
                    pf = read_json(binance.PORTFOLIO, default={})

            active_shorts = [p for p in (pf.get("shorts") or []) if p.get("on_binance")]
            if not active_shorts:
                if dry_run:
                    break
                rem = end_mono - time.monotonic()
                if rem <= 1.0:
                    break
                time.sleep(_fast_guard_next_sleep(step, rem))
                continue
            # V14/V21 Fix: if any live short is missing entry (post-open /sync lagged), retry sync until entry arrives (max 2 attempts if lock was busy)
            if not dry_run and missing_entry_synced < 2 and any(not dec(p.get("entry")) for p in active_shorts):
                cmd_guard(dry_run=False)
                pf = read_json(binance.PORTFOLIO, default=pf)
                active_shorts = [p for p in (pf.get("shorts") or []) if p.get("on_binance")]
                if not any(not dec(p.get("entry")) for p in active_shorts):
                    missing_entry_synced = 2
                else:
                    missing_entry_synced += 1
                if not active_shorts:
                    continue
            pos_meta = read_json(POS_META_PATH, default={})
            btc_ref = read_json(BTC_REF_PATH, default=None)
            need_btc_ref = (
                not _btc_ref_is_fresh(btc_ref, now_u)
                and any(_is_sqz_mode((pos_meta.get(p.get("id")) or {}).get("exit_mode")) for p in active_shorts)
            )
            need_eth_ref = (
                not _eth_ref_is_fresh(btc_ref, now_u)
                and any(_sqz_eth_15m_threshold((pos_meta.get(p.get("id")) or {}).get("exit_mode")) is not None for p in active_shorts)
            )
            if need_btc_ref or need_eth_ref:
                ref_updates = {}
                if need_btc_ref and need_eth_ref:
                    with contextlib.suppress(Exception):
                        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ref_ex:
                            f_btc = ref_ex.submit(fetch_klines_15m, "BTCUSDT", 24)
                            f_eth = ref_ex.submit(fetch_klines_15m, "ETHUSDT", 12)
                            with contextlib.suppress(Exception):
                                _, fresh_btc = _btc_squeeze_returns_from_bars(f_btc.result(), now_u)
                                if fresh_btc:
                                    ref_updates.update(fresh_btc)
                            with contextlib.suppress(Exception):
                                _, fresh_eth = _eth_15m_ref_from_bars(f_eth.result(), now_u)
                                if fresh_eth:
                                    ref_updates.update(fresh_eth)
                elif need_btc_ref:
                    with contextlib.suppress(Exception):
                        _, fresh_btc = _btc_squeeze_returns_from_bars(fetch_klines_15m("BTCUSDT", limit=24), now_u)
                        if fresh_btc:
                            ref_updates.update(fresh_btc)
                elif need_eth_ref:
                    with contextlib.suppress(Exception):
                        _, fresh_eth = _eth_15m_ref_from_bars(fetch_klines_15m("ETHUSDT", limit=12), now_u)
                        if fresh_eth:
                            ref_updates.update(fresh_eth)
                if ref_updates:
                    btc_ref = _save_btc_ref_merged(ref_updates) or {**(btc_ref or {}), **ref_updates}
            if (
                not b1h_refresh_tried
                and not _b1h_ref_is_fresh(btc_ref, now_u)
                and any(_sqz_b1h_threshold((pos_meta.get(p.get("id")) or {}).get("exit_mode")) is not None for p in active_shorts)
            ):
                b1h_refresh_tried = True
                with contextlib.suppress(Exception):
                    refreshed_b1h = _refresh_b1h_ema_ref_from_ch(now_u)
                    if refreshed_b1h:
                        btc_ref = refreshed_b1h
            promote_ids = []
            try:
                marks = fetch_mark_prices()
                breaches, promote_ids = check_fast_guard_events(pos_meta, pf, marks, now_u, btc_ref=btc_ref)
            except Exception as e:  # noqa: BLE001
                print(_dump_json({"ok": False, "event": "fast_guard_err", "at_th": f"{now_u.astimezone(TH):%Y-%m-%d %H:%M:%S}", "error": safe_err(e)}))
                breaches = []
            if breaches:
                backoff_map = _load_close_backoff(now_u) if not dry_run else {}
                eligible_breaches = [b for b in breaches if b.get("id") not in backoff_map]
                if eligible_breaches:
                    closed_fast = _close_fast_breaches(eligible_breaches, now_u, dry_run=dry_run)
                    if not dry_run:
                        _update_close_backoff(closed_fast, now_u)
                    print(_dump_json({"ok": True, "event": "fast_guard_trigger", "at_th": f"{now_u.astimezone(TH):%Y-%m-%d %H:%M:%S}", "breaches": eligible_breaches, "closed": closed_fast}))
                if dry_run:
                    break
            elif promote_ids and not dry_run:
                # Immediately lock RUNNER96 profit stop at 24h without waiting up to 5 min for next cmd_guard
                cmd_guard(dry_run=False)
            rem = end_mono - time.monotonic()
            if rem <= 1.0:
                break
            time.sleep(_fast_guard_next_sleep(step, rem))
        return 0


def check_clickhouse_15m_ready(now_utc, min_ratio=0.95, min_syms=100):
    """Verify ClickHouse `altcoin.ohlcv` has ingested >= `min_ratio` of active futures symbols for the latest closed 15m bar."""
    now_u = now_utc.astimezone(dt.timezone.utc)
    cur_15m = now_u.replace(minute=(now_u.minute // 15) * 15, second=0, microsecond=0)
    tgt_dt = cur_15m - dt.timedelta(minutes=15)
    prev_dt = tgt_dt - dt.timedelta(minutes=15)
    tgt_s = f"{tgt_dt:%Y-%m-%d %H:%M}:00"
    prev_s = f"{prev_dt:%Y-%m-%d %H:%M}:00"
    try:
        rows = list(market.ch(
            """SELECT
                   toString(uniqExactIf(symbol, open_time = toDateTime({prev_t:String}))),
                   toString(uniqExactIf(symbol, open_time = toDateTime({cur_t:String})))
               FROM altcoin.ohlcv
               WHERE market='um_futures' AND interval='15m'
                 AND open_time IN (toDateTime({prev_t:String}), toDateTime({cur_t:String}))""",
            prev_t=prev_s,
            cur_t=tgt_s,
        ))
        prev_cnt = int(rows[0][0]) if rows and len(rows[0]) >= 2 else 0
        cur_cnt = int(rows[0][1]) if rows and len(rows[0]) >= 2 else 0
    except Exception as e:  # noqa: BLE001
        return {"ready": False, "cur_cnt": 0, "prev_cnt": 0, "pct": 0.0, "target_15m": tgt_s, "error": safe_err(e)}
    pct = (cur_cnt / prev_cnt * 100.0) if prev_cnt > 0 else 0.0
    ready = bool(prev_cnt > 0 and cur_cnt >= int(min_syms) and (cur_cnt / prev_cnt) >= float(min_ratio))
    return {"ready": ready, "cur_cnt": cur_cnt, "prev_cnt": prev_cnt, "pct": round(pct, 1), "target_15m": tgt_s}


def wait_for_clickhouse_15m(now_utc, max_wait_sec=180, poll_sec=15, min_ratio=0.95, min_syms=100, dry_run=False):
    """Wait until ClickHouse latest closed 15m bar is >= `min_ratio` complete before scanning/opening trades."""
    t0 = time.monotonic()
    chk = check_clickhouse_15m_ready(now_utc, min_ratio=min_ratio, min_syms=min_syms)
    if dry_run or chk["ready"] or max_wait_sec <= 0:
        return {**chk, "waited_sec": 0}
    while not chk["ready"]:
        elapsed = time.monotonic() - t0
        rem = float(max_wait_sec) - elapsed
        if rem <= 1.0:
            break
        time.sleep(min(float(poll_sec), rem))
        chk = check_clickhouse_15m_ready(utcnow(), min_ratio=min_ratio, min_syms=min_syms)
    return {**chk, "waited_sec": int(round(time.monotonic() - t0))}


def cmd_auto_trade(max_opens=3, dry_run=False):
    """Deterministic hourly V21 trader: 2-phase non-blocking lock (Parallel Exit-First -> Unlocked Scan -> Locked Open)."""
    now_utc = utcnow()
    actions = []
    closed_out = []
    held_bars_15m = {}

    # Phase 1 (locked ~2s): Exit-First sync & close any MUST_CLOSE positions immediately BEFORE market scan (parallel batch >=2)
    with file_lock(LOCK_PATH, blocking=True) as got:
        if not got:
            print(_dump_json({"ok": True, "skipped": "lock_busy"}))
            return 0
        pf, sync_out, state, cb = _sync_and_portfolio(now_utc, do_sync=True)
        if pf.get("error"):
            print(_dump_json({"ok": False, "error": safe_err(pf["error"]), "circuit_breaker": cb, "sync": sync_out}))
            return 1

        positions = _evaluate_open_positions(pf, now_utc, bars_out=held_bars_15m)
        backoff_map_at = _load_close_backoff(now_utc) if not dry_run else {}
        must_close = [
            pos for pos in positions
            if pos.get("action") == "MUST_CLOSE" and pos.get("id") not in backoff_map_at
        ]
        if must_close:
            if dry_run:
                for pos in must_close:
                    why = f"AUTO_V21: {pos.get('exit_reason') or 'MUST_CLOSE'}"
                    closed_out.append({"id": pos["id"], "symbol": pos["symbol"], "dry_run": True, "why": why})
                    actions.append(f"[DRY] ปิด {pos['symbol']}: {why}")
            elif len(must_close) == 1:
                pos = must_close[0]
                why = f"AUTO_V21: {pos.get('exit_reason') or 'MUST_CLOSE'}"
                ok, res, _ = _execute_close(pos["id"], why, now_utc=now_utc)
                closed_out.append({"id": pos["id"], "symbol": pos["symbol"], "ok": ok, "pnl_usdt": res.get("pnl_usdt"), "why": why, **({"error": res.get("error")} if not ok and res.get("error") else {})})
                if ok:
                    actions.append(f"ปิด {pos['symbol']} ({res.get('pnl_usdt')} USDT): {why}")
                else:
                    actions.append(f"ปิดไม่สำเร็จ {pos['symbol']} (รอตรวจ): {why}")
            else:
                batch_items = [
                    {"id": pos["id"], "symbol": pos["symbol"], "why": f"AUTO_V21: {pos.get('exit_reason') or 'MUST_CLOSE'}"}
                    for pos in must_close
                ]
                for ok, res in _execute_close_batch(batch_items, now_utc=now_utc):
                    sym_i = res.get("symbol")
                    why_i = res.get("why")
                    closed_out.append({"id": res.get("id"), "symbol": sym_i, "ok": ok, "pnl_usdt": res.get("pnl_usdt"), "why": why_i, **({"error": res.get("error")} if not ok and res.get("error") else {})})
                    if ok:
                        actions.append(f"ปิด {sym_i} ({res.get('pnl_usdt')} USDT): {why_i}")
                    else:
                        actions.append(f"ปิดไม่สำเร็จ {sym_i} (รอตรวจ): {why_i}")

        if closed_out and not dry_run:
            _update_close_backoff(closed_out, now_utc)
            pf = read_json(binance.PORTFOLIO, default=pf)
            state = read_json(STATE_PATH, default=state)
            _, cb = evaluate_circuit_breaker(state, pf.get("equity_usdt"), now_utc.isoformat())

    # Phase 2 (UNLOCKED): verify ClickHouse 15m data is complete FIRST (wait up to 3m if collector lags), then scan
    ch_check = {"ready": True, "cur_cnt": 0, "prev_cnt": 0, "pct": 100.0, "waited_sec": 0, "target_15m": None}
    try:
        ch_check = wait_for_clickhouse_15m(now_utc, max_wait_sec=180, poll_sec=15, min_ratio=0.95, min_syms=100, dry_run=dry_run)
        now_utc = utcnow()
        try:
            regime, candidates, _cooled, opened_today, _held_tech = _scan_market_and_candidates(
                pf, now_utc, held_bars_15m=held_bars_15m
            )
        except Exception:  # noqa: BLE001
            if dry_run or now_utc.minute >= 5:
                raise
            time.sleep(5)
            now_utc = utcnow()
            pf_retry = read_json(binance.PORTFOLIO, default=pf)
            regime, candidates, _cooled, opened_today, _held_tech = _scan_market_and_candidates(
                pf_retry, now_utc, held_bars_15m=held_bars_15m
            )
        retries = 0
        while (
            not dry_run
            and retries < 2
            and regime.get("funding_ok")
            and (
                (float(regime.get("complete_1h_pct") or 0.0) < 90.0 and now_utc.minute < 12)
                or (float(regime.get("complete_15m_pct") or 0.0) < 85.0 and (now_utc.minute % 15) < 10)
            )
        ):
            retries += 1
            time.sleep(25)
            now_utc = utcnow()
            pf_mid = read_json(binance.PORTFOLIO, default=pf)
            regime, candidates, _cooled, opened_today, _held_tech = _scan_market_and_candidates(
                pf_mid, now_utc, held_bars_15m=held_bars_15m
            )
    except Exception as e:  # noqa: BLE001
        err_msg = safe_err(e)
        if not dry_run:
            _append_journal("scan_error", {"error": err_msg, "closed": closed_out}, now_utc)
            if closed_out:
                summary_err = f"V21 SCAN_ERROR (ปิดไปแล้ว {sum(1 for x in closed_out if x.get('ok'))}/{len(closed_out)} ไม้): {err_msg}"[:280]
                _write_round_report("DATA_INCOMPLETE_HOLD", summary_err, actions=actions, now_utc=now_utc, preserve_plan_if_no_picks=True)
        print(_dump_json({"ok": False, "strategy": STRATEGY_VERSION, "error": err_msg, "closed": closed_out}))
        return 1

    # Phase 3 (locked ~2s): re-read latest portfolio/state/pos_meta (in case fast-guard acted during scan), open top setups
    with file_lock(LOCK_PATH, blocking=True) as got2:
        if not got2:
            print(_dump_json({"ok": True, "skipped": "lock_busy_phase3"}))
            return 0
        now_utc = utcnow()
        today_iso = now_utc.astimezone(dt.timezone.utc).date().isoformat()
        pf = read_json(binance.PORTFOLIO, default=pf)
        state = read_json(STATE_PATH, default=state)
        new_state_p3, cb = evaluate_circuit_breaker(state, pf.get("equity_usdt"), now_utc.isoformat())
        if not dry_run and pf.get("equity_usdt"):
            write_json_atomic(STATE_PATH, new_state_p3)
        opened_today_now = opened_today_utc(pf, today_iso)
        opened_hour_now = opened_this_hour_utc(pf, now_utc)
        cooled_now = cooldown_symbols(pf, now_utc) if ENFORCE_OPEN_COOLDOWN else set()
        # V14 Fix: also block symbols still awaiting Binance REALIZED_PNL settlement (any entry in pf["shorts"])
        held_now = set(pf.get("held_symbols") or ()) | {
            x["symbol"] for x in (pf.get("shorts") or []) if x.get("symbol")
        } | {c.get("symbol") for c in closed_out if c.get("symbol")}

        journal_tail = _recent_journal(40)
        last_round_regime = _extract_last_round_regime(journal_tail)
        regime_changed = bool(last_round_regime and last_round_regime != regime.get("regime_label"))
        daily_remaining = max(0, DAILY_MAX_OPENS - len(opened_today_now)) if DAILY_MAX_OPENS > 0 else None
        max_opens_i = int(max_opens or 0)
        hour_remaining = None if max_opens_i <= 0 else max(0, max_opens_i - len(opened_hour_now))
        open_slots_remaining = max(0, MAX_OPEN - len(held_now)) if MAX_OPEN > 0 else None
        pos_meta_p3 = read_json(POS_META_PATH, default={})
        cur_heat_u, setup_counts = compute_open_heat_and_counts(pf, pos_meta_p3)
        side_counts = count_open_by_side(pf, pos_meta_p3)

        reg_lbl = regime.get("regime_label")
        active_squeeze = bool(regime.get("active_squeeze")) if "active_squeeze" in regime else (reg_lbl == "BULLISH_SQUEEZE_CAUTION")
        active_crash = bool(regime.get("active_crash"))
        opened_out = []
        skipped_risk = []
        picks_rec = []
        ch_15m_ok = (
            bool(dry_run)
            or bool(ch_check.get("ready"))
            or float(ch_check.get("pct") or 0.0) >= 85.0
            or float(regime.get("complete_15m_pct") or 0.0) >= 85.0
        )
        can_open = (
            not cb.get("tripped")
            and bool(regime.get("funding_ok"))
            and ch_15m_ok
            and (daily_remaining is None or daily_remaining > 0)
        )
        if not ch_15m_ok:
            skipped_risk.append({
                "symbol": "ALL",
                "side": "BOTH",
                "setup": "CLICKHOUSE_WAIT",
                "reason": f"ch_15m_incomplete ({ch_check.get('cur_cnt')}/{ch_check.get('prev_cnt')} = {ch_check.get('pct')}%, waited {ch_check.get('waited_sec')}s)",
            })

        # V23 Parity: max_opens <= 0 & MAX_OPEN == 0 means unlimited (constrained only by available_usdt >= MIN_AVAIL_USDT)
        caps = [c for c in (hour_remaining, daily_remaining, open_slots_remaining) if c is not None]
        effective_max_opens = min(caps) if caps else None
        if can_open and (effective_max_opens is None or effective_max_opens > 0):
            flat_hits = []
            for cand in candidates:
                sym = cand["symbol"]
                c_side = str(cand.get("side") or "SHORT").upper()
                hit_specs = cand.get("hit_specs") or [{
                    "side": c_side,
                    "setup": cand["primary_setup"],
                    "score": cand["score"],
                    "stop_pct": cand["suggested_stop_pct"],
                    "tp_pct": cand["suggested_tp_pct"],
                    "exit_mode": cand.get("exit_mode"),
                }]
                for h in hit_specs:
                    flat_hits.append((float(h.get("score", cand["score"])), sym, cand, {**h, "side": str(h.get("side") or c_side).upper()}))
            flat_hits.sort(key=lambda item: (-item[0], item[1]))

            # V16.1: prefetch live spreads in parallel for top eligible candidates when live-opening >=2 candidates
            prefetched_spreads = {}
            if not dry_run:
                top_spread_syms = []
                seen_sp = set()
                prefetch_cap = 8 if effective_max_opens is None else min(effective_max_opens + 2, 8)
                for _sc, _sym, _c, _h in flat_hits:
                    if _sym in seen_sp or _sym in held_now or _sym in cooled_now or _sym in opened_today_now:
                        continue
                    seen_sp.add(_sym)
                    top_spread_syms.append(_sym)
                    if len(top_spread_syms) >= prefetch_cap:
                        break
                if len(top_spread_syms) >= 2:
                    def _get_sp(s):
                        try:
                            return s, fetch_live_spread(s)
                        except Exception:  # noqa: BLE001
                            return s, None
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(top_spread_syms)) as sp_ex:
                        for _s, _v in sp_ex.map(_get_sp, top_spread_syms):
                            if _v is not None:
                                prefetched_spreads[_s] = _v

            taken_syms = set()
            sim_avail_u = dec(pf.get("available_usdt"))
            for chosen_score, sym, cand, h in flat_hits:
                if effective_max_opens is not None and sum(1 for x in opened_out if x.get("ok") or x.get("dry_run")) >= effective_max_opens:
                    break
                if sym in taken_syms or sym in held_now or sym in cooled_now or sym in opened_today_now:
                    continue
                h_setup = h["setup"]
                h_side = str(h.get("side") or "SHORT").upper()
                # Per-Side Directional Shock & Regime Gates (optional; disabled when ENFORCE_REGIME_OPEN_GATE=False)
                if h_side == "SHORT":
                    if ENFORCE_REGIME_OPEN_GATE:
                        if active_squeeze:
                            skipped_risk.append({"symbol": sym, "side": h_side, "setup": h_setup, "reason": "active_squeeze"})
                            continue
                        # V20 Belt-and-Suspenders: when 65% < BD <= 85% (BULLISH_SQUEEZE_CAUTION without active squeeze), allow ONLY Bull-Moat setups
                        if reg_lbl == "BULLISH_SQUEEZE_CAUTION" and h_setup not in BULL_REGIME_SETUPS:
                            skipped_risk.append({"symbol": sym, "side": h_side, "setup": h_setup, "reason": "bull_breadth_gt65"})
                            continue
                    if MAX_SHORT_OPEN > 0 and side_counts["SHORT"] >= MAX_SHORT_OPEN:
                        skipped_risk.append({"symbol": sym, "side": h_side, "setup": h_setup, "reason": f"short_cap_{MAX_SHORT_OPEN}"})
                        continue
                else:
                    if ENFORCE_REGIME_OPEN_GATE and active_crash:
                        skipped_risk.append({"symbol": sym, "side": h_side, "setup": h_setup, "reason": "active_crash"})
                        continue
                    if MAX_LONG_OPEN > 0 and side_counts["LONG"] >= MAX_LONG_OPEN:
                        skipped_risk.append({"symbol": sym, "side": h_side, "setup": h_setup, "reason": f"long_cap_{MAX_LONG_OPEN}"})
                        continue
                h_sl = str(h["stop_pct"])
                h_tp = str(h["tp_pct"])
                if h_side == "LONG":
                    h_exit = h.get("exit_mode") or LONG_EXIT_MODE
                else:
                    h_exit = h.get("exit_mode") or (NEW7_EXIT_MODE if h_setup == "NEW7_BAS_1H" else DEFAULT_EXIT_MODE)
                h_risk_u = (LOT_USDT * dec(h_sl, DEFAULT_STOP_PCT)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                # Per-Engine Concurrency Cap (empty = unlimited)
                eng_cap = ENGINE_MAX_OPEN.get(h_setup)
                if eng_cap is not None and setup_counts.get(h_setup, 0) >= eng_cap:
                    skipped_risk.append({"symbol": sym, "side": h_side, "setup": h_setup, "reason": f"engine_cap_{eng_cap}"})
                    continue
                # Portfolio Heat Cap + Per-Engine Heat Ceiling (0 = unlimited)
                eff_heat_cap = None
                if MAX_OPEN_HEAT_USDT > Decimal("0"):
                    eff_heat_cap = min(MAX_OPEN_HEAT_USDT, ENGINE_HEAT_CAP[h_setup]) if h_setup in ENGINE_HEAT_CAP else MAX_OPEN_HEAT_USDT
                elif h_setup in ENGINE_HEAT_CAP and ENGINE_HEAT_CAP[h_setup] > Decimal("0"):
                    eff_heat_cap = ENGINE_HEAT_CAP[h_setup]
                if eff_heat_cap is not None and cur_heat_u + h_risk_u > eff_heat_cap:
                    skipped_risk.append({"symbol": sym, "setup": h_setup, "reason": f"heat_cap ({cur_heat_u}+{h_risk_u}>{eff_heat_cap})"})
                    continue

                setup, sl, tp, exit_mode, sl_risk_u = h_setup, h_sl, h_tp, h_exit, h_risk_u
                if setup == "SPOTDMP_4H":
                    tb_tag = f"stb4h={cand.get('spot_tb_4h')} tb4h={cand.get('tb_4h')} psv={cand.get('ps_vrat')}x"
                elif setup == "SPOTDMP_1H":
                    tb_tag = f"stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')} psv={cand.get('ps_vrat')}x"
                elif setup == "SPOTFLOW_POS_1H":
                    tb_tag = f"stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x ext={cand.get('ext_e10_1h_pct')}% tb1h={cand.get('tb_1h')}"
                elif setup in ("SP_LEAD_2H", "SPOTFLOW_POS_2H", "BULL_SPOTFLOW_2H"):
                    tb_tag = f"stb2h={cand.get('spot_tb_2h')} svr2h={cand.get('spot_vrat_2h')}x tb2h={cand.get('tb_2h')}"
                elif setup == "SPOTDMP_POS_2H":
                    tb_tag = f"stb2h={cand.get('spot_tb_2h')} svr2h={cand.get('spot_vrat_2h')}x tb2h={cand.get('tb_2h')} psv={cand.get('ps_vrat')}x"
                elif setup == "WHALESP_2H":
                    tb_tag = f"stsz2h={cand.get('spot_tsize_r_2h')}x stb2h={cand.get('spot_tb_2h')} svr2h={cand.get('spot_vrat_2h')}x tb2h={cand.get('tb_2h')}"
                elif setup == "WHALESP_4H":
                    tb_tag = f"stsz4h={cand.get('spot_tsize_r_4h')}x stb4h={cand.get('spot_tb_4h')} svr4h={cand.get('spot_vrat_4h')}x tb4h={cand.get('tb_4h')}"
                elif setup == "SPOTFLOW_POS_4H":
                    tb_tag = f"stb4h={cand.get('spot_tb_4h')} svr4h={cand.get('spot_vrat_4h')}x tb4h={cand.get('tb_4h')}"
                elif setup == "BASIMP_1H":
                    tb_tag = f"bas1h={cand.get('bas1h_pct')}% stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "BASFOMO_1H":
                    tb_tag = f"bas15={cand.get('bas15_pct')}% bas1h={cand.get('bas1h_pct')}% stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "EMAREJ_2H":
                    tb_tag = f"uw2h={cand.get('uwick_2h')} vr2h={cand.get('vrat_2h')}x tb2h={cand.get('tb_2h')}"
                elif setup == "SP_UWICK_1H":
                    tb_tag = f"spuw1h={cand.get('spot_uwick_1h')} stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "SP_UWICK_2H":
                    tb_tag = f"spuw2h={cand.get('spot_uwick_2h')} stb2h={cand.get('spot_tb_2h')} svr2h={cand.get('spot_vrat_2h')}x tb2h={cand.get('tb_2h')}"
                elif setup == "BLOWOFF_2H":
                    tb_tag = f"pr2h={cand.get('prev_ret_2h_pct')}% pvr2h={cand.get('vrat_2h_prev')}x tb2h={cand.get('tb_2h')}"
                elif setup == "BASOVH_1H":
                    tb_tag = f"stb1h={cand.get('spot_tb_1h')} tb1h={cand.get('tb_1h')}"
                elif setup == "WHALESP_1H":
                    tb_tag = f"stsz={cand.get('spot_tsize_r_1h')}x stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "SP_LEAD_4H":
                    tb_tag = f"stb4h={cand.get('spot_tb_4h')} svr4h={cand.get('spot_vrat_4h')}x tb4h={cand.get('tb_4h')}"
                elif setup == "POST30D_4H":
                    tb_tag = f"age={cand.get('age_days')}d vr4h={cand.get('vrat_4h')}x tb4h={cand.get('tb_4h')}"
                elif setup == "BEAR_BRK_1H":
                    tb_tag = f"vr1h={cand.get('vrat_1h')}x ext={cand.get('ext_e10_1h_pct')}% tb1h={cand.get('tb_1h')}"
                elif setup == "BASCRUSH_4H":
                    tb_tag = f"tb4h={cand.get('tb_4h')}"
                elif setup == "L_SQUEEZE_1H":
                    tb_tag = f"tb1h={cand.get('tb_1h')} stb1h={cand.get('spot_tb_1h')} vr1h={cand.get('vrat_1h')}x fund={cand.get('funding_day_pct')}%"
                elif setup == "L_SPOTACC_1H":
                    tb_tag = f"stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "L_SP_LWICK_1H":
                    tb_tag = f"splw1h={cand.get('spot_lwick_1h')} stb1h={cand.get('spot_tb_1h')} svr1h={cand.get('spot_vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "L_TREND_BRK_1H":
                    tb_tag = f"rsi1h={cand.get('rsi1h')} vr1h={cand.get('vrat_1h')}x tb1h={cand.get('tb_1h')}"
                elif setup == "L_EMA_BOUNCE_2H":
                    tb_tag = f"vr2h={cand.get('vrat_2h')}x tb2h={cand.get('tb_2h')}"
                elif setup in ("EMA_RSI_SHORT", "EMA_RSI_LONG"):
                    trig_s = h.get("trigger") or "TREND"
                    tb_tag = f"{trig_s} rs24={cand.get('rs24_pct')}% rsi15={cand.get('rsi15m')} rsi1h={cand.get('rsi1h')} vr15={cand.get('vrat_15m')}x f={cand.get('funding_day_pct')}%"
                else:
                    tb_tag = f"tb1h={cand.get('tb_1h')}"
                bas_tag = f"bas8h={cand['bas8h_pct']}%" if cand.get("bas8h_pct") is not None else "perp_only"
                reason = (
                    f"V23 {h_side} {setup} score={chosen_score} {tb_tag} {bas_tag} "
                    f"r24={cand['ret_24h_pct']}% r7d={cand['ret_7d_pct']}% SL={float(sl)*100:.1f}%/TP={float(tp)*100:.0f}%"
                )
                pick_entry = {**cand, "side": h_side, "primary_setup": setup, "score": chosen_score, "suggested_stop_pct": float(sl), "suggested_tp_pct": float(tp), "exit_mode": exit_mode}
                if dry_run:
                    if sim_avail_u is None or sim_avail_u < MIN_AVAIL_USDT:
                        skipped_risk.append({"symbol": sym, "side": h_side, "setup": setup, "reason": f"avail_below_{MIN_AVAIL_USDT}"})
                        break
                    opened_out.append({"symbol": sym, "side": h_side, "setup": setup, "exit_mode": exit_mode, "dry_run": True, "stop_pct": sl, "tp_pct": tp})
                    picks_rec.append(pick_entry)
                    actions.append(f"[DRY] เปิด {h_side} {sym} ({setup}, {exit_mode})")
                    sim_avail_u -= MARGIN_USDT
                    cur_heat_u += sl_risk_u
                    setup_counts[setup] = setup_counts.get(setup, 0) + 1
                    side_counts[h_side] = side_counts.get(h_side, 0) + 1
                    taken_syms.add(sym)
                else:
                    ok, res, _ = _execute_open(
                        sym, sl, tp, reason,
                        setup=setup,
                        score=chosen_score,
                        regime_label=reg_lbl,
                        btc_px=(regime.get("btc") or {}).get("price"),
                        exit_mode=exit_mode,
                        now_utc=now_utc,
                        entry_est=cand.get("price"),
                        prefetched_spread=prefetched_spreads.get(sym),
                        side=h_side,
                    )
                    if ok:
                        opened_out.append({"symbol": sym, "side": h_side, "setup": setup, "exit_mode": exit_mode, "ok": True, "id": res.get("id")})
                        picks_rec.append(pick_entry)
                        actions.append(f"เปิด {h_side} {sym} ({setup}, {exit_mode}, SL {float(sl)*100:.1f}%/TP {float(tp)*100:.0f}%)")
                        cur_heat_u += sl_risk_u
                        setup_counts[setup] = setup_counts.get(setup, 0) + 1
                        side_counts[h_side] = side_counts.get(h_side, 0) + 1
                        taken_syms.add(sym)
                    else:
                        opened_out.append({"symbol": sym, "side": h_side, "setup": setup, "ok": False, "error": res.get("error")})
                        taken_syms.add(sym)
                        if "ครบเพดาน" in str(res.get("error") or "") or "available_usdt" in str(res.get("error") or ""):
                            break

        # Refresh post-trade portfolio snapshot so summary & stdout reflect real final equity/open_count
        pf_final = read_json(binance.PORTFOLIO, default=pf) if (not dry_run and (closed_out or any(x.get("ok") for x in opened_out))) else pf
        final_open_count = len(set(pf_final.get("held_symbols") or ()) | {x["symbol"] for x in pf_final.get("shorts") or [] if x.get("on_binance") or not x.get("entry")})
        final_equity = pf_final.get("equity_usdt")

        # Record console plan on every 15-minute auto-trade run (preserving earlier picks on quiet ticks)
        if not dry_run:
            btc = regime.get("btc") or {}
            brd = regime.get("breadth") or {}
            heat_tag = f"{cur_heat_u}/{MAX_OPEN_HEAT_USDT}U" if MAX_OPEN_HEAT_USDT > Decimal("0") else f"{cur_heat_u}U"
            open_cap_tag = f"{final_open_count}/{MAX_OPEN}" if MAX_OPEN > 0 else f"{final_open_count}ไม้"
            summary = (
                f"V23 {reg_lbl} BTC={btc.get('price')} B1D={brd.get('pct_above_1d_sma20')}% B1H={brd.get('pct_above_1h_ema20')}% "
                f"(CH15m={ch_check.get('pct')}% w={ch_check.get('waited_sec')}s 1H={regime.get('complete_1h_pct')}%) | ปิด={sum(1 for x in closed_out if x.get('ok'))} เปิด={sum(1 for x in opened_out if x.get('ok'))} "
                f"สัญญาณ={len(candidates)} heat={heat_tag} พอร์ต={final_equity}U ({open_cap_tag} S{side_counts['SHORT']}/L{side_counts['LONG']})"
            )[:280]
            _write_round_report(
                reg_lbl,
                summary,
                universe=regime.get("liquid_universe", 0),
                picks=picks_rec,
                actions=actions,
                now_utc=now_utc,
                preserve_plan_if_no_picks=True,
            )

        cfg_cb_out = _config_cb_fields(cb_summary=cb, state=new_state_p3)
        print(_dump_json({
            "ok": True,
            "strategy": STRATEGY_VERSION,
            "dry_run": dry_run,
            "at_th": f"{now_utc.astimezone(TH):%Y-%m-%d %H:%M:%S}",
            "regime": reg_lbl,
            "complete_1h_pct": regime.get("complete_1h_pct"),
            "complete_15m_pct": regime.get("complete_15m_pct"),
            "ch_ready": ch_check.get("ready"),
            "ch_15m_pct": ch_check.get("pct"),
            "ch_15m_cnt": f"{ch_check.get('cur_cnt')}/{ch_check.get('prev_cnt')}",
            "ch_wait_sec": ch_check.get("waited_sec"),
            "patched_1h": regime.get("patched_1h"),
            "patched_spot_15m": regime.get("patched_spot_15m"),
            "spot_ok": regime.get("spot_ok"),
            "wait_retries": retries,
            "open_heat_usdt": str(cur_heat_u),
            "max_heat_usdt": str(MAX_OPEN_HEAT_USDT),
            **cfg_cb_out,
            "hourly_max_opens": max_opens_i,
            "enforce_regime_gate": ENFORCE_REGIME_OPEN_GATE,
            "enforce_cooldown": ENFORCE_OPEN_COOLDOWN,
            "candidates_found": len(candidates),
            "skipped_risk": skipped_risk,
            "closed": closed_out,
            "opened": opened_out,
            "equity_usdt": final_equity,
            "open_count": final_open_count,
        }))
        return 0


def compute_live_stats(journal_entries):
    """Aggregate realized trade performance by setup from `agy-journal.jsonl` close events using Decimal math."""
    by_setup = {}
    tot_n = 0
    tot_wins = 0
    tot_gw = Decimal("0")
    tot_gl = Decimal("0")
    for row in journal_entries or ():
        if row.get("event") != "close":
            continue
        cr = row.get("closed_record") or {}
        pnl = dec(row.get("pnl_usdt") if row.get("pnl_usdt") is not None else cr.get("pnl_usdt"))
        if pnl is None:
            continue
        setup = row.get("setup") or "LEGACY"
        rec = by_setup.setdefault(setup, {"n": 0, "wins": 0, "gw": Decimal("0"), "gl": Decimal("0")})
        rec["n"] += 1
        tot_n += 1
        if pnl > Decimal("0"):
            rec["wins"] += 1
            rec["gw"] += pnl
            tot_wins += 1
            tot_gw += pnl
        else:
            rec["gl"] += -pnl
            tot_gl += -pnl

    def _fmt(r):
        n = r["n"]
        w = r["wins"]
        l = n - w
        net = r["gw"] - r["gl"]
        pf = (r["gw"] / r["gl"]) if r["gl"] > Decimal("0") else (Decimal("99") if r["gw"] > Decimal("0") else Decimal("0"))
        avg_w = (r["gw"] / Decimal(w)) if w > 0 else Decimal("0")
        avg_l = (-r["gl"] / Decimal(l)) if l > 0 else Decimal("0")
        return {
            "n": n,
            "wins": w,
            "losses": l,
            "wr_pct": round(w / n * 100.0, 1) if n else 0.0,
            "net_usdt": str(net.quantize(Decimal("0.01"))),
            "avg_win_usdt": str(avg_w.quantize(Decimal("0.01"))),
            "avg_loss_usdt": str(avg_l.quantize(Decimal("0.01"))),
            "pf": round(float(pf), 2),
        }

    return {
        "overall": _fmt({"n": tot_n, "wins": tot_wins, "gw": tot_gw, "gl": tot_gl}),
        "by_setup": {k: _fmt(v) for k, v in sorted(by_setup.items())},
    }


def cmd_report_stats(full=False):
    entries = _recent_journal(limit=5000)
    stats = compute_live_stats(entries)
    print(_dump_json({"ok": True, "strategy": STRATEGY_VERSION, **stats}, pretty=full))
    return 0


def cmd_open(args):
    with file_lock(LOCK_PATH, blocking=True) as got:
        if not got:
            print(_dump_json({"ok": False, "error": "lock_busy"}))
            return 1
        _, out, rc = _execute_open(
            args.symbol,
            args.stop_pct,
            args.tp_pct,
            args.reason,
            setup=args.setup,
            exit_mode=args.exit_mode,
            side=getattr(args, "side", ALLOWED_SIDE),
        )
    print(_dump_json(out))
    return rc


def cmd_close(args):
    with file_lock(LOCK_PATH, blocking=True) as got:
        if not got:
            print(_dump_json({"ok": False, "error": "lock_busy"}))
            return 1
        _, out, rc = _execute_close(args.id, args.why)
    print(_dump_json(out))
    return rc


def cmd_record_round(args):
    picks = json.loads(args.picks_json) if args.picks_json else []
    actions = json.loads(args.actions_json) if args.actions_json else []
    out = _write_round_report(args.regime, args.summary, universe=args.universe, picks=picks, actions=actions)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_reset_cb(args):
    if not args.confirm_reset:
        print(json.dumps({"ok": False, "error": "ต้องระบุ --confirm-reset เพื่อรีเซ็ต Circuit Breaker"}, ensure_ascii=False))
        return 2
    with file_lock(LOCK_PATH, blocking=True) as got:
        if not got:
            print(_dump_json({"ok": False, "error": "lock_busy"}))
            return 1
        now_utc = utcnow()
        pf = binance.save_portfolio(binance.fetch_portfolio())
        eq = dec(pf.get("equity_usdt")) if not pf.get("error") else None
        if eq is None or eq <= Decimal("0"):
            print(json.dumps({"ok": False, "error": "อ่าน equity_usdt จาก Proxy ไม่ได้ จึงตั้ง baseline ใหม่ไม่ได้"}, ensure_ascii=False))
            return 1
        cb_u_rst, cb_p_rst = compute_auto_cb_limits(eq, LEVERAGE) if CB_AUTO_ENABLED else (CB_MAX_LOSS_USDT, CB_MAX_LOSS_PCT)
        eq_q_str = str(eq.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        st = {
            "baseline_equity_usdt": eq_q_str,
            "baseline_set_at": now_utc.isoformat(),
            "peak_equity_usdt": eq_q_str,
            "last_equity_usdt": eq_q_str,
            "last_checked_at": now_utc.isoformat(),
            "cb_auto": bool(CB_AUTO_ENABLED),
            "cb_leverage": int(LEVERAGE),
            "cb_ref_equity_usdt": eq_q_str,
            "cb_limit_usdt": str(cb_u_rst.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "cb_limit_pct": str((cb_p_rst * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "cb_dd_usdt": "0.00",
            "cb_dd_pct": "0.00",
            "circuit_breaker_tripped": False,
            "tripped_at": None,
            "tripped_reason": None,
            "reset_at": now_utc.isoformat(),
        }
        write_json_atomic(STATE_PATH, st)
        _append_journal("reset_cb", {"new_baseline": st["baseline_equity_usdt"]}, now_utc)
    print(_dump_json({"ok": True, "state": st}))
    return 0


def main(argv=None):
    init_env()
    p = argparse.ArgumentParser(description="Antigravity V23 Autonomous Dual-Side SHORT/LONG Intraday Trader CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="Read portfolio & circuit breaker status without placing orders")
    p_status.add_argument("--full", action="store_true", help="Pretty-print full JSON output")

    p_scan = sub.add_parser("scan", help="Sync ledger, check 15m stops on open positions, and scan V23 Dual-Side candidates")
    p_scan.add_argument("--full", action="store_true", help="Return full uncompacted JSON")

    p_guard = sub.add_parser("guard", help="24/7 full exit guardian: sync + 15m bars + parallel close any MUST_CLOSE positions")
    p_guard.add_argument("--dry-run", action="store_true", help="Check exits without closing orders")

    p_fguard = sub.add_parser("fast-guard", help="5-second live mark-price & quad-channel squeeze exit guardian (runs every 1m via cron)")
    p_fguard.add_argument("--duration", type=int, default=55, help="Total loop duration in seconds")
    p_fguard.add_argument("--interval", type=int, default=5, help="Polling interval in seconds (min 5)")
    p_fguard.add_argument("--dry-run", action="store_true", help="Check mark breaches without closing orders")

    p_auto = sub.add_parser("auto-trade", help="15m V23 autonomous trader: ClickHouse-ready wait, Exit-First, Dual-Side SHORT/LONG open")
    p_auto.add_argument("--max-opens", type=int, default=HOURLY_MAX_OPENS, help="Max new positions to open per hour (0 = unlimited, constrained by available_usdt)")
    p_auto.add_argument("--dry-run", action="store_true", help="Evaluate exits and entries without sending orders")

    p_stats = sub.add_parser("report-stats", help="Aggregate live realized performance by setup from agy-journal.jsonl")
    p_stats.add_argument("--full", action="store_true", help="Pretty-print full JSON output")

    p_open = sub.add_parser("open", help="Open 200 USDT (Margin 100 x 2x isolated) SHORT or LONG position past hard guardrails")
    p_open.add_argument("--symbol", required=True)
    p_open.add_argument("--side", default=ALLOWED_SIDE, choices=("SHORT", "LONG"))
    p_open.add_argument("--stop-pct", required=True)
    p_open.add_argument("--tp-pct", required=True)
    p_open.add_argument("--reason", required=True)
    p_open.add_argument("--setup", default="V22")
    p_open.add_argument(
        "--exit-mode",
        default=DEFAULT_EXIT_MODE,
        choices=(
            "V22_LONG_SQZ96", "V21_NEW7_SQZ96", "V18_AA_SQZ96", "V17_AA_SQZ96",
            "V14_AA_SQZ96", "V14_NEW7_SQZ96",
            "V12_AA_SQZ96", "V12_NEW7_SQZ96", "V11_AA_SQZ96", "V11_NEW7_SQZ96",
            "V10_AA_SQZ96", "V10_NEW7_SQZ96", "V9_AA_SQZ96", "V9_NEW7_SQZ96",
            "V8_AA_SQZ96", "V8_NEW7_SQZ96", "V7_AA_HYB96", "V6_LT8_96",
            "V4_LT8_72", "V3_PURE24", "LEGACY",
        ),
    )

    p_close = sub.add_parser("close", help="Close one bot position via Proxy")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--why", required=True)

    p_rec = sub.add_parser("record-round", help="Write console plan & journal for the current round")
    p_rec.add_argument("--regime", required=True)
    p_rec.add_argument("--summary", required=True)
    p_rec.add_argument("--universe", type=int, default=0)
    p_rec.add_argument("--picks-json", default="")
    p_rec.add_argument("--actions-json", default="")

    p_rcb = sub.add_parser("reset-cb", help="Reset circuit breaker baseline (explicit command only)")
    p_rcb.add_argument("--confirm-reset", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(full=args.full)
    if args.cmd == "scan":
        return cmd_scan(full=args.full)
    if args.cmd == "guard":
        return cmd_guard(dry_run=args.dry_run)
    if args.cmd == "fast-guard":
        return cmd_fast_guard(duration=args.duration, interval=args.interval, dry_run=args.dry_run)
    if args.cmd == "auto-trade":
        return cmd_auto_trade(max_opens=args.max_opens, dry_run=args.dry_run)
    if args.cmd == "report-stats":
        return cmd_report_stats(full=args.full)
    if args.cmd == "open":
        return cmd_open(args)
    if args.cmd == "close":
        return cmd_close(args)
    if args.cmd == "record-round":
        return cmd_record_round(args)
    if args.cmd == "reset-cb":
        return cmd_reset_cb(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
