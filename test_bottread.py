"""Run: .venv/Scripts/python test_bottread.py  (no framework, asserts only)."""
import logging
import os
import tempfile

import market

# team.py logs to cache/team.log via basicConfig, a no-op once root has a handler: tests must not
# write there (the app lists its ERROR lines as real problems)
logging.getLogger().addHandler(logging.NullHandler())


def test_dca_math():
    flat = [100.0] * 110
    (roi, under), *_ = market.dca_windows(flat)
    assert abs(roi - (-market.FEE)) < 1e-12 and abs(under - (-market.FEE)) < 1e-12  # flat price: only fees lost
    up = [100.0] * 103 + [200.0]                       # last week doubles: 103 cheap buys + 1 at 200
    roi, _ = market.dca_windows(up)[0]
    expect = ((103 / 100 + 1 / 200) * (1 - market.FEE) * 200) / 104 - 1
    assert abs(roi - expect) < 1e-12
    assert len(market.dca_windows([1.0] * (104 + 8))) == 3  # ends at len, len-4, len-8
    assert market.dca_windows([1.0] * 103) == []


def test_weekly_sampling_ends_today():
    days = [(f"d{i}", float(i + 1)) for i in range(400)]
    w, mayer = market.weekly(days)
    assert w[-1] == 400.0 and w[-2] == 393.0           # latest day kept, then every 7th back
    assert mayer[0] is None and abs(mayer[-1] - 400 / (sum(range(201, 401)) / 200)) < 1e-12


def test_pegged():
    assert market.is_pegged([("d", 1.0 + (i % 3) * 0.001) for i in range(400)])
    assert not market.is_pegged([("d", 1.0 + i * 0.01) for i in range(400)])


def test_short_rule_on_completed_days():
    # C3+4H rule: downtrend perps of any age (> 45d past a -20% drawdown; no 60d cap since 2026-11-08), 30-day geometric-mean volume >= $5M, not pegged
    from datetime import date, timedelta
    day = [date(2026, 6, 1) + timedelta(days=i) for i in range(200)]
    px_down = [30.0 - i * 0.1 for i in range(200)]      # daily downtrend: Close < SMA7 and Close < SMA20
    px_up = [5.0 + i * 0.01 for i in range(200)]        # daily uptrend
    hc_cross = [10.0 + i for i in range(15)] + [16.0]   # prev RSI(14) = 100.0 (>= 65), latest drops to 61.9 (< 65)
    ok = market.short_row("NEWUSDT", "NEW", day[:20], px_down[:20], [8e6] * 20, hc_cross)
    assert ok["fail"] == [] and ok["price"] == 16.0 and ok["rsi_prev"] == 100.0 and abs(ok["rsi"] - 61.9) < 0.1 and abs(ok["vol30_usd"] - 8e6) < 1
    assert ok["trend_1d"] == "ขาลง (ต่ำกว่า SMA7/20)" and ok["downtrend_1d"] is True
    # no age cap since 2026-11-08 (E3): a 120-day coin in a downtrend 40% off its top passes the bleed tier
    old = market.short_row("OLDUSDT", "OLD", day[:120], px_down[:120], [8e6] * 120, hc_cross)
    assert old["fail"] == [] and old["age_days"] == 120 and old["signal"] == "PostPeakBleed (RSI<60)", old
    gold = market.short_row("PAXGUSDT", "PAXG", day[:120], px_down[:120], [8e6] * 120, hc_cross)   # the same chart on a gold perp
    assert gold["fail"] == ["สัญญาทองคำ (ไม่เทรดตามเทรนด์คริปโต)"], gold
    # coins 8..60d in daily uptrend are excluded
    up_fail = market.short_row("UPUSDT", "UP", day[:20], px_up[:20], [8e6] * 20, hc_cross)
    assert up_fail["fail"] == ["กราฟ Day (ราคาปิดเมื่อวาน) ยังไม่เป็นขาลงเต็มตัว (ขาขึ้น (เหนือ SMA7/20))"]
    assert market.short_row("NEWUSDT", "NEW", day[:20], px_down[:20], [8e6] * 20, hc_cross[:15])["fail"] == ["แท่ง 1 ชม. ไม่พอคำนวณ RSI(14)"]
    assert market.short_row("NEWUSDT", "NEW", day[:20], px_down[:20], [8e6] * 20, [10.0 + i for i in range(16)])["fail"] == [
        "RSI(14) 1 ชม. ล่าสุด 100.0 ≥ 65 (ยังไม่ตัดลง)"]
    assert market.short_row("NEWUSDT", "NEW", day[:20], px_down[:20], [8e6] * 20, [30.0 - i for i in range(16)])["fail"] == [
        "RSI(14) 1 ชม. แท่งก่อน 0.0 < 55"]
    assert market.short_row("NEWUSDT", "NEW", day[:20], px_down[:20], [4e6] * 20, hc_cross)["fail"] == ["volume 30 วัน $4.0M < $5M"]
    assert market.short_row("USD1USDT", "USD1", day[:6], [1.0] * 6, [8e6] * 6, hc_cross)["fail"] == ["stablecoin"]
    # a contract relisted after a 3-day gap at 3% of the old price is a new listing: age and volume count from the relist
    old = day[:150] + day[153:173]
    relisted = market.short_row("RELUSDT", "REL", old, [100.0] * 150 + [4.0 - i * 0.05 for i in range(20)], [8e6] * 170, hc_cross)
    assert relisted["fail"] == [] and relisted["age_days"] == 20
    # only the last 30 candles count for volume (geometric mean, so one huge day cannot carry a dead coin)
    v = market.short_row("NEWUSDT", "NEW", day[:40], px_down[:40], [1e9] * 10 + [1e6] * 29 + [1e9], hc_cross)
    assert v["vol30_usd"] < 5e6 and v["fail"] == ["volume 30 วัน $1.3M < $5M"]
    # Backtested Trend-Following Short tiers + 4H RSI<55 (2026-10-15):
    # 1) New listing <= 7 days still at ATH (dd_ath = 0% > -8% and in uptrend) is rejected (anti-pump filter);
    #    once it rolls over >= 8% from peak (e.g. 12.0 -> 10.5 = -12.5%), it passes on 1h RSI<55
    hc_cross60 = [10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 11.5]  # prev ~65.7 -> now ~52.8
    new7_pump = market.short_row("PUMPUSDT", "PUMP", day[:6], px_up[:6], [8e6] * 6, hc_cross60)
    assert new7_pump["fail"] == ["เหรียญใหม่ 6 วัน ยังไม่เริ่มขาลง (ย่อจากยอด +0% ต้อง ≤ -8% หรือต่ำกว่า SMA7/20)"]
    px_new_roll = [10.0, 11.0, 12.0, 11.5, 11.0, 10.5]                                    # peak 12.0 -> 10.5 (dd_ath = -12.5% <= -8%)
    new7 = market.short_row("FRESHUSDT", "FRESH", day[:6], px_new_roll, [8e6] * 6, hc_cross60)
    assert new7["fail"] == [] and new7["age_days"] == 6 and 60.0 <= new7["rsi_prev"] < 70.0 and new7["rsi"] < 60.0
    assert new7["signal"] == "NewListing≤7d (RSI<55)"
    # 2) Young downtrend <= 30d passes on RSI<55; 31..45d passes on RSI<60; 46..60d requires dd_ath <= -20% and RSI<60
    yd30 = market.short_row("YDUSDT", "YD", day[:25], px_down[:25], [8e6] * 25, hc_cross60)
    assert yd30["fail"] == [] and yd30["age_days"] == 25 and yd30["signal"] == "YoungDown≤30d (RSI<55)"
    yd45 = market.short_row("YD45USDT", "YD45", day[:40], px_down[:40], [8e6] * 40, hc_cross60)
    assert yd45["fail"] == [] and yd45["age_days"] == 40 and yd45["signal"] == "YoungDown≤45d (RSI<60)"
    # 46..60d with shallow drawdown (-10% > -20%) is rejected; with deep drawdown (-25% <= -20%) passes
    px_shallow = [20.0 - i * (2.0 / 54) for i in range(55)]   # 20.0 -> 18.0 (-10%)
    shallow = market.short_row("SHALUSDT", "SHAL", day[:55], px_shallow, [8e6] * 55, hc_cross60)
    assert shallow["fail"] == ["อายุ 55 วัน (> 45) ยังย่อจากยอด -10% ไม่ถึง -20%"]
    px_bleed = [20.0 - i * (5.0 / 54) for i in range(55)]     # 20.0 -> 15.0 (-25%)
    bleed = market.short_row("BLEEDUSDT", "BLEED", day[:55], px_bleed, [8e6] * 55, hc_cross60)
    assert bleed["fail"] == [] and bleed["signal"] == "PostPeakBleed (RSI<60)" and abs(bleed["dd_ath"] - (-0.25)) < 1e-6
    # 3) 4H RSI(14) < 55 cross catches a strict aligned downtrend coin (Close < SMA7 <= SMA20 and dd_ath <= -15%)
    hc_cross55 = [10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.5, 10.2, 10.6, 11.0, 11.4, 11.8, 10.5]  # prev ~59.0 (>=55) -> now ~48.6 (<55)
    hc_4h = []
    for v4 in hc_cross55:
        hc_4h.extend([v4] * 4)   # 64 hourly bars: 4h closes match hc_cross55 (crosses <55), while last two 1h closes are flat (10.5, 10.5)
    px_strict = [30.0 - i * 0.25 for i in range(25)]          # 30.0 -> 24.0 (-20% <= -15%, Close < SMA7 < SMA20)
    via4h = market.short_row("FOURHUSDT", "FOURH", day[:25], px_strict, [8e6] * 25, hc_4h)
    assert via4h["fail"] == [] and via4h["signal"] == "YoungDown≤30d (4H-RSI<55)"
    # 4) Funding per day (perp_funding scales each coin's rate to a day, like the backtest's daily sum) < -0.90% is
    #    rejected as crowded short squeeze risk; >= -0.90% passes (-0.45%/day used to fail the old -0.30%/round gate)
    crowded = market.short_row("CROWDUSDT", "CROWD", day[:25], px_down[:25], [8e6] * 25, hc_cross60, funding=-0.0096)
    assert crowded["fail"] == ["funding -0.96%/วัน < -0.90%/วัน (คนรุม short แน่น เสี่ยง squeeze)"], crowded["fail"]
    ok_fund = market.short_row("OKFUNDUSDT", "OKFUND", day[:25], px_down[:25], [8e6] * 25, hc_cross60, funding=-0.0045)
    assert ok_fund["fail"] == [] and ok_fund["funding"] == -0.0045
    # 5) Established coins (> 60d) pass SHORT like any age since 2026-11-08 (E3);
    #    when market regime is BULL (allow_long=True), strong uptrend breakouts pass as LONG
    px_aligned_old = [100.0 - i * 0.6 for i in range(90)]     # 100 -> 46.6 (dd_ath = -53.4%, age 90d > 60d)
    aligned_old = market.short_row("CRVUSDT", "CRV", day[:90], px_aligned_old, [8e6] * 90, hc_cross60)
    assert aligned_old["fail"] == [] and aligned_old["side"] == "SHORT" and aligned_old["signal"].startswith("PostPeakBleed"), aligned_old
    # 6) Bi-Directional Trend-Following LONG signals when market regime allows LONG:
    bull_regime = {"trend": "BULL", "trend_th": "ขาขึ้น (BTC Bull + ตลาดเขียว)", "allow_long": True, "allow_short": True}
    hc_up55 = [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 10.8]  # prev ~41 (<56) -> now ~58 (>=55, <=74)
    hc_up_full = [10.0] * 10 + hc_up55   # 26 bars so chg_24h = 10.8 / 10.0 - 1 = +8% (within +2%..+25%)
    px_ath_bo = [10.0 + i * 0.15 for i in range(60)]          # steady uptrend at ATH (dd_ath = 0%, r7 > +6%, r14 > +10%, Close > SMA7 > SMA20)
    long_ath = market.short_row("SUIUSDT", "SUI", day[:60], px_ath_bo, [15e6] * 60, hc_up_full, funding=0.0001, mkt_regime=bull_regime)
    assert long_ath["fail"] == [] and long_ath["side"] == "LONG" and long_ath["signal"] == "ATH-Breakout (LONG RSI>55)", long_ath
    market.LONG_ON = False   # live (ลุงจืด 2026-11-08): SHORT only, the same coin gives its SHORT row
    try:
        assert market.short_row("SUIUSDT", "SUI", day[:60], px_ath_bo, [15e6] * 60, hc_up_full, funding=0.0001, mkt_regime=bull_regime)["side"] == "SHORT"
    finally:
        market.LONG_ON = True
    px_ath_dip = px_ath_bo[:50] + [20.0] + px_ath_bo[51:]     # spike 20.0 then latest 18.85 (dd_ath = -5.75%: old -4% gate failed, new -8% passes)
    long_dip = market.short_row("SUIUSDT", "SUI", day[:60], px_ath_dip, [15e6] * 60, hc_up_full, funding=0.0001, mkt_regime=bull_regime)
    assert long_dip["fail"] == [] and long_dip["signal"] == "ATH-Breakout (LONG RSI>55)", long_dip
    px_young_up = [12.2] + [9.5 + i * 0.08 for i in range(19)]  # peak 12.2, latest 10.94 (dd_ath = -10.3% in -12%..-8%), Close > SMA7 > SMA20
    long_young = market.short_row("TAOUSDT", "TAO", day[:20], px_young_up, [15e6] * 20, hc_up_full, funding=0.0001, mkt_regime=bull_regime)
    assert long_young["fail"] == [] and long_young["side"] == "LONG" and long_young["signal"] == "YoungUptrend≤30d (LONG RSI>50)", long_young
    # Market regime detector: BTC 1h uptrend + breadth >= 50% -> BULL (allow_long=True, allow_short=False)
    reg_bull = market.market_regime({"BTCUSDT": ([], [60000.0 + i * 100.0 for i in range(60)]),
                                     "ETHUSDT": ([], [3000.0 + i * 10.0 for i in range(30)]),
                                     "SOLUSDT": ([], [150.0 + i * 1.0 for i in range(30)])})
    assert reg_bull["btc_up_1h"] is True and reg_bull["allow_long"] is True and reg_bull["allow_short"] is False, reg_bull
    reg_bear = market.market_regime({"BTCUSDT": ([], [60000.0 - i * 100.0 for i in range(60)]),
                                     "ETHUSDT": ([], [3000.0 - i * 10.0 for i in range(30)]),
                                     "SOLUSDT": ([], [150.0 - i * 1.0 for i in range(30)])})
    assert reg_bear["btc_up_1h"] is False and reg_bear["allow_long"] is False and reg_bear["allow_short"] is True, reg_bear



def test_rsi():
    assert market.rsi(list(range(1, 30))) == 100.0
    assert market.rsi([1.0] * 5) is None


def test_fit_coerces_model_json():
    import ollama
    schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "sentiment": {"type": "integer", "minimum": -2, "maximum": 2},
        "risk": {"type": "string", "enum": ["none", "delist"]}, "veto": {"type": "boolean"}}}}}}
    got = ollama.fit([{"sentiment": {"FOO": -2, "BAR": 1}, "risk": "DELIST", "veto": "true"}, {"sentiment": 9, "risk": "??"}], schema)
    assert got == {"items": [{"sentiment": -2, "risk": "delist", "veto": True}, {"sentiment": 2, "risk": "none", "veto": False}]}
    assert ollama.parse_json('```json\n{"a": 1}\n```') == {"a": 1} == ollama.parse_json(' {"a": 1} ')


def test_own_chart_fallback_data():
    import json
    import chart
    days = [(f"2020-01-{i:04d}", float(i + 1)) for i in range(250)]
    page = chart.own_chart_html(days)
    close, sma = [json.loads(x) for x in __import__("re").findall(r"setData\((\[.*?\])\)", page)]
    assert len(close) == 250 and len(sma) == 51                    # MA200 starts at day 200
    assert sma[0] == {"time": "2020-01-0199", "value": sum(range(1, 201)) / 200}
    assert set(chart.IMAGE_NOTE) == {"own", "own_ma200", "none"}
    tried, real_shoot, real_chat = [], chart._shoot, chart.ollama.chat
    chart._shoot = lambda page, name, png, waits, min_bytes: tried.append(name) or name == "FOOUSDT.html"
    try:
        assert chart.snapshot("FOOUSDT", "cache", days)[1] == "own_ma200"   # 250 days: the MA is on the picture
        assert chart.snapshot("FOOUSDT", "cache", days[:30])[1] == "own"    # a young perp: no MA, the note says so
        assert chart.snapshot("BARUSDT", "cache", days) == (None, "none")
        assert tried == ["FOOUSDT.html", "FOOUSDT.html", "BARUSDT.html"]     # our ClickHouse chart only, no TradingView
        # when a >15 KB png for the same (symbol, last_day, len(days)) already exists, snapshot reuses it without launching Edge
        tmp_charts = tempfile.mkdtemp()
        cached_png = os.path.join(tmp_charts, f"FOOUSDT-D-{days[-1][0].replace('-', '')}-{len(days)}-20261015-0800.png")
        with open(cached_png, "wb") as f:
            f.write(b"x" * 16_000)
        assert chart.snapshot("FOOUSDT", tmp_charts, days) == (cached_png, "own_ma200") and len(tried) == 3
        # the AI describes the shape; a number it wrote that isn't in its own input is blanked
        chart.ollama.chat = lambda *a, **k: {"trend": "down", "note_th": "แนวรับ 0.42 แล้วไหลลง"}
        assert chart.read("m", "FOOUSDT", {}, None, "none") == {"trend": "down", "note_th": ""}
        calls = []
        chart.ollama.chat = lambda *a, **k: calls.append(1) or {"trend": "down", "note_th": "พุ่งแล้วย่อ ต่ำกว่าเส้นเฉลี่ย 200 วัน"}
        assert chart.read("m", "FOOUSDT", {}, None, "own_ma200")["note_th"].startswith("พุ่ง") and len(calls) == 1
        real_rc = chart.READINGS_CACHE
        chart.READINGS_CACHE = os.path.join(tmp_charts, "chart-readings.json")
        try:
            assert chart.read("m", "FOOUSDT", {}, cached_png, "own_ma200")["note_th"].startswith("พุ่ง") and len(calls) == 2
            assert chart.read("m", "FOOUSDT", {}, cached_png, "own_ma200")["note_th"].startswith("พุ่ง") and len(calls) == 2  # cached!
        finally:
            chart.READINGS_CACHE = real_rc
    finally:
        chart._shoot, chart.ollama.chat = real_shoot, real_chat


def test_clock_reasks_until_plan_written():
    import tempfile
    from datetime import datetime, timezone
    import team
    team.REPORTS = tempfile.mkdtemp()
    t = datetime(2026, 10, 11, 1, 0, tzinfo=timezone.utc)
    assert team.dca_due(t.replace(hour=0, minute=0), set()) is None      # before the daily close + 30m
    assert team.dca_due(t, set()) == "2026-10-11"                         # restart with no plan -> ask again
    assert team.dca_due(t, {"2026-10-11"}) is None                        # already asked in this process
    os.makedirs(os.path.join(team.REPORTS, "2026-10-11"))
    open(os.path.join(team.REPORTS, "2026-10-11", "dca-plan.md"), "w").close()
    assert team.dca_due(t, set()) is None                                 # plan exists -> done


def test_quant_picks_youngest_not_held_every_hour():
    # the code picks (never the LLM): passing coins youngest first, minus what the server already shorts or cooled down after a loss,
    # minus names the server cannot trade (a Chinese name is fine, a 21-letter base does not fit an id); every day (only the CIO's short list waits for complete data)
    import json
    import tempfile
    from datetime import datetime, timezone
    import bus
    import team
    got, sent = {}, []

    def rows():
        return [{"symbol": s, "base": s[:-4], "price": 1.0, "age_days": a, "vol30_usd": 3e7, "r7": None, "fail": []}
                for s, a in (("AUSDT", 8), ("牛来USDT", 9), ("BUSDT", 10), ("DUSDT", 10), ("CUSDT", 11), ("X" * 21 + "USDT", 12))]
    real = bus.listen, bus.send, market.short_screen, team.held_shorts, team.REPORTS, team.binance.PORTFOLIO, team.now
    bus.listen = lambda group, topics, handle, **kw: got.update(handle=handle)
    bus.send = lambda t, m: sent.append(m) if t == "bt.dca.candidates" else None
    tmp_dir = tempfile.mkdtemp()
    pf_path = os.path.join(tmp_dir, "portfolio.json")
    with open(pf_path, "w", encoding="utf-8") as f:
        json.dump({"closed": [
            {"symbol": "DUSDT", "coin": "D", "pnl_usdt": "-1.25", "closed_at": "2026-10-12T00:00:00+00:00"},   # loss 12 h ago -> cooldown (1 day)
            {"symbol": "AUSDT", "coin": "A", "pnl_usdt": "2.50", "closed_at": "2026-10-11T12:00:00+00:00"},    # profit -> not cooled
            {"symbol": "CUSDT", "coin": "C", "pnl_usdt": "-0.50", "closed_at": "2026-10-11T11:00:00+00:00"},   # loss 25 h ago -> expired
        ]}, f)
    team.held_shorts, team.REPORTS, team.binance.PORTFOLIO = (lambda: {"BUSDT"}), tmp_dir, pf_path
    team.now = lambda: datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    try:
        assert team.cooldown_shorts() == {"DUSDT"}
        team.quant()
        for entry in (True, False):
            market.short_screen = lambda: (rows(), {"universe": 5, "data_to": "2026-10-11", "entry_day": entry, "late": []})
            got["handle"]([("bt.dca.request", {"run_id": "2026-10-12"})])
        on, off = sent
        assert [p["symbol"] for p in on["picks"]] == ["AUSDT", "牛来USDT", "CUSDT"], on["picks"]
        assert on["passing_coins"] == ["A", "牛来", "B", "D", "C"] and on["held"] == ["B"] and on["cooled"] == ["D"] and on["entry_day"] is True
        assert off["picks"] == on["picks"] and off["passing"] == 5 and off["entry_day"] is False
        team.OPEN_NEW = False
        for trend in ("🔴 ตลาดขาลง/พักตัว (BTC 1H พักตัว/ขาลง · ตลาดบวก 6%) — เปิด SHORT / งดเปิด LONG ใหม่",
                      "🟢 ตลาดขาขึ้น (BTC 1H EMA21>50 · ตลาดบวก 60%) — เปิดได้ทั้ง LONG และ SHORT ตามเทรนด์", "ขาลง/พักตัว (เน้นเปิด SHORT)"):
            market.short_screen = lambda: (rows(), {"universe": 5, "data_to": "2026-10-11", "entry_day": True, "late": [], "mkt_trend": trend})
            got["handle"]([("bt.dca.request", {"run_id": "2026-10-12"})])
            t = sent[-1]["mkt_trend"]
            assert t.endswith(" — " + team.PAUSED_TH) and t.count("—") == 1 and "เปิด SHORT" not in t and "เปิดได้" not in t, t
        assert sent[-3]["mkt_trend"].startswith("🔴 ตลาดขาลง/พักตัว (BTC 1H พักตัว/ขาลง · ตลาดบวก 6%) — ⛔"), sent[-3]
        team.OPEN_NEW, trend = True, "🔴 ตลาดขาลง/พักตัว (BTC 1H พักตัว/ขาลง · ตลาดบวก 6%) — เปิด SHORT / งดเปิด LONG ใหม่"
        got["handle"]([("bt.dca.request", {"run_id": "2026-10-12"})])
        assert sent[-1]["mkt_trend"] == trend, sent[-1]
    finally:
        team.OPEN_NEW = True
        bus.listen, bus.send, market.short_screen, team.held_shorts, team.REPORTS, team.binance.PORTFOLIO, team.now = real


def test_each_screenshot_gets_its_own_edge_profile():
    # a shared --user-data-dir hands the shot to a still-running Edge and returns before the png exists -> "none"
    import chart
    dirs, real = [], chart.subprocess.run
    chart.subprocess.run = lambda args, **kw: dirs.append(next(a for a in args if a.startswith("--user-data-dir=")))
    try:
        chart._shoot("<p>x</p>", "t1.html", os.path.abspath("cache/none.png"), (1,), 1)
        chart._shoot("<p>x</p>", "t2.html", os.path.abspath("cache/none.png"), (1,), 1)
    finally:
        chart.subprocess.run = real
    assert len(dirs) == 2 and dirs[0] != dirs[1]
    assert not any(os.path.exists(d.split("=", 1)[1]) for d in dirs)      # temp profiles cleaned up


