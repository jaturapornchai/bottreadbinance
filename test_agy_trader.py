"""Offline unit tests for agy_trader.py V21 (run: .venv/Scripts/python test_agy_trader.py)."""
import datetime as dt
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agy_trader as agy  # noqa: E402


def test_decimal_and_circuit_breaker():
    # 1. Auto Safe CB leverage scaling & floor (Decimal exact)
    assert agy.compute_auto_cb_limits("1000.00", leverage=1) == (Decimal("50.00"), Decimal("0.05"))
    assert agy.compute_auto_cb_limits("1000.00", leverage=2) == (Decimal("75.00"), Decimal("0.075"))
    assert agy.compute_auto_cb_limits("916.80", leverage=2) == (Decimal("68.76"), Decimal("0.075"))
    assert agy.compute_auto_cb_limits("1000.00", leverage=3) == (Decimal("100.00"), Decimal("0.10"))
    assert agy.compute_auto_cb_limits("1000.00", leverage=5) == (Decimal("100.00"), Decimal("0.10"))  # capped at 10%
    assert agy.compute_auto_cb_limits("200.00", leverage=2) == (Decimal("20.00"), Decimal("0.075"))   # 20.00 USDT floor

    # 2. Default Auto Safe CB (CB_AUTO_ENABLED=True, LEVERAGE=2 -> -7.50% from High-Water Mark peak)
    now_iso = "2026-11-22T08:00:00+00:00"
    st, sm = agy.evaluate_circuit_breaker({}, "1000.00", now_iso)
    assert st["baseline_equity_usdt"] == "1000.00"
    assert st["peak_equity_usdt"] == "1000.00"
    assert st["cb_auto"] is True and st["cb_leverage"] == 2
    assert st["cb_ref_equity_usdt"] == "1000.00" and st["cb_limit_usdt"] == "75.00" and st["cb_limit_pct"] == "7.50"
    assert st["circuit_breaker_tripped"] is False
    assert sm["dd_usdt"] == "0.00" and sm["cb_limit_usdt"] == "75.00" and sm["cb_limit_pct"] == "7.50"

    # Peak ratchets up to 1200.00 -> auto CB limit updates to 90.00 USDT (7.50% of 1200)
    st, sm = agy.evaluate_circuit_breaker(st, "1200.00", "2026-11-22T08:30:00+00:00")
    assert st["peak_equity_usdt"] == "1200.00" and st["cb_ref_equity_usdt"] == "1200.00"
    assert st["cb_limit_usdt"] == "90.00" and st["circuit_breaker_tripped"] is False

    # Drop -89.99 USDT (-7.4991...%) from peak 1200 -> must NOT trip
    st, sm = agy.evaluate_circuit_breaker(st, "1110.01", "2026-11-22T09:00:00+00:00")
    assert st["circuit_breaker_tripped"] is False
    assert sm["dd_usdt"] == "-89.99"

    # Drop exactly -90.00 USDT (-7.50%) from peak 1200 -> TRIPS!
    st, sm = agy.evaluate_circuit_breaker(st, "1110.00", "2026-11-22T10:00:00+00:00")
    assert st["circuit_breaker_tripped"] is True
    assert sm["tripped"] is True and sm["cb_dd_usdt"] == "-90.00" and sm["cb_dd_pct"] == "-7.50"
    assert st["tripped_at"] == "2026-11-22T10:00:00+00:00"

    # Sticky: even if equity recovers above previous peak, breaker remains tripped while peak records 1250
    st, sm = agy.evaluate_circuit_breaker(st, "1250.00", "2026-11-22T11:00:00+00:00")
    assert st["circuit_breaker_tripped"] is True
    assert st["peak_equity_usdt"] == "1250.00"

    # 3. Legacy fixed CB fallback when CB_AUTO_ENABLED=False (-45.00 USDT or -5% vs baseline)
    orig_auto = agy.CB_AUTO_ENABLED
    try:
        agy.CB_AUTO_ENABLED = False
        st2, _ = agy.evaluate_circuit_breaker({}, "800.00", now_iso)
        st2, _ = agy.evaluate_circuit_breaker(st2, "760.01", now_iso)
        assert st2["circuit_breaker_tripped"] is False
        st2, _ = agy.evaluate_circuit_breaker(st2, "760.00", now_iso)
        assert st2["circuit_breaker_tripped"] is True
    finally:
        agy.CB_AUTO_ENABLED = orig_auto


def _base_portfolio():
    return {
        "available_usdt": "500.00",
        "equity_usdt": "880.00",
        "shorts": [],
        "held_symbols": [],
        "closed": [],
        "orders": [],
    }


def test_validate_open_guardrails():
    now_utc = dt.datetime(2026, 11, 24, 10, 0, tzinfo=dt.timezone.utc)
    pf = _base_portfolio()
    st = {"circuit_breaker_tripped": False}

    ok, err = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf, st, now_utc, spread="0.001")
    assert ok is True and err is None

    # V22: allow BOTH SHORT and LONG; reject invalid sides & enforce MAX_LONG_OPEN = 4
    ok, err = agy.validate_open_guardrails("SOLUSDT", "LONG", "0.050", "0.45", pf, st, now_utc, spread="0.001")
    assert ok is True and err is None
    for bad_side in ("BUY", "SELL", "", "BOTH"):
        ok, err = agy.validate_open_guardrails("SOLUSDT", bad_side, "0.050", "0.45", pf, st, now_utc)
        assert ok is False and "SHORT" in err and "LONG" in err
    pf_4long = {
        **pf,
        "shorts": [{"id": f"l{i}", "symbol": f"L{i}USDT", "side": "LONG", "entry": "10", "on_binance": True} for i in range(4)],
        "held_symbols": [f"L{i}USDT" for i in range(4)],
    }
    # Unlimited LONG/SHORT sides (MAX_LONG_OPEN == MAX_OPEN == 8): 5th LONG and SHORT both allowed!
    ok, err = agy.validate_open_guardrails("SOLUSDT", "LONG", "0.050", "0.45", pf_4long, st, now_utc)
    assert ok is True and err is None
    ok, err = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.050", "0.45", pf_4long, st, now_utc)
    assert ok is True and err is None
    # When MAX_LONG_OPEN is explicitly capped, guardrail enforces it
    orig_ml = agy.MAX_LONG_OPEN
    try:
        agy.MAX_LONG_OPEN = 4
        ok, err = agy.validate_open_guardrails("SOLUSDT", "LONG", "0.050", "0.45", pf_4long, st, now_utc)
        assert ok is False and "LONG" in err and "4" in err
    finally:
        agy.MAX_LONG_OPEN = orig_ml

    # Reject forbidden symbols
    for bad in ("BTCUSDT", "USDCUSDT", "PAXGUSDT"):
        ok, _ = agy.validate_open_guardrails(bad, "SHORT", "0.060", "0.40", pf, st, now_utc)
        assert ok is False

    # Reject stop/tp out of validated V9 range (2.5%..7.5% / 7%..60%)
    for s, t in (("0.024", "0.60"), ("0.076", "0.60"), ("0.060", "0.069"), ("0.060", "0.601")):
        ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", s, t, pf, st, now_utc)
        assert ok is False
    for s, t in (("0.025", "0.07"), ("0.045", "0.22"), ("0.060", "0.40"), ("0.075", "0.60")):
        ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", s, t, pf, st, now_utc)
        assert ok is True

    # Reject CB tripped
    ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf, {"circuit_breaker_tripped": True}, now_utc)
    assert ok is False

    # Reject portfolio error & unclear
    ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", {"error": "down"}, st, now_utc)
    assert ok is False
    pf_unclear = {**pf, "shorts": [{"symbol": "ETHUSDT", "coin": "ETH", "on_binance": True, "unclear": True}]}
    ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_unclear, st, now_utc)
    assert ok is False

    # Unlimited open positions by default (MAX_OPEN == 0): 9th/10th position allowed when available_usdt >= 101!
    pf_full = {**pf, "held_symbols": [f"C{i}USDT" for i in range(8)]}
    ok, err = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_full, st, now_utc)
    assert ok is True and err is None
    # When MAX_OPEN is explicitly capped > 0, guardrail still enforces it
    orig_mo = agy.MAX_OPEN
    try:
        agy.MAX_OPEN = 8
        ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_full, st, now_utc)
        assert ok is False
    finally:
        agy.MAX_OPEN = orig_mo

    # Reject available_usdt < 101
    ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", {**pf, "available_usdt": "100.99"}, st, now_utc)
    assert ok is False
    ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", {**pf, "available_usdt": "101.00"}, st, now_utc)
    assert ok is True

    # Reject held symbol
    ok, _ = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", {**pf, "held_symbols": ["SOLUSDT"]}, st, now_utc)
    assert ok is False

    # Cooldown disabled by default (ENFORCE_OPEN_COOLDOWN=False); verify bypass + helper enforcement when enabled
    pf_win_2h = {**pf, "closed": [{"symbol": "SOLUSDT", "closed_at": (now_utc - dt.timedelta(hours=2)).isoformat(), "pnl": "5.00"}]}
    assert agy.ENFORCE_OPEN_COOLDOWN is False
    assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_win_2h, st, now_utc)[0] is True
    orig_cd = agy.ENFORCE_OPEN_COOLDOWN
    try:
        agy.ENFORCE_OPEN_COOLDOWN = True
        assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_win_2h, st, now_utc)[0] is False
        pf_win_5h = {**pf, "closed": [{"symbol": "SOLUSDT", "closed_at": (now_utc - dt.timedelta(hours=5)).isoformat(), "pnl": "5.00"}]}
        assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_win_5h, st, now_utc)[0] is True

        pf_loss_48h = {**pf, "closed": [{"symbol": "SOLUSDT", "closed_at": (now_utc - dt.timedelta(hours=48)).isoformat(), "pnl": "-2.00"}]}
        assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_loss_48h, st, now_utc)[0] is False
        pf_loss_73h = {**pf, "closed": [{"symbol": "SOLUSDT", "closed_at": (now_utc - dt.timedelta(hours=73)).isoformat(), "pnl": "-2.00"}]}
        assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_loss_73h, st, now_utc)[0] is True

        pf_2loss_7d = {
            **pf,
            "closed": [
                {"symbol": "SOLUSDT", "closed_at": (now_utc - dt.timedelta(days=6)).isoformat(), "pnl": "-3.00"},
                {"symbol": "SOLUSDT", "closed_at": (now_utc - dt.timedelta(days=4)).isoformat(), "pnl": "-3.00"},
            ],
        }
        assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_2loss_7d, st, now_utc)[0] is False
    finally:
        agy.ENFORCE_OPEN_COOLDOWN = orig_cd

    # Reject duplicate symbol opened today UTC; DAILY_MAX_OPENS = 0 (unlimited daily opens unlocked by user 2026-11-26)
    pf_today = {**pf, "orders": [{"kind": "open", "how": "placed", "symbol": "SOLUSDT", "at": "2026-11-24T01:00:00+00:00"}]}
    assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_today, st, now_utc)[0] is False
    pf_10 = {**pf, "orders": [{"kind": "open", "how": "placed", "symbol": f"COIN{i}USDT", "at": "2026-11-24T01:00:00+00:00"} for i in range(10)]}
    assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_10, st, now_utc)[0] is True
    orig_daily = agy.DAILY_MAX_OPENS
    try:
        agy.DAILY_MAX_OPENS = 10
        ok, err = agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf_10, st, now_utc)
        assert ok is False and "10" in err
    finally:
        agy.DAILY_MAX_OPENS = orig_daily

    # Reject spread > 0.005
    assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf, st, now_utc, spread="0.0051")[0] is False
    assert agy.validate_open_guardrails("SOLUSDT", "SHORT", "0.060", "0.40", pf, st, now_utc, spread="0.0050")[0] is True


def test_compute_open_heat_and_counts():
    # Mixed 1x legacy (qty=1 -> 100U notional) + 2x new (qty=2 -> 200U notional) + fallback without qty (LOT_USDT=200U)
    pf = {
        "shorts": [
            {"id": "p1", "symbol": "AAAUSDT", "qty": "1", "entry": "100", "on_binance": True},   # 1x (100U) * 6% = 6.00U
            {"id": "p2", "symbol": "BBBUSDT", "qty": "2", "entry": "100", "on_binance": True},   # profit-locked -> 0.00U
            {"id": "p3", "symbol": "CCCUSDT", "qty": "2", "entry": "100", "on_binance": True},   # 2x (200U) * 7.5% = 15.00U
            {"id": "p4", "symbol": "DDDUSDT", "qty": "2", "entry": "100", "on_binance": False},  # not on_binance -> ignored
            {"id": "p5", "symbol": "EEEUSDT", "entry": "100", "on_binance": True},               # no qty -> fallback LOT_USDT=200U * 5% = 10.00U
        ]
    }
    meta = {
        "p1": {"setup": "NEW7_BAS_1H", "stop_pct": "0.060", "stop_px": "106.0"},   # 1x unprotected -> 6.00 USDT
        "p2": {"setup": "NEW7_BAS_1H", "stop_pct": "0.060", "stop_px": "97.5"},    # profit-locked -> 0.00 USDT!
        "p3": {"setup": "PARA75_G_1H", "stop_pct": "0.075", "stop_px": "107.5"},   # 2x unprotected -> 15.00 USDT
        "p4": {"setup": "NEW7_BAS_1H", "stop_pct": "0.060", "stop_px": "106.0"},   # not on_binance -> ignored
        "p5": {"setup": "SP_UWICK_1H", "stop_pct": "0.050", "stop_px": "105.0"},   # fallback 200U -> 10.00 USDT
    }
    heat, counts = agy.compute_open_heat_and_counts(pf, meta)
    assert heat == Decimal("31.00")
    assert counts == {"NEW7_BAS_1H": 2, "PARA75_G_1H": 1, "SP_UWICK_1H": 1}


def test_position_exit_v3_pure24_and_legacy():
    opened = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)
    t0_ms = int(opened.timestamp() * 1000)
    bar_ms = 900_000

    meta_v3 = {"exit_mode": "V3_PURE24", "stop_pct": "0.060", "tp_pct": "0.25"}
    bars_v3 = [
        (t0_ms, Decimal("100.5"), Decimal("96.0"), Decimal("97.0")),
        (t0_ms + bar_ms, Decimal("99.8"), Decimal("98.5"), Decimal("99.5")),
    ]
    ev = agy.evaluate_position_exit(meta_v3, "100", "99.5", bars_v3, opened.isoformat(), opened + dt.timedelta(hours=9))
    assert ev["action"] == "HOLD" and ev["ratchet_stage"] == "INIT"
    assert Decimal(ev["stop_px"]) == Decimal("106") and Decimal(ev["tp_px"]) == Decimal("75")

    bars_sl = bars_v3 + [(t0_ms + 2 * bar_ms, Decimal("106.1"), Decimal("99.0"), Decimal("105.0"))]
    ev_sl = agy.evaluate_position_exit(meta_v3, "100", "105.0", bars_sl, opened.isoformat(), opened + dt.timedelta(hours=2))
    assert ev_sl["action"] == "MUST_CLOSE" and "STOP_LOSS_15M" in ev_sl["exit_reason"]

    bars_tp = bars_v3 + [(t0_ms + 2 * bar_ms, Decimal("98.0"), Decimal("74.9"), Decimal("76.0"))]
    ev_tp = agy.evaluate_position_exit(meta_v3, "100", "76.0", bars_tp, opened.isoformat(), opened + dt.timedelta(hours=5))
    assert ev_tp["action"] == "MUST_CLOSE" and "TAKE_PROFIT_15M" in ev_tp["exit_reason"]

    ev_max = agy.evaluate_position_exit(meta_v3, "100", "92.0", [], opened.isoformat(), opened + dt.timedelta(hours=24))
    assert ev_max["action"] == "MUST_CLOSE" and "MAX_HOLD_24H" in ev_max["exit_reason"]

    meta_locked = {**meta_v3, "stop_px": "99.6", "ratchet_stage": "BE"}
    ev_lk = agy.evaluate_position_exit(meta_locked, "100", "99.7", [], opened.isoformat(), opened + dt.timedelta(hours=1))
    assert ev_lk["action"] == "MUST_CLOSE" and "TRAILING_STOP_MARK" in ev_lk["exit_reason"]

    meta_leg = {"stop_pct": "0.035", "tp_pct": "0.09"}
    bars_be = [(t0_ms, Decimal("99.8"), Decimal("97.5"), Decimal("98.5"))]
    ev_l = agy.evaluate_position_exit(meta_leg, "100", "98.5", bars_be, opened.isoformat(), opened + dt.timedelta(minutes=20))
    assert ev_l["action"] == "HOLD" and ev_l["ratchet_stage"] == "BE" and Decimal(ev_l["stop_px"]) == Decimal("99.6")

    ev_stag = agy.evaluate_position_exit(meta_leg, "100", "98.8", [], opened.isoformat(), opened + dt.timedelta(hours=8))
    assert ev_stag["action"] == "MUST_CLOSE" and "STAGNANT_8H" in ev_stag["exit_reason"]


