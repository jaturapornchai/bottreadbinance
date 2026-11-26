"""Chart Analyst agent: draws the coin's daily closes from ClickHouse in headless Edge/Chromium, reads the picture with a vision model.
Only our own data: no TradingView (its feed isn't ours to check), and the reading carries no numbers (the code shows those)."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time

import ollama

ROOT = os.path.dirname(os.path.abspath(__file__))
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe", "/usr/bin/chromium"]   # last = Linux server image
# open-source lightweight-charts (the library only; every price on it is our ClickHouse close)
LWC = """<!doctype html><html><body style="margin:0"><div id="c"></div>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script>const c=LightweightCharts.createChart(document.getElementById("c"),{{width:1280,height:720,
rightPriceScale:{{mode:1}}}});
c.addLineSeries({{color:"#2962ff",lineWidth:2}}).setData({close});
c.addLineSeries({{color:"#ff9800",lineWidth:2}}).setData({sma});
c.timeScale().fitContent();</script></body></html>"""


def _seed_local_state(profile: str, browser: str = ""):
    """Copy password_manager.os_password_blank/last_changed from Chrome/Edge Local State on Windows so headless Chromium
    153+ does not call LogonUser(user, '.', '') with an empty password and trigger Windows Account Lockout."""
    if os.name != "nt":
        return False
    local_app = os.environ.get("LOCALAPPDATA") or ""
    if not local_app:
        return False
    chrome_ls = os.path.join(local_app, "Google", "Chrome", "User Data", "Local State")
    edge_ls = os.path.join(local_app, "Microsoft", "Edge", "User Data", "Local State")
    sources = (edge_ls, chrome_ls) if "edge" in (browser or "").lower() else (chrome_ls, edge_ls)
    for src in sources:
        try:
            with open(src, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        pm = data.get("password_manager") if isinstance(data, dict) else None
        if not isinstance(pm, dict):
            continue
        blank = pm.get("os_password_blank")
        changed = pm.get("os_password_last_changed")
        if isinstance(blank, bool) and isinstance(changed, str) and changed:
            try:
                with open(os.path.join(profile, "Local State"), "w", encoding="utf-8", newline="\n") as f:
                    json.dump({"password_manager": {"os_password_blank": blank, "os_password_last_changed": changed}}, f)
                return True
            except Exception:  # noqa: BLE001
                return False
    return False


def _shoot(page_html, name, png, waits, min_bytes):
    """Headless Edge screenshot of a local page. Own profile dir so it never touches the user's Edge windows."""
    browser = next(b for b in BROWSERS if os.path.exists(b))
    page = os.path.join(ROOT, "cache", "tv", name)
    os.makedirs(os.path.dirname(page), exist_ok=True)
    with open(page, "w", encoding="utf-8") as f:
        f.write(page_html)
    for wait_ms in waits:
        # fresh profile per shot: with a shared one, a still-running Edge takes the job and this call returns
        # before the png is written (charts came back "none" and the png showed up later)
        profile = tempfile.mkdtemp(prefix="edge-", dir=os.path.join(ROOT, "cache"))
        try:
            _seed_local_state(profile, browser)
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1280,720",
                            *(["--no-sandbox"] if os.name != "nt" else []),   # Chromium in Docker has no user namespaces
                            f"--user-data-dir={profile}", f"--timeout={wait_ms}",
                            f"--screenshot={png}", pathlib.Path(page).as_uri()],
                           capture_output=True, timeout=wait_ms / 1000 + 60)
        finally:
            shutil.rmtree(profile, ignore_errors=True)
        if os.path.exists(png) and os.path.getsize(png) > min_bytes:
            return True
    if os.path.exists(png):
        os.remove(png)  # don't leave spinner screenshots in reports/charts
    return False


def own_chart_html(days):
    closes = [c for _, c in days]
    close = [{"time": d, "value": c} for d, c in days]
    sma = [{"time": days[i][0], "value": sum(closes[i - 199:i + 1]) / 200} for i in range(199, len(days))]
    return LWC.format(close=json.dumps(close), sma=json.dumps(sma))