def test_seed_local_state_writes_password_manager_fields():
    import json
    import shutil
    import chart
    tmp = tempfile.mkdtemp()
    old_local = os.environ.get("LOCALAPPDATA")
    real_run = chart.subprocess.run
    try:
        os.environ["LOCALAPPDATA"] = tmp
        chrome_dir = os.path.join(tmp, "Google", "Chrome", "User Data")
        edge_dir = os.path.join(tmp, "Microsoft", "Edge", "User Data")
        os.makedirs(chrome_dir, exist_ok=True)
        os.makedirs(edge_dir, exist_ok=True)

        # 1. Chrome Local State -> copies only password_manager.os_password_blank + os_password_last_changed
        with open(os.path.join(chrome_dir, "Local State"), "w", encoding="utf-8") as f:
            json.dump({"os_crypt": {"encrypted_key": "secret"}, "password_manager": {
                "os_password_blank": False, "os_password_last_changed": "13380000000000000"}}, f)
        prof1 = os.path.join(tmp, "p1")
        os.makedirs(prof1)
        if os.name == "nt":
            assert chart._seed_local_state(prof1) is True
            with open(os.path.join(prof1, "Local State"), encoding="utf-8") as f:
                seeded1 = json.load(f)
            assert seeded1 == {"password_manager": {"os_password_blank": False, "os_password_last_changed": "13380000000000000"}}

        # 2. Edge Local State lacks password_manager -> msedge falls back to Chrome Local State
        with open(os.path.join(edge_dir, "Local State"), "w", encoding="utf-8") as f:
            json.dump({"browser": {"enabled_labs_experiments": []}}, f)
        prof2 = os.path.join(tmp, "p2")
        os.makedirs(prof2)
        if os.name == "nt":
            assert chart._seed_local_state(prof2, r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") is True
            with open(os.path.join(prof2, "Local State"), encoding="utf-8") as f:
                seeded2 = json.load(f)
            assert seeded2["password_manager"]["os_password_blank"] is False

        # 3. Incomplete / corrupt Local State -> does not raise and does not create Local State
        with open(os.path.join(chrome_dir, "Local State"), "w", encoding="utf-8") as f:
            json.dump({"password_manager": {"os_password_blank": False, "os_password_last_changed": ""}}, f)
        with open(os.path.join(edge_dir, "Local State"), "w", encoding="utf-8") as f:
            f.write("{corrupt")
        prof3 = os.path.join(tmp, "p3")
        os.makedirs(prof3)
        assert chart._seed_local_state(prof3, "msedge.exe") is False
        assert not os.path.exists(os.path.join(prof3, "Local State"))

        # 4. _shoot seeds Local State into the temp --user-data-dir before spawning the browser
        with open(os.path.join(chrome_dir, "Local State"), "w", encoding="utf-8") as f:
            json.dump({"password_manager": {"os_password_blank": False, "os_password_last_changed": "13380000000000000"}}, f)
        seen = []

        def fake_run(args, **kw):
            udir = next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir="))
            ls = os.path.join(udir, "Local State")
            if os.path.exists(ls):
                with open(ls, encoding="utf-8") as f:
                    seen.append(json.load(f))
            else:
                seen.append(None)

        chart.subprocess.run = fake_run
        chart._shoot("<p>seed</p>", "t_seed.html", os.path.join(tmp, "none.png"), (1,), 1)
        if os.name == "nt":
            assert seen and isinstance(seen[0], dict) and seen[0]["password_manager"]["os_password_blank"] is False
    finally:
        chart.subprocess.run = real_run
        if old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = old_local
        t_seed = os.path.join(chart.ROOT, "cache", "tv", "t_seed.html")
        if os.path.exists(t_seed):
            os.remove(t_seed)
        shutil.rmtree(tmp, ignore_errors=True)


def test_settle_waits_for_empty_queues_idle_members_and_plan():
    # `team.py once` stops after settle(): never while a group has queued/in-hand work or the plan is missing
    import bus
    import team
    states = [(1, set(), True), (0, {"cio"}, True), (0, set(), True), (0, set(), False), (0, set(), True), (0, set(), True)]
    i = [-1]

    def flush():   # settle() calls it once at the start of every look
        i[0] += 1
        bus.working.clear()
        bus.working.update(states[i[0]][1])
    real = bus.flush, bus.lag, dict(bus.groups)
    bus.flush, bus.lag = flush, lambda g, t: states[i[0]][0]
    bus.groups.update({"cio": ["bt.dca.review"]})
    try:
        assert team.settle(lambda: states[i[0]][2], max_s=60, every_s=0) is True
        assert i[0] == 5                                                   # two quiet looks in a row after the last busy one
        i[0], states[:] = -1, [(1, set(), True)] * 1000
        assert team.settle(max_s=0.05, every_s=0.01) is False             # never quiet -> gives up
    finally:
        bus.flush, bus.lag = real[0], real[1]
        bus.groups.clear()
        bus.groups.update(real[2])
        bus.working.clear()


def test_settle_rides_out_a_kafka_error():
    # a 10 s broker timeout in lag() used to escape settle() and kill the round silently (pythonw has no stderr)
    import bus
    import team
    n = [0]

    def lag(g, t):
        n[0] += 1
        if n[0] == 1:
            raise RuntimeError("KafkaException _TIMED_OUT")
        return 0
    real = bus.flush, bus.lag, dict(bus.groups)
    bus.flush, bus.lag = (lambda: None), lag
    bus.groups.update({"cio": ["bt.dca.review"]})
    try:
        assert team.settle(max_s=60, every_s=0) is True and n[0] == 3       # failed look = not quiet, then 2 quiet looks
    finally:
        bus.flush, bus.lag = real[0], real[1]
        bus.groups.clear()
        bus.groups.update(real[2])


def test_cio_keeps_a_run_whose_plan_failed_and_retries_it():
    # finish() used to run after pending.pop(): one failed call lost the plan and the bus retry found nothing to do
    import bus
    import market
    import team
    got, sent, calls, chat = {}, [], [0], []

    def send(t, m):
        if t == "bt.team.chat":
            chat.append((m["who"], m["text"]))
        if t == "bt.dca.plan":
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("HTTP 500")
        sent.append(t)
    real = bus.listen, bus.send, team.time.sleep
    bus.listen = lambda group, topics, handle, **kw: got.update(handle=handle)
    bus.send, team.time.sleep = send, (lambda s: None)
    try:
        team.cio()
        review = {"run_id": "2026-10-11", "picks": [], "reviews": [], "universe": 0, "passing": 0,
                  "passing_coins": [], "held": [], "entry_day": False, "data_to": "2026-10-10"}
        got["handle"]([("bt.dca.review", review)])        # no picks -> plan due at once, its send fails
        assert "bt.dca.plan" not in sent
        got["handle"]([])                                  # next idle tick
        assert sent.count("bt.dca.plan") == 1
        # fixed code text under the AI's name says no AI ran (D12)
        assert ("หัวหน้าพอร์ต", "[2026-10-11] (โค้ด ไม่ได้เรียก AI) ไม่มีเหรียญเข้ากฎที่ยังไม่ได้ถือ/ไม่ติดพักรอบนี้ → สรุปแผนเลย") in chat, chat
        assert ("หัวหน้าพอร์ต", "[2026-10-11] (โค้ด ไม่ได้เรียก AI) สรุปแผน: ไม่มี") in chat, chat
    finally:
        bus.listen, bus.send, team.time.sleep = real


def short_row(amt, entry, mark=None, pnl="0.00000000"):
    """A /fapi/v3/positionRisk row (one-way mode: a short has a negative positionAmt)."""
    return {"positionAmt": amt, "entryPrice": entry, "markPrice": mark or entry, "unRealizedProfit": pnl,
            "liquidationPrice": "9.99", "isolatedWallet": "50"}


def futures_order(cid, qty, price, status="FILLED"):
    """A /fapi/v1/order answer written by hand (for fake.orders or a ledger line)."""
    from decimal import Decimal
    import binance
    return {"orderId": 9, "clientOrderId": cid, "status": status, "executedQty": qty, "cumQuote": str(Decimal(qty) * Decimal(price)),
            "avgPrice": price, "updateTime": int(binance.utcnow().timestamp() * 1000)}


def income_row(sym, kind, amount, ms, asset="USDT"):
    return {"symbol": sym, "incomeType": kind, "income": amount, "asset": asset, "time": ms, "info": "", "tranId": ms, "tradeId": ""}


SHORT_KEYS = ("SHORT_LOT_USDT", "SHORT_LIVE", "BINANCE_API_KEY", "BINANCE_API_SECRET", "DCA_PLAN_TOKEN", "DCA_PROXY_HOST")
TOKEN = "t" * 40   # pragma: allowlist secret — test value only


class FakeBinance:
    """Stands in for binance.Client on fapi (and the few spot calls of check/portfolio, told apart by path): records every
    call and keeps the futures account as state, answering in Binance's shapes with money as strings. A filled MARKET
    SELL adds to the short, a reduceOnly BUY takes from it; a test deletes positions[sym] to play a stop, take-profit or
    liquidation fill on Binance's side."""
    offset = 0

    def __init__(self, usdt="1000", bid="2"):
        self.calls, self.posts, self.usdt, self.bid, self.ask, self.spot_usdt = [], [], usdt, bid, None, "0"   # ask None = the bid
        self.symbols = ["TRXUSDT", "BNBUSDT", "DOGEUSDT", "CAKEUSDT", "ADAUSDT", "SOLUSDT"]
        self.step, self.min_qty, self.market_step = "1", "1", "1"
        self.symbol_lots, self.symbol_bids, self.user_trades = {}, {}, {}
        self.orders, self.algos, self.positions, self.income = {}, {}, {}, []
        self.dual = self.multi = self.fill_then_error = self.answer_new = self.answer_no_price = False   # answer_no_price: POST says FILLED with cumQuote 0   # answer_new: POST says NEW, a GET says FILLED   # fill_then_error: post_error comes after the order filled
        self.post_error = self.algo_error = self.margin_error = None
        self.reject, self.unreachable, self.n = {}, set(), 0     # reject: symbol -> 4xx; unreachable: client ids a GET cannot reach
        self.info = {}                                            # symbol -> exchangeInfo fields to override (status, contractType)

    def sync_clock(self):
        pass

    def sent(self, path, method="POST"):
        return [q for m, p, q in self.posts if (m, p) == (method, path)]

    def call(self, method, path, signed=False, **q):
        import binance
        self.calls.append((method, path))
        if method != "GET":
            self.posts.append((method, path, q))
        ms = int(binance.utcnow().timestamp() * 1000)
        if path == "/fapi/v1/time":
            return {"serverTime": ms}
        if path == "/fapi/v1/exchangeInfo":
            out_syms = []
            for s in self.symbols:
                st, mq, mst = self.symbol_lots.get(s, (self.step, self.min_qty, self.market_step))
                notional = "50" if s == "BTCUSDT" else "5"
                out_syms.append({"symbol": s, "status": "TRADING", "contractType": "PERPETUAL",
                                 "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"],
                                 "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001", "minPrice": "0.0001", "maxPrice": "1000000"},
                                             {"filterType": "LOT_SIZE", "minQty": mq, "stepSize": st, "maxQty": "1000000"},
                                             {"filterType": "MARKET_LOT_SIZE", "minQty": mq, "stepSize": mst, "maxQty": "100000"},
                                             {"filterType": "MIN_NOTIONAL", "notional": notional},
                                             {"filterType": "POSITION_RISK_CONTROL", "positionControlSide": "NONE"}],
                                 **self.info.get(s, {})})
            return {"symbols": out_syms}
        if path == "/fapi/v1/ticker/bookTicker":
            b_px, a_px = self.symbol_bids.get(q["symbol"], (self.bid, self.ask or self.bid))
            return {"symbol": q["symbol"], "bidPrice": b_px, "bidQty": "1000", "askPrice": a_px, "askQty": "1000"}
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": self.dual}
        if path == "/fapi/v1/multiAssetsMargin":
            return {"multiAssetsMargin": self.multi}
        if path == "/fapi/v3/account":
            return {"availableBalance": self.usdt, "totalWalletBalance": self.usdt, "totalUnrealizedProfit": "0.00000000",
                    "totalMarginBalance": self.usdt}
        if path == "/fapi/v3/positionRisk":   # a flat row too: Binance lists symbols with open orders
            return [{"symbol": "XRPUSDT", **short_row("0", "0")}] + [{"symbol": s, **p} for s, p in self.positions.items()]
        if path == "/fapi/v1/order" and method == "GET":
            cid = q["origClientOrderId"]
            if cid in self.unreachable:
                raise ConnectionError("fake: Binance unreachable")
            if cid in self.orders:
                return self.orders[cid]
            raise binance.BinanceError(400, {"code": -2013, "msg": "Order does not exist."})
        if path == "/fapi/v1/order" and method == "POST":
            if q["symbol"] in self.reject:
                raise self.reject[q["symbol"]]
            if self.post_error and not self.fill_then_error:
                raise self.post_error
            o = self._fill(q, ms)
            if self.post_error:
                raise self.post_error
            if self.answer_no_price:
                return {**o, "cumQuote": "0"}
            return {**o, "status": "NEW", "executedQty": "0", "cumQuote": "0", "avgPrice": "0.00"} if self.answer_new else o
        if path == "/fapi/v1/order/test":
            return {}
        if path == "/fapi/v1/leverage":
            return {"symbol": q["symbol"], "leverage": q["leverage"], "maxNotionalValue": "1000000"}
        if path == "/fapi/v1/marginType":
            if self.margin_error:
                raise self.margin_error
            return {"code": 200, "msg": "success"}
        if path == "/fapi/v1/algoOrder" and method == "POST":
            if self.algo_error:
                raise self.algo_error
            if q["clientAlgoId"] in self.algos:   # docs: "unique among open orders" (the error code is not documented)
                raise binance.BinanceError(400, {"code": -4116, "msg": "fake: clientAlgoId is duplicated"})
            self.n += 1
            self.algos[q["clientAlgoId"]] = {"algoId": self.n, "clientAlgoId": q["clientAlgoId"], "algoType": q["algoType"],
                                             "symbol": q["symbol"], "side": q["side"], "orderType": q["type"],
                                             "triggerPrice": q["triggerPrice"], "workingType": q["workingType"],
                                             "closePosition": q["closePosition"] == "true", "algoStatus": "NEW"}
            return self.algos[q["clientAlgoId"]]
        if path == "/fapi/v1/algoOrder" and method == "DELETE":
            a = self.algos.pop(q["clientAlgoId"])
            return {"algoId": a["algoId"], "clientAlgoId": a["clientAlgoId"], "code": "200", "msg": "success"}
        if path == "/fapi/v1/openAlgoOrders":
            return list(self.algos.values())
        if path == "/fapi/v1/userTrades":
            return list(self.user_trades.get(q["orderId"], []))
        if path == "/fapi/v1/income":
            got = [r for r in self.income if r["time"] >= q["startTime"] and q.get("symbol", r["symbol"]) == r["symbol"]
                   and q.get("incomeType", r["incomeType"]) == r["incomeType"]]
            return got[(q["page"] - 1) * q["limit"]:q["page"] * q["limit"]]
        if path == "/api/v3/account":
            return {"balances": [{"asset": "USDT", "free": self.spot_usdt, "locked": "0"}]}
        if path == "/sapi/v1/account/apiRestrictions":
            return {"enableReading": True, "enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}
        raise AssertionError(f"FakeBinance: unexpected {method} {path}")

    def _fill(self, q, ms):
        from decimal import Decimal
        qty, sym = Decimal(q["quantity"]), q["symbol"]
        b_px, a_px = self.symbol_bids.get(sym, (self.bid, self.ask or self.bid))
        px = b_px if sym not in self.symbol_bids else (a_px if q["side"] == "BUY" else b_px)
        prev = self.positions.get(sym)
        old_amt = Decimal(prev.get("positionAmt", "0")) if prev else Decimal(0)
        if q.get("reduceOnly") == "true":
            amt = max(old_amt - qty, Decimal(0)) if q["side"] == "SELL" else min(old_amt + qty, Decimal(0))
            entry_px = prev["entryPrice"] if prev else px
        else:
            amt = old_amt - qty if q["side"] == "SELL" else old_amt + qty
            if prev and old_amt * amt > 0:
                entry_px = str((abs(old_amt) * Decimal(prev["entryPrice"]) + qty * Decimal(px)) / abs(amt))
            else:
                entry_px = px
        if amt:
            base_row = prev or {**short_row("0", entry_px), "marginType": "isolated"}
            self.positions[sym] = {**base_row, "positionAmt": str(amt), "entryPrice": entry_px, "markPrice": base_row.get("markPrice") or entry_px}
        else:
            self.positions.pop(sym, None)
        self.n += 1
        o = {"orderId": self.n, "symbol": sym, "clientOrderId": q["newClientOrderId"], "status": "FILLED", "side": q["side"],
             "type": "MARKET", "executedQty": str(qty), "cumQuote": str(qty * Decimal(px)), "avgPrice": px, "time": ms, "updateTime": ms}
        self.orders[q["newClientOrderId"]] = o   # Binance remembers it: a GET by client id now finds it
        return o


def short_row(amt, entry, mark=None, pnl="0.00000000"):
    """A /fapi/v3/positionRisk row (one-way mode: a short has a negative positionAmt)."""
    return {"positionAmt": amt, "entryPrice": entry, "markPrice": mark or entry, "unRealizedProfit": pnl,
            "liquidationPrice": "9.99", "isolatedWallet": "50"}


def futures_order(cid, qty, price, status="FILLED"):
    """A /fapi/v1/order answer written by hand (for fake.orders or a ledger line)."""
    from decimal import Decimal
    import binance
    return {"orderId": 9, "clientOrderId": cid, "status": status, "executedQty": qty, "cumQuote": str(Decimal(qty) * Decimal(price)),
            "avgPrice": price, "updateTime": int(binance.utcnow().timestamp() * 1000)}


def income_row(sym, kind, amount, ms, asset="USDT"):
    return {"symbol": sym, "incomeType": kind, "income": amount, "asset": asset, "time": ms, "info": "", "tranId": ms, "tradeId": ""}


SHORT_KEYS = ("SHORT_LOT_USDT", "SHORT_LIVE", "BINANCE_API_KEY", "BINANCE_API_SECRET", "DCA_PLAN_TOKEN", "DCA_PROXY_HOST")
TOKEN = "t" * 40   # pragma: allowlist secret — test value only


def short_env(fake, **env):
    """Point binance at `fake` with a temp ledger, a clock fixed at 2026-10-11 12:00 UTC (a test may set binance.utcnow
    to move it; restore puts the real one back) and the given env; returns (binance, restore)."""
    from datetime import datetime, timezone
    import binance
    saved = {k: os.environ.get(k) for k in SHORT_KEYS}
    real = binance.Client, binance.LEDGER, binance.utcnow, binance.PORTFOLIO
    os.environ.update({"SHORT_LOT_USDT": "50", "SHORT_LIVE": "0", "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s",
                       "DCA_PLAN_TOKEN": TOKEN, **env})
    binance.Client = lambda key="", secret="", host=binance.FAPI: fake
    tmp = tempfile.mkdtemp()
    binance.LEDGER = os.path.join(tmp, "short-orders.jsonl")
    binance.PORTFOLIO = os.path.join(tmp, "portfolio.json")
    binance.utcnow = lambda: datetime(2026, 10, 11, 12, tzinfo=timezone.utc)

    def restore():
        binance.Client, binance.LEDGER, binance.utcnow, binance.PORTFOLIO = real
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return binance, restore


def test_binance_signature_matches_docs_example():
    # the HMAC worked example from Binance's SPOT API docs (their published demo secret, not a real one)
    import hashlib
    import hmac
    q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    demo = b"NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"  # pragma: allowlist secret
    assert hmac.new(demo, q.encode(), hashlib.sha256).hexdigest() == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_short_settings_reject_a_bad_lot():
    from decimal import Decimal
    binance, restore = short_env(FakeBinance())
    try:
        for bad in ("abc", "-5", "1e3", "0", "0.00", "50.123", "NaN", "5 0"):
            os.environ["SHORT_LOT_USDT"] = bad
            cfg, why = binance.settings()
            assert cfg is None and "SHORT_LOT_USDT" in why, bad
        for off in ("", "  "):
            os.environ["SHORT_LOT_USDT"] = off
            cfg, why = binance.settings()
            assert cfg is None and "ปิดอยู่" in why, off
        os.environ["SHORT_LOT_USDT"] = " 50.25 "
        for live, want in (("1", True), (" 1 ", True), ("0", False), ("true", False), ("yes", False), ("", False)):
            os.environ["SHORT_LIVE"] = live
            cfg, why = binance.settings()
            assert why is None and cfg["lot"] == Decimal("50.25") and cfg["live"] is want, live
    finally:
        restore()


def opens(binance, syms, run_id="2026-10-11", side="SHORT", usdt="50", today=None, lev=1):
    """What the team's open_new sends: one POST /open per coin, here straight to the server's handler. Returns the replies."""
    from datetime import date
    from decimal import Decimal
    return [binance.open_one(run_id, s, side, Decimal(usdt), lev, today or date.fromisoformat(run_id)) for s in syms]


def test_short_dry_run_only_asks_binance_to_validate():
    fake = FakeBinance()
    binance, restore = short_env(fake)
    try:
        out = opens(binance, ["TRXUSDT", "BNBUSDT", "DOGEUSDT", "CAKEUSDT"])
        assert {(m, p) for m, p, q in fake.posts} == {("POST", "/fapi/v1/order/test")}, fake.posts
        assert [(q["symbol"], q["side"], q["type"], q["quantity"], q["newClientOrderId"]) for q in fake.sent("/fapi/v1/order/test")] == [
            ("TRXUSDT", "SELL", "MARKET", "25", "bto-2026-10-11-TRX"), ("BNBUSDT", "SELL", "MARKET", "25", "bto-2026-10-11-BNB"),
            ("DOGEUSDT", "SELL", "MARKET", "25", "bto-2026-10-11-DOGE"), ("CAKEUSDT", "SELL", "MARKET", "25", "bto-2026-10-11-CAKE")
        ], fake.posts                                                                                # 50 USDT / 2
        assert [r["status"] for r in out] == ["tested"] * 4 and all("ตรวจคำสั่งผ่าน" in r["line"] for r in out), out
        assert binance.ledger() == [] and not os.path.exists(binance.LEDGER)
    finally:
        restore()


def test_short_live_opens_isolated_at_the_asked_leverage_with_no_stop_or_take_profit():
    from decimal import Decimal
    fake = FakeBinance(bid="0.3333")
    fake.market_step = "10"                                  # LOT_SIZE and MARKET_LOT_SIZE both apply: the coarser step wins
    binance, restore = short_env(fake, SHORT_LIVE="1", SHORT_LOT_USDT="55")
    fake.margin_error = binance.BinanceError(400, {"code": -4046, "msg": "No need to change margin type."})
    try:
        (r,) = opens(binance, ["TRXUSDT"], usdt="55")
        assert [p for m, p, q in fake.posts] == ["/fapi/v1/leverage", "/fapi/v1/marginType", "/fapi/v1/order"], fake.posts   # ลุงจืด 2026-10-13 "ไม่มี stop เลย"
        lev, margin, sell = [q for m, p, q in fake.posts]
        assert (lev["symbol"], lev["leverage"], margin["symbol"], margin["marginType"]) == ("TRXUSDT", 1, "TRXUSDT", "ISOLATED")
        assert sell == {"symbol": "TRXUSDT", "side": "SELL", "type": "MARKET", "quantity": "160",      # 55 / 0.3333 = 165.0 -> 160
                        "newClientOrderId": "bto-2026-10-11-TRX", "newOrderRespType": "RESULT"}, sell
        rows = binance.ledger()
        assert [(r["kind"], r["how"]) for r in rows] == [("open", "sending"), ("open", "placed")], rows
        assert (rows[-1]["qty"], rows[-1]["avg_price"], Decimal(rows[-1]["quote_qty"])) == ("160", "0.33330000", Decimal("53.328")), rows
        assert (r["status"], r["why"], r["cid"], r["order_status"], r["qty"]) == ("placed", None, "bto-2026-10-11-TRX", "FILLED", "160"), r
        assert "TRX: เปิด short แล้ว 160 TRX" in r["line"], r
        assert fake.positions["TRXUSDT"]["positionAmt"] == "-160"
        # 2x isolated open (ลุงจืดอนุมัติ R0 2026-11-26): leverage=2 is accepted and sent to Binance
        n2 = len(fake.posts)
        (r2,) = opens(binance, ["ADAUSDT"], usdt="55", lev=2)
        assert (r2["status"], r2["order_status"]) == ("placed", "FILLED"), r2
        lev2, margin2, _ = [q for m, p, q in fake.posts[n2:]]
        assert (lev2["symbol"], lev2["leverage"], margin2["symbol"], margin2["marginType"]) == ("ADAUSDT", 2, "ADAUSDT", "ISOLATED")
        # the server's lot is a hard cap: a bigger request is refused before Binance is asked
        n = len(fake.calls)
        (r,) = opens(binance, ["DOGEUSDT"], usdt="55.01")
        assert (r["status"], r["why"]) == ("refused", "cap") and len(fake.calls) == n, r
        # any other margin-type answer (e.g. a cross position already there) stops that coin before the order
        fake.margin_error = binance.BinanceError(400, {"code": -4048, "msg": "Margin type cannot be changed if there exists position."})
        n = len(fake.posts)
        (r,) = opens(binance, ["BNBUSDT"], usdt="55")
        assert [p for m, p, q in fake.posts[n:]] == ["/fapi/v1/leverage", "/fapi/v1/marginType"], fake.posts[n:]
        assert (r["status"], r["why"]) == ("refused", "error") and "ผิดพลาด" in r["line"], r
    finally:
        restore()


def test_short_rerun_never_opens_the_same_short_twice():
    from datetime import date, datetime, timezone
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT"])
        assert opens(binance, ["TRXUSDT"])[0]["why"] == "sent" and len(fake.sent("/fapi/v1/order")) == 1
        assert opens(binance, ["TRXUSDT"], run_id="2026-10-12")[0]["why"] == "mine" and len(fake.sent("/fapi/v1/order")) == 1
        # Binance closed it (liquidation / by hand) and /sync recorded it: the same run id still does not reopen it
        del fake.positions["TRXUSDT"]
        fake.income.append(income_row("TRXUSDT", "REALIZED_PNL", "-25", fake.orders["bto-2026-10-11-TRX"]["updateTime"] + 1))
        binance.sync()
        (r,) = opens(binance, ["TRXUSDT"])
        assert r["line"] == "ข้าม TRX: เปิดของแผน 2026-10-11 ไปแล้ว (bto-2026-10-11-TRX)" and len(fake.sent("/fapi/v1/order")) == 1, r
        # ledger lost: the open order's client id is asked first, so it is still not sent again
        os.remove(binance.LEDGER)
        binance.utcnow = lambda: datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
        (r,) = opens(binance, ["TRXUSDT"], today=date(2026, 10, 11))
        assert r["status"] == "found" and "มีคำสั่ง bto-2026-10-11-TRX อยู่แล้ว" in r["line"] and len(fake.sent("/fapi/v1/order")) == 1, r
        # ... dated when Binance got it (days held, funding)
        sent_at = datetime.fromtimestamp(fake.orders["bto-2026-10-11-TRX"]["time"] / 1000, timezone.utc).isoformat()
        assert binance.ledger(binance.LEDGER)[-1]["at"] == sent_at != binance.utcnow().isoformat()
    finally:
        restore()


def test_short_opens_every_coin_it_is_sent_and_skips_coins_it_must_not_touch():
    fake = FakeBinance()
    fake.positions["BNBUSDT"] = short_row("3", "600")                                         # ลุงจืด's own long, by hand
    fake.algos["bts-2026-10-01-DOGE"] = {"algoId": 1, "clientAlgoId": "bts-2026-10-01-DOGE", "symbol": "DOGEUSDT",
                                         "orderType": "STOP_MARKET", "triggerPrice": "0.3"}   # a stop the ledger does not know
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        out = opens(binance, ["TRXUSDT", "BNBUSDT", "DOGEUSDT", "CAKEUSDT", "ADAUSDT", "SOLUSDT"])
        assert [q["symbol"] for q in fake.sent("/fapi/v1/order")] == ["TRXUSDT", "CAKEUSDT", "ADAUSDT", "SOLUSDT"], out
        assert [r["why"] for r in out] == [None, "held", "stale", None, None, None], out
        assert out[1]["line"] == "ข้าม BNB: มี position บน Binance อยู่แล้ว (ลุงจืดเปิดเอง) — ไม่ยุ่ง", out[1]
        assert not fake.sent("/fapi/v1/algoOrder", "DELETE")
    finally:
        restore()


def test_short_stops_opening_when_the_futures_wallet_runs_out():
    # decided on .88 (1x): a lot needs MARGIN_USDT * 1.01 available; each open sent takes MARGIN_USDT + LOT_USDT * fee
    # (100 + 100 * 0.05%) from what is left,
    # a refused one nothing
    import team
    syms = ["TRXUSDT", "BNBUSDT", "DOGEUSDT"]
    real = team.binance.fetch_portfolio, team.binance.post, team.BTC_HEDGE_ON
    team.BTC_HEDGE_ON = False
    try:
        for usdt, answers, opened in (("101", "placed", 1), ("100.99", "placed", 0), ("201.049", "placed", 1), ("201.05", "placed", 2),
                                      ("101", "refused", 3), ("101", "unclear", 1), ("101", "tested", 1), ("101", "found", 1)):
            sent, lines = [], []
            team.binance.fetch_portfolio = lambda: {"available_usdt": usdt, "shorts": [], "closed": []}
            team.binance.post = lambda path, payload, timeout=300: sent.append(payload) or {"status": answers, "why": None, "line": "x"}
            unsure = team.open_new("2026-10-11", syms, dict.fromkeys(syms, "SHORT"), lines)
            assert len(sent) == opened and unsure is (answers == "unclear"), (usdt, answers, lines)
            assert opened == 3 or "หยุดเปิดใหม่" in lines[-1], (usdt, lines)
        assert sent[0] == {"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "100", "leverage": 1}, sent
    finally:
        team.binance.fetch_portfolio, team.binance.post, team.BTC_HEDGE_ON = real


def test_short_refuses_hedge_or_multi_assets_mode():
    for flag, why, says in (("dual", "hedge", "Hedge mode"), ("multi", "multi", "Multi-Assets mode")):
        fake = FakeBinance()
        setattr(fake, flag, True)
        binance, restore = short_env(fake, SHORT_LIVE="1")
        try:
            (r,) = opens(binance, ["TRXUSDT"])
            assert r["why"] == why and says in r["line"] and fake.posts == [], r
        finally:
            restore()


def test_short_below_binance_minimums_sends_nothing():
    # 5 / 2 -> 2 TRX = 4 USDT < MIN_NOTIONAL 5; 50 / 10 -> 5 TRX < minQty 10; 6 / 2 -> 3 TRX = 6 USDT and 100 / 10 = minQty: fine
    for lot, bid, min_qty, live, ok in (("5", "2", "1", "1", False), ("50", "10", "10", "1", False), ("5", "2", "1", "0", False),
                                        ("6", "2", "1", "0", True), ("100", "10", "10", "0", True)):
        fake = FakeBinance(bid=bid)
        fake.min_qty = min_qty
        binance, restore = short_env(fake, SHORT_LIVE=live, SHORT_LOT_USDT=lot)
        try:
            (r,) = opens(binance, ["TRXUSDT"], usdt=lot)
            if ok:
                assert r["status"] == "tested" and len(fake.sent("/fapi/v1/order/test")) == 1, (lot, r)
            else:
                assert fake.posts == [] and (r["why"], r["line"]) == ("min_qty", f"ข้าม TRX: ไม้ละ {lot}.00 USDT ต่ำกว่าขั้นต่ำของ Binance"), (lot, r)
        finally:
            restore()


def test_short_rejected_open_is_recorded_and_not_counted():
    from datetime import date
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    fake.reject["TRXUSDT"] = binance.BinanceError(400, {"code": -2019, "msg": "Margin is insufficient."})
    try:
        opens(binance, ["TRXUSDT"])
        # a rejected id may be sent again (nothing was placed) and never counts toward the day's cap
        out = opens(binance, ["TRXUSDT", "BNBUSDT", "DOGEUSDT", "CAKEUSDT"])
        assert [q["symbol"] for q in fake.sent("/fapi/v1/order")] == ["TRXUSDT", "TRXUSDT", "BNBUSDT", "DOGEUSDT", "CAKEUSDT"], out
        assert binance.finals()["bto-2026-10-11-TRX"]["how"] == "rejected", binance.ledger()
        assert out[0]["status"] == "rejected" and out[0]["line"].startswith("TRX: Binance ไม่รับคำสั่งเปิด short"), out[0]
        assert sorted(p["symbol"] for p in binance.positions().values()) == ["BNBUSDT", "CAKEUSDT", "DOGEUSDT"]
        assert binance.opens_on(date(2026, 10, 11)) == 3
    finally:
        restore()


def test_short_opens_at_most_20_a_day_counted_by_send_date():
    # ลุงจืด 2026-10-26: at most DAILY_OPEN_CAP opens per server UTC day, counted from the ledger by the day each was sent
    # (not its run id: a round can cross midnight); a rejected one does not count
    from datetime import date, datetime, timezone
    fake = FakeBinance()
    fake.symbols = [f"C{i:02d}USDT" for i in range(25)]
    binance, restore = short_env(fake, SHORT_LIVE="1")
    noon = binance.utcnow
    fake.reject["C02USDT"] = binance.BinanceError(400, {"code": -2019, "msg": "Margin is insufficient."})
    try:
        binance.utcnow = lambda: datetime(2026, 10, 10, 23, 50, tzinfo=timezone.utc)
        opens(binance, fake.symbols[:2], run_id="2026-10-10")                                  # yesterday's
        binance.utcnow = noon
        out = opens(binance, fake.symbols[2:], run_id="2026-10-10", today=date(2026, 10, 11))   # yesterday's run, sent today
        assert binance.DAILY_OPEN_CAP == 20 and [r["why"] for r in out] == [None] * 21 + ["daily_cap"] * 2, out
        assert [r["status"] for r in out[:2]] == ["rejected", "placed"], out[:2]
        assert out[-1]["line"] == "ข้าม C24: วันนี้ (UTC) server เปิดครบ 20 ไม้แล้ว", out[-1]
        assert (binance.opens_on(date(2026, 10, 10)), binance.opens_on(date(2026, 10, 11))) == (2, 20)
        assert len(fake.sent("/fapi/v1/order")) == 23
    finally:
        restore()


def test_short_refuses_a_thin_book():
    # a market order into a book whose ask is more than 0.5% above the bid is refused before anything is sent
    fake = FakeBinance(bid="2")
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        fake.ask = "2.0101"                                                                   # 0.505%
        (r,) = opens(binance, ["TRXUSDT"])
        assert (r["status"], r["why"]) == ("refused", "spread") and fake.posts == [], r
        fake.ask = "2.01"                                                                     # 0.5%: fine; a SHORT sells into the bid,
        out = opens(binance, ["BNBUSDT"]) + opens(binance, ["TRXUSDT"], side="LONG")           # a LONG buys from the ask
        assert [r["status"] for r in out] == ["placed"] * 2, out
        assert [q["quantity"] for q in fake.sent("/fapi/v1/order")] == ["25", "24"], fake.posts   # 50 / 2 = 25; 50 / 2.01 = 24.9 -> 24
    finally:
        restore()


def test_short_timeout_asks_binance_before_reporting_and_never_resends():
    fake = FakeBinance()
    fake.post_error = TimeoutError("read timed out")
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        (r,) = opens(binance, ["TRXUSDT"])
        assert fake.calls.count(("GET", "/fapi/v1/order")) == 2 and len(fake.sent("/fapi/v1/order")) == 1, fake.calls
        assert r["status"] == "unclear" and "ไม่ได้คำตอบ" in r["line"] and binance.finals()["bto-2026-10-11-TRX"]["how"] == "sending", r
        assert binance.positions()["bto-2026-10-11-TRX"]["unclear"] and not fake.sent("/fapi/v1/algoOrder")
        fake.post_error = None
        assert opens(binance, ["TRXUSDT"])[0]["why"] == "sent" and len(fake.sent("/fapi/v1/order")) == 1
        # 408 / 5xx after Binance did fill it: the answer is fetched by client id
        for sym, status in (("BNBUSDT", 503), ("DOGEUSDT", 408)):
            fake.post_error, fake.fill_then_error = binance.BinanceError(status, {"code": -1007, "msg": "Timeout waiting for response"}), True
            (r,) = opens(binance, [sym])
            f = binance.finals()[f"bto-2026-10-11-{sym[:-4]}"]
            assert (r["status"], f["how"], f["status"], f["qty"]) == ("found", "found-after-error", "FILLED", "25"), r
        assert len(fake.sent("/fapi/v1/order")) == 3
    finally:
        restore()


def test_short_filled_answer_without_price_is_unclear_until_asked_again():
    # Binance answers a MARKET order FILLED with cumQuote 0 (CT 2026-10-12, then every first answer in the server ledger):
    # the reply says unclear; /sync (or the next /open) asks Binance (GET) before anything uses it; an old line is fixed the same way
    fake = FakeBinance(bid="0.3333")
    fake.answer_no_price = True
    binance, restore = short_env(fake, SHORT_LIVE="1", SHORT_LOT_USDT="55")
    try:
        (r,) = opens(binance, ["TRXUSDT"], usdt="55")
        assert (r["status"], r["order_status"], r["quote_qty"]) == ("unclear", "FILLED", "0"), r
        assert binance.positions()["bto-2026-10-11-TRX"]["unclear"] and "รอราคาจาก Binance" in r["line"] and "มูลค่า 0.00" not in r["line"], r
        fake.orders["bto-2026-10-11-BNB"] = o = futures_order("bto-2026-10-11-BNB", "314", "0.31758")   # CT as it is on the server
        fake.positions["BNBUSDT"] = short_row("-314", "0.31758")
        binance.record("2026-10-11", "BNBUSDT", "open", {**o, "cumQuote": "0"}, "placed")
        out = binance.sync()
        trx, bnb = binance.positions()["bto-2026-10-11-TRX"], binance.positions()["bto-2026-10-11-BNB"]
        assert (trx["entry"], bnb["entry"]) == (binance.dec("0.3333"), binance.dec("0.31758")), binance.ledger()
        assert out["status"] == "ok" and out["unclear"] == 0 and not (trx["unclear"] or bnb["unclear"] or fake.sent("/fapi/v1/algoOrder")), out
    finally:
        restore()


def test_short_unanswered_order_is_settled_by_asking_binance():
    # a final 'sending' line is looked up by /sync: found -> its real outcome dated when it was sent; not found and
    # 10+ minutes old -> rejected (never placed); otherwise it stays unclear
    from datetime import datetime, timezone
    fake = FakeBinance()
    fake.post_error = TimeoutError("read timed out")
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        assert [r["status"] for r in opens(binance, ["TRXUSDT", "BNBUSDT"])] == ["unclear", "unclear"]   # both time out
        fake.post_error = None
        fake.orders["bto-2026-10-11-TRX"] = futures_order("bto-2026-10-11-TRX", "25", "2")    # TRX did fill after all
        fake.positions["TRXUSDT"] = short_row("-25", "2")
        binance.utcnow = lambda: datetime(2026, 10, 11, 12, 5, tzinfo=timezone.utc)
        lines = binance.sync()["lines"]
        f = binance.finals()
        assert (f["bto-2026-10-11-TRX"]["how"], f["bto-2026-10-11-BNB"]["how"]) == ("found-after-error", "sending"), lines
        assert f["bto-2026-10-11-TRX"]["at"] == "2026-10-11T12:00:00+00:00" and not binance.positions()["bto-2026-10-11-TRX"]["unclear"]
        assert any(x.startswith("คำสั่ง bto-2026-10-11-TRX ที่ไม่รู้ผล ตรวจใน Binance แล้ว") for x in lines), lines
        binance.utcnow = lambda: datetime(2026, 10, 11, 12, 11, tzinfo=timezone.utc)
        lines = binance.sync()["lines"]
        assert binance.finals()["bto-2026-10-11-BNB"]["how"] == "rejected" and "bto-2026-10-11-BNB" not in binance.positions()
        assert any(x.startswith("BNB: คำสั่ง bto-2026-10-11-BNB ที่ไม่รู้ผล ไม่มีใน Binance") for x in lines), lines
    finally:
        restore()


def test_short_open_order_is_asked_again_and_keeps_its_send_time():
    # an answer of NEW is asked again by each /sync until final; the settled line keeps the send time (the days held and
    # the daily cap read it); never marked rejected, and the position is not closed while it is open
    from datetime import datetime, timezone
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    noon = binance.utcnow
    try:
        binance.utcnow = lambda: datetime(2026, 10, 10, 23, 55, tzinfo=timezone.utc)
        binance.record("2026-10-10", "TRXUSDT", "open", futures_order("bto-2026-10-10-TRX", "0", "0", "NEW"), "found-after-error")
        binance.utcnow = noon
        fake.orders["bto-2026-10-10-TRX"] = futures_order("bto-2026-10-10-TRX", "0", "0", "NEW")   # still open
        n = len(binance.ledger())
        out = binance.sync()
        assert len(binance.ledger()) == n and binance.positions()["bto-2026-10-10-TRX"]["unclear"], binance.ledger()
        assert out["unclear"] == 1 and any("ยังไม่รู้ผล" in x for x in out["lines"]) and fake.posts == [], out
        assert binance.close_one("bto-2026-10-10-TRX", "AI ตัดสินปิด")["why"] == "unclear" and fake.posts == []
        fake.orders["bto-2026-10-10-TRX"] = futures_order("bto-2026-10-10-TRX", "25", "2")          # then it filled
        fake.positions["TRXUSDT"] = short_row("-25", "2")
        binance.sync()
        f = binance.finals()["bto-2026-10-10-TRX"]
        assert (f["status"], f["how"], f["at"]) == ("FILLED", "found-after-error", "2026-10-10T23:55:00+00:00"), f
        assert not binance.positions()["bto-2026-10-10-TRX"]["unclear"] and not fake.sent("/fapi/v1/algoOrder")
    finally:
        restore()


def test_short_routine_reread_is_settled_and_only_a_lost_answer_is_found_after_error():
    # D5 (2026-11-06): Binance answers every MARKET order with cumQuote 0, so settle re-reads nearly all of them; that
    # re-read of an answered order is 'settled', only an order whose answer was lost is 'found-after-error'. Older
    # ledger lines are shown the same way in /portfolio, never rewritten
    fake = FakeBinance()
    fake.answer_no_price = True
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT"])                                                           # answered: 'placed', cumQuote 0
        fake.answer_no_price, fake.post_error = False, TimeoutError("read timed out")
        opens(binance, ["BNBUSDT"])                                                           # no answer: 'sending'
        fake.orders["bto-2026-10-11-BNB"] = futures_order("bto-2026-10-11-BNB", "25", "2")    # it filled after all
        fake.positions["BNBUSDT"] = short_row("-25", "2")
        binance.record("2026-10-11", "ADAUSDT", "open", futures_order("bto-2026-10-11-ADA", "0", "0", "NEW"), "found")  # the ledger had lost it
        fake.orders["bto-2026-10-11-ADA"] = futures_order("bto-2026-10-11-ADA", "25", "2")
        fake.positions["ADAUSDT"] = short_row("-25", "2")
        binance.sync()
        f = binance.finals()
        assert (f["bto-2026-10-11-TRX"]["how"], f["bto-2026-10-11-BNB"]["how"], f["bto-2026-10-11-ADA"]["how"]) == (
            "settled", "found-after-error", "found-after-error"), binance.ledger()
        o = futures_order("bto-2026-10-11-DOGE", "25", "2")                                  # settle's line before 2026-11-06
        binance.record("2026-10-11", "DOGEUSDT", "open", {**o, "cumQuote": "0"}, "placed")
        binance.record("2026-10-11", "DOGEUSDT", "open", o, "found-after-error")
        how = {x["symbol"]: x["how"] for x in binance.portfolio()["orders"]}
        assert (how["TRXUSDT"], how["BNBUSDT"], how["DOGEUSDT"]) == ("settled", "found-after-error", "settled"), how
        assert binance.finals()["bto-2026-10-11-DOGE"]["how"] == "found-after-error"           # the ledger keeps what it wrote
    finally:
        restore()


def test_short_answered_new_is_settled_before_the_next_open():
    fake = FakeBinance()
    fake.answer_new = True
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        out = opens(binance, ["TRXUSDT", "DOGEUSDT"], run_id="2026-10-12")
        # TRX filled on Binance but answered NEW: DOGE's /open asks Binance first instead of refusing every coin after it
        assert [r["status"] for r in out] == ["unclear", "unclear"] and len(fake.sent("/fapi/v1/order")) == 2, out
        assert not binance.positions()["bto-2026-10-12-TRX"]["unclear"] and binance.positions()["bto-2026-10-12-DOGE"]["unclear"]
        binance.sync()
        assert not any(p["unclear"] for p in binance.positions().values()) and not fake.algos
    finally:
        restore()


def test_short_old_stop_and_take_profit_orders_are_cancelled():
    # the orders the old exit rule left on Binance (closePosition: they would close a position) are cancelled by a live
    # /sync; ids the ledger does not know are left alone; while one cannot be cancelled its coin is not opened
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT"])
        for aid, sym in (("bts-2026-10-11-TRX", "TRXUSDT"), ("btt-2026-10-11-TRX", "TRXUSDT"), ("bts-2026-09-01-ADA", "ADAUSDT")):
            fake.algos[aid] = {"algoId": 9, "clientAlgoId": aid, "symbol": sym, "orderType": "STOP_MARKET", "triggerPrice": "3"}
        real_call = fake.call

        def no_cancel(method, path, signed=False, **q):
            if method == "DELETE":
                raise ConnectionError("fake: no answer")
            return real_call(method, path, signed, **q)
        fake.call = no_cancel
        lines = binance.sync()["lines"]
        (r,) = opens(binance, ["ADAUSDT"])
        fake.call = real_call
        assert sum("ไม่สำเร็จ" in x for x in lines) == 2 and any("bts-2026-09-01-ADA" in x and "ไม่ยกเลิก" in x for x in lines), lines
        assert r["line"] == "ข้าม ADA: stop/take-profit ของไม้เก่ายังค้างบน Binance — ตรวจใน Binance" and len(fake.sent("/fapi/v1/order")) == 1, r
        os.environ["SHORT_LIVE"] = "0"                                                        # test mode leaves them alone
        n = len(fake.calls)
        binance.sync()
        assert not fake.sent("/fapi/v1/algoOrder", "DELETE") and ("GET", "/fapi/v1/openAlgoOrders") not in fake.calls[n:], fake.calls[n:]
        os.environ["SHORT_LIVE"] = "1"
        lines = binance.sync()["lines"]
        assert sorted(q["clientAlgoId"] for q in fake.sent("/fapi/v1/algoOrder", "DELETE")) == ["bts-2026-10-11-TRX", "btt-2026-10-11-TRX"]
        assert list(fake.algos) == ["bts-2026-09-01-ADA"] and "TRXUSDT" in fake.positions, lines
        assert "TRX: ยกเลิก bts-2026-10-11-TRX (เลิกใช้ stop/take-profit บน Binance)" in lines, lines
    finally:
        restore()


def test_short_ai_closes_only_the_bot_positions_it_names_at_market():
    # ลุงจืด 2026-10-13 "ไม่มี stop เลย", "ให้ AI ตัดสินทั้งหมด": /close from the AI team is the only exit (besides
    # liquidation); no time limit; ลุงจืด's own positions and ids the ledger does not know are never touched
    from datetime import timedelta
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    noon = binance.utcnow
    try:
        opens(binance, ["TRXUSDT", "DOGEUSDT"])                                               # 25 each at 2
        fake.positions["BNBUSDT"] = short_row("-10", "2")                                     # ลุงจืด's own short
        binance.utcnow = lambda: noon() + timedelta(days=45)
        n = len(fake.posts)
        os.environ["SHORT_LIVE"] = "0"                                                        # test mode: no order for a live position either
        r = binance.close_one("bto-2026-10-11-TRX", "AI ตัดสินปิด")
        assert fake.posts[n:] == [] and (r["status"], r["line"]) == ("tested", "TRX: AI ตัดสินปิด — โหมดทดสอบไม่ส่งคำสั่ง"), r
        os.environ["SHORT_LIVE"] = "1"
        fake.positions["TRXUSDT"]["positionAmt"] = "-20"                                      # resized by hand: a human decides
        r = binance.close_one("bto-2026-10-11-TRX", "AI ตัดสินปิด")
        assert fake.posts[n:] == [] and r["why"] == "qty_mismatch", r
        assert r["line"] == "TRX: short บน Binance 20 ไม่เท่าที่บอทเปิด 25 — บอทไม่ปิดเอง ตรวจใน Binance", r
        fake.positions["TRXUSDT"]["positionAmt"] = "-25"
        assert binance.close_one("bto-2026-10-11-BNB", "AI ตัดสินปิด")["why"] == "unknown_position" and fake.posts[n:] == []
        os.environ["SHORT_LOT_USDT"] = ""                                                     # Auto Short off: the AI can still exit
        r = binance.close_one("bto-2026-10-11-TRX", "AI ตัดสินปิด")
        assert fake.sent("/fapi/v1/order")[2:] == [{"symbol": "TRXUSDT", "side": "BUY", "type": "MARKET", "quantity": "25", "reduceOnly": "true",
                                                    "newClientOrderId": "btc-2026-10-11-TRX", "newOrderRespType": "RESULT"}], r
        assert r["status"] == "placed" and r["line"].startswith("TRX: ปิด short (AI ตัดสินปิด)"), r
        assert "BNBUSDT" in fake.positions and "DOGEUSDT" in fake.positions, fake.positions
        fake.positions["TRXUSDT"] = short_row("-5", "2")                                      # a TRX short again (by hand): no second close
        assert binance.close_one("bto-2026-10-11-TRX", "AI ตัดสินปิด")["why"] == "gone" and len(fake.sent("/fapi/v1/order")) == 3
        assert any("รอ Binance ลงกำไรขาดทุน" in x for x in binance.sync()["lines"])
        fake.income.append(income_row("TRXUSDT", "REALIZED_PNL", "3.75", int(noon().timestamp() * 1000) + 1))
        lines = binance.sync()["lines"]
        assert binance.finals()["btx-2026-10-11-TRX"]["why"] == "AI ตัดสินปิด", binance.finals()["btx-2026-10-11-TRX"]
        assert any(x.startswith("TRX: short ปิดแล้ว — AI ตัดสินปิด · สุทธิ 3.75 USDT") for x in lines), lines
        assert binance.close_one("bto-2026-10-11-TRX", "AI ตัดสินปิด")["why"] == "closed"
        # a close that did not finish (expired / cancelled): a human checks, nothing is sent again
        binance.record("2026-10-11", "ADAUSDT", "open", futures_order("bto-2026-10-11-ADA", "25", "2"), "placed")
        binance.record("2026-10-11", "ADAUSDT", "close", futures_order("btc-2026-10-11-ADA", "0", "0", "EXPIRED"), "placed",
                       pos="bto-2026-10-11-ADA", why="AI ตัดสินปิด")
        fake.positions["ADAUSDT"] = short_row("-25", "2")
        n = len(fake.posts)
        r = binance.close_one("bto-2026-10-11-ADA", "AI ตัดสินปิด")
        assert fake.posts[n:] == [] and r["why"] == "close_incomplete" and "สถานะ EXPIRED" in r["line"], r
    finally:
        restore()


def test_short_sync_never_records_a_position_binance_still_holds():
    # REALIZED_PNL on the coin after the open (e.g. ลุงจืด trading it too) must not make /sync call a held bot short closed:
    # it would leave shorts[] and no AI exit would ever see it again (no stop on Binance)
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT", "DOGEUSDT"])
        ms = fake.orders["bto-2026-10-11-TRX"]["updateTime"]
        del fake.positions["DOGEUSDT"]                                                       # only DOGE is gone
        fake.income += [income_row("TRXUSDT", "REALIZED_PNL", "4", ms + 1), income_row("DOGEUSDT", "REALIZED_PNL", "-2", ms + 1)]
        binance.sync()
        f = binance.finals()
        assert "btx-2026-10-11-TRX" not in f and f["btx-2026-10-11-DOGE"]["pnl"] == "-2", f
        assert not binance.positions()["bto-2026-10-11-TRX"]["closed"]
    finally:
        restore()


def test_short_closed_by_binance_is_recorded_with_its_real_pnl():
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT", "DOGEUSDT"])
        ms = fake.orders["bto-2026-10-11-TRX"]["updateTime"]
        for sym in ("TRXUSDT", "DOGEUSDT"):
            del fake.positions[sym]                                                          # Binance closed both (liquidation / by hand)
        fake.income = [income_row("TRXUSDT", "COMMISSION", "-0.05", ms)]                      # only the opening fee posted yet
        n = len(binance.ledger())
        lines = binance.sync()["lines"]
        assert len(binance.ledger()) == n and sum("รอ Binance ลงกำไรขาดทุน" in x for x in lines) == 2, lines
        fake.income += [income_row("TRXUSDT", "REALIZED_PNL", "0.1", ms + 1), income_row("TRXUSDT", "REALIZED_PNL", "0.2", ms + 2),
                        income_row("TRXUSDT", "FUNDING_FEE", "0.1", ms + 3), income_row("TRXUSDT", "COMMISSION", "-0.05", ms + 4),
                        income_row("TRXUSDT", "COMMISSION", "-0.001", ms + 4, "BNB"),
                        income_row("TRXUSDT", "REALIZED_PNL", "7", ms - 3_600_000),          # an older TRX trade: not this short
                        income_row("DOGEUSDT", "REALIZED_PNL", "-25", ms + 5)]
        lines = binance.sync()["lines"]
        f = binance.finals()
        trx = f["btx-2026-10-11-TRX"]
        # -0.05 + 0.1 + 0.2 + 0.1 - 0.05 in Decimal is exactly 0.30 (floats: 0.30000000000000004)
        assert (trx["kind"], trx["pos"], trx["pnl"], trx["funding"], trx["fees"], trx["other_assets"]) == (
            "closed", "bto-2026-10-11-TRX", "0.30", "0.1", "-0.10", {"BNB": "-0.001"}), trx
        assert trx["why"].startswith("ปิดนอกบอท") and f["btx-2026-10-11-DOGE"]["pnl"] == "-25", f
        assert any(x.startswith("TRX: short ปิดแล้ว — ปิดนอกบอท") and "สุทธิ 0.30 USDT" in x for x in lines), lines
        n = len(binance.ledger())
        binance.sync()
        assert len(binance.ledger()) == n                                                     # recorded once
        # a closed coin is opened again by a later run id, never by its own (the loss cooldown is the team's: SHORT_COOLDOWN_DAYS)
        assert opens(binance, ["TRXUSDT"])[0]["why"] == "sent"
        opens(binance, ["TRXUSDT", "DOGEUSDT"], run_id="2026-10-12")
        assert "bto-2026-10-12-TRX" in fake.orders and "bto-2026-10-12-DOGE" in fake.orders
    finally:
        restore()


def test_short_with_an_unclear_order_is_never_touched():
    # an order with an unknown outcome freezes its position: no close even when the AI says so, no new position at all,
    # and /sync never records it closed (its order may still fill while another trade's REALIZED_PNL is already there)
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT"])
        binance.record("2026-10-11", "TRXUSDT", "close", {"clientOrderId": "btc-2026-10-11-TRX"}, "sending",
                       pos="bto-2026-10-11-TRX", why="AI ตัดสินปิด")
        binance.record("2026-10-11", "CAKEUSDT", "open", {"clientOrderId": "bto-2026-10-11-CAKE"}, "sending")
        binance.record("2026-10-11", "ADAUSDT", "open", {"clientOrderId": "bto-2026-10-11-ADA"}, "sending")   # not on Binance
        fake.unreachable |= {"btc-2026-10-11-TRX", "bto-2026-10-11-CAKE", "bto-2026-10-11-ADA"}   # Binance cannot be asked yet
        fake.positions["CAKEUSDT"] = short_row("-25", "2")
        fake.income.append(income_row("ADAUSDT", "REALIZED_PNL", "-3", int(binance.utcnow().timestamp() * 1000) + 1))
        n = len(fake.posts)
        out = binance.sync()
        assert out["unclear"] == 3 and sum("ไม่ทำอะไรกับไม้นี้" in x for x in out["lines"]) == 3, out
        assert not any(r["kind"] == "closed" for r in binance.ledger()), binance.ledger()
        assert [binance.close_one(i, "AI ตัดสินปิด")["why"] for i in ("bto-2026-10-11-TRX", "bto-2026-10-11-CAKE")] == ["unclear"] * 2
        (r,) = opens(binance, ["DOGEUSDT"])
        assert fake.posts[n:] == [] and r["why"] == "unclear", r
        assert r["line"] == "ข้าม DOGE: TRX, CAKE มีคำสั่งที่ยังไม่รู้ผล — ไม่เปิดไม้ใหม่จนกว่าจะตรวจใน Binance", r
    finally:
        restore()


def test_short_portfolio_shows_the_account_before_the_lot_is_set():
    # setup not finished (no SHORT_LOT_USDT): no opening, but the app still sees the futures wallet with the key alone
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LOT_USDT="")
    try:
        p = binance.portfolio()
        assert "error" not in p and p["wallet_usdt"] == "1000.00" and p["lot_usdt"] is None, p
        assert p["mode"].startswith("Auto Short ปิดอยู่") and p["shorts"] == [], p
        (r,) = opens(binance, ["TRXUSDT"], run_id="2026-10-12")
        assert (r["status"], r["why"]) == ("refused", "live_off") and r["line"].startswith("Auto Short ปิดอยู่"), r
        assert fake.posts == [], fake.posts
    finally:
        restore()


def test_short_portfolio_is_strings_for_the_app():
    import json
    fake = FakeBinance(bid="0.3333")
    fake.spot_usdt = "12.5"
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        opens(binance, ["TRXUSDT", "BNBUSDT"])                                                # 150 each (50 / 0.3333)
        ms = fake.orders["bto-2026-10-11-TRX"]["updateTime"]
        fake.positions["TRXUSDT"].update(markPrice="0.3", unRealizedProfit="4.99")
        del fake.positions["BNBUSDT"]                                                        # Binance closed BNB
        fake.income = [income_row("TRXUSDT", "FUNDING_FEE", "0.1", ms + 1), income_row("TRXUSDT", "FUNDING_FEE", "0.2", ms + 2),
                       income_row("TRXUSDT", "FUNDING_FEE", "5", ms - 3_600_000),               # before this short opened
                       income_row("BNBUSDT", "FUNDING_FEE", "0.4", ms + 1), income_row("BNBUSDT", "REALIZED_PNL", "-1.25", ms + 3)]
        binance.sync()                                                                        # records BNB's close
        p = binance.portfolio()
        json.dumps(p)                                                                         # no Decimal left: the app reads JSON
        (s,) = p["shorts"]
        assert {k: s[k] for k in ("id", "symbol", "qty", "entry", "mark", "pnl_usdt", "pnl_pct", "funding_usdt", "days", "on_binance",
                                  "unclear")} == {
            "id": "bto-2026-10-11-TRX", "symbol": "TRXUSDT", "qty": "150", "entry": "0.33330000", "mark": "0.3", "pnl_usdt": "4.99",
            "pnl_pct": "9.98", "funding_usdt": "0.30", "days": 0, "on_binance": True, "unclear": False}, s
        assert not {"stop", "take_profit", "close_by"} & set(s), s
        (c,) = p["closed"]
        assert (c["symbol"], c["pnl"], c["pnl_usdt"], c["funding_usdt"], c["fees_usdt"]) == ("BNBUSDT", "-0.85", "-0.85", "0.40", "0.00"), c
        assert c["why"].startswith("ปิดนอกบอท") and p["held_symbols"] == ["TRXUSDT"], p
        assert (p["realized_usdt"], p["wallet_usdt"], p["available_usdt"], p["equity_usdt"], p["spot_usdt"], p["lot_usdt"], p["mode"]) == (
            "-0.85", "1000.00", "1000.00", "1000.00", "12.50", "50", "ซื้อขายจริง"), p
        # D2 (2026-11-06) + R0 (2026-11-26): code picks entries and exits, the AI only comments; the guard is 2x
        assert "ทีม AI" not in p["rule"] and "โค้ดตรวจกติกาออกทุกรอบ" in p["rule"] and "isolated ไม่เกิน 2x" in p["rule"], p["rule"]
        assert "ปิดตามกติกา · AI เขียนแค่ความเห็น ·" in p["rule"], p["rule"]                      # the AI clause stands alone
    finally:
        restore()


def test_short_server_only_accepts_a_clean_request():
    # the team machine is outside the server's trust boundary: anything odd is a 400 before Binance is touched; a
    # Chinese-named perp (Binance lists them since 2025-10) is a real pair
    from decimal import Decimal
    import binance
    ok = {"run_id": "2026-10-11", "symbol": "CAKEUSDT", "side": "SHORT", "usdt": "100"}
    assert binance.MAX_LEVERAGE == 2
    assert binance.open_from(ok) == ("2026-10-11", "CAKEUSDT", "SHORT", Decimal("100"), 1)
    assert binance.open_from({**ok, "usdt": "200", "leverage": 1})[3:] == (Decimal("200"), 1)
    assert binance.open_from({**ok, "usdt": "200", "leverage": 2})[3:] == (Decimal("200"), 2)
    assert binance.open_from({**ok, "symbol": "币安人生USDT", "side": "LONG", "usdt": "50.25"})[1:] == ("币安人生USDT", "LONG", Decimal("50.25"), 1)
    assert binance.close_from({"id": "bto-2026-10-11-CT"}) == ("bto-2026-10-11-CT", "ไม่ระบุเหตุผล")   # the AI decides no close
    assert binance.close_from({"id": "bto-2026-10-11-z0123456789", "why": "ตัดขาดทุน"})[1] == "ตัดขาดทุน"
    assert binance.run_id_of({"run_id": "2026-10-11"}) == "2026-10-11"
    bad_open = ([{**ok, "run_id": r} for r in ("today", "2026-10-1", 20261011, "2026-13-45", None)]
                + [{**ok, "symbol": s} for s in ("cakeusdt", "cake&x=1", "A" * 21 + "USDT", "CAKE", 1, None)]
                + [{**ok, "side": s} for s in ("short", "SELL", None, 1)]
                + [{**ok, "usdt": u} for u in (100, 100.5, "0", "0.00", "-5", "1e3", "NaN", "50.123", "5 0", "", "1234567890", None)]
                + [{**ok, "leverage": v} for v in (3, 0, -1, 125, "2", 2.0, True, None)])   # 3+: guard is 2x isolated since 2026-11-26 R0
    bad_close = [{}, {"id": "btc-2026-10-11-CT"}, {"id": "bto-" + "A" * 33}, {"id": "bto-x/../y"}, {"id": 1},
                 {"id": "bto-2026-10-11-CT", "why": ""}, {"id": "bto-2026-10-11-CT", "why": "x" * 201}, {"id": "bto-2026-10-11-CT", "why": 1}]
    for fn, body in ([(binance.open_from, b) for b in bad_open] + [(binance.close_from, b) for b in bad_close]
                     + [(binance.run_id_of, {}), (binance.open_from, [1]), (binance.close_from, "x")]):
        try:
            fn(body)
            raise AssertionError(body)
        except (ValueError, TypeError, AttributeError):
            pass


def test_short_gate_skips_an_old_plan():
    # a restart replays old plans from Kafka: only today's and yesterday's may open
    from datetime import date
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    today = date(2026, 10, 11)
    try:
        for run_id, ok in (("2026-10-11", True), ("2026-10-10", True), ("2026-10-09", False), ("2026-10-12", False)):
            cfg, why, line = binance.gate(run_id, today)
            assert (cfg is not None, why, line) == (ok, None if ok else "old_run", None if ok else f"ข้ามแผน {run_id} เพราะเป็นแผนเก่า"), run_id
        (r,) = opens(binance, ["TRXUSDT"], run_id="2026-10-09", today=today)
        assert (r["status"], r["why"], r["line"]) == ("refused", "old_run", "ข้ามแผน 2026-10-09 เพราะเป็นแผนเก่า") and fake.calls == [], r
        os.environ["BINANCE_API_KEY"] = ""                                                   # no key on the server: nothing reaches Binance
        assert binance.gate("2026-10-11", today)[1] == "setup"
        r, c = opens(binance, ["TRXUSDT"], today=today)[0], binance.close_one("bto-2026-10-11-TRX", "AI ตัดสินปิด")
        assert (r["why"], c["why"]) == ("setup", "setup") and fake.calls == [], (r, c)
    finally:
        restore()


def test_short_ledger_skips_a_torn_line():
    # one truncated line (crash mid-write) must not break every round, and the next row must not be glued onto it
    import contextlib
    import io
    binance, restore = short_env(FakeBinance())
    try:
        binance.record("2026-10-11", "TRXUSDT", "open", futures_order("bto-2026-10-11-TRX", "25", "2"), "placed")
        with open(binance.LEDGER, "a", encoding="utf-8") as f:
            f.write('{"at": "2026-10-11T12:00:00+00:00", "actor": "bottr')               # torn: no newline
        binance.record("2026-10-11", "BNBUSDT", "open", futures_order("bto-2026-10-11-BNB", "1", "50"), "placed")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert sorted(binance.positions()) == ["bto-2026-10-11-BNB", "bto-2026-10-11-TRX"]
        assert "line 2" in err.getvalue(), err.getvalue()
    finally:
        restore()


def test_short_pc_requests_reach_the_server_and_back():
    # .88 post() -> (tunnel) -> server service -> open_one / close_one / sync with the key held only on the server;
    # every non-200 answer (or one without a status) raises on the client instead of being read as an answer
    import contextlib
    import http.client
    import http.server
    import threading
    fake = FakeBinance()
    binance, restore = short_env(fake, DCA_PROXY_HOST="198.51.100.7")
    srv = http.server.HTTPServer(("127.0.0.1", 0), binance._Service)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_tunnel, real_ask = binance.tunnel, binance.ask
    binance.tunnel = lambda: contextlib.nullcontext(srv.server_address[1])

    def raw(method, path, body, headers):
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.putrequest(method, path)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        res = conn.getresponse()
        res.read()
        conn.close()
        return res.status

    def fails(path, body, says):
        try:
            binance.post(path, body)
        except RuntimeError as e:
            assert says in str(e), e
            return True
        return False
    try:
        r = binance.post("/open", {"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "50"})
        assert r["status"] == "tested" and "ตรวจคำสั่งผ่าน" in r["line"] and fake.calls.count(("POST", "/fapi/v1/order/test")) == 1, r
        n = len(fake.calls)                                                                   # the server's clock, not the run id, says old
        r = binance.post("/open", {"run_id": "2026-10-09", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "50"})
        assert (r["status"], r["why"]) == ("refused", "old_run") and len(fake.calls) == n, r
        r = binance.post("/close", {"id": "bto-2026-10-11-TRX", "why": "AI ตัดสินปิด"})
        assert (r["status"], r["why"]) == ("refused", "unknown_position"), r                  # test mode never opened it
        s = binance.post("/sync", {"run_id": "2026-10-11"})
        assert (s["status"], s["lines"], s["unclear"]) == ("ok", [], 0), s
        assert fails("/open", {"run_id": "2026-10-11", "symbol": "trxusdt", "side": "SHORT", "usdt": "50"}, "HTTP 400")
        assert fails("/open", {"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": 50}, "HTTP 400")
        assert fails("/open", {"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "50", "leverage": 3}, "HTTP 400")
        assert fails("/plan", {"run_id": "2026-10-11", "short": ["TRXUSDT"]}, "HTTP 404")    # the old route is gone
        for status, method, path, body, headers in (
                (403, "POST", "/open", b"{}", {"X-BotTread-Token": "x" * 40, "Content-Length": "2"}),
                (403, "GET", "/portfolio", None, {"X-BotTread-Token": "x" * 40}),
                (400, "POST", "/open", b"{}", {"X-BotTread-Token": TOKEN, "Content-Length": "abc"}),
                (400, "POST", "/sync", b"", {"X-BotTread-Token": TOKEN, "Content-Length": "0"}),
                (400, "POST", "/sync", b"not json", {"X-BotTread-Token": TOKEN, "Content-Length": "8"}),
                (400, "POST", "/sync", b"[" * 60000, {"X-BotTread-Token": TOKEN, "Content-Length": "60000"}),   # RecursionError
                (404, "GET", "/nope", None, {"X-BotTread-Token": TOKEN}),
                (404, "POST", "/nope", b"{}", {"X-BotTread-Token": TOKEN, "Content-Length": "2"})):
            assert raw(method, path, body, headers) == status, (status, method, path, headers)
        try:
            binance.ask("GET", "/nope")
            raise AssertionError("404 read as an answer")
        except RuntimeError as e:
            assert "HTTP 404" in str(e) and "ไม่มีหน้านี้" in str(e), e
        # an older server answers 200 without a status (or /sync without lines): an error too
        for answer, path in (({"lines": ["ok"]}, "/open"), ({"lines": ["ok"]}, "/close"), ({"status": "ok"}, "/sync"),
                             ({"status": "placed", "why": None}, "/close"), ({"status": "placed", "line": "x"}, "/open")):
            binance.ask = lambda *a, **k: answer
            assert fails(path, {}, "ไม่ครบ"), (answer, path)
        binance.ask = real_ask
        log = [r["text"] for r in binance.fetch_portfolio()["log"]]                          # the app's server log, newest first
        for want in ("/open TRX SHORT 1x 50.00 USDT → tested: TRX: short 1x 25 TRX", "/close bto-2026-10-11-TRX (AI ตัดสินปิด) → refused (unknown_position)",
                     "/sync → ok · ไม่รู้ผล 0 ไม้", "server ไม่รับ /open: symbol", "server ไม่รับ /open: usdt", "ปฏิเสธ POST /open",
                     "ปฏิเสธ GET /portfolio", "ไม่รับ /sync: ข้อมูลว่างหรือใหญ่เกิน", "server ไม่รับ /sync", "ปฏิเสธ GET /nope",
                     "ปฏิเสธ POST /nope"):
            assert any(t.startswith(want) for t in log), (want, log)
        # 20 opens sent today (server UTC): yesterday's still-valid run id cannot open a 21st
        os.environ["SHORT_LIVE"] = "1"
        for i in range(20):
            cid = f"bto-2026-10-11-C{i:02d}"
            binance.record("2026-10-11", f"C{i:02d}USDT", "open", {"clientOrderId": cid}, "sending")
            binance.record("2026-10-11", f"C{i:02d}USDT", "open", futures_order(cid, "1", "2"), "placed")
        n = len(fake.calls)
        r = binance.post("/open", {"run_id": "2026-10-10", "symbol": "CAKEUSDT", "side": "SHORT", "usdt": "50"})
        assert (r["status"], r["why"]) == ("refused", "daily_cap") and len(fake.calls) == n, r
        n = len(fake.calls)
        os.environ["DCA_PLAN_TOKEN"] = "short"   # a weak token on the server locks the service instead of opening it
        assert fails("/sync", {"run_id": "2026-10-11"}, "DCA_PLAN_TOKEN") and len(fake.calls) == n
    finally:
        srv.shutdown()
        binance.tunnel, binance.ask = real_tunnel, real_ask
        restore()


def test_short_service_finishes_the_order_when_the_client_hangs_up():
    # the order is sent and journaled before the reply: a team machine that times out (or drops the tunnel) mid-order only
    # loses the answer; its /sync finds the order in the ledger, and nothing is sent twice
    import contextlib
    import http.server
    import threading
    import time
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1", DCA_PROXY_HOST="198.51.100.7")
    srv = http.server.HTTPServer(("127.0.0.1", 0), binance._Service)
    srv.handle_error = lambda request, client_address: None   # the reply to a closed socket may fail: not this test's concern
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_tunnel, real_call = binance.tunnel, fake.call
    binance.tunnel = lambda: contextlib.nullcontext(srv.server_address[1])

    def slow(method, path, signed=False, **q):
        if (method, path) == ("POST", "/fapi/v1/order"):
            time.sleep(1)                                                                     # Binance is slow today
        return real_call(method, path, signed, **q)
    fake.call = slow
    try:
        try:
            binance.post("/open", {"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "50"}, timeout=0.3)
            raise AssertionError("no timeout")
        except OSError:   # TimeoutError: the client gave up and closed its socket
            pass
        for _ in range(100):
            if binance.finals().get("bto-2026-10-11-TRX", {}).get("how") == "placed":
                break
            time.sleep(0.05)
        assert binance.finals()["bto-2026-10-11-TRX"]["how"] == "placed" and len(fake.sent("/fapi/v1/order")) == 1, binance.ledger()
        r = binance.post("/open", {"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "50"})
        assert r["why"] == "sent" and len(fake.sent("/fapi/v1/order")) == 1, r
        assert any(x["text"].startswith("/open TRX SHORT 1x 50.00 USDT → placed") for x in binance.ledger(binance.log_path()))
    finally:
        srv.shutdown()
        binance.tunnel, fake.call = real_tunnel, real_call
        restore()


def test_short_service_adopts_the_systemd_socket():
    # under bottread-dca.socket systemd holds 127.0.0.1:8787 (also while the service restarts) and hands it over as
    # fd 3: the service serves on it instead of binding the port itself; without systemd it binds as before
    import http.client
    import http.server
    import socket
    import threading
    binance, restore = short_env(FakeBinance())
    pre = socket.socket()
    pre.bind(("127.0.0.1", 0))
    pre.listen()
    servers, real_socket, real_forever = [], socket.socket, http.server.HTTPServer.serve_forever
    http.server.HTTPServer.serve_forever = lambda self: servers.append(self)
    os.environ.update(LISTEN_FDS="1", LISTEN_PID=str(os.getpid()))
    try:
        socket.socket = lambda *a, fileno=None, **k: pre if fileno == 3 else real_socket(*a, **k)
        binance.serve(0)
        socket.socket = real_socket
        assert servers[0].socket is pre
        threading.Thread(target=servers[0].handle_request, daemon=True).start()
        conn = http.client.HTTPConnection(*pre.getsockname(), timeout=10)
        conn.request("GET", "/nope", headers={"X-BotTread-Token": TOKEN})
        assert conn.getresponse().status == 404
        conn.close()
        os.environ["LISTEN_PID"] = "1"                                           # fds meant for another process
        binance.serve(0)
        assert servers[1].socket is not pre
        servers[1].server_close()
    finally:
        socket.socket, http.server.HTTPServer.serve_forever = real_socket, real_forever
        for k in ("LISTEN_FDS", "LISTEN_PID"):
            os.environ.pop(k, None)
        pre.close()
        restore()


def test_round_without_a_plan_still_runs_the_exits():
    # quant/risk/CIO down (ClickHouse or Ollama) = no plan, but the AI exit review of the open shorts and recording the
    # ones Binance closed must not depend on them
    import bus
    import team
    calls, tmp = [], tempfile.mkdtemp()

    class Joinable:
        def join(self, timeout=None):
            pass
    real = (team.LISTENERS, team.supervise, team.settle, team.say, team.trade, team.REPORTS,
            bus.send, bus.start_at_end)
    team.LISTENERS, team.supervise = [], lambda m: Joinable()
    team.settle = lambda done=lambda: True, max_s=0, every_s=0: done()
    team.say, team.trade, team.REPORTS, bus.send = (lambda *a: None), (lambda run, short: calls.append(short)), tmp, (lambda *a, **k: None)
    try:
        assert team.once() is False and calls == [[]]                         # no plan: upkeep-only request
        run = team.now().strftime("%Y-%m-%d")
        os.makedirs(os.path.join(tmp, run))
        plan = os.path.join(tmp, run, "dca-plan.md")
        team.settle = lambda done=lambda: True, max_s=0, every_s=0: open(plan, "w").close() or os.utime(plan, (2e9, 2e9)) or done()
        assert team.once() is True and calls == [[]]                          # a plan: the buyer handles it, no extra call
    finally:
        (team.LISTENERS, team.supervise, team.settle, team.say, team.trade, team.REPORTS,
         bus.send, bus.start_at_end) = real
        bus.stop.clear()


def pick(base, age=10, r7=0.25):
    """A passing row of market.short_screen."""
    return {"symbol": base + "USDT", "base": base, "price": 0.012345678, "rsi_prev": 75.2, "rsi": 62.4, "age_days": age,
            "vol30_usd": 45.6e6, "r7": r7, "fail": []}


def test_cio_opens_only_on_entry_day_but_analyses_every_hour():
    # hourly rounds analyse the passing coins; the plan's short list stays empty until the last hour's ClickHouse data is
    # complete, then stops at MAX_PLAN; an AI that fails still leaves a plan (with the code's own summary)
    import bus
    import team
    got, plans, answers = {}, [], iter(["สรุป", RuntimeError("HTTP 500")])

    def chat(*a, **k):
        x = next(answers)
        if isinstance(x, Exception):
            raise x
        return x
    real = bus.listen, bus.send, team.ollama.chat, team.say
    bus.listen = lambda group, topics, handle, **kw: got.update(handle=handle)
    bus.send = lambda t, m: plans.append(m) if t == "bt.dca.plan" else None
    team.ollama.chat, team.say = chat, (lambda who, text: None)
    try:
        team.cio()
        for entry, picks in ((False, [pick("CT")]), (True, [pick(f"C{i:02d}") for i in range(team.MAX_PLAN + 2)])):
            run = f"r{entry}"
            review = {"run_id": run, "picks": picks, "reviews": [], "universe": 300, "passing": len(picks),
                      "passing_coins": [p["base"] for p in picks], "held": [], "entry_day": entry, "data_to": "2026-10-10 00:00"}
            got["handle"]([("bt.dca.review", review)] + [("bt.chart.result", {"purpose": "dca", "run_id": run, "coin": p["base"],
                                                                               "chart": None}) for p in picks[:team.MAX_CHARTS]])
        off, on = plans
        assert off["short"] == [] and off["narrative"] == "สรุป"
        assert on["short"] == [f"C{i:02d}USDT" for i in range(team.MAX_PLAN)]
        assert on["narrative"].startswith("AI สรุปไม่ผ่านการตรวจ (เรียก AI ไม่สำเร็จ)") and f"C{team.MAX_PLAN + 1:02d}" in on["narrative"]
    finally:
        bus.listen, bus.send, team.ollama.chat, team.say = real


def test_ai_numbers_must_come_from_the_data():
    from decimal import Decimal
    import ollama
    assert ollama.numbers("+1,705 ไม้ ลง 12.50% ๑๒\n1. หัวข้อ") == {Decimal(1705), Decimal("12.5"), Decimal(12)}
    assert ollama.ungrounded("ลง 258% ใน 7 วัน", "TEST +258.3% r7 +7%") == ["258"]     # rounded = not the data
    assert ollama.ungrounded("ราคา 0.012346", '{"price": "0.0123457"}') == ["0.012346"]
    assert ollama.ungrounded("พุ่งแล้วย่อ", "") == []


def test_cio_summary_shows_only_numbers_from_the_data():
    import json
    import team
    r = {"data_to": "2026-10-10 00:00", "entry_day": True, "universe": 300, "passing": 1, "reviews": []}
    asked, real = [], team.ollama.chat
    try:
        team.ollama.chat = lambda model, system, user, **k: asked.append(user) or "CT ขึ้น +25% ใน 7 วัน volume $45.6M"
        assert team.summarize(r, [pick("CT")], ["CTUSDT"], [], {}) == "CT ขึ้น +25% ใน 7 วัน volume $45.6M"
        asked.clear()
        team.ollama.chat = lambda model, system, user, **k: asked.append(user) or "CT ขึ้น 26% คาดว่าลงอีก 88%"
        out = team.summarize(r, [pick("CT")], ["CTUSDT"], [], {})
        assert len(asked) == 2 and "ไม่อยู่ใน input: 26, 88" in asked[1]   # one retry naming the made-up numbers
        assert out.startswith("AI สรุปไม่ผ่านการตรวจ (มีตัวเลขที่ไม่อยู่ในข้อมูล: 26, 88)") and "CT" in out
        assert "วันนี้ server เปิด" in out and team.PAUSED_TH not in out, out
        team.OPEN_NEW, asked[:] = False, []
        team.ollama.chat = lambda model, system, user, **k: asked.append(json.loads(user)) or 1 / 0
        out = team.summarize(r, [pick("CT")], ["CTUSDT"], [], {})
        assert team.PAUSED_TH in out and "วันนี้ server เปิด" not in out and "เรียก AI ไม่สำเร็จ" in out, out
        assert asked[0]["open_new"] is False and asked[0]["opens_today"] == [] and asked[0]["opens_count"] == 0, asked
    finally:
        team.ollama.chat, team.OPEN_NEW = real, True


def test_risk_lines_need_numbers_from_the_coins_own_figures():
    import bus
    import team
    coins = {"CT": team.facts(pick("CT", r7=0.26)), "AB": team.facts(pick("AB", r7=-0.05))}   # 26: in no rule text
    got = [{"coin": "ct", "risks_th": ["ขึ้น +26% ใน 7 วัน อาจพุ่งต่อ", "funding +0.13% สูง", "อายุ 10 วัน ข้อมูลน้อย"]},
           {"coin": "CT", "risks_th": ["ซ้ำ"]}, {"coin": "ZZ", "risks_th": ["ไม่อยู่ในแผน"]},
           {"coin": "AB", "risks_th": ["ขึ้น +26%"]}]                                  # CT's figure, not AB's
    assert team.grounded_reviews(got, coins, team.SHORT_RULE_TH) == (
        [{"coin": "CT", "risks_th": ["ขึ้น +26% ใน 7 วัน อาจพุ่งต่อ", "อายุ 10 วัน ข้อมูลน้อย"]}], 2)
    # the AI failing = no comments, the round goes on
    box, sent = {}, []
    real = bus.listen, bus.send, team.ollama.chat, team.say
    bus.listen = lambda group, topics, handle, **kw: box.update(handle=handle)
    bus.send = lambda t, m: sent.append((t, m["reviews"]))
    team.say = lambda who, text: None
    team.ollama.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
    try:
        team.risk_critic()
        box["handle"]([("bt.dca.candidates", {"run_id": "r", "picks": [pick("CT")], "data_to": "2026-10-10 00:00"})])
        assert sent == [("bt.dca.review", [])]
    finally:
        bus.listen, bus.send, team.ollama.chat, team.say = real


def test_short_screen_needs_each_perps_whole_day():
    import datetime as dt
    days = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(12)]          # to Monday 2026-10-12
    data = {"AUSDT": (days, [20.0 - i * 0.1 for i in range(12)], [30e6] * 12), "BUSDT": (days, [15.0 - i * 0.1 for i in range(12)], [30e6] * 12)}
    hrs = [f"2026-10-12 {h:02d}:00:00" for h in range(9, 24)] + ["2026-10-13 00:00:00"]
    # same age_days (11): A has prev RSI ~92.9 (one small dip earlier), B has prev RSI = 100.0 (straight up) -> B ranks before A when both complete
    hc_a = [10.0, 9.0] + [10.0 + i for i in range(13)] + [15.0]
    hc_b = [10.0 + i for i in range(15)] + [16.0]
    hdata = {"AUSDT": (hrs, hc_a), "BUSDT": (hrs, hc_b)}
    real = market.usdt_perps, market.load_perp_daily, market.load_perp_hourly
    market.usdt_perps = lambda: {"AUSDT": "A", "BUSDT": "B"}
    market.load_perp_daily = lambda syms: (data, {}, {})
    market.load_perp_hourly = lambda syms: (hdata, {"AUSDT": "2026-10-13 00:45:00", "BUSDT": "2026-10-13 00:15:00"})
    try:
        rows, meta = market.legacy_short_screen(dt.datetime(2026, 10, 13, 1, 5, tzinfo=dt.timezone.utc))    # 01:05 UTC (< 15m grace)
        fails = {r["symbol"]: r["fail"] for r in rows}
        assert fails == {"AUSDT": [], "BUSDT": ["ข้อมูลชั่วโมง 2026-10-13 00:00 UTC ใน ClickHouse ยังไม่ครบ (ถึง 00:15 UTC)"]}
        assert not meta["entry_day"] and meta["late"] == ["BUSDT"]
        rows, meta = market.legacy_short_screen(dt.datetime(2026, 10, 13, 1, 15, tzinfo=dt.timezone.utc))   # grace over: B stays out
        assert meta["entry_day"] and [r["symbol"] for r in rows if not r["fail"]] == ["AUSDT"]
        market.load_perp_hourly = lambda syms: (hdata, {"AUSDT": "2026-10-13 00:45:00", "BUSDT": "2026-10-13 00:45:00"})
        rows, meta = market.legacy_short_screen(dt.datetime(2026, 10, 13, 1, 5, tzinfo=dt.timezone.utc))    # all complete: B (higher prev RSI) first
        assert meta["entry_day"] and meta["late"] == [] and [r["symbol"] for r in rows if not r["fail"]] == ["BUSDT", "AUSDT"]
    finally:
        market.usdt_perps, market.load_perp_daily, market.load_perp_hourly = real


def test_chartist_reads_a_new_perp_without_a_spot_pair():
    # most coins the bot shorts are new perps with no spot pair: the chart is the perp's own ClickHouse closes
    import datetime as dt
    import bus
    import chart
    import team
    got, sent, shot = {}, [], []
    days = [team.now().date() - dt.timedelta(days=i) for i in range(10, 0, -1)]
    real = bus.listen, bus.send, market.load_daily, market.load_perp_daily, chart.snapshot, chart.read, team.btc_days, team.say
    bus.listen = lambda group, topics, handle, **kw: got.update(handle=handle)
    bus.send = lambda t, m: sent.append(m)
    market.load_daily = lambda sym: {}
    market.load_perp_daily = lambda syms: ({syms[0]: (days, [float(10 - i) for i in range(10)], [1.0] * 10)}, "")
    chart.snapshot = lambda sym, out_dir, d: shot.append((sym, d[-1])) or (None, "none")
    chart.read = lambda *a: {"trend": "down", "note_th": ""}
    team.btc_days, team.say = (lambda: None), (lambda who, text: None)
    try:
        team.chartist()
        got["handle"]([("bt.chart.request", {"coin": "CT", "purpose": "dca"})])
        assert shot == [("CTUSDT", (days[-1].isoformat(), 1.0))] and sent[0]["ok"] and sent[0]["symbol"] == "CTUSDT"
        got["handle"]([("bt.chart.request", {"coin": "牛来", "purpose": "dca"})])
        assert shot[-1][0] == "牛来USDT" and sent[-1]["ok"], sent[-1]
    finally:
        bus.listen, bus.send, market.load_daily, market.load_perp_daily, chart.snapshot, chart.read, team.btc_days, team.say = real


def test_exit_rule_is_the_backtests():
    # ลุงจืด 2026-10-28 "กลับ 1x + ออกตามกติกา": research/loosen_rules_backtest.py sim_short / sim_long on 1h bars, checked
    # every round on the completed hours since the entry bar (the last hour closed before the open), then the live mark
    from datetime import datetime, timedelta, timezone
    import team
    t0 = datetime(2026, 10, 20, 10, 5, tzinfo=timezone.utc)                 # opened 10:05 -> entry bar 09:00

    def bars(rows, start=datetime(2026, 10, 13, 9)):                         # one row per hour from start: (high, low, close)
        return [(f"{start + timedelta(hours=k):%Y-%m-%d %H}:00:00", *r) for k, r in enumerate(rows)]
    flat = [(1.01, 0.99, 1.0)] * 169                                         # hi[i-168 .. i] up to the entry bar: swing high 1.01
    saved_lv = team.LOWVOL_EXIT_ON
    team.LOWVOL_EXIT_ON = False
    rule = team.rule_exit
    try:
        # SHORT stop = swing high + 5% clamped to 8%..45%: 1.01 -> +8% (clamped up) = 1.08
        assert rule("SHORT", 1.0, 1.0, t0, bars(flat + [(1.02, 0.98, 1.0)] * 3), t0 + timedelta(hours=3)) is None
        assert rule("SHORT", 1.0, 1.0, t0, bars(flat + [(1.02, 0.98, 1.0), (1.08, 0.98, 1.0)]), t0 + timedelta(hours=2)) == "หลุด stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
        assert rule("SHORT", 1.0, 1.08, t0, bars(flat + [(1.02, 0.98, 1.0)]), t0) == "ราคา mark หลุด stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
        assert rule("SHORT", 1.0, 1.07, t0, bars(flat + [(1.02, 0.98, 1.0)]), t0) is None
        assert rule("SHORT", 1.0, 1.0, t0, bars(flat[:-1] + [(1.09, 0.99, 1.0)]), t0) is None                  # the entry bar's own high: no stop
        # a 7-day high far above the entry widens the stop, capped at +45%; the bar before the window does not count
        spike = [(1.30, 0.99, 1.0)] + flat[1:]
        assert rule("SHORT", 1.0, 1.0, t0, bars(spike + [(1.30, 0.98, 1.0)]), t0) is None                       # stop 1.35
        assert rule("SHORT", 1.0, 1.0, t0, bars(spike + [(1.35, 0.98, 1.0)]), t0) == "หลุด stop 1.35 (+35.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
        early = [(2.0, 0.99, 1.0)] + flat                                        # 169 hours before the entry bar: outside
        assert rule("SHORT", 1.0, 1.0, t0, bars(early + [(1.08, 0.98, 1.0)], datetime(2026, 10, 13, 8)), t0) == "หลุด stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
        assert rule("SHORT", 1.0, 1.0, t0, bars([(3.0, 0.99, 1.0)] + flat[1:] + [(1.44, 0.98, 1.0)]), t0) is None   # 45% cap: stop 1.45
        assert rule("SHORT", 1.0, 1.0, t0, bars([(3.0, 0.99, 1.0)] + flat[1:] + [(1.45, 0.98, 1.0)]), t0) == "หลุด stop 1.45 (+45.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
        # TP at -35% (bar low), the RSI < 30 exit only at >= 20% profit, 21 days
        assert rule("SHORT", 1.0, 0.9, t0, bars(flat + [(1.0, 0.65, 0.9)]), t0) == "ถึง TP -35% · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"
        fall = [(1.0, c - 0.005, c) for c in (0.99 - k * 0.011 for k in range(19))]   # lower closes: RSI 0, profit 1%..20.8%
        assert rule("SHORT", 1.0, 0.79, t0, bars(flat + fall), t0) == "RSI 1 ชม. 0.0 < 30 ตอนกำไร 20.8%"
        assert rule("SHORT", 1.0, 0.803, t0, bars(flat + fall[:18]), t0) is None                                # 19.7% profit: hold
        t10 = t0.replace(minute=0)                                               # the entry bar closed: 21 days = bar 504 closes
        assert rule("SHORT", 1.0, 1.0, t0, bars(flat), t10 + timedelta(days=21)) == "ถือครบ 21 วัน"
        assert rule("SHORT", 1.0, 1.0, t0, bars(flat), t10 + timedelta(days=21) - timedelta(minutes=1)) is None
        # no bars or no open time: the widest stop (+45%) and TP against the mark
        assert rule("SHORT", 1.0, 1.44, None, [], t0) is None and rule("SHORT", 1.0, 1.45, None, [], t0).startswith("ราคา mark หลุด stop 1.45")
        assert rule("SHORT", 1.0, 0.65, None, [], t0) == "ราคา mark ถึง TP -35% · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"
        # LONG: stop -12%, TP +35%, trail (peak >= 15% then 8% off it), RSI > 72 at >= 18%, 7 days
        assert rule("LONG", 1.0, 1.0, t0, bars(flat + [(1.02, 0.88, 1.0)]), t0) == "หลุด stop -12% · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
        assert rule("LONG", 1.0, 1.0, t0, bars(flat + [(1.35, 0.99, 1.0)]), t0) == "ถึง TP +35% · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"
        got = rule("LONG", 1.0, 1.07, t0, bars(flat + [(1.16, 0.99, 1.15), (1.15, 1.06, 1.07)]), t0)
        assert got == "กำไรเคยถึง 16.0% แล้วย่อเหลือ 7.0%", got
        rise = [(c + 0.005, 1.0, c) for c in (1.0 + (k + 1) * 0.011 for k in range(17))]   # higher closes: RSI 100, profit 1.1%..18.7%
        assert rule("LONG", 1.0, 1.187, t0, bars(flat + rise), t0) == "RSI 1 ชม. 100.0 > 72 ตอนกำไร 18.7%"
        assert rule("LONG", 1.0, 1.176, t0, bars(flat + rise[:16]), t0) is None
        assert rule("LONG", 1.0, 1.0, t0, bars(flat), t0 + timedelta(days=7)) == "ถือครบ 7 วัน"
        assert rule("LONG", 1.0, 0.88, None, [], t0) == "ราคา mark หลุด stop -12% · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
    finally:
        team.LOWVOL_EXIT_ON = saved_lv


def test_exit_review_closes_what_the_rule_closes():
    # every round: /sync, then the rule on each held position (Binance's entry/mark, ClickHouse 1h bars), /close with the
    # rule's reason, then the plan's opens; ClickHouse down = the mark against the widest stop; portfolio unread = nothing
    from datetime import datetime, timezone
    import team
    sent, said = [], []
    pf = {"shorts": [
        {"id": "bto-2026-10-20-CT", "symbol": "CTUSDT", "coin": "CT", "side": "SHORT", "entry": "1.0", "mark": "1.02", "liq_price": "1.95", "pnl_pct": "-2.00",
         "days": 0, "funding_usdt": "0.00", "on_binance": True, "unclear": False, "opened_at": "2026-10-20T10:05:12+00:00"},
        {"id": "bto-2026-10-20-PONS", "symbol": "PONSUSDT", "coin": "PONS", "side": "SHORT", "entry": "1.0", "mark": "1.0", "pnl_pct": "0.00",
         "days": 0, "on_binance": True, "unclear": False, "opened_at": "2026-10-20T10:05:12+00:00"},
        {"id": "bto-2026-10-10-X", "symbol": "XUSDT", "coin": "X", "entry": "1", "mark": "1", "on_binance": False, "unclear": False}],   # gone: not checked
        "available_usdt": "1000", "closed": []}
    hours = [f"2026-10-{13 + k // 24:02d} {k % 24:02d}:00:00" for k in range(9, 9 + 170)]          # 2026-10-13 09:00 .. 2026-10-20 10:00
    flat = [(h, 1.01, 0.99, 1.0) for h in hours]
    bars = {"CTUSDT": flat[:-1] + [(hours[-1], 1.09, 0.99, 1.02)], "PONSUSDT": flat}                   # CT's 10:00 bar topped its 1.08 stop

    def post(path, payload, timeout=300):
        sent.append((path, payload.get("id") or payload.get("symbol"), payload.get("why")))
        return {"status": "ok", "lines": []} if path == "/sync" else {"status": "placed", "why": None, "line": f"{path} ok"}
    real = (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.binance.utcnow, team.hourly_bars, team.say,
            team.REPORTS, team.LOWVOL_EXIT_ON, team.BTC_HEDGE_ON)
    team.REPORTS, team.LOWVOL_EXIT_ON, team.BTC_HEDGE_ON = tempfile.mkdtemp(), False, False
    team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post = (lambda: pf), (lambda p: None), post
    team.binance.utcnow = lambda: datetime(2026, 10, 20, 11, 1, tzinfo=timezone.utc)
    team.hourly_bars, team.say = (lambda syms, days=30: {s: bars[s] for s in syms}), (lambda who, text: said.append((who, text)))
    try:
        team.trade("2026-10-20", ["NEWUSDT"], {"sides": {"NEWUSDT": "SHORT"}})
        assert sent == [("/sync", None, None), ("/close", "bto-2026-10-20-CT", "หลุด stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"), ("/open", "NEWUSDT", None)], sent
        who, verdict = [x for x in said if "ตรวจกติกาออก" in x[1]][-1]
        assert who == "คนซื้อขาย" and not [t for w, t in said if w == "หัวหน้าพอร์ต"], said
        assert "CT: ปิด — หลุด stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop (-2.00%)" in verdict and "PONS: ถือต่อ (0.00%)" in verdict and "X:" not in verdict, verdict
        # ClickHouse down: no bars, the mark meets the widest stop (+45%) only
        team.hourly_bars = lambda syms, days=30: 1 / 0
        sent.clear()
        team.trade("2026-10-20", [])
        assert sent == [("/sync", None, None)] and any(w == "คนซื้อขาย" and "อ่านแท่ง 1 ชม. จาก ClickHouse ไม่ได้" in t for w, t in said), sent
        pf["shorts"][1]["mark"] = "1.45"
        sent.clear()
        team.trade("2026-10-20", [])
        assert sent == [("/sync", None, None), ("/close", "bto-2026-10-20-PONS", "ราคา mark หลุด stop 1.45 (+45.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop")], sent
        # the portfolio cannot be read: nothing closes, nothing opens
        team.binance.fetch_portfolio = lambda: {"error": "ต่อ server ไม่ได้"}
        sent.clear()
        team.trade("2026-10-20", ["NEWUSDT"], {"sides": {"NEWUSDT": "SHORT"}})
        assert sent == [("/sync", None, None)] and any(w == "คนซื้อขาย" and "รอบนี้ถือทุกไม้" in t for w, t in said), sent
        # paused (OPEN_NEW False): exits still run, nothing opens
        team.binance.fetch_portfolio, team.OPEN_NEW = (lambda: {"shorts": [], "available_usdt": "1000", "closed": []}), False
        sent.clear()
        team.trade("2026-10-20", ["NEWUSDT"], {"sides": {"NEWUSDT": "SHORT"}})
        assert sent == [("/sync", None, None)] and "หยุดเปิดไม้ใหม่" in [t for w, t in said if w == "คนซื้อขาย"][-1], (sent, said[-1])
    finally:
        team.OPEN_NEW = True
        (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.binance.utcnow, team.hourly_bars, team.say,
         team.REPORTS, team.LOWVOL_EXIT_ON, team.BTC_HEDGE_ON) = real


def test_watch_closes_on_binance_15m_candles_between_rounds():
    # hourly checks closed a crossed stop up to an hour late (D:/tread2/research/loss_study/exec_gap): `team.py watch`
    # meets the stop/TP with Binance's own 15m candles since the entry, closes, books, and is silent when nothing closes
    import io
    import json
    from datetime import datetime, timedelta, timezone
    import team
    t0 = datetime(2026, 10, 20, 10, 5, tzinfo=timezone.utc)
    flat = [(f"{datetime(2026, 10, 13, 9) + timedelta(hours=k):%Y-%m-%d %H}:00:00", 1.01, 0.99, 1.0) for k in range(170)]   # stop 1.08
    saved_lv = team.LOWVOL_EXIT_ON
    team.LOWVOL_EXIT_ON = False
    rule = team.rule_exit
    assert rule("SHORT", 1.0, 1.0, t0, flat, t0 + timedelta(minutes=20), (1.07, 0.66)) is None
    assert rule("SHORT", 1.0, 1.0, t0, flat, t0 + timedelta(minutes=20), (1.08, 0.99)) == "แท่ง 15 นาทีล่าสุดแตะ stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
    assert rule("SHORT", 1.0, 1.0, t0, flat, t0 + timedelta(minutes=20), (1.0, 0.65)) == "แท่ง 15 นาทีล่าสุดแตะ TP -35% · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"
    assert rule("SHORT", 1.0, 1.09, t0, flat, t0 + timedelta(minutes=20), (1.2, 0.99)).startswith("ราคา mark หลุด stop 1.08")
    assert rule("LONG", 1.0, 1.0, t0, flat, t0 + timedelta(minutes=20), (1.0, 0.88)).startswith("แท่ง 15 นาทีล่าสุดแตะ stop -12%")
    pf = {"shorts": [{"id": "bto-2026-10-20-CT", "symbol": "CTUSDT", "coin": "CT", "side": "SHORT", "entry": "1.0", "mark": "1.02", "pnl_pct": "-2.00",
                      "on_binance": True, "unclear": False, "opened_at": "2026-10-20T10:05:12+00:00"}], "closed": []}
    sent, said, asked, candles = [], [], [], [[0, "1.0", "1.05", "0.99", "1.0"]]

    def post(path, payload, timeout=300):
        sent.append((path, payload.get("id"), payload.get("why")))
        return {"status": "ok", "lines": []} if path == "/sync" else {"status": "placed", "why": None, "line": "ปิด CT แล้ว"}

    def urlopen(url, timeout=None):
        asked.append(url)
        return io.BytesIO(json.dumps(candles).encode())
    real = (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.binance.utcnow, team.hourly_bars, team.say,
            team.REPORTS, team.recent_range, team.urllib.request.urlopen)
    team.REPORTS, team.recent_range, team.urllib.request.urlopen = tempfile.mkdtemp(), binance_candles, urlopen
    team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post = (lambda: pf), (lambda p: None), post
    team.binance.utcnow = lambda: datetime(2026, 10, 20, 12, 16, tzinfo=timezone.utc)
    team.hourly_bars, team.say = (lambda syms, days=30: {s: flat for s in syms}), (lambda who, text: said.append(text))
    try:
        team.watch()
        assert sent == [] and said == [], (sent, said)                      # below the stop: silent
        # candles since the later of the entry and the start of last hour (11:00), by open time
        assert asked == [f"{market.FAPI_KLINES}?symbol=CTUSDT&interval=15m&startTime={int(datetime(2026, 10, 20, 11, tzinfo=timezone.utc).timestamp() * 1000)}&limit=10"], asked
        candles.append([0, "1.0", "1.081", "1.0", "1.02"])
        team.watch()
        assert sent == [("/close", "bto-2026-10-20-CT", "แท่ง 15 นาทีล่าสุดแตะ stop 1.08 (+8.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"), ("/sync", None, None)], sent
        assert "ตรวจกติกาออก 1 ไม้" in said[0] and "ปิดตามกติกาออก (ตรวจทุก 15 นาที): ปิด CT แล้ว" in said[1], said
        team.urllib.request.urlopen = lambda url, timeout=None: 1 / 0        # Binance candles down: the mark is still checked
        sent.clear()
        team.watch()
        assert sent == []
        pf["shorts"][0]["mark"] = "1.08"
        team.watch()
        assert sent[0][2].startswith("ราคา mark หลุด stop 1.08"), sent
        # ClickHouse down: the 21-day exit still counts from opened_at
        pf["shorts"][0].update(mark="1.0", opened_at="2026-09-29T10:05:12+00:00")
        team.hourly_bars = lambda syms, days=30: 1 / 0
        sent.clear()
        team.watch()
        assert sent[0][2] == "ถือครบ 21 วัน", sent
    finally:
        team.LOWVOL_EXIT_ON = saved_lv
        (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.binance.utcnow, team.hourly_bars, team.say,
         team.REPORTS, team.recent_range, team.urllib.request.urlopen) = real


def test_runner_watches_exits_every_quarter_hour_between_rounds():
    import datetime as dt
    import runner
    t = dt.datetime(2026, 11, 10, 8, 15, 30, tzinfo=dt.timezone.utc)
    assert runner.watch_slot(t) == dt.datetime(2026, 11, 10, 8, 0, tzinfo=dt.timezone.utc)                          # 1 min into the quarter
    assert runner.watch_slot(t + dt.timedelta(seconds=30)) == dt.datetime(2026, 11, 10, 8, 15, tzinfo=dt.timezone.utc)
    d = tempfile.mkdtemp()
    real = (runner.CACHE, runner.REQUESTS, runner.PID_FILE, runner.kafka_up, runner.team_pid, runner.utcnow)
    runner.CACHE, runner.REQUESTS, runner.PID_FILE = d, os.path.join(d, "requests"), os.path.join(d, "team.pid")
    runner.kafka_up, runner.team_pid = (lambda: True), (lambda: None)
    os.makedirs(runner.REQUESTS)

    class Proc:
        def __init__(self, args):
            self.args, self.returncode, self.pid = args, None, 1

        def poll(self):
            return self.returncode
    started, clock = [], [dt.datetime(2026, 11, 10, 8, 20, tzinfo=dt.timezone.utc)]
    runner.utcnow = lambda: clock[0]
    r = runner.Runner()
    r.py = lambda *a, session=False: started.append(Proc(a)) or started[-1]
    teams = lambda: [p.args[1:] for p in started if p.args[0] == "team.py"]   # noqa: E731
    try:
        open(runner.PID_FILE, "w").close()
        os.utime(runner.PID_FILE, (clock[0].timestamp(),) * 2)   # the round started just now: none due
        r.tick()
        r.tick()
        assert teams() == [("watch",)], teams()                   # once per quarter
        open(os.path.join(runner.REQUESTS, "close-bto-2026-11-09-AB"), "w").close()
        open(os.path.join(runner.REQUESTS, "run"), "w").close()
        r.tick()
        assert teams() == [("watch",)] and r.cl is None, teams()  # a hand close and a round wait for a running watch
        r.wt.returncode = 0
        r.tick()
        assert teams() == [("watch",), ("close", "bto-2026-11-09-AB")], teams()
        clock[0] += dt.timedelta(minutes=15)
        r.tick()
        assert len(teams()) == 2                                  # no watch beside a hand close
        r.cl.returncode = 0
        r.tick()
        assert teams()[2:] == [("once",)], teams()                # the pressed round, then no watch beside it
        r.team.returncode = 0
        r.tick()
        assert teams()[3:] == [("watch",)], teams()               # the quarter missed behind them runs right after
    finally:
        runner.CACHE, runner.REQUESTS, runner.PID_FILE, runner.kafka_up, runner.team_pid, runner.utcnow = real


def test_short_chinese_named_perp_gets_ascii_order_ids():
    # Binance lists perps like 牛来USDT; its client ids allow ^[.A-Z:/a-z0-9_-]{1,36}$ only, so the coin part of the id is
    # a fixed hash tag: same coin -> same id every round (the rerun guard), never a real (upper-case) ticker
    import re
    fake = FakeBinance(bid="0.5")
    fake.symbols.append("牛来USDT")
    binance, restore = short_env(fake, SHORT_LIVE="1")
    ok = re.compile(r"[.A-Z:/a-z0-9_-]{1,36}")
    try:
        (r,) = opens(binance, ["牛来USDT"])
        (sell,) = [q for m, p, q in fake.posts if p in ("/fapi/v1/order", "/fapi/v1/algoOrder")]
        ids = [sell["newClientOrderId"], binance.sibling(sell["newClientOrderId"], "close")]
        assert all(ok.fullmatch(i) for i in ids) and sell["symbol"] == "牛来USDT", ids
        assert ids[0] == r["cid"] == binance.order_id("open", "2026-10-11", "牛来USDT") != binance.order_id("open", "2026-10-11", "币安人生USDT")
        assert binance.order_id("open", "2026-10-11", "TRXUSDT") == "bto-2026-10-11-TRX"      # ASCII ids unchanged
        assert binance.close_from({"id": r["cid"]})[0] == r["cid"]                            # /close takes it back
        assert r["status"] == "placed" and "牛来: เปิด short แล้ว" in r["line"], r
        assert opens(binance, ["牛来USDT"])[0]["why"] == "sent" and len(fake.sent("/fapi/v1/order")) == 1
    finally:
        restore()


def test_binance_signs_a_chinese_symbol_percent_encoded():
    # Binance: percent-encode (UTF-8) first, then sign that exact string, else -1022
    import hashlib
    import hmac
    import http.client
    import binance
    seen = []

    class Conn:
        def __init__(self, *a, **kw):
            pass

        def request(self, method, url, headers=None):
            seen.append(url)

        def getresponse(self):
            return type("R", (), {"status": 200, "read": lambda self: b"{}"})()

        def close(self):
            pass
    real, http.client.HTTPSConnection = http.client.HTTPSConnection, Conn
    try:
        binance.Client("k", "s").call("POST", "/fapi/v1/order", signed=True, symbol="牛来USDT", side="SELL")
    finally:
        http.client.HTTPSConnection = real
    q, sig = seen[0].split("?", 1)[1].rsplit("&signature=", 1)
    assert seen[0].isascii() and q.startswith("symbol=%E7%89%9B%E6%9D%A5USDT&side=SELL&"), seen
    assert sig == hmac.new(b"s", q.encode(), hashlib.sha256).hexdigest()


def test_bus_never_raises_on_a_broken_character_from_an_ai():
    # a lone surrogate (json "\ud800" in an AI answer) must not stop the round before the plan and the closes go out
    import json
    import bus
    got, real = [], bus._producer
    bus._producer = type("P", (), {"produce": lambda self, t, b: got.append(b), "poll": lambda self, n: 0})()
    try:
        bus.send("bt.team.chat", {"text": "a\ud800b"})
        assert json.loads(got[0].decode("utf-8")) == {"text": "a?b"}, got
    finally:
        bus._producer = real


def test_secretary_writes_a_plan_with_the_current_exit_rule():
    # the plan's limits must describe the exit in force (no stop order on Binance, the code checks the exit rule every round)
    import json
    import team
    tmp, real = tempfile.mkdtemp(), team.REPORTS
    team.REPORTS = tmp
    try:
        os.makedirs(os.path.join(tmp, "2026-10-13"), exist_ok=True)
        with open(os.path.join(tmp, "2026-10-13", "screen.json"), "w", encoding="utf-8") as f:
            json.dump({"rows": [{"base": "SIREN", "price": 0.42, "score": 66.2, "rsi_prev": 42.1, "rsi": 44.5, "rsi_4h_prev": 38.0, "rsi_4h": 39.2,
                                 "trend_1d": "Close<SMA7<=SMA20", "dd_ath": -0.788, "age_days": 51, "vol30_usd": 36.1e6, "r7": -0.45,
                                 "fail": ["ยังไม่เกิดสัญญาณเด้งหมดแรง"]}]}, f)
        path = team.write_plan({"run_id": "2026-10-13", "reviews": [], "data_to": "2026-10-12", "narrative": "x", "picks": [], "charts": {},
                                "entry_day": True, "short": [], "universe": 0, "passing": 0, "passing_coins": [], "held": ["CT"]})
        text = open(path, encoding="utf-8").read()
        assert "โค้ดตรวจกติกาออกทุกรอบ" in text and "1x isolated" in text and "ทีม AI ตัดสินปิด" not in text, text
        assert "SIREN" in text and "Watchlist" in text, text
        assert "## Watchlist 1 เหรียญ" in text and "## สรุปจาก AI" in text and "5 เหรียญ" not in text, text
        no_ai = open(team.write_plan({"run_id": "2026-10-13", "reviews": [], "data_to": "2026-10-12", "narrative": team.NO_PICKS_TH, "picks": [], "charts": {},
                                      "entry_day": True, "short": [], "universe": 0, "passing": 0, "passing_coins": [], "held": ["CT"]}), encoding="utf-8").read()
        assert "## สรุป (โค้ด ไม่ได้เรียก AI)" in no_ai and "สรุปจาก AI" not in no_ai, no_ai   # no AI call = no AI heading
        plan_json = json.loads(open(os.path.join(tmp, "2026-10-13", "dca-plan.json"), encoding="utf-8").read())
        assert len(plan_json["watchlist"]) == 1 and plan_json["watchlist"][0]["coin"] == "SIREN", plan_json
        assert plan_json["open_new"] is True and team.PAUSED_TH not in text, plan_json
        assert plan_json["watchlist"][0]["trend"].startswith("[พร้อมเปิด SHORT เมื่อ RSI ตัดลง]"), plan_json["watchlist"]
        team.OPEN_NEW = False   # paused: the console reads open_new, the md says so instead of "รอบนี้เปิดไม้ได้"
        text = open(team.write_plan({"run_id": "2026-10-13", "reviews": [], "data_to": "2026-10-12", "narrative": "x", "picks": [], "charts": {},
                                     "entry_day": True, "short": [], "universe": 0, "passing": 0, "passing_coins": [], "held": []}), encoding="utf-8").read()
        plan_json = json.loads(open(os.path.join(tmp, "2026-10-13", "dca-plan.json"), encoding="utf-8").read())
        assert plan_json["open_new"] is False and team.PAUSED_TH in text and "รอบนี้เปิดไม้ได้" not in text, plan_json
        assert plan_json["watchlist"][0]["trend"].startswith("[รอ RSI ตัดลง (SHORT) · หยุดเปิดไม้ใหม่อยู่]") and "พร้อมเปิด" not in text, plan_json["watchlist"]
    finally:
        team.REPORTS, team.OPEN_NEW = real, True


def test_analysis_log_records_round_for_gemini():
    import json
    import team
    tmp, real = tempfile.mkdtemp(), (team.REPORTS, team.ENTRY_SIGNALS)
    team.REPORTS, team.ENTRY_SIGNALS = tmp, os.path.join(tmp, "entry-signals.json")
    try:
        os.makedirs(os.path.join(tmp, "2026-10-14"), exist_ok=True)
        with open(os.path.join(tmp, "2026-10-14", "screen.json"), "w", encoding="utf-8") as f:
            json.dump({"data_to": "2026-10-14 03:00", "entry_day": True, "universe": 523, "late": [],
                       "rows": [pick("CT"), {**pick("BTW"), "rsi_prev": 88.5, "rsi": 91.9, "fail": ["RSI(14) 1 ชม. แท่งล่าสุด 91.9 (ต้อง < 70)"]}]}, f)
        plan = {"run_id": "2026-10-14", "data_to": "2026-10-14 03:00", "entry_day": True, "universe": 523,
                "picks": [pick("CT")], "charts": {"CT": {"chart": {"trend": "down", "note_th": "ย่อ"}}},
                "reviews": [{"coin": "CT", "risks_th": ["เสี่ยงเด้ง"]}], "narrative": "สรุป"}
        pf = {"mode": "ซื้อขายจริง", "equity_usdt": "892.53", "wallet_usdt": "892.53", "available_usdt": "792.53",
              "unrealized_usdt": "0.00", "realized_usdt": "0.00", "shorts": [{"symbol": "CTUSDT", "coin": "CT", "pnl_pct": "1.20"}], "closed": []}
        exit_info = {"positions": [{"coin": "OP"}], "charts": {}, "risk_reviews": [], "decisions": {"OP": {"action": "close", "reason_th": "ปิดทำกำไร"}}}
        team.write_analysis_log("2026-10-14", ["CTUSDT"], ["OPUSDT"], ["เปิด CT แล้ว"], pf, plan, exit_info)
        row = json.loads(open(os.path.join(tmp, "analysis-log.jsonl"), encoding="utf-8").read().strip())
        assert row["run_id"] == "2026-10-14" and row["overbought_top"][0]["coin"] == "BTW" and row["passing"][0]["coin"] == "CT", row
        assert row["entry_ai"]["short_plan"] == ["CTUSDT"] and row["exit_ai"]["close_plan"] == ["OPUSDT"], row
        md = open(os.path.join(tmp, "analysis-log.md"), encoding="utf-8").read()
        assert "BTW (88.5→91.9" in md and "OP: close (ปิดทำกำไร)" in md and "Equity 892.53 USDT" in md, md
        calls, replies = [], iter([{"status": "ok", "lines": []},
                                   {"status": "unclear", "why": None, "line": "OP: ส่งคำสั่งเปิด short แล้ว 650.4 OP สถานะ FILLED — รอราคาจาก Binance"},
                                   {"status": "ok", "lines": ["คำสั่ง bto-2026-10-14-OP ที่ไม่รู้ผล ตรวจใน Binance แล้ว: OP: เปิด short แล้ว 650.4 OP มูลค่า 99.99 USDT"]}])
        real_post, real_exit, real_say, real_hedge = team.binance.post, team.exit_review, team.say, team.BTC_HEDGE_ON
        real_pf = team.binance.fetch_portfolio, team.binance.save_portfolio   # never ask the live server or touch cache/portfolio.json
        team.BTC_HEDGE_ON = False
        team.binance.post = lambda path, payload, timeout=300: calls.append(path) or next(replies)
        team.binance.fetch_portfolio, team.binance.save_portfolio = (lambda: pf), (lambda p: p)
        team.exit_review, team.say = (lambda r, info=None: []), (lambda *a: None)
        try:
            team.trade("2026-10-14", ["OPUSDT"], {**plan, "sides": {"OPUSDT": "SHORT"}})
            assert calls == ["/sync", "/open", "/sync"], calls
        finally:
            team.binance.post, team.exit_review, team.say, team.BTC_HEDGE_ON = real_post, real_exit, real_say, real_hedge
            team.binance.fetch_portfolio, team.binance.save_portfolio = real_pf
    finally:
        team.REPORTS, team.ENTRY_SIGNALS = real


def test_long_live_opens_and_closes_1x_isolated():
    from decimal import Decimal
    fake = FakeBinance(bid="2")
    binance, restore = short_env(fake, SHORT_LIVE="1", SHORT_LOT_USDT="50")
    try:
        (r,) = opens(binance, ["SOLUSDT"], side="LONG")
        assert "SOL: เปิด long แล้ว 25 SOL" in r["line"], r
        assert fake.positions["SOLUSDT"]["positionAmt"] == "25"
        pos = binance.positions()
        assert len(pos) == 1 and pos["bto-2026-10-11-SOL"]["side"] == "LONG", pos
        fake.positions["SOLUSDT"].update(markPrice="2.20", unRealizedProfit="5.00000000")   # +10% on long (25 * 0.20 = +5.00 USDT)
        pf = binance.portfolio()
        assert pf["shorts"][0]["coin"] == "SOL" and pf["shorts"][0]["side"] == "LONG"
        assert Decimal(pf["shorts"][0]["pnl_usdt"]) == Decimal("5.00") and Decimal(pf["shorts"][0]["pnl_pct"]) == Decimal("10.00"), pf["shorts"]
        # the AI team closes the LONG -> SELL reduceOnly
        r = binance.close_one("bto-2026-10-11-SOL", "AI ตัดสินปิด")
        assert r["line"].startswith("SOL: ปิด long (AI ตัดสินปิด) → SOL: ปิด long แล้ว 25 SOL"), r
        assert (fake.sent("/fapi/v1/order")[-1]["side"], fake.sent("/fapi/v1/order")[-1]["reduceOnly"]) == ("SELL", "true")
        assert "SOLUSDT" not in fake.positions
        fake.income.append(income_row("SOLUSDT", "REALIZED_PNL", "5.00", fake.orders["bto-2026-10-11-SOL"]["updateTime"] + 1))
        binance.sync()                                                                        # records the closed position
        pf2 = binance.portfolio()
        assert pf2["shorts"] == [] and pf2["closed"][0]["coin"] == "SOL" and pf2["closed"][0]["side"] == "LONG", pf2
    finally:
        restore()


def test_market_regime_rejects_bear_bounce_when_ema21_below_ema50_or_below_daily_sma20():
    # Regression for 2026-10-16 LPT/ZK bull trap: BTC bounced 1 bar above EMA21/50 while EMA21 < EMA50 in a bear market
    import market
    btc_bounce = [82200.0] * 35 + [81900.0] * 19 + [82600.0]   # EMA21 < EMA50 despite 1-bar spike
    alt_hc = [10.0] * 31 + [10.5] * 24
    reg_trap = market.market_regime({"BTCUSDT": ([], btc_bounce), "SOLUSDT": ([], alt_hc), "ETHUSDT": ([], alt_hc)})
    assert reg_trap["btc_up_1h"] is False and reg_trap["allow_long"] is False and reg_trap["allow_short"] is True, reg_trap
    # True aligned uptrend (Close > EMA21 > EMA50 and Daily Close > SMA20) allows LONG
    btc_bull = [80000.0 + i * 50.0 for i in range(60)]
    daily_bull = {"BTCUSDT": ([], [75000.0 + i * 200.0 for i in range(25)], [])}
    reg_ok = market.market_regime({"BTCUSDT": ([], btc_bull), "SOLUSDT": ([], alt_hc), "ETHUSDT": ([], alt_hc)}, daily_bull)
    assert reg_ok["btc_up_1h"] is True and reg_ok["allow_long"] is True and reg_ok["allow_short"] is False, reg_ok
    # Even if 1H EMA21 > EMA50, if BTC Daily is below its 20-day SMA, LONG is blocked; the SHORT pause looks at BTC 1H
    # and breadth only (backtest config 4), so a strong 1H market under a bearish BTC day opens neither side
    daily_bear = {"BTCUSDT": ([], [95000.0 - i * 500.0 for i in range(25)], [])}
    reg_d_bear = market.market_regime({"BTCUSDT": ([], btc_bull), "SOLUSDT": ([], alt_hc), "ETHUSDT": ([], alt_hc)}, daily_bear)
    assert reg_d_bear["btc_up_1h"] is False and reg_d_bear["allow_long"] is False and reg_d_bear["allow_short"] is False, reg_d_bear
    alt_dn = [10.5] * 31 + [10.0] * 24                                  # breadth 1/3 <= 55%: shorts stay open
    reg_d_mixed = market.market_regime({"BTCUSDT": ([], btc_bull), "SOLUSDT": ([], alt_dn), "ETHUSDT": ([], alt_dn)}, daily_bear)
    assert reg_d_mixed["allow_long"] is False and reg_d_mixed["allow_short"] is True, reg_d_mixed
    # 2026-11-10: BTC's last daily close under its 200-day mean pauses new SHORTs (research/loss_study/regime); above it, or
    # with under 200 days of data, the gate stays out of the way
    under = {"BTCUSDT": ([], [90000.0] * 199 + [80000.0], [])}
    reg_under = market.market_regime({"BTCUSDT": ([], btc_bounce), "SOLUSDT": ([], alt_dn), "ETHUSDT": ([], alt_dn)}, under)
    assert reg_under["allow_short"] is False and "200 วัน" in reg_under["short_pause"] and "พักเปิด SHORT" in reg_under["mkt_trend"], reg_under
    over = {"BTCUSDT": ([], [80000.0] * 199 + [90000.0], [])}
    reg_over = market.market_regime({"BTCUSDT": ([], btc_bounce), "SOLUSDT": ([], alt_dn), "ETHUSDT": ([], alt_dn)}, over)
    assert reg_over["allow_short"] is True and reg_over["short_pause"] == "", reg_over
    short_hist = {"BTCUSDT": ([], [90000.0] * 198 + [80000.0], [])}
    assert market.market_regime({"BTCUSDT": ([], btc_bounce), "SOLUSDT": ([], alt_dn)}, short_hist)["allow_short"] is True


def test_short_row_counts_age_hours_and_4h_bars_like_the_backtest():
    # research/long_short_trend_backtest.py: age = today - first day, SHORT only after 24 hourly bars, 4H RSI only on a UTC 4H close
    import market
    from datetime import date, timedelta
    day = [date(2026, 6, 1) + timedelta(days=i) for i in range(20)]
    px = [30 * 0.99 ** i for i in range(20)]                            # aligned daily downtrend, -17% from the top
    hc = [20.0] * 30 + [20 + 0.02 * k for k in range(20)]
    hc += [hc[-1] - 0.03 * k for k in range(1, 12)]                     # 4H RSI 65.5 -> 53.0, 1H RSI stays ~30
    four = market.short_row("XUSDT", "X", day, px, [8e6] * 20, hc)
    assert four["age_days"] == 20 and four["signal"] == "YoungDown≤30d (4H-RSI<55)" and four["fail"] == [], four
    mid = market.short_row("XUSDT", "X", day, px, [8e6] * 20, hc, h4_close=False)   # a 4H bar is still open: no 4H signal
    assert mid["signal"] == "YoungDown≤30d (RSI<55)" and mid["fail"], mid
    hc_cross = [10.0 + i for i in range(15)] + [16.0]                   # 1H RSI 100 -> 61.9
    young = market.short_row("NEWUSDT", "NEW", day[:2], [30.0, 27.0], [8e6] * 2, hc_cross)
    assert young["age_days"] == 2 and young["fail"] == ["เข้าซื้อขายได้ 16 ชม. (SHORT ต้องรอครบ 24 ชม.)"], young
    young = market.short_row("NEWUSDT", "NEW", day[:2], [30.0, 27.0], [8e6] * 2, [10.0] * 9 + hc_cross)
    assert young["fail"] == [] and young["signal"].startswith("NewListing≤7d"), young


def test_short_row_matches_backtest_config_4():
    # research/long_short_trend_backtest.py config 4 (BI-DIRECTIONAL MKT-SWITCH A): tiers judge the day by yesterday's
    # daily close (display fields stay real-time for the AI exit team), SHORT pullback from the highest daily high,
    # LONG tiers ATH / 90d / Young need 24 hourly bars and a 1H RSI cross up from above 25
    from datetime import date, timedelta
    day = [date(2026, 6, 1) + timedelta(days=i) for i in range(120)]
    bull = {"allow_long": True, "allow_short": True}
    hc_cross60 = [10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 11.5]  # 1H RSI ~65.7 -> ~52.8
    hc_up = [10.0] * 10 + [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0, 10.0, 9.8, 9.6, 9.4, 9.2, 9.0, 10.8]  # 26 bars, ~41 -> ~58
    # yesterday closed under SMA7/20 (27.6 < 27.9 < 28.55); this hour bounced above them, 1H RSI 62.5 -> 59.0
    px_down = [30.0 - i * 0.1 for i in range(25)]
    base = [28.4 + 0.3 * (k % 2) for k in range(20)]
    hc_bounce = base + [base[-1] + (30.4 - base[-1]) * (k + 1) / 8 for k in range(8)] + [30.4 - 0.2 * (k + 1) for k in range(3)]
    for regime in (None, bull):   # a LONG tier that did not trigger must not hide the SHORT
        bounce = market.short_row("BOUNCEUSDT", "BOUNCE", day[:25], px_down, [8e6] * 25, hc_bounce, mkt_regime=regime)
        assert bounce["side"] == "SHORT" and bounce["fail"] == [] and bounce["signal"] == "YoungDown≤30d (RSI<55)", bounce
    assert bounce["trend_1d"] == "ขาขึ้น (เหนือ SMA7/20)" and bounce["downtrend_1d"] is False
    assert abs(bounce["score"] - (50 - 25 * 1.5 - (27.6 / 30 - 1) * 20 + bounce["rsi_prev"])) < 1e-3, bounce
    # a new listing <= 7d shorts on a 1H cross only (no 4H path); its pullback counts from the highest daily high
    hc_4h = [x for x in [10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.0, 10.0, 9.5, 10.2, 10.6, 11.0, 11.4, 11.8, 10.5] for _ in range(4)]
    new4h = market.short_row("NEW4HUSDT", "NEW4H", day[:5], [30.0 - 2.5 * i for i in range(5)], [8e6] * 5, hc_4h)
    assert new4h["signal"] == "NewListing≤7d (RSI<55)" and new4h["fail"], new4h
    wick = market.short_row("WICKUSDT", "WICK", day[:5], [10.0, 10.5, 11.0, 11.2, 11.0], [8e6] * 5, hc_cross60,
                            hi=[10.2, 11.0, 13.0, 11.5, 11.2])          # 11.0 / 13.0 = -15% from the wick
    assert wick["fail"] == [] and wick["signal"] == "NewListing≤7d (RSI<55)", wick
    px_ath = [10.0 + i * 0.15 for i in range(60)]
    low_rsi = [20.0] * 10 + [20.0 - 0.5 * k for k in range(1, 15)] + [24.0]       # 1H RSI 0 -> 71.6: no LONG from <= 25
    assert market.short_row("SUIUSDT", "SUI", day[:60], px_ath, [15e6] * 60, low_rsi, mkt_regime=bull)["side"] == "SHORT"
    assert market.short_row("SUIUSDT", "SUI", day[:60], px_ath, [15e6] * 60, hc_up[-16:], mkt_regime=bull)["side"] == "SHORT"
    ath = market.short_row("SUIUSDT", "SUI", day[:60], px_ath, [15e6] * 60, hc_up, mkt_regime=bull)
    assert ath["side"] == "LONG" and ath["fail"] == [], ath
    assert abs(ath["score"] - (95 + ath["rsi"] + (px_ath[-1] / px_ath[-8] - 1) * 30)) < 1e-3, ath
    # ATH breakout needs Close above SMA7/20 only (SMA7 < SMA20 after a dip still counts)
    dip = market.short_row("DIPUSDT", "DIP", day[:20], [10.0] * 13 + [8.0] * 6 + [10.0], [15e6] * 20, hc_up, mkt_regime=bull)
    assert dip["fail"] == [] and dip["signal"] == "ATH-Breakout (LONG RSI>55)", dip
    # 90-day breakout: at the 90-day high, 8.7% under an older ATH, Close > SMA7 >= SMA20
    px_90 = [12.0] * 10 + [9.0 + k * 0.018 for k in range(110)]
    b90 = market.short_row("B90USDT", "B90", day[:120], px_90, [15e6] * 120, hc_up, mkt_regime=bull)
    assert b90["fail"] == [] and b90["signal"] == "90d-Breakout (LONG RSI>55)", b90
    assert abs(b90["score"] - (80 + b90["rsi"] + (px_90[-1] / px_90[-8] - 1) * 30)) < 1e-3, b90


def test_short_screen_judges_breadth_and_day_like_the_backtest():
    # breadth counts liquid, non-gold perps that traded this hour; the 00 UTC hour is judged by the day before it
    import datetime as dt
    days = [dt.date(2026, 9, 13) + dt.timedelta(days=i) for i in range(30)]          # to 2026-10-12
    hrs = [f"{dt.datetime(2026, 10, 10, 13) + dt.timedelta(hours=k):%Y-%m-%d %H:00:00}" for k in range(60)]  # to 10-13 00:00
    up = [100.0 + k for k in range(60)]
    daily = {"BTCUSDT": (days, [50000.0 + 100 * i for i in range(30)], [9e9] * 30),
             "DOWNUSDT": (days, [20.0 - 0.1 * i for i in range(30)], [30e6] * 30),
             "ILLUSDT": (days, [5.0] * 30, [1e6] * 30),                                     # never $5M a day
             "PEGUSDT": (days, [1.0 + 0.001 * (i % 2) for i in range(30)], [30e6] * 30),    # stablecoin
             "STALEUSDT": (days, [5.0] * 30, [30e6] * 30),                                  # no bar this hour
             "PAXGUSDT": (days, [3000.0] * 30, [30e6] * 30)}                                # gold
    hourly = {s: (hrs, up) for s in daily}
    hourly["BTCUSDT"] = (hrs, [53000.0 + 10 * k for k in range(60)])
    hourly["DOWNUSDT"] = (hrs, [200.0 - k for k in range(60)])
    hourly["STALEUSDT"] = (hrs[:-1], up[:-1])
    newest = {s: "2026-10-13 00:45:00" for s in daily}
    real = market.usdt_perps, market.load_perp_daily, market.load_perp_hourly, market.perp_funding
    market.usdt_perps = lambda: {s: s[:-4] for s in daily}
    market.load_perp_daily = lambda syms: (daily, {}, {})
    market.load_perp_hourly = lambda syms: (hourly, newest)
    market.perp_funding = lambda: {}
    try:
        # Binance funding unreadable: the funding gate cannot judge any coin, so the round opens no SHORT
        rows, meta = market.legacy_short_screen(dt.datetime(2026, 10, 13, 1, 5, tzinfo=dt.timezone.utc))
        assert meta["allow_short"] is False and "funding" in meta["mkt_trend"], meta
        assert all(r["fail"] for r in rows if r["side"] == "SHORT"), rows
        market.perp_funding = lambda: {"BTCUSDT": 0.0}
        rows, meta = market.legacy_short_screen(dt.datetime(2026, 10, 13, 1, 5, tzinfo=dt.timezone.utc))
        assert meta["breadth_1h"] == 0.5 and meta["allow_short"] is True and meta["allow_long"] is True, meta  # BTC up, DOWN down
        assert {r["symbol"]: r["age_days"] for r in rows}["DOWNUSDT"] == 30
        market.load_perp_hourly = lambda syms: ({s: (h[:-1], c[:-1]) for s, (h, c) in hourly.items()}, newest)
        rows, meta = market.legacy_short_screen(dt.datetime(2026, 10, 13, 0, 5, tzinfo=dt.timezone.utc))  # last hour 10-12 23:00
        assert meta["data_to"] == "2026-10-12 23:00" and {r["symbol"]: r["age_days"] for r in rows}["DOWNUSDT"] == 29, rows
    finally:
        market.usdt_perps, market.load_perp_daily, market.load_perp_hourly, market.perp_funding = real


def test_rule_text_names_every_live_entry_tier():
    # ลุงจืด 2026-11-22 "เล่นเฉพาะ short" + "สัญญาณแท่ง 1 ชม.": live strategy is 1h | LOWVOL_BREAKDOWN | G_BREADTH (SHORT only,
    # no BTC hedge leg, entries any hour); team.SHORT_RULE_TH is built at import from the live switches
    import inspect
    import team
    assert market.short_screen is market.lowvol_breakdown_screen
    assert "lv_signal() if is_setup" in inspect.getsource(market.lowvol_breakdown_eval)
    real = market.LV_SHORT_ONLY, market.LV_TF
    try:
        market.LV_SHORT_ONLY, market.LV_TF = True, "1h"
        assert market.lv_signal() == "1H-LowVolBrk (G_BREADTH)"
        market.LV_TF = "1d"
        assert market.lv_signal() == "1D-LowVolBrk (G_BREADTH)"
        market.LV_SHORT_ONLY = False
        assert market.lv_signal() == "1D-LowVolBrk (H_DELTA0)"
    finally:
        market.LV_SHORT_ONLY, market.LV_TF = real
    assert team.TF1H is True
    for term in ("1H Low-Vol Support Breakdown", "SHORT ล้วน", "เปิดได้ทุกชั่วโมง", "1h | LOWVOL_BREAKDOWN | G_BREADTH", "TEST -0.96%/เดือน p=0.8277",
                 "ไม่ผ่านเกณฑ์สถิติ", "แท่ง 1 ชม. ≥ 55 แท่ง", "Low 20 แท่ง 1 ชม.", "Base Universe ≥ 25", "< 45%", "< +10%", "ไม้ละ 100 USDT",
                 "ไม่เกิน 8 ไม้", "3×ATR20 ของแท่ง 1 ชม.", "-30%", "+15%", "168 ชม. (7 วัน)", "1x isolated"):
        assert term in team.SHORT_RULE_TH, (term, team.SHORT_RULE_TH)
    for term in ("ATH-Breakout", "BTC Hedge", "0.001 BTC", "p=0.0086", "07:00–08:59", "21 วัน", "+1.01%", "1D Low-Vol"):
        assert term not in team.SHORT_RULE_TH, (term, team.SHORT_RULE_TH)


def test_perp_funding_is_per_day_like_the_backtest():
    # the backtest gates on that day's funding sum >= -0.9%; live scales each coin's last rate to a day by its own funding
    # interval (fundingInfo lists the non-8h coins; most new listings fund every 4h, so -0.20%/round = -1.20%/day)
    import io
    import json
    pages = {market.FAPI_FUNDING_INFO: [{"symbol": "FOURUSDT", "fundingIntervalHours": 4},
                                        {"symbol": "ONEUSDT", "fundingIntervalHours": 1}],
             market.FAPI_FUNDING: [{"symbol": "FOURUSDT", "lastFundingRate": "-0.00200000"},
                                   {"symbol": "EIGHTUSDT", "lastFundingRate": "-0.00250000"},
                                   {"symbol": "ONEUSDT", "lastFundingRate": "0.00010000"}]}
    real = market.urllib.request.urlopen
    market.urllib.request.urlopen = lambda url, timeout=None: io.BytesIO(json.dumps(pages[url]).encode())
    try:
        f = market.perp_funding()
    finally:
        market.urllib.request.urlopen = real
    assert {k: round(v, 6) for k, v in f.items()} == {"FOURUSDT": -0.012, "EIGHTUSDT": -0.0075, "ONEUSDT": 0.0024}, f
    assert f["FOURUSDT"] < market.SHORT_MIN_FUNDING_DAY <= f["EIGHTUSDT"]

def test_runner_schedules_rounds_like_the_app():
    # the server's runner.py took over the Windows app's timer: a round an hour after the last one started
    # (cache/team.pid mtime), 10 min after a failed start; never ran = due now
    import datetime as dt
    import runner
    t = dt.datetime(2026, 10, 18, 8, 0, tzinfo=dt.timezone.utc)
    assert runner.next_auto(None, None) < t
    assert runner.next_auto(t, None) == t + dt.timedelta(minutes=60)
    assert runner.next_auto(t, t) == t + dt.timedelta(minutes=60)                                   # a good start: wait out the hour
    assert runner.next_auto(t, t + dt.timedelta(minutes=55)) == t + dt.timedelta(minutes=65)       # failed at 08:55: retry 09:05
    assert runner.next_auto(None, t) == t + dt.timedelta(minutes=10)
    # ลุงจืด 2026-10-18: rounds start at minute 0 — a round pressed at 10:23 (or one that started late) is followed at 11:00
    t23 = dt.datetime(2026, 10, 18, 10, 23, 41, tzinfo=dt.timezone.utc)
    assert runner.next_auto(t23, t23) == dt.datetime(2026, 10, 18, 11, 0, tzinfo=dt.timezone.utc)
    assert runner.next_auto(t + dt.timedelta(seconds=7), None) == t + dt.timedelta(minutes=60)



def test_runner_closes_by_hand_at_once_even_beside_a_round():
    # the console's ปิดไม้ button (cache/requests/close-<id>): starts within one tick, beside a round too (ลุงจืด 2026-11-11: a press
    # waited 6 min behind a round), never two closes at once, a bad name is never run, a due round waits
    import runner
    d = tempfile.mkdtemp()
    real = (runner.CACHE, runner.REQUESTS, runner.PID_FILE, runner.kafka_up, runner.team_pid)
    runner.CACHE, runner.REQUESTS, runner.PID_FILE = d, os.path.join(d, "requests"), os.path.join(d, "team.pid")
    runner.kafka_up, runner.team_pid = (lambda: True), (lambda: None)
    os.makedirs(runner.REQUESTS)

    class Proc:
        def __init__(self, args):
            self.args, self.returncode, self.pid = args, None, 1

        def poll(self):
            return self.returncode
    started = []
    r = runner.Runner()
    r.py = lambda *a, session=False: started.append(Proc(a)) or started[-1]
    teams = lambda: [p.args for p in started if p.args[0] == "team.py"]   # noqa: E731
    try:
        open(runner.PID_FILE, "w").close()
        os.utime(runner.PID_FILE, (0, 0))   # last round long ago: one is due
        open(os.path.join(runner.REQUESTS, "close-bto-2026-11-09-EF"), "w").close()
        r.team = Proc(("team.py", "once"))   # a round is running: the close starts beside it, told so (no second chat line)
        r.tick()
        assert teams() == [("team.py", "close-in-round", "bto-2026-11-09-EF")] and os.listdir(runner.REQUESTS) == [], teams()
        for n in ("close-bto-2026-11-09-AB", "close-bto-x.y", "close-bto-2026-11-09-CD"):
            open(os.path.join(runner.REQUESTS, n), "w").close()
        r.tick()                            # that close is still working: the next presses wait for it
        assert len(teams()) == 1 and len(os.listdir(runner.REQUESTS)) == 3, (teams(), os.listdir(runner.REQUESTS))
        started.clear()
        r.cl.returncode = 0
        r.team.returncode = 0               # the round ended: the closes go first, the due round waits
        open(os.path.join(runner.REQUESTS, "run"), "w").close()
        good, r.py = r.py, lambda *a, session=False: 1 / 0
        try:                                # a failed start keeps every press for the next tick
            r.tick()
        except ZeroDivisionError:
            pass
        assert len(os.listdir(runner.REQUESTS)) == 4 and r.cl is None, os.listdir(runner.REQUESTS)
        r.py = good
        r.tick()
        assert teams() == [("team.py", "close", "bto-2026-11-09-AB", "bto-2026-11-09-CD")], teams()
        assert sorted(os.listdir(runner.REQUESTS)) == ["close-bto-x.y", "run"] and "กำลังปิด 2 ไม้" in r.status, r.status
        import json
        state = lambda: json.load(open(os.path.join(d, "runner.json"), encoding="utf-8"))   # noqa: E731
        assert state()["closing"] is True and state()["team_running"] is False   # deploy.sh waits on either
        r.tick()
        assert len(teams()) == 1 and "run" in os.listdir(runner.REQUESTS)
        r.cl.returncode = 0
        r.tick()
        assert teams()[1:] == [("team.py", "once")] and os.listdir(runner.REQUESTS) == ["close-bto-x.y"], (teams(), r.status)
        assert state()["closing"] is False
        # cache/requests removed by hand: rounds and runner.json go on
        import shutil
        shutil.rmtree(runner.REQUESTS)
        r.team.returncode, r.last_attempt = 0, None
        r.tick()
        assert teams()[-1] == ("team.py", "once") and len(teams()) == 3, teams()
    finally:
        runner.CACHE, runner.REQUESTS, runner.PID_FILE, runner.kafka_up, runner.team_pid = real


def test_runner_wakes_within_a_second_for_a_pressed_close():
    # ลุงจืด 2026-11-12 "กดปิดไม้ ปิดทันที": the 5 s wait between ticks ends at the first second that sees a close request
    import runner
    d = tempfile.mkdtemp()
    real = (runner.REQUESTS, runner.Runner, runner.time.sleep)
    runner.REQUESTS = os.path.join(d, "requests")
    naps, ticks = [], []

    class R:
        status = ""

        def tick(self):
            ticks.append(len(naps))
            if len(ticks) == 3:
                raise KeyboardInterrupt

    def sleep(s):
        naps.append(s)
        if len(naps) == 7:   # 5 quiet seconds, then a press in the 2nd second of the next wait
            open(os.path.join(runner.REQUESTS, "close-bto-2026-11-09-AB"), "w").close()
    runner.Runner, runner.time.sleep = R, sleep
    try:
        runner.main()
    except KeyboardInterrupt:
        pass
    finally:
        runner.REQUESTS, runner.Runner, runner.time.sleep = real
    assert ticks == [0, 5, 7] and set(naps) == {1}, (ticks, naps)

def test_close_button_closes_only_the_bot_positions_it_names():
    import team
    sent, said = [], []
    pf = {"shorts": [{"id": "bto-2026-11-09-CT", "symbol": "CTUSDT", "coin": "CT", "on_binance": True, "unclear": False}]}

    def post(path, payload, timeout=300):
        sent.append((path, payload.get("id"), payload.get("why")))
        return {"status": "ok", "lines": ["synced"]} if path == "/sync" else {"status": "placed", "why": None, "line": "CT: ปิดแล้ว"}
    real = (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.say, team.REPORTS)
    team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post = (lambda: pf), (lambda p: None), post
    team.say, team.REPORTS = (lambda who, text: said.append((who, text))), tempfile.mkdtemp()
    try:
        team.close_by_hand(["bto-2026-11-09-CT", "bto-2026-11-09-GONE"])
        assert sent == [("/close", "bto-2026-11-09-CT", "ลุงจืดกดปิดจากหน้า console"), ("/sync", None, None)], sent
        who, text = said[-1]
        assert who == "คนซื้อขาย" and "ลุงจืดกดปิดไม้จากหน้า console" in text and "CT: ปิดแล้ว" in text and "bto-2026-11-09-GONE: ไม่มีไม้นี้" in text, text
        # no Secretary between rounds: the chat tab's file gets the line straight away
        chat = os.path.join(team.REPORTS, "team-chat", os.listdir(os.path.join(team.REPORTS, "team-chat"))[0])
        assert open(chat, encoding="utf-8").read().endswith(f"**คนซื้อขาย**: {text}\n")
        # beside a round its Secretary is recording the topic: the line goes to the file only, never to the topic (it would be written twice)
        n = len(said)
        team.close_by_hand(["bto-2026-11-09-CT"], in_round=True)
        assert len(said) == n and open(chat, encoding="utf-8").read().count("**: [") == 2, said[n:]
        # the close itself fails: the line says press again, not "next round" (the exit rule may hold it)
        team.binance.post = lambda path, payload, timeout=300: {"status": "ok", "lines": []} if path == "/sync" else 1 / 0
        team.close_by_hand(["bto-2026-11-09-CT"])
        assert "กดปิดไม้ใหม่" in said[-1][1] and "รอบหน้าตรวจกติกาใหม่" not in said[-1][1], said[-1]
        team.binance.post = post
        # the portfolio cannot be read: nothing is sent but the bookkeeping
        team.binance.fetch_portfolio = lambda: {"error": "ต่อ server ไม่ได้"}
        sent.clear()
        team.close_by_hand(["bto-2026-11-09-CT"])
        assert sent == [("/sync", None, None)] and "ยังไม่ได้ส่งคำสั่ง" in said[-1][1], (sent, said[-1])
    finally:
        team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.say, team.REPORTS = real

def test_quant_waits_in_round_until_clickhouse_has_the_hour():
    # a round starts at HH:00 but CoinLake sweeps the last 15m bar in over ~6 min: the quant re-screens every 30 s
    # while perps are late and entry_day is off (short_screen turns entry_day on at HH:15 without the late ones)
    import bus
    import team
    sent, sleeps, got = [], [], {}
    row = {"symbol": "AUSDT", "base": "A", "price": 1.0, "age_days": 8, "vol30_usd": 3e7, "r7": None, "fail": []}
    real = bus.listen, bus.send, market.short_screen, team.held_shorts, team.cooldown_shorts, team.REPORTS, team.time.sleep
    bus.listen = lambda group, topics, handle, **kw: got.update(handle=handle)
    bus.send = lambda t, m: sent.append(m) if t == "bt.dca.candidates" else None
    team.held_shorts, team.cooldown_shorts, team.REPORTS = (lambda: set()), (lambda: set()), tempfile.mkdtemp()
    team.time.sleep = sleeps.append
    try:
        team.quant()
        for metas in ([(["A"], False), (["A"], False), ([], True)],   # complete at the 3rd look
                      [(["A"], True)],                                 # HH:15 passed: go on without the late ones
                      [([], False)]):                                  # no live perp at all (ClickHouse empty): nothing to wait for
            sleeps.clear()
            screens = iter(metas)
            market.short_screen = lambda: (lambda late, entry: ([dict(row)], {"universe": 1, "data_to": "2026-10-18 03:00",
                                                                              "entry_day": entry, "late": late}))(*next(screens))
            got["handle"]([("bt.dca.request", {"run_id": "2026-10-18"})])
            assert len(sleeps) == len(metas) - 1 and set(sleeps) <= {30}, sleeps
            assert sent[-1]["entry_day"] is metas[-1][1]
    finally:
        bus.listen, bus.send, market.short_screen, team.held_shorts, team.cooldown_shorts, team.REPORTS, team.time.sleep = real

def test_buyer_cools_a_coin_4h_after_its_last_close_and_one_day_after_a_loss():
    # SHORT_REST_HOURS = 4 (ลุงจืด 2026-11-09) after any close, SHORT_COOLDOWN_DAYS = 1 (2026-10-26) after a loss, decided on .88:
    # the coin's LAST close and its unrounded pnl; a close
    # stamped a little ahead of this clock still counts (no upper bound); the buyer skips held and cooled coins
    from datetime import datetime, timezone
    import team
    t = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    pf = {"available_usdt": "1000", "shorts": [{"symbol": "FUSDT", "coin": "F"}], "held_symbols": ["GUSDT"], "closed": [
        {"symbol": "AUSDT", "pnl": "-0.001", "pnl_usdt": "-0.00", "closed_at": "2026-10-12T00:00:00+00:00"},   # rounds to 0.00: still a loss
        {"symbol": "BUSDT", "pnl": "-5", "pnl_usdt": "-5.00", "closed_at": "2026-10-11T12:00:00+00:00"},       # exactly 1 day ago: done
        {"symbol": "CUSDT", "pnl": "2", "pnl_usdt": "2.00", "closed_at": "2026-10-12T08:00:00+00:00"},         # its last close won exactly 4 h ago: done
        {"symbol": "CUSDT", "pnl": "-5", "pnl_usdt": "-5.00", "closed_at": "2026-10-12T06:00:00+00:00"},
        {"symbol": "DUSDT", "pnl": "-1", "pnl_usdt": "-1.00", "closed_at": "2026-10-12T11:59:00+00:00"},       # its last close lost
        {"symbol": "DUSDT", "pnl": "3", "pnl_usdt": "3.00", "closed_at": "2026-10-12T06:00:00+00:00"},
        {"symbol": "EUSDT", "pnl_usdt": "-2.00", "closed_at": "2026-10-12T10:00:00+00:00"},                     # an older server: no pnl
        {"symbol": "IUSDT", "pnl": "-1", "pnl_usdt": "-1.00", "closed_at": "2026-10-12T12:05:00+00:00"},       # server clock ahead
        {"symbol": "JUSDT", "pnl": "4", "pnl_usdt": "4.00", "closed_at": "2026-10-12T08:01:00+00:00"},         # won 3 h 59 m ago: rests
        {"symbol": "KUSDT", "pnl": "1", "pnl_usdt": "1.00", "closed_at": "2026-10-12T07:59:00+00:00"}]}        # won 4 h 1 m ago: open
    assert (team.SHORT_REST_HOURS, team.SHORT_COOLDOWN_DAYS) == (4, 1)
    assert team.cooldown_shorts(t, pf) == {"AUSDT", "DUSDT", "EUSDT", "IUSDT", "JUSDT"}
    sent, lines = [], []
    real = team.binance.fetch_portfolio, team.binance.post, team.now, team.BTC_HEDGE_ON
    team.binance.fetch_portfolio, team.now, team.BTC_HEDGE_ON = (lambda: pf), (lambda: t), False
    team.binance.post = lambda path, payload, timeout=300: sent.append(payload["symbol"]) or {"status": "placed", "why": None, "line": "ok"}
    try:
        syms = ["AUSDT", "BUSDT", "CUSDT", "FUSDT", "GUSDT", "HUSDT", "JUSDT", "KUSDT"]
        assert team.open_new("2026-10-12", syms, dict.fromkeys(syms, "SHORT"), lines) is False
        assert sent == ["BUSDT", "CUSDT", "HUSDT", "KUSDT"], lines
        rest = "ไม้ล่าสุดเพิ่งปิด ยังไม่ครบเวลาพัก (ทุกไม้ 4 ชม. · ปิดขาดทุน 1 วัน)"
        assert f"ข้าม A: {rest}" in lines and f"ข้าม J: {rest}" in lines and "ข้าม F: มี position อยู่แล้ว" in lines and "ข้าม G: มี position อยู่แล้ว" in lines
    finally:
        team.binance.fetch_portfolio, team.binance.post, team.now, team.BTC_HEDGE_ON = real


def test_buyer_keeps_closing_after_one_close_fails_and_opens_only_when_sure():
    # every close is tried and a failed one is said loudly; an answer that may have sent an order brings a second /sync;
    # nothing opens when the portfolio cannot be read or a bot order on Binance is unclear; an /open with no answer stops the opens
    import team
    pf = {"available_usdt": "1000", "closed": [], "shorts": [
        {"id": "bto-2026-10-11-CT", "symbol": "CTUSDT", "coin": "CT", "on_binance": True, "unclear": False},
        {"id": "bto-2026-10-11-OP", "symbol": "OPUSDT", "coin": "OP", "on_binance": True, "unclear": False}]}
    sent, said, close = [], [], ["CTUSDT", "OPUSDT"]

    def post(path, payload, timeout=300):
        sent.append((path, payload.get("id") or payload.get("symbol")))
        if path == "/sync":
            return {"status": "ok", "lines": [], "unclear": 0}
        if payload.get("id") == "bto-2026-10-11-CT" or payload.get("symbol") == "AUSDT":
            raise RuntimeError("server ตอบ HTTP 500")
        return dict(answer)
    answer = {"status": "unclear", "why": None, "line": "ส่งแล้ว"}
    real = (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.exit_review, team.say,
            team.write_analysis_log, team.BTC_HEDGE_ON)
    team.BTC_HEDGE_ON = False
    team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post = (lambda: pf), (lambda p: p), post
    team.exit_review = lambda run_id, info=None: info.update(ids={x["symbol"]: x["id"] for x in pf["shorts"]}) or list(close)
    team.say, team.write_analysis_log = (lambda who, text: said.append(text)), (lambda *a, **k: None)
    plan = {"sides": {"AUSDT": "SHORT", "BUSDT": "SHORT"}}
    try:
        team.trade("2026-10-13", [])
        assert sent == [("/sync", None), ("/close", "bto-2026-10-11-CT"), ("/close", "bto-2026-10-11-OP"), ("/sync", None)], sent
        assert any("⚠️ ปิด CT ไม่สำเร็จ" in t for t in said), said
        close[:] = ["OPUSDT"]                                                                   # an unclear close on its own syncs again
        for a, again in (({"status": "unclear", "why": None, "line": "ส่งแล้ว"}, True),
                         ({"status": "refused", "why": "qty_mismatch", "line": "จำนวนไม่ตรง"}, False)):
            answer, n = a, len(said)
            sent.clear()
            team.trade("2026-10-13", [])
            assert sent == [("/sync", None), ("/close", "bto-2026-10-11-OP")] + [("/sync", None)] * again, (a, sent)
        assert any("⚠️ ปิด OP ไม่ได้ (qty_mismatch)" in t for t in said[n:]), said[n:]
        answer = {"status": "unclear", "why": None, "line": "ส่งแล้ว"}
        pf["closed"] = [{"symbol": "XUSDT", "pnl": "-1", "closed_at": None}]                   # open_new raises after the close went out
        sent.clear()
        team.trade("2026-10-13", ["BUSDT"], plan)
        assert sent == [("/sync", None), ("/close", "bto-2026-10-11-OP"), ("/sync", None)], sent
        pf["closed"] = []
        boom = [RuntimeError("bus down")]                                                       # an error inside the closes, after OP went out

        def say(who, text):
            if boom and "ปิด CT" in text:
                raise boom.pop()
            said.append(text)
        team.say, close[:] = say, ["OPUSDT", "CTUSDT"]
        sent.clear()
        team.trade("2026-10-13", [])
        assert sent == [("/sync", None), ("/close", "bto-2026-10-11-OP"), ("/close", "bto-2026-10-11-CT"), ("/sync", None)], sent
        team.say = lambda who, text: said.append(text)
        close[:] = []
        sent.clear()
        team.trade("2026-10-13", ["AUSDT", "BUSDT"], plan)                                     # A has no answer: B waits for the next round
        assert sent == [("/sync", None), ("/open", "AUSDT"), ("/sync", None)], sent
        pf["shorts"][1]["unclear"] = True
        sent.clear()
        team.trade("2026-10-13", ["BUSDT"], plan)
        assert sent == [("/sync", None)] and "OP มีคำสั่งที่ยังไม่รู้ผล" in said[-1], said[-1]
        for broken in ({"error": "ต่อ server ไม่ได้"}, {"available_usdt": "1000"}):
            team.binance.fetch_portfolio = lambda: broken
            sent.clear()
            team.trade("2026-10-13", ["BUSDT"], plan)
            assert sent == [("/sync", None)] and "ไม่เปิดไม้ใหม่รอบนี้: อ่านพอร์ตจาก server ไม่ได้" in said[-1], (broken, said[-1])
    finally:
        (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.exit_review, team.say,
         team.write_analysis_log, team.BTC_HEDGE_ON) = real


def test_live_is_short_only_and_holds_at_most_max_open():
    # ลุงจืด 2026-11-08: a LONG in a plan is skipped, and a round stops at MAX_OPEN positions on Binance (his own counted)
    import team
    real = team.binance.fetch_portfolio, team.binance.post, market.LONG_ON, team.BTC_HEDGE_ON
    sent, lines = [], []
    market.LONG_ON, team.BTC_HEDGE_ON = False, False
    held = [f"H{i:02d}USDT" for i in range(team.MAX_OPEN - 2)]
    team.binance.fetch_portfolio = lambda: {"available_usdt": "5000", "shorts": [], "held_symbols": held, "closed": []}
    team.binance.post = lambda path, payload, timeout=300: sent.append(payload) or {"status": "placed", "why": None, "line": "ok"}
    try:
        syms = ["AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT"]
        team.open_new("2026-11-08", syms, {"AAUSDT": "LONG", "BBUSDT": "SHORT", "CCUSDT": "SHORT", "DDUSDT": "SHORT"}, lines)
        assert [(p["symbol"], p["side"], p["usdt"]) for p in sent] == [("BBUSDT", "SHORT", "100"), ("CCUSDT", "SHORT", "100")], sent
        assert "ข้าม AA: ปิดฝั่ง LONG (SHORT อย่างเดียว)" in lines and lines[-1].startswith(f"ถือครบ {team.MAX_OPEN} ไม้"), lines
        # 2 bot shorts closed earlier this round (still in p["shorts"] until /sync, off Binance) free their slots
        gone = [{"symbol": s, "coin": s[:-4], "entry": "1.5", "on_binance": False} for s in ("XXUSDT", "YYUSDT")]
        team.binance.fetch_portfolio = lambda: {"available_usdt": "5000", "shorts": gone, "held_symbols": held, "closed": []}
        sent.clear(), lines.clear()
        team.open_new("2026-11-08", ["XXUSDT", "BBUSDT", "CCUSDT", "DDUSDT"], dict.fromkeys(["XXUSDT", "BBUSDT", "CCUSDT", "DDUSDT"], "SHORT"), lines)
        assert [p["symbol"] for p in sent] == ["BBUSDT", "CCUSDT"], (sent, lines)
    finally:
        team.binance.fetch_portfolio, team.binance.post, market.LONG_ON, team.BTC_HEDGE_ON = real


def test_buyer_sends_each_coin_on_its_plan_side():
    # the plan's side goes out as is (a LONG sent as SHORT is a real position the wrong way); no side = skipped; at most
    # MAX_PLAN coins a plan; a refusal that holds for every coin (cap, daily cap, Auto Short off ...) stops the round loudly
    import team
    syms = [f"C{i:02d}USDT" for i in range(team.MAX_PLAN + 2)]
    sides = {s: "LONG" if i % 2 else "SHORT" for i, s in enumerate(syms) if i != 3}
    sent, lines, said = [], [], []
    real = team.binance.fetch_portfolio, team.binance.post, team.say, team.BTC_HEDGE_ON, team.MAX_OPEN
    team.BTC_HEDGE_ON, team.MAX_OPEN = False, team.MAX_PLAN   # the hold cap has its own test; here a plan must run to its end
    team.binance.fetch_portfolio = lambda: {"available_usdt": "5000", "shorts": [], "closed": []}
    team.say = lambda who, text: said.append(text)
    try:
        for why, n in ((None, team.MAX_PLAN - 1), ("held", team.MAX_PLAN - 1), ("cap", 1), ("daily_cap", 1), ("live_off", 1), ("unclear", 1)):
            sent.clear(), lines.clear(), said.clear()
            team.binance.post = lambda path, payload, timeout=300: sent.append(payload) or {
                "status": "refused" if why else "placed", "why": why, "line": f"server: {why}"}
            team.open_new("2026-10-11", syms, sides, lines)
            assert len(sent) == n and (n == team.MAX_PLAN - 1 or any(t.startswith("[2026-10-11] ⚠️") and why in t for t in said)), (why, sent, said)
        team.binance.post = lambda path, payload, timeout=300: sent.append(payload) or {"status": "placed", "why": None, "line": "ok"}
        sent.clear(), lines.clear()
        team.open_new("2026-10-11", syms, sides, lines)
        assert [(p["symbol"], p["side"]) for p in sent] == [(s, sides[s]) for s in syms[:team.MAX_PLAN] if s != "C03USDT"], sent
        assert "ข้าม C03: แผนไม่ได้บอกฝั่ง LONG/SHORT" in lines, lines
    finally:
        team.binance.fetch_portfolio, team.binance.post, team.say, team.BTC_HEDGE_ON, team.MAX_OPEN = real


def test_short_refuses_a_coin_binance_will_not_take_now():
    # not trading (settling, delisted), not a perpetual, or under Binance's position risk control: nothing is sent
    fake = FakeBinance()
    fake.info["CAKEUSDT"] = {"status": "SETTLING"}
    fake.info["ADAUSDT"] = {"contractType": "CURRENT_QUARTER"}
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        out = opens(binance, ["CAKEUSDT", "ADAUSDT", "NOPEUSDT"])
        assert [(r["status"], r["why"]) for r in out] == [("refused", "not_tradable")] * 3 and fake.posts == [], out
        ok = {"status": "TRADING", "contractType": "PERPETUAL", "orderTypes": ["MARKET"], "filters": []}
        assert binance.can_short(ok) is None
        assert binance.can_short({**ok, "filters": [{"filterType": "POSITION_RISK_CONTROL", "positionControlSide": "BOTH"}]})
    finally:
        restore()


def test_short_portfolio_lists_the_closes_the_cooldown_reads():
    # team.cooldown_shorts decides on /portfolio closed[]: newest first with the unrounded pnl (-0.001 is a loss though
    # pnl_usdt shows -0.00), every close of the last 30 days and never fewer than the newest 50
    from datetime import timedelta
    import team
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    now = binance.utcnow()
    try:
        for i in range(70):                                                                   # one close every 12 h: 60 in 30 days
            sym, cid, at = f"C{i:02d}USDT", f"bto-2026-09-01-C{i:02d}", now - timedelta(hours=12 * i + 1)
            binance.record("2026-09-01", sym, "open", futures_order(cid, "1", "2"), "placed", at=(at - timedelta(hours=1)).isoformat())
            binance.record("2026-09-01", sym, "closed", {"clientOrderId": binance.sibling(cid, "closed")}, "synced", at=at.isoformat(),
                           pos=cid, why="AI ตัดสินปิด", pnl="-0.001" if i == 0 else "1", funding="0", fees="0")
        p = binance.portfolio()
        assert [r["symbol"] for r in p["closed"]] == [f"C{i:02d}USDT" for i in range(60)], [r["symbol"] for r in p["closed"]]
        assert (p["closed"][0]["pnl"], p["closed"][0]["pnl_usdt"]) == ("-0.001", "-0.00"), p["closed"][0]
        assert team.cooldown_shorts(now, p) == {"C00USDT"}
        binance.utcnow = lambda: now + timedelta(days=10)                                     # 40 in 30 days: still the newest 50
        assert len(binance.portfolio()["closed"]) == 50
    finally:
        restore()


def test_short_realized_sums_the_unrounded_pnl_and_rounds_once():
    # D6 (2026-11-06): realized_usdt = the stored pnl summed, then rounded to cents once (rounding each close first
    # showed -19.10 on the server for a real -19.13)
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    try:
        for i, pnl in enumerate(("-13.634", "-5.4978", "-0.004")):
            cid = f"bto-2026-10-10-C{i}"
            binance.record("2026-10-10", f"C{i}USDT", "open", futures_order(cid, "1", "2"), "placed")
            binance.record("2026-10-10", f"C{i}USDT", "closed", {"clientOrderId": binance.sibling(cid, "closed")}, "synced",
                           pos=cid, why="ปิดตามกติกา", pnl=pnl, funding="0", fees="0")
        p = binance.portfolio()
        assert sorted(r["pnl_usdt"] for r in p["closed"]) == ["-0.00", "-13.63", "-5.50"], p["closed"]   # each close still shows its cents
        assert p["realized_usdt"] == "-19.14", p["realized_usdt"]                             # -19.1358; cents first gave -19.13
    finally:
        restore()


def test_short_order_out_but_ledger_write_failed_is_unclear():
    # Binance filled it, then writing the 'placed' line failed (disk full): the 'sending' line stands, so the answer is
    # 'unclear' (the team syncs again and counts the money), never 'refused'
    fake = FakeBinance()
    binance, restore = short_env(fake, SHORT_LIVE="1")
    real, hows = binance.record, []

    def record(*a, **k):
        hows.append(a[4])
        if len(hows) == 2:
            raise OSError("No space left on device")
        return real(*a, **k)
    binance.record = record
    try:
        (r,) = opens(binance, ["TRXUSDT"])
        assert (r["status"], r["why"], hows) == ("unclear", "error", ["sending", "placed"]), (r, hows)
        assert binance.finals()["bto-2026-10-11-TRX"]["how"] == "sending" and len(fake.sent("/fapi/v1/order")) == 1
    finally:
        binance.record = real
        restore()


def test_deploy_never_ships_the_pc_env():
    # .88's .env is the only copy of DCA_PLAN_TOKEN after a rotation (README Deploy server): a deploy from the PC must
    # never put the PC's (old, or no) token back over it
    import re
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy.sh"), encoding="utf-8").read()
    tar = re.search(r"tar czf - (.*?) \. \|", src, re.S).group(1)
    assert "--exclude=./.env " in tar + " ", tar


def test_team_uses_only_what_the_proxy_binance_has():
    # binance.py on the server is only a proxy now: every binance.X team.py calls must still exist
    import re
    import team
    names = set(re.findall(r"\bbinance\.(?!py\b)([A-Za-z_]\w*)", open(team.__file__, encoding="utf-8").read()))
    assert {"post", "fetch_portfolio", "save_portfolio", "coin", "dec"} <= names, names
    assert all(hasattr(team.binance, n) for n in names), sorted(n for n in names if not hasattr(team.binance, n))



def lv_daily_fixture():
    """(today_utc, perps, daily, fund7, newest): 90 daily bars to 2026-11-14 for BTC and 9 coins. LV1/LV2 pass every daily
    check and close under their 20-day low on the last day, SQZ has funding below the limit, HOLD* do not break on the day
    bar, HIVOL* sit in the top third of vol14."""
    import datetime as dt
    days = [dt.date(2026, 8, 17) + dt.timedelta(days=i) for i in range(90)]   # last day = 2026-11-14 (yesterday of 2026-11-15)
    today_utc = dt.date(2026, 11, 15)
    perps = {"BTCUSDT": "BTC"}
    daily, fund7, newest = {}, {}, {}

    btc_c = [65000.0 * (1.0005 ** i) for i in range(90)]
    daily["BTCUSDT"] = (days, btc_c, [c * 1.01 for c in btc_c], [c * 0.99 for c in btc_c], btc_c, [2e9] * 90, [96] * 90)
    fund7["BTCUSDT"] = (0.0001, 0.0003)
    newest["BTCUSDT"] = "2026-11-14 23:45:00"

    specs = [
        ("LV1USDT", 0.004, -0.0045, True, 0.0001),
        ("LV2USDT", 0.003, -0.0060, True, 0.0001),
        ("SQZUSDT", 0.003, -0.0050, True, -0.0008),    # f24 = -0.08% < -0.03% -> squeeze risk!
        ("HOLDUSDT", 0.004, -0.0045, False, 0.0001),
        ("HOLD2USDT", 0.0045, -0.0045, False, 0.0001),
        ("HOLD3USDT", 0.005, -0.0045, False, 0.0001),
        ("HIVOL4USDT", 0.060, -0.0045, True, 0.0001),
        ("HIVOL5USDT", 0.065, -0.0045, True, 0.0001),
        ("HIVOL6USDT", 0.070, -0.0045, True, 0.0001),
    ]
    for sym, amp, drift, brk, f24 in specs:
        perps[sym] = sym[:-4]
        closes = []
        p_val = 100.0
        for i in range(89):
            step_r = drift + (amp if i % 2 == 0 else -amp)
            p_val = p_val * (1.0 + step_r)
            closes.append(p_val)
        closes.append(closes[-1])
        opens = [closes[0]] + closes[:-1]
        highs = [max(opens[i], closes[i]) * 1.006 for i in range(90)]
        lows = [min(opens[i], closes[i]) * (0.996 if i < 89 else 0.999) for i in range(90)]
        lows[-2] = closes[-2] * 0.999
        sup20_prev = min(lows[-22:-2])
        if closes[-2] < sup20_prev:
            closes[-2] = sup20_prev * 1.002
            highs[-2] = max(highs[-2], closes[-2] * 1.002)
        prev_low20 = min(lows[-21:-1])
        closes[-1] = prev_low20 * (0.985 if brk else 1.015)
        lows[-1] = closes[-1] * 0.998
        highs[-1] = max(opens[-1], closes[-1]) * 1.003
        daily[sym] = (days, opens, highs, lows, closes, [15e6] * 90, [96] * 90)
        fund7[sym] = (f24, f24 * 3)
        newest[sym] = "2026-11-14 23:45:00"
    return today_utc, perps, daily, fund7, newest


def lv_hourly_fixture(end_dt, brk=True, n=240, qv=625000.0):
    """market.load_perp_hourly_qv record of n 1h bars, the last one opening at end_dt: a falling coin whose last bar closes
    1.5% under the low of the 20 bars before it (brk) or 1.5% above it; the bar before closed above its own 20-bar low."""
    import datetime as dt
    closes, p = [], 100.0
    for i in range(n - 1):
        p *= 1.0 - 0.0006 + (0.003 if i % 2 == 0 else -0.003)
        closes.append(p)
    closes.append(closes[-1])
    opens = [closes[0]] + closes[:-1]
    highs = [max(opens[i], closes[i]) * 1.001 for i in range(n)]
    lows = [min(opens[i], closes[i]) * 0.999 for i in range(n)]
    lows[-2] = closes[-2] * 0.9995
    assert closes[-2] >= min(lows[-22:-2])
    closes[-1] = min(lows[-21:-1]) * (0.985 if brk else 1.015)
    lows[-1], highs[-1] = closes[-1] * 0.998, max(closes[-2], closes[-1]) * 1.001
    hours = [f"{end_dt - dt.timedelta(hours=n - 1 - i):%Y-%m-%d %H}:00:00" for i in range(n)]
    return (hours, closes, [qv] * n, highs, lows, [4] * n)


def test_lowvol_breakdown_eval_matches_research_backtest_parity():
    # Exact mathematical parity with d:/tread2/research/short_v2/{lowvol_basket,lowvol_breakdown}.py + 00:00-01:59 UTC gate
    import datetime as dt
    import math
    today_utc, perps, daily, fund7, newest = lv_daily_fixture()
    h_qv = {}

    t_open = dt.datetime(2026, 11, 15, 0, 20, tzinfo=dt.timezone.utc)
    rows, meta = market.lowvol_breakdown_eval(perps, daily, h_qv, fund7, today_utc, newest_daily=newest, fund_ok=True, now_utc=t_open)
    passing = [r["symbol"] for r in rows if not r["fail"]]
    assert meta["entry_day"] is True and meta["waiting_ch"] is False, meta
    assert passing == ["LV2USDT", "LV1USDT"], (passing, [(r["symbol"], r["fail"]) for r in rows])
    by_sym = {r["symbol"]: r for r in rows}
    assert any("funding" in f for f in by_sym["SQZUSDT"]["fail"]), by_sym["SQZUSDT"]
    assert any("แนวรับ 20 วัน" in f for f in by_sym["HOLDUSDT"]["fail"]), by_sym["HOLDUSDT"]
    assert any("ความผันผวน" in f for f in by_sym["HIVOL4USDT"]["fail"]), by_sym["HIVOL4USDT"]
    assert all(0.10 <= by_sym[s]["stop_dist"] <= 0.35 for s in passing)

    # Outside 00:00-01:59 UTC (e.g. 10:05 UTC): entry_day is False AND waiting_ch is False (quant never loops mid-day!)
    newest_late = {**newest, "LV1USDT": "2026-11-14 23:15:00"}
    _, meta_mid = market.lowvol_breakdown_eval(perps, daily, h_qv, fund7, today_utc, newest_daily=newest_late, fund_ok=True,
                                               now_utc=dt.datetime(2026, 11, 15, 10, 5, tzinfo=dt.timezone.utc))
    assert meta_mid["entry_day"] is False and meta_mid["waiting_ch"] is False, meta_mid
    # At 00:05 UTC (< 15m grace) when LV1 is still missing 23:45: entry_day is False and waiting_ch is True
    _, meta_wait = market.lowvol_breakdown_eval(perps, daily, h_qv, fund7, today_utc, newest_daily=newest_late, fund_ok=True,
                                                now_utc=dt.datetime(2026, 11, 15, 0, 5, tzinfo=dt.timezone.utc))
    assert meta_wait["entry_day"] is False and meta_wait["waiting_ch"] is True and meta_wait["late"] == ["LV1USDT"], meta_wait

    # ลุงจืด 2026-11-22 "เล่นเฉพาะ short": G_BREADTH gate of lowvol_basket.py:206 — the same signals open only on a day with
    # Base_Universe >= 25 coins, < 45% of them above their SMA20 and BTC 7d < +10% (both strict)
    def gated():
        g_rows, g_meta = market.lowvol_breakdown_eval(perps, daily, h_qv, fund7, today_utc, newest_daily=newest, fund_ok=True, now_utc=t_open)
        return [r["symbol"] for r in g_rows if not r["fail"]], g_meta, {r["symbol"]: r for r in g_rows}
    real_gate = market.LV_SHORT_ONLY, market.LV_GATE_MIN_BASE, market.LV_GATE_MAX_BREADTH, market.LV_GATE_MAX_BTC_RET7
    try:
        market.LV_SHORT_ONLY = True
        got, g_meta, g_by = gated()                          # 9 fixture coins < 25: no entry, every row says why
        assert got == [] and g_meta["entry_day"] is False and g_meta["allow_short"] is False, (got, g_meta)
        assert "ตัวกรองตลาด SHORT ล้วนไม่ผ่าน" in g_meta["mkt_trend"] and "ต้อง ≥ 25" in g_meta["mkt_trend"], g_meta["mkt_trend"]
        assert g_by["LV1USDT"]["fail"] == ["ตัวกรองตลาด SHORT ล้วนไม่ผ่าน"], g_by["LV1USDT"]["fail"]
        market.LV_GATE_MIN_BASE = 1                          # universe big enough: the falling market passes the gate
        got, g_meta, g_by = gated()
        assert got == passing and g_meta["entry_day"] is True and g_by["LV1USDT"]["signal"] == "1D-LowVolBrk (G_BREADTH)", (got, g_meta)
        assert "SHORT ล้วน" in g_meta["mkt_trend"] and "BTC Hedge" not in g_meta["mkt_trend"], g_meta["mkt_trend"]
        market.LV_GATE_MAX_BREADTH = g_meta["breadth_1h"]    # breadth equal to the limit is not below it
        assert gated()[0] == [] and gated()[1]["entry_day"] is False
        market.LV_GATE_MAX_BREADTH = real_gate[2]
        btc7 = daily["BTCUSDT"][4][-1] / daily["BTCUSDT"][4][-8] - 1.0
        market.LV_GATE_MAX_BTC_RET7 = btc7                   # ... and so is BTC 7d
        assert gated()[0] == [] and "BTC 7d" in gated()[1]["mkt_trend"]
        market.LV_GATE_MAX_BTC_RET7 = btc7 + 1e-9
        assert gated()[0] == passing
    finally:
        market.LV_SHORT_ONLY, market.LV_GATE_MIN_BASE, market.LV_GATE_MAX_BREADTH, market.LV_GATE_MAX_BTC_RET7 = real_gate

    # Exact indicator parity cross-check against d:/tread2/research/short_v2/lowvol_breakdown.py when present on Windows dev PC
    ref_dir = "D:/tread2/research/short_v2"
    if os.path.isdir(ref_dir):
        import contextlib
        import sys
        with contextlib.suppress(ImportError):
            import numpy as np
            sys.path.insert(0, ref_dir)
            try:
                import lowvol_breakdown as ref_brk
                _, h, lo, c, _, _ = daily["LV2USDT"][1:]
                sma50, ema20, atr20, sup20, sup20_prev = market.daily_tf_indicators(h, lo, c)
                arr = lambda xs: np.array(xs, dtype=np.float64)   # noqa: E731
                r_sma50, r_ema20, r_atr20, r_sup20 = ref_brk.compute_tf_indicators(np.arange(len(c)), arr(h), arr(lo), arr(c))
                assert math.isclose(sma50, float(r_sma50[-1]), rel_tol=0, abs_tol=1e-12)
                assert math.isclose(ema20, float(r_ema20[-1]), rel_tol=0, abs_tol=1e-10)
                assert math.isclose(atr20, float(r_atr20[-1]), rel_tol=0, abs_tol=1e-12)
                assert math.isclose(sup20, float(r_sup20[-1]), rel_tol=0, abs_tol=1e-12)
                assert math.isclose(sup20_prev, float(r_sup20[-2]), rel_tol=0, abs_tol=1e-12)
            finally:
                sys.path.remove(ref_dir)


def test_binance_multi_leg_btc_hedge_open_portfolio_close_and_sync():
    # Multi-leg BTCUSDT LONG hedge on One-way mode: 2 pairs open on same run_id, per-leg portfolio PnL/funding,
    # partial close of 1 leg without qty_mismatch, exact per-leg closed PnL + userTrades fees, and manual BTC refusal
    import re
    from decimal import Decimal
    fake = FakeBinance(usdt="864.01", bid="2")
    fake.symbols.append("BTCUSDT")
    fake.symbol_lots["BTCUSDT"] = ("0.001", "0.001", "0.001")
    fake.symbol_bids["BTCUSDT"] = ("67000", "67010")
    binance, restore = short_env(fake, SHORT_LIVE="1", SHORT_LOT_USDT="100")
    cid_re = re.compile(r"^bto-[0-9A-Za-z-]{1,32}$")   # console/src/lib/{portfolio,requests}.ts validation regex
    def do_open(m):
        run_id, sym, side, usdt, lev = binance.open_from(m)
        pair, req_qty = m.get("pair"), binance.dec(m["qty"]) if m.get("qty") is not None else None
        return binance.open_one(run_id, sym, side, usdt, lev, binance.utcnow().date(), pair=pair, req_qty=req_qty)

    try:
        # 0. Trust boundary: pair/qty on non-BTCUSDT LONG is rejected with ValueError
        for bad in ({"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "LONG", "usdt": "68", "pair": "DOGE"},
                    {"run_id": "2026-10-11", "symbol": "BTCUSDT", "side": "SHORT", "usdt": "68", "pair": "TRX"}):
            try:
                binance.open_from(bad)
                raise AssertionError(f"expected ValueError for {bad}")
            except ValueError:
                pass

        # 1. Manual BTC position held by ลุงจืด -> refused with 'held', untouched
        fake.positions["BTCUSDT"] = {**short_row("0.005", "66000"), "marginType": "isolated"}
        r_held = do_open({"run_id": "2026-10-11", "symbol": "BTCUSDT", "side": "LONG", "usdt": "68", "qty": "0.001", "pair": "TRX"})
        assert (r_held["status"], r_held["why"]) == ("refused", "held"), r_held
        del fake.positions["BTCUSDT"]

        # 2. Pair 1 (TRX) + Pair 2 (DOGE) open on the same run_id
        r_b1 = do_open({"run_id": "2026-10-11", "symbol": "BTCUSDT", "side": "LONG", "usdt": "68", "qty": "0.001", "pair": "TRX"})
        r_s1 = do_open({"run_id": "2026-10-11", "symbol": "TRXUSDT", "side": "SHORT", "usdt": "67.01"})
        fake.symbol_bids["BTCUSDT"] = ("67200", "67210")   # 2nd leg fills at 67,210!
        r_b2 = do_open({"run_id": "2026-10-11", "symbol": "BTCUSDT", "side": "LONG", "usdt": "68", "qty": "0.001", "pair": "DOGE"})
        r_s2 = do_open({"run_id": "2026-10-11", "symbol": "DOGEUSDT", "side": "SHORT", "usdt": "67.21"})
        assert [x["status"] for x in (r_b1, r_s1, r_b2, r_s2)] == ["placed"] * 4, (r_b1, r_s1, r_b2, r_s2)
        assert r_b1["cid"] == "bto-2026-10-11-BTC-TRX" and r_b2["cid"] == "bto-2026-10-11-BTC-DOGE"
        assert cid_re.fullmatch(r_b1["cid"]) and cid_re.fullmatch(r_b2["cid"])
        # 2nd BTC leg skipped /fapi/v1/marginType (since actual_btc > 0, avoiding Binance -4048)
        margin_syms = [q["symbol"] for q in fake.sent("/fapi/v1/marginType")]
        assert margin_syms.count("BTCUSDT") == 1, margin_syms
        assert fake.positions["BTCUSDT"]["positionAmt"] == "0.002"

        # 3. Mark BTC at 68,210 and post -0.02 USDT funding -> each 0.001 leg gets its own entry PnL and -0.01 funding share
        fake.positions["BTCUSDT"].update(markPrice="68210", unRealizedProfit="2.20000000")
        t_fund = int(binance.utcnow().timestamp() * 1000) + 1000
        fake.income.append(income_row("BTCUSDT", "FUNDING_FEE", "-0.02000000", t_fund))
        pf = binance.portfolio()
        by_id = {x["id"]: x for x in pf["shorts"]}
        assert set(by_id) == {"bto-2026-10-11-BTC-TRX", "bto-2026-10-11-TRX", "bto-2026-10-11-BTC-DOGE", "bto-2026-10-11-DOGE"}
        # Leg 1 bought at 67,010 -> at 68,210 mark: (68210 - 67010) * 0.001 = +1.20 USDT; Leg 2 bought at 67,210 -> +1.00 USDT
        assert (by_id["bto-2026-10-11-BTC-TRX"]["pnl_usdt"], by_id["bto-2026-10-11-BTC-TRX"]["funding_usdt"]) == ("1.20", "-0.01"), by_id
        assert (by_id["bto-2026-10-11-BTC-DOGE"]["pnl_usdt"], by_id["bto-2026-10-11-BTC-DOGE"]["funding_usdt"]) == ("1.00", "-0.01"), by_id
        assert by_id["bto-2026-10-11-BTC-TRX"]["entry_signal"] == "Hedge คู่ TRX (LONG 1:1)"

        # 4. Close ONLY Pair 1 (TRX + BTC(TRX)) at BTC bid=68,010 while Pair 2 stays open -> NO qty_mismatch!
        fake.symbol_bids["BTCUSDT"] = ("68010", "68020")
        c_s1 = binance.close_one("bto-2026-10-11-TRX", "ถึง TP -30%")
        c_b1 = binance.close_one("bto-2026-10-11-BTC-TRX", "ปิด hedge คู่ TRX พร้อมขา short")
        assert c_s1["status"] == "placed" and c_b1["status"] == "placed", (c_s1, c_b1)
        assert fake.positions["BTCUSDT"]["positionAmt"] == "0.001"
        # Record userTrades commissions for Leg 1 open & close orders + TRX realized PnL
        open_oid = fake.orders["bto-2026-10-11-BTC-TRX"]["orderId"]
        close_oid = fake.orders["btc-2026-10-11-BTC-TRX"]["orderId"]
        fake.user_trades[open_oid] = [{"commission": "0.03350500", "commissionAsset": "USDT"}]
        fake.user_trades[close_oid] = [{"commission": "0.03400500", "commissionAsset": "USDT"}]
        fake.income.append(income_row("TRXUSDT", "REALIZED_PNL", "15.00000000", t_fund + 500))
        sync_res = binance.sync()
        assert sync_res["status"] == "ok" and sync_res["unclear"] == 0, sync_res
        pf2 = binance.portfolio()
        assert {x["id"] for x in pf2["shorts"]} == {"bto-2026-10-11-BTC-DOGE", "bto-2026-10-11-DOGE"}, pf2["shorts"]
        closed_btc = [x for x in pf2["closed"] if x["symbol"] == "BTCUSDT"][0]
        # Price PnL = (68010 - 67010)*0.001 = +1.00; funding = -0.01; fees = -(0.033505 + 0.034005) = -0.067510 -> net = +0.922490
        assert (closed_btc["pair"], Decimal(closed_btc["pnl"]), closed_btc["pnl_usdt"]) == ("TRX", Decimal("0.92249"), "0.92"), closed_btc
    finally:
        restore()


def test_buyer_paired_open_and_atomic_rollback_on_alt_refusal():
    # team.open_new with BTC_HEDGE_ON=True: 0.001 BTC paired sizing, /sync settle between Leg 1 & Leg 2,
    # atomic rollback (/close bto-<run>-BTC-<COIN>) when Alt SHORT fails, and MAX_PAIRS cap
    from decimal import Decimal
    import team
    calls, lines, said = [], [], []
    pf = {"available_usdt": "300.00", "shorts": [], "held_symbols": [], "closed": []}

    def post(path, payload, timeout=300):
        calls.append((path, dict(payload)))
        if path == "/sync":
            return {"status": "ok", "lines": [], "unclear": 0}
        if path == "/open" and payload.get("pair") == "TRX":
            # Simulate real Binance: POST MARKET answers FILLED with cumQuote=0 ('unclear'), then /sync settles it!
            return {"status": "unclear", "why": None, "cid": "bto-2026-11-14-BTC-TRX", "order_status": "FILLED",
                    "qty": "0.001", "quote_qty": "0", "line": "BTC unclear"}
        if path == "/open" and payload.get("symbol") == "TRXUSDT":
            return {"status": "placed", "why": None, "cid": "bto-2026-11-14-TRX", "order_status": "FILLED",
                    "qty": "335", "quote_qty": "67.00", "line": "TRX ok"}
        if path == "/open" and payload.get("pair") == "DOGE":
            return {"status": "placed", "why": None, "cid": "bto-2026-11-14-BTC-DOGE", "order_status": "FILLED",
                    "qty": "0.001", "quote_qty": "67.00", "line": "BTC DOGE ok"}
        if path == "/open" and payload.get("symbol") == "DOGEUSDT":
            return {"status": "refused", "why": "spread", "cid": "bto-2026-11-14-DOGE", "line": "ข้าม DOGE: spread กว้าง"}
        if path == "/close":
            return {"status": "placed", "why": None, "cid": "btc-2026-11-14-BTC-DOGE", "line": "BTC: ปิด hedge คู่ DOGE แล้ว"}
        raise AssertionError(f"unexpected {path} {payload}")

    real = team.binance.fetch_portfolio, team.binance.post, team.btc_book_ask, team.say
    team.binance.fetch_portfolio, team.binance.post = (lambda: pf), post
    team.btc_book_ask, team.say = (lambda: Decimal("67000")), (lambda who, text: said.append(text))
    try:
        # 210 USDT wallet: Pair 1 (TRX) takes ~134.07 USDT -> ~75.93 USDT left (< ~141.68 needed), unless tested with 280 first
        pf["available_usdt"] = "280.00"
        unsure = team.open_new("2026-11-14", ["TRXUSDT", "DOGEUSDT"],
                               {"TRXUSDT": "SHORT", "DOGEUSDT": "SHORT"}, lines)
        assert unsure is True   # triggers final /sync in trade() to book settled/rolled-back legs
        seq = [(p, q.get("symbol") or q.get("id"), q.get("pair"), q.get("qty"), q.get("usdt")) for p, q in calls]
        assert seq == [
            ("/open", "BTCUSDT", "TRX", "0.001", "67.00"),      # Leg 1 TRX hedge
            ("/sync", None, None, None, None),                  # Settle Leg 1 cumQuote=0 before Leg 2
            ("/open", "TRXUSDT", None, None, "67.00"),          # Leg 2 TRX short matched to 0.001 * 67000
            ("/open", "BTCUSDT", "DOGE", "0.001", "67.00"),     # Leg 1 DOGE hedge
            ("/open", "DOGEUSDT", None, None, "67.00"),         # Leg 2 DOGE short -> refused ('spread')!
            ("/sync", None, None, None, None),                  # Settle before rollback /close
            ("/close", "bto-2026-11-14-BTC-DOGE", None, None, None),  # Immediate atomic rollback of BTC(DOGE)!
            # Remaining wallet after TRX (~145.93 USDT) >= ~141.68 -> SOL would try unless wallet started at 210!
        ][:7], seq
        assert any("BTC: ปิด hedge คู่ DOGE แล้ว" in ln for ln in lines), lines

        # Wallet insufficient for 2nd pair: 210 USDT opens TRX (~134.07 used -> ~75.93 left < ~141.68 pair_need) and stops
        calls.clear(), lines.clear()
        pf["available_usdt"] = "210.00"
        team.open_new("2026-11-14", ["TRXUSDT", "DOGEUSDT"], {"TRXUSDT": "SHORT", "DOGEUSDT": "SHORT"}, lines)
        assert [q.get("symbol") for p, q in calls if p == "/open"] == ["BTCUSDT", "TRXUSDT"], calls
        assert "ไม่พอเปิดคู่ DOGE+BTC" in lines[-1], lines
    finally:
        team.binance.fetch_portfolio, team.binance.post, team.btc_book_ask, team.say = real


def test_lowvol_exit_rule_and_paired_close_cascade():
    # LOWVOL_EXIT_ON=True: 3xATR20 stop [10%..35%], -30% TP, +15% 1d UTC close ratcheting SL to entry*0.96 (+4% locked),
    # 21d max hold, paired BTC(<COIN>) cascade in exit_review, orphan hedge cleanup, and Console ปิดไม้ paired close
    from datetime import datetime, timedelta, timezone
    import team
    t0 = datetime(2026, 11, 1, 0, 10, tzinfo=timezone.utc)   # opened 00:10 UTC after 2026-10-31 signal day
    # Build 22 completed UTC days before entry (2026-10-10 .. 2026-10-31) with daily range 0.06 on price 1.0 -> ATR20=0.06 -> 3*ATR20=18%
    pre_bars = []
    d_start = datetime(2026, 10, 10, 0, tzinfo=timezone.utc)
    for k in range(22 * 24):
        ts = d_start + timedelta(hours=k)
        h_idx = k % 24
        pre_bars.append((f"{ts:%Y-%m-%d %H}:00:00", 1.03 if h_idx == 5 else 1.01, 0.97 if h_idx == 12 else 0.99, 1.00))

    # 1. Initial SL at +18% (1.18), TP at -30% (0.70)
    post_ok = [(f"2026-11-01 {h:02d}:00:00", 1.17, 0.72, 0.90) for h in range(5)]
    assert team.rule_exit("SHORT", 1.0, 0.90, t0, pre_bars + post_ok, t0 + timedelta(hours=5)) is None
    post_sl = post_ok + [("2026-11-01 05:00:00", 1.181, 0.89, 1.15)]
    assert team.rule_exit("SHORT", 1.0, 1.15, t0, pre_bars + post_sl, t0 + timedelta(hours=6)) == "หลุด stop 1.18 (+18.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
    post_tp = post_ok + [("2026-11-01 05:00:00", 0.92, 0.70, 0.71)]
    assert team.rule_exit("SHORT", 1.0, 0.71, t0, pre_bars + post_tp, t0 + timedelta(hours=6)) == "ถึง TP -30% · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"
    # Live 15m candle vs mark TP check (regression check for mark <= tp!)
    assert team.rule_exit("SHORT", 1.0, 0.75, t0, pre_bars + post_ok, t0 + timedelta(hours=5), (0.92, 0.69)).startswith("แท่ง 15 นาทีล่าสุดแตะ TP -30%")
    assert team.rule_exit("SHORT", 1.0, 0.69, t0, pre_bars + post_ok, t0 + timedelta(hours=5), (0.92, 0.69)).startswith("ราคา mark ถึง TP -30%")

    # 2. Intraday dip to 0.82 (+18%) that bounces to 0.88 (+12% < +15%) at 23:00 UTC does NOT ratchet SL
    day1_bounce = [(f"2026-11-01 {h:02d}:00:00", 0.98, 0.82 if h == 10 else 0.87, 0.88) for h in range(24)]
    day2_retest = [("2026-11-02 00:00:00", 0.99, 0.88, 0.97)]
    assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre_bars + day1_bounce + day2_retest, datetime(2026, 11, 2, 1, tzinfo=timezone.utc)) is None

    # 3. Completed 1d close at 23:00 UTC = 0.84 (+16% >= +15%) ratchets SL to 0.96 (-4.0% locked profit)!
    day1_win = [(f"2026-11-01 {h:02d}:00:00", 0.95, 0.83, 0.84) for h in range(24)]
    day2_trail = [("2026-11-02 00:00:00", 0.96, 0.84, 0.96)]
    got_trail = team.rule_exit("SHORT", 1.0, 0.96, t0, pre_bars + day1_win + day2_trail, datetime(2026, 11, 2, 1, tzinfo=timezone.utc))
    assert got_trail == "หลุด trailing stop ล็อกกำไร 0.96 (-4.0%) · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop", got_trail

    # 4. exit_review cascades Alt SHORT close -> paired BTC(TRX) close + cleans up orphan BTC(GONE), holding DOGE + BTC(DOGE)
    pf = {"shorts": [
        {"id": "bto-2026-11-01-TRX", "symbol": "TRXUSDT", "coin": "TRX", "side": "SHORT", "entry": "1.0", "mark": "1.19", "pnl_pct": "-19.00",
         "on_binance": True, "unclear": False, "opened_at": t0.isoformat()},
        {"id": "bto-2026-11-01-BTC-TRX", "symbol": "BTCUSDT", "coin": "BTC", "pair": "TRX", "side": "LONG", "entry": "67000", "mark": "68000",
         "pnl_pct": "1.49", "on_binance": True, "unclear": False, "opened_at": t0.isoformat()},
        {"id": "bto-2026-11-01-DOGE", "symbol": "DOGEUSDT", "coin": "DOGE", "side": "SHORT", "entry": "1.0", "mark": "0.95", "pnl_pct": "5.00",
         "on_binance": True, "unclear": False, "opened_at": t0.isoformat()},
        {"id": "bto-2026-11-01-BTC-DOGE", "symbol": "BTCUSDT", "coin": "BTC", "pair": "DOGE", "side": "LONG", "entry": "67000", "mark": "68000",
         "pnl_pct": "1.49", "on_binance": True, "unclear": False, "opened_at": t0.isoformat()},
        {"id": "bto-2026-11-01-BTC-GONE", "symbol": "BTCUSDT", "coin": "BTC", "pair": "GONE", "side": "LONG", "entry": "67000", "mark": "68000",
         "pnl_pct": "1.49", "on_binance": True, "unclear": False, "opened_at": t0.isoformat()},
    ], "available_usdt": "500", "closed": []}
    sent, said = [], []
    real = (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.binance.utcnow,
            team.hourly_bars, team.say, team.REPORTS, team.ENTRY_STOPS)
    tmp = tempfile.mkdtemp()
    team.REPORTS, team.ENTRY_STOPS = tmp, os.path.join(tmp, "entry-stops.json")
    team.binance.fetch_portfolio, team.binance.save_portfolio = (lambda: pf), (lambda p: None)
    team.binance.post = lambda path, payload, timeout=300: sent.append((path, payload.get("id"), payload.get("why"))) or (
        {"status": "ok", "lines": []} if path == "/sync" else {"status": "placed", "why": None, "line": "closed"})
    team.binance.utcnow = lambda: t0 + timedelta(hours=6)
    team.hourly_bars = lambda syms, days=45: {s: pre_bars + post_ok for s in syms}
    team.say = lambda who, text: said.append((who, text))
    try:
        info = {}
        to_close = team.exit_review("2026-11-01", info)
        assert to_close == ["TRXUSDT", "bto-2026-11-01-BTC-TRX", "bto-2026-11-01-BTC-GONE"], (to_close, info)
        lines = []
        team.close_positions("2026-11-01", to_close, info["ids"], lines, info["whys"])
        assert [(p, cid) for p, cid, _ in sent] == [
            ("/close", "bto-2026-11-01-TRX"),
            ("/close", "bto-2026-11-01-BTC-TRX"),
            ("/close", "bto-2026-11-01-BTC-GONE"),
        ], sent
        # 5. Console ปิดไม้ button on DOGE automatically closes its paired BTC(DOGE) leg too!
        sent.clear()
        team.close_by_hand(["bto-2026-11-01-DOGE"])
        assert [(p, cid) for p, cid, _ in sent] == [
            ("/close", "bto-2026-11-01-DOGE"),
            ("/close", "bto-2026-11-01-BTC-DOGE"),
            ("/sync", None),
        ], sent
    finally:
        (team.binance.fetch_portfolio, team.binance.save_portfolio, team.binance.post, team.binance.utcnow,
         team.hourly_bars, team.say, team.REPORTS, team.ENTRY_STOPS) = real


def test_lowvol_1h_eval_reads_the_breakdown_on_the_1h_bar_and_opens_any_hour():
    # ลุงจืด 2026-11-22 "ทำงานทุก ชม. หาเหรียญที่ short ได้ เล่น 24/7" -> "สัญญาณแท่ง 1 ชม.": research cell 1h | LOWVOL_BREAKDOWN
    # (d:/tread2/research/short_v2/lowvol_breakdown.py scan_symbol_tf_trades): the breakdown bar is the 1h bar that just
    # closed, the daily context (Base_Universe, anti-squeeze, Cf < SMA20, rel14, gate) is yesterday's, and volume 168 h and
    # funding 24 h are measured up to that hour
    import datetime as dt
    utc = dt.timezone.utc
    today_utc, perps, daily, fund7, _ = lv_daily_fixture()
    end = dt.datetime(2026, 11, 15, 5, 0, tzinfo=utc)             # the bar that opened 05:00 closed at 06:00 UTC
    brk = {"LV1USDT", "LV2USDT", "SQZUSDT", "HIVOL4USDT"}
    h_qv = {s: lv_hourly_fixture(end, brk=s in brk) for s in perps if s != "BTCUSDT"}
    newest = {s: "2026-11-15 05:45:00" for s in perps}
    t = dt.datetime(2026, 11, 15, 6, 5, tzinfo=utc)

    def run(h=None, new=None, now=t, rt=None, ok=True):
        rows, meta = market.lowvol_breakdown_eval(perps, daily, h or h_qv, fund7, today_utc, new or newest, ok, now_utc=now, fund_rt=rt)
        return [r["symbol"] for r in rows if not r["fail"]], meta, {r["symbol"]: r for r in rows}

    real = market.LV_TF, market.LV_SHORT_ONLY, market.LV_GATE_MIN_BASE
    market.LV_TF, market.LV_SHORT_ONLY = "1h", False
    try:
        got, meta, by = run()
        assert got == ["LV2USDT", "LV1USDT"], (got, [(r, by[r]["fail"]) for r in by])
        assert meta["entry_day"] is True and meta["waiting_ch"] is False and meta["data_to"] == "2026-11-15 06:00", meta
        lv1 = by["LV1USDT"]
        assert lv1["signal"] == "1H-LowVolBrk (H_DELTA0)" and lv1["price"] == h_qv["LV1USDT"][1][-1], lv1
        assert lv1["stop_dist"] == 0.10 and 0 < 3.0 * lv1["atr20"] / lv1["price"] < 0.10, lv1   # 3 x ATR20 of 1h bars: under the 10% floor
        assert any("แนวรับ 20 ชม." in f for f in by["HOLDUSDT"]["fail"]), by["HOLDUSDT"]["fail"]
        assert any("funding 24 ชม." in f for f in by["SQZUSDT"]["fail"]), by["SQZUSDT"]["fail"]
        assert any("ความผันผวน" in f for f in by["HIVOL4USDT"]["fail"]), by["HIVOL4USDT"]["fail"]

        # LV1's day bar is under its 20-day low as well, but only the 1h bar decides
        got, _, by = run({**h_qv, "LV1USDT": lv_hourly_fixture(end, brk=False)})
        assert got == ["LV2USDT"] and any("แนวรับ 20 ชม." in f for f in by["LV1USDT"]["fail"]), (got, by["LV1USDT"]["fail"])

        # only the first bar under the support: an hour later the next bar closes lower still -> not chased
        h1, c1, q1, hi1, lo1, n1 = h_qv["LV1USDT"]
        nxt = (h1 + ["2026-11-15 06:00:00"], c1 + [c1[-1] * 0.99], q1 + [q1[-1]], hi1 + [c1[-1]], lo1 + [c1[-1] * 0.989], n1 + [4])
        got, m7, by = run({**h_qv, "LV1USDT": nxt}, {s: "2026-11-15 06:45:00" for s in perps}, t + dt.timedelta(hours=1))
        assert got == [] and by["LV1USDT"]["fail"] == ["หลุดแนวรับไปตั้งแต่แท่งก่อนแล้ว (ไม่ไล่ราคา)"], (got, by["LV1USDT"]["fail"])
        assert m7["data_to"] == "2026-11-15 07:00" and m7["entry_day"] is True, m7

        # a signal bar missing a 15m candle, or with a gap before it, is no signal bar
        short = (h1, c1, q1, hi1, lo1, n1[:-1] + [3])
        assert any("แท่ง 1 ชม. ไม่ครบ" in f for f in run({**h_qv, "LV1USDT": short})[2]["LV1USDT"]["fail"])
        gap = tuple(x[:-2] + x[-1:] for x in h_qv["LV1USDT"])
        assert any("แท่ง 1 ชม. ไม่ครบ" in f for f in run({**h_qv, "LV1USDT": gap})[2]["LV1USDT"]["fail"])

        # volume 168 h up to the signal hour: 200,500 USDT/h until yesterday 23:00 passes the day check, the 6 thin hours of
        # today pull the mean of the 168 h ending 06:00 down to 193,375
        thin = (h1, c1, [200500.0 if h < "2026-11-15" else 1000.0 for h in h1], hi1, lo1, n1)
        f = run({**h_qv, "LV1USDT": thin})[2]["LV1USDT"]["fail"]
        assert len(f) == 1 and "volume เฉลี่ย 168 ชม. ถึง 06:00 UTC" in f[0], f

        # funding 24 h up to the signal hour (research f24_rt >= -0.03%, the limit itself passes)
        got, _, by = run(rt={"LV2USDT": -0.0004, "LV1USDT": -0.0003})
        assert got == ["LV1USDT"] and by["LV1USDT"]["funding"] == -0.0003, (got, by["LV1USDT"])
        assert by["LV2USDT"]["fail"] == ["funding 24 ชม. ถึง 06:00 UTC -0.040% < -0.030% (คนรุม short แน่น)"], by["LV2USDT"]["fail"]

        # ClickHouse lag: the round waits (waiting_ch) until HH:15 for the hour's last candle, then opens what is complete
        slow = {**newest, "LV1USDT": "2026-11-15 05:30:00"}
        _, m, _ = run(new=slow)
        assert m["entry_day"] is False and m["waiting_ch"] is True and m["late"] == ["LV1USDT"] and "05:45 UTC" in m["mkt_trend"], m
        got, m, by = run(new=slow, now=t.replace(minute=16))
        assert got == ["LV2USDT"] and m["entry_day"] is True and m["waiting_ch"] is False, (got, m)
        assert any("ClickHouse ยังไม่ครบถึง 05:45 UTC" in f for f in by["LV1USDT"]["fail"]), by["LV1USDT"]["fail"]
        no_btc = {**newest, "BTCUSDT": "2026-11-15 05:30:00"}
        assert run(new=no_btc)[1]["waiting_ch"] is True
        _, m, _ = run(new=no_btc, now=t.replace(minute=20))
        assert m["entry_day"] is False and m["waiting_ch"] is False and "BTCUSDT" in m["mkt_trend"], m
        # ... and a coin that stopped trading hours ago is not waited for
        gone = {**h_qv, "HOLD3USDT": tuple(x[:-4] for x in h_qv["HOLD3USDT"])}
        _, m, by = run(gone, {**newest, "HOLD3USDT": "2026-11-15 01:45:00"})
        assert m["late"] == [] and m["entry_day"] is True, m
        assert any("แท่ง 1 ชม. ไม่ครบ" in f for f in by["HOLD3USDT"]["fail"]), by["HOLD3USDT"]["fail"]

        # every hour of the day is an entry hour; the bar closing at 00:00 UTC uses the day that just closed
        for hh, mm in ((0, 20), (13, 40), (23, 59)):
            now = dt.datetime(2026, 11, 15, hh, mm, tzinfo=utc)
            last = f"{now.replace(minute=0) - dt.timedelta(minutes=15):%Y-%m-%d %H:%M}:00"
            _, m, _ = run(new={s: last for s in perps}, now=now)
            assert m["entry_day"] is True and m["data_to"] == f"2026-11-15 {hh:02d}:00", (hh, m)

        # G_BREADTH (the live mode): the same signals behind yesterday's market gate
        market.LV_SHORT_ONLY = True
        got, m, _ = run()
        assert got == [] and m["entry_day"] is False and m["allow_short"] is False and "ต้อง ≥ 25" in m["mkt_trend"], (got, m)
        market.LV_GATE_MIN_BASE = 1
        got, m, by = run()
        assert got == ["LV2USDT", "LV1USDT"] and m["entry_day"] is True and by["LV1USDT"]["signal"] == "1H-LowVolBrk (G_BREADTH)", (got, m)
        assert all(x in m["mkt_trend"] for x in ("1H Low-Vol Breakdown SHORT ล้วน", "เปิด SHORT ได้ทุกชั่วโมง", "06:00 UTC")), m["mkt_trend"]
        _, m, _ = run(ok=False)                                  # Binance funding unreadable: no entries this round
        assert m["entry_day"] is False and "funding" in m["mkt_trend"], m
    finally:
        market.LV_TF, market.LV_SHORT_ONLY, market.LV_GATE_MIN_BASE = real


def test_lowvol_1h_bar_rule_matches_the_research_scan_bar_by_bar():
    # lowvol_breakdown.scan_symbol_tf_trades `valid_brk` restated for one bar i of a random falling 1h series: bc < sma50,
    # ema20 < sma50, bc < sup20 (low of bars i-20..i-1), bc[i-1] >= sup20[i-1], (bc[i-1] - bc[i]) / bc[i-1] <= 8%.
    # The live evaluator must agree at every hour (each hour = the series cut at bar i, daily context passing)
    import datetime as dt
    import random
    utc = dt.timezone.utc
    today_utc, perps, daily, fund7, _ = lv_daily_fixture()
    end = dt.datetime(2026, 11, 15, 5, 0, tzinfo=utc)
    rnd, p, c, hi, lo = random.Random(20261122), 100.0, [], [], []
    for _ in range(240):
        o = p
        p = o * (1.0 + rnd.gauss(-0.0008, 0.006)) * (1.0 - (rnd.uniform(0.02, 0.11) if rnd.random() < 0.04 else 0.0))
        c.append(p)
        hi.append(max(o, p) * (1.0 + rnd.uniform(0.0, 0.003)))
        lo.append(min(o, p) * (1.0 - rnd.uniform(0.0, 0.003)))

    def research(i):
        cs, ls = c[:i + 1], lo[:i + 1]
        sma50 = sum(cs[-50:]) / 50.0
        return (cs[-1] < sma50 and market.causal_ema_series(cs, 20)[-1] < sma50 and cs[-1] < min(ls[-21:-1])
                and cs[-2] >= min(ls[-22:-2]) and (cs[-2] - cs[-1]) / cs[-2] <= 0.08)

    real = market.LV_TF, market.LV_SHORT_ONLY
    market.LV_TF, market.LV_SHORT_ONLY = "1h", False
    try:
        want, got = [], []
        for i in range(54, 240):                                  # live needs 55 bars in its window
            hours = [f"{end - dt.timedelta(hours=i - k):%Y-%m-%d %H}:00:00" for k in range(i + 1)]
            hq = (hours, c[:i + 1], [5e6] * (i + 1), hi[:i + 1], lo[:i + 1], [4] * (i + 1))
            rows, _ = market.lowvol_breakdown_eval(perps, daily, {"LV1USDT": hq}, fund7, today_utc, {s: "2026-11-15 05:45:00" for s in perps},
                                                   True, now_utc=dt.datetime(2026, 11, 15, 6, 5, tzinfo=utc))
            row = next(r for r in rows if r["symbol"] == "LV1USDT")
            want.append(research(i))
            got.append(not row["fail"])
            assert got[-1] == want[-1], (i, row["fail"])
        assert 3 <= sum(want) <= 60, sum(want)                    # the series has real signals and real refusals
    finally:
        market.LV_TF, market.LV_SHORT_ONLY = real


def test_lowvol_1h_exit_ratchets_on_any_1h_close_and_holds_168_hours():
    # research run_tf_exit on 1h bars: stop = clip(3 x ATR20 of the 1h bars up to the signal bar, 10%, 35%), TP -30%, any
    # later 1h bar closing >= +15% in profit moves the stop to entry x 0.96 from the next bar on, out after 168 bars
    from datetime import datetime, timedelta, timezone
    import team
    t0 = datetime(2026, 11, 15, 6, 12, tzinfo=timezone.utc)      # opened 06:12 UTC on the signal bar that closed 06:00
    start = datetime(2026, 11, 13, 0, tzinfo=timezone.utc)

    def bars_pre(rng):                                           # 54 1h bars to the signal bar 05:00, each with range `rng` around 1.0
        return [(f"{start + timedelta(hours=k):%Y-%m-%d %H}:00:00", 1.0 + rng / 2, 1.0 - rng / 2, 1.00) for k in range(54)]

    def bar(h, hi, lo, c):
        return (f"2026-11-15 {h:02d}:00:00", hi, lo, c)

    pre = bars_pre(0.02)
    assert pre[-1][0] == "2026-11-15 05:00:00"
    at = lambda h, m=5: datetime(2026, 11, 15, h, m, tzinfo=timezone.utc)   # noqa: E731
    sl_txt = " · ปิดด้วยราคาตลาด ไม่ใช่ราคา stop"
    real = market.LV_TF, team.LOWVOL_EXIT_ON
    market.LV_TF, team.LOWVOL_EXIT_ON = "1h", True
    try:
        # 1. no saved stop: 3 x ATR20 of the 1h bars = 6% -> the 10% floor; a 5% range gives 15%
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + [bar(6, 1.099, 0.95, 0.97)], at(7)) is None
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + [bar(6, 1.101, 0.95, 0.97)], at(7)) == "หลุด stop 1.1 (+10.0%)" + sl_txt
        wide = bars_pre(0.05)
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, wide + [bar(6, 1.149, 0.95, 0.97)], at(7)) is None
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, wide + [bar(6, 1.151, 0.95, 0.97)], at(7)) == "หลุด stop 1.15 (+15.0%)" + sl_txt
        # ... the stop saved at entry wins over it
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + [bar(6, 1.19, 0.95, 0.97)], at(7), stop_dist=0.2) is None
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + [bar(6, 1.21, 0.95, 0.97)], at(7), stop_dist=0.2) == "หลุด stop 1.2 (+20.0%)" + sl_txt

        # 2. a 1h bar (not the day's last) closing +15.5% moves the stop to 0.96 for the bars after it
        won = [bar(6, 0.99, 0.84, 0.845)]
        got = team.rule_exit("SHORT", 1.0, 0.955, t0, pre + won + [bar(7, 0.961, 0.85, 0.955)], at(8))
        assert got == "หลุด trailing stop ล็อกกำไร 0.96 (-4.0%)" + sl_txt, got
        assert team.rule_exit("SHORT", 1.0, 0.90, t0, pre + won + [bar(7, 0.959, 0.85, 0.90)], at(8)) is None
        # ... the bar that earns the ratchet is itself checked against the old stop (its high 0.97 is not a stop-out)
        assert team.rule_exit("SHORT", 1.0, 0.85, t0, pre + [bar(6, 0.97, 0.83, 0.84), bar(7, 0.95, 0.84, 0.85)], at(8)) is None
        # ... and only once its hour is over: at 06:40 the 06:00 bar is still forming
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + won, at(6, 40)) is None
        got = team.rule_exit("SHORT", 1.0, 0.97, t0, pre + won, at(7, 2))
        assert got == "ราคา mark หลุด trailing stop ล็อกกำไร 0.96 (-4.0%)" + sl_txt, got
        # ... +14.9% is not enough
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + [bar(6, 0.99, 0.85, 0.851)], at(7, 2)) is None

        # 3. TP -30%
        assert team.rule_exit("SHORT", 1.0, 0.71, t0, pre + [bar(6, 0.99, 0.70, 0.71)], at(7)) == "ถึง TP -30% · ปิดด้วยราคาตลาด ไม่ใช่ราคา TP"

        # 4. 168 hours from the signal bar's close (06:00)
        closed = datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc)
        assert team.rule_exit("SHORT", 1.0, 0.99, t0, pre, closed + timedelta(hours=167, minutes=59)) is None
        assert team.rule_exit("SHORT", 1.0, 0.99, t0, pre, closed + timedelta(hours=168)) == "ถือครบ 168 ชม."

        # the daily rule on the same bars: no 20 daily bars -> 35% stop, the ratchet waits for the 23:00 bar, 21 days
        market.LV_TF = "1d"
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + [bar(6, 1.101, 0.95, 0.97)], at(7)) is None
        assert team.rule_exit("SHORT", 1.0, 0.97, t0, pre + won, at(7, 2), stop_dist=0.10) is None
        assert team.rule_exit("SHORT", 1.0, 0.99, t0, pre, closed + timedelta(hours=168)) is None
    finally:
        market.LV_TF, team.LOWVOL_EXIT_ON = real