def test_position_exit_v4_and_v6_lt8_96():
    opened = dt.datetime(2026, 11, 20, 0, 0, 15, tzinfo=dt.timezone.utc)
    bar0_ms = int(dt.datetime(2026, 11, 20, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    bar_ms = 900_000
    meta_v6 = {"exit_mode": "V6_LT8_96", "stop_pct": "0.060", "tp_pct": "0.40"}

    # 1. Opening at 00:00:15 (within 3m grace) includes bar 00:00:00! MFE +6% -> stays INIT (no early BE choke)
    bars_early = [
        (bar0_ms, Decimal("101.0"), Decimal("94.0"), Decimal("95.0")),
        (bar0_ms + bar_ms, Decimal("100.5"), Decimal("97.0"), Decimal("99.0")),
    ]
    ev1 = agy.evaluate_position_exit(meta_v6, "100", "99.0", bars_early, opened.isoformat(), opened + dt.timedelta(hours=9))
    assert ev1["action"] == "HOLD" and ev1["ratchet_stage"] == "INIT" and Decimal(ev1["stop_px"]) == Decimal("106")
    assert Decimal(ev1["mfe_pct"]) == Decimal("6.00")

    # Opening at 00:08:00 (> 3m grace) MUST skip bar 00:00:00 so pre-entry high doesn't falsely SL!
    opened_late = dt.datetime(2026, 11, 20, 0, 8, 0, tzinfo=dt.timezone.utc)
    bars_pre_spike = [(bar0_ms, Decimal("110.0"), Decimal("99.0"), Decimal("100.0"))]
    ev_late = agy.evaluate_position_exit(meta_v6, "100", "99.5", bars_pre_spike, opened_late.isoformat(), opened_late + dt.timedelta(minutes=20))
    assert ev_late["action"] == "HOLD"

    # 2. MFE +8.5% -> LOCK8 stop = 97.5 (+2.5% locked) for NEXT bar
    bars_lock = bars_early + [(bar0_ms + 2 * bar_ms, Decimal("99.5"), Decimal("91.5"), Decimal("93.0"))]
    ev2 = agy.evaluate_position_exit(meta_v6, "100", "93.0", bars_lock, opened.isoformat(), opened + dt.timedelta(hours=2))
    assert ev2["action"] == "HOLD" and ev2["ratchet_stage"] == "LOCK8" and Decimal(ev2["stop_px"]) == Decimal("97.5")

    # 3. MFE +14.0% -> TRAIL13 stop = 90.5
    bars_tr = bars_lock + [(bar0_ms + 3 * bar_ms, Decimal("93.5"), Decimal("86.0"), Decimal("88.0"))]
    ev3 = agy.evaluate_position_exit(meta_v6, "100", "88.0", bars_tr, opened.isoformat(), opened + dt.timedelta(hours=3))
    assert ev3["action"] == "HOLD" and ev3["ratchet_stage"] == "TRAIL13" and Decimal(ev3["stop_px"]) == Decimal("90.5")

    # 4. At 24h: if pnl < +5% (+3%) -> MUST_CLOSE MAX_HOLD_24H
    ev_24_close = agy.evaluate_position_exit(meta_v6, "100", "97.0", [], opened.isoformat(), opened + dt.timedelta(hours=24))
    assert ev_24_close["action"] == "MUST_CLOSE" and "MAX_HOLD_24H" in ev_24_close["exit_reason"]

    # 5. At 24h: if pnl >= +5% (+6.5%) -> unlocks RUNNER96 with stop <= 97.5, holds past 72h, closes at 96h
    ev_24_run = agy.evaluate_position_exit({**meta_v6, "mfe_pct": "0.070"}, "100", "93.5", [], opened.isoformat(), opened + dt.timedelta(hours=24))
    assert ev_24_run["action"] == "HOLD" and ev_24_run["ratchet_stage"] == "RUNNER96" and ev_24_run["runner_unlocked"] is True
    assert Decimal(ev_24_run["stop_px"]) <= Decimal("97.5")
    ev_75 = agy.evaluate_position_exit(ev_24_run["updated_meta"], "100", "85.0", [], opened.isoformat(), opened + dt.timedelta(hours=75))
    assert ev_75["action"] == "HOLD"
    ev_96 = agy.evaluate_position_exit(ev_24_run["updated_meta"], "100", "85.0", [], opened.isoformat(), opened + dt.timedelta(hours=96))
    assert ev_96["action"] == "MUST_CLOSE" and "MAX_HOLD_96H" in ev_96["exit_reason"]

    # 6. Backward compatibility: V4_LT8_72 still closes at 72h
    meta_v4 = {"exit_mode": "V4_LT8_72", "stop_pct": "0.060", "tp_pct": "0.35"}
    ev_v4_run = agy.evaluate_position_exit({**meta_v4, "mfe_pct": "0.070"}, "100", "93.5", [], opened.isoformat(), opened + dt.timedelta(hours=24))
    assert ev_v4_run["ratchet_stage"] == "RUNNER72"
    ev_v4_72 = agy.evaluate_position_exit(ev_v4_run["updated_meta"], "100", "85.0", [], opened.isoformat(), opened + dt.timedelta(hours=72))
    assert ev_v4_72["action"] == "MUST_CLOSE" and "MAX_HOLD_72H" in ev_v4_72["exit_reason"]


def test_classify_regime():
    assert agy.classify_regime(59000, 70000, -0.02, -0.005, -0.01, 0.20, 0.35, pct_above_15m_ema20=0.30, btc_1h_ema20=59500) == "BEARISH_FAVORABLE"
    assert agy.classify_regime(60500, 70000, 0.01, 0.010, 0.01, 0.20, 0.35, pct_above_15m_ema20=0.30, btc_1h_ema20=59500) == "NEUTRAL_SELECTIVE"
    assert agy.classify_regime(59000, 70000, 0.00, 0.002, 0.00, 0.20, 0.35, pct_above_15m_ema20=0.60, btc_1h_ema20=59500) == "NEUTRAL_SELECTIVE"
    assert agy.classify_regime(68000, 65000, 0.01, 0.005, 0.01, 0.55, 0.60) == "BULL_TREND_CAUTION"
    # Backtest-parity squeeze caution triggers
    assert agy.classify_regime(61000, 70000, 0.04, 0.005, 0.01, 0.20, 0.40) == "BULLISH_SQUEEZE_CAUTION"
    assert agy.classify_regime(59000, 70000, -0.03, 0.020, 0.01, 0.20, 0.40) == "BULLISH_SQUEEZE_CAUTION"
    assert agy.classify_regime(59000, 70000, -0.01, 0.010, 0.035, 0.20, 0.40) == "BULLISH_SQUEEZE_CAUTION"
    assert agy.classify_regime(61000, 70000, 0.01, 0.005, 0.01, 0.66, 0.40) == "BULLISH_SQUEEZE_CAUTION"
    assert agy.classify_regime(61000, 70000, 0.01, 0.005, 0.01, 0.20, 0.73) == "BULLISH_SQUEEZE_CAUTION"
    assert agy.classify_regime(59000, 70000, 0.00, 0.005, 0.01, 0.20, 0.40, btc_15m_ret1=0.012) == "BULLISH_SQUEEZE_CAUTION"
    # Non-backtest 15m_ema20 alone no longer hard-blocks as squeeze
    assert agy.classify_regime(59000, 70000, 0.00, 0.005, 0.01, 0.20, 0.40, pct_above_15m_ema20=0.76) == "NEUTRAL_SELECTIVE"


def test_evaluate_v6_short_setups_and_stats():
    base = dict(
        age_days=3.0,
        breadth_1d=0.25,
        btc_ret7d=-0.03,
        bar_complete_1h=True,
        cur_px=0.95,
        o_1h=0.97,
        hi_1h=0.98,
        lo_1h=0.94,
        c_1h=0.95,
        c_1h_prev=0.98,
        qv_1h=1_000_000.0,
        qv_avg20_1h=800_000.0,
        tb_1h=0.40,
        ema10_1h=0.975,
        ema10_1h_prev=0.978,
        atr14_1h_pct=0.018,
        hi24=1.05,
        hi7d=1.10,
        ret_4h=-0.02,
        ret_12h=-0.03,
        ret_24h=-0.05,
        ret_7d=-0.15,
        d_sma20=1.10,
        f_day=0.0002,
        has_spot_24h=True,
        bas8h=0.0002,
        bas12h_max=0.0010,
        bas_4h_close=0.0001,
        is_fresh_4h_window=False,
        bar_complete_4h=False,
    )

    # 1. NEW7_BAS_1H fires with TP 60%, blocked by dead ATR < 0.8% and >3% chase drop
    hits = agy.evaluate_v6_short_setups(**base)
    assert [h["setup"] for h in hits] == ["NEW7_BAS_1H"]
    assert hits[0]["stop_pct"] == 0.060 and hits[0]["tp_pct"] == 0.600
    assert agy.evaluate_v6_short_setups(**{**base, "atr14_1h_pct": 0.007}) == []
    assert agy.evaluate_v6_short_setups(**{**base, "cur_px": 0.91}) == []  # >3% flash drop chase guard
    assert agy.evaluate_v6_short_setups(**{**base, "breadth_1d": 0.55}) == []
    assert agy.evaluate_v6_short_setups(**{**base, "bas8h": -0.0020}) == []
    assert [h["setup"] for h in agy.evaluate_v6_short_setups(**{**base, "bas8h": None, "atr14_1h_pct": None})] == ["NEW7_BAS_1H"]

    # 2. PARA75_G_1H fires with priority > NEW7 (score >= 332) and TP 60%
    para = {
        **base,
        "age_days": 200.0, "breadth_1d": 0.40, "btc_ret7d": 0.03, "ret_7d": 0.85,
        "hi7d": 1.05, "c_1h": 0.95, "cur_px": 0.95, "c_1h_prev": 0.985,
        "ema10_1h": 0.975, "ema10_1h_prev": 0.980, "tb_1h": 0.42,
    }
    hits_p = agy.evaluate_v6_short_setups(**para)
    assert [h["setup"] for h in hits_p] == ["PARA75_G_1H"]
    assert hits_p[0]["stop_pct"] == 0.075 and hits_p[0]["tp_pct"] == 0.600 and hits_p[0]["score"] > 332.0

    # 3. DCAT_WICK_1H fires with Priority #1 (score > 368) and TP 60%
    dcat = {
        **base,
        "age_days": 180.0, "breadth_1d": 0.25, "d_sma20": 1.15,
        "o_1h": 0.99, "hi_1h": 1.04, "lo_1h": 0.94, "c_1h": 0.96, "cur_px": 0.96,
        "qv_1h": 1_800_000.0, "qv_avg20_1h": 1_000_000.0, "ret_4h": 0.07, "tb_1h": 0.41, "bas8h": 0.0001,
    }
    hits_d = agy.evaluate_v6_short_setups(**dcat)
    assert [h["setup"] for h in hits_d] == ["DCAT_WICK_1H"]
    assert hits_d[0]["stop_pct"] == 0.050 and hits_d[0]["tp_pct"] == 0.600 and hits_d[0]["score"] > 368.0

    # 4. BASCRUSH_4H fires with Priority #2 (score > 335) and beats NEW7 when both hit!
    bc4 = {
        **base,
        "age_days": 4.0, "breadth_1d": 0.35, "btc_ret7d": 0.02, "has_spot_24h": True,
        "is_fresh_4h_window": True, "bar_complete_4h": True,
        "bas8h": 0.0012, "bas12h_max": 0.0028, "bas_4h_close": 0.0010,
        "o_4h": 1.00, "c_4h": 0.95, "c_4h_prev": 1.01, "ema10_4h": 0.98, "ema10_4h_prev": 0.99,
        "cur_px": 0.95, "tb_4h": 0.41,
    }
    hits_b = agy.evaluate_v6_short_setups(**bc4)
    assert hits_b[0]["setup"] == "BASCRUSH_4H" and hits_b[0]["score"] > hits_b[-1]["score"]
    assert hits_b[0]["stop_pct"] == 0.060 and hits_b[0]["tp_pct"] == 0.600

    # 5. Live stats aggregation
    st = agy.compute_live_stats([
        {"event": "close", "setup": "BASCRUSH_4H", "pnl_usdt": "12.50"},
        {"event": "close", "setup": "BASCRUSH_4H", "pnl_usdt": "-4.50"},
        {"event": "close", "setup": "DCAT_WICK_1H", "pnl_usdt": "15.00"},
        {"event": "open", "setup": "NEW7_BAS_1H"},
    ])
    assert st["overall"]["n"] == 3 and st["overall"]["wins"] == 2 and st["overall"]["net_usdt"] == "23.00"


def test_v7_spotdmp_hybrid_exit_1m_sanitize_and_24h_promote():
    # 1. SPOTDMP_4H triggers on BD <= 0.40, spot_tb_4h < 0.40, ps_vrat >= 2.0, clean basis >= -0.05%, fresh 4H cross < EMA10
    sd_base = dict(
        age_days=120.0, breadth_1d=0.35, btc_ret7d=-0.02, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.97, hi_1h=0.98, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.98,
        qv_1h=1_000_000.0, qv_avg20_1h=900_000.0, tb_1h=0.45, ema10_1h=0.97, ema10_1h_prev=0.975,
        atr14_1h_pct=0.015, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0001, bas12h_max=0.0010, bas_4h_close=0.0001,
        spot_tb_4h=0.37, ps_vrat=2.8, is_fresh_4h_window=True, bar_complete_4h=True,
        o_4h=1.00, c_4h=0.95, c_4h_prev=1.01, tb_4h=0.41, ema10_4h=0.98, ema10_4h_prev=0.99,
    )
    hits_sd = agy.evaluate_v8_short_setups(**sd_base)
    assert [h["setup"] for h in hits_sd] == ["SPOTDMP_4H"]
    assert hits_sd[0]["stop_pct"] == 0.050 and hits_sd[0]["tp_pct"] == 0.600 and hits_sd[0]["exit_mode"] == "V18_AA_SQZ96"
    assert hits_sd[0]["score"] < 260.0  # stays below NEW7/core priority
    # Blocked by BD > 0.40, spot_tb_4h >= 0.40, ps_vrat < 2.0, crowded basis < -0.05%, or non-4H window
    assert agy.evaluate_v8_short_setups(**{**sd_base, "breadth_1d": 0.42}) == []
    assert agy.evaluate_v8_short_setups(**{**sd_base, "spot_tb_4h": 0.40}) == []
    assert agy.evaluate_v8_short_setups(**{**sd_base, "ps_vrat": 1.99}) == []
    assert agy.evaluate_v8_short_setups(**{**sd_base, "ps_vrat": None}) == []
    assert agy.evaluate_v8_short_setups(**{**sd_base, "bas8h": -0.0008}) == []
    assert agy.evaluate_v8_short_setups(**{**sd_base, "is_fresh_4h_window": False}) == []

    # NEW7_BAS_1H gets V12_NEW7_SQZ96 exit_mode and requires >= 22 1h bars (matches backtest)
    n7_base = {**sd_base, "age_days": 2.0, "is_fresh_4h_window": False, "tb_1h": 0.42, "ret_24h": None}
    hits_n7 = agy.evaluate_v8_short_setups(**n7_base)
    assert [h["setup"] for h in hits_n7] == ["NEW7_BAS_1H"] and hits_n7[0]["exit_mode"] == "V21_NEW7_SQZ96"
    assert agy.evaluate_v8_short_setups(**{**n7_base, "bars_1h_count": 21}) == []

    # 2. 3-tuple spot_map computes exact spot_tb_4h
    base_dt = dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc)
    m15_t = [(base_dt + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(16)]
    m15_cl = [100.05] * 16
    spot_map = {t: (100.0, 50_000.0, 19_000.0) for t in m15_t}
    bst = agy.compute_spot_basis_stats(
        m15_t, m15_cl, spot_map,
        target_4h_last_15m_str=m15_t[-1], end_ts_str=m15_t[-1], target_4h_start_str=m15_t[0],
    )
    assert bst["has_spot_24h"] is True and abs(bst["spot_tb_4h"] - 0.38) < 1e-9

    # 3. V7_AA_HYB96 exit mode: MFE >= 5.5% -> HALF55 (stop 101.5 = -1.5% risk), then LOCK8 -> TRAIL13 -> RUNNER96
    opened = dt.datetime(2026, 11, 24, 0, 0, 0, tzinfo=dt.timezone.utc)
    t0 = int(opened.timestamp() * 1000)
    bms = 900_000
    meta_v7 = {"exit_mode": "V7_AA_HYB96", "stop_pct": "0.060", "tp_pct": "0.40"}
    ev_h = agy.evaluate_position_exit(meta_v7, "100", "96.0", [(t0, Decimal("101.0"), Decimal("94.4"), Decimal("96.0"))], opened.isoformat(), opened + dt.timedelta(minutes=20))
    assert ev_h["action"] == "HOLD" and ev_h["ratchet_stage"] == "HALF55" and Decimal(ev_h["stop_px"]) == Decimal("101.5")
    # Reversal hitting HALF55 stop closes with HALF_STOP_15M at -1.5% instead of -6.0%!
    ev_hs = agy.evaluate_position_exit(ev_h["updated_meta"], "100", "101.6", [(t0 + bms, Decimal("101.6"), Decimal("96.0"), Decimal("101.5"))], opened.isoformat(), opened + dt.timedelta(minutes=35))
    assert ev_hs["action"] == "MUST_CLOSE" and "HALF_STOP_15M" in ev_hs["exit_reason"]
    # Heat calculation sees HALF55 reduced risk (3.00 USDT on 200U notional, 1.50 USDT on 100U qty=1)
    h_u, _ = agy.compute_open_heat_and_counts({"shorts": [{"id": "p1", "symbol": "AAAUSDT", "qty": "1", "entry": "100", "on_binance": True}]}, {"p1": ev_h["updated_meta"]})
    assert h_u == Decimal("1.50")
    # Continuation past +8.5% transitions HALF55 -> LOCK8 (97.5)
    ev_lk = agy.evaluate_position_exit(ev_h["updated_meta"], "100", "93.0", [(t0 + bms, Decimal("97.0"), Decimal("91.5"), Decimal("93.0"))], opened.isoformat(), opened + dt.timedelta(minutes=35))
    assert ev_lk["action"] == "HOLD" and ev_lk["ratchet_stage"] == "LOCK8" and Decimal(ev_lk["stop_px"]) == Decimal("97.5")

    # 4. sanitize_first_bar_1m eliminates pre-entry wick spike at 00:00:05 before 00:00:15 fill
    fill_dt = dt.datetime(2026, 11, 24, 0, 0, 15, tzinfo=dt.timezone.utc)
    bars_15m = [(t0, Decimal("108.0"), Decimal("95.0"), Decimal("98.0"))]  # 108.0 occurred at 00:00:05 pre-entry!
    bars_1m = [
        (t0, Decimal("108.0"), Decimal("99.0"), Decimal("100.0")),            # 00:00..00:01 (includes pre-entry)
        (t0 + 60_000, Decimal("101.0"), Decimal("97.5"), Decimal("99.0")),    # 00:01..00:02 post-entry
        (t0 + 120_000, Decimal("100.5"), Decimal("97.0"), Decimal("98.0")),   # 00:02..00:03 post-entry
    ]
    san = agy.sanitize_first_bar_1m(bars_15m, bars_1m, fill_dt.isoformat(), fill_dt + dt.timedelta(minutes=20))
    assert len(san) == 1 and san[0][1] == Decimal("101.0") and san[0][2] == Decimal("97.0") and san[0][3] == Decimal("98.0")
    ev_san = agy.evaluate_position_exit(meta_v7, "100", "98.0", san, fill_dt.isoformat(), fill_dt + dt.timedelta(minutes=20))
    assert ev_san["action"] == "HOLD"  # zero phantom SL!

    # 5. check_fast_guard_events detects 24h runner promotion immediately
    now_24 = opened + dt.timedelta(hours=24, minutes=1)
    pf_24 = {"shorts": [{"id": "p_win", "symbol": "WINUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()}]}
    meta_24 = {"p_win": {"exit_mode": "V7_AA_HYB96", "stop_pct": "0.06", "tp_pct": "0.40", "stop_px": "106.0", "tp_px": "60.0", "runner_unlocked": False}}
    br_24, prom_24 = agy.check_fast_guard_events(meta_24, pf_24, {"WINUSDT": Decimal("93.5")}, now_24)
    assert br_24 == [] and prom_24 == ["p_win"]


def test_spot_basis_4h_flow_fast_guard_and_1h_patch():
    hrs = [f"2026-11-24 {h:02d}:00:00" for h in range(8)]
    ops = [100.0] * 8
    his = [105.0, 108.0, 104.0, 103.0, 102.0, 103.0, 101.0, 100.0]
    los = [98.0, 99.0, 97.0, 96.0, 95.0, 94.0, 93.0, 92.0]
    cls = [102.0, 101.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0]
    qvs = [100.0, 300.0, 100.0, 100.0, 200.0, 200.0, 200.0, 200.0]
    tbs = [0.60, 0.40, 0.50, 0.50, 0.40, 0.40, 0.40, 0.40]
    cnts = [4] * 8
    rows4 = agy._hourly_to_4h_flow(hrs, ops, his, los, cls, qvs, tbs, cnts)
    assert len(rows4) == 2 and rows4[0][7] == 16 and abs(rows4[1][6] - 0.40) < 1e-9

    base_dt = dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc)
    m15_t = [(base_dt + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(48)]
    m15_cl = [100.20 if i < 20 else 100.05 for i in range(48)]
    spot_map = {t: (100.0, 20_000.0) for t in m15_t}
    st = agy.compute_spot_basis_stats(m15_t, m15_cl, spot_map, target_4h_last_15m_str=m15_t[-1], end_ts_str=m15_t[-1])
    assert st["has_spot_24h"] is True and abs(st["bas12h_max"] - 0.0020) < 1e-9 and abs(st["bas_4h_close"] - 0.0005) < 1e-9

    # check_fast_mark_breach handles V6_LT8_96 (holds at 75h, closes at 96h) and V4_LT8_72 (closes at 72h)
    now_u = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
    pf = {
        "shorts": [
            {"id": "p1", "symbol": "AAAUSDT", "entry": "100", "on_binance": True, "opened_at": (now_u - dt.timedelta(hours=2)).isoformat()},
            {"id": "p2", "symbol": "BBBUSDT", "entry": "100", "on_binance": True, "opened_at": (now_u - dt.timedelta(hours=2)).isoformat()},
            {"id": "p3", "symbol": "CCCUSDT", "entry": "100", "on_binance": True, "opened_at": (now_u - dt.timedelta(hours=75)).isoformat()},
            {"id": "p4", "symbol": "DDDUSDT", "entry": "100", "on_binance": True, "opened_at": (now_u - dt.timedelta(hours=96)).isoformat()},
        ]
    }
    meta = {
        "p1": {"exit_mode": "V6_LT8_96", "stop_pct": "0.06", "tp_pct": "0.40", "stop_px": "106.0", "tp_px": "60.0"},
        "p2": {"exit_mode": "V6_LT8_96", "stop_pct": "0.06", "tp_pct": "0.40", "stop_px": "106.0", "tp_px": "60.0"},
        "p3": {"exit_mode": "V6_LT8_96", "stop_pct": "0.06", "tp_pct": "0.40", "stop_px": "97.5", "tp_px": "60.0", "runner_unlocked": True},
        "p4": {"exit_mode": "V6_LT8_96", "stop_pct": "0.06", "tp_pct": "0.40", "stop_px": "97.5", "tp_px": "60.0", "runner_unlocked": True},
    }
    marks = {"AAAUSDT": Decimal("106.1"), "BBBUSDT": Decimal("59.9"), "CCCUSDT": Decimal("88.0"), "DDDUSDT": Decimal("88.0")}
    br = agy.check_fast_mark_breach(meta, pf, marks, now_u)
    by_id = {b["id"]: b["reason"] for b in br}
    assert "p1" in by_id and "FAST_SL_MARK" in by_id["p1"]
    assert "p2" in by_id and "FAST_TP_MARK" in by_id["p2"]
    assert "p3" not in by_id
    assert "p4" in by_id and "FAST_MAX_HOLD_96H" in by_id["p4"]

    # _patch_live_1h_from_binance: appends closed target bar and ignores still-open bar
    t_prev = int(dt.datetime(2026, 11, 24, 2, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    t_tgt = int(dt.datetime(2026, 11, 24, 3, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    t_open = int(dt.datetime(2026, 11, 24, 4, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    orig_fetch = agy._fetch_rest_klines
    try:
        agy._fetch_rest_klines = lambda url, sym, interval, limit=3, timeout=10: (
            sym,
            [
                [t_tgt, "10.0", "10.5", "9.5", "9.7", "100", t_tgt + 3599999, "1000.0", 50, "40", "400.0", "0"],
                [t_open, "9.7", "9.8", "9.6", "9.65", "10", t_open + 3599999, "100.0", 5, "5", "50.0", "0"],
            ],
        )
        hf = {"TESTUSDT": (["2026-11-24 02:00:00"], [10.2], [10.3], [9.9], [10.0], [500.0], [0.5], [4])}
        n_p = agy._patch_live_1h_from_binance(hf, ["TESTUSDT"], "2026-11-24 03:00:00", dt.datetime(2026, 11, 24, 4, 0, 10, tzinfo=dt.timezone.utc))
        assert n_p == 1
        assert hf["TESTUSDT"][0] == ["2026-11-24 02:00:00", "2026-11-24 03:00:00"]
        assert hf["TESTUSDT"][4][-1] == 9.7 and abs(hf["TESTUSDT"][6][-1] - 0.4) < 1e-9 and hf["TESTUSDT"][7][-1] == 4
    finally:
        agy._fetch_rest_klines = orig_fetch


def test_live_bars_to_m15_series():
    now_utc = dt.datetime(2026, 11, 22, 21, 46, 0, tzinfo=dt.timezone.utc)
    t_old = int(dt.datetime(2026, 11, 21, 23, 45, tzinfo=dt.timezone.utc).timestamp() * 1000)
    t_prev = int(dt.datetime(2026, 11, 22, 21, 15, tzinfo=dt.timezone.utc).timestamp() * 1000)
    t_closed = int(dt.datetime(2026, 11, 22, 21, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)
    t_open = int(dt.datetime(2026, 11, 22, 21, 45, tzinfo=dt.timezone.utc).timestamp() * 1000)
    bars = [
        (t_open, Decimal("1.05"), Decimal("0.98"), Decimal("1.02")),
        (t_closed, Decimal("1.04"), Decimal("0.99"), Decimal("1.00")),
        (t_old, Decimal("1.10"), Decimal("1.05"), Decimal("1.08")),
        (t_prev, Decimal("1.06"), Decimal("1.00"), Decimal("1.03")),
    ]
    ts, cl, hi, lo = agy._live_bars_to_m15_series(bars, now_utc, start_iso="2026-11-22")
    assert ts == ["2026-11-22 21:15:00", "2026-11-22 21:30:00"]
    assert agy._compute_m15_stats(cl, hi, lo, 1.0) == (None,) * 12


def test_scrub_ips():
    raw = "proxy=203.0.113.45 host=192.168.1.10 local=127.0.0.1 lan=10.0.0.5 wg=172.16.5.2 pub=198.51.100.7 ver=3.14.4 px=0.00123"
    out = agy.scrub_ips(raw)
    assert "203.0.113.45" not in out and "198.51.100.7" not in out and out.count("<ip>") == 2
    assert "192.168.1.10" in out and "127.0.0.1" in out and "ver=3.14.4" in out


def test_compact_scan_helpers():
    sample = {"a": 1, "b": "ไทย"}
    assert agy._dump_json(sample, pretty=False) == '{"a":1,"b":"ไทย"}'
    assert agy._extract_last_round_regime([{"event": "round", "regime": "BEARISH_FAVORABLE (BTC)"}]) == "BEARISH_FAVORABLE"
    ccand = agy._compact_candidate({
        "symbol": "TESTUSDT", "coin": "TEST", "primary_setup": "DCAT_WICK_1H", "setups": ["DCAT_WICK_1H"],
        "score": 395.0, "age_days": 120.0, "price": 1.0, "tb_1h": 0.41, "atr1h_pct": 1.85,
        "suggested_stop_pct": 0.05, "suggested_tp_pct": 0.40, "exit_mode": "V6_LT8_96",
    })
    assert "coin" not in ccand and ccand["atr1h_pct"] == 1.85 and ccand["exit_mode"] == "V6_LT8_96"


def test_v8_sqz_cut_trail22_and_bugfixes():
    opened = dt.datetime(2026, 11, 24, 4, 0, 0, tzinfo=dt.timezone.utc)
    t0 = int(opened.timestamp() * 1000)
    bms = 900_000

    # 1. V8_AA_SQZ96 Two-Stage Runner Trail: MFE >= 22% tightens trail from 4.5% -> 3.5% (TRAIL22)
    meta_v8 = {"exit_mode": "V8_AA_SQZ96", "stop_pct": "0.060", "tp_pct": "0.40"}
    bars_big = [
        (t0, Decimal("101.0"), Decimal("85.0"), Decimal("86.0")),          # MFE +15% -> TRAIL13 (stop 89.5 = +10.5%)
        (t0 + bms, Decimal("88.0"), Decimal("76.0"), Decimal("78.0")),    # MFE +24% -> TRAIL22 (stop 79.5 = +20.5%!)
    ]
    ev_tr2 = agy.evaluate_position_exit(meta_v8, "100", "78.0", bars_big, opened.isoformat(), opened + dt.timedelta(hours=1))
    assert ev_tr2["action"] == "HOLD" and ev_tr2["ratchet_stage"] == "TRAIL22"
    assert Decimal(ev_tr2["stop_px"]) == Decimal("79.5")

    # 2. V8 BTC Squeeze Emergency Cut (causal 15m): cuts unprotected losing INIT position when BTC 1h >= +2.2%
    # Build BTC 15m bars from t0 - 4h through t0 + 15m; BTC jumps +2.3% at t0 + bms
    btc_bars = [(t0 - k * bms, Decimal("60000"), Decimal("59900"), Decimal("60000")) for k in range(16, 0, -1)]
    btc_bars.append((t0, Decimal("60200"), Decimal("59900"), Decimal("60100")))
    btc_bars.append((t0 + bms, Decimal("61500"), Decimal("60100"), Decimal("61380")))  # +2.30% vs 1h ago (60000)
    alt_bars = [
        (t0, Decimal("101.0"), Decimal("99.5"), Decimal("100.5")),        # opening bar: no sqz cut on bar 0
        (t0 + bms, Decimal("103.0"), Decimal("100.5"), Decimal("102.5")), # losing (-2.5%), stage=INIT -> BTC_SQUEEZE_CUT_15M!
    ]
    ev_sqz = agy.evaluate_position_exit(
        meta_v8, "100", "102.5", alt_bars, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_bars
    )
    assert ev_sqz["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in ev_sqz["exit_reason"]

    # Profitable position (bar_pnl > 0) MUST NOT be cut by BTC squeeze!
    alt_win = [
        (t0, Decimal("100.5"), Decimal("98.0"), Decimal("98.5")),
        (t0 + bms, Decimal("99.5"), Decimal("97.5"), Decimal("98.0")),
    ]
    ev_win = agy.evaluate_position_exit(
        meta_v8, "100", "98.0", alt_win, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_bars
    )
    assert ev_win["action"] == "HOLD"

    # Protected HALF55 position MUST NOT be cut by BTC squeeze (already capped at -1.5%)
    meta_half = {**meta_v8, "ratchet_stage": "HALF55", "stop_px": "101.5", "mfe_pct": "0.060"}
    alt_half = [
        (t0, Decimal("101.0"), Decimal("94.0"), Decimal("99.0")),
        (t0 + bms, Decimal("101.2"), Decimal("99.0"), Decimal("101.0")),
    ]
    ev_half = agy.evaluate_position_exit(
        meta_half, "100", "101.0", alt_half, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_bars
    )
    assert ev_half["action"] == "HOLD"

    # 3. Fast-guard live BTC squeeze check via cached btc_ref
    now_fg = opened + dt.timedelta(minutes=35)
    pf_fg = {"shorts": [{"id": "p_sqz", "symbol": "AAAUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()}]}
    meta_fg = {"p_sqz": {"exit_mode": "V8_NEW7_SQZ96", "stop_pct": "0.06", "tp_pct": "0.40", "stop_px": "106.0", "tp_px": "60.0", "ratchet_stage": "INIT"}}
    btc_ref_ok = {"at": (now_fg - dt.timedelta(minutes=2)).isoformat(), "ref_1h_close": "60000", "ref_4h_close": "60000"}
    br_sqz, _ = agy.check_fast_guard_events(meta_fg, pf_fg, {"AAAUSDT": Decimal("102.0"), "BTCUSDT": Decimal("61400")}, now_fg, btc_ref=btc_ref_ok)
    assert len(br_sqz) == 1 and "FAST_BTC_SQUEEZE_CUT" in br_sqz[0]["reason"]
    # Stale btc_ref (> 10m old) MUST be ignored
    btc_ref_stale = {"at": (now_fg - dt.timedelta(minutes=15)).isoformat(), "ref_1h_close": "60000", "ref_4h_close": "60000"}
    br_stale, _ = agy.check_fast_guard_events(meta_fg, pf_fg, {"AAAUSDT": Decimal("102.0"), "BTCUSDT": Decimal("61400")}, now_fg, btc_ref=btc_ref_stale)
    assert br_stale == []

    # 4. Bug fix #1: post-entry sanitized first bar opened at :05:00 (>3m into bar) is NOT dropped!
    late_open = dt.datetime(2026, 11, 24, 4, 5, 0, tzinfo=dt.timezone.utc)
    san_bars = [(t0, Decimal("101.0"), Decimal("94.0"), Decimal("95.0"))]  # MFE +6% post-entry
    ev_san_late = agy.evaluate_position_exit(
        meta_v8, "100", "95.0", san_bars, late_open.isoformat(), late_open + dt.timedelta(minutes=15), first_bar_sanitized=True
    )
    assert ev_san_late["ratchet_stage"] == "HALF55" and Decimal(ev_san_late["mfe_pct"]) == Decimal("6.00")

    # 5. Bug fix #2: _patch_live_1h_from_binance repairs an incomplete earlier hour inside the 4H window
    orig_fetch = agy._fetch_rest_klines
    try:
        t00 = int(dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        t01 = int(dt.datetime(2026, 11, 24, 1, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        t02 = int(dt.datetime(2026, 11, 24, 2, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        t03 = int(dt.datetime(2026, 11, 24, 3, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        agy._fetch_rest_klines = lambda url, sym, interval, limit=6, timeout=10: (
            sym,
            [
                [t00, "10", "11", "9", "10", "10", t00 + 3599999, "1000", 10, "4", "400", "0"],
                [t01, "10", "11", "9", "9.8", "10", t01 + 3599999, "1000", 10, "4", "400", "0"],
                [t02, "9.8", "10", "9", "9.5", "10", t02 + 3599999, "1000", 10, "4", "400", "0"],
                [t03, "9.5", "10", "9", "9.2", "10", t03 + 3599999, "1000", 10, "4", "400", "0"],
            ],
        )
        # Hour 01:00 has cnt=3 (incomplete) even though 03:00 is complete!
        hf = {
            "GAPUSDT": (
                ["2026-11-24 00:00:00", "2026-11-24 01:00:00", "2026-11-24 02:00:00", "2026-11-24 03:00:00"],
                [10.0, 10.0, 9.8, 9.5], [11.0, 11.0, 10.0, 10.0], [9.0, 9.0, 9.0, 9.0], [10.0, 9.9, 9.5, 9.2],
                [1000.0, 700.0, 1000.0, 1000.0], [0.4, 0.4, 0.4, 0.4], [4, 3, 4, 4],
            )
        }
        n_p = agy._patch_live_1h_from_binance(hf, ["GAPUSDT"], "2026-11-24 03:00:00", dt.datetime(2026, 11, 24, 4, 0, 10, tzinfo=dt.timezone.utc))
        assert n_p == 1 and hf["GAPUSDT"][7] == [4, 4, 4, 4] and hf["GAPUSDT"][4][1] == 9.8
    finally:
        agy._fetch_rest_klines = orig_fetch


def test_v9_spotdmp1h_new7_half65_tr3_tp60_and_fixes():
    # 1. SPOTDMP_1H Sniper fires on high-purity 1H institutional spot distribution + perp leverage fuel
    sd1_base = dict(
        age_days=120.0, breadth_1d=0.35, btc_ret7d=-0.02, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.99, hi_1h=1.00, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.985,
        qv_1h=1_500_000.0, qv_avg20_1h=900_000.0, tb_1h=0.39, ema10_1h=0.975, ema10_1h_prev=0.980,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0001, ps_vrat=2.2,
        spot_1h_complete=True, spot_tb_1h=0.32, spot_vrat_1h=1.7,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_1h = agy.evaluate_v9_short_setups(**sd1_base)
    assert [h["setup"] for h in hits_1h] == ["SPOTDMP_1H", "SPOTFLOW_POS_1H"]
    assert hits_1h[0]["stop_pct"] == 0.050 and hits_1h[0]["tp_pct"] == 0.600 and hits_1h[0]["exit_mode"] == "V18_AA_SQZ96"
    assert hits_1h[0]["score"] < 260.0 and hits_1h[0]["score"] > hits_1h[1]["score"]  # stays below core & above SPOTFLOW_POS_1H
    # Strict guards on SPOTDMP_1H
    assert "SPOTDMP_1H" not in [h["setup"] for h in agy.evaluate_v9_short_setups(**{**sd1_base, "ps_vrat": 1.99})]
    assert agy.evaluate_v9_short_setups(**{**sd1_base, "bas8h": -0.0001}) == []
    assert agy.evaluate_v9_short_setups(**{**sd1_base, "bas8h": None}) == []
    assert "SPOTDMP_1H" not in [h["setup"] for h in agy.evaluate_v9_short_setups(**{**sd1_base, "spot_tb_1h": 0.35})]
    assert agy.evaluate_v9_short_setups(**{**sd1_base, "spot_vrat_1h": 1.49}) == []
    assert agy.evaluate_v9_short_setups(**{**sd1_base, "tb_1h": 0.42}) == []
    assert agy.evaluate_v9_short_setups(**{**sd1_base, "lo_1h": 0.90}) == []  # lwick = 0.05/0.10 = 0.50 >= 0.30!
    assert "SPOTDMP_1H" not in [h["setup"] for h in agy.evaluate_v9_short_setups(**{**sd1_base, "breadth_1d": 0.41})]
    assert agy.evaluate_v9_short_setups(**{**sd1_base, "spot_1h_complete": False}) == []

    # 2. _spot_15m_to_1h_flow aggregates exact 1H spot_tb & 20-hour spot_vrat; rejects incomplete hour
    base_dt = dt.datetime(2026, 11, 23, 0, 0, tzinfo=dt.timezone.utc)
    smap = {}
    for h in range(21):
        qv_bar = 25_000.0 if h < 20 else 50_000.0  # target hour = 200k vs 100k avg20 -> vrat = 2.0x
        tb_bar = qv_bar * (0.50 if h < 20 else 0.30)
        for q in range(4):
            t_s = (base_dt + dt.timedelta(hours=h, minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S")
            smap[t_s] = (100.0, qv_bar, tb_bar)
    fl = agy._spot_15m_to_1h_flow(smap, "2026-11-23 20:00:00", end_ts_str="2026-11-23 20:45:00")
    assert fl["spot_1h_complete"] is True and abs(fl["spot_tb_1h"] - 0.30) < 1e-9 and abs(fl["spot_vrat_1h"] - 2.0) < 1e-9
    smap.pop("2026-11-23 20:45:00")
    fl_inc = agy._spot_15m_to_1h_flow(smap, "2026-11-23 20:00:00", end_ts_str="2026-11-23 20:45:00")
    assert fl_inc["spot_1h_complete"] is False and fl_inc["spot_tb_1h"] is None

    # 3. compute_spot_basis_stats spot_qv_24h_override prevents CH gap from deflating spot_qv_24h
    m15_t = [(base_dt + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(16)]
    bst_ov = agy.compute_spot_basis_stats(
        m15_t, [100.1] * 16, {t: (100.0, 10_000.0, 4_000.0) for t in m15_t},
        spot_qv_24h_override=2_000_000.0,
    )
    assert bst_ov["has_spot_24h"] is True and bst_ov["spot_qv_24h"] == 2_000_000.0

    # 4. V9_NEW7_SQZ96: MFE >= 6.5% -> HALF65 (-2.0% stop = 102.0, heat 2.00U), MFE >= 30% -> TRAIL30 (-2.5%), TP 60%
    opened = dt.datetime(2026, 11, 24, 4, 0, 0, tzinfo=dt.timezone.utc)
    t0 = int(opened.timestamp() * 1000)
    bms = 900_000
    meta_n7 = {"exit_mode": "V9_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60"}
    ev_h65 = agy.evaluate_position_exit(meta_n7, "100", "95.0", [(t0, Decimal("101.0"), Decimal("93.4"), Decimal("95.0"))], opened.isoformat(), opened + dt.timedelta(minutes=20))
    assert ev_h65["action"] == "HOLD" and ev_h65["ratchet_stage"] == "HALF65"
    assert Decimal(ev_h65["stop_px"]) == Decimal("102") and Decimal(ev_h65["tp_px"]) == Decimal("40")
    h_n7, _ = agy.compute_open_heat_and_counts({"shorts": [{"id": "n1", "symbol": "NEWUSDT", "qty": "1", "entry": "100", "on_binance": True}]}, {"n1": ev_h65["updated_meta"]})
    assert h_n7 == Decimal("2.00")
    # Reversal hitting HALF65 closes with HALF_STOP_15M
    ev_hs65 = agy.evaluate_position_exit(ev_h65["updated_meta"], "100", "102.2", [(t0 + bms, Decimal("102.2"), Decimal("95.0"), Decimal("102.1"))], opened.isoformat(), opened + dt.timedelta(minutes=35))
    assert ev_hs65["action"] == "MUST_CLOSE" and "HALF_STOP_15M" in ev_hs65["exit_reason"]
    # Mega-dump MFE +32% -> TRAIL30 (stop = 100 * (1 - 0.295) = 70.5)
    ev_tr3 = agy.evaluate_position_exit(ev_h65["updated_meta"], "100", "70.0", [(t0 + bms, Decimal("95.0"), Decimal("68.0"), Decimal("70.0"))], opened.isoformat(), opened + dt.timedelta(minutes=35))
    assert ev_tr3["action"] == "HOLD" and ev_tr3["ratchet_stage"] == "TRAIL30" and Decimal(ev_tr3["stop_px"]) == Decimal("70.5")

    # 5. V9 rolling 15m BTC reference closes pick exact ~1h/4h anchor (±7.5m) instead of drifted ref_1h_close
    now_fg = dt.datetime(2026, 11, 24, 6, 44, 0, tzinfo=dt.timezone.utc)
    t_530 = int(dt.datetime(2026, 11, 24, 5, 15, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)  # closes 05:30 (74m ago)
    t_545 = int(dt.datetime(2026, 11, 24, 5, 30, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)  # closes 05:45 (59m ago!)
    btc_ref_roll = {
        "at": (now_fg - dt.timedelta(minutes=1)).isoformat(),
        "ref_1h_close": "59000",  # stale 74m-ago bar would falsely trigger +2.3%
        "ref_4h_close": "60000",
        "closes_15m": {str(t_530): "59000", str(t_545): "60000"},
    }
    # BTC mark = 60350 -> +0.58% vs exact 59m bar (60000), NOT +2.29% vs drifted 74m bar (59000)!
    hit_no, r1_no, _ = agy._eval_live_btc_squeeze({"BTCUSDT": Decimal("60350")}, btc_ref_roll, now_fg)
    assert hit_no is False and r1_no < Decimal("0.01")
    # BTC mark = 61350 -> +2.25% vs exact 59m bar (60000) -> triggers V9 fast-guard BTC squeeze!
    pf_v9 = {"shorts": [{"id": "p_v9", "symbol": "AAAUSDT", "entry": "100", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()}]}
    meta_v9 = {"p_v9": {"exit_mode": "V9_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60", "stop_px": "105.0", "tp_px": "40.0", "ratchet_stage": "INIT"}}
    br_v9, _ = agy.check_fast_guard_events(meta_v9, pf_v9, {"AAAUSDT": Decimal("101.5"), "BTCUSDT": Decimal("61350")}, now_fg, btc_ref=btc_ref_roll)
    assert len(br_v9) == 1 and "FAST_BTC_SQUEEZE_CUT" in br_v9[0]["reason"]

    # 6. Wall-clock aligned fast-guard deadline prevents minute-boundary lock overrun
    d_sec = agy._fast_guard_deadline_sec(55, dt.datetime(2026, 11, 24, 6, 5, 12, tzinfo=dt.timezone.utc))
    assert 43.0 <= d_sec <= 44.0
    assert agy._btc_ref_is_fresh(btc_ref_roll, now_fg) is True
    assert agy._btc_ref_is_fresh({"at": (now_fg - dt.timedelta(minutes=12)).isoformat()}, now_fg) is False


def test_v10_splead4h_lw35_aa10_sqz20_and_fixes():
    assert "SP_LEAD_4H" not in agy.ENGINE_MAX_OPEN
    assert agy.ENGINE_HEAT_CAP == {}

    # 1. SP_LEAD_4H fires on 4H institutional Spot-Leads-Perp divergence even without ps_vrat>=2 or tb_4h<0.44
    spl_base = dict(
        age_days=120.0, breadth_1d=0.42, btc_ret7d=-0.02, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.97, hi_1h=0.98, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.98,
        qv_1h=1_000_000.0, qv_avg20_1h=900_000.0, tb_1h=0.46, ema10_1h=0.97, ema10_1h_prev=0.975,
        atr14_1h_pct=0.015, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0002, ps_vrat=1.10,
        spot_4h_complete=True, spot_tb_4h=0.33, spot_vrat_4h=1.45,
        is_fresh_4h_window=True, bar_complete_4h=True,
        o_4h=1.00, hi_4h=1.01, lo_4h=0.94, c_4h=0.95, c_4h_prev=1.01, tb_4h=0.46, ema10_4h=0.98, ema10_4h_prev=0.99,
    )
    hits_spl = agy.evaluate_v10_short_setups(**spl_base)
    assert [h["setup"] for h in hits_spl] == ["SP_LEAD_4H"]
    assert hits_spl[0]["stop_pct"] == 0.050 and hits_spl[0]["tp_pct"] == 0.600 and hits_spl[0]["exit_mode"] == "V18_AA_SQZ96"
    assert 246.0 < hits_spl[0]["score"] < 330.0
    # Strict guards on SP_LEAD_4H
    assert agy.evaluate_v10_short_setups(**{**spl_base, "breadth_1d": 0.46}) == []
    assert agy.evaluate_v10_short_setups(**{**spl_base, "bas8h": -0.0001}) == []
    assert agy.evaluate_v10_short_setups(**{**spl_base, "lo_4h": 0.91}) == []  # lwick = 0.04/0.10 = 0.40 >= 0.30
    assert agy.evaluate_v10_short_setups(**{**spl_base, "spot_4h_complete": False}) == []
    assert agy.evaluate_v10_short_setups(**{**spl_base, "spot_tb_4h": 0.38}) == []
    assert [h["setup"] for h in agy.evaluate_v10_short_setups(**{**spl_base, "tb_4h": 0.38})] == ["SPOTFLOW_POS_4H"]  # SP_LEAD_4H blocked (div=0.05 < 0.06); V16 SPOTFLOW_POS_4H matches
    assert agy.evaluate_v10_short_setups(**{**spl_base, "tb_4h": 0.38, "spot_vrat_4h": 1.35}) == []  # div=0.05 < 0.06 and spot_vrat<1.4 blocks SPOTFLOW_POS_4H too
    assert agy.evaluate_v10_short_setups(**{**spl_base, "spot_vrat_4h": 1.19}) == []

    # 2. SPOTDMP_4H clean-close guard (lwick_4h < 0.35 rejects hammer dips)
    sd4_base = {
        **spl_base, "breadth_1d": 0.35, "ps_vrat": 2.5, "spot_tb_4h": 0.37, "tb_4h": 0.40,
        "spot_vrat_4h": 1.0,  # blocks SP_LEAD_4H so SPOTDMP_4H is tested cleanly
    }
    assert [h["setup"] for h in agy.evaluate_v10_short_setups(**sd4_base)] == ["SPOTDMP_4H"]  # lwick=0.143 < 0.35
    assert agy.evaluate_v10_short_setups(**{**sd4_base, "lo_4h": 0.91}) == []  # lwick=0.40 >= 0.35 rejected!

    # 3. _spot_15m_to_4h_flow aggregates 16/16 bars & 20-bar 4H SMA volume ratio
    base_dt = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)
    smap4 = {}
    for b4 in range(21):
        qv_bar = 10_000.0 if b4 < 20 else 15_000.0  # target 4H = 240k vs 160k avg20 -> vrat = 1.5x
        tb_bar = qv_bar * (0.50 if b4 < 20 else 0.32)
        for q in range(16):
            t_s = (base_dt + dt.timedelta(hours=4 * b4, minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S")
            smap4[t_s] = (100.0, qv_bar, tb_bar)
    tgt_4h = (base_dt + dt.timedelta(hours=80)).strftime("%Y-%m-%d %H:%M:%S")
    end_4h = (base_dt + dt.timedelta(hours=83, minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
    fl4 = agy._spot_15m_to_4h_flow(smap4, tgt_4h, end_ts_str=end_4h)
    assert fl4["spot_4h_complete"] is True and abs(fl4["spot_tb_4h_flow"] - 0.32) < 1e-9 and abs(fl4["spot_vrat_4h"] - 1.5) < 1e-9
    smap4.pop(end_4h)
    fl4_inc = agy._spot_15m_to_4h_flow(smap4, tgt_4h, end_ts_str=end_4h)
    assert fl4_inc["spot_4h_complete"] is False and fl4_inc["spot_tb_4h_flow"] is None

    # 4. V10_AA_SQZ96 tightened HALF55 (-1.0%, 1.00U heat) vs V10_NEW7_SQZ96 (-2.0%) vs legacy V9_AA_SQZ96 (-1.5%)
    opened = dt.datetime(2026, 11, 24, 4, 0, 0, tzinfo=dt.timezone.utc)
    t0 = int(opened.timestamp() * 1000)
    bms = 900_000
    ev_v10_aa = agy.evaluate_position_exit(
        {"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "96.0", [(t0, Decimal("100.5"), Decimal("94.4"), Decimal("96.0"))],
        opened.isoformat(), opened + dt.timedelta(minutes=20),
    )
    assert ev_v10_aa["ratchet_stage"] == "HALF55" and Decimal(ev_v10_aa["stop_px"]) == Decimal("101.0")
    h_v10, _ = agy.compute_open_heat_and_counts(
        {"shorts": [{"id": "p10", "symbol": "AAAUSDT", "qty": "1", "entry": "100", "on_binance": True}]},
        {"p10": ev_v10_aa["updated_meta"]},
    )
    assert h_v10 == Decimal("1.00")
    ev_v10_hit = agy.evaluate_position_exit(
        ev_v10_aa["updated_meta"], "100", "101.1", [(t0 + bms, Decimal("101.1"), Decimal("96.0"), Decimal("101.05"))],
        opened.isoformat(), opened + dt.timedelta(minutes=35),
    )
    assert ev_v10_hit["action"] == "MUST_CLOSE" and "HALF_STOP_15M" in ev_v10_hit["exit_reason"]
    # V10_NEW7_SQZ96 keeps HALF65 at -2.0% (102.0) and legacy V9_AA_SQZ96 stays at -1.5% (101.5)
    ev_v10_n7 = agy.evaluate_position_exit(
        {"exit_mode": "V10_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60"},
        "100", "95.0", [(t0, Decimal("100.5"), Decimal("93.4"), Decimal("95.0"))],
        opened.isoformat(), opened + dt.timedelta(minutes=20),
    )
    assert ev_v10_n7["ratchet_stage"] == "HALF65" and Decimal(ev_v10_n7["stop_px"]) == Decimal("102.0")
    ev_v9_aa = agy.evaluate_position_exit(
        {"exit_mode": "V9_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "96.0", [(t0, Decimal("100.5"), Decimal("94.4"), Decimal("96.0"))],
        opened.isoformat(), opened + dt.timedelta(minutes=20),
    )
    assert ev_v9_aa["ratchet_stage"] == "HALF55" and Decimal(ev_v9_aa["stop_px"]) == Decimal("101.5")

    # 5. V10 sharpened BTC Squeeze (+2.0%/1h | +3.5%/4h) vs legacy V9 (+2.2%/1h | +4.0%/4h) + Fix #1 rolling live mark
    btc_bars = [(t0 - k * bms, Decimal("60000"), Decimal("59900"), Decimal("60000")) for k in range(16, 0, -1)]
    btc_bars.append((t0, Decimal("60200"), Decimal("59900"), Decimal("60100")))
    btc_bars.append((t0 + bms, Decimal("61300"), Decimal("60100"), Decimal("61230")))  # +2.05% vs 1h ago
    alt_loss = [
        (t0, Decimal("101.0"), Decimal("99.5"), Decimal("100.5")),
        (t0 + bms, Decimal("102.5"), Decimal("100.5"), Decimal("102.0")),
    ]
    ev_sqz10 = agy.evaluate_position_exit(
        {"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_bars,
    )
    ev_sqz9 = agy.evaluate_position_exit(
        {"exit_mode": "V9_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_bars,
    )
    assert ev_sqz10["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in ev_sqz10["exit_reason"]
    assert ev_sqz9["action"] == "HOLD"

    # Live rolling BTC mark check (Fix #1) + check_fast_guard_events mode differentiation at +2.05%
    now_fg = opened + dt.timedelta(minutes=35)
    t_anchor = int((now_fg - dt.timedelta(minutes=75)).timestamp() * 1000)  # closes ~60m ago
    btc_ref_v10 = {
        "at": (now_fg - dt.timedelta(minutes=1)).isoformat(),
        "ref_1h_close": "60000", "ref_4h_close": "60000",
        "closes_15m": {str(t_anchor): "60000"},
    }
    pf_mix = {"shorts": [
        {"id": "p10", "symbol": "AAAUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "p9", "symbol": "BBBUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
    ]}
    meta_mix = {
        "p10": {"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60", "stop_px": "105.0", "tp_px": "40.0", "ratchet_stage": "INIT"},
        "p9": {"exit_mode": "V9_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60", "stop_px": "105.0", "tp_px": "40.0", "ratchet_stage": "INIT"},
    }
    br_mix, _ = agy.check_fast_guard_events(
        meta_mix, pf_mix, {"AAAUSDT": Decimal("101.5"), "BBBUSDT": Decimal("101.5"), "BTCUSDT": Decimal("61230")},
        now_fg, btc_ref=btc_ref_v10,
    )
    assert [b["id"] for b in br_mix] == ["p10"] and "FAST_BTC_SQUEEZE_CUT" in br_mix[0]["reason"]

    # Fix #1: evaluate_position_exit live mark check triggers V10 at +2.05% via rolling map
    btc_bars_live = [(t0 - k * bms, Decimal("60000"), Decimal("59900"), Decimal("60000")) for k in range(16, 0, -1)]
    btc_bars_live.append((t0, Decimal("60100"), Decimal("59900"), Decimal("60000")))
    ev_live10 = agy.evaluate_position_exit(
        {"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "101.5", [(t0, Decimal("101.5"), Decimal("99.5"), Decimal("101.0"))],
        opened.isoformat(), opened + dt.timedelta(minutes=25),
        btc_bars_15m=btc_bars_live, btc_mark_px="61230",
    )
    assert ev_live10["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_MARK" in ev_live10["exit_reason"]

    # 6. Fix #2: _patch_shortlist_15m_from_binance patches live Binance Spot coins even when ClickHouse spot_15m is empty
    orig_fetch = agy._fetch_rest_klines
    try:
        t_tgt = int(dt.datetime(2026, 11, 24, 3, 45, tzinfo=dt.timezone.utc).timestamp() * 1000)
        agy._fetch_rest_klines = lambda url, sym, interval, limit=20, timeout=10: (
            sym,
            [[t_tgt, "2.0", "2.1", "1.9", "1.95", "100", t_tgt + 899999, "10000.0", 50, "35", "3500.0", "0"]],
        )
        m15_d = {"LITUSDT": (["2026-11-24 03:45:00"], [1.96], [10000.0], [2.1], [1.9])}
        spot_d = {}  # ClickHouse has 0 rows for LITUSDT!
        n_sp = agy._patch_shortlist_15m_from_binance(
            m15_d, spot_d, ["LITUSDT"], "2026-11-24 03:45:00",
            dt.datetime(2026, 11, 24, 4, 0, 10, tzinfo=dt.timezone.utc),
            spot_live_syms={"LITUSDT"},
        )
        assert n_sp == 1 and "LITUSDT" in spot_d and spot_d["LITUSDT"]["2026-11-24 03:45:00"] == (1.95, 10000.0, 3500.0, 50.0, 2.0, 2.1, 1.9)
    finally:
        agy._fetch_rest_klines = orig_fetch


def test_v11_bearbrk_post30d_b42_s18_h50_and_fixes():
    assert "BEAR_BRK_1H" not in agy.ENGINE_MAX_OPEN
    assert "POST30D_4H" not in agy.ENGINE_MAX_OPEN
    assert agy.ENGINE_HEAT_CAP == {}

    # 1. BASCRUSH_4H V11 Bull-Transition guard (`BD <= 0.42` passes, `0.43` blocked; NEW7 still allowed at `0.45`)
    bc_v11 = dict(
        age_days=60.0, breadth_1d=0.42, btc_ret7d=0.01, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.97, hi_1h=0.98, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.98,
        qv_1h=1_000_000.0, qv_avg20_1h=900_000.0, tb_1h=0.46, ema10_1h=0.97, ema10_1h_prev=0.975,
        atr14_1h_pct=0.016, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True,
        bas8h=0.0010, bas12h_max=0.0028, bas_4h_close=0.0008,
        is_fresh_4h_window=True, bar_complete_4h=True,
        o_4h=1.00, hi_4h=1.01, lo_4h=0.94, c_4h=0.95, c_4h_prev=1.01, tb_4h=0.41, ema10_4h=0.98, ema10_4h_prev=0.99,
    )
    hits_b42 = agy.evaluate_v11_short_setups(**bc_v11)
    assert [h["setup"] for h in hits_b42] == ["BASCRUSH_4H"]
    assert hits_b42[0]["exit_mode"] == "V18_AA_SQZ96"
    assert agy.evaluate_v11_short_setups(**{**bc_v11, "breadth_1d": 0.43}) == []
    hits_n7_45 = agy.evaluate_v11_short_setups(**{**bc_v11, "age_days": 3.0, "breadth_1d": 0.45, "tb_1h": 0.42})
    assert [h["setup"] for h in hits_n7_45] == ["NEW7_BAS_1H"] and hits_n7_45[0]["exit_mode"] == "V21_NEW7_SQZ96"

    # 2. BEAR_BRK_1H deep-bear support breakdown (NO fresh_e10 required: c_1h_prev already < ema10_1h_prev!)
    bb_base = dict(
        age_days=150.0, breadth_1d=0.25, btc_ret7d=-0.04, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.945, o_1h=0.975, hi_1h=0.980, lo_1h=0.940, c_1h=0.945, c_1h_prev=0.960,
        qv_1h=1_800_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.34,
        ema10_1h=0.968, ema10_1h_prev=0.972, ema20_1h=0.985, lo16_1h_prev=0.955,
        atr14_1h_pct=0.018, hi24=1.02, hi7d=1.12, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.06, ret_7d=-0.12,
        d_sma20=1.08, f_day=0.0001, has_spot_24h=True, bas8h=0.0002,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_bb = agy.evaluate_v11_short_setups(**bb_base)
    assert [h["setup"] for h in hits_bb] == ["BEAR_BRK_1H"]
    assert hits_bb[0]["stop_pct"] == 0.050 and hits_bb[0]["tp_pct"] == 0.600 and hits_bb[0]["exit_mode"] == "V18_AA_SQZ96"
    assert 244.0 < hits_bb[0]["score"] < 300.0
    # Strict guards on BEAR_BRK_1H
    assert agy.evaluate_v11_short_setups(**{**bb_base, "breadth_1d": 0.36}) == []
    assert agy.evaluate_v11_short_setups(**{**bb_base, "bas8h": -0.0001}) == []
    assert agy.evaluate_v11_short_setups(**{**bb_base, "bas8h": None}) == []
    assert agy.evaluate_v11_short_setups(**{**bb_base, "ema10_1h": 0.990}) == []        # ema10 >= ema20
    assert agy.evaluate_v11_short_setups(**{**bb_base, "c_1h_prev": 0.950}) == []       # not fresh break (< lo16 already)
    assert agy.evaluate_v11_short_setups(**{**bb_base, "ema10_1h": 0.984, "ema20_1h": 0.995}) == []  # ext_e10 = -3.96% < -3.5%
    assert agy.evaluate_v11_short_setups(**{**bb_base, "lo_1h": 0.930}) == []           # lwick = 0.015/0.05 = 0.30 >= 0.25
    assert agy.evaluate_v11_short_setups(**{**bb_base, "tb_1h": 0.38}) == []
    assert agy.evaluate_v11_short_setups(**{**bb_base, "qv_1h": 1_550_000.0}) == []     # vrat = 1.55 < 1.60

    # 3. POST30D_4H Phase-2 distribution (7 < age <= 30d, works even perp-only has_spot_24h=False, SL 5.5%)
    p30_base = dict(
        age_days=14.0, breadth_1d=0.40, btc_ret7d=-0.01, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.97, hi_1h=0.98, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.97,
        qv_1h=800_000.0, qv_avg20_1h=700_000.0, tb_1h=0.46, ema10_1h=0.97, ema10_1h_prev=0.975,
        atr14_1h_pct=0.020, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=False, bas8h=None,
        is_fresh_4h_window=True, bar_complete_4h=True,
        o_4h=1.00, hi_4h=1.01, lo_4h=0.94, c_4h=0.95, c_4h_prev=1.01,
        qv_4h=3_000_000.0, qv_avg20_4h=2_000_000.0, tb_4h=0.40, ema10_4h=0.98, ema10_4h_prev=0.99,
    )
    hits_p30 = agy.evaluate_v11_short_setups(**p30_base)
    assert [h["setup"] for h in hits_p30] == ["POST30D_4H"]
    assert hits_p30[0]["stop_pct"] == 0.055 and hits_p30[0]["tp_pct"] == 0.600 and hits_p30[0]["exit_mode"] == "V18_AA_SQZ96"
    assert 252.0 < hits_p30[0]["score"] < 300.0
    # Strict guards on POST30D_4H
    assert agy.evaluate_v11_short_setups(**{**p30_base, "age_days": 7.0, "tb_1h": 0.49}) == []   # <= 7d belongs to NEW7
    assert agy.evaluate_v11_short_setups(**{**p30_base, "age_days": 30.1}) == []
    assert agy.evaluate_v11_short_setups(**{**p30_base, "breadth_1d": 0.46}) == []
    assert agy.evaluate_v11_short_setups(**{**p30_base, "lo_4h": 0.91}) == []           # lwick = 0.04/0.10 = 0.40 >= 0.30
    assert agy.evaluate_v11_short_setups(**{**p30_base, "tb_4h": 0.44}) == []
    assert agy.evaluate_v11_short_setups(**{**p30_base, "qv_4h": 2_380_000.0}) == []    # vrat_4h = 1.19 < 1.20

    # 4. V11_AA_SQZ96 earlier HALF50 (-1.0% @ +5.0% MFE, 1.00U heat) vs V10_AA_SQZ96 (stays INIT @ +5.2%) vs V11_NEW7_SQZ96 (HALF65)
    opened = dt.datetime(2026, 11, 24, 4, 0, 0, tzinfo=dt.timezone.utc)
    t0 = int(opened.timestamp() * 1000)
    bms = 900_000
    bar_mfe52 = [(t0, Decimal("100.5"), Decimal("94.8"), Decimal("96.0"))]  # MFE = +5.2%
    ev_v11_aa = agy.evaluate_position_exit(
        {"exit_mode": "V11_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "96.0", bar_mfe52, opened.isoformat(), opened + dt.timedelta(minutes=20),
    )
    ev_v10_52 = agy.evaluate_position_exit(
        {"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "96.0", bar_mfe52, opened.isoformat(), opened + dt.timedelta(minutes=20),
    )
    assert ev_v11_aa["ratchet_stage"] == "HALF50" and Decimal(ev_v11_aa["stop_px"]) == Decimal("101.0")
    assert ev_v10_52["ratchet_stage"] == "INIT" and Decimal(ev_v10_52["stop_px"]) == Decimal("105.0")
    h_v11, _ = agy.compute_open_heat_and_counts(
        {"shorts": [{"id": "p11", "symbol": "AAAUSDT", "qty": "1", "entry": "100", "on_binance": True}]},
        {"p11": ev_v11_aa["updated_meta"]},
    )
    assert h_v11 == Decimal("1.00")
    ev_v11_hit = agy.evaluate_position_exit(
        ev_v11_aa["updated_meta"], "100", "101.2",
        [(t0 + bms, Decimal("101.2"), Decimal("96.0"), Decimal("101.1"))],
        opened.isoformat(), opened + dt.timedelta(minutes=35),
    )
    assert ev_v11_hit["action"] == "MUST_CLOSE" and "HALF_STOP_15M" in ev_v11_hit["exit_reason"]
    # V11_NEW7_SQZ96 keeps HALF65 (-2.0% @ +6.5%)
    ev_v11_n7 = agy.evaluate_position_exit(
        {"exit_mode": "V11_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60"},
        "100", "95.0", [(t0, Decimal("100.5"), Decimal("93.4"), Decimal("95.0"))],
        opened.isoformat(), opened + dt.timedelta(minutes=20),
    )
    assert ev_v11_n7["ratchet_stage"] == "HALF65" and Decimal(ev_v11_n7["stop_px"]) == Decimal("102.0")

    # 5. Asymmetric 3-tier BTC Squeeze: V11_AA (+1.8%/3.2%), V10 (+2.0%/3.5%), V11_NEW7 & V9 (+2.2%/4.0%)
    def _btc_bars_jump(cl_last):
        bs = [(t0 - k * bms, Decimal("60000"), Decimal("59900"), Decimal("60000")) for k in range(16, 0, -1)]
        bs.append((t0, Decimal("60200"), Decimal("59900"), Decimal("60100")))
        bs.append((t0 + bms, cl_last + Decimal("50"), Decimal("60100"), cl_last))
        return bs

    alt_loss = [
        (t0, Decimal("101.0"), Decimal("99.5"), Decimal("100.5")),
        (t0 + bms, Decimal("102.5"), Decimal("100.5"), Decimal("102.0")),
    ]
    # At +1.85% (61110): ONLY V11_AA cuts; V10_AA and V11_NEW7 HOLD
    btc_185 = _btc_bars_jump(Decimal("61110"))
    assert agy.evaluate_position_exit({"exit_mode": "V11_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60"}, "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_185)["action"] == "MUST_CLOSE"
    assert agy.evaluate_position_exit({"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60"}, "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_185)["action"] == "HOLD"
    assert agy.evaluate_position_exit({"exit_mode": "V11_NEW7_SQZ96", "stop_pct": "0.06", "tp_pct": "0.60"}, "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_185)["action"] == "HOLD"
    # At +2.05% (61230): V11_AA & V10_AA cut; V11_NEW7 still HOLDS (2.2% threshold)!
    btc_205 = _btc_bars_jump(Decimal("61230"))
    assert agy.evaluate_position_exit({"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60"}, "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_205)["action"] == "MUST_CLOSE"
    assert agy.evaluate_position_exit({"exit_mode": "V11_NEW7_SQZ96", "stop_pct": "0.06", "tp_pct": "0.60"}, "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_205)["action"] == "HOLD"
    # At +2.25% (61350): V11_NEW7 also cuts
    btc_225 = _btc_bars_jump(Decimal("61350"))
    assert agy.evaluate_position_exit({"exit_mode": "V11_NEW7_SQZ96", "stop_pct": "0.06", "tp_pct": "0.60"}, "100", "102.0", alt_loss, opened.isoformat(), opened + dt.timedelta(hours=1), btc_bars_15m=btc_225)["action"] == "MUST_CLOSE"

    # Fast-guard 3-tier BTC squeeze differentiation
    now_fg = opened + dt.timedelta(minutes=35)
    t_anc = int((now_fg - dt.timedelta(minutes=75)).timestamp() * 1000)
    btc_ref_3 = {"at": (now_fg - dt.timedelta(minutes=1)).isoformat(), "ref_1h_close": "60000", "ref_4h_close": "60000", "closes_15m": {str(t_anc): "60000"}}
    pf_3 = {"shorts": [
        {"id": "p11a", "symbol": "AAAUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "p10a", "symbol": "BBBUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "p11n", "symbol": "CCCUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
    ]}
    meta_3 = {
        "p11a": {"exit_mode": "V11_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60", "stop_px": "105.0", "tp_px": "40.0", "ratchet_stage": "INIT"},
        "p10a": {"exit_mode": "V10_AA_SQZ96", "stop_pct": "0.05", "tp_pct": "0.60", "stop_px": "105.0", "tp_px": "40.0", "ratchet_stage": "INIT"},
        "p11n": {"exit_mode": "V11_NEW7_SQZ96", "stop_pct": "0.06", "tp_pct": "0.60", "stop_px": "106.0", "tp_px": "40.0", "ratchet_stage": "INIT"},
    }
    mk_loss = {"AAAUSDT": Decimal("101.5"), "BBBUSDT": Decimal("101.5"), "CCCUSDT": Decimal("101.5")}
    br_185, _ = agy.check_fast_guard_events(meta_3, pf_3, {**mk_loss, "BTCUSDT": Decimal("61110")}, now_fg, btc_ref=btc_ref_3)
    assert [b["id"] for b in br_185] == ["p11a"]
    br_205, _ = agy.check_fast_guard_events(meta_3, pf_3, {**mk_loss, "BTCUSDT": Decimal("61230")}, now_fg, btc_ref=btc_ref_3)
    assert sorted(b["id"] for b in br_205) == ["p10a", "p11a"]
    br_225, _ = agy.check_fast_guard_events(meta_3, pf_3, {**mk_loss, "BTCUSDT": Decimal("61350")}, now_fg, btc_ref=btc_ref_3)
    assert sorted(b["id"] for b in br_225) == ["p10a", "p11a", "p11n"]

    # 6. V11 Fix #2: _fetch_spot_flow_rest_fallback returns exact 21-bar spot_tb & spot_vrat for 4h and 1h (skips open bar)
    orig_fetch = agy._fetch_rest_klines
    try:
        t4_base = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)
        rows_4h = []
        for i in range(22):  # 21 closed bars + 1 still-open bar
            ts_ms = int((t4_base + dt.timedelta(hours=4 * i)).timestamp() * 1000)
            qv = 200_000.0 if i < 20 else (300_000.0 if i == 20 else 50_000.0)
            tb_qv = qv * (0.50 if i < 20 else 0.34)
            rows_4h.append([ts_ms, "10", "11", "9", "9.5", "100", ts_ms + 14399999, str(qv), 50, "40", str(tb_qv), "0"])
        tgt_4h = (t4_base + dt.timedelta(hours=80)).strftime("%Y-%m-%d %H:00:00")
        now_4h = t4_base + dt.timedelta(hours=84, seconds=10)
        agy._fetch_rest_klines = lambda url, sym, interval, limit=25, timeout=10: (sym, rows_4h)
        fb4 = agy._fetch_spot_flow_rest_fallback(["NEWSPUSDT"], "4h", tgt_4h, now_4h)
        assert fb4["NEWSPUSDT"]["spot_4h_complete"] is True
        assert abs(fb4["NEWSPUSDT"]["spot_tb_4h_flow"] - 0.34) < 1e-9
        assert abs(fb4["NEWSPUSDT"]["spot_vrat_4h"] - 1.5) < 1e-9

        t1_base = dt.datetime(2026, 11, 23, 0, 0, tzinfo=dt.timezone.utc)
        rows_1h = []
        for i in range(22):
            ts_ms = int((t1_base + dt.timedelta(hours=i)).timestamp() * 1000)
            qv = 100_000.0 if i < 20 else (180_000.0 if i == 20 else 20_000.0)
            tb_qv = qv * (0.50 if i < 20 else 0.31)
            rows_1h.append([ts_ms, "10", "11", "9", "9.5", "100", ts_ms + 3599999, str(qv), 50, "40", str(tb_qv), "0"])
        tgt_1h = (t1_base + dt.timedelta(hours=20)).strftime("%Y-%m-%d %H:00:00")
        now_1h = t1_base + dt.timedelta(hours=21, seconds=10)
        agy._fetch_rest_klines = lambda url, sym, interval, limit=25, timeout=10: (sym, rows_1h)
        fb1 = agy._fetch_spot_flow_rest_fallback(["NEWSPUSDT"], "1h", tgt_1h, now_1h)
        assert fb1["NEWSPUSDT"]["spot_1h_complete"] is True
        assert abs(fb1["NEWSPUSDT"]["spot_tb_1h"] - 0.31) < 1e-9
        assert abs(fb1["NEWSPUSDT"]["spot_vrat_1h"] - 1.8) < 1e-9
    finally:
        agy._fetch_rest_klines = orig_fetch

    # 7. V11 Fix #3: first_bar_1m_failed=True skips contaminated opening 15m bar when filled mid-bar (e.g. 04:01:00)
    mid_fill = dt.datetime(2026, 11, 24, 4, 1, 0, tzinfo=dt.timezone.utc)  # within 3m grace!
    bars_contam = [
        (t0, Decimal("108.0"), Decimal("99.0"), Decimal("99.5")),        # pre-entry wick spike at 04:00:10!
        (t0 + bms, Decimal("100.5"), Decimal("94.5"), Decimal("95.5")),  # post-entry clean bar -> MFE +5.5% -> HALF50
    ]
    ev_fail_1m = agy.evaluate_position_exit(
        {"exit_mode": "V11_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "95.5", bars_contam, mid_fill.isoformat(), mid_fill + dt.timedelta(minutes=35),
        first_bar_1m_failed=True,
    )
    assert ev_fail_1m["action"] == "HOLD" and ev_fail_1m["ratchet_stage"] == "HALF50"
    assert Decimal(ev_fail_1m["stop_px"]) == Decimal("101.0")


def test_v12_basovh_whalesp_b1h85_and_fixes():
    import os
    import tempfile

    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.BASCRUSH_BTC_RET4H_MIN == -0.040
    assert agy.DEFAULT_EXIT_MODE == "V18_AA_SQZ96"
    assert agy.NEW7_EXIT_MODE == "V21_NEW7_SQZ96"
    assert agy.V12_AA_SQZ_B1H == Decimal("0.85")
    assert agy.MAX_OPEN_HEAT_USDT == Decimal("0")
    assert agy.ENGINE_MAX_OPEN == {}
    assert agy.ENGINE_HEAT_CAP == {}

    # 1. BASOVH_1H: 24/7 Hourly All-Age Crowded-Long Perp Basis Overheat Unwind (SL 5.5%, TP 60%, V12_AA_SQZ96)
    bo_base = dict(
        age_days=120.0, breadth_1d=0.44, btc_ret7d=0.01, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.99, hi_1h=1.00, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.985,
        qv_1h=1_200_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.39, ema10_1h=0.975, ema10_1h_prev=0.980,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0014, ps_vrat=1.30,
        spot_1h_complete=True, spot_tb_1h=0.38, spot_vrat_1h=1.00, spot_tsize_r_1h=1.00,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_bo = agy.evaluate_v12_short_setups(**bo_base)
    assert [h["setup"] for h in hits_bo] == ["BASOVH_1H"]
    assert hits_bo[0]["stop_pct"] == 0.055 and hits_bo[0]["tp_pct"] == 0.600 and hits_bo[0]["exit_mode"] == "V18_AA_SQZ96"
    assert 270.0 < hits_bo[0]["score"] < 320.0
    # Strict guards on BASOVH_1H
    assert agy.evaluate_v12_short_setups(**{**bo_base, "breadth_1d": 0.46}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "bas8h": 0.0009}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "bas8h": None}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "tb_1h": 0.42}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "spot_tb_1h": 0.42}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "lo_1h": 0.90}) == []  # lwick = 0.05/0.10 = 0.50 >= 0.30
    assert agy.evaluate_v12_short_setups(**{**bo_base, "spot_1h_complete": False}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "has_spot_24h": False}) == []
    assert agy.evaluate_v12_short_setups(**{**bo_base, "f_day": -0.0013}) == []

    # 2. WHALESP_1H: 24/7 Hourly All-Age Institutional Spot Whale Distribution (SL 5.0%, TP 60%, V12_AA_SQZ96)
    wsp_base = dict(
        age_days=120.0, breadth_1d=0.44, btc_ret7d=0.01, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.95, o_1h=0.99, hi_1h=1.00, lo_1h=0.94, c_1h=0.95, c_1h_prev=0.985,
        qv_1h=1_200_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.41, ema10_1h=0.975, ema10_1h_prev=0.980,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0002, ps_vrat=1.30,
        spot_1h_complete=True, spot_tb_1h=0.35, spot_vrat_1h=1.60, spot_tsize_r_1h=1.80,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_wsp = agy.evaluate_v12_short_setups(**wsp_base)
    assert [h["setup"] for h in hits_wsp] == ["WHALESP_1H", "SPOTFLOW_POS_1H"]
    assert hits_wsp[0]["stop_pct"] == 0.050 and hits_wsp[0]["tp_pct"] == 0.600 and hits_wsp[0]["exit_mode"] == "V18_AA_SQZ96"
    assert 290.0 < hits_wsp[0]["score"] < 330.0 and hits_wsp[0]["score"] > hits_wsp[1]["score"]
    # Strict guards on WHALESP_1H
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "breadth_1d": 0.46}) == []
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "bas8h": -0.0001}) == []
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "tb_1h": 0.44}) == []
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "spot_tb_1h": 0.40}) == []
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "spot_vrat_1h": 1.19}) == []
    assert "WHALESP_1H" not in [h["setup"] for h in agy.evaluate_v12_short_setups(**{**wsp_base, "spot_tsize_r_1h": 1.19})]
    assert "WHALESP_1H" not in [h["setup"] for h in agy.evaluate_v12_short_setups(**{**wsp_base, "spot_tsize_r_1h": None})]
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "lo_1h": 0.90}) == []
    assert agy.evaluate_v12_short_setups(**{**wsp_base, "spot_1h_complete": False}) == []

    # 3. Spot trade_count plumbing in _spot_15m_to_1h_flow (4-tuple + 3-tuple compat) & _fetch_spot_flow_rest_fallback
    base_dt = dt.datetime(2026, 11, 23, 0, 0, tzinfo=dt.timezone.utc)
    smap4 = {}
    smap3 = {}
    for h in range(21):
        qv_bar = 25_000.0 if h < 20 else 60_000.0     # prior 1h qv=100k, cur 1h qv=240k -> vrat=2.4x
        tc_bar = 25.0 if h < 20 else 30.0             # prior 1h tc=100 (avg tsize=1000), cur 1h tc=120 (tsize=2000) -> tsize_r=2.0x
        tb_bar = qv_bar * (0.50 if h < 20 else 0.34)
        for q in range(4):
            t_s = (base_dt + dt.timedelta(hours=h, minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S")
            smap4[t_s] = (100.0, qv_bar, tb_bar, tc_bar)
            smap3[t_s] = (100.0, qv_bar, tb_bar)
    fl4 = agy._spot_15m_to_1h_flow(smap4, "2026-11-23 20:00:00", end_ts_str="2026-11-23 20:45:00")
    assert fl4["spot_1h_complete"] is True
    assert abs(fl4["spot_tb_1h"] - 0.34) < 1e-9
    assert abs(fl4["spot_vrat_1h"] - 2.4) < 1e-9
    assert abs(fl4["spot_tc_1h"] - 120.0) < 1e-9
    assert abs(fl4["spot_tsize_r_1h"] - 2.0) < 1e-9
    fl3 = agy._spot_15m_to_1h_flow(smap3, "2026-11-23 20:00:00", end_ts_str="2026-11-23 20:45:00")
    assert fl3["spot_1h_complete"] is True and fl3["spot_tsize_r_1h"] is None

    orig_fetch = agy._fetch_rest_klines
    seen_limits = []
    try:
        rows_1h = []
        for i in range(22):
            ts_ms = int((base_dt + dt.timedelta(hours=i)).timestamp() * 1000)
            qv = 100_000.0 if i < 20 else (240_000.0 if i == 20 else 10_000.0)
            tc = 100 if i < 20 else (120 if i == 20 else 10)
            tb_qv = qv * (0.50 if i < 20 else 0.33)
            rows_1h.append([ts_ms, "10", "11", "9", "9.5", "100", ts_ms + 3599999, str(qv), tc, "40", str(tb_qv), "0"])
        tgt_1h = (base_dt + dt.timedelta(hours=20)).strftime("%Y-%m-%d %H:00:00")
        now_1h = base_dt + dt.timedelta(hours=21, seconds=10)
        agy._fetch_rest_klines = lambda url, sym, interval, limit=25, timeout=10: (sym, rows_1h)
        fb1 = agy._fetch_spot_flow_rest_fallback(["WHALEUSDT"], "1h", tgt_1h, now_1h)
        assert fb1["WHALEUSDT"]["spot_1h_complete"] is True
        assert abs(fb1["WHALEUSDT"]["spot_tc_1h"] - 120.0) < 1e-9
        assert abs(fb1["WHALEUSDT"]["spot_tsize_r_1h"] - 2.0) < 1e-9

        # V12 Fix #1: _patch_shortlist_15m_from_binance requests limit=50 (12.5h) for full bas8h/bas12h coverage
        t_tgt = int(dt.datetime(2026, 11, 24, 3, 45, tzinfo=dt.timezone.utc).timestamp() * 1000)
        def _cap_fetch(url, sym, interval, limit=20, timeout=10):
            seen_limits.append(limit)
            return sym, [[t_tgt, "2.0", "2.1", "1.9", "1.95", "100", t_tgt + 899999, "10000.0", 80, "35", "3500.0", "0"]]
        agy._fetch_rest_klines = _cap_fetch
        sp_new = {}
        agy._patch_shortlist_15m_from_binance(
            {"NEWCUSDT": (["2026-11-24 03:45:00"], [1.96], [10000.0], [2.1], [1.9])},
            sp_new, ["NEWCUSDT"], "2026-11-24 03:45:00",
            dt.datetime(2026, 11, 24, 4, 0, 10, tzinfo=dt.timezone.utc),
            spot_live_syms={"NEWCUSDT"},
        )
        assert seen_limits == [50] and sp_new["NEWCUSDT"]["2026-11-24 03:45:00"] == (1.95, 10000.0, 3500.0, 80.0, 2.0, 2.1, 1.9)
    finally:
        agy._fetch_rest_klines = orig_fetch

    # 4. _eval_live_b1h_squeeze: min-syms, staleness (>90m), BTCUSDT exclusion, exact >= 0.85 threshold
    now_fg = dt.datetime(2026, 11, 24, 6, 30, 0, tzinfo=dt.timezone.utc)
    ema_20 = {f"C{i:02d}USDT": "100.0" for i in range(20)}
    ema_20["BTCUSDT"] = "60000.0"  # must be excluded from alt breadth
    ref_fresh = {"ema20_1h_at": (now_fg - dt.timedelta(minutes=25)).isoformat(), "ema20_1h_map": ema_20}
    ref_stale = {"ema20_1h_at": (now_fg - dt.timedelta(minutes=95)).isoformat(), "ema20_1h_map": ema_20}
    ref_small = {"ema20_1h_at": (now_fg - dt.timedelta(minutes=5)).isoformat(), "ema20_1h_map": {f"C{i:02d}USDT": "100" for i in range(19)}}
    mk_16_20 = {f"C{i:02d}USDT": Decimal("101" if i < 16 else "99") for i in range(20)}
    mk_16_20["BTCUSDT"] = Decimal("61000")
    mk_17_20 = {f"C{i:02d}USDT": Decimal("101" if i < 17 else "99") for i in range(20)}
    mk_17_20["BTCUSDT"] = Decimal("59000")
    assert agy._eval_live_b1h_squeeze(mk_17_20, ref_small, now_fg) == (False, None, 0)
    assert agy._eval_live_b1h_squeeze(mk_17_20, ref_stale, now_fg) == (False, None, 0)
    hit80, val80, cnt80 = agy._eval_live_b1h_squeeze(mk_16_20, ref_fresh, now_fg)
    assert hit80 is False and val80 == Decimal("0.80") and cnt80 == 20
    hit85, val85, cnt85 = agy._eval_live_b1h_squeeze(mk_17_20, ref_fresh, now_fg)
    assert hit85 is True and val85 == Decimal("0.85") and cnt85 == 20

    # 5. Dual-channel B1H Squeeze in check_fast_guard_events: ONLY V12_AA_SQZ96 INIT losers cut (BTC flat!)
    opened = now_fg - dt.timedelta(minutes=45)
    t_anc = int((now_fg - dt.timedelta(minutes=75)).timestamp() * 1000)
    btc_ref_b1h = {
        "at": (now_fg - dt.timedelta(minutes=1)).isoformat(),
        "ref_1h_close": "60000", "ref_4h_close": "60000",
        "closes_15m": {str(t_anc): "60000"},
        "ema20_1h_at": (now_fg - dt.timedelta(minutes=20)).isoformat(),
        "ema20_1h_map": ema_20,
    }
    pf_b1h = {"shorts": [
        {"id": "v12_aa_loss", "symbol": "AAAUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "v12_aa_win", "symbol": "BBBUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "v12_aa_half", "symbol": "CCCUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "v12_n7_loss", "symbol": "DDDUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
        {"id": "v11_aa_loss", "symbol": "EEEUSDT", "entry": "100", "on_binance": True, "opened_at": opened.isoformat()},
    ]}
    meta_b1h = {
        "v12_aa_loss": {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "105.5", "tp_px": "40", "ratchet_stage": "INIT"},
        "v12_aa_win": {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "105.5", "tp_px": "40", "ratchet_stage": "INIT"},
        "v12_aa_half": {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "101.0", "tp_px": "40", "ratchet_stage": "HALF50"},
        "v12_n7_loss": {"exit_mode": "V12_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60", "stop_px": "106.0", "tp_px": "40", "ratchet_stage": "INIT"},
        "v11_aa_loss": {"exit_mode": "V11_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "105.0", "tp_px": "40", "ratchet_stage": "INIT"},
    }
    mk_b1h = {
        **mk_17_20, "BTCUSDT": Decimal("60100"),
        "AAAUSDT": Decimal("101.5"), "BBBUSDT": Decimal("99.2"),
        "CCCUSDT": Decimal("100.5"), "DDDUSDT": Decimal("101.5"), "EEEUSDT": Decimal("101.5"),
    }
    br_b1h, _ = agy.check_fast_guard_events(meta_b1h, pf_b1h, mk_b1h, now_fg, btc_ref=btc_ref_b1h)
    assert [b["id"] for b in br_b1h] == ["v12_aa_loss"] and "FAST_B1H_SQUEEZE_CUT" in br_b1h[0]["reason"]

    # 6. evaluate_position_exit: V12 B1H_SQUEEZE_CUT_MARK + V12 Fix #5 (24h MAX_HOLD not skipped & not overwriting B1H)
    t0 = int(opened.timestamp() * 1000)
    bar_loss = [(t0, Decimal("101.5"), Decimal("99.5"), Decimal("101.2"))]
    ev_b1h_v12 = agy.evaluate_position_exit(
        {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60"},
        "100", "101.5", bar_loss, opened.isoformat(), opened + dt.timedelta(minutes=30),
        live_b1h="0.86",
    )
    assert ev_b1h_v12["action"] == "MUST_CLOSE" and "B1H_SQUEEZE_CUT_MARK" in ev_b1h_v12["exit_reason"]
    # V12_NEW7_SQZ96 and V11_AA_SQZ96 ignore live_b1h
    assert agy.evaluate_position_exit(
        {"exit_mode": "V12_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60"},
        "100", "101.5", bar_loss, opened.isoformat(), opened + dt.timedelta(minutes=30),
        live_b1h="0.92",
    )["action"] == "HOLD"
    assert agy.evaluate_position_exit(
        {"exit_mode": "V11_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60"},
        "100", "101.5", bar_loss, opened.isoformat(), opened + dt.timedelta(minutes=30),
        live_b1h="0.92",
    )["action"] == "HOLD"
    # Fix #5a: INIT loser past 24h with empty bars & no squeeze STILL hits MAX_HOLD_24H
    ev_24h_fix = agy.evaluate_position_exit(
        {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60"},
        "100", "101.0", [], opened.isoformat(), opened + dt.timedelta(hours=25),
        live_b1h="0.45",
    )
    assert ev_24h_fix["action"] == "MUST_CLOSE" and "MAX_HOLD_24H" in ev_24h_fix["exit_reason"]
    # Fix #5b: B1H_SQUEEZE_CUT_MARK past 24h is NOT overwritten by MAX_HOLD_24H
    ev_24h_b1h = agy.evaluate_position_exit(
        {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60"},
        "100", "101.0", [], opened.isoformat(), opened + dt.timedelta(hours=25),
        live_b1h="0.88",
    )
    assert ev_24h_b1h["action"] == "MUST_CLOSE" and "B1H_SQUEEZE_CUT_MARK" in ev_24h_b1h["exit_reason"]

    # 7. _save_btc_ref_merged preserves sibling keys across BTC & ema20_1h_map updates
    orig_ref_path = agy.BTC_REF_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.BTC_REF_PATH = os.path.join(td, "agy-btc-ref.json")
            agy._save_btc_ref_merged({"at": now_fg.isoformat(), "closes_15m": {"1": "60000"}})
            agy._save_btc_ref_merged({"ema20_1h_at": now_fg.isoformat(), "ema20_1h_map": {"ETHUSDT": "3000"}})
            agy._save_btc_ref_merged({"at": (now_fg + dt.timedelta(minutes=1)).isoformat(), "ref_1h_close": "60100"})
            saved = agy.read_json(agy.BTC_REF_PATH, default={})
            assert saved["closes_15m"] == {"1": "60000"}
            assert saved["ema20_1h_map"] == {"ETHUSDT": "3000"}
            assert saved["ref_1h_close"] == "60100"
    finally:
        agy.BTC_REF_PATH = orig_ref_path


def test_v12_1_production_hardening():
    import contextlib
    import io
    import json
    import tempfile
    import threading
    # 1. Dedicated lock on _save_btc_ref_merged: concurrent threads never lose sibling keys
    orig_ref = agy.BTC_REF_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.BTC_REF_PATH = os.path.join(td, "agy-btc-ref.json")
            errs = []

            def _w(k, v):
                try:
                    for i in range(10):
                        agy._save_btc_ref_merged({k: f"{v}_{i}"})
                except Exception as e:
                    errs.append(e)

            ts_th = [threading.Thread(target=_w, args=(f"k{j}", f"v{j}")) for j in range(4)]
            for th in ts_th:
                th.start()
            for th in ts_th:
                th.join()
            assert not errs
            saved = agy.read_json(agy.BTC_REF_PATH, default={})
            assert all(saved.get(f"k{j}") == f"v{j}_9" for j in range(4))
            assert os.path.exists(f"{agy.BTC_REF_PATH}.lock")
    finally:
        agy.BTC_REF_PATH = orig_ref

    # 2. evaluate_position_exit hard horizon cap (96h): late bar starting at/after 96h closes as MAX_HOLD_96H, NOT SL
    opened = dt.datetime(2026, 11, 20, 0, 0, 0, tzinfo=dt.timezone.utc)
    t_open = int(opened.timestamp() * 1000)
    t_96h = t_open + 96 * 3_600_000
    ev_cap = agy.evaluate_position_exit(
        {"exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60", "runner_unlocked": True, "ratchet_stage": "RUNNER96", "stop_px": "97.5"},
        "100", "95.0",
        [(t_96h, Decimal("120.0"), Decimal("94.0"), Decimal("95.0"))],
        opened.isoformat(),
        opened + dt.timedelta(hours=97),
    )
    assert ev_cap["action"] == "MUST_CLOSE" and "MAX_HOLD_96H" in ev_cap["exit_reason"], f"got {ev_cap}"

    # 3. _evaluate_open_positions preserves pos_meta for unclear/pending shorts (on_binance=False)
    orig_pm = agy.POS_META_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.POS_META_PATH = os.path.join(td, "agy-positions.json")
            agy.write_json_atomic(agy.POS_META_PATH, {
                "bto-unclear": {"symbol": "UNCUSDT", "exit_mode": "V12_AA_SQZ96", "setup": "BASOVH_1H"},
                "bto-gone": {"symbol": "OLDUSDT", "exit_mode": "V12_AA_SQZ96", "setup": "BASOVH_1H"},
            })
            pf_unc = {"shorts": [{"id": "bto-unclear", "symbol": "UNCUSDT", "on_binance": False, "unclear": True}]}
            agy._evaluate_open_positions(pf_unc, dt.datetime(2026, 11, 24, 10, 0, 0, tzinfo=dt.timezone.utc))
            kept = agy.read_json(agy.POS_META_PATH, default={})
            assert "bto-unclear" in kept and kept["bto-unclear"]["exit_mode"] == "V12_AA_SQZ96"
            assert "bto-gone" not in kept
    finally:
        agy.POS_META_PATH = orig_pm

    # 4. _spot_15m_to_1h_flow & _spot_15m_to_4h_flow reject non-contiguous 21-bar history (forcing REST fallback)
    t_tgt = dt.datetime(2026, 11, 24, 9, 0, 0, tzinfo=dt.timezone.utc)
    sp_gap_1h = {}
    for h_back in range(22):
        if h_back == 5:
            continue  # missing 1 entire hour -> non-contiguous!
        ht = t_tgt - dt.timedelta(hours=h_back)
        for m in (0, 15, 30, 45):
            sp_gap_1h[(ht + dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")] = (10.0, 1000.0, 350.0, 50.0)
    r_gap = agy._spot_15m_to_1h_flow(sp_gap_1h, t_tgt.strftime("%Y-%m-%d %H:%M:%S"))
    assert r_gap["spot_1h_complete"] is True and r_gap["spot_vrat_1h"] is None and r_gap["spot_tsize_r_1h"] is None

    # 5. compute_spot_basis_stats reports spot_24h_bars accurately
    t0_b = dt.datetime(2026, 11, 24, 0, 0, 0, tzinfo=dt.timezone.utc)
    mt = [(t0_b + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(40)]
    mcl = [10.0] * 40
    sp_part = {mt[i]: (10.0, 50000.0, 20000.0, 100.0) for i in range(20, 40)}
    bs = agy.compute_spot_basis_stats(mt, mcl, sp_part)
    assert bs["spot_24h_bars"] == 20

    # 6. opened_this_hour_utc counts opens in the current UTC hour only
    now_hr = dt.datetime(2026, 11, 24, 10, 15, 0, tzinfo=dt.timezone.utc)
    pf_hr = {
        "orders": [
            {"kind": "open", "how": "live", "symbol": "AAAUSDT", "at": "2026-11-24T10:00:12+00:00"},
            {"kind": "open", "how": "rejected", "symbol": "REJUSDT", "at": "2026-11-24T10:00:14+00:00"},
            {"kind": "open", "how": "live", "symbol": "OLDUSDT", "at": "2026-11-24T09:00:12+00:00"},
        ],
        "shorts": [{"symbol": "BBBUSDT", "opened_at": "2026-11-24T10:01:00+00:00"}],
    }
    assert agy.opened_this_hour_utc(pf_hr, now_hr) == {"AAAUSDT", "BBBUSDT"}

    # 7. cmd_auto_trade: (a) LOCK_PATH is RELEASED during market scan, (b) flattened sim_v31 priority beats secondary fallback,
    #    and (c) persistent hourly cap blocks reopening in the same UTC hour
    orig_lock = agy.LOCK_PATH
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_pf = agy.binance.PORTFOLIO
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_now = agy.utcnow
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "agy-guard.lock")
            agy.POS_META_PATH = os.path.join(td, "agy-positions.json")
            agy.STATE_PATH = os.path.join(td, "agy-state.json")
            agy.binance.PORTFOLIO = os.path.join(td, "portfolio.json")
            agy.write_json_atomic(agy.POS_META_PATH, {
                f"existing-n{i}": {"symbol": f"EX{i}USDT", "setup": "NEW7_BAS_1H", "stop_pct": "0.060", "stop_px": "100", "entry": "100"}
                for i in range(4)
            })
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900", "peak_equity_usdt": "900", "circuit_breaker_tripped": False})
            pf_mock = {
                "equity_usdt": "900.00", "available_usdt": "500.00", "held_symbols": [f"EX{i}USDT" for i in range(4)],
                "shorts": [{"id": f"existing-n{i}", "symbol": f"EX{i}USDT", "entry": "100", "on_binance": True} for i in range(4)],
                "orders": [], "closed": [],
            }
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf_mock)
            lock_free_during_scan = []

            def _mock_sync(now_u, do_sync=True):
                return pf_mock, {"status": "ok"}, {"baseline_equity_usdt": "900", "peak_equity_usdt": "900"}, {"tripped": False}

            def _mock_eval(pf, now_u, bars_out=None, flow_out=None):
                return []

            def _mock_scan(pf, now_u, held_bars_15m=None):
                with agy.file_lock(agy.LOCK_PATH, blocking=False) as got_inner:
                    lock_free_during_scan.append(got_inner)
                cands = [
                    {
                        "symbol": "AAAUSDT", "primary_setup": "NEW7_BAS_1H", "score": 280.0,
                        "suggested_stop_pct": 0.060, "suggested_tp_pct": 0.60, "exit_mode": "V14_NEW7_SQZ96",
                        "ret_24h_pct": -1.0, "ret_7d_pct": -5.0, "tb_1h": 0.35, "spot_tb_1h": 0.34,
                        "hit_specs": [
                            {"setup": "NEW7_BAS_1H", "score": 280.0, "stop_pct": 0.060, "tp_pct": 0.60, "exit_mode": "V14_NEW7_SQZ96"},
                            {"setup": "SPOTDMP_1H", "score": 242.0, "stop_pct": 0.050, "tp_pct": 0.60, "exit_mode": "V14_AA_SQZ96"},
                        ],
                    },
                    {
                        "symbol": "BBBUSDT", "primary_setup": "WHALESP_1H", "score": 270.0,
                        "suggested_stop_pct": 0.050, "suggested_tp_pct": 0.60, "exit_mode": "V14_AA_SQZ96",
                        "ret_24h_pct": -1.0, "ret_7d_pct": -5.0, "tb_1h": 0.35, "spot_tb_1h": 0.34,
                        "hit_specs": [
                            {"setup": "WHALESP_1H", "score": 270.0, "stop_pct": 0.050, "tp_pct": 0.60, "exit_mode": "V14_AA_SQZ96"},
                        ],
                    },
                ]
                reg = {"regime_label": "BEARISH_FAVORABLE", "funding_ok": True, "complete_1h_pct": 100.0, "btc": {"price": 60000}, "breadth": {"pct_above_1d_sma20": 20.0, "pct_above_1h_ema20": 20.0}}
                return reg, cands, [], [], {}

            agy._sync_and_portfolio = _mock_sync
            agy._evaluate_open_positions = _mock_eval
            agy._scan_market_and_candidates = _mock_scan
            agy.utcnow = lambda: now_hr
            orig_chw12 = agy.wait_for_clickhouse_15m
            agy.wait_for_clickhouse_15m = lambda *a, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0}
            orig_eng_cap = agy.ENGINE_MAX_OPEN
            agy.ENGINE_MAX_OPEN = {"NEW7_BAS_1H": 4}
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = agy.cmd_auto_trade(max_opens=1, dry_run=True)
                assert rc == 0 and lock_free_during_scan == [True], f"lock_free={lock_free_during_scan}"
                out_j = json.loads(buf.getvalue())
                # AAAUSDT NEW7_BAS_1H (280) is blocked by engine_cap_4; BBBUSDT WHALESP_1H (270) MUST beat AAAUSDT SPOTDMP_1H (242)!
                assert len(out_j["opened"]) == 1 and out_j["opened"][0]["symbol"] == "BBBUSDT" and out_j["opened"][0]["setup"] == "WHALESP_1H", f"got {out_j['opened']}"

                # Persistent hourly cap: if 1 order was already opened this hour and max_opens=1, opens=0; max_opens=0 -> unlimited!
                pf_mock["orders"] = [{"kind": "open", "how": "live", "symbol": "PREVUSDT", "at": "2026-11-24T10:00:10+00:00"}]
                agy.write_json_atomic(agy.binance.PORTFOLIO, pf_mock)
                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc2 = agy.cmd_auto_trade(max_opens=1, dry_run=True)
                assert rc2 == 0 and json.loads(buf2.getvalue())["opened"] == []
                buf3 = io.StringIO()
                with contextlib.redirect_stdout(buf3):
                    rc3 = agy.cmd_auto_trade(max_opens=0, dry_run=True)
                assert rc3 == 0 and len(json.loads(buf3.getvalue())["opened"]) == 2
            finally:
                agy.ENGINE_MAX_OPEN = orig_eng_cap
                agy.wait_for_clickhouse_15m = orig_chw12
    finally:
        agy.LOCK_PATH = orig_lock
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.binance.PORTFOLIO = orig_pf
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.utcnow = orig_now

    # 8. CLI open --exit-mode accepts V12_AA_SQZ96 & V12_NEW7_SQZ96 (verified via argparse without sending order)
    orig_exec_open = agy._execute_open
    try:
        seen_open = []
        agy._execute_open = lambda *a, **kw: (seen_open.append(kw.get("exit_mode")), True, {"ok": True}, 0)[1:]
        buf_cli = io.StringIO()
        with contextlib.redirect_stdout(buf_cli):
            rc_cli = agy.main(["open", "--symbol", "ETHUSDT", "--stop-pct", "0.055", "--tp-pct", "0.60", "--reason", "test", "--exit-mode", "V12_AA_SQZ96"])
        assert rc_cli == 0 and seen_open == ["V12_AA_SQZ96"]
    finally:
        agy._execute_open = orig_exec_open


def test_v13_bcnofl4_and_production_fixes():
    import contextlib
    import io
    import json
    import tempfile

    # 1. V13 BASCRUSH_4H Flash-Crash Capitulation Guard (`btc_ret4h > -4.0%`): blocks -4.0%/-4.1%, allows -3.9%/None, never blocks SP_LEAD_4H
    bc_kw = dict(
        age_days=120.0, breadth_1d=0.30, btc_ret7d=-0.02,
        bar_complete_1h=True, bars_1h_count=200, cur_px=95.0,
        o_1h=96.0, hi_1h=97.0, lo_1h=94.5, c_1h=95.0, c_1h_prev=96.0,
        qv_1h=5e6, qv_avg20_1h=5e6, tb_1h=0.45, ema10_1h=96.0, ema10_1h_prev=95.5,
        atr14_1h_pct=0.02, has_spot_24h=True, bas8h=0.0005, bas12h_max=0.0030, bas_4h_close=0.0005,
        spot_4h_complete=True, spot_tb_4h=0.32, spot_vrat_4h=1.5, ps_vrat=1.2,
        is_fresh_4h_window=True, bar_complete_4h=True, o_4h=99.0, hi_4h=100.0, lo_4h=94.5, c_4h=95.0, c_4h_prev=99.5,
        qv_4h=20e6, qv_avg20_4h=10e6, tb_4h=0.41, ema10_4h=98.0, ema10_4h_prev=98.5, f_day=0.0,
    )
    h_blk = [h["setup"] for h in agy.evaluate_v13_short_setups(**bc_kw, btc_ret4h=-0.041)]
    assert "BASCRUSH_4H" not in h_blk and "SP_LEAD_4H" in h_blk, f"got {h_blk}"
    h_edge = [h["setup"] for h in agy.evaluate_v13_short_setups(**bc_kw, btc_ret4h=-0.040)]
    assert "BASCRUSH_4H" not in h_edge
    h_ok = [h["setup"] for h in agy.evaluate_v13_short_setups(**bc_kw, btc_ret4h=-0.039)]
    assert "BASCRUSH_4H" in h_ok

    # 2. opened_today_utc & opened_this_hour_utc count closed trades opened today/this-hour (beats Proxy 30-order cap)
    now_u = dt.datetime(2026, 11, 24, 11, 20, 0, tzinfo=dt.timezone.utc)
    pf_cl = {
        "orders": [],
        "shorts": [],
        "closed": [
            {"symbol": "AAAUSDT", "opened_at": "2026-11-24T11:00:05+00:00", "closed_at": "2026-11-24T11:10:00+00:00", "pnl": "-1.0"},
            {"symbol": "BBBUSDT", "opened_at": "2026-11-24T03:00:05+00:00", "closed_at": "2026-11-24T05:00:00+00:00", "pnl": "2.0"},
        ],
    }
    assert agy.opened_today_utc(pf_cl, "2026-11-24") == {"AAAUSDT", "BBBUSDT"}
    assert agy.opened_this_hour_utc(pf_cl, now_u) == {"AAAUSDT"}

    # 3. cooldown_symbols takes max(cooldown_until) across history (72h loss still blocks even after a later profit close)
    pf_cd = {
        "closed": [
            {"symbol": "LOSUSDT", "closed_at": "2026-11-23T00:00:00+00:00", "pnl": "-5.0"},
            {"symbol": "LOSUSDT", "closed_at": "2026-11-23T12:00:00+00:00", "pnl": "2.0"},
        ]
    }
    assert "LOSUSDT" in agy.cooldown_symbols(pf_cd, now_u)

    # 4. compute_open_heat_and_counts counts unclear/pending shorts & validate_open_guardrails blocks on_binance=False unclear
    pf_u = {
        "equity_usdt": "880.00", "available_usdt": "780.00", "held_symbols": [],
        "shorts": [{"id": "bto-u", "symbol": "UNCUSDT", "on_binance": False, "unclear": True}],
        "orders": [], "closed": [],
    }
    heat_u, sc_u = agy.compute_open_heat_and_counts(pf_u, {"bto-u": {"setup": "BASOVH_1H", "stop_pct": "0.055"}})
    assert heat_u == Decimal("11.00") and sc_u == {"BASOVH_1H": 1}  # unclear has no qty -> LOT_USDT(200) * 5.5% = 11.00U
    ok_g, err_g = agy.validate_open_guardrails("ETHUSDT", "SHORT", "0.055", "0.60", pf_u, {"circuit_breaker_tripped": False}, now_u)
    assert not ok_g and "ยังไม่รู้ผล" in err_g

    # 5. Parallel _execute_close_batch sends /close for >=2 breaches, calls /sync once, and handles partial failure cleanly
    orig_post = agy.binance.post
    orig_fetch = agy.binance.fetch_portfolio
    orig_save = agy.binance.save_portfolio
    orig_pfp = agy.binance.PORTFOLIO
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_jp = agy.JOURNAL_PATH
    orig_lk = agy.LOCK_PATH
    orig_chat = agy._append_chat_line
    orig_rep = agy._write_round_report
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.binance.PORTFOLIO = os.path.join(td, "portfolio.json")
            agy.POS_META_PATH = os.path.join(td, "agy-positions.json")
            agy.STATE_PATH = os.path.join(td, "agy-state.json")
            agy.JOURNAL_PATH = os.path.join(td, "journal.jsonl")
            agy.LOCK_PATH = os.path.join(td, "guard.lock")
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900", "peak_equity_usdt": "900", "circuit_breaker_tripped": False})
            agy.write_json_atomic(agy.POS_META_PATH, {
                "bto-1": {"symbol": "AAAUSDT", "setup": "BASOVH_1H", "exit_mode": "V12_AA_SQZ96"},
                "bto-2": {"symbol": "BBBUSDT", "setup": "WHALESP_1H", "exit_mode": "V12_AA_SQZ96"},
                "bto-3": {"symbol": "CCCUSDT", "setup": "SPOTDMP_1H", "exit_mode": "V12_AA_SQZ96"},
            })
            agy.write_json_atomic(agy.binance.PORTFOLIO, {
                "equity_usdt": "880.00",
                "shorts": [
                    {"id": "bto-1", "symbol": "AAAUSDT", "on_binance": True},
                    {"id": "bto-2", "symbol": "BBBUSDT", "on_binance": True},
                    {"id": "bto-3", "symbol": "CCCUSDT", "on_binance": True},
                ],
                "closed": [],
            })
            calls = []

            def _mock_post(path, body):
                calls.append((path, body.get("id")))
                if path == "/close" and body.get("id") == "bto-3":
                    return {"status": "refused", "line": "refused"}
                return {"status": "placed", "line": "ok"}

            pf_after = {
                "equity_usdt": "882.00",
                "shorts": [{"id": "bto-3", "symbol": "CCCUSDT", "on_binance": True}],
                "closed": [
                    {"symbol": "AAAUSDT", "closed_at": now_u.isoformat(), "pnl_usdt": "1.20"},
                    {"symbol": "BBBUSDT", "closed_at": now_u.isoformat(), "pnl_usdt": "-0.80"},
                ],
            }
            agy.binance.post = _mock_post
            agy.binance.fetch_portfolio = lambda: pf_after
            agy.binance.save_portfolio = lambda p: (agy.write_json_atomic(agy.binance.PORTFOLIO, p), p)[1]
            agy._append_chat_line = lambda *a, **kw: None
            rep_summaries = []
            agy._write_round_report = lambda reg, summary, **kw: rep_summaries.append(summary)

            breaches = [
                {"id": "bto-1", "symbol": "AAAUSDT", "reason": "FAST_BTC_SQUEEZE_CUT"},
                {"id": "bto-2", "symbol": "BBBUSDT", "reason": "FAST_BTC_SQUEEZE_CUT"},
                {"id": "bto-3", "symbol": "CCCUSDT", "reason": "FAST_BTC_SQUEEZE_CUT"},
            ]
            res_cl = agy._close_fast_breaches(breaches, now_u, dry_run=False)
            assert sum(1 for p, _ in calls if p == "/close") == 3
            assert sum(1 for p, _ in calls if p == "/sync") == 1, f"expected 1 unified sync, got {calls}"
            ok_map = {x["id"]: x["ok"] for x in res_cl}
            assert ok_map == {"bto-1": True, "bto-2": True, "bto-3": False}, f"got {ok_map}"
            meta_left = agy.read_json(agy.POS_META_PATH, default={})
            assert "bto-1" not in meta_left and "bto-2" not in meta_left and "bto-3" in meta_left
            assert rep_summaries and "2/3" in rep_summaries[0] and "ปิดไม่สำเร็จ CCCUSDT" in rep_summaries[0]

            # 6. Pre-open pending_open meta persists on /open exception (no LEGACY downgrade) & is cleaned on refused
            agy.write_json_atomic(agy.POS_META_PATH, {})
            pf_open_ok = {"equity_usdt": "880.00", "available_usdt": "780.00", "held_symbols": [], "shorts": [], "orders": [], "closed": []}
            agy.binance.fetch_portfolio = lambda: pf_open_ok
            agy.binance.post = lambda path, body: (_ for _ in ()).throw(RuntimeError("ssh_timeout"))
            orig_spr = agy.fetch_live_spread
            agy.fetch_live_spread = lambda sym: Decimal("0.0005")
            try:
                ok_to, _, _ = agy._execute_open("ETHUSDT", "0.055", "0.60", "test timeout", setup="BASOVH_1H", exit_mode="V12_AA_SQZ96", now_utc=now_u)
                assert not ok_to
                exp_cid = agy.binance.order_id("open", "2026-11-24", "ETHUSDT")
                pm_after_to = agy.read_json(agy.POS_META_PATH, default={})
                assert exp_cid in pm_after_to and pm_after_to[exp_cid]["pending_open"] is True and pm_after_to[exp_cid]["exit_mode"] == "V12_AA_SQZ96"

                # Refused open cleans up pending_open meta immediately
                agy.write_json_atomic(agy.POS_META_PATH, {})
                agy.binance.post = lambda path, body: {"status": "refused", "cid": exp_cid, "line": "refused"}
                ok_ref, _, _ = agy._execute_open("ETHUSDT", "0.055", "0.60", "test refused", setup="BASOVH_1H", exit_mode="V12_AA_SQZ96", now_utc=now_u)
                assert not ok_ref and exp_cid not in agy.read_json(agy.POS_META_PATH, default={})
            finally:
                agy.fetch_live_spread = orig_spr

            # 7. cmd_auto_trade Phase 2 scan exception returns clean JSON error (no silent crash)
            orig_sync = agy._sync_and_portfolio
            orig_evp = agy._evaluate_open_positions
            orig_scn = agy._scan_market_and_candidates
            orig_chw13 = agy.wait_for_clickhouse_15m
            try:
                agy.wait_for_clickhouse_15m = lambda *a, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0}
                agy._sync_and_portfolio = lambda now_x, do_sync=True: (pf_open_ok, {"status": "ok"}, {}, {"tripped": False})
                agy._evaluate_open_positions = lambda pf_x, now_x, bars_out=None: []
                agy._scan_market_and_candidates = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ch_down"))
                buf_e = io.StringIO()
                with contextlib.redirect_stdout(buf_e):
                    rc_e = agy.cmd_auto_trade(max_opens=1, dry_run=True)
                assert rc_e == 1
                je = json.loads(buf_e.getvalue())
                assert je["ok"] is False and "ch_down" in je["error"]
            finally:
                agy._sync_and_portfolio = orig_sync
                agy._evaluate_open_positions = orig_evp
                agy._scan_market_and_candidates = orig_scn
                agy.wait_for_clickhouse_15m = orig_chw13

            # 8. _evaluate_open_positions skips 1m fetch while opening 15m bar is still open, and tolerates <=120s skew once closed
            orig_k15 = agy.fetch_klines_15m_raw
            orig_k1m = agy.fetch_klines_1m
            orig_btc15 = agy.fetch_klines_15m
            orig_mk = agy.fetch_mark_prices
            try:
                agy.write_json_atomic(agy.POS_META_PATH, {
                    "bto-skew": {"symbol": "SKWUSDT", "exit_mode": "V12_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60", "pending_open": True}
                })
                now_sk = dt.datetime(2026, 11, 24, 11, 16, 0, tzinfo=dt.timezone.utc)
                op_skew = (now_sk + dt.timedelta(seconds=30)).isoformat()  # Proxy clock 30s ahead -> opening bar 11:15 still open
                t_b0 = int(dt.datetime(2026, 11, 24, 11, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
                t_b1 = int(dt.datetime(2026, 11, 24, 11, 15, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
                seen_1m = []
                agy.fetch_klines_15m_raw = lambda sym, limit=400: ([(t_b0, Decimal("101"), Decimal("99"), Decimal("100"))], [])
                agy.fetch_klines_1m = lambda sym, **kw: (seen_1m.append(sym), [])[1]
                agy.fetch_klines_15m = lambda sym, limit=400: []
                agy.fetch_mark_prices = lambda: {}
                pf_sk = {"shorts": [{"id": "bto-skew", "symbol": "SKWUSDT", "on_binance": True, "entry": "100", "mark": "100", "opened_at": op_skew}]}
                evs = agy._evaluate_open_positions(pf_sk, now_sk)
                assert seen_1m == [], f"expected zero wasted 1m fetch while opening bar is still open, got {seen_1m}"
                assert evs[0]["exit_mode"] == "V12_AA_SQZ96"
                saved_sk = agy.read_json(agy.POS_META_PATH, default={})
                assert "pending_open" not in saved_sk["bto-skew"]

                # Once opening bar 11:15 closes at 11:30 (even with -30s clock skew -> now=11:29:30), 1m fetch MUST run
                now_closed = dt.datetime(2026, 11, 24, 11, 29, 30, tzinfo=dt.timezone.utc)
                agy.fetch_klines_15m_raw = lambda sym, limit=400: (
                    [(t_b0, Decimal("101"), Decimal("99"), Decimal("100")), (t_b1, Decimal("101"), Decimal("99"), Decimal("100"))],
                    [],
                )
                agy._evaluate_open_positions(pf_sk, now_closed)
                assert seen_1m == ["SKWUSDT"], f"expected 1m fetch once opening bar closed (-30s skew), got {seen_1m}"
            finally:
                agy.fetch_klines_15m_raw = orig_k15
                agy.fetch_klines_1m = orig_k1m
                agy.fetch_klines_15m = orig_btc15
                agy.fetch_mark_prices = orig_mk
    finally:
        agy.binance.post = orig_post
        agy.binance.fetch_portfolio = orig_fetch
        agy.binance.save_portfolio = orig_save
        agy.binance.PORTFOLIO = orig_pfp
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.JOURNAL_PATH = orig_jp
        agy.LOCK_PATH = orig_lk
        agy._append_chat_line = orig_chat
        agy._write_round_report = orig_rep


def test_v14_sqz1_keepalive_and_hardening():
    import contextlib
    import io
    import json
    import tempfile

    now_u = dt.datetime(2026, 11, 24, 12, 30, 0, tzinfo=dt.timezone.utc)
    opened_iso = (now_u - dt.timedelta(hours=2)).isoformat()

    # 1. V14 SQZ_STG1: extends BTC & B1H Squeeze Cut to HALF50/HALF65 losing trades (cur_pnl < 0),
    #    while protecting HALF50 winners (cur_pnl >= 0), LOCK8/TRAIL13, and preserving V12 INIT-only backward compat.
    btc_ref = {
        "at": now_u.isoformat(),
        "ref_1h_close": "100000",
        "ref_4h_close": "100000",
        "ema20_1h_at": now_u.isoformat(),
        "ema20_1h_map": {f"C{i}USDT": "100" for i in range(25)},
    }
    marks_b1h = {"BTCUSDT": "100000", **{f"C{i}USDT": "101" for i in range(22)}, **{f"C{i}USDT": "99" for i in range(22, 25)}}
    marks_b1h.update({"AA1USDT": "100.5", "AA2USDT": "99.5", "AA3USDT": "100.5", "N71USDT": "101.0", "V12USDT": "100.5"})
    pf_sqz = {
        "shorts": [
            {"id": "v14_aa_half_loss", "symbol": "AA1USDT", "on_binance": True, "entry": "100", "opened_at": opened_iso},
            {"id": "v14_aa_half_win",  "symbol": "AA2USDT", "on_binance": True, "entry": "100", "opened_at": opened_iso},
            {"id": "v14_aa_lock8",     "symbol": "AA3USDT", "on_binance": True, "entry": "100", "opened_at": opened_iso},
            {"id": "v14_n7_half_loss", "symbol": "N71USDT", "on_binance": True, "entry": "100", "opened_at": opened_iso},
            {"id": "v12_aa_half_loss", "symbol": "V12USDT", "on_binance": True, "entry": "100", "opened_at": opened_iso},
        ]
    }
    meta_sqz = {
        "v14_aa_half_loss": {"exit_mode": "V14_AA_SQZ96",   "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "101.0", "tp_px": "40", "ratchet_stage": "HALF50"},
        "v14_aa_half_win":  {"exit_mode": "V14_AA_SQZ96",   "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "101.0", "tp_px": "40", "ratchet_stage": "HALF50"},
        "v14_aa_lock8":     {"exit_mode": "V14_AA_SQZ96",   "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "97.5",  "tp_px": "40", "ratchet_stage": "LOCK8"},
        "v14_n7_half_loss": {"exit_mode": "V14_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60", "stop_px": "102.0", "tp_px": "40", "ratchet_stage": "HALF65"},
        "v12_aa_half_loss": {"exit_mode": "V12_AA_SQZ96",   "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "101.0", "tp_px": "40", "ratchet_stage": "HALF50"},
    }
    br_b1h, _ = agy.check_fast_guard_events(meta_sqz, pf_sqz, marks_b1h, now_u, btc_ref=btc_ref)
    br_b1h_map = {b["id"]: b["reason"] for b in br_b1h}
    # B1H >= 85%: cuts v14_aa_half_loss ONLY (plus AA3 hits SL 97.5); does NOT cut v14_aa_half_win, v14_n7_half_loss, or v12_aa_half_loss!
    assert "v14_aa_half_loss" in br_b1h_map and "FAST_B1H_SQUEEZE_CUT" in br_b1h_map["v14_aa_half_loss"], br_b1h_map
    assert "v14_aa_half_win" not in br_b1h_map
    assert "v14_n7_half_loss" not in br_b1h_map
    assert "v12_aa_half_loss" not in br_b1h_map

    # BTC +2.3%/1h squeeze: cuts BOTH v14_aa_half_loss (HALF50) AND v14_n7_half_loss (HALF65), still leaves v12_aa_half_loss alone
    marks_btc = {**marks_b1h, "BTCUSDT": "102300", **{f"C{i}USDT": "99" for i in range(25)}}
    br_btc, _ = agy.check_fast_guard_events(meta_sqz, pf_sqz, marks_btc, now_u, btc_ref=btc_ref)
    br_btc_map = {b["id"]: b["reason"] for b in br_btc}
    assert "v14_aa_half_loss" in br_btc_map and "FAST_BTC_SQUEEZE_CUT" in br_btc_map["v14_aa_half_loss"]
    assert "v14_n7_half_loss" in br_btc_map and "FAST_BTC_SQUEEZE_CUT" in br_btc_map["v14_n7_half_loss"]
    assert "v14_aa_half_win" not in br_btc_map and "v12_aa_half_loss" not in br_btc_map

    # 15m causal bar path in evaluate_position_exit cuts V14 HALF50 loser on BTC squeeze
    t0_ms = int((now_u - dt.timedelta(hours=2)).timestamp() * 1000)
    btc_bars = [(t0_ms + i * 900_000, Decimal("100000"), Decimal("100000"), Decimal("100000")) for i in range(6)]
    btc_bars.append((t0_ms + 6 * 900_000, Decimal("102000"), Decimal("101900"), Decimal("102000")))  # +2.0% 1h
    sym_bars = [(t0_ms + 6 * 900_000, Decimal("100.8"), Decimal("100.1"), Decimal("100.4"))]
    ev_h50 = agy.evaluate_position_exit(
        {"exit_mode": "V14_AA_SQZ96", "stop_pct": "0.055", "tp_pct": "0.60", "stop_px": "101.0", "ratchet_stage": "HALF50", "mfe_pct": "0.052", "last_bar_ms": t0_ms + 5 * 900_000},
        "100", "100.4", sym_bars, opened_iso, now_u, btc_bars_15m=btc_bars,
    )
    assert ev_h50["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in ev_h50["exit_reason"], ev_h50

    # 2. Reopen guard: pending-settlement short (on_binance=False, entry!=None) blocks same-symbol reopen without consuming MAX_OPEN
    pf_ps = {
        "equity_usdt": "880.00", "available_usdt": "780.00", "held_symbols": [],
        "shorts": [{"id": "bto-old", "symbol": "PNDUSDT", "on_binance": False, "entry": "10.0", "unclear": False}],
        "orders": [], "closed": [],
    }
    ok_same, err_same = agy.validate_open_guardrails("PNDUSDT", "SHORT", "0.055", "0.60", pf_ps, {"circuit_breaker_tripped": False}, now_u)
    assert not ok_same and "REALIZED_PNL" in err_same, err_same
    ok_other, err_other = agy.validate_open_guardrails("ETHUSDT", "SHORT", "0.055", "0.60", pf_ps, {"circuit_breaker_tripped": False}, now_u)
    assert ok_other and err_other is None, err_other

    # 3. spot_tb == 0.0 (100% taker sell) is preserved as 0.0, NOT coerced to None
    base_dt = dt.datetime(2026, 11, 23, 0, 0, tzinfo=dt.timezone.utc)
    smap_zero = {}
    for h in range(21):
        for q in range(4):
            ts_s = (base_dt + dt.timedelta(hours=h, minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S")
            smap_zero[ts_s] = (100.0, 50_000.0, 0.0 if h == 20 else 20_000.0, 100.0)
    fl_z = agy._spot_15m_to_1h_flow(smap_zero, "2026-11-23 20:00:00", end_ts_str="2026-11-23 20:45:00")
    assert fl_z["spot_1h_complete"] is True and fl_z["spot_tb_1h"] == 0.0, fl_z

    # 4. CRITICAL Fixes: portfolio-error guard in _execute_close/_execute_close_batch, persistent close backoff,
    #    _write_round_report plan preservation, _execute_open error + tentative entry_est stops, and cmd_guard parallel batch
    orig_post = agy.binance.post
    orig_fetch = agy.binance.fetch_portfolio
    orig_save = agy.binance.save_portfolio
    orig_pfp = agy.binance.PORTFOLIO
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_jp = agy.JOURNAL_PATH
    orig_lk = agy.LOCK_PATH
    orig_cbk = agy.CLOSE_BACKOFF_PATH
    orig_rep_dir = agy.REPORTS
    orig_chat = agy._append_chat_line
    orig_spr = agy.fetch_live_spread
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.binance.PORTFOLIO = os.path.join(td, "portfolio.json")
            agy.POS_META_PATH = os.path.join(td, "agy-positions.json")
            agy.STATE_PATH = os.path.join(td, "agy-state.json")
            agy.JOURNAL_PATH = os.path.join(td, "journal.jsonl")
            agy.LOCK_PATH = os.path.join(td, "guard.lock")
            agy.CLOSE_BACKOFF_PATH = os.path.join(td, "close-backoff.json")
            agy.REPORTS = os.path.join(td, "reports")
            agy._append_chat_line = lambda *a, **kw: None
            agy.fetch_live_spread = lambda sym: Decimal("0.0005")
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900", "peak_equity_usdt": "900", "circuit_breaker_tripped": False})
            agy.write_json_atomic(agy.POS_META_PATH, {
                "bto-err1": {"symbol": "ERR1USDT", "exit_mode": "V14_AA_SQZ96", "stop_pct": "0.055"},
                "bto-err2": {"symbol": "ERR2USDT", "exit_mode": "V14_AA_SQZ96", "stop_pct": "0.055"},
            })

            # 4a. _execute_close & _execute_close_batch MUST NOT delete pos_meta when fetch_portfolio returns error
            agy.binance.post = lambda path, body: {"status": "unclear", "line": "unclear"}
            agy.binance.fetch_portfolio = lambda: {"error": "proxy_down", "shorts": []}
            agy.binance.save_portfolio = lambda p: p
            ok_c, res_c, _ = agy._execute_close("bto-err1", "SL", now_utc=now_u)
            assert not ok_c and res_c["still_on_binance"] is True and "proxy_down" in res_c.get("error", "")
            assert "bto-err1" in agy.read_json(agy.POS_META_PATH, default={})

            batch_res = agy._execute_close_batch(
                [{"id": "bto-err1", "symbol": "ERR1USDT", "why": "SQZ"}, {"id": "bto-err2", "symbol": "ERR2USDT", "why": "SQZ"}],
                now_utc=now_u,
            )
            assert all(not ok_b and r_b["still_on_binance"] is True for ok_b, r_b in batch_res)
            pm_after_err = agy.read_json(agy.POS_META_PATH, default={})
            assert "bto-err1" in pm_after_err and "bto-err2" in pm_after_err

            # 4b. Persistent cross-process close_fail_backoff (45s -> 120s -> 300s -> cleared on ok=True)
            agy._update_close_backoff([{"id": "bto-err1", "ok": False}], now_u)
            b1 = agy._load_close_backoff(now_u)
            assert "bto-err1" in b1 and abs((b1["bto-err1"] - now_u.timestamp()) - 45.0) < 1.0
            agy._update_close_backoff([{"id": "bto-err1", "ok": False}], now_u + dt.timedelta(seconds=10))
            b2 = agy._load_close_backoff(now_u + dt.timedelta(seconds=10))
            assert abs((b2["bto-err1"] - (now_u + dt.timedelta(seconds=10)).timestamp()) - 120.0) < 1.0
            agy._update_close_backoff([{"id": "bto-err1", "ok": True}], now_u + dt.timedelta(seconds=20))
            assert "bto-err1" not in agy._load_close_backoff(now_u + dt.timedelta(seconds=20))

            # 4c. _write_round_report(..., preserve_plan_if_no_picks=True) keeps hourly short/rows intact
            agy.write_json_atomic(agy.binance.PORTFOLIO, {"held_symbols": ["ETHUSDT"], "closed": []})
            agy._write_round_report("BEARISH_FAVORABLE", "hourly plan", universe=190, picks=[{"symbol": "ETHUSDT", "primary_setup": "BASOVH_1H", "score": 270}], now_utc=now_u)
            agy._write_round_report("BEARISH_FAVORABLE", "fast-guard exit", actions=["FAST_GUARD exit"], now_utc=now_u, preserve_plan_if_no_picks=True)
            plan_saved = agy.read_json(os.path.join(agy.REPORTS, "2026-11-24", "dca-plan.json"), default={})
            assert plan_saved["short"] == ["ETHUSDT"] and len(plan_saved["rows"]) == 1 and plan_saved["universe"] == 190

            # 4d. _execute_open returns error on refused & sets tentative entry_est stops when post-open /sync lacks entry
            pf_clean = {"equity_usdt": "880.00", "available_usdt": "780.00", "held_symbols": [], "shorts": [], "orders": [], "closed": []}
            agy.binance.fetch_portfolio = lambda: pf_clean
            agy.binance.post = lambda path, body: {"status": "refused", "why": "ครบเพดานรายวัน", "line": "refused"}
            ok_r, res_r, _ = agy._execute_open("ETHUSDT", "0.055", "0.60", "test", setup="BASOVH_1H", exit_mode="V14_AA_SQZ96", now_utc=now_u)
            assert not ok_r and "ครบเพดาน" in str(res_r.get("error") or "")

            agy.binance.post = lambda path, body: {"status": "placed", "cid": "bto-2026-11-24-ETH", "line": "placed"}
            ok_e, _, _ = agy._execute_open("ETHUSDT", "0.055", "0.60", "test", setup="BASOVH_1H", exit_mode="V14_AA_SQZ96", now_utc=now_u, entry_est=2000.0)
            assert ok_e
            pm_est = agy.read_json(agy.POS_META_PATH, default={})["bto-2026-11-24-ETH"]
            assert pm_est["tentative_stops"] is True and Decimal(pm_est["stop_px"]) == Decimal("2110.000")
            # Real fill entry in evaluate_position_exit replaces tentative stops cleanly
            ev_real = agy.evaluate_position_exit(pm_est, "1980", "1980", [], now_u.isoformat(), now_u)
            assert "tentative_stops" not in ev_real["updated_meta"] and Decimal(ev_real["updated_meta"]["stop_px"]) == Decimal("1980") * Decimal("1.055")

            # 4e. CLI --exit-mode accepts V14_AA_SQZ96 & V14_NEW7_SQZ96
            orig_eo = agy._execute_open
            try:
                seen_em = []
                agy._execute_open = lambda *a, **kw: (seen_em.append(kw.get("exit_mode")), True, {"ok": True}, 0)[1:]
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    assert agy.main(["open", "--symbol", "ETHUSDT", "--stop-pct", "0.055", "--tp-pct", "0.60", "--reason", "t", "--exit-mode", "V14_AA_SQZ96"]) == 0
                assert seen_em == ["V14_AA_SQZ96"]
            finally:
                agy._execute_open = orig_eo
    finally:
        agy.binance.post = orig_post
        agy.binance.fetch_portfolio = orig_fetch
        agy.binance.save_portfolio = orig_save
        agy.binance.PORTFOLIO = orig_pfp
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.JOURNAL_PATH = orig_jp
        agy.LOCK_PATH = orig_lk
        agy.CLOSE_BACKOFF_PATH = orig_cbk
        agy.REPORTS = orig_rep_dir
        agy._append_chat_line = orig_chat
        agy.fetch_live_spread = orig_spr


def test_v15_uncaged_spotflow_pos_1h_and_parity():
    import contextlib
    import io
    import json
    import tempfile

    # 1. Locked R0 constants + V15 uncaged portfolio controls
    assert agy.ALLOWED_SIDE == "SHORT"
    assert agy.MARGIN_USDT == Decimal("100")
    assert agy.LOT_USDT == Decimal("200")
    assert agy.LEVERAGE == 2
    assert agy.MAX_OPEN == 0
    assert agy.MAX_LONG_OPEN == 0
    assert agy.MAX_SHORT_OPEN == 0
    assert agy.DAILY_MAX_OPENS == 0
    assert agy.CB_AUTO_ENABLED is True
    assert agy.CB_MAX_LOSS_USDT == Decimal("45.00")
    assert agy.CB_MAX_LOSS_PCT == Decimal("0.05")
    assert agy.SHORT_COOLDOWN_DAYS == 3
    assert agy.MAX_LOSSES_PER_SYM_7D == 2
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.MAX_OPEN_HEAT_USDT == Decimal("0")
    assert agy.ENGINE_MAX_OPEN == {}
    assert agy.ENGINE_HEAT_CAP == {}
    assert agy.DEFAULT_EXIT_MODE == "V18_AA_SQZ96"
    assert agy.NEW7_EXIT_MODE == "V21_NEW7_SQZ96"

    # 2. SPOTFLOW_POS_1H fires WITHOUT fresh_1h_cross (continuation below EMA10) with strict positive-basis guard
    sfp_base = dict(
        age_days=180.0, breadth_1d=0.40, btc_ret7d=-0.02, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.955, o_1h=0.972, hi_1h=0.976, lo_1h=0.950, c_1h=0.955, c_1h_prev=0.968,
        qv_1h=1_200_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.38,
        ema10_1h=0.975, ema10_1h_prev=0.978,  # c_1h_prev (0.968) < ema10_1h_prev (0.978) -> NOT fresh cross!
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.03, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0001, ps_vrat=1.20,
        spot_1h_complete=True, spot_tb_1h=0.32, spot_vrat_1h=1.65, spot_tsize_r_1h=1.05,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_sfp = agy.evaluate_v15_short_setups(**sfp_base)
    assert [h["setup"] for h in hits_sfp] == ["SPOTFLOW_POS_1H"], hits_sfp
    assert hits_sfp[0]["stop_pct"] == 0.050 and hits_sfp[0]["tp_pct"] == 0.600 and hits_sfp[0]["exit_mode"] == "V18_AA_SQZ96"
    expected_sc = round(225.0 + (0.45 - 0.32) * 80.0 + (0.45 - 0.38) * 50.0, 2)
    assert hits_sfp[0]["score"] == expected_sc and hits_sfp[0]["score"] < 245.0
    # Strict guards on SPOTFLOW_POS_1H (especially ZERO negative basis dilution!)
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "bas8h": -0.0001}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "bas8h": None}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "breadth_1d": 0.46}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "ema10_1h": 1.000}) == []        # ext = -4.5% < -4.0%
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "c_1h": 0.976, "cur_px": 0.976, "o_1h": 0.980, "hi_1h": 0.985}) == []  # above ema10
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "cur_px": 0.976}) == []          # live cur_px reclaimed ema10
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "lo_1h": 0.930}) == []           # lwick >= 0.30
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "tb_1h": 0.42}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "spot_tb_1h": 0.36}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "spot_vrat_1h": 1.49}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "spot_1h_complete": False}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "has_spot_24h": False}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "btc_ret7d": 0.08}) == []
    assert agy.evaluate_v15_short_setups(**{**sfp_base, "f_day": -0.0013}) == []

    # 3. _classify_1h_shortlist_tier: Tier-1 core stays ahead of Tier-2 SPOTFLOW_POS_1H; rejects overextended/weak bars
    cl_fresh = [100.0] * 21 + [100.5, 99.0]  # fresh cross below EMA10 (~100.0)
    op_fresh = [100.0] * 22 + [100.5]
    hi_fresh = [101.0] * 22 + [100.8]
    lo_fresh = [99.0] * 22 + [98.8]
    qv_base = [1_000_000.0] * 23
    tb_ok = [0.50] * 22 + [0.38]
    assert agy._classify_1h_shortlist_tier(120.0, op_fresh, hi_fresh, lo_fresh, cl_fresh, qv_base, tb_ok) == 1
    # Continuation bar (prev already < EMA10, ext ~ -2.0% >= -4%, tb 0.38 < 0.42, lwick ~ 0.10 < 0.30) -> Tier 2!
    cl_cont = [100.0] * 20 + [98.5, 98.0, 97.2]
    op_cont = [100.0] * 22 + [98.2]
    hi_cont = [101.0] * 22 + [98.3]
    lo_cont = [99.0] * 22 + [97.1]
    assert agy._classify_1h_shortlist_tier(120.0, op_cont, hi_cont, lo_cont, cl_cont, qv_base, tb_ok) == 2
    # Overextended (> 4% below EMA10) -> Tier 0 (skip wasted REST fetch)
    cl_over = [100.0] * 20 + [98.5, 98.0, 94.0]
    lo_over = [99.0] * 22 + [93.9]
    assert agy._classify_1h_shortlist_tier(120.0, op_cont, hi_cont, lo_over, cl_over, qv_base, tb_ok) == 0
    # Perp tb >= 0.42 -> Tier 0 for SPOTFLOW_POS_1H
    assert agy._classify_1h_shortlist_tier(120.0, op_cont, hi_cont, lo_cont, cl_cont, qv_base, [0.50] * 22 + [0.42]) == 0

    # 4. cmd_auto_trade default max_opens=3: uncages multi-slot SPOTDMP_4H & BEAR_BRK_1H up to 32U heat,
    #    while capping SPOTFLOW_POS_1H at 2 and blocking overflow above 32U!
    now_hr = dt.datetime(2026, 11, 24, 12, 0, 10, tzinfo=dt.timezone.utc)
    orig_lock = agy.LOCK_PATH
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_pf = agy.binance.PORTFOLIO
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_now = agy.utcnow
    orig_chw15 = agy.wait_for_clickhouse_15m
    try:
        agy.wait_for_clickhouse_15m = lambda *a, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0}
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "agy-guard.lock")
            agy.POS_META_PATH = os.path.join(td, "agy-positions.json")
            agy.STATE_PATH = os.path.join(td, "agy-state.json")
            agy.binance.PORTFOLIO = os.path.join(td, "portfolio.json")
            # 3 existing open shorts = 15.00U heat (including 1 SPOTDMP_4H + 1 SPOTFLOW_POS_1H)
            agy.write_json_atomic(agy.POS_META_PATH, {
                "p1": {"symbol": "EX1USDT", "setup": "SPOTDMP_4H", "stop_pct": "0.050", "stop_px": "105", "entry": "100"},
                "p2": {"symbol": "EX2USDT", "setup": "SPOTFLOW_POS_1H", "stop_pct": "0.050", "stop_px": "105", "entry": "100"},
                "p3": {"symbol": "EX3USDT", "setup": "WHALESP_1H", "stop_pct": "0.050", "stop_px": "105", "entry": "100"},
            })
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900", "peak_equity_usdt": "900", "circuit_breaker_tripped": False})
            pf_mock = {
                "equity_usdt": "900.00", "available_usdt": "600.00",
                "held_symbols": ["EX1USDT", "EX2USDT", "EX3USDT"],
                "shorts": [
                    {"id": "p1", "symbol": "EX1USDT", "entry": "100", "on_binance": True},
                    {"id": "p2", "symbol": "EX2USDT", "entry": "100", "on_binance": True},
                    {"id": "p3", "symbol": "EX3USDT", "entry": "100", "on_binance": True},
                ],
                "orders": [], "closed": [],
            }
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf_mock)

            def _mk_c(sym, setup, score, sl=0.050):
                return {
                    "symbol": sym, "primary_setup": setup, "score": score,
                    "suggested_stop_pct": sl, "suggested_tp_pct": 0.60, "exit_mode": "V14_AA_SQZ96",
                    "ret_24h_pct": -2.0, "ret_7d_pct": -8.0, "tb_1h": 0.35, "spot_tb_1h": 0.32,
                    "spot_vrat_1h": 1.8, "ext_e10_1h_pct": -1.5,
                    "hit_specs": [{"setup": setup, "score": score, "stop_pct": sl, "tp_pct": 0.60, "exit_mode": "V14_AA_SQZ96"}],
                }

            agy._sync_and_portfolio = lambda now_u, do_sync=True: (pf_mock, {"status": "ok"}, {}, {"tripped": False})
            agy._evaluate_open_positions = lambda pf, now_u, bars_out=None, flow_out=None: []
            agy._scan_market_and_candidates = lambda pf, now_u, held_bars_15m=None: (
                {"regime_label": "BEARISH_FAVORABLE", "funding_ok": True, "complete_1h_pct": 100.0, "btc": {"price": 60000}, "breadth": {"pct_above_1d_sma20": 20.0, "pct_above_1h_ema20": 20.0}},
                [
                    _mk_c("C1USDT", "BEAR_BRK_1H", 265.0),       # +5U -> 20U (uncaged! V14 would block at 5U idle cap)
                    _mk_c("C2USDT", "SPOTDMP_4H", 255.0),        # +5U -> 25U (2nd SPOTDMP_4H! V14 would block at cap 1)
                    _mk_c("C3USDT", "SPOTFLOW_POS_1H", 238.0),   # +5U -> 30U (2nd SPOTFLOW_POS_1H allowed, reaches default max_opens=3)
                    _mk_c("C4USDT", "SPOTFLOW_POS_1H", 236.0),   # blocked by max_opens=3 AND cap 2
                ],
                [], [], {},
            )
            agy.utcnow = lambda: now_hr

            # Default unlimited mode (max_opens=0, MAX_OPEN_HEAT_USDT=0, ENGINE_MAX_OPEN={}): all 4 candidates open! (3 existing 200U*5%=30U + 4 new 200U*5%=40U = 70.00U)
            buf0 = io.StringIO()
            with contextlib.redirect_stdout(buf0):
                rc0 = agy.cmd_auto_trade(max_opens=0, dry_run=True)
            assert rc0 == 0
            out_j0 = json.loads(buf0.getvalue())
            assert out_j0["strategy"] == "V23_EMA_RSI_MOOD"
            assert out_j0["max_heat_usdt"] == "0"
            assert len(out_j0["opened"]) == 4 and out_j0["open_heat_usdt"] == "70.00", out_j0

            # Verify explicit heat cap (64U) & SPOTFLOW_POS_1H cap 2 when enabled
            orig_mh = agy.MAX_OPEN_HEAT_USDT
            orig_em = agy.ENGINE_MAX_OPEN
            agy.MAX_OPEN_HEAT_USDT = Decimal("64.00")
            agy.ENGINE_MAX_OPEN = {"SPOTFLOW_POS_1H": 2}
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = agy.cmd_auto_trade(max_opens=3, dry_run=True)
                assert rc == 0
                out_j = json.loads(buf.getvalue())
                assert out_j["max_heat_usdt"] == "64.00"
                assert [(x["symbol"], x["setup"]) for x in out_j["opened"]] == [
                    ("C1USDT", "BEAR_BRK_1H"),
                    ("C2USDT", "SPOTDMP_4H"),
                    ("C3USDT", "SPOTFLOW_POS_1H"),
                ], out_j["opened"]
                assert out_j["open_heat_usdt"] == "60.00"

                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc2 = agy.cmd_auto_trade(max_opens=5, dry_run=True)
                assert rc2 == 0
                out_j2 = json.loads(buf2.getvalue())
                assert len(out_j2["opened"]) == 3  # C4USDT blocked by engine_cap_2!
                assert any(s["symbol"] == "C4USDT" and s["reason"] == "engine_cap_2" for s in out_j2["skipped_risk"]), out_j2["skipped_risk"]
            finally:
                agy.MAX_OPEN_HEAT_USDT = orig_mh
                agy.ENGINE_MAX_OPEN = orig_em
    finally:
        agy.LOCK_PATH = orig_lock
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.binance.PORTFOLIO = orig_pf
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.utcnow = orig_now
        agy.wait_for_clickhouse_15m = orig_chw15


def test_v16_2h_posmoat_engines_and_parity():
    # 1. Version & R0 rails preserved
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.LOT_USDT == Decimal("200") and agy.MARGIN_USDT == Decimal("100") and agy.LEVERAGE == 2 and agy.MAX_OPEN == 0 and agy.DAILY_MAX_OPENS == 0
    assert agy.CB_AUTO_ENABLED is True and agy.CB_MAX_LOSS_USDT == Decimal("45.00") and agy.CB_MAX_LOSS_PCT == Decimal("0.05")

    # 2. _hourly_to_2h_flow aggregates pairs of 1H bars into 2H UTC buckets
    hrs = ["2026-11-24 00:00:00", "2026-11-24 01:00:00", "2026-11-24 02:00:00", "2026-11-24 03:00:00", "2026-11-24 04:00:00"]
    h2 = agy._hourly_to_2h_flow(
        hrs,
        [10.0, 9.8, 9.5, 9.2, 9.0],
        [10.2, 9.9, 9.7, 9.4, 9.1],
        [9.7, 9.4, 9.1, 8.9, 8.8],
        [9.8, 9.5, 9.2, 9.0, 8.9],
        [100.0, 300.0, 200.0, 200.0, 50.0],
        [0.60, 0.30, 0.40, 0.20, 0.50],
        [4, 4, 4, 4, 4],
    )
    assert len(h2) == 2  # trailing incomplete 04:00 skipped
    assert h2[0][0] == "2026-11-24 00:00:00" and h2[0][1] == 10.0 and h2[0][2] == 10.2 and h2[0][3] == 9.4 and h2[0][4] == 9.5
    assert abs(h2[0][5] - 400.0) < 1e-9 and abs(h2[0][6] - (60.0 + 90.0) / 400.0) < 1e-9 and h2[0][7] == 8
    assert h2[1][0] == "2026-11-24 02:00:00" and abs(h2[1][6] - 0.30) < 1e-9 and h2[1][7] == 8

    # 3. _spot_15m_to_2h_flow & _spot_15m_to_4h_flow compute spot_vrat and spot_tsize_r with exact 21-bar continuity
    base2 = dt.datetime(2026, 11, 22, 0, 0, tzinfo=dt.timezone.utc)
    smap2 = {}
    for b2 in range(21):
        qv_b = 2000.0 if b2 == 20 else 1000.0
        tc_b = 10.0 if b2 == 20 else 10.0  # tsize doubles on last bar -> spot_tsize_r_2h == 2.0
        for q15 in range(8):
            t_s = (base2 + dt.timedelta(hours=b2 * 2, minutes=q15 * 15)).strftime("%Y-%m-%d %H:%M:%S")
            smap2[t_s] = (1.0, qv_b / 8.0, (qv_b * 0.32) / 8.0, tc_b / 8.0)
    f2 = agy._spot_15m_to_2h_flow(smap2, "2026-11-23 16:00:00", end_ts_str="2026-11-23 17:45:00")
    assert f2["spot_2h_complete"] is True
    assert abs(f2["spot_tb_2h"] - 0.32) < 1e-9
    assert abs(f2["spot_vrat_2h"] - 2.0) < 1e-9
    assert abs(f2["spot_tsize_r_2h"] - 2.0) < 1e-9

    base4 = dt.datetime(2026, 11, 20, 0, 0, tzinfo=dt.timezone.utc)
    smap4 = {}
    for b4 in range(21):
        qv_b = 3000.0 if b4 == 20 else 2000.0
        tc_b = 10.0  # tsize ratio = 1.5
        for q15 in range(16):
            t_s = (base4 + dt.timedelta(hours=b4 * 4, minutes=q15 * 15)).strftime("%Y-%m-%d %H:%M:%S")
            smap4[t_s] = (1.0, qv_b / 16.0, (qv_b * 0.35) / 16.0, tc_b / 16.0)
    f4 = agy._spot_15m_to_4h_flow(smap4, "2026-11-23 08:00:00", end_ts_str="2026-11-23 11:45:00")
    assert f4["spot_4h_complete"] is True and abs(f4["spot_tsize_r_4h"] - 1.5) < 1e-9

    # 4. All 4 V16 2H engines (SP_LEAD_2H, WHALESP_2H, SPOTDMP_POS_2H, SPOTFLOW_POS_2H) + strict bas8h>=0 & subordinated priority (<225)
    base_2h = dict(
        age_days=150.0, breadth_1d=0.38, btc_ret7d=-0.03, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.955, o_1h=0.950, hi_1h=0.960, lo_1h=0.945, c_1h=0.955, c_1h_prev=0.950,  # green 1h -> no 1h hit
        qv_1h=1_000_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.52, ema10_1h=0.960, ema10_1h_prev=0.960,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.02, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0002, ps_vrat=2.40,
        is_fresh_2h_window=True, bar_complete_2h=True,
        o_2h=0.990, hi_2h=0.995, lo_2h=0.950, c_2h=0.955, c_2h_prev=0.985,
        tb_2h=0.41, ema10_2h=0.975, ema10_2h_prev=0.980,
        spot_2h_complete=True, spot_tb_2h=0.33, spot_vrat_2h=1.60, spot_tsize_r_2h=1.35,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_2h = agy.evaluate_v16_short_setups(**base_2h)
    names_2h = [h["setup"] for h in hits_2h]
    assert names_2h == ["WHALESP_2H", "SP_LEAD_2H", "SPOTDMP_POS_2H", "SPOTFLOW_POS_2H"], hits_2h
    assert all(221.0 <= h["score"] < 225.0 for h in hits_2h), hits_2h  # strictly subordinated below V15 Core (225+)!
    assert all(h["stop_pct"] == 0.050 and h["tp_pct"] == 0.600 and h["exit_mode"] == "V18_AA_SQZ96" for h in hits_2h)
    # Zero negative-basis dilution across all 4 2H engines
    assert agy.evaluate_v16_short_setups(**{**base_2h, "bas8h": -0.0001}) == []
    assert agy.evaluate_v16_short_setups(**{**base_2h, "bas8h": None}) == []
    assert agy.evaluate_v16_short_setups(**{**base_2h, "is_fresh_2h_window": False}) == []
    # Continuation mode (not fresh_2h_cross): only SPOTFLOW_POS_2H fires!
    hits_cont2 = agy.evaluate_v16_short_setups(**{**base_2h, "c_2h_prev": 0.970})
    assert [h["setup"] for h in hits_cont2] == ["SPOTFLOW_POS_2H"], hits_cont2
    # Overextended 2H (< -4.5% below EMA10) blocks SPOTFLOW_POS_2H
    assert agy.evaluate_v16_short_setups(**{**base_2h, "c_2h_prev": 0.970, "ema10_2h": 1.010}) == []

    # 5. V16 4H additions (WHALESP_4H & SPOTFLOW_POS_4H) + strict bas8h>=0 & subordinated priority
    base_4h_v16 = dict(
        age_days=150.0, breadth_1d=0.40, btc_ret7d=-0.02, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.955, o_1h=0.950, hi_1h=0.960, lo_1h=0.945, c_1h=0.955, c_1h_prev=0.950,
        qv_1h=1_000_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.52, ema10_1h=0.960, ema10_1h_prev=0.960,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.02, ret_12h=-0.04, ret_24h=-0.05, ret_7d=-0.10,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True, bas8h=0.0002, ps_vrat=1.20,
        is_fresh_4h_window=True, bar_complete_4h=True,
        o_4h=0.990, hi_4h=0.995, lo_4h=0.950, c_4h=0.955, c_4h_prev=0.985,
        tb_4h=0.39, ema10_4h=0.975, ema10_4h_prev=0.980,
        spot_4h_complete=True, spot_tb_4h=0.36, spot_vrat_4h=1.50, spot_tsize_r_4h=1.35,
    )
    hits_4h_v16 = agy.evaluate_v16_short_setups(**base_4h_v16)
    assert [h["setup"] for h in hits_4h_v16] == ["WHALESP_4H", "SPOTFLOW_POS_4H"], hits_4h_v16
    assert all(221.0 <= h["score"] < 225.0 for h in hits_4h_v16)
    assert agy.evaluate_v16_short_setups(**{**base_4h_v16, "bas8h": -0.0001}) == []

    # 6. _fetch_spot_flow_rest_fallback supports 2h interval and returns spot_tsize_r_2h
    now_2h = dt.datetime(2026, 11, 24, 14, 0, 10, tzinfo=dt.timezone.utc)
    t_start_ms = int(dt.datetime(2026, 11, 22, 20, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    mock_rows_2h = []
    for i in range(21):
        t_ms = t_start_ms + i * 7_200_000
        qv_i = 2000.0 if i == 20 else 1000.0
        mock_rows_2h.append([t_ms, "1", "1.1", "0.9", "0.95", "100", t_ms + 7_199_999, str(qv_i), 10, "30", str(qv_i * 0.34), "0"])
    orig_fetch = agy._fetch_rest_klines
    try:
        agy._fetch_rest_klines = lambda url, sym, interval, limit=25, timeout=10: (sym, mock_rows_2h)
        fb2 = agy._fetch_spot_flow_rest_fallback(["TESTUSDT"], "2h", "2026-11-24 12:00:00", now_2h)
        assert "TESTUSDT" in fb2 and fb2["TESTUSDT"]["spot_2h_complete"] is True
        assert abs(fb2["TESTUSDT"]["spot_tb_2h"] - 0.34) < 1e-9
        assert abs(fb2["TESTUSDT"]["spot_vrat_2h"] - 2.0) < 1e-9
        assert abs(fb2["TESTUSDT"]["spot_tsize_r_2h"] - 2.0) < 1e-9
    finally:
        agy._fetch_rest_klines = orig_fetch


def test_v16_1_parallel_patch_8h_gap_spread_and_labels():
    import tempfile
    # 1. Verify 8h basis gap repair triggers even when recent 1h bars (req_ts) are already present
    now_u = dt.datetime(2026, 11, 24, 16, 0, 5, tzinfo=dt.timezone.utc)
    # Only 4 bars (15:00..15:45) present -> req_ts satisfied, but < 24 bars in 8h window -> MUST patch!
    t_recent = ["2026-11-24 15:00:00", "2026-11-24 15:15:00", "2026-11-24 15:30:00", "2026-11-24 15:45:00"]
    m15 = {"GAPUSDT": (list(t_recent), [1.0] * 4, [100.0] * 4, [1.01] * 4, [0.99] * 4)}
    sp15 = {"GAPUSDT": {ts: (1.0, 100.0, 40.0, 10.0) for ts in t_recent}}

    base_ms = int(dt.datetime(2026, 11, 24, 8, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    mock_15m = [
        [base_ms + i * 900_000, "1", "1.02", "0.98", "1.0", "100", base_ms + (i + 1) * 900_000 - 1, "100", 10, "40", "40", "0"]
        for i in range(32)
    ]
    calls = []
    orig_fetch = agy._fetch_rest_klines
    try:
        def _mock_fetch(url, sym, interval, limit=3, timeout=10):
            calls.append((url, sym, interval, limit))
            return sym, mock_15m
        agy._fetch_rest_klines = _mock_fetch
        patched = agy._patch_shortlist_15m_from_binance(
            m15, sp15, ["GAPUSDT"], "2026-11-24 15:45:00", now_u,
            window_start_str="2026-11-24 15:00:00", spot_live_syms={"GAPUSDT"},
        )
        assert patched == 1
        assert len(m15["GAPUSDT"][0]) == 32, len(m15["GAPUSDT"][0])
        assert len(sp15["GAPUSDT"]) == 32, len(sp15["GAPUSDT"])
        assert any(u == agy.FAPI_KLINES for u, _, _, _ in calls)
        assert any(u == agy.SPOT_KLINES for u, _, _, _ in calls)
    finally:
        agy._fetch_rest_klines = orig_fetch

    # 2. Verify prefetched_spread skips redundant fetch_live_spread call in _execute_open
    spread_calls = []
    orig_sp = agy.fetch_live_spread
    orig_fp = agy.binance.fetch_portfolio
    orig_sav = agy.binance.save_portfolio
    orig_post = agy.binance.post
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_es = agy.ENTRY_SIGNALS_PATH
    orig_jn = agy.JOURNAL_PATH
    orig_chat = agy._append_chat_line
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.STATE_PATH = os.path.join(td, "st.json")
            agy.ENTRY_SIGNALS_PATH = os.path.join(td, "es.json")
            agy.JOURNAL_PATH = os.path.join(td, "jn.jsonl")
            agy._append_chat_line = lambda *a, **k: None
            pf_ok = {"equity_usdt": "900.00", "available_usdt": "800.00", "shorts": [], "held_symbols": [], "orders": [], "closed": []}
            agy.binance.fetch_portfolio = lambda: pf_ok
            agy.binance.save_portfolio = lambda pf: pf
            agy.binance.post = lambda path, body: {"status": "tested", "cid": "bto-2026-11-24-AAA", "line": "ok"}
            agy.fetch_live_spread = lambda sym: (spread_calls.append(sym) or Decimal("0.0005"))

            ok, res, rc = agy._execute_open(
                "AAAUSDT", "0.050", "0.600", "test V16.1", setup="SP_LEAD_2H",
                now_utc=now_u, entry_est=1.0, prefetched_spread=Decimal("0.0004"),
            )
            assert ok is True and rc == 0
            assert spread_calls == [], f"Expected prefetched spread reuse, got {spread_calls}"
            assert res["spread"] == "0.0004"
            pm = agy.read_json(agy.POS_META_PATH, default={})
            assert pm["bto-2026-11-24-AAA"]["opened_by"] == "Antigravity-V22"
            es = agy.read_json(agy.ENTRY_SIGNALS_PATH, default={})
            assert es["AAA"] == "AGY-V22 SHORT (SP_LEAD_2H)"
    finally:
        agy.fetch_live_spread = orig_sp
        agy.binance.fetch_portfolio = orig_fp
        agy.binance.save_portfolio = orig_sav
        agy.binance.post = orig_post
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.ENTRY_SIGNALS_PATH = orig_es
        agy.JOURNAL_PATH = orig_jn
        agy._append_chat_line = orig_chat


def test_v16_2_production_parity_and_hardening():
    import tempfile

    now_u = dt.datetime(2026, 11, 24, 16, 0, 10, tzinfo=dt.timezone.utc)

    # 1. check_fast_guard_events ignores stale tentative_stops once real Binance fill `p["entry"]` arrives
    #    (entry_est=1.000 -> tentative stop=1.050; actual fill=1.008 -> true SL=1.0584; mark=1.053 must NOT false-stop!)
    pf_real = {
        "shorts": [{
            "id": "bto-2026-11-24-AAA",
            "symbol": "AAAUSDT",
            "entry": "1.008",
            "on_binance": True,
            "opened_at": "2026-11-24T16:00:05+00:00",
        }]
    }
    pm_tent = {
        "bto-2026-11-24-AAA": {
            "symbol": "AAAUSDT",
            "exit_mode": "V14_AA_SQZ96",
            "stop_pct": "0.050",
            "tp_pct": "0.600",
            "entry_est": "1.000",
            "stop_px": "1.050",
            "tp_px": "0.400",
            "tentative_stops": True,
            "ratchet_stage": "INIT",
        }
    }
    br_ok, _ = agy.check_fast_guard_events(pm_tent, pf_real, {"AAAUSDT": Decimal("1.053")}, now_u)
    assert br_ok == [], f"Expected no premature stop on stale tentative_stops, got {br_ok}"
    br_hit, _ = agy.check_fast_guard_events(pm_tent, pf_real, {"AAAUSDT": Decimal("1.059")}, now_u)
    assert len(br_hit) == 1 and "FAST_SL_MARK" in br_hit[0]["reason"]
    # When real fill has NOT arrived yet (entry=None), tentative stop 1.050 MUST still protect!
    pf_pending = {"shorts": [{**pf_real["shorts"][0], "entry": None}]}
    br_pend, _ = agy.check_fast_guard_events(pm_tent, pf_pending, {"AAAUSDT": Decimal("1.053")}, now_u)
    assert len(br_pend) == 1 and "FAST_SL_MARK" in br_pend[0]["reason"]

    # 2. _patch_shortlist_15m_from_binance(check_12h_gap=True) repairs 9-12h gaps even when 8h has 32/32 bars
    t_8h = [(dt.datetime(2026, 11, 24, 8, 0, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(32)]
    m15_8h = {"BC4USDT": (list(t_8h), [1.0] * 32, [100.0] * 32, [1.01] * 32, [0.99] * 32)}
    sp15_8h = {"BC4USDT": {ts: (1.0, 100.0, 40.0, 10.0) for ts in t_8h}}
    base12_ms = int(dt.datetime(2026, 11, 24, 4, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    mock_12h = [
        [base12_ms + i * 900_000, "1", "1.02", "0.98", "1.0", "100", base12_ms + (i + 1) * 900_000 - 1, "100", 10, "40", "40", "0"]
        for i in range(48)
    ]
    calls_12h = []
    orig_fetch = agy._fetch_rest_klines
    try:
        def _mock_f12(url, sym, interval, limit=3, timeout=10):
            calls_12h.append((url, sym))
            return sym, mock_12h
        agy._fetch_rest_klines = _mock_f12
        # Without check_12h_gap: 8h has 32/32 bars -> no patch needed
        p0 = agy._patch_shortlist_15m_from_binance(
            m15_8h, sp15_8h, ["BC4USDT"], "2026-11-24 15:45:00", now_u,
            window_start_str="2026-11-24 12:00:00", spot_live_syms={"BC4USDT"}, check_12h_gap=False,
        )
        assert p0 == 0 and calls_12h == []
        # With check_12h_gap=True (4H window): 12h only has 32 < 36 bars -> MUST patch both perp & spot!
        p1 = agy._patch_shortlist_15m_from_binance(
            m15_8h, sp15_8h, ["BC4USDT"], "2026-11-24 15:45:00", now_u,
            window_start_str="2026-11-24 12:00:00", spot_live_syms={"BC4USDT"}, check_12h_gap=True,
        )
        assert p1 == 1 and len(m15_8h["BC4USDT"][0]) == 48 and len(sp15_8h["BC4USDT"]) == 48
        assert len(calls_12h) == 2
    finally:
        agy._fetch_rest_klines = orig_fetch

    # 3. _evaluate_open_positions auto-refreshes stale ema20_1h_map (>90m) so cmd_guard catches B1H >= 85% squeeze
    orig_pm = agy.POS_META_PATH
    orig_ref = agy.BTC_REF_PATH
    orig_f15 = agy.fetch_klines_15m
    orig_f15r = agy.fetch_klines_15m_raw
    orig_marks = agy.fetch_mark_prices
    orig_refr = agy._refresh_b1h_ema_ref_from_ch
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.BTC_REF_PATH = os.path.join(td, "btc_ref.json")
            agy.write_json_atomic(agy.POS_META_PATH, {
                "bto-2026-11-24-BBB": {
                    "symbol": "BBBUSDT", "side": "SHORT", "setup": "SP_LEAD_2H", "exit_mode": "V14_AA_SQZ96",
                    "stop_pct": "0.050", "tp_pct": "0.600", "ratchet_stage": "INIT", "last_bar_ms": 9999999999999,
                }
            })
            # Stale ema20_1h_at (3 hours old)
            agy.write_json_atomic(agy.BTC_REF_PATH, {
                "ema20_1h_map": {f"C{i}USDT": "1.0" for i in range(25)},
                "ema20_1h_at": "2026-11-24T12:00:00+00:00",
            })
            refreshed_flag = []

            def _mock_refresh(nu):
                refreshed_flag.append(True)
                return {
                    "ema20_1h_map": {f"C{i}USDT": "1.0" for i in range(25)},
                    "ema20_1h_at": nu.isoformat(timespec="seconds"),
                }

            agy._refresh_b1h_ema_ref_from_ch = _mock_refresh
            agy.fetch_klines_15m = lambda sym, limit=400: []
            agy.fetch_klines_15m_raw = lambda sym, limit=400: ([], [])
            marks_sqz = {"BTCUSDT": Decimal("60000"), "BBBUSDT": Decimal("1.02")}
            for i in range(25):
                marks_sqz[f"C{i}USDT"] = Decimal("1.05")  # 100% > EMA20 -> B1H squeeze!
            agy.fetch_mark_prices = lambda: marks_sqz
            pf_ev = {
                "shorts": [{
                    "id": "bto-2026-11-24-BBB", "symbol": "BBBUSDT", "coin": "BBB", "side": "SHORT",
                    "entry": "1.00", "mark": "1.02", "on_binance": True,
                    "opened_at": "2026-11-24T15:00:00+00:00",
                }]
            }
            evs = agy._evaluate_open_positions(pf_ev, now_u)
            assert refreshed_flag == [True], "Expected stale ema20_1h_map refresh inside _evaluate_open_positions"
            assert len(evs) == 1 and evs[0]["action"] == "MUST_CLOSE"
            assert "B1H_SQUEEZE_CUT_MARK" in (evs[0]["exit_reason"] or ""), evs[0]
    finally:
        agy.POS_META_PATH = orig_pm
        agy.BTC_REF_PATH = orig_ref
        agy.fetch_klines_15m = orig_f15
        agy.fetch_klines_15m_raw = orig_f15r
        agy.fetch_mark_prices = orig_marks
        agy._refresh_b1h_ema_ref_from_ch = orig_refr



def test_v17_basimp_blowoff2h_btc15_sqz():
    import contextlib
    import io
    import json
    import tempfile

    # 1. Version, exit mode, BTC15 constant, and Hard R0 rails preserved
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.DEFAULT_EXIT_MODE == "V18_AA_SQZ96"
    assert agy.NEW7_EXIT_MODE == "V21_NEW7_SQZ96"
    assert agy.V17_AA_SQZ_BTC_15M == Decimal("0.018")
    assert agy.ALLOWED_SIDE == "SHORT" and agy.LOT_USDT == Decimal("200") and agy.MARGIN_USDT == Decimal("100") and agy.LEVERAGE == 2
    assert agy.MAX_OPEN == 0 and agy.DAILY_MAX_OPENS == 0
    assert agy.CB_AUTO_ENABLED is True and agy.CB_MAX_LOSS_USDT == Decimal("45.00") and agy.CB_MAX_LOSS_PCT == Decimal("0.05")

    # 2. compute_spot_basis_stats computes exact bas1h (4-bar mean, >=2 valid, None when <2)
    base_dt = dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc)
    t_32 = [(base_dt + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(32)]
    cl_32 = [0.9996] * 28 + [1.0010] * 4
    smap_32 = {ts: (1.0, 50_000.0, 20_000.0) for ts in t_32}
    st_b = agy.compute_spot_basis_stats(t_32, cl_32, smap_32)
    assert st_b["bas1h"] is not None and abs(st_b["bas1h"] - 0.0010) < 1e-9, st_b
    expected_bas8h = (28 * (-0.0004) + 4 * 0.0010) / 32.0
    assert st_b["bas8h"] is not None and abs(st_b["bas8h"] - expected_bas8h) < 1e-9, st_b
    smap_sparse = {ts: (1.0, 50_000.0, 20_000.0) for ts in t_32[:-3]}
    st_sp = agy.compute_spot_basis_stats(t_32, cl_32, smap_sparse)
    assert st_sp["bas1h"] is None and st_sp["bas8h"] is not None, st_sp

    # 3. Priority #20 BASIMP_1H: positive basis-impulse fresh breakdown (even when bas8h slightly negative down to -0.05%)
    basimp_base = dict(
        age_days=160.0, breadth_1d=0.35, btc_ret7d=-0.03, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.985, o_1h=1.002, hi_1h=1.005, lo_1h=0.982, c_1h=0.985, c_1h_prev=1.003,
        qv_1h=1_000_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.39,
        ema10_1h=0.995, ema10_1h_prev=0.998,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.02, ret_12h=-0.03, ret_24h=-0.04, ret_7d=-0.08,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True,
        bas1h=0.0006, bas8h=-0.0003, ps_vrat=1.20,
        spot_1h_complete=True, spot_tb_1h=0.33, spot_vrat_1h=1.35, spot_tsize_r_1h=1.00,
        is_fresh_2h_window=False, bar_complete_2h=False,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_bi = agy.evaluate_v17_short_setups(**basimp_base)
    assert [h["setup"] for h in hits_bi] == ["BASIMP_1H"], hits_bi
    assert hits_bi[0]["stop_pct"] == 0.050 and hits_bi[0]["tp_pct"] == 0.600 and hits_bi[0]["exit_mode"] == "V18_AA_SQZ96"
    assert hits_bi[0]["score"] == round(221.0 + (0.45 - 0.33) * 5.0, 2) and hits_bi[0]["score"] < 225.0
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "bas8h": -0.0006}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "bas1h": 0.0002}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "bas1h": 0.0003, "bas8h": -0.0001}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "c_1h_prev": 0.990}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "spot_tb_1h": 0.38}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "tb_1h": 0.43}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "spot_vrat_1h": 1.19}) == []
    assert agy.evaluate_v17_short_setups(**{**basimp_base, "has_spot_24h": False}) == []

    # 4. Priority #19 BLOWOFF_2H: 2H 2-bar post-squeeze blow-off reversal (Spot-backed AND Perp-Only!)
    blow_base = dict(
        age_days=120.0, breadth_1d=0.30, btc_ret7d=-0.02, bar_complete_1h=True, bars_1h_count=200,
        cur_px=1.015, o_1h=1.010, hi_1h=1.020, lo_1h=1.005, c_1h=1.015, c_1h_prev=1.010,
        qv_1h=900_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.52, ema10_1h=1.020, ema10_1h_prev=1.020,
        atr14_1h_pct=0.020, hi24=1.08, hi7d=1.15, ret_4h=0.01, ret_12h=-0.02, ret_24h=-0.03, ret_7d=-0.09,
        d_sma20=1.10, f_day=0.0001, has_spot_24h=False, bas8h=None, ps_vrat=None,
        is_fresh_2h_window=True, bar_complete_2h=True,
        o_2h_prev=1.000, c_2h_prev=1.060, qv_2h_prev=2_000_000.0, qv_avg20_2h_prev=1_000_000.0,
        o_2h=1.055, hi_2h=1.060, lo_2h=1.010, c_2h=1.015,
        tb_2h=0.38, ema10_2h=1.030, ema10_2h_prev=1.025,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_bl = agy.evaluate_v17_short_setups(**blow_base)
    assert [h["setup"] for h in hits_bl] == ["BLOWOFF_2H"], hits_bl
    assert hits_bl[0]["stop_pct"] == 0.055 and hits_bl[0]["tp_pct"] == 0.600 and hits_bl[0]["exit_mode"] == "V18_AA_SQZ96"
    assert hits_bl[0]["score"] == round(223.8 + 0.06 * 10.0, 2) and hits_bl[0]["score"] < 225.0
    hits_bl_sp = agy.evaluate_v17_short_setups(**{**blow_base, "has_spot_24h": True, "bas8h": -0.0004})
    assert [h["setup"] for h in hits_bl_sp] == ["BLOWOFF_2H"], hits_bl_sp
    assert agy.evaluate_v17_short_setups(**{**blow_base, "has_spot_24h": True, "bas8h": -0.0008}) == []
    assert agy.evaluate_v17_short_setups(**{**blow_base, "is_fresh_2h_window": False}) == []
    assert agy.evaluate_v17_short_setups(**{**blow_base, "c_2h": 1.031, "cur_px": 1.031}) == []
    assert agy.evaluate_v17_short_setups(**{**blow_base, "c_2h_prev": 1.044}) == []
    assert agy.evaluate_v17_short_setups(**{**blow_base, "qv_2h_prev": 1_400_000.0}) == []
    assert agy.evaluate_v17_short_setups(**{**blow_base, "d_sma20": 1.010}) == []
    assert agy.evaluate_v17_short_setups(**{**blow_base, "tb_2h": 0.42}) == []

    # 5. V17_AA_SQZ96 BTC 15m flash-squeeze (+1.8%/15m) cuts INIT & HALF50 losers, skips winners/LOCK8/V14 modes
    t0 = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
    t0_ms = int(t0.timestamp() * 1000)
    btc_bars = []
    for i in range(18):
        ts = t0_ms + i * 900_000
        c_val = "98.0" if i == 16 else "100.0"
        btc_bars.append((ts, Decimal(c_val), Decimal(c_val), Decimal(c_val)))
    now_bar17 = t0 + dt.timedelta(minutes=18 * 15 + 1)
    btc_map, _ = agy._btc_squeeze_returns_from_bars(btc_bars, now_bar17)
    pos_bars_loss = [(t0_ms + 17 * 900_000, Decimal("100.9"), Decimal("100.2"), Decimal("100.8"))]
    pos_bars_win = [(t0_ms + 17 * 900_000, Decimal("99.8"), Decimal("98.5"), Decimal("99.0"))]

    opened_t = (t0 + dt.timedelta(minutes=15 * 15)).isoformat()
    ev_v17_init = agy.evaluate_position_exit(
        {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_bars,
    )
    assert ev_v17_init["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in (ev_v17_init["exit_reason"] or ""), ev_v17_init

    ev_v17_half = agy.evaluate_position_exit(
        {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "101.0", "ratchet_stage": "HALF50", "mfe_pct": "0.052", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_bars,
    )
    assert ev_v17_half["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in (ev_v17_half["exit_reason"] or ""), ev_v17_half

    # Live mark BTC_SQUEEZE_CUT_MARK when no new 15m bar yet but live BTC mark rips >= +1.8% vs latest completed 15m close (98.0 -> 99.9)
    now_mark = t0 + dt.timedelta(minutes=17 * 15 + 3)
    ev_v17_mark = agy.evaluate_position_exit(
        {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), [], opened_t, now_mark, btc_bars_15m=btc_bars[:17], btc_mark_px=Decimal("99.9"),
    )
    assert ev_v17_mark["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_MARK" in (ev_v17_mark["exit_reason"] or ""), ev_v17_mark

    ev_v17_win = agy.evaluate_position_exit(
        {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("99.0"), pos_bars_win, opened_t, now_bar17, btc_bars_15m=btc_bars,
    )
    assert ev_v17_win["action"] == "HOLD", ev_v17_win

    pos_bars_lock8 = [(t0_ms + 17 * 900_000, Decimal("97.3"), Decimal("96.5"), Decimal("97.0"))]
    ev_v17_lock8 = agy.evaluate_position_exit(
        {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "97.5", "ratchet_stage": "LOCK8", "mfe_pct": "0.085", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("97.0"), pos_bars_lock8, opened_t, now_bar17, btc_bars_15m=btc_bars,
    )
    assert ev_v17_lock8["action"] == "HOLD", ev_v17_lock8

    for legacy_em in ("V14_AA_SQZ96", "V14_NEW7_SQZ96"):
        ev_leg = agy.evaluate_position_exit(
            {"exit_mode": legacy_em, "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
            Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_bars,
        )
        assert ev_leg["action"] == "HOLD", (legacy_em, ev_leg)

    # 6. check_fast_guard_events catches live 5s BTC 15m flash-squeeze for V17_AA_SQZ96 (INIT & HALF50) only
    now_fg = t0 + dt.timedelta(minutes=17 * 15 + 3)
    _, btc_ref_fg = agy._btc_squeeze_returns_from_bars(btc_bars[:17], now_fg)
    pf_fg = {
        "shorts": [
            {"id": "v17_init", "symbol": "A1USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "v17_half", "symbol": "A2USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "v17_win",  "symbol": "A3USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "v14_init", "symbol": "A4USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
        ]
    }
    pm_fg = {
        "v17_init": {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT"},
        "v17_half": {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "101.0", "ratchet_stage": "HALF50"},
        "v17_win":  {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT"},
        "v14_init": {"exit_mode": "V14_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT"},
    }
    marks_fg = {"BTCUSDT": Decimal("99.9"), "A1USDT": Decimal("100.5"), "A2USDT": Decimal("100.5"), "A3USDT": Decimal("99.2"), "A4USDT": Decimal("100.5")}
    br_fg, _ = agy.check_fast_guard_events(pm_fg, pf_fg, marks_fg, now_fg, btc_ref=btc_ref_fg)
    br_ids = {b["id"] for b in br_fg}
    assert br_ids == {"v17_init", "v17_half"}, br_fg
    assert all("FAST_BTC_SQUEEZE_CUT" in b["reason"] for b in br_fg), br_fg

    # 7. _btc_ref_is_fresh triggers refresh right after next 15m close (+15s) when age >= 30s, throttles < 30s
    assert agy._btc_ref_is_fresh(btc_ref_fg, now_fg + dt.timedelta(seconds=10)) is True
    t_next_bar_done = t0 + dt.timedelta(minutes=18 * 15, seconds=20)
    ref_old_bar = {"at": (t_next_bar_done - dt.timedelta(seconds=60)).isoformat(), "last_bar_ms": t0_ms + 16 * 900_000}
    assert agy._btc_ref_is_fresh(ref_old_bar, t_next_bar_done) is False
    ref_just_fetched = {"at": (t_next_bar_done - dt.timedelta(seconds=10)).isoformat(), "last_bar_ms": t0_ms + 16 * 900_000}
    assert agy._btc_ref_is_fresh(ref_just_fetched, t_next_bar_done) is True

    # 8. CLI open --exit-mode V17_AA_SQZ96 accepted
    seen_em = []
    orig_cmd_open = agy.cmd_open
    try:
        def _cap_open(args):
            seen_em.append(args.exit_mode)
            return 0
        agy.cmd_open = _cap_open
        assert agy.main(["open", "--symbol", "ETHUSDT", "--stop-pct", "0.050", "--tp-pct", "0.60", "--reason", "t", "--exit-mode", "V17_AA_SQZ96"]) == 0
        assert seen_em == ["V17_AA_SQZ96"]
    finally:
        agy.cmd_open = orig_cmd_open

    # 9. cmd_auto_trade transient scan-exception 1-time retry succeeds when 1st call fails at minute < 5
    now_r = dt.datetime(2026, 11, 24, 14, 0, 5, tzinfo=dt.timezone.utc)
    orig_lock = agy.LOCK_PATH
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_jn = agy.JOURNAL_PATH
    orig_pf = agy.binance.PORTFOLIO
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_now = agy.utcnow
    orig_sleep = agy.time.sleep
    orig_wrr = agy._write_round_report
    orig_chw = agy.wait_for_clickhouse_15m
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "agy-guard.lock")
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.STATE_PATH = os.path.join(td, "st.json")
            agy.JOURNAL_PATH = os.path.join(td, "jn.jsonl")
            agy.binance.PORTFOLIO = os.path.join(td, "pf.json")
            agy.write_json_atomic(agy.POS_META_PATH, {})
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900", "peak_equity_usdt": "900", "circuit_breaker_tripped": False})
            pf_m = {"equity_usdt": "900.00", "available_usdt": "900.00", "held_symbols": [], "shorts": [], "orders": [], "closed": []}
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf_m)
            agy._sync_and_portfolio = lambda nu, do_sync=True: (pf_m, {"status": "ok"}, {}, {"tripped": False})
            agy._evaluate_open_positions = lambda pf, nu, bars_out=None, flow_out=None: []
            agy._write_round_report = lambda *a, **k: None
            agy.wait_for_clickhouse_15m = lambda nu, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0, "target_15m": "2026-11-24 00:00:00"}
            agy.utcnow = lambda: now_r
            sleeps = []
            agy.time.sleep = lambda s: sleeps.append(s)
            calls = {"n": 0}
            def _flaky_scan(pf, nu, held_bars_15m=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("transient CH timeout")
                return (
                    {"regime_label": "BEARISH_FAVORABLE", "funding_ok": True, "complete_1h_pct": 100.0, "complete_15m_pct": 100.0, "btc": {"price": 60000}, "breadth": {"pct_above_1d_sma20": 20.0, "pct_above_1h_ema20": 20.0}},
                    [], [], [], {},
                )
            agy._scan_market_and_candidates = _flaky_scan
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = agy.cmd_auto_trade(max_opens=3, dry_run=False)
            assert rc == 0 and calls["n"] == 2 and sleeps == [5], (rc, calls, sleeps)
            out_j = json.loads(buf.getvalue())
            assert out_j["ok"] is True and out_j["strategy"] == "V23_EMA_RSI_MOOD"
    finally:
        agy.LOCK_PATH = orig_lock
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.JOURNAL_PATH = orig_jn
        agy.binance.PORTFOLIO = orig_pf
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.utcnow = orig_now
        agy.time.sleep = orig_sleep
        agy._write_round_report = orig_wrr
        agy.wait_for_clickhouse_15m = orig_chw


def test_v18_basfomo_eth15_sqz_and_parity():
    # 1. Version, exit mode, ETH15 constant, per-engine cap, and Hard R0 rails preserved
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.DEFAULT_EXIT_MODE == "V18_AA_SQZ96"
    assert agy.NEW7_EXIT_MODE == "V21_NEW7_SQZ96"
    assert agy.V18_AA_SQZ_ETH_15M == Decimal("0.020")
    assert agy.ENGINE_MAX_OPEN == {}
    assert agy.ALLOWED_SIDE == "SHORT" and agy.LOT_USDT == Decimal("200") and agy.MARGIN_USDT == Decimal("100") and agy.LEVERAGE == 2
    assert agy.MAX_OPEN == 0 and agy.DAILY_MAX_OPENS == 0
    assert agy.CB_AUTO_ENABLED is True and agy.CB_MAX_LOSS_USDT == Decimal("45.00") and agy.CB_MAX_LOSS_PCT == Decimal("0.05")

    # 2. compute_spot_basis_stats computes exact bas15 at end_ts_str (and None when end spot close missing)
    base_dt = dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc)
    t_32 = [(base_dt + dt.timedelta(minutes=15 * i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(32)]
    cl_32 = [0.9996] * 28 + [1.0005, 1.0006, 1.0007, 1.0012]
    smap_32 = {ts: (1.0, 50_000.0, 20_000.0) for ts in t_32}
    st_b = agy.compute_spot_basis_stats(t_32, cl_32, smap_32)
    assert st_b["bas15"] is not None and abs(st_b["bas15"] - 0.0012) < 1e-9, st_b
    assert st_b["bas1h"] is not None and abs(st_b["bas1h"] - 0.00075) < 1e-9, st_b
    assert st_b["bas15"] > st_b["bas1h"] > st_b["bas8h"] + 0.0003, st_b
    smap_no_last = {ts: (1.0, 50_000.0, 20_000.0) for ts in t_32[:-1]}
    st_nl = agy.compute_spot_basis_stats(t_32, cl_32, smap_no_last)
    assert st_nl["bas15"] is None and st_nl["bas1h"] is not None, st_nl

    # 3. Priority #21 BASFOMO_1H: 3-step accelerating retail Perp FOMO into Spot distribution continuation
    basfomo_base = dict(
        age_days=160.0, breadth_1d=0.36, btc_ret7d=-0.03, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.985, o_1h=0.994, hi_1h=0.995, lo_1h=0.983, c_1h=0.985, c_1h_prev=0.994,
        qv_1h=1_000_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.39,
        ema10_1h=0.998, ema10_1h_prev=1.002,
        atr14_1h_pct=0.018, hi24=1.05, hi7d=1.10, ret_4h=-0.02, ret_12h=-0.03, ret_24h=-0.04, ret_7d=-0.08,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=True,
        bas15=0.0007, bas1h=0.0004, bas8h=-0.0002, ps_vrat=1.20,
        spot_1h_complete=True, spot_tb_1h=0.32, spot_vrat_1h=1.60, spot_tsize_r_1h=1.00,
        is_fresh_2h_window=False, bar_complete_2h=False,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_bf = agy.evaluate_v18_short_setups(**basfomo_base)
    assert [h["setup"] for h in hits_bf] == ["BASFOMO_1H"], hits_bf
    assert hits_bf[0]["stop_pct"] == 0.050 and hits_bf[0]["tp_pct"] == 0.600 and hits_bf[0]["exit_mode"] == "V18_AA_SQZ96"
    assert hits_bf[0]["score"] == round(220.8 + (0.45 - 0.32) * 5.0, 2) and hits_bf[0]["score"] < 221.5
    # Boundary rejections
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "bas15": 0.00029}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "bas15": 0.0004, "bas1h": 0.0004}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "bas1h": 0.00005, "bas8h": -0.0002}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "bas8h": -0.0006}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "c_1h": 0.950, "lo_1h": 0.949, "cur_px": 0.950}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "spot_tb_1h": 0.36}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "tb_1h": 0.42}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "spot_vrat_1h": 1.49}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "has_spot_24h": False}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "breadth_1d": 0.46}) == []
    assert agy.evaluate_v18_short_setups(**{**basfomo_base, "bas15": None}) == []

    # 4. V18_AA_SQZ96 ETH 15m flash-squeeze (+2.0%/15m) cuts INIT & HALF50 losers even when BTC is flat; skips winners/LOCK8/V17/V14
    t0 = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.timezone.utc)
    t0_ms = int(t0.timestamp() * 1000)
    btc_flat = [(t0_ms + i * 900_000, Decimal("100.0"), Decimal("100.0"), Decimal("100.0")) for i in range(18)]
    eth_rip = []
    for i in range(18):
        ts = t0_ms + i * 900_000
        c_val = "100.0" if i < 17 else "102.1"
        eth_rip.append((ts, Decimal(c_val), Decimal(c_val), Decimal(c_val)))
    now_bar17 = t0 + dt.timedelta(minutes=18 * 15 + 1)
    pos_bars_loss = [(t0_ms + 17 * 900_000, Decimal("100.9"), Decimal("100.2"), Decimal("100.8"))]
    pos_bars_win = [(t0_ms + 17 * 900_000, Decimal("99.8"), Decimal("98.5"), Decimal("99.0"))]
    opened_t = (t0 + dt.timedelta(minutes=15 * 15)).isoformat()

    ev_v18_init = agy.evaluate_position_exit(
        {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_flat, eth_bars_15m=eth_rip,
    )
    assert ev_v18_init["action"] == "MUST_CLOSE" and "ETH_SQUEEZE_CUT_15M" in (ev_v18_init["exit_reason"] or ""), ev_v18_init

    ev_v18_half = agy.evaluate_position_exit(
        {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "101.0", "ratchet_stage": "HALF50", "mfe_pct": "0.052", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_flat, eth_bars_15m=eth_rip,
    )
    assert ev_v18_half["action"] == "MUST_CLOSE" and "ETH_SQUEEZE_CUT_15M" in (ev_v18_half["exit_reason"] or ""), ev_v18_half

    # Live mark ETH_SQUEEZE_CUT_MARK when no new completed 15m bar yet but live ETH mark rips >= +2.0% vs latest completed close (100.0 -> 102.1)
    now_mark = t0 + dt.timedelta(minutes=17 * 15 + 3)
    ev_v18_mark = agy.evaluate_position_exit(
        {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), [], opened_t, now_mark, btc_bars_15m=btc_flat[:17], btc_mark_px=Decimal("100.0"),
        eth_bars_15m=eth_rip[:17], eth_mark_px=Decimal("102.1"),
    )
    assert ev_v18_mark["action"] == "MUST_CLOSE" and "ETH_SQUEEZE_CUT_MARK" in (ev_v18_mark["exit_reason"] or ""), ev_v18_mark

    # Winner and LOCK8 protected profit MUST HOLD
    ev_v18_win = agy.evaluate_position_exit(
        {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("99.0"), pos_bars_win, opened_t, now_bar17, btc_bars_15m=btc_flat, eth_bars_15m=eth_rip,
    )
    assert ev_v18_win["action"] == "HOLD", ev_v18_win

    pos_bars_lock8 = [(t0_ms + 17 * 900_000, Decimal("97.3"), Decimal("96.5"), Decimal("97.0"))]
    ev_v18_lock8 = agy.evaluate_position_exit(
        {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "97.5", "ratchet_stage": "LOCK8", "mfe_pct": "0.085", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("97.0"), pos_bars_lock8, opened_t, now_bar17, btc_bars_15m=btc_flat, eth_bars_15m=eth_rip,
    )
    assert ev_v18_lock8["action"] == "HOLD", ev_v18_lock8

    # Legacy V17/V14/NEW7 modes MUST NOT cut on ETH-only 15m squeeze
    for legacy_em in ("V17_AA_SQZ96", "V14_AA_SQZ96", "V14_NEW7_SQZ96"):
        ev_leg = agy.evaluate_position_exit(
            {"exit_mode": legacy_em, "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
            Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_flat, eth_bars_15m=eth_rip,
        )
        assert ev_leg["action"] == "HOLD", (legacy_em, ev_leg)

    # V18_AA_SQZ96 also inherits V17 BTC 15m flash-squeeze (+1.8%/15m)
    btc_rip = [(t0_ms + i * 900_000, Decimal("98.0" if i == 16 else "100.0"), Decimal("98.0" if i == 16 else "100.0"), Decimal("98.0" if i == 16 else "100.0")) for i in range(18)]
    ev_v18_btc = agy.evaluate_position_exit(
        {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT", "last_bar_ms": t0_ms + 16 * 900_000},
        Decimal("100.0"), Decimal("100.8"), pos_bars_loss, opened_t, now_bar17, btc_bars_15m=btc_rip, eth_bars_15m=btc_flat,
    )
    assert ev_v18_btc["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in (ev_v18_btc["exit_reason"] or ""), ev_v18_btc

    # 5. check_fast_guard_events catches live 5s ETH 15m flash-squeeze for V18_AA_SQZ96 (INIT & HALF50) only
    now_fg = t0 + dt.timedelta(minutes=17 * 15 + 3)
    _, btc_ref_fg = agy._btc_squeeze_returns_from_bars(btc_flat[:17], now_fg)
    _, eth_ref_fg = agy._eth_15m_ref_from_bars(eth_rip[:17], now_fg)
    merged_ref = {**btc_ref_fg, **eth_ref_fg}
    pf_fg = {
        "shorts": [
            {"id": "v18_init", "symbol": "A1USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "v18_half", "symbol": "A2USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "v18_win",  "symbol": "A3USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "v17_init", "symbol": "A4USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
            {"id": "n7_init",  "symbol": "A5USDT", "entry": "100.0", "on_binance": True, "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat()},
        ]
    }
    pm_fg = {
        "v18_init": {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT"},
        "v18_half": {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "stop_px": "101.0", "ratchet_stage": "HALF50"},
        "v18_win":  {"exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT"},
        "v17_init": {"exit_mode": "V17_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.60", "ratchet_stage": "INIT"},
        "n7_init":  {"exit_mode": "V14_NEW7_SQZ96", "stop_pct": "0.075", "tp_pct": "0.60", "ratchet_stage": "INIT"},
    }
    marks_fg = {
        "BTCUSDT": Decimal("100.0"), "ETHUSDT": Decimal("102.1"),
        "A1USDT": Decimal("100.5"), "A2USDT": Decimal("100.5"), "A3USDT": Decimal("99.2"),
        "A4USDT": Decimal("100.5"), "A5USDT": Decimal("100.5"),
    }
    br_fg, _ = agy.check_fast_guard_events(pm_fg, pf_fg, marks_fg, now_fg, btc_ref=merged_ref)
    assert {b["id"] for b in br_fg} == {"v18_init", "v18_half"}, br_fg
    assert all("FAST_ETH_SQUEEZE_CUT" in b["reason"] for b in br_fg), br_fg

    # 6. _eth_ref_is_fresh triggers refresh right after next 15m close (+15s) when age >= 30s, throttles < 30s
    assert agy._eth_ref_is_fresh(merged_ref, now_fg + dt.timedelta(seconds=10)) is True
    assert agy._eth_ref_is_fresh(btc_ref_fg, now_fg) is False
    t_next_bar_done = t0 + dt.timedelta(minutes=18 * 15, seconds=20)
    ref_old_eth = {
        "eth_at": (t_next_bar_done - dt.timedelta(seconds=60)).isoformat(),
        "eth_last_bar_ms": t0_ms + 16 * 900_000,
        "eth_closes_15m": {str(t0_ms + 16 * 900_000): "100.0"},
    }
    assert agy._eth_ref_is_fresh(ref_old_eth, t_next_bar_done) is False
    ref_just_eth = {
        "eth_at": (t_next_bar_done - dt.timedelta(seconds=10)).isoformat(),
        "eth_last_bar_ms": t0_ms + 16 * 900_000,
        "eth_closes_15m": {str(t0_ms + 16 * 900_000): "100.0"},
    }
    assert agy._eth_ref_is_fresh(ref_just_eth, t_next_bar_done) is True

    # 7. CLI open --exit-mode V18_AA_SQZ96 (and default) accepted
    seen_em = []
    orig_cmd_open = agy.cmd_open
    try:
        def _cap_open(args):
            seen_em.append((args.setup, args.exit_mode))
            return 0
        agy.cmd_open = _cap_open
        assert agy.main(["open", "--symbol", "ETHUSDT", "--stop-pct", "0.050", "--tp-pct", "0.60", "--reason", "t", "--exit-mode", "V18_AA_SQZ96"]) == 0
        assert agy.main(["open", "--symbol", "ETHUSDT", "--stop-pct", "0.050", "--tp-pct", "0.60", "--reason", "t"]) == 0
        assert seen_em == [("V22", "V18_AA_SQZ96"), ("V22", "V18_AA_SQZ96")], seen_em
    finally:
        agy.cmd_open = orig_cmd_open



def test_v19_emarej2h_and_parity():
    # 1. Version, exit mode, per-engine cap, constants, and Hard R0 rails preserved
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.DEFAULT_EXIT_MODE == "V18_AA_SQZ96"
    assert agy.NEW7_EXIT_MODE == "V21_NEW7_SQZ96"
    assert agy.ENGINE_MAX_OPEN == {}
    assert agy.MAX_OPEN_HEAT_USDT == Decimal("0")
    assert agy.EMAREJ2H_BD_MAX == 0.45 and agy.EMAREJ2H_KISS_RATIO == 0.997
    assert agy.EMAREJ2H_UWICK_MIN == 0.25 and agy.EMAREJ2H_LWICK_MAX == 0.25
    assert agy.EMAREJ2H_TB_MAX == 0.42 and agy.EMAREJ2H_VRAT_MIN == 1.3
    assert agy.ALLOWED_SIDE == "SHORT" and agy.LOT_USDT == Decimal("200") and agy.MARGIN_USDT == Decimal("100") and agy.LEVERAGE == 2
    assert agy.MAX_OPEN == 0 and agy.DAILY_MAX_OPENS == 0
    assert agy.CB_AUTO_ENABLED is True and agy.CB_MAX_LOSS_USDT == Decimal("45.00") and agy.CB_MAX_LOSS_PCT == Decimal("0.05")

    # 2. Priority #22 EMAREJ_2H triggers cleanly on Perp-Only (has_spot_24h=False, bas8h=None) & Deep-Bear (bas8h=-0.0004)
    # 2H bar: o=0.988, hi=1.000 (kisses ema10_2h=1.000), lo=0.974, c=0.978 -> rng=0.026, uwick=0.012/0.026=0.4615, lwick=0.004/0.026=0.1538
    emarej_base = dict(
        age_days=180.0, breadth_1d=0.18, btc_ret7d=-0.05, bar_complete_1h=True, bars_1h_count=200,
        cur_px=0.978, o_1h=0.984, hi_1h=0.986, lo_1h=0.976, c_1h=0.978, c_1h_prev=0.985,
        qv_1h=1_000_000.0, qv_avg20_1h=1_000_000.0, tb_1h=0.44,
        ema10_1h=0.995, ema10_1h_prev=0.996,
        atr14_1h_pct=0.018, hi24=1.06, hi7d=1.15, ret_4h=-0.02, ret_12h=-0.04, ret_24h=-0.06, ret_7d=-0.12,
        d_sma20=1.05, f_day=0.0001, has_spot_24h=False,
        bas15=None, bas1h=None, bas8h=None, ps_vrat=None,
        is_fresh_2h_window=True, bar_complete_2h=True,
        o_2h=0.988, hi_2h=1.000, lo_2h=0.974, c_2h=0.978, o_2h_prev=0.992, c_2h_prev=0.986,
        qv_2h=1_500_000.0, qv_avg20_2h=1_000_000.0, qv_2h_prev=1_000_000.0, qv_avg20_2h_prev=1_000_000.0,
        tb_2h=0.38, ema10_2h=1.000, ema10_2h_prev=1.005, ema20_2h=1.020,
        is_fresh_4h_window=False, bar_complete_4h=False,
    )
    hits_em = agy.evaluate_v19_short_setups(**emarej_base)
    assert [h["setup"] for h in hits_em] == ["EMAREJ_2H"], hits_em
    uw_exp = (1.000 - 0.988) / (1.000 - 0.974)
    assert hits_em[0]["score"] == round(220.6 + uw_exp * 0.5, 2) and hits_em[0]["score"] < 221.1, hits_em
    assert hits_em[0]["stop_pct"] == 0.050 and hits_em[0]["tp_pct"] == 0.600 and hits_em[0]["exit_mode"] == "V18_AA_SQZ96"

    # Deep-Bear clean basis (-0.04%) also passes
    hits_db = agy.evaluate_v19_short_setups(**{**emarej_base, "has_spot_24h": True, "bas8h": -0.0004})
    assert [h["setup"] for h in hits_db] == ["EMAREJ_2H"], hits_db

    # 3. Every boundary rejection condition
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "is_fresh_2h_window": False}) == []
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "bar_complete_2h": False}) == []
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "d_sma20": 0.970}) == []
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "ema20_2h": 0.995}) == []
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "ema20_2h": None}) == []
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "c_2h_prev": 1.005}) == []  # fresh cross must NOT fire EMAREJ_2H
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "hi_2h": 0.996}) == []      # < ema10_2h * 0.997
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "o_2h": 0.996, "hi_2h": 0.999, "lo_2h": 0.974, "c_2h": 0.978}) == []  # uwick=0.12 < 0.25
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "lo_2h": 0.960}) == []      # lwick=0.45 >= 0.25
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "tb_2h": 0.42}) == []       # tb_2h >= 0.42
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "qv_2h": 1_290_000.0}) == []  # vrat_2h=1.29 < 1.3
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "bas8h": -0.0006}) == []    # crowded short
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "breadth_1d": 0.46}) == []  # breadth > 0.45
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "cur_px": 0.945}) == []     # anti-chase (< c_2h * 0.97)
    assert agy.evaluate_v19_short_setups(**{**emarej_base, "f_day": -0.0013}) == []    # extreme negative funding (< -0.12%/d)

    # 4. Priority subordination: BASFOMO_1H outranks EMAREJ_2H
    both_bf = agy.evaluate_v19_short_setups(**{
        **emarej_base,
        "has_spot_24h": True, "bas15": 0.0007, "bas1h": 0.0004, "bas8h": -0.0002,
        "spot_1h_complete": True, "spot_tb_1h": 0.32, "spot_vrat_1h": 1.60, "tb_1h": 0.39,
    })
    assert [h["setup"] for h in both_bf] == ["BASFOMO_1H", "EMAREJ_2H"], both_bf
    assert both_bf[0]["score"] > both_bf[1]["score"]

    # 5. cmd_auto_trade enforces EMAREJ_2H engine cap = 2 (opens max 2 of 3 candidates) & formats uw2h/vr2h/tb2h tag
    import contextlib
    import io
    import json
    import tempfile
    orig_lock = agy.LOCK_PATH
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_jn = agy.JOURNAL_PATH
    orig_pf = agy.binance.PORTFOLIO
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_now = agy.utcnow
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "lock")
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.STATE_PATH = os.path.join(td, "st.json")
            agy.JOURNAL_PATH = os.path.join(td, "jn.jsonl")
            agy.binance.PORTFOLIO = os.path.join(td, "pf.json")
            t_now = dt.datetime(2026, 11, 25, 2, 0, 5, tzinfo=dt.timezone.utc)
            pf0 = {"equity_usdt": "900.00", "available_usdt": "900.00", "held_symbols": [], "shorts": [], "closed": []}
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf0)
            agy.write_json_atomic(agy.POS_META_PATH, {})
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900.00", "circuit_breaker_tripped": False})
            agy.utcnow = lambda: t_now
            agy._sync_and_portfolio = lambda now, do_sync=True: (pf0, {"ok": True}, {"baseline_equity_usdt": "900.00", "circuit_breaker_tripped": False}, {"tripped": False})
            agy._evaluate_open_positions = lambda pf, now, bars_out=None: []
            cands3 = [
                {
                    "symbol": f"C{i}USDT", "coin": f"C{i}", "primary_setup": "EMAREJ_2H", "score": 220.85 - i * 0.05,
                    "hit_specs": [{"setup": "EMAREJ_2H", "score": 220.85 - i * 0.05, "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": "V18_AA_SQZ96"}],
                    "suggested_stop_pct": 0.050, "suggested_tp_pct": 0.600, "exit_mode": "V18_AA_SQZ96",
                    "price": 1.0, "uwick_2h": 0.46, "vrat_2h": 1.55, "tb_2h": 0.36, "bas8h_pct": None,
                    "ret_24h_pct": -5.0, "ret_7d_pct": -12.0,
                }
                for i in range(3)
            ]
            agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None: (
                {"regime_label": "BEARISH_FAVORABLE", "funding_ok": True, "spot_ok": True, "complete_1h_pct": 100.0, "btc": {"price": 60000}, "breadth": {"pct_above_1d_sma20": 18.0, "pct_above_1h_ema20": 25.0}},
                cands3, [], [], {},
            )
            orig_ec19 = agy.ENGINE_MAX_OPEN
            orig_chw19 = agy.wait_for_clickhouse_15m
            agy.ENGINE_MAX_OPEN = {"EMAREJ_2H": 2}
            agy.wait_for_clickhouse_15m = lambda *a, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0}
            try:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = agy.cmd_auto_trade(max_opens=3, dry_run=True)
                assert rc == 0, rc
                out_j = json.loads(buf.getvalue())
                assert out_j["ok"] is True and out_j["strategy"] == "V23_EMA_RSI_MOOD"
                assert len(out_j["opened"]) == 2 and [x["symbol"] for x in out_j["opened"]] == ["C0USDT", "C1USDT"], out_j
                assert any(s["symbol"] == "C2USDT" and s["reason"] == "engine_cap_2" for s in out_j["skipped_risk"]), out_j
            finally:
                agy.ENGINE_MAX_OPEN = orig_ec19
                agy.wait_for_clickhouse_15m = orig_chw19
    finally:
        agy.LOCK_PATH = orig_lock
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.JOURNAL_PATH = orig_jn
        agy.binance.PORTFOLIO = orig_pf
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.utcnow = orig_now

def test_v20_bull_spotflow2h_and_parity():
    """Verify V20 23-engine BULL_SPOTFLOW_2H Neutral/Bull Overheat Moat, active_squeeze gate, and R0/cap parity."""
    # 1. Constants & Hard R0 Rails Lock
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.ALLOWED_SIDE == "SHORT"
    assert agy.MARGIN_USDT == Decimal("100")
    assert agy.LOT_USDT == Decimal("200")
    assert agy.LEVERAGE == 2
    assert agy.MAX_OPEN == 0
    assert agy.DAILY_MAX_OPENS == 0
    assert agy.CB_AUTO_ENABLED is True
    assert agy.CB_MAX_LOSS_USDT == Decimal("45.00")
    assert agy.CB_MAX_LOSS_PCT == Decimal("0.05")
    assert agy.MAX_OPEN_HEAT_USDT == Decimal("0")
    assert agy.ENGINE_MAX_OPEN == {}
    assert agy.BULL_REGIME_SETUPS == frozenset({"BULL_SPOTFLOW_2H"})
    assert agy.evaluate_v20_short_setups is agy.evaluate_v12_short_setups

    # 2. Positive trigger in Neutral/Bull (45% < BD <= 85%, b1h <= 72%, even with btc_ret7d >= +8%)
    bull_base = dict(
        age_days=180.0,
        breadth_1d=0.70,
        breadth_1h=0.58,
        btc_ret7d=0.11,
        bar_complete_1h=True,
        is_fresh_2h_window=True,
        bar_complete_2h=True,
        has_spot_24h=True,
        cur_px=0.975,
        o_2h=0.996,
        hi_2h=0.998,
        lo_2h=0.971,
        c_2h=0.976,
        c_2h_prev=0.985,
        ema10_2h=1.000,
        ema10_2h_prev=1.002,
        tb_2h=0.38,
        bas8h=0.0012,
        spot_2h_complete=True,
        spot_tb_2h=0.32,
        spot_vrat_2h=1.70,
        atr14_1h_pct=0.022,
        f_day=0.0004,
    )
    hits = agy.evaluate_v20_short_setups(**bull_base)
    assert [h["setup"] for h in hits] == ["BULL_SPOTFLOW_2H"], hits
    assert hits[0]["stop_pct"] == 0.055 and hits[0]["tp_pct"] == 0.600 and hits[0]["exit_mode"] == "V18_AA_SQZ96"
    assert round(220.4 + (0.45 - 0.32) * 5.0, 2) == hits[0]["score"]

    # Boundary BD=0.4501 and BD=0.85 pass; BD=0.45 and BD=0.851 fail
    assert [h["setup"] for h in agy.evaluate_v20_short_setups(**{**bull_base, "breadth_1d": 0.451})] == ["BULL_SPOTFLOW_2H"]
    assert [h["setup"] for h in agy.evaluate_v20_short_setups(**{**bull_base, "breadth_1d": 0.85})] == ["BULL_SPOTFLOW_2H"]
    assert "BULL_SPOTFLOW_2H" not in [h["setup"] for h in agy.evaluate_v20_short_setups(**{**bull_base, "breadth_1d": 0.45})]
    assert agy.evaluate_v20_short_setups(**{**bull_base, "breadth_1d": 0.851}) == []
    assert agy.evaluate_v20_short_setups(**{**bull_base, "breadth_1d": None}) == []

    # 3. Strict negative boundary guards
    assert agy.evaluate_v20_short_setups(**{**bull_base, "is_fresh_2h_window": False}) == []
    assert agy.evaluate_v20_short_setups(**{**bull_base, "bar_complete_2h": False}) == []
    assert agy.evaluate_v20_short_setups(**{**bull_base, "has_spot_24h": False}) == []
    assert agy.evaluate_v20_short_setups(**{**bull_base, "spot_2h_complete": False}) == []
    assert agy.evaluate_v20_short_setups(**{**bull_base, "breadth_1h": 0.721}) == []    # b1h > 72%
    assert agy.evaluate_v20_short_setups(**{**bull_base, "bas8h": 0.00099}) == []       # bas8h < +0.10%
    assert agy.evaluate_v20_short_setups(**{**bull_base, "spot_tb_2h": 0.36}) == []     # stb >= 0.36
    assert agy.evaluate_v20_short_setups(**{**bull_base, "tb_2h": 0.41}) == []          # ptb >= 0.41
    assert agy.evaluate_v20_short_setups(**{**bull_base, "spot_vrat_2h": 1.49}) == []   # svrat < 1.5
    assert agy.evaluate_v20_short_setups(**{**bull_base, "c_2h": 0.959, "cur_px": 0.958, "lo_2h": 0.955}) == []  # ext < -4%
    assert agy.evaluate_v20_short_setups(**{**bull_base, "lo_2h": 0.960}) == []         # lwick >= 0.28
    assert agy.evaluate_v20_short_setups(**{**bull_base, "cur_px": 0.945}) == []        # anti-chase (< c_2h * 0.97)
    assert agy.evaluate_v20_short_setups(**{**bull_base, "f_day": -0.0013}) == []       # extreme negative funding

    # 4. Active squeeze helper separates BTC/1H squeeze from calm bull breadth (>65%)
    assert agy.is_active_squeeze(0.01, 0.005, 0.01, 0.58, btc_15m_ret1=0.002) is False
    assert agy.is_active_squeeze(0.04, 0.005, 0.01, 0.40, btc_15m_ret1=0.002) is True
    assert agy.is_active_squeeze(0.01, 0.020, 0.01, 0.40, btc_15m_ret1=0.002) is True
    assert agy.is_active_squeeze(0.01, 0.005, 0.035, 0.40, btc_15m_ret1=0.002) is True
    assert agy.is_active_squeeze(0.01, 0.005, 0.01, 0.40, btc_15m_ret1=0.012) is True
    assert agy.is_active_squeeze(0.01, 0.005, 0.01, 0.721, btc_15m_ret1=0.002) is True

    # 5. cmd_auto_trade in BD=72% (BULLISH_SQUEEZE_CAUTION, active_squeeze=False) opens max 2 BULL_SPOTFLOW_2H,
    #    blocks any non-bull setup, and blocks ALL setups when active_squeeze=True
    import contextlib
    import io
    import json
    import tempfile
    orig_lock = agy.LOCK_PATH
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_jn = agy.JOURNAL_PATH
    orig_pf = agy.binance.PORTFOLIO
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_now = agy.utcnow
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "lock")
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.STATE_PATH = os.path.join(td, "st.json")
            agy.JOURNAL_PATH = os.path.join(td, "jn.jsonl")
            agy.binance.PORTFOLIO = os.path.join(td, "pf.json")
            t_now = dt.datetime(2026, 11, 25, 4, 0, 5, tzinfo=dt.timezone.utc)
            pf0 = {"equity_usdt": "900.00", "available_usdt": "900.00", "held_symbols": [], "shorts": [], "closed": []}
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf0)
            agy.write_json_atomic(agy.POS_META_PATH, {})
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900.00", "circuit_breaker_tripped": False})
            agy.utcnow = lambda: t_now
            agy._sync_and_portfolio = lambda now, do_sync=True: (pf0, {"ok": True}, {"baseline_equity_usdt": "900.00", "circuit_breaker_tripped": False}, {"tripped": False})
            agy._evaluate_open_positions = lambda pf, now, bars_out=None: []
            cands_mix = [
                {
                    "symbol": "BEAR0USDT", "coin": "BEAR0", "primary_setup": "WHALESP_1H", "score": 280.0,
                    "hit_specs": [{"setup": "WHALESP_1H", "score": 280.0, "stop_pct": 0.050, "tp_pct": 0.600, "exit_mode": "V18_AA_SQZ96"}],
                    "suggested_stop_pct": 0.050, "suggested_tp_pct": 0.600, "exit_mode": "V18_AA_SQZ96",
                    "price": 1.0, "bas8h_pct": 0.15, "ret_24h_pct": 4.0, "ret_7d_pct": 12.0,
                },
            ] + [
                {
                    "symbol": f"B{i}USDT", "coin": f"B{i}", "primary_setup": "BULL_SPOTFLOW_2H", "score": 221.1 - i * 0.05,
                    "hit_specs": [{"setup": "BULL_SPOTFLOW_2H", "score": 221.1 - i * 0.05, "stop_pct": 0.055, "tp_pct": 0.600, "exit_mode": "V18_AA_SQZ96"}],
                    "suggested_stop_pct": 0.055, "suggested_tp_pct": 0.600, "exit_mode": "V18_AA_SQZ96",
                    "price": 1.0, "spot_tb_2h": 0.31, "spot_vrat_2h": 1.85, "tb_2h": 0.37, "bas8h_pct": 0.14,
                    "ret_24h_pct": 5.0, "ret_7d_pct": 18.0,
                }
                for i in range(3)
            ]
            # Case A & B test regime gate + engine cap when explicitly enabled (default ENFORCE_REGIME_OPEN_GATE=False)
            orig_rg20 = agy.ENFORCE_REGIME_OPEN_GATE
            orig_ec20 = agy.ENGINE_MAX_OPEN
            orig_chw20 = agy.wait_for_clickhouse_15m
            agy.ENFORCE_REGIME_OPEN_GATE = True
            agy.ENGINE_MAX_OPEN = {"BULL_SPOTFLOW_2H": 2}
            agy.wait_for_clickhouse_15m = lambda *a, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0}
            try:
                agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None: (
                    {"regime_label": "BULLISH_SQUEEZE_CAUTION", "active_squeeze": False, "funding_ok": True, "spot_ok": True, "complete_1h_pct": 100.0, "btc": {"price": 65000}, "breadth": {"pct_above_1d_sma20": 72.0, "pct_above_1h_ema20": 55.0}},
                    cands_mix, [], [], {},
                )
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = agy.cmd_auto_trade(max_opens=3, dry_run=True)
                assert rc == 0, rc
                out_a = json.loads(buf.getvalue())
                assert out_a["ok"] is True and out_a["strategy"] == "V23_EMA_RSI_MOOD"
                assert [x["symbol"] for x in out_a["opened"]] == ["B0USDT", "B1USDT"], out_a
                assert any(s["symbol"] == "BEAR0USDT" and s["reason"] == "bull_breadth_gt65" for s in out_a["skipped_risk"]), out_a
                assert any(s["symbol"] == "B2USDT" and s["reason"] == "engine_cap_2" for s in out_a["skipped_risk"]), out_a

                # Case B: active_squeeze=True -> ZERO opens when ENFORCE_REGIME_OPEN_GATE=True
                agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None: (
                    {"regime_label": "BULLISH_SQUEEZE_CAUTION", "active_squeeze": True, "funding_ok": True, "spot_ok": True, "complete_1h_pct": 100.0, "btc": {"price": 67000}, "breadth": {"pct_above_1d_sma20": 72.0, "pct_above_1h_ema20": 76.0}},
                    cands_mix, [], [], {},
                )
                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc2 = agy.cmd_auto_trade(max_opens=3, dry_run=True)
                assert rc2 == 0, rc2
                out_b = json.loads(buf2.getvalue())
                assert out_b["opened"] == [], out_b
            finally:
                agy.ENFORCE_REGIME_OPEN_GATE = orig_rg20
                agy.ENGINE_MAX_OPEN = orig_ec20
                agy.wait_for_clickhouse_15m = orig_chw20
    finally:
        agy.LOCK_PATH = orig_lock
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.JOURNAL_PATH = orig_jn
        agy.binance.PORTFOLIO = orig_pf
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.utcnow = orig_now



def test_v21_spuw_n7sqz_and_bugfixes():
    """Verify V21 25-engine SP_UWICK_1H/2H, V21_NEW7_SQZ96 quint-squeeze, Spot OHLC aggregation, and 6 production fixes."""
    # 1. Constants & Strategy Metadata
    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.NEW7_EXIT_MODE == "V21_NEW7_SQZ96"
    assert agy.ENGINE_MAX_OPEN == {}
    assert agy.V21_NEW7_SQZ_BTC_15M == Decimal("0.022")
    assert agy.V21_NEW7_SQZ_ETH_15M == Decimal("0.025")
    assert agy.V21_NEW7_SQZ_B1H == Decimal("0.90")

    # 2. Spot OHLC 1H & 2H aggregation (7-tuple) + NULL tb_qv preservation + legacy 4-tuple compat
    base_dt = dt.datetime(2026, 11, 24, 0, 0, tzinfo=dt.timezone.utc)
    sp_map_1h = {}
    for hr in range(21):
        for q in range(4):
            t_s = (base_dt + dt.timedelta(hours=hr, minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S")
            if hr == 20:
                # Final 1H bar: open=100, high=106 (at q=1), low=96 (at q=3), close=98 -> red=True, uwick=(106-100)/(106-96)=0.60
                o_q = [100.0, 101.0, 103.0, 99.0][q]
                h_q = [102.0, 106.0, 103.5, 99.5][q]
                l_q = [99.5, 100.5, 98.5, 96.0][q]
                c_q = [101.0, 103.0, 99.0, 98.0][q]
                sp_map_1h[t_s] = (c_q, 40000.0, 14000.0, 200.0, o_q, h_q, l_q)
            else:
                sp_map_1h[t_s] = (100.0, 20000.0, 10000.0, 200.0, 100.0, 101.0, 99.0)
    f1 = agy._spot_15m_to_1h_flow(sp_map_1h, "2026-11-24 20:00:00")
    assert f1["spot_1h_complete"] is True
    assert f1["spot_red_1h"] is True
    assert abs(f1["spot_uwick_1h"] - 0.60) < 1e-6
    assert abs(f1["spot_tb_1h"] - 0.35) < 1e-6
    assert abs(f1["spot_vrat_1h"] - 2.0) < 1e-6

    # NULL taker_buy_quote_volume preserved as None (not spoofed as 0.0)
    sp_null = {k: (v[0], v[1], None, v[3], v[4], v[5], v[6]) for k, v in sp_map_1h.items()}
    f1_null = agy._spot_15m_to_1h_flow(sp_null, "2026-11-24 20:00:00")
    assert f1_null["spot_tb_1h"] is None and f1_null["spot_red_1h"] is True

    # Legacy 4-tuple map keeps backward compatibility (uwick=None, tb works)
    sp_leg = {k: v[:4] for k, v in sp_map_1h.items()}
    f1_leg = agy._spot_15m_to_1h_flow(sp_leg, "2026-11-24 20:00:00")
    assert f1_leg["spot_1h_complete"] is True and f1_leg["spot_uwick_1h"] is None and abs(f1_leg["spot_tb_1h"] - 0.35) < 1e-6

    # Spot OHLC 2H aggregation
    sp_map_2h = {}
    t2_0 = dt.datetime(2026, 11, 22, 0, 0, tzinfo=dt.timezone.utc)
    for b2 in range(21):
        for q in range(8):
            t_s = (t2_0 + dt.timedelta(hours=2 * b2, minutes=15 * q)).strftime("%Y-%m-%d %H:%M:%S")
            if b2 == 20:
                o_q = 100.0 if q == 0 else 99.0
                h_q = 105.0 if q == 2 else 101.0
                l_q = 95.0 if q == 7 else 98.0
                c_q = 97.0 if q == 7 else 99.0
                sp_map_2h[t_s] = (c_q, 30000.0, 10500.0, 150.0, o_q, h_q, l_q)
            else:
                sp_map_2h[t_s] = (100.0, 15000.0, 7500.0, 150.0, 100.0, 101.0, 99.0)
    f2 = agy._spot_15m_to_2h_flow(sp_map_2h, "2026-11-23 16:00:00")
    assert f2["spot_2h_complete"] is True and f2["spot_red_2h"] is True
    assert abs(f2["spot_uwick_2h"] - 0.50) < 1e-6 and abs(f2["spot_vrat_2h"] - 2.0) < 1e-6

    # 3. SP_UWICK_1H & SP_UWICK_2H setup evaluation + strict boundaries
    uw1_base = dict(
        age_days=120.0, breadth_1d=0.32, breadth_1h=0.30, btc_ret7d=-0.03,
        bar_complete_1h=True, bars_1h_count=80, cur_px=0.985,
        o_1h=1.01, hi_1h=1.02, lo_1h=0.98, c_1h=0.988, c_1h_prev=1.005,
        qv_1h=1_000_000.0, qv_avg20_1h=700_000.0, tb_1h=0.39,
        ema10_1h=0.995, ema10_1h_prev=1.000, ema20_1h=1.01, atr14_1h_pct=0.02,
        d_sma20=1.05, f_day=0.0, has_spot_24h=True, bas8h=-0.0003,
        spot_1h_complete=True, spot_tb_1h=0.37, spot_vrat_1h=1.45,
        spot_red_1h=True, spot_uwick_1h=0.35,
    )
    h_uw1 = agy.evaluate_v21_short_setups(**uw1_base)
    assert [h["setup"] for h in h_uw1] == ["SP_UWICK_1H"], h_uw1
    assert h_uw1[0]["stop_pct"] == 0.050 and h_uw1[0]["tp_pct"] == 0.600 and h_uw1[0]["exit_mode"] == "V18_AA_SQZ96"
    assert abs(h_uw1[0]["score"] - round(220.35 + 0.35, 2)) < 1e-6
    assert agy.evaluate_v21_short_setups(**{**uw1_base, "spot_red_1h": False}) == []
    assert agy.evaluate_v21_short_setups(**{**uw1_base, "spot_uwick_1h": 0.29}) == []
    assert agy.evaluate_v21_short_setups(**{**uw1_base, "bas8h": -0.0006}) == []
    assert agy.evaluate_v21_short_setups(**{**uw1_base, "c_1h_prev": 0.990}) == []  # requires fresh_1h_cross

    uw2_base = dict(
        age_days=120.0, breadth_1d=0.32, breadth_1h=0.30, btc_ret7d=-0.03,
        bar_complete_1h=False, cur_px=0.975, atr14_1h_pct=0.02, f_day=0.0,
        has_spot_24h=True, bas8h=-0.0004,
        is_fresh_2h_window=True, bar_complete_2h=True,
        o_2h=1.00, hi_2h=1.01, lo_2h=0.97, c_2h=0.978, c_2h_prev=0.990,
        tb_2h=0.40, ema10_2h=0.995, ema10_2h_prev=1.000, ema20_2h=1.02,
        spot_2h_complete=True, spot_tb_2h=0.37, spot_vrat_2h=1.40,
        spot_red_2h=True, spot_uwick_2h=0.34,
    )
    h_uw2 = agy.evaluate_v21_short_setups(**uw2_base)
    assert [h["setup"] for h in h_uw2] == ["SP_UWICK_2H"], h_uw2
    assert abs(h_uw2[0]["score"] - round(220.45 + 0.34, 2)) < 1e-6
    assert agy.evaluate_v21_short_setups(**{**uw2_base, "is_fresh_2h_window": False}) == []
    assert agy.evaluate_v21_short_setups(**{**uw2_base, "spot_uwick_2h": 0.29}) == []
    assert agy.evaluate_v21_short_setups(**{**uw2_base, "spot_red_2h": False}) == []

    # 4. V21_NEW7_SQZ96 Noise-Tolerant Quint-Channel Squeeze Cut (BTC15>=2.2%, ETH15>=2.5%, B1H>=90%)
    opened = dt.datetime(2026, 11, 24, 10, 0, tzinfo=dt.timezone.utc)
    t0_ms = int(opened.timestamp() * 1000)
    bar_ms = 900_000
    meta_v21_n7 = {"exit_mode": "V21_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60"}
    bars_loss = [
        (t0_ms, Decimal("100.5"), Decimal("99.5"), Decimal("100.2")),
        (t0_ms + bar_ms, Decimal("101.5"), Decimal("100.0"), Decimal("101.2")),
    ]
    # BTC 15m +2.1% -> HOLDS for V21_NEW7_SQZ96; +2.2% -> MUST_CLOSE!
    btc_21 = [(t0_ms, Decimal("60000"), Decimal("60000"), Decimal("60000")), (t0_ms + bar_ms, Decimal("61260"), Decimal("60000"), Decimal("61260"))]
    btc_22 = [(t0_ms, Decimal("60000"), Decimal("60000"), Decimal("60000")), (t0_ms + bar_ms, Decimal("61320"), Decimal("60000"), Decimal("61320"))]
    assert agy.evaluate_position_exit(meta_v21_n7, "100", "101.2", bars_loss, opened.isoformat(), opened + dt.timedelta(minutes=35), btc_bars_15m=btc_21)["action"] == "HOLD"
    ev_b22 = agy.evaluate_position_exit(meta_v21_n7, "100", "101.2", bars_loss, opened.isoformat(), opened + dt.timedelta(minutes=35), btc_bars_15m=btc_22)
    assert ev_b22["action"] == "MUST_CLOSE" and "BTC_SQUEEZE_CUT_15M" in ev_b22["exit_reason"]

    # ETH 15m +2.4% -> HOLDS; +2.5% -> MUST_CLOSE!
    eth_24 = [(t0_ms, Decimal("3000"), Decimal("3000"), Decimal("3000")), (t0_ms + bar_ms, Decimal("3072"), Decimal("3000"), Decimal("3072"))]
    eth_25 = [(t0_ms, Decimal("3000"), Decimal("3000"), Decimal("3000")), (t0_ms + bar_ms, Decimal("3075"), Decimal("3000"), Decimal("3075"))]
    assert agy.evaluate_position_exit(meta_v21_n7, "100", "101.2", bars_loss, opened.isoformat(), opened + dt.timedelta(minutes=35), eth_bars_15m=eth_24)["action"] == "HOLD"
    ev_e25 = agy.evaluate_position_exit(meta_v21_n7, "100", "101.2", bars_loss, opened.isoformat(), opened + dt.timedelta(minutes=35), eth_bars_15m=eth_25)
    assert ev_e25["action"] == "MUST_CLOSE" and "ETH_SQUEEZE_CUT_15M" in ev_e25["exit_reason"]

    # B1H 89% -> HOLDS for V21_NEW7_SQZ96; 90% -> MUST_CLOSE!
    assert agy.evaluate_position_exit(meta_v21_n7, "100", "101.2", [], opened.isoformat(), opened + dt.timedelta(minutes=35), live_b1h=Decimal("0.89"))["action"] == "HOLD"
    ev_b90 = agy.evaluate_position_exit(meta_v21_n7, "100", "101.2", [], opened.isoformat(), opened + dt.timedelta(minutes=35), live_b1h=Decimal("0.90"))
    assert ev_b90["action"] == "MUST_CLOSE" and "B1H_SQUEEZE_CUT_MARK" in ev_b90["exit_reason"]

    # Fast-guard parity for V21_NEW7_SQZ96 (BTC15 +2.2%, ETH15 +2.5%, B1H 90%; legacy V14_NEW7_SQZ96 untouched)
    now_fg = opened + dt.timedelta(minutes=35)
    t_prev_ms = int(dt.datetime(2026, 11, 24, 10, 15, tzinfo=dt.timezone.utc).timestamp() * 1000)
    pf_fg = {"shorts": [
        {"id": "v21_n7", "symbol": "N7USDT", "entry": "100", "opened_at": opened.isoformat(), "on_binance": True},
        {"id": "v14_n7", "symbol": "O7USDT", "entry": "100", "opened_at": opened.isoformat(), "on_binance": True},
    ]}
    pm_fg = {
        "v21_n7": {"exit_mode": "V21_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60", "ratchet_stage": "HALF65", "stop_px": "102.0"},
        "v14_n7": {"exit_mode": "V14_NEW7_SQZ96", "stop_pct": "0.060", "tp_pct": "0.60", "ratchet_stage": "HALF65", "stop_px": "102.0"},
    }
    ema_map = {f"C{i}USDT": 1.0 for i in range(20)}
    btc_ref_fg = {
        "at": now_fg.isoformat(),
        "eth_at": now_fg.isoformat(),
        "ema20_1h_at": now_fg.isoformat(),
        "closes_15m": {str(t_prev_ms): "60000"},
        "eth_closes_15m": {str(t_prev_ms): "3000"},
        "ema20_1h_map": ema_map,
    }
    # B1H = 90% (18/20 above EMA20): v21_n7 triggers, v14_n7 holds
    marks_b90 = {"BTCUSDT": "60100", "ETHUSDT": "3010", "N7USDT": "101.0", "O7USDT": "101.0"}
    for i in range(20):
        marks_b90[f"C{i}USDT"] = "1.05" if i < 18 else "0.95"
    br_b90, _ = agy.check_fast_guard_events(pm_fg, pf_fg, marks_b90, now_fg, btc_ref=btc_ref_fg)
    assert [b["id"] for b in br_b90] == ["v21_n7"] and "FAST_B1H_SQUEEZE_CUT" in br_b90[0]["reason"] and "90%" in br_b90[0]["reason"]

    # BTC 15m +2.2% (61320/60000): v21_n7 triggers, v14_n7 holds
    marks_b22 = {**marks_b90, "BTCUSDT": "61320", **{f"C{i}USDT": "0.95" for i in range(20)}}
    br_b22, _ = agy.check_fast_guard_events(pm_fg, pf_fg, marks_b22, now_fg, btc_ref=btc_ref_fg)
    assert [b["id"] for b in br_b22] == ["v21_n7"] and "FAST_BTC_SQUEEZE_CUT" in br_b22[0]["reason"]

    # ETH 15m +2.5% (3075/3000): v21_n7 triggers, v14_n7 holds
    marks_e25 = {**marks_b90, "ETHUSDT": "3075", **{f"C{i}USDT": "0.95" for i in range(20)}}
    br_e25, _ = agy.check_fast_guard_events(pm_fg, pf_fg, marks_e25, now_fg, btc_ref=btc_ref_fg)
    assert [b["id"] for b in br_e25] == ["v21_n7"] and "FAST_ETH_SQUEEZE_CUT" in br_e25[0]["reason"] and "2.5%" in br_e25[0]["reason"]

    # 5. Production Fix: Funding retry + 30m cache fallback + stale rejection
    import tempfile
    orig_fc = agy.FUNDING_CACHE_PATH
    orig_pf_fn = agy.market.perp_funding
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.FUNDING_CACHE_PATH = os.path.join(td, "fund.json")
            calls = {"n": 0}
            def _flaky():
                calls["n"] += 1
                return {"BTCUSDT": 0.0003} if calls["n"] >= 2 else {}
            agy.market.perp_funding = _flaky
            got_f = agy._fetch_funding_with_cache(now_fg)
            assert got_f == {"BTCUSDT": 0.0003} and calls["n"] == 2
            # Now fail completely -> must use fresh cache (10m later)
            agy.market.perp_funding = lambda: {}
            assert agy._fetch_funding_with_cache(now_fg + dt.timedelta(minutes=10)) == {"BTCUSDT": 0.0003}
            # Stale (>30m) -> must return {}
            assert agy._fetch_funding_with_cache(now_fg + dt.timedelta(minutes=35)) == {}
    finally:
        agy.FUNDING_CACHE_PATH = orig_fc
        agy.market.perp_funding = orig_pf_fn

    # 6. Production Fix: Compact scan payload keeps BULL_SPOTFLOW_2H when BD=72% & active_squeeze=False
    orig_lock = agy.LOCK_PATH
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_pm = agy.POS_META_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "lock")
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.write_json_atomic(agy.POS_META_PATH, {})
            pf_s = {"mode": "sim", "wallet_usdt": "900", "equity_usdt": "900", "available_usdt": "900", "shorts": [], "held_symbols": [], "closed": []}
            agy._sync_and_portfolio = lambda now, do_sync=True: (pf_s, {"ok": True}, {}, {"tripped": False})
            agy._evaluate_open_positions = lambda pf, now, bars_out=None, flow_out=None: []
            cand_bull = [{"symbol": "B0USDT", "primary_setup": "BULL_SPOTFLOW_2H", "score": 221.0, "price": 1.0}]
            agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None, held_flow_15m=None: (
                {"regime_label": "BULLISH_SQUEEZE_CAUTION", "active_squeeze": False, "funding_ok": True},
                cand_bull, [], [], {},
            )
            _, out_calm = agy._build_scan_payload(now_fg, full=False)
            assert len(out_calm["candidates"]) == 1 and "candidates_omitted_reason" not in out_calm
            agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None, held_flow_15m=None: (
                {"regime_label": "BULLISH_SQUEEZE_CAUTION", "active_squeeze": True, "funding_ok": True},
                cand_bull, [], [], {},
            )
            # With ENFORCE_REGIME_OPEN_GATE=False (default), active_squeeze does NOT omit candidates
            _, out_sqz_open = agy._build_scan_payload(now_fg, full=False)
            assert len(out_sqz_open["candidates"]) == 1 and "candidates_omitted_reason" not in out_sqz_open
            orig_rg21 = agy.ENFORCE_REGIME_OPEN_GATE
            try:
                agy.ENFORCE_REGIME_OPEN_GATE = True
                _, out_sqz = agy._build_scan_payload(now_fg, full=False)
                assert out_sqz["candidates"] == [] and out_sqz["candidates_omitted_reason"] == "bullish_squeeze_caution"
            finally:
                agy.ENFORCE_REGIME_OPEN_GATE = orig_rg21
    finally:
        agy.LOCK_PATH = orig_lock
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.POS_META_PATH = orig_pm



def test_v22_dual_side_short_long():
    """Verify V22 Dual-Side SHORT+LONG rules: 5 LONG engines, LONG Decimal exit/ratchet/crash-cut, 5s fast-guard, heat, auto-trade & Console plan."""
    import io
    import tempfile
    from contextlib import redirect_stdout

    assert agy.STRATEGY_VERSION == "V23_EMA_RSI_MOOD"
    assert agy.ALLOWED_SIDES == frozenset({"SHORT", "LONG"})
    assert agy.MARGIN_USDT == Decimal("100") and agy.LOT_USDT == Decimal("200") and agy.LEVERAGE == 2 and agy.MAX_OPEN == 0 and agy.MAX_LONG_OPEN == 0 and agy.MAX_SHORT_OPEN == 0
    assert agy.DAILY_MAX_OPENS == 0 and agy.HOURLY_MAX_OPENS == 0 and agy.ENFORCE_REGIME_OPEN_GATE is False and agy.ENFORCE_OPEN_COOLDOWN is False and agy.CB_AUTO_ENABLED is True

    # 1. Combined SHORT+LONG open heat (mixed 1x qty=1 + 2x qty=2) & per-side counts
    pf_mix = {
        "shorts": [
            {"id": "s1", "symbol": "AAAUSDT", "side": "SHORT", "qty": "1", "entry": "100", "on_binance": True},  # 1x (100U) * 5% = 5.00U
            {"id": "l1", "symbol": "BBBUSDT", "side": "LONG", "qty": "2", "entry": "100", "on_binance": True},   # 2x (200U) * 5% = 10.00U
            {"id": "l2", "symbol": "CCCUSDT", "side": "LONG", "qty": "2", "entry": "100", "on_binance": True},   # profit-locked -> 0.00U
        ]
    }
    meta_mix = {
        "s1": {"side": "SHORT", "setup": "SP_UWICK_1H", "stop_pct": "0.050", "stop_px": "105.0"},   # 5.00U (1x)
        "l1": {"side": "LONG", "setup": "L_SQUEEZE_1H", "stop_pct": "0.050", "stop_px": "95.0"},    # 10.00U (2x)
        "l2": {"side": "LONG", "setup": "L_SPOTACC_1H", "stop_pct": "0.050", "stop_px": "102.5"},   # profit-locked -> 0.00U
    }
    heat_mix, cnt_mix = agy.compute_open_heat_and_counts(pf_mix, meta_mix)
    assert heat_mix == Decimal("15.00")
    assert cnt_mix == {"SP_UWICK_1H": 1, "L_SQUEEZE_1H": 1, "L_SPOTACC_1H": 1}
    assert agy.count_open_by_side(pf_mix, meta_mix) == {"SHORT": 1, "LONG": 2}

    # 2. Causal LONG exit engine: SL before TP, upward ratchet HALF50->LOCK8->TRAIL13->TRAIL22->TRAIL30, 24h runner, BTC/ETH crash cuts
    opened = dt.datetime(2026, 11, 25, 0, 0, tzinfo=dt.timezone.utc)
    t0 = int(opened.timestamp() * 1000)
    bms = 900_000
    meta_l = {"side": "LONG", "exit_mode": "V22_LONG_SQZ96", "stop_pct": "0.050", "tp_pct": "0.400"}

    # 2a. Conservative SL checked BEFORE TP when same bar hits both
    ev_both = agy.evaluate_position_exit(meta_l, "100", "105", [(t0, Decimal("145"), Decimal("94.9"), Decimal("130"))], opened.isoformat(), opened + dt.timedelta(minutes=20))
    assert ev_both["action"] == "MUST_CLOSE" and "STOP_LOSS_15M" in ev_both["exit_reason"] and ev_both["side"] == "LONG"

    # 2b. Take profit on 15m high >= 140
    ev_tp = agy.evaluate_position_exit(meta_l, "100", "138", [(t0, Decimal("140.1"), Decimal("99.5"), Decimal("139"))], opened.isoformat(), opened + dt.timedelta(minutes=20))
    assert ev_tp["action"] == "MUST_CLOSE" and "TAKE_PROFIT_15M" in ev_tp["exit_reason"]

    # 2c. Upward ratchet progression: HALF50 (+5.5% -> 99.0), LOCK8 (+8.5% -> 102.5), TRAIL13 (+14% -> 109.5), TRAIL22 (+23% -> 119.5), TRAIL30 (+32% -> 129.5)
    bars_r = [
        (t0, Decimal("105.5"), Decimal("99.5"), Decimal("104.0")),
        (t0 + bms, Decimal("108.5"), Decimal("103.0"), Decimal("107.0")),
        (t0 + 2 * bms, Decimal("114.0"), Decimal("106.5"), Decimal("112.0")),
        (t0 + 3 * bms, Decimal("123.0"), Decimal("111.0"), Decimal("121.0")),
        (t0 + 4 * bms, Decimal("132.0"), Decimal("120.5"), Decimal("130.0")),
    ]
    ev_h50 = agy.evaluate_position_exit(meta_l, "100", "104.0", bars_r[:1], opened.isoformat(), opened + dt.timedelta(minutes=20))
    assert ev_h50["action"] == "HOLD" and ev_h50["ratchet_stage"] == "HALF50" and Decimal(ev_h50["stop_px"]) == Decimal("99")
    ev_lk8 = agy.evaluate_position_exit(meta_l, "100", "107.0", bars_r[:2], opened.isoformat(), opened + dt.timedelta(minutes=35))
    assert ev_lk8["action"] == "HOLD" and ev_lk8["ratchet_stage"] == "LOCK8" and Decimal(ev_lk8["stop_px"]) == Decimal("102.5")
    ev_t13 = agy.evaluate_position_exit(meta_l, "100", "112.0", bars_r[:3], opened.isoformat(), opened + dt.timedelta(minutes=50))
    assert ev_t13["action"] == "HOLD" and ev_t13["ratchet_stage"] == "TRAIL13" and Decimal(ev_t13["stop_px"]) == Decimal("109.5")
    ev_t22 = agy.evaluate_position_exit(meta_l, "100", "121.0", bars_r[:4], opened.isoformat(), opened + dt.timedelta(minutes=65))
    assert ev_t22["action"] == "HOLD" and ev_t22["ratchet_stage"] == "TRAIL22" and Decimal(ev_t22["stop_px"]) == Decimal("119.5")
    ev_t30 = agy.evaluate_position_exit(meta_l, "100", "130.0", bars_r[:5], opened.isoformat(), opened + dt.timedelta(minutes=80))
    assert ev_t30["action"] == "HOLD" and ev_t30["ratchet_stage"] == "TRAIL30" and Decimal(ev_t30["stop_px"]) == Decimal("129.5")
    # Pullback to 129.4 triggers TRAILING_STOP_MARK
    ev_tr_hit = agy.evaluate_position_exit(ev_t30["updated_meta"], "100", "129.4", [], opened.isoformat(), opened + dt.timedelta(minutes=85))
    assert ev_tr_hit["action"] == "MUST_CLOSE" and "TRAILING_STOP_MARK" in ev_tr_hit["exit_reason"]

    # 2d. 24h Runner unlock for LONG (>= +5% -> RUNNER96, holds to 75h, closes at 96h; < +5% closes at 24h)
    ev_l24_no = agy.evaluate_position_exit(meta_l, "100", "103.0", [], opened.isoformat(), opened + dt.timedelta(hours=24))
    assert ev_l24_no["action"] == "MUST_CLOSE" and "MAX_HOLD_24H" in ev_l24_no["exit_reason"]
    ev_l24_ok = agy.evaluate_position_exit({**meta_l, "mfe_pct": "0.070"}, "100", "106.0", [], opened.isoformat(), opened + dt.timedelta(hours=24))
    assert ev_l24_ok["action"] == "HOLD" and ev_l24_ok["ratchet_stage"] == "RUNNER96" and Decimal(ev_l24_ok["stop_px"]) >= Decimal("102.5")
    ev_l96 = agy.evaluate_position_exit(ev_l24_ok["updated_meta"], "100", "115.0", [], opened.isoformat(), opened + dt.timedelta(hours=96))
    assert ev_l96["action"] == "MUST_CLOSE" and "MAX_HOLD_96H" in ev_l96["exit_reason"]

    # 2e. BTC/ETH 15m Crash Cut cuts unprotected losing LONG, ignores BTC upward squeeze & ignores profitable LONG
    btc_bars_cr = [
        (t0 - bms, Decimal("60000"), Decimal("59900"), Decimal("60000")),
        (t0, Decimal("60100"), Decimal("59900"), Decimal("60000")),
        (t0 + bms, Decimal("60000"), Decimal("58800"), Decimal("58900")),  # -1.83% 15m BTC crash!
    ]
    coin_bars_loss = [
        (t0, Decimal("100.5"), Decimal("99.0"), Decimal("99.8")),
        (t0 + bms, Decimal("99.8"), Decimal("97.0"), Decimal("97.5")),
    ]
    ev_bcr = agy.evaluate_position_exit(meta_l, "100", "97.5", coin_bars_loss, opened.isoformat(), opened + dt.timedelta(minutes=35), btc_bars_15m=btc_bars_cr, btc_mark_px="58900")
    assert ev_bcr["action"] == "MUST_CLOSE" and "BTC_CRASH_CUT_15M" in ev_bcr["exit_reason"]
    # Profitable LONG (+2%) must NOT be crash-cut
    coin_bars_win = [
        (t0, Decimal("101.0"), Decimal("99.5"), Decimal("100.5")),
        (t0 + bms, Decimal("102.5"), Decimal("100.5"), Decimal("102.0")),
    ]
    ev_bcr_w = agy.evaluate_position_exit(meta_l, "100", "102.0", coin_bars_win, opened.isoformat(), opened + dt.timedelta(minutes=35), btc_bars_15m=btc_bars_cr, btc_mark_px="58900")
    assert ev_bcr_w["action"] == "HOLD"

    # 3. 5-second Fast Guard: side-by-side SHORT + LONG isolation (BTC crash cuts LONG only; BTC squeeze cuts SHORT only)
    now_fg = dt.datetime(2026, 11, 25, 4, 20, tzinfo=dt.timezone.utc)
    t_prev_ms = int(dt.datetime(2026, 11, 25, 4, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    pf_dual = {
        "shorts": [
            {"id": "p_long", "symbol": "L1USDT", "side": "LONG", "entry": "100", "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat(), "on_binance": True},
            {"id": "p_short", "symbol": "S1USDT", "side": "SHORT", "entry": "100", "opened_at": (now_fg - dt.timedelta(hours=1)).isoformat(), "on_binance": True},
        ]
    }
    pm_dual = {
        "p_long": {"side": "LONG", "exit_mode": "V22_LONG_SQZ96", "stop_pct": "0.050", "tp_pct": "0.400", "stop_px": "95.0", "tp_px": "140.0", "ratchet_stage": "INIT"},
        "p_short": {"side": "SHORT", "exit_mode": "V18_AA_SQZ96", "stop_pct": "0.050", "tp_pct": "0.600", "stop_px": "105.0", "tp_px": "40.0", "ratchet_stage": "INIT"},
    }
    btc_ref_d = {
        "at": now_fg.isoformat(),
        "eth_at": now_fg.isoformat(),
        "closes_15m": {str(t_prev_ms): "60000"},
        "eth_closes_15m": {str(t_prev_ms): "3000"},
    }
    # BTC dumps -2.0% -> cuts losing LONG only, keeps SHORT untouched
    br_cr, _ = agy.check_fast_guard_events(pm_dual, pf_dual, {"BTCUSDT": "58800", "ETHUSDT": "2980", "L1USDT": "98.0", "S1USDT": "101.0"}, now_fg, btc_ref=btc_ref_d)
    assert [b["id"] for b in br_cr] == ["p_long"] and "FAST_BTC_CRASH_CUT" in br_cr[0]["reason"]
    # ETH dumps -2.2% -> cuts losing LONG only
    br_ecr, _ = agy.check_fast_guard_events(pm_dual, pf_dual, {"BTCUSDT": "59900", "ETHUSDT": "2934", "L1USDT": "98.5", "S1USDT": "101.0"}, now_fg, btc_ref=btc_ref_d)
    assert [b["id"] for b in br_ecr] == ["p_long"] and "FAST_ETH_CRASH_CUT" in br_ecr[0]["reason"]
    # BTC pumps +2.0% -> cuts losing SHORT only, keeps LONG untouched
    br_sq, _ = agy.check_fast_guard_events(pm_dual, pf_dual, {"BTCUSDT": "61200", "ETHUSDT": "3010", "L1USDT": "98.5", "S1USDT": "101.0"}, now_fg, btc_ref=btc_ref_d)
    assert [b["id"] for b in br_sq] == ["p_short"] and "FAST_BTC_SQUEEZE_CUT" in br_sq[0]["reason"]
    # Direct LONG mark SL & TP checks
    br_lsl, _ = agy.check_fast_guard_events(pm_dual, pf_dual, {"BTCUSDT": "60000", "ETHUSDT": "3000", "L1USDT": "94.9", "S1USDT": "99.0"}, now_fg, btc_ref=btc_ref_d)
    assert [b["id"] for b in br_lsl] == ["p_long"] and "FAST_SL_MARK" in br_lsl[0]["reason"]
    br_ltp, _ = agy.check_fast_guard_events(pm_dual, pf_dual, {"BTCUSDT": "60000", "ETHUSDT": "3000", "L1USDT": "140.5", "S1USDT": "99.0"}, now_fg, btc_ref=btc_ref_d)
    assert [b["id"] for b in br_ltp] == ["p_long"] and "FAST_TP_MARK" in br_ltp[0]["reason"]

    # 4. Evaluate all 5 LONG engines + negative guards
    # 4a. L_SQUEEZE_1H (works in deep bear when bas8h <= -0.05%)
    hits_sqz = agy.evaluate_v22_long_setups(
        breadth_1d=0.16, breadth_1h=0.18, bar_complete_1h=True, cur_px=10.25,
        o_1h=10.0, hi_1h=10.30, lo_1h=9.90, c_1h=10.22, c_1h_prev=9.95,
        qv_1h=3_000_000, qv_avg20_1h=1_500_000, tb_1h=0.60,
        ema10_1h=10.05, ema10_1h_prev=10.02, atr14_1h_pct=0.025, ret_24h=-0.02,
        f_day=-0.0004, has_spot_24h=True, bas8h=-0.0012,
        spot_1h_complete=True, spot_tb_1h=0.58, spot_vrat_1h=1.4, spot_green_1h=True, spot_lwick_1h=0.15,
    )
    assert any(h["setup"] == "L_SQUEEZE_1H" and h["side"] == "LONG" and h["exit_mode"] == "V22_LONG_SQZ96" for h in hits_sqz)
    # Anti-chase guard (> +3% above signal close) must reject
    hits_chase = agy.evaluate_v22_long_setups(
        breadth_1d=0.16, breadth_1h=0.18, bar_complete_1h=True, cur_px=10.60,
        o_1h=10.0, hi_1h=10.30, lo_1h=9.90, c_1h=10.22, c_1h_prev=9.95,
        qv_1h=3_000_000, qv_avg20_1h=1_500_000, tb_1h=0.60,
        ema10_1h=10.05, ema10_1h_prev=10.02, atr14_1h_pct=0.025, ret_24h=-0.02,
        f_day=-0.0004, has_spot_24h=True, bas8h=-0.0012,
        spot_1h_complete=True, spot_tb_1h=0.58, spot_vrat_1h=1.4, spot_green_1h=True, spot_lwick_1h=0.15,
    )
    assert hits_chase == []

    # 4b. L_SPOTACC_1H & L_SP_LWICK_1H
    hits_sp = agy.evaluate_v22_long_setups(
        breadth_1d=0.30, breadth_1h=0.35, btc_ret1h=0.002, bar_complete_1h=True, cur_px=10.20,
        o_1h=10.0, hi_1h=10.25, lo_1h=9.70, c_1h=10.18, c_1h_prev=10.02,
        qv_1h=2_000_000, qv_avg20_1h=1_200_000, tb_1h=0.58,
        ema10_1h=10.05, ema10_1h_prev=10.03, atr14_1h_pct=0.02, f_day=0.0001,
        has_spot_24h=True, bas8h=0.0002,
        spot_1h_complete=True, spot_tb_1h=0.62, spot_vrat_1h=1.8, spot_green_1h=True, spot_lwick_1h=0.38,
    )
    sp_names = {h["setup"] for h in hits_sp}
    assert "L_SPOTACC_1H" in sp_names and "L_SP_LWICK_1H" in sp_names

    # 4c. L_TREND_BRK_1H (fires in bull/recovery; blocked in deep bear)
    kw_tr = dict(
        btc_px=62000, btc_1h_ema20=61500, bar_complete_1h=True, cur_px=10.30,
        o_1h=10.0, hi_1h=10.35, lo_1h=9.95, c_1h=10.28, c_1h_prev=10.10,
        qv_1h=3_000_000, qv_avg20_1h=1_500_000, tb_1h=0.60,
        ema10_1h=10.10, ema10_1h_prev=10.05, ema20_1h=9.90, hi16_1h_prev=10.20,
        rsi1h_now=62.0, atr14_1h_pct=0.02, d_sma20=9.50, f_day=0.0002, bas8h=0.0003,
    )
    assert any(h["setup"] == "L_TREND_BRK_1H" for h in agy.evaluate_v22_long_setups(breadth_1d=0.45, breadth_1h=0.55, **kw_tr))
    assert not any(h["setup"] == "L_TREND_BRK_1H" for h in agy.evaluate_v22_long_setups(breadth_1d=0.15, breadth_1h=0.18, **{**kw_tr, "btc_px": 59000}))

    # 4d. L_EMA_BOUNCE_2H (zero Spot dependency)
    hits_2h = agy.evaluate_v22_long_setups(
        is_fresh_2h_window=True, bar_complete_2h=True, cur_px=10.25,
        o_2h=10.10, hi_2h=10.28, lo_2h=9.95, c_2h=10.24, c_2h_prev=10.12,
        qv_2h=4_000_000, qv_avg20_2h=2_500_000, tb_2h=0.60,
        ema10_2h=10.02, ema10_2h_prev=10.00, ema20_2h=9.85,
        atr14_1h_pct=0.02, d_sma20=9.50, f_day=0.0001, bas8h=0.0002,
    )
    assert any(h["setup"] == "L_EMA_BOUNCE_2H" for h in hits_2h)

    # 5. Directional isolation in cmd_auto_trade + _execute_open(side="LONG") + Console dca-plan.json
    assert agy.is_active_crash(-0.055, -0.005, -0.01, 0.30) is True
    assert agy.is_active_crash(-0.02, -0.005, -0.01, 0.18) is False  # quiet bear does NOT block L_SQUEEZE_1H
    assert agy.is_active_crash(-0.02, -0.005, -0.01, 0.08) is True   # <10% panic waterfall blocks LONG

    orig_lock = agy.LOCK_PATH
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_pm = agy.POS_META_PATH
    orig_st = agy.STATE_PATH
    orig_rep = agy.REPORTS
    orig_sig = agy.ENTRY_SIGNALS_PATH
    orig_jrn = agy.JOURNAL_PATH
    orig_open = agy._execute_open
    orig_ch_wait = agy.wait_for_clickhouse_15m
    orig_pf_path = agy.binance.PORTFOLIO
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.LOCK_PATH = os.path.join(td, "lock")
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.STATE_PATH = os.path.join(td, "st.json")
            agy.REPORTS = os.path.join(td, "reports")
            agy.ENTRY_SIGNALS_PATH = os.path.join(td, "sig.json")
            agy.JOURNAL_PATH = os.path.join(td, "journal.jsonl")
            agy.binance.PORTFOLIO = os.path.join(td, "portfolio.json")
            pf_at = {"mode": "sim", "wallet_usdt": "900", "equity_usdt": "900", "available_usdt": "900", "shorts": [], "held_symbols": [], "closed": [], "orders": []}
            agy.write_json_atomic(agy.POS_META_PATH, {})
            agy.write_json_atomic(agy.STATE_PATH, {"circuit_breaker_tripped": False})
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf_at)
            agy._sync_and_portfolio = lambda now, do_sync=True: (pf_at, {"ok": True}, {"circuit_breaker_tripped": False}, {"tripped": False})
            agy._evaluate_open_positions = lambda pf, now, bars_out=None, flow_out=None: []
            agy.wait_for_clickhouse_15m = lambda now, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0, "target_15m": "2026-11-25 00:00:00"}
            cands_dual = [
                {"symbol": "SHRTUSDT", "coin": "SHRT", "side": "SHORT", "primary_setup": "SP_UWICK_1H", "score": 250.0, "price": 10.0, "ret_24h_pct": 1.0, "ret_7d_pct": 2.0, "suggested_stop_pct": 0.05, "suggested_tp_pct": 0.60, "exit_mode": "V18_AA_SQZ96"},
                {"symbol": "LONGUSDT", "coin": "LONG", "side": "LONG", "primary_setup": "L_SQUEEZE_1H", "score": 245.0, "price": 20.0, "ret_24h_pct": -1.0, "ret_7d_pct": -5.0, "suggested_stop_pct": 0.05, "suggested_tp_pct": 0.40, "exit_mode": "V22_LONG_SQZ96"},
            ]
            # Default ENFORCE_REGIME_OPEN_GATE=False: BOTH SHORT and LONG open even during active_squeeze / active_crash!
            agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None: (
                {"regime_label": "BULLISH_SQUEEZE_CAUTION", "active_squeeze": True, "active_crash": False, "funding_ok": True, "complete_1h_pct": 100.0, "complete_15m_pct": 100.0, "btc": {"price": 62000}, "breadth": {"pct_above_1d_sma20": 50.0, "pct_above_1h_ema20": 75.0}},
                cands_dual, [], [], {},
            )
            called_opens = []
            def _fake_open(sym, sl, tp, reason, **kw):
                called_opens.append((sym, kw.get("side"), kw.get("setup"), kw.get("exit_mode")))
                return True, {"ok": True, "id": f"bto-test-{sym}", "symbol": sym, "side": kw.get("side")}, 0
            agy._execute_open = _fake_open
            buf_both = io.StringIO()
            with redirect_stdout(buf_both):
                rc_both = agy.cmd_auto_trade(max_opens=0, dry_run=False)
            assert rc_both == 0
            assert called_opens == [
                ("SHRTUSDT", "SHORT", "SP_UWICK_1H", "V18_AA_SQZ96"),
                ("LONGUSDT", "LONG", "L_SQUEEZE_1H", "V22_LONG_SQZ96"),
            ]
            # When ENFORCE_REGIME_OPEN_GATE=True, directional isolation still works cleanly
            orig_rg22 = agy.ENFORCE_REGIME_OPEN_GATE
            try:
                agy.ENFORCE_REGIME_OPEN_GATE = True
                called_opens.clear()
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = agy.cmd_auto_trade(max_opens=3, dry_run=False)
                assert rc == 0
                assert called_opens == [("LONGUSDT", "LONG", "L_SQUEEZE_1H", "V22_LONG_SQZ96")]
                plan_day = os.path.join(agy.REPORTS, agy.utcnow().date().isoformat(), "dca-plan.json")
                plan_obj = agy.read_json(plan_day)
                assert plan_obj["sides"]["LONGUSDT"] == "LONG" and plan_obj["rows"][0]["side"] == "LONG"

                # During active_crash (active_squeeze=False): LONG must be skipped, SHORT must open!
                called_opens.clear()
                agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None: (
                    {"regime_label": "BEARISH_FAVORABLE", "active_squeeze": False, "active_crash": True, "funding_ok": True, "complete_1h_pct": 100.0, "complete_15m_pct": 100.0, "btc": {"price": 57000}, "breadth": {"pct_above_1d_sma20": 15.0, "pct_above_1h_ema20": 8.0}},
                    cands_dual, [], [], {},
                )
                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    rc2 = agy.cmd_auto_trade(max_opens=3, dry_run=False)
                assert rc2 == 0
                assert called_opens == [("SHRTUSDT", "SHORT", "SP_UWICK_1H", "V18_AA_SQZ96")]
            finally:
                agy.ENFORCE_REGIME_OPEN_GATE = orig_rg22
    finally:
        agy.LOCK_PATH = orig_lock
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.POS_META_PATH = orig_pm
        agy.STATE_PATH = orig_st
        agy.REPORTS = orig_rep
        agy.ENTRY_SIGNALS_PATH = orig_sig
        agy.JOURNAL_PATH = orig_jrn
        agy._execute_open = orig_open
        agy.wait_for_clickhouse_15m = orig_ch_wait
        agy.binance.PORTFOLIO = orig_pf_path


def test_v22_m15_engines_and_scheduler():
    """Verify M15_BRK_SHORT / M15_BRK_LONG engines and Dev-on-the-Fly agy_scheduler.py."""
    import tempfile
    import agy_scheduler as sched

    # 1. M15_BRK_SHORT fires cleanly on fresh 15m support breakdown; rejects chase / high wick / bad spot_tb
    h_s = agy.evaluate_v22_m15_setups(
        m15_complete=True, cur_px=9.75, c15_now=9.78, c15_prev=9.92,
        hi15_now=9.95, lo15_now=9.75, qv15_now=500_000, qv15_avg20=200_000,
        ema20_15m=9.95, sma50_15m=10.10, rsi15m_now=36.0, atr14_15m_pct=0.012,
        low16_15m=9.85, res16_15m=10.20, breadth_1d=0.30, bas8h=0.0001, f_day=0.0001, spot_tb_15m=0.38,
    )
    assert len(h_s) == 1 and h_s[0]["setup"] == "M15_BRK_SHORT" and h_s[0]["side"] == "SHORT" and h_s[0]["stop_pct"] == 0.040
    # Reject when Spot buyers step in (spot_tb_15m >= 0.45)
    assert agy.evaluate_v22_m15_setups(
        m15_complete=True, cur_px=9.75, c15_now=9.78, c15_prev=9.92,
        hi15_now=9.95, lo15_now=9.75, qv15_now=500_000, qv15_avg20=200_000,
        ema20_15m=9.95, sma50_15m=10.10, rsi15m_now=36.0, atr14_15m_pct=0.012,
        low16_15m=9.85, res16_15m=10.20, breadth_1d=0.30, bas8h=0.0001, f_day=0.0001, spot_tb_15m=0.52,
    ) == []

    # 2. M15_BRK_LONG fires cleanly on fresh 15m resistance breakout; rejects chase > +1.5%
    h_l = agy.evaluate_v22_m15_setups(
        m15_complete=True, cur_px=10.25, c15_now=10.22, c15_prev=10.05,
        hi15_now=10.25, lo15_now=10.02, qv15_now=600_000, qv15_avg20=250_000,
        ema20_15m=10.00, sma50_15m=9.90, rsi15m_now=63.0, atr14_15m_pct=0.011,
        low16_15m=9.80, res16_15m=10.15, breadth_1d=0.25, bas8h=-0.0008, f_day=-0.0002, spot_tb_15m=0.60,
    )
    assert len(h_l) == 1 and h_l[0]["setup"] == "M15_BRK_LONG" and h_l[0]["side"] == "LONG" and h_l[0]["exit_mode"] == "V22_LONG_SQZ96"
    assert agy.evaluate_v22_m15_setups(
        m15_complete=True, cur_px=10.45, c15_now=10.22, c15_prev=10.05,
        hi15_now=10.25, lo15_now=10.02, qv15_now=600_000, qv15_avg20=250_000,
        ema20_15m=10.00, sma50_15m=9.90, rsi15m_now=63.0, atr14_15m_pct=0.011,
        low16_15m=9.80, res16_15m=10.15, breadth_1d=0.25, bas8h=-0.0008, f_day=-0.0002, spot_tb_15m=0.60,
    ) == []

    # 3. Scheduler 15m wall-clock slot alignment (:05, :20, :35, :50 after ClickHouse settles)
    t1 = dt.datetime(2026, 11, 25, 0, 1, 0, tzinfo=dt.timezone.utc)
    assert sched.next_slot_utc(t1, 15) == dt.datetime(2026, 11, 25, 0, 5, 0, tzinfo=dt.timezone.utc)
    t2 = dt.datetime(2026, 11, 25, 0, 5, 0, tzinfo=dt.timezone.utc)
    assert sched.next_slot_utc(t2, 15) == dt.datetime(2026, 11, 25, 0, 20, 0, tzinfo=dt.timezone.utc)
    t3 = dt.datetime(2026, 11, 25, 0, 50, 0, tzinfo=dt.timezone.utc)
    assert sched.next_slot_utc(t3, 15) == dt.datetime(2026, 11, 25, 1, 5, 0, tzinfo=dt.timezone.utc)
    t4 = dt.datetime(2026, 11, 25, 0, 34, 59, tzinfo=dt.timezone.utc)
    assert sched.next_slot_utc(t4, 15) == dt.datetime(2026, 11, 25, 0, 35, 0, tzinfo=dt.timezone.utc)
    assert sched.compute_align_delay_sec(dt.datetime(2026, 11, 25, 0, 5, 1, tzinfo=dt.timezone.utc)) == 0.0
    assert sched.compute_align_delay_sec(dt.datetime(2026, 11, 25, 0, 20, 2, tzinfo=dt.timezone.utc)) == 0.0
    assert sched.compute_align_delay_sec(dt.datetime(2026, 11, 25, 0, 0, 10, tzinfo=dt.timezone.utc)) == 290.0

    # 4. Scheduler IP scrubbing & syntax-error fail-safe before hot-sync
    assert "[REDACTED_IP]" in sched.scrub_text("err 203.0.113.55")
    assert "192.168.1.10" in sched.scrub_text("host 192.168.1.10")
    orig_tp = sched.TRADER_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            bad_py = os.path.join(td, "bad.py")
            with open(bad_py, "w", encoding="utf-8") as f:
                f.write("def broken(:\n")
            sched.TRADER_PATH = bad_py
            r_bad = sched.sync_trader_if_changed({}, force=True)
            assert r_bad["synced"] is False and r_bad["status"] == "syntax_error"
            good_py = os.path.join(td, "good.py")
            with open(good_py, "w", encoding="utf-8") as f:
                f.write("X = 1\n")
            sched.TRADER_PATH = good_py
            sha_g = sched.file_sha256(good_py)
            r_same = sched.sync_trader_if_changed({"last_synced_sha256": sha_g}, force=False)
            assert r_same["synced"] is False and r_same["status"] == "unchanged"
    finally:
        sched.TRADER_PATH = orig_tp


def test_v22_console_manual_close_and_heartbeat():
    """Verify fast-guard handles Monitor console close-bto-... & portfolio refresh requests safely + writes heartbeat."""
    import tempfile

    orig_req = agy.REQUESTS_DIR
    orig_hb = agy.GUARD_HEARTBEAT_PATH
    orig_lock = agy.LOCK_PATH
    orig_pf = agy.binance.PORTFOLIO
    orig_st = agy.STATE_PATH
    orig_rep = agy.REPORTS
    orig_jrn = agy.JOURNAL_PATH
    orig_close = agy._execute_close
    orig_fetch = agy.binance.fetch_portfolio
    orig_save = agy.binance.save_portfolio
    orig_marks = agy.fetch_mark_prices
    orig_pm = agy.POS_META_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            req_dir = os.path.join(td, "requests")
            os.makedirs(req_dir, exist_ok=True)
            agy.REQUESTS_DIR = req_dir
            agy.GUARD_HEARTBEAT_PATH = os.path.join(td, "agy-guard-heartbeat.json")
            agy.LOCK_PATH = os.path.join(td, "guard.lock")
            agy.binance.PORTFOLIO = os.path.join(td, "portfolio.json")
            agy.STATE_PATH = os.path.join(td, "state.json")
            agy.POS_META_PATH = os.path.join(td, "agy-positions.json")
            agy.REPORTS = os.path.join(td, "reports")
            agy.JOURNAL_PATH = os.path.join(td, "reports", "agy-journal.jsonl")
            os.makedirs(agy.REPORTS, exist_ok=True)
            agy.write_json_atomic(agy.POS_META_PATH, {
                "bto-2026-11-25-BB": {
                    "symbol": "BBBUSDT", "side": "LONG", "setup": "EMA_RSI_LONG",
                    "exit_mode": "V22_LONG_SQZ96", "stop_pct": "0.050", "stop_px": "95.0",
                    "tp_pct": "0.30", "ratchet_stage": "INIT", "mfe_pct": "0.010", "mae_pct": "0",
                },
            })
            # Deliberately give BBBUSDT a different premiumIndex mark (104.00) vs positionRisk mark (105.5) to verify positionRisk takes precedence
            agy.fetch_mark_prices = lambda: {"BTCUSDT": Decimal("61234.50"), "BBBUSDT": Decimal("104.00")}
            agy.write_json_atomic(agy.binance.PORTFOLIO, {
                "equity_usdt": "910.00",
                "shorts": [{"id": "bto-2026-11-25-AA", "symbol": "AAAUSDT", "on_binance": True}],
            })
            now_u = dt.datetime(2026, 11, 25, 0, 10, 0, tzinfo=dt.timezone.utc)

            # Heartbeat write
            agy._write_guard_heartbeat(now_u)
            hb = agy.read_json(agy.GUARD_HEARTBEAT_PATH)
            assert hb and hb["ok"] is True and hb["strategy"] == agy.STRATEGY_VERSION

            # Valid close + stale/unknown ID + invalid filename (must be ignored)
            open(os.path.join(req_dir, "close-bto-2026-11-25-AA"), "w", encoding="utf-8").close()
            open(os.path.join(req_dir, "close-bto-2026-11-25-GONE"), "w", encoding="utf-8").close()
            open(os.path.join(req_dir, "run"), "w", encoding="utf-8").close()

            called_close = []
            def _fake_close(pid, why, now_utc=None):
                called_close.append((pid, why))
                return True, {"ok": True, "id": pid, "symbol": "AAAUSDT", "pnl_usdt": "1.25"}, 0

            agy._execute_close = _fake_close
            out = agy._process_console_requests(now_u, dry_run=False)
            assert len(called_close) == 1 and called_close[0][0] == "bto-2026-11-25-AA"
            assert "MANUAL_CLOSE" in called_close[0][1]
            assert len(out["closed"]) == 1 and out["closed"][0]["ok"] is True
            assert not os.path.exists(os.path.join(req_dir, "close-bto-2026-11-25-AA"))
            assert not os.path.exists(os.path.join(req_dir, "close-bto-2026-11-25-GONE"))
            assert os.path.exists(os.path.join(req_dir, "run"))  # legacy request untouched

            # Portfolio refresh request
            fetched = []
            agy.binance.fetch_portfolio = lambda: {
                "equity_usdt": "912.50",
                "shorts": [{"id": "bto-2026-11-25-BB", "symbol": "BBBUSDT", "side": "LONG", "entry": "100", "mark": "105.5", "on_binance": True}],
            }
            agy.binance.save_portfolio = lambda p: (fetched.append(p) or p)
            open(os.path.join(req_dir, "portfolio"), "w", encoding="utf-8").close()
            out2 = agy._process_console_requests(now_u, dry_run=False)
            assert out2["portfolio_refreshed"] is True and len(fetched) == 1
            assert not os.path.exists(os.path.join(req_dir, "portfolio"))
            # Live MFE/ratchet/Heat/BTC + Auto CB heartbeat metadata updated immediately on refresh (+5.5% LONG -> HALF50, stop=99.0, 200U*1%=2.00U!)
            pm_after = agy.read_json(agy.POS_META_PATH)
            assert pm_after["bto-2026-11-25-BB"]["ratchet_stage"] == "HALF50"
            assert Decimal(pm_after["bto-2026-11-25-BB"]["mfe_pct"]) == Decimal("0.055000")
            assert Decimal(pm_after["bto-2026-11-25-BB"]["stop_px"]) == Decimal("99")
            hb_after = agy.read_json(agy.GUARD_HEARTBEAT_PATH)
            assert hb_after["btc_price"] == "61234.50" and hb_after["open_heat_usdt"] == "2.00"
            assert hb_after["leverage"] == 2 and hb_after["margin_usdt"] == "100" and hb_after["lot_usdt"] == "200" and hb_after["min_avail_usdt"] == "101"
            assert hb_after["max_open"] == 0 and hb_after["max_long_open"] == 0 and hb_after["max_short_open"] == 0
            assert hb_after["cb_auto"] is True and hb_after["cb_ref_equity_usdt"] == "912.50" and hb_after["cb_limit_usdt"] == "68.44" and hb_after["cb_limit_pct"] == "7.50"
            # Fallback to marks_map only when positionRisk mark is missing (107.0 -> mfe +7.0%)
            agy._sync_live_pos_meta_and_heat(
                {"shorts": [{"id": "bto-2026-11-25-BB", "symbol": "BBBUSDT", "side": "LONG", "entry": "100", "on_binance": True}]},
                marks_map={"BBBUSDT": Decimal("107.0")},
            )
            assert Decimal(agy.read_json(agy.POS_META_PATH)["bto-2026-11-25-BB"]["mfe_pct"]) == Decimal("0.070000")

            # Wall-clock 10-second (:00/:10/:20/:30/:40/:50) server portfolio refresh: fires once per slot, skips same slot & fresh mtime
            os.utime(agy.binance.PORTFOLIO, (now_u.timestamp() - 20, now_u.timestamp() - 20))
            slot_a, ref_a = agy._refresh_portfolio_slot_10s(now_u, None, dry_run=False)
            assert ref_a is True and len(fetched) == 2 and slot_a == int(now_u.timestamp()) // 10
            # Same 10s slot (+5s) -> skipped!
            slot_a2, ref_a2 = agy._refresh_portfolio_slot_10s(now_u + dt.timedelta(seconds=5), slot_a, dry_run=False)
            assert ref_a2 is False and slot_a2 == slot_a and len(fetched) == 2
            # Next 10s slot (:10) -> fires again!
            t_10 = now_u + dt.timedelta(seconds=10)
            slot_b, ref_b = agy._refresh_portfolio_slot_10s(t_10, slot_a, dry_run=False)
            assert ref_b is True and slot_b == slot_a + 1 and len(fetched) == 3
            # Already fresh file mtime in current 10s slot (e.g. written by cmd_guard) -> skipped without calling fetch_portfolio!
            t_20 = now_u + dt.timedelta(seconds=20)
            os.utime(agy.binance.PORTFOLIO, (t_20.timestamp() + 1, t_20.timestamp() + 1))
            slot_c, ref_c = agy._refresh_portfolio_slot_10s(t_20, slot_b, dry_run=False)
            assert ref_c is False and slot_c == slot_b + 1 and len(fetched) == 3
            # dry_run=True never calls fetch_portfolio
            slot_d, ref_d = agy._refresh_portfolio_slot_10s(now_u + dt.timedelta(seconds=30), slot_c, dry_run=True)
            assert ref_d is False and slot_d == slot_c and len(fetched) == 3
            # Wall-clock 5s sleep alignment so 10s slots fire cleanly at :00/:10/:20/:30/:40/:50
            assert abs(agy._fast_guard_next_sleep(5, 20.0, now_ts=1001.2) - 3.85) < 1e-6
            assert abs(agy._fast_guard_next_sleep(5, 20.0, now_ts=1005.3) - 4.75) < 1e-6
            assert abs(agy._fast_guard_next_sleep(5, 20.0, now_ts=1009.95) - 0.15) < 1e-6
            assert abs(agy._fast_guard_next_sleep(5, 2.0, now_ts=1005.3) - 2.0) < 1e-6
    finally:
        agy.REQUESTS_DIR = orig_req
        agy.GUARD_HEARTBEAT_PATH = orig_hb
        agy.LOCK_PATH = orig_lock
        agy.binance.PORTFOLIO = orig_pf
        agy.STATE_PATH = orig_st
        agy.REPORTS = orig_rep
        agy.JOURNAL_PATH = orig_jrn
        agy._execute_close = orig_close
        agy.binance.fetch_portfolio = orig_fetch
        agy.binance.save_portfolio = orig_save
        agy.fetch_mark_prices = orig_marks
        agy.POS_META_PATH = orig_pm


def test_v23_ema_rsi_mood_and_clickhouse_wait():
    """Verify V23 EMA_RSI_SHORT / EMA_RSI_LONG (anti-extended, funding guard, pullback/breakout, RS ranking, ATR SL) + CH wait."""
    # 1. EMA_RSI_SHORT fires on EMA9 pullback touch with ATR-scaled SL; rejects stretched (>2.5% from EMA21), heavy neg funding, no pullback/breakout
    base_s = dict(
        m15_complete=True, cur_px=9.85, c15_now=9.86, c15_prev=9.95,
        hi15_now=9.91, lo15_now=9.83, qv15_now=300_000, qv15_avg20=150_000,
        ema9_15m=9.90, ema21_15m=10.05, sma50_15m=10.20, rsi15m_now=35.0,
        low16_15m=9.70, res16_15m=10.30,
        ema9_1h=10.00, ema21_1h=10.15, rsi1h_now=36.0,
        atr14_1h_pct=0.022, ret_24h=-0.06, btc_ret24=-0.01, f_day=-0.0004,
    )
    hs = agy.evaluate_v23_ema_rsi_mood(**base_s)
    assert len(hs) == 1 and hs[0]["setup"] == "EMA_RSI_SHORT" and hs[0]["side"] == "SHORT"
    assert hs[0]["trigger"] == "PULLBACK" and hs[0]["stop_pct"] == 0.055  # 2.5 * 0.022 = 5.5%
    # Reject stretched > 2.5% below EMA21 (cur_px=9.75 vs ema21=10.05 -> -2.99%)
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_s, "cur_px": 9.75, "c15_now": 9.76}) == []
    # Reject heavy negative funding (< -0.15%/day like BARD -0.71%/day)
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_s, "f_day": -0.0020}) == []
    # Reject mid-air bar with neither pullback touch nor fresh 16-bar breakdown
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_s, "hi15_now": 9.87, "low16_15m": 9.70}) == []
    # Accept fresh 16-bar breakdown even without EMA9 touch (and clamp ATR SL to [3.5%, 6.5%])
    hs_brk = agy.evaluate_v23_ema_rsi_mood(**{**base_s, "hi15_now": 9.87, "low16_15m": 9.90, "atr14_1h_pct": 0.010})
    assert len(hs_brk) == 1 and hs_brk[0]["trigger"] == "BREAKOUT" and hs_brk[0]["stop_pct"] == 0.035
    # Reject low vol & incomplete 15m
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_s, "qv15_now": 100_000}) == []
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_s, "m15_complete": False}) == []

    # 2. EMA_RSI_LONG fires on EMA9 pullback OR fresh 16-bar breakout; RS vs BTC boosts leader score; rejects stretched & crowded funding
    base_l = dict(
        m15_complete=True, cur_px=10.16, c15_now=10.15, c15_prev=10.05,
        hi15_now=10.19, lo15_now=10.08, qv15_now=400_000, qv15_avg20=150_000,
        ema9_15m=10.10, ema21_15m=9.95, sma50_15m=9.80, rsi15m_now=64.0,
        low16_15m=9.70, res16_15m=10.30,
        ema9_1h=10.05, ema21_1h=9.90, rsi1h_now=62.0,
        atr14_1h_pct=0.030, ret_24h=0.09, btc_ret24=-0.02, f_day=0.0002,
    )
    hl = agy.evaluate_v23_ema_rsi_mood(**base_l)
    assert len(hl) == 1 and hl[0]["setup"] == "EMA_RSI_LONG" and hl[0]["side"] == "LONG" and hl[0]["exit_mode"] == "V22_LONG_SQZ96"
    assert hl[0]["trigger"] == "PULLBACK" and hl[0]["stop_pct"] == 0.065  # 2.5 * 0.030 = 7.5% clamped to 6.5%
    hl_weak_rs = agy.evaluate_v23_ema_rsi_mood(**{**base_l, "ret_24h": 0.015, "btc_ret24": 0.02})
    assert len(hl_weak_rs) == 1 and hl[0]["score"] > hl_weak_rs[0]["score"]  # strong RS vs BTC outranks laggard
    # Reject stretched > 2.5% above EMA21 (cur_px=10.25 vs ema21=9.95 -> +3.02%)
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_l, "cur_px": 10.25, "c15_now": 10.22}) == []
    # Reject crowded positive funding (> +0.10%/day) & overbought RSI > 74
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_l, "f_day": 0.0015}) == []
    assert agy.evaluate_v23_ema_rsi_mood(**{**base_l, "rsi15m_now": 79.0}) == []
    # Accept fresh 16-bar breakout without pullback touch
    hl_brk = agy.evaluate_v23_ema_rsi_mood(**{**base_l, "lo15_now": 10.13, "res16_15m": 10.10})
    assert len(hl_brk) == 1 and hl_brk[0]["trigger"] == "BREAKOUT"

    # 3. ClickHouse 15m pre-flight wait polls until >= 95% complete
    orig_ch = agy.market.ch
    orig_sleep = agy.time.sleep
    calls = []
    try:
        seq = [[("200", "120")], [("200", "196")]]
        def _fake_ch(sql, **kw):
            calls.append(kw)
            return seq[min(len(calls) - 1, len(seq) - 1)]
        agy.market.ch = _fake_ch
        agy.time.sleep = lambda s: None
        now_u = dt.datetime(2026, 11, 25, 0, 20, 0, tzinfo=dt.timezone.utc)
        res = agy.wait_for_clickhouse_15m(now_u, max_wait_sec=30, poll_sec=5, min_ratio=0.95, min_syms=100, dry_run=False)
        assert res["ready"] is True and res["cur_cnt"] == 196 and res["pct"] == 98.0 and len(calls) == 2
        assert calls[0]["cur_t"] == "2026-11-25 00:00:00" and calls[0]["prev_t"] == "2026-11-24 23:45:00"
    finally:
        agy.market.ch = orig_ch
        agy.time.sleep = orig_sleep