def snapshot(symbol, out_dir, days):
    """(png, source): our ClickHouse daily-close chart ("own", "own_ma200" once there are 200 days for the MA),
    else (None, "none"). A blank page is ~11 KB; a drawn chart is 15 KB+. Reuses today's png when daily bars match."""
    os.makedirs(out_dir, exist_ok=True)
    src = "own_ma200" if len(days) >= 200 else "own"
    tag = f"{symbol}-D-{days[-1][0].replace('-', '')}-{len(days)}" if days else ""
    if tag:
        for name in sorted(os.listdir(out_dir), reverse=True):
            if name.startswith(tag) and name.endswith(".png"):
                p = os.path.join(out_dir, name)
                if os.path.getsize(p) > 15_000:
                    return p, src
    png = os.path.join(out_dir, f"{tag or symbol + '-D'}-{time.strftime('%Y%m%d-%H%M')}.png")
    if _shoot(own_chart_html(days), f"{symbol}.html", png, (8000,), 15_000):
        return png, src
    return None, "none"


IMAGE_NOTE = {"own": "ภาพ = เส้นราคาปิดรายวัน (น้ำเงิน) สเกล log วาดจาก ClickHouse",
              "own_ma200": "ภาพ = เส้นราคาปิดรายวัน (น้ำเงิน) + เส้นเฉลี่ย 200 วัน (ส้ม) สเกล log วาดจาก ClickHouse",
              "none": "ไม่มีภาพ (วาดกราฟไม่ได้) — ดูจาก indicators อย่างเดียว ถ้าตัดสินไม่ได้ให้ตอบ trend = unclear"}

SCHEMA = {"type": "object", "required": ["trend", "note_th"], "properties": {
    "trend": {"type": "string", "enum": ["unclear", "up", "down", "sideways"]}, "note_th": {"type": "string"}}}

SYSTEM = """คุณคือช่างกราฟของทีม BotTrade ดูกราฟของเหรียญที่ทีมพิจารณา short (ได้เมื่อราคาลง เสียเมื่อราคาขึ้น) ฟิลด์ image บอกว่าภาพคืออะไร
- ใช้เฉพาะสิ่งที่เห็นในภาพ + indicators ที่ให้มา (โค้ดคำนวณจาก ClickHouse) ห้ามเดา ห้ามใช้ข่าวหรือความรู้ภายนอก
- indicators: price = ราคาปิดวันล่าสุด, dd_from_ath = ห่างจากราคาปิดสูงสุดในข้อมูลที่มี (ไม่ใช่ ATH จริงถ้าข้อมูลสั้น), ret_30d = ราคาเปลี่ยนใน 30 วัน, days_of_data = จำนวนวันในกราฟ
- trend: ทิศทางของเส้นราคาในภาพ ถ้าภาพไม่ชัดหรือข้อมูลน้อยเกินตัดสิน ตอบ unclear
- note_th: 1-2 ประโยคภาษาไทยบรรยายรูปทรงกราฟ (เช่น พุ่งแล้วย่อ ไหลลงต่อเนื่อง แกว่งในกรอบ) ห้ามมีตัวเลขใดๆ (ตัวเลขจริงโค้ดแสดงเอง) ไม่อวย"""


READINGS_CACHE = os.path.join(ROOT, "cache", "chart-readings.json")


def read(model, symbol, ind, png, source):
    """trend + note_th. A note with a number that isn't in its own input is blanked: the AI describes shape, never figures.
    Caches readings by PNG filename so unchanged daily charts aren't re-read every hour by the vision model."""
    key = os.path.basename(png) if png and os.path.exists(png) else None
    saved = {}
    if key:
        try:
            with open(READINGS_CACHE, encoding="utf-8") as f:
                saved = json.load(f)
            if key in saved and isinstance(saved[key], dict) and "trend" in saved[key] and "note_th" in saved[key]:
                return dict(saved[key])
        except Exception:  # noqa: BLE001
            saved = {}
    note = IMAGE_NOTE[source]
    user = json.dumps({"symbol": symbol, "image": note, "indicators": ind}, ensure_ascii=False, default=str)
    r = ollama.chat(model, SYSTEM, user, SCHEMA, image_path=png, think=False, max_wait_s=600)
    if ollama.ungrounded(r["note_th"], symbol + note):
        r["note_th"] = ""
    if key and r.get("trend"):
        try:
            os.makedirs(os.path.dirname(READINGS_CACHE), exist_ok=True)
            saved[key] = {"trend": r["trend"], "note_th": r["note_th"]}
            if len(saved) > 300:
                saved = dict(list(saved.items())[-200:])
            with open(READINGS_CACHE, "w", encoding="utf-8") as f:
                json.dump(saved, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    return r