def test_funding_history_keeps_what_a_page_cuts_inside_one_settlement():
    # Binance returns 1000 records a call and hundreds of perps settle in the same millisecond: the page after a cut starts
    # AT the last time (repeats dropped), not 1 ms later; the windows are the research's (t0 + 60 s, t1 + 60 s]
    import io
    import json
    t_sec = 1_763_186_400                                        # 2026-11-15 06:00 UTC
    end_ms = t_sec * 1000
    recs = [{"symbol": f"C{i:03d}USDT", "fundingTime": end_ms - k * 28_800_000, "fundingRate": "0.0001"} for k in (2, 1, 0) for i in range(600)]
    recs += [{"symbol": "EDGEUSDT", "fundingTime": end_ms - 86_400_000 + 60_000, "fundingRate": "0.5"},    # on the open end: out
             {"symbol": "EDGEUSDT", "fundingTime": end_ms - 86_400_000 + 60_001, "fundingRate": "0.002"},
             {"symbol": "EDGEUSDT", "fundingTime": end_ms + 60_000, "fundingRate": "0.003"},               # on the closed end: in
             {"symbol": "EDGEUSDT", "fundingTime": end_ms + 60_001, "fundingRate": "0.5"}]
    recs.sort(key=lambda r: r["fundingTime"])
    calls = []

    def urlopen(req, timeout=None):
        q = dict(market.urllib.parse.parse_qsl(market.urllib.parse.urlsplit(req.full_url).query))
        calls.append(q)
        return io.BytesIO(json.dumps([r for r in recs if int(q["startTime"]) <= r["fundingTime"] <= int(q["endTime"])][:int(q["limit"])]).encode())

    real = market.urllib.request.urlopen
    market.urllib.request.urlopen = urlopen
    market._FUND.clear()
    try:
        f24, ok = market.perp_funding_24h(t_sec)
        assert ok and len(calls) == 3, (ok, len(calls))
        assert len(f24) == 601 and all(abs(f24[f"C{i:03d}USDT"] - 0.0003) < 1e-12 for i in range(600)), len(f24)   # none of the 1,800 lost
        assert abs(f24["EDGEUSDT"] - 0.005) < 1e-12, f24["EDGEUSDT"]
        f7, ok7 = market.perp_funding_7d(t_sec)
        assert ok7 and abs(f7["C599USDT"][0] - 0.0003) < 1e-12 and abs(f7["C599USDT"][1] - 0.0003) < 1e-12, f7["C599USDT"]
        assert abs(f7["EDGEUSDT"][0] - 0.005) < 1e-12 and abs(f7["EDGEUSDT"][1] - 0.505) < 1e-12, f7["EDGEUSDT"]
        # a round screens again every 30 s: what was read is kept, the next read asks only for its last 2 minutes again
        # (Binance allows 500 calls per 5 min per IP), and that picks up a record that showed up late
        recs.append({"symbol": "LATEUSDT", "fundingTime": end_ms + 5, "fundingRate": "0.004"})
        recs.sort(key=lambda r: r["fundingTime"])
        del calls[:]
        f24b, okb = market.perp_funding_24h(t_sec)
        assert okb and [int(q["startTime"]) for q in calls] == [end_ms + 60_000 - market.FUND_REREAD_MS], calls
        assert abs(f24b["LATEUSDT"] - 0.004) < 1e-12 and {s: v for s, v in f24b.items() if s != "LATEUSDT"} == f24
        del calls[:]
        f7b, ok7b = market.perp_funding_7d(t_sec)
        assert ok7b and len(calls) == 1 and {s: v for s, v in f7b.items() if s != "LATEUSDT"} == f7, len(calls)
        del calls[:]
        f24c, okc = market.perp_funding_24h(t_sec + 3600)                    # the next hour: only what came after
        assert okc and [int(q["startTime"]) for q in calls] == [end_ms + 60_000 - market.FUND_REREAD_MS], calls
        assert abs(f24c["C000USDT"] - 0.0003) < 1e-12 and abs(f24c["EDGEUSDT"] - 0.503) < 1e-12, f24c["EDGEUSDT"]
        del calls[:]
        assert market.perp_funding_7d(t_sec)[0] == f7b and not calls         # ends before the kept tail: nothing to ask
        # 1,200 records in one millisecond cannot be paged: unreadable (the round opens nothing), never a short list;
        # a read that fails leaves the kept records as they were
        kept = dict(market._FUND, rec=dict(market._FUND["rec"]))
        recs[:] = [{"symbol": f"D{i:04d}USDT", "fundingTime": end_ms + 7_200_000, "fundingRate": "0.0001"} for i in range(1200)]
        assert market.perp_funding_24h(t_sec + 7200) == ({}, False) and market._FUND == kept
        market._FUND.clear()
        assert market.perp_funding_24h(t_sec + 7200) == ({}, False) and not market._FUND
    finally:
        market.urllib.request.urlopen = real
        market._FUND.clear()