def test_v23_r0_2x_unlimited_and_auto_cb():
    """Verify 2026-11-26 R0: 2x isolated (Margin 100 -> 200U), MAX_OPEN=0 opens beyond 8 until avail < 101, and Auto Safe CB."""
    import contextlib
    import io
    import json
    import tempfile

    # 1. _execute_open sends usdt="200", leverage=2 to Order Proxy
    orig_fetch = agy.binance.fetch_portfolio
    orig_save = agy.binance.save_portfolio
    orig_post = agy.binance.post
    orig_spr = agy.fetch_live_spread
    orig_st = agy.STATE_PATH
    orig_pm = agy.POS_META_PATH
    orig_jrn = agy.JOURNAL_PATH
    orig_chat = agy._append_chat_line
    orig_pfp = agy.binance.PORTFOLIO
    orig_lk = agy.LOCK_PATH
    orig_sync = agy._sync_and_portfolio
    orig_eval = agy._evaluate_open_positions
    orig_scan = agy._scan_market_and_candidates
    orig_chw = agy.wait_for_clickhouse_15m
    try:
        with tempfile.TemporaryDirectory() as td:
            agy.STATE_PATH = os.path.join(td, "st.json")
            agy.POS_META_PATH = os.path.join(td, "pm.json")
            agy.JOURNAL_PATH = os.path.join(td, "jrn.jsonl")
            agy.binance.PORTFOLIO = os.path.join(td, "pf.json")
            agy.LOCK_PATH = os.path.join(td, "lock")
            agy._append_chat_line = lambda *a, **k: None
            pf_9 = {
                "equity_usdt": "916.80", "available_usdt": "350.00",
                "held_symbols": [f"H{i}USDT" for i in range(8)],
                "shorts": [{"id": f"h{i}", "symbol": f"H{i}USDT", "qty": "1", "entry": "100", "on_binance": True} for i in range(8)],
                "orders": [], "closed": [],
            }
            agy.write_json_atomic(agy.binance.PORTFOLIO, pf_9)
            agy.write_json_atomic(agy.STATE_PATH, {"baseline_equity_usdt": "900.00", "peak_equity_usdt": "916.80", "circuit_breaker_tripped": False})
            agy.write_json_atomic(agy.POS_META_PATH, {})
            agy.binance.fetch_portfolio = lambda: pf_9
            agy.binance.save_portfolio = lambda p: p
            agy.fetch_live_spread = lambda s: Decimal("0.001")
            sent_bodies = []
            def _fake_post(path, body):
                if path == "/open":
                    sent_bodies.append(body)
                    return {"status": "placed", "cid": f"bto-2026-11-26-{body['symbol'][:-4]}", "line": "placed"}
                return {"ok": True}
            agy.binance.post = _fake_post
            now_u = dt.datetime(2026, 11, 26, 2, 30, 0, tzinfo=dt.timezone.utc)
            ok, res, _ = agy._execute_open("NEW9USDT", "0.050", "0.30", "test 2x 9th pos", setup="EMA_RSI_LONG", side="LONG", now_utc=now_u)
            assert ok is True, res
            assert len(sent_bodies) == 1 and sent_bodies[0]["usdt"] == "200" and sent_bodies[0]["leverage"] == 2 and sent_bodies[0]["side"] == "LONG", sent_bodies

            # 2. cmd_auto_trade(max_opens=0, dry_run=True) with 8 existing positions + avail=350 opens #9, #10, #11 and stops when sim_avail drops to 50 < 101!
            agy._sync_and_portfolio = lambda now, do_sync=True: (pf_9, {"ok": True}, agy.read_json(agy.STATE_PATH, default={}), {"tripped": False})
            agy._evaluate_open_positions = lambda pf, now, bars_out=None, flow_out=None: []
            agy.wait_for_clickhouse_15m = lambda now, **kw: {"ready": True, "cur_cnt": 200, "prev_cnt": 200, "pct": 100.0, "waited_sec": 0}
            cands_5 = [
                {
                    "symbol": f"N{i}USDT", "coin": f"N{i}", "side": "LONG" if i % 2 == 0 else "SHORT",
                    "primary_setup": "EMA_RSI_LONG" if i % 2 == 0 else "EMA_RSI_SHORT",
                    "score": 250.0 - i, "price": 10.0, "ret_24h_pct": 2.0, "ret_7d_pct": 5.0,
                    "suggested_stop_pct": 0.050, "suggested_tp_pct": 0.30,
                    "exit_mode": "V22_LONG_SQZ96" if i % 2 == 0 else "V18_AA_SQZ96",
                }
                for i in range(5)
            ]
            agy._scan_market_and_candidates = lambda pf, now, held_bars_15m=None: (
                {"regime_label": "NEUTRAL_RANGE", "funding_ok": True, "complete_1h_pct": 100.0, "complete_15m_pct": 100.0, "btc": {"price": 62000}, "breadth": {"pct_above_1d_sma20": 50.0, "pct_above_1h_ema20": 50.0}},
                cands_5, [], [], {},
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = agy.cmd_auto_trade(max_opens=0, dry_run=True)
            assert rc == 0
            out = json.loads(buf.getvalue())
            assert [x["symbol"] for x in out["opened"]] == ["N0USDT", "N1USDT", "N2USDT"], out  # 350 -> 250 -> 150 -> 50 (< 101 stop!)
            assert any(s["symbol"] == "N3USDT" and "avail_below_101" in s["reason"] for s in out["skipped_risk"]), out["skipped_risk"]
            assert out["leverage"] == 2 and out["margin_usdt"] == "100" and out["lot_usdt"] == "200" and out["min_avail_usdt"] == "101"
            assert out["max_open"] == 0 and out["max_long_open"] == 0 and out["max_short_open"] == 0
            assert out["cb_auto"] is True and out["cb_ref_equity_usdt"] == "916.80" and out["cb_limit_usdt"] == "68.76" and out["cb_limit_pct"] == "7.50"
    finally:
        agy.binance.fetch_portfolio = orig_fetch
        agy.binance.save_portfolio = orig_save
        agy.binance.post = orig_post
        agy.fetch_live_spread = orig_spr
        agy.STATE_PATH = orig_st
        agy.POS_META_PATH = orig_pm
        agy.JOURNAL_PATH = orig_jrn
        agy._append_chat_line = orig_chat
        agy.binance.PORTFOLIO = orig_pfp
        agy.LOCK_PATH = orig_lk
        agy._sync_and_portfolio = orig_sync
        agy._evaluate_open_positions = orig_eval
        agy._scan_market_and_candidates = orig_scan
        agy.wait_for_clickhouse_15m = orig_chw


def run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")


if __name__ == "__main__":
    run_all()
    print("ALL agy_trader tests passed")