def test_lowvol_1h_screen_reads_the_day_once_and_the_hour_every_time():
    # a round re-screens every 30 s while ClickHouse lacks the hour's last candle: the 450 daily bars + 7-day funding are
    # read once a UTC day (again each time during the 00:00 hour, when yesterday is still arriving), the 1h bars and the
    # 24h funding up to the hour every time; an unreadable 24h funding stops entries
    import datetime as dt
    utc = dt.timezone.utc
    today_utc, perps, daily, fund7, newest_d = lv_daily_fixture()
    end = dt.datetime(2026, 11, 15, 5, 0, tzinfo=utc)
    h_qv = {s: lv_hourly_fixture(end) for s in ("LV1USDT", "LV2USDT")}
    calls, rt_ok = [], [True]
    real = (market.usdt_perps, market.load_perp_daily_ohlcv, market.perp_funding_7d, market.load_perp_hourly_qv, market.perp_funding_24h,
            market.LV_TF, market.LV_SHORT_ONLY, dict(market._DAY_CTX))
    market.usdt_perps = lambda: perps
    market.load_perp_daily_ohlcv = lambda syms, days=450, before_day=None: calls.append(("day", before_day)) or (daily, newest_d)
    market.perp_funding_7d = lambda t_sec: calls.append(("f7", t_sec)) or (fund7, True)
    market.load_perp_hourly_qv = lambda syms, days=10: calls.append(("hour",)) or (h_qv, {s: "2026-11-15 05:45:00" for s in perps})
    market.perp_funding_24h = lambda t_sec: calls.append(("f24", t_sec)) or ({"LV2USDT": -0.0004}, rt_ok[0])
    market.LV_TF, market.LV_SHORT_ONLY = "1h", False
    market._DAY_CTX.clear()
    try:
        now = dt.datetime(2026, 11, 15, 6, 5, tzinfo=utc)
        t_close = int(dt.datetime(2026, 11, 15, 6, 0, tzinfo=utc).timestamp())
        midnight = int(dt.datetime(2026, 11, 15, 0, 0, tzinfo=utc).timestamp())
        rows, meta = market.short_screen(now)
        assert [r["symbol"] for r in rows if not r["fail"]] == ["LV1USDT"] and meta["entry_day"] is True, meta   # LV2: 24h funding -0.04%
        market.short_screen(now + dt.timedelta(seconds=30))
        assert calls == [("day", today_utc), ("f7", midnight), ("hour",), ("f24", t_close), ("hour",), ("f24", t_close)], calls
        rt_ok[0] = False
        _, meta = market.short_screen(now)
        assert meta["entry_day"] is False and meta["allow_short"] is False and "funding" in meta["mkt_trend"], meta
        # the 00:00 hour of the next day: read the day again on every screen
        del calls[:]
        nxt = dt.datetime(2026, 11, 16, 0, 5, tzinfo=utc)
        market.short_screen(nxt)
        market.short_screen(nxt)
        assert [c[0] for c in calls] == ["day", "f7", "hour", "f24"] * 2, calls
        # the daily rule never asks for the hour's funding
        del calls[:]
        market.LV_TF = "1d"
        market.short_screen(now)
        assert "f24" not in [c[0] for c in calls], calls
    finally:
        saved = real[7]
        (market.usdt_perps, market.load_perp_daily_ohlcv, market.perp_funding_7d, market.load_perp_hourly_qv, market.perp_funding_24h,
         market.LV_TF, market.LV_SHORT_ONLY) = real[:7]
        market._DAY_CTX.clear()
        market._DAY_CTX.update(saved)


if __name__ == "__main__":
    import sys
    import traceback
    import team
    assert (market.LONG_ON is False and team.OPEN_NEW is True and market.LV_SHORT_ONLY is True and team.BTC_HEDGE_ON is False
            and team.LOWVOL_EXIT_ON is True and market.short_screen is market.lowvol_breakdown_screen and team.MARGIN_USDT == 100
            and team.MAX_OPEN == 8 and team.MAX_PAIRS == 6 and market.LV_TF == "1h" and market.LV_MAX_HOLD_HOURS == 168
            ), "live SHORT-only 1h (G_BREADTH) config changed"
    # the tests cover the paired daily H_DELTA0 path too (live is SHORT only + breadth gate on 1h bars); the 1h tests set LV_TF themselves
    team.BTC_HEDGE_ON, market.LV_SHORT_ONLY, market.LV_TF = True, False, "1d"
    team.OPEN_NEW = True   # the tests cover opening
    market.LONG_ON = True  # ... and the LONG paths (live is SHORT only: market.py LONG_ON)
    binance_candles, team.recent_range = team.recent_range, lambda positions, now_: {}   # no test reads Binance; the watch test puts it back
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
            except Exception:  # noqa: BLE001 — team.py's sys.excepthook would send it to the muted log
                traceback.print_exc()
                sys.exit(1)
            print("ok", name)
