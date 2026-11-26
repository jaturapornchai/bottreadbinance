"""Auto Short trading proxy (ลุงจืด 2026-10-26 "binance.py ให้เป็นแค่ proxy ในการเทรด การตัดสินใจ อยู่ ฝั่งนี้เลย" + "ป้องกันด้วย").

The Binance key lives only on the proxy server (<PROXY_SERVER_IP>, the IP the key is locked to). There this file runs as a
service (`python3 binance.py serve`, 127.0.0.1:8787) that signs and sends orders; the team machine (.88, team.py) makes
every trading decision (which coin, LONG/SHORT, lot size, when to close, cooldown, budget) and reaches the service only
through an SSH tunnel (`ssh -L` as a user that may only forward to 127.0.0.1:8787) with DCA_PLAN_TOKEN on every request.
The service is a thin proxy WITH guardrails, so a wrong or stolen client can do no more than:
  POST /open  {run_id, symbol, side, usdt, leverage?}: one 1x-MAX_LEVERAGE ISOLATED MARKET order of at most SHORT_LOT_USDT
              notional, id derived here
              (bto-<run_id>-<coin>, run_id = today or yesterday UTC, never resent), at most DAILY_OPEN_CAP a UTC day,
              never on a coin anyone holds, not while a bot order is unclear, not on a wide book (MAX_SPREAD);
  POST /close {id, why?}: the whole ledger qty of one bot position, reduceOnly at market (ลุงจืด's own positions never);
  POST /sync  {run_id}: bookkeeping only (settle unclear orders, record what Binance closed with its real PnL);
  GET /portfolio, GET /check: read only.
No stop-loss or take-profit on Binance (ลุงจืด 2026-10-13 "ไม่มี stop เลย"): a position nobody closes runs until Binance
liquidates it (1x isolated: a short near +100%, a long only near zero). Real orders only when SHORT_LIVE=1 (set by root on the server); otherwise /open
goes to /fapi/v1/order/test (validated, never traded) and /close sends nothing.
Money is Decimal end to end and stored as strings; every order is appended to SHORT_LEDGER before it is sent.
Python 3.8+ (the server's python3): no removesuffix / dict |=."""
import contextlib
import hashlib
import hmac
import http.client
import http.server
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation

HERE = os.path.dirname(os.path.abspath(__file__))
FAPI, SPOT = "fapi.binance.com", "api.binance.com"
PORT = 8787   # the service, on the server's loopback only
LEDGER = os.environ.get("SHORT_LEDGER") or os.path.join(HERE, "reports", "short-orders.jsonl")
PORTFOLIO = os.path.join(HERE, "cache", "portfolio.json")   # PC side: what the Windows app shows
CENT = Decimal("0.01")
DAILY_OPEN_CAP = 20             # opens sent per server UTC day (ลุงจืด 2026-10-26; the most ever was 14 on 2026-10-15)
MAX_SPREAD = Decimal("0.005")   # (ask - bid) / bid above this = a thin book: no market order into it
MAX_LEVERAGE = 2                # ลุงจืดอนุมัติ R0 2026-11-26: 2x isolated (Margin 100 × 2x = 200 USDT); more is refused
MAX_HEDGE_LEGS = 8              # max concurrent paired BTCUSDT LONG hedge legs (H_DELTA0: 1 leg per active alt SHORT)
CLOSED_DAYS, CLOSED_MIN = 30, 50   # /portfolio lists every close of the last 30 days, at least the newest 50
SETUP = "รัน bottread-dca-setup ใน Console ของ server"
PREFIX = {"open": "bto", "close": "btc", "closed": "btx", "stop": "bts", "tp": "btt"}
log = logging.getLogger("short")


class BinanceError(Exception):
    def __init__(self, status, body):
        self.status, self.code = status, body.get("code") if isinstance(body, dict) else None
        super().__init__(f"Binance HTTP {status}: {str(body)[:300]}")


class Client:
    def __init__(self, key="", secret="", host=FAPI):
        self.key, self.secret, self.host, self.offset = key, secret.encode(), host, 0

    def call(self, method, path, signed=False, **params):
        if signed:   # HMAC-SHA256 of the exact query string; the secret itself never leaves this server
            params.update(recvWindow=10000, timestamp=int(time.time() * 1000) + self.offset)
        q = urllib.parse.urlencode(params)
        if signed:
            q += "&signature=" + hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
        conn = http.client.HTTPSConnection(self.host, timeout=30)
        try:
            conn.request(method, path + ("?" + q if q else ""), headers={"X-MBX-APIKEY": self.key} if self.key else {})
            r = conn.getresponse()
            raw = r.read()
        finally:
            conn.close()
        try:
            body = json.loads(raw, parse_float=Decimal) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        if r.status != 200:
            raise BinanceError(r.status, body)
        return body

    def sync_clock(self):
        self.offset = self.call("GET", "/fapi/v1/time" if self.host == FAPI else "/api/v3/time")["serverTime"] - int(time.time() * 1000)


def utcnow():
    return datetime.now(timezone.utc)


def coin(sym):
    return sym[:-4] if sym.endswith("USDT") else sym


def dec(x):
    return Decimal(str(x))


def num(x):
    """Decimal -> plain string for Binance (no exponent)."""
    return format(x.normalize(), "f")


def money(x):
    return f"{x:,.2f}"


def as_str(x, q=None):
    """Decimal -> JSON string (None stays None); q = quantum to round to."""
    return None if x is None else str(x.quantize(q) if q else x)


def filters(info):
    return {f["filterType"]: f for f in info["filters"]}


def lot(info):
    """(step, min qty) a MARKET order's quantity must respect: LOT_SIZE and MARKET_LOT_SIZE both apply."""
    f = filters(info)
    sizes = [f[k] for k in ("LOT_SIZE", "MARKET_LOT_SIZE") if k in f]
    return max([dec(s["stepSize"]) for s in sizes] or [Decimal(0)]), max([dec(s["minQty"]) for s in sizes] or [Decimal(0)])


def floor_to(q, step):
    return (q / step).to_integral_value(ROUND_DOWN) * step if step else q


def _tag(name, max_len=20):
    if not name.isascii() or len(name) > max_len or not re.fullmatch(r"[A-Z0-9]+", name):
        return "z" + hashlib.sha256(name.encode()).hexdigest()[:10]
    return name


def order_id(kind, run_id, sym, pair=None):
    """Same plan + coin (+ hedge pair) -> same id (<= 36 chars): a rerun asks Binance whether the order already exists."""
    name = coin(sym)
    if not name.isascii():   # ids allow ASCII only: a Chinese-named coin gets a fixed tag (lower case, so never a real ticker)
        name = "z" + hashlib.sha256(name.encode()).hexdigest()[:10]
    if pair:
        return f"{PREFIX[kind]}-{run_id}-{name}-{_tag(pair, 14)}"
    return f"{PREFIX[kind]}-{run_id}-{name}"


def sibling(open_id, kind):
    """The close / stop / take-profit / closed id of a short = its open id with another prefix: one of each per short."""
    return PREFIX[kind] + open_id[3:]


def ledger(path=None):
    """Every readable line of the order ledger (or of `path`). A torn line (crash mid-write) is skipped with a warning:
    each POST is preceded by a complete 'sending' line, so skipping one never hides an order."""
    path = path or LEDGER
    rows = []
    try:
        with open(path, "rb") as f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:   # JSONDecodeError and UnicodeDecodeError
                        print(f"warning: {path} line {n} is not JSON, skipped", file=sys.stderr)
    except FileNotFoundError:
        pass
    return rows


def log_path():
    return os.path.join(os.path.dirname(LEDGER), "short-log.jsonl")


def journal(run_id, lines):
    """The service's own log next to the ledger: what it decided and why, one row per line (ลุงจืด 2026-10-11 "ทำระบบ
    log จะได้ตรวจสอบง่าย"). The app shows the newest 100 under พอร์ต Binance. ponytail: no rotation (a few KB a day);
    stops past 50 MB so a flood of wrong-token requests cannot fill the disk."""
    path = log_path()
    at = utcnow().isoformat(timespec="seconds")
    with contextlib.suppress(OSError):   # a log that cannot be written must never block an order
        if os.path.exists(path) and os.path.getsize(path) > 50_000_000:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(json.dumps({"at": at, "run_id": run_id, "text": t}, ensure_ascii=False) + "\n" for t in lines)


def finals():
    """{client_order_id: last ledger line} — the last line about an order is its outcome; dict order = order last sent
    (a rejected order resent later replays after what happened in between)."""
    out = {}
    for r in ledger():
        if r["how"] == "sending":
            out.pop(r["client_order_id"], None)
        out[r["client_order_id"]] = r
    return out


def record(run_id, sym, kind, o, how, at=None, **extra):
    """Append-only audit line (never edited; fixes go in as new lines). kind: open (SELL) | close (BUY reduceOnly) |
    closed (Binance shows the short gone; PnL from its income history). at = when an older order was sent (settle)."""
    qty, quote = dec(o.get("executedQty", "0")), dec(o.get("cumQuote", "0"))
    row = {"at": at or utcnow().isoformat(), "actor": "bottread-short", "run_id": run_id, "kind": kind, "symbol": sym,
           "client_order_id": o.get("clientOrderId"), "order_id": o.get("orderId"), "how": how, "status": o.get("status"),
           "qty": str(qty), "quote_qty": str(quote), "avg_price": str((quote / qty).quantize(Decimal("1e-8"))) if qty else None,
           "update_time": o.get("updateTime"), **extra}
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "ab+") as f:
        if f.seek(0, os.SEEK_END):
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":   # a torn last line (crash mid-write) must not swallow this one
                f.write(b"\n")
        f.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
        f.flush()
        os.fsync(f.fileno())   # on disk before the order goes out: a power cut must not lose that it was sent
    return row


def settings():
    """Validated Auto Short settings from the server's /etc/bottread/dca.env, or (None, why) when it is off."""
    raw = (os.environ.get("SHORT_LOT_USDT") or "").strip()
    if not raw:
        return None, f"Auto Short ปิดอยู่ (ยังไม่ได้ตั้งไม้ละกี่ USDT — {SETUP})"
    try:
        lot_usdt = Decimal(raw)
    except InvalidOperation:
        lot_usdt = None
    if lot_usdt is None or not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", raw) or lot_usdt <= 0:
        return None, f"ไม้ละ SHORT_LOT_USDT ต้องเป็นตัวเลข USDT เช่น 50 หรือ 50.25 (ตอนนี้: {raw[:20]!r}) — {SETUP}"
    return {"lot": lot_usdt, "live": live(), **(keys() or {"key": "", "secret": ""})}, None


def keys():
    """{key, secret} of the server's Binance API key, or None. Reading and closing need only these."""
    k = {"key": (os.environ.get("BINANCE_API_KEY") or "").strip(), "secret": (os.environ.get("BINANCE_API_SECRET") or "").strip()}
    return k if k["key"] and k["secret"] else None


def live():
    return os.environ.get("SHORT_LIVE", "").strip() == "1"


def mode(live):
    return "ซื้อขายจริง" if live else "ทดสอบ (ไม่ซื้อขายจริง)"


def gate(run_id, today):
    """(settings, None, None) when /open may send now, else (None, why code, Thai line). Only today's or yesterday's run
    id (a round can cross UTC midnight): an older id whose ledger line is gone would pass GET-before-POST (Binance
    forgets orders after 3-90 days) and open twice."""
    cfg, why = settings()
    if not cfg:
        return None, "live_off", why
    if not (cfg["key"] and cfg["secret"]):
        return None, "setup", f"ยังไม่ได้ใส่ Binance API key บน server ({SETUP})"
    if not 0 <= (today - date.fromisoformat(run_id)).days <= 1:
        return None, "old_run", f"ข้ามแผน {run_id} เพราะเป็นแผนเก่า"
    return cfg, None, None


DONE = ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED")   # Binance statuses that never change again


def unclear(r):
    """Outcome not known yet: still open at Binance, or filled but answered without its price (Binance's POST answer said
    cumQuote 0 for a FILLED CT short on 2026-10-12; a GET had the real one): entry unknown = no stop-loss, so ask again."""
    return r["how"] == "sending" or (r["kind"] != "closed" and r["how"] != "rejected" and
                                     (r.get("status") not in DONE or (dec(r["qty"]) > 0 and not dec(r["quote_qty"]))))


def positions():
    """The bot's own positions (SHORT or LONG) from its ledger, keyed by the open order's client id, oldest first. A position
    is open until a 'closed' line; unclear = one of its orders has an unknown outcome (a human checks Binance first).
    Positions ลุงจืด opened by hand are never in here, so never touched."""
    pos = {}
    for r in finals().values():
        if r["how"] == "rejected":
            continue
        if r["kind"] == "open":
            if unclear(r) or dec(r["qty"]) > 0:
                pos[r["client_order_id"]] = {"id": r["client_order_id"], "symbol": r["symbol"], "run_id": r["run_id"], "at": r["at"],
                                             "side": r.get("side") or "SHORT", "pair": r.get("pair"), "order_id": r.get("order_id"),
                                             "ms": r.get("update_time"), "qty": dec(r["qty"]), "quote": dec(r.get("quote_qty", "0")),
                                             "entry": dec(r["avg_price"]) if r["avg_price"] else None,
                                             "unclear": unclear(r), "close": None, "closed": None}
        elif r.get("pos") in pos:
            p = pos[r["pos"]]
            if r["kind"] == "close":
                p["close"], p["unclear"] = r, p["unclear"] or unclear(r)
            else:
                p["closed"] = r
    return pos


def on_binance(c):
    """{symbol: position row} for every symbol the futures account holds a position in (a short has positionAmt < 0)."""
    return {r["symbol"]: r for r in c.call("GET", "/fapi/v3/positionRisk", signed=True) if dec(r["positionAmt"])}


def our_algos(c):
    """{clientAlgoId: order} of the open stop-loss / take-profit orders this bot placed."""
    return {a["clientAlgoId"]: a for a in c.call("GET", "/fapi/v1/openAlgoOrders", signed=True)
            if (a.get("clientAlgoId") or "")[:4] in ("bts-", "btt-")}


def symbols_info(c):
    return {s["symbol"]: s for s in c.call("GET", "/fapi/v1/exchangeInfo")["symbols"]}


def income(c, start_ms, sym=None, kind=None):
    """Every income line (realized PnL, fees, funding, liquidation …) since start_ms; Binance keeps 3 months."""
    rows, page, end = [], 1, int(time.time() * 1000) + c.offset
    while True:
        params = {"startTime": start_ms, "endTime": end, "limit": 1000, "page": page}
        if sym:
            params["symbol"] = sym
        if kind:
            params["incomeType"] = kind
        got = c.call("GET", "/fapi/v1/income", signed=True, **params)
        rows += got
        if len(got) < 1000:
            return rows
        page += 1


def opened_ms(p):
    """When a short opened, minus a minute so its opening fee is inside an income query from here."""
    return (p["ms"] or int(datetime.fromisoformat(p["at"]).timestamp() * 1000)) - 60_000


def closed_ms(p):
    if p.get("close") and p["close"].get("update_time"):
        return int(p["close"]["update_time"]) + 60_000
    if p.get("closed") and p["closed"].get("at"):
        return int(datetime.fromisoformat(p["closed"]["at"]).timestamp() * 1000) + 60_000
    return int(time.time() * 1000) + 60_000


def hedge_funding_share(p, all_pos, fund_rows):
    """Pro-rate BTCUSDT FUNDING_FEE income rows across all bot BTCUSDT LONG hedge legs active at each funding timestamp."""
    legs = [q for q in all_pos if q["symbol"] == p["symbol"] and q.get("side") == "LONG" and q.get("pair") and q["qty"] > 0]
    t0, t1 = opened_ms(p), closed_ms(p)
    tot = Decimal(0)
    for r in fund_rows:
        if r.get("symbol") != p["symbol"] or r.get("asset", "USDT") != "USDT":
            continue
        ts = int(r["time"])
        if not (t0 <= ts <= t1):
            continue
        denom = sum((q["qty"] for q in legs if opened_ms(q) <= ts <= closed_ms(q)), Decimal(0))
        if denom > 0:
            tot += dec(r["income"]) * p["qty"] / denom
    return tot


def order_fees(c, sym, oids):
    """Exact USDT + non-USDT commissions for specific orderIds via /fapi/v1/userTrades; returns (usdt_fees, other_assets) or None."""
    usdt_fee, other = Decimal(0), {}
    for oid in oids:
        if not oid:
            return None
        try:
            trades = c.call("GET", "/fapi/v1/userTrades", signed=True, symbol=sym, orderId=oid)
        except Exception:  # noqa: BLE001
            return None
        for t in trades or []:
            asset, comm = t.get("commissionAsset", "USDT"), -abs(dec(t.get("commission", "0")))
            if asset == "USDT":
                usdt_fee += comm
            elif comm:
                other[asset] = str(dec(other.get(asset, "0")) + comm)
    return usdt_fee, other


def closed(c, p, why, all_pos=None):
    """Record a short Binance no longer shows, with what it really made: every USDT income line of that symbol since it
    opened (realized PnL, both fees, funding, liquidation). For a paired BTCUSDT LONG hedge leg (p['pair']), compute exact
    per-leg PnL from its own open/close fills + order commissions + pro-rated funding so concurrent hedge legs never double-count."""
    if p.get("pair") and p.get("close") and p["close"].get("status") == "FILLED" and p.get("entry") and p["close"].get("avg_price"):
        open_quote = p["quote"] if p.get("quote") else p["qty"] * p["entry"]
        close_quote = dec(p["close"]["quote_qty"]) if dec(p["close"].get("quote_qty", "0")) > 0 else p["qty"] * dec(p["close"]["avg_price"])
        price_pnl = close_quote - open_quote
        rows = income(c, opened_ms(p), p["symbol"])
        fund_rows = [r for r in rows if r.get("incomeType") == "FUNDING_FEE"]
        fund = hedge_funding_share(p, all_pos or list(positions().values()), fund_rows)
        got_fees = order_fees(c, p["symbol"], [p.get("order_id"), p["close"].get("order_id")])
        if got_fees is not None:
            fees, other = got_fees
        else:
            fees = -(open_quote + close_quote) * Decimal("0.0005")
            other = {}
        net = price_pnl + fund + fees
        why = why or f"ปิด hedge คู่ {p['pair']}"
        return record(p["run_id"], p["symbol"], "closed", {"clientOrderId": sibling(p["id"], "closed")}, "synced", pos=p["id"],
                      side="LONG", pair=p["pair"], why=why, pnl=str(net), funding=str(fund), fees=str(fees), other_assets=other)
    rows = income(c, opened_ms(p), p["symbol"])
    if not any(r["incomeType"] == "REALIZED_PNL" for r in rows):
        return None   # ponytail: Binance has not posted it yet (a close at exactly the entry never posts one: a human checks)
    usdt = [r for r in rows if r.get("asset", "USDT") == "USDT"]
    other = {}
    for r in rows:
        if r.get("asset", "USDT") != "USDT":
            other[r["asset"]] = str(dec(other.get(r["asset"], "0")) + dec(r["income"]))

    def total(*kinds):
        return sum((dec(r["income"]) for r in usdt if not kinds or r["incomeType"] in kinds), Decimal(0))
    share = Decimal(1)
    if p.get("pair"):
        peers = [q for q in (all_pos or positions().values())
                 if q["symbol"] == p["symbol"] and q.get("pair") and not q["closed"] and q["qty"] > 0]
        denom = sum((q["qty"] for q in peers), Decimal(0))
        if denom > 0:
            share = p["qty"] / denom
    why = why or "ปิดนอกบอท (ถูกบังคับปิด, stop/take-profit ที่ค้างจากกฎเดิม หรือปิดเองในแอป)"
    extra_side = {k: p[k] for k in ("side", "pair") if p.get(k) and (k != "side" or p[k] == "LONG")}
    return record(p["run_id"], p["symbol"], "closed", {"clientOrderId": sibling(p["id"], "closed")}, "synced", pos=p["id"], why=why,
                  pnl=str(total() * share), funding=str(total("FUNDING_FEE") * share), fees=str(total("COMMISSION") * share),
                  other_assets=other, **extra_side)


def settle(c):
    """Ask Binance about every order whose outcome is not final: a 'sending' line (timeout, crash) or an answer that was
    still open. Found and changed -> its real outcome, dated when it was sent; a 'sending' order unknown to Binance
    10+ minutes later -> never placed ('rejected'); anything else stays unclear."""
    lines = []
    for r in [r for r in finals().values() if unclear(r)]:
        sym, cid = r["symbol"], r["client_order_id"]
        extra = {k: r[k] for k in ("pos", "why", "side", "pair") if k in r}
        try:
            o = c.call("GET", "/fapi/v1/order", signed=True, symbol=sym, origClientOrderId=cid)
        except BinanceError as e:
            if r["how"] == "sending" and e.code == -2013 and utcnow() - datetime.fromisoformat(r["at"]) > timedelta(minutes=10):
                record(r["run_id"], sym, r["kind"], {"clientOrderId": cid}, "rejected", r["at"], **extra)
                lines.append(f"{coin(sym)}: คำสั่ง {cid} ที่ไม่รู้ผล ไม่มีใน Binance — นับว่าไม่ได้ส่ง")
            continue
        except Exception:  # noqa: BLE001 — unreachable now: stays unclear, asked again next round
            log.exception("settle %s failed", cid)
            continue
        if (r["how"] != "sending" and o.get("status") == r.get("status") and dec(o.get("executedQty", "0")) == dec(r["qty"])
                and dec(o.get("cumQuote", "0")) == dec(r["quote_qty"])):
            continue   # still open, nothing new: no extra line every hour
        # only an answered POST ('placed') is routine; a lost answer or an order the ledger lost ('found') stays a warning
        how = "found-after-error" if r["how"] in ("sending", "found", "found-after-error") else "settled"
        lines.append(f"คำสั่ง {cid} ที่ไม่รู้ผล ตรวจใน Binance แล้ว: " + filled(record(r["run_id"], sym, r["kind"], o, how, r["at"], **extra)))
    return lines


def filled(r):
    side_th = "long" if r.get("side") == "LONG" else "short"
    act = f"เปิด {side_th}" if r["kind"] == "open" else f"ปิด {side_th}"
    if dec(r["qty"]) > 0 and not dec(r["quote_qty"]):   # Binance's answer had no price: settle asks for it in this round
        return f"{coin(r['symbol'])}: ส่งคำสั่ง{act} แล้ว {num(dec(r['qty']))} {coin(r['symbol'])} สถานะ {r['status']} — รอราคาจาก Binance"
    return (f"{coin(r['symbol'])}: {act} แล้ว {num(dec(r['qty']))} {coin(r['symbol'])} มูลค่า {money(dec(r['quote_qty']))} USDT"
            f" (เฉลี่ย {r['avg_price']}) สถานะ {r['status']}")


def _send(c, run_id, sym, kind, params, **extra):
    """One MARKET order, ledger first. Returns (status, ledger row or None, Thai line); status: placed | found |
    rejected | unclear (sent, outcome unknown: the 'sending' line keeps it unclear) | refused (nothing sent)."""
    side_th = "long" if extra.get("side") == "LONG" else "short"
    cid, act, name = params["newClientOrderId"], f"เปิด {side_th}" if kind == "open" else f"ปิด {side_th}", coin(sym)
    try:
        o = c.call("GET", "/fapi/v1/order", signed=True, symbol=sym, origClientOrderId=cid)
    except BinanceError as e:
        if e.code != -2013:   # anything but "Order does not exist" means we cannot be sure: do not send
            return "refused", None, f"{name}: ตรวจคำสั่งเดิมไม่ได้ ไม่{act} ({e})"
        o = None
    except Exception as e:  # noqa: BLE001
        return "refused", None, f"{name}: ตรวจคำสั่งเดิมไม่ได้ ไม่{act} ({str(e)[:150]})"
    if o:
        sent_ms = o.get("time") or o.get("updateTime")   # the ledger lost it: date it by Binance (hold days, funding)
        r = record(run_id, sym, kind, o, "found", at=datetime.fromtimestamp(sent_ms / 1000, timezone.utc).isoformat() if sent_ms else None, **extra)
        return "found", r, f"{name}: มีคำสั่ง {cid} อยู่แล้ว (สถานะ {r['status']}) ไม่{act}ซ้ำ"
    try:   # written BEFORE the POST: a timeout, crash or full disk after this still counts (positions 'unclear')
        record(run_id, sym, kind, {"clientOrderId": cid}, "sending", **extra)
    except OSError as e:
        return "refused", None, f"{name}: เขียนบันทึกคำสั่งไม่ได้ ไม่{act} ({e})"
    try:
        o = c.call("POST", "/fapi/v1/order", signed=True, **params)
    except BinanceError as e:
        if e.status < 500 and e.status != 408:   # 4xx = rejected, nothing placed; 408/5xx = Binance does not know yet
            return "rejected", record(run_id, sym, kind, {"clientOrderId": cid}, "rejected", **extra), f"{name}: Binance ไม่รับคำสั่ง{act} ({e})"
        return _after_error(c, run_id, sym, kind, cid, e, extra)
    except Exception as e:  # noqa: BLE001 — timeout/connection drop: the order may still exist
        return _after_error(c, run_id, sym, kind, cid, e, extra)
    r = record(run_id, sym, kind, o, "placed", **extra)
    return "placed", r, filled(r)


def _after_error(c, run_id, sym, kind, cid, err, extra):
    """Status unknown at Binance: ask by our client id before reporting anything; never resend blindly."""
    try:
        r = record(run_id, sym, kind, c.call("GET", "/fapi/v1/order", signed=True, symbol=sym, origClientOrderId=cid),
                   "found-after-error", **extra)
    except Exception:  # noqa: BLE001 — not found or still unreachable: the 'sending' line keeps it unclear, a human checks
        return "unclear", None, f"{coin(sym)}: ส่งคำสั่งแล้วไม่ได้คำตอบ ({str(err)[:150]}) — เปิดดูประวัติคำสั่ง {cid} ใน Binance"
    return "found", r, filled(r)


def reply(status, why, line, cid, r=None):
    """The one answer of /open and /close: the team branches on status and why, never on the Thai line. A ledger row
    whose outcome is not final is 'unclear' whatever the send said: Binance answers every MARKET order FILLED with
    cumQuote 0 (all 50 first answers in the server ledger), so a real order stays unclear until /sync asks again."""
    if r is not None and unclear(r):
        status = "unclear"
    r = r or {}
    return {"status": status, "why": why, "cid": cid, "order_status": r.get("status"), "qty": r.get("qty"),
            "quote_qty": r.get("quote_qty"), "avg_price": r.get("avg_price"), "line": line}


def _failed(cid, name, e):
    """An error mid-request: unclear when a 'sending' line now stands for this id (the order may be out), else nothing was sent."""
    log.exception("%s failed", cid)
    r = finals().get(cid)
    return reply("unclear" if r and unclear(r) else "refused", "error",
                 f"{name}: ผิดพลาด ({str(e)[:150]}) — เปิดดูประวัติคำสั่ง {cid} ใน Binance", cid)


def _close(c, p, why):
    """The whole bot position at market, reduceOnly (it can never open or grow one); id = its open id's sibling."""
    is_long = p.get("side") == "LONG"
    params = {"symbol": p["symbol"], "side": "SELL" if is_long else "BUY", "type": "MARKET", "quantity": num(p["qty"]), "reduceOnly": "true",
              "newClientOrderId": sibling(p["id"], "close"), "newOrderRespType": "RESULT"}
    extra = {"pos": p["id"], "why": why, **({"side": "LONG"} if is_long else {}), **({"pair": p["pair"]} if p.get("pair") else {})}
    return _send(c, p["run_id"], p["symbol"], "close", params, **extra)


def held_amt(p, held):
    """How much of the bot position's side Binance holds now (`held` = its positionRisk row or None): <= 0 = gone."""
    if not held:
        return Decimal(0)
    return dec(held["positionAmt"]) if p.get("side") == "LONG" else -dec(held["positionAmt"])


def active_hedge_qty(sym, open_pos):
    """Total unclosed, un-filled-close bot BTCUSDT LONG hedge quantity across all active hedge legs."""
    return sum((q["qty"] for q in open_pos
                if q["symbol"] == sym and q.get("side") == "LONG" and q.get("pair")
                and not q["closed"] and not (q["close"] and q["close"]["status"] == "FILLED")), Decimal(0))


def gone(p, amt):
    return amt <= 0 or bool(p["close"] and p["close"]["status"] == "FILLED")


def stuck(p, amt, open_pos=None):
    """(why, Thai line) when the bot must not close this position itself (a human checks Binance first), else None."""
    name, side_th = coin(p["symbol"]), "long" if p.get("side") == "LONG" else "short"
    if p["close"]:   # it ended without closing it all (expired / cancelled)
        return "close_incomplete", f"{name}: คำสั่งปิด {p['close']['client_order_id']} สถานะ {p['close']['status']} แต่ยังมี {side_th} {num(amt)} — ตรวจใน Binance"
    want = active_hedge_qty(p["symbol"], open_pos if open_pos is not None else positions().values()) if p.get("pair") else p["qty"]
    if amt != want or amt < p["qty"]:   # ลุงจืด resized it in the app, or it is his own position on the coin after the bot's was closed
        return "qty_mismatch", f"{name}: {side_th} บน Binance {num(amt)} ไม่เท่าที่บอทเปิด {num(want)} — บอทไม่ปิดเอง ตรวจใน Binance"
    return None


def record_gone(c, p, all_pos=None):
    """A bot position Binance no longer shows (closed by /close, liquidated or closed by hand): record it with its real PnL."""
    name = f"{coin(p['symbol'])} (คู่ {p['pair']})" if p.get("pair") else coin(p["symbol"])
    side_th = "long" if p.get("side") == "LONG" else "short"
    why = p["close"].get("why") if p["close"] else None   # None: see closed()
    r = closed(c, p, why, all_pos=all_pos)
    if not r:
        return [f"{name}: {side_th} หายจาก Binance แล้ว ({why or 'ถูกบังคับปิดหรือปิดเองในแอป'}) — รอ Binance ลงกำไรขาดทุน จะบันทึกรอบหน้า"]
    return [f"{name}: {side_th} ปิดแล้ว — {r['why']} · สุทธิ {money(dec(r['pnl']))} USDT (funding {money(dec(r['funding']))},"
            f" ค่าธรรมเนียม {money(dec(r['fees']))})"]


def cleanup(c, algos):
    """Cancel the stop-loss / take-profit orders the old exit rule placed (closePosition: one left on Binance would close
    the whole position on its coin). Ids the ledger does not know are left alone and reported."""
    pos, lines = positions(), []
    for aid, a in algos.items():
        if "bto" + aid[3:] not in pos:
            lines.append(f"{coin(a['symbol'])}: มีคำสั่ง {aid} ที่ ledger ไม่รู้จัก — ไม่ยกเลิก ตรวจใน Binance")
            continue
        try:
            c.call("DELETE", "/fapi/v1/algoOrder", signed=True, clientAlgoId=aid)
            lines.append(f"{coin(a['symbol'])}: ยกเลิก {aid} (เลิกใช้ stop/take-profit บน Binance)")
        except Exception as e:  # noqa: BLE001 — tried again next round
            lines.append(f"{coin(a['symbol'])}: ยกเลิก {aid} ไม่สำเร็จ ({str(e)[:150]})")
    return lines


def open_unclear():
    return sum(1 for p in positions().values() if not p["closed"] and p["unclear"])


def sync():
    """POST /sync, bookkeeping only (the team calls it before deciding and after an unclear answer): settle orders of
    unknown outcome, then record the bot positions Binance no longer shows. An unclear position is never recorded as
    closed: its order may still fill (a 503 in flight) while REALIZED_PNL of another trade on the coin is already there."""
    k = keys()
    if not k:
        return {"status": "refused", "lines": [f"ยังไม่ได้ใส่ Binance API key บน server ({SETUP})"], "unclear": open_unclear()}
    try:
        c = Client(k["key"], k["secret"])
        c.sync_clock()
        lines = settle(c)
        all_pos = list(positions().values())
        pos = [p for p in all_pos if not p["closed"]]
        held = on_binance(c) if pos else {}
    except Exception as e:  # noqa: BLE001 — the ledger is unchanged; the next /sync tries again
        log.exception("sync failed")
        return {"status": "error", "lines": [f"ตรวจบัญชีไม่สำเร็จ: {str(e)[:200]}"], "unclear": open_unclear()}
    for p in pos:
        if p["unclear"]:
            lines.append(f"{coin(p['symbol'])}: มีคำสั่งที่ยังไม่รู้ผล — ไม่ทำอะไรกับไม้นี้จนกว่าจะตรวจประวัติคำสั่งใน Binance ({p['id']})")
            continue
        amt = held_amt(p, held.get(p["symbol"]))
        try:
            if gone(p, amt):
                lines += record_gone(c, p, all_pos=all_pos)
            elif stuck(p, amt, pos):
                lines.append(stuck(p, amt, pos)[1])
        except Exception as e:  # noqa: BLE001 — one coin (e.g. delisted) must not stop the others
            log.exception("sync %s failed", p["id"])
            lines.append(f"{coin(p['symbol'])}: บันทึกไม้ไม่สำเร็จ ({str(e)[:150]})")
    if live():   # ponytail: kept until a read-only openAlgoOrders on the server shows no bts-/btt- order is left
        try:
            lines += cleanup(c, our_algos(c))
        except Exception as e:  # noqa: BLE001 — next round tries again
            log.exception("cleanup failed")
            lines.append(f"ยกเลิก stop/take-profit เก่าไม่สำเร็จ ({str(e)[:150]}) — ลองใหม่รอบหน้า")
    return {"status": "ok", "lines": lines, "unclear": open_unclear()}


def can_short(i):
    """Why a USDⓈ-M symbol cannot take a new market position now, or None."""
    if not i or i["status"] != "TRADING" or i.get("contractType") != "PERPETUAL" or "MARKET" not in i["orderTypes"]:
        return "เปิด position แบบ market ไม่ได้ตอนนี้"
    if filters(i).get("POSITION_RISK_CONTROL", {}).get("positionControlSide", "NONE") != "NONE":
        return "Binance จำกัดการเปิด position เหรียญนี้"
    return None


def opens_on(day):
    """Opens this server sent on UTC `day` (the date of their 'sending' line, not the run id) that Binance did not reject."""
    sent = {r["client_order_id"]: r["at"][:10] for r in ledger() if r["kind"] == "open" and r["how"] == "sending"}
    f = finals()
    return sum(1 for cid, d in sent.items() if d == day.isoformat() and f[cid]["how"] != "rejected")


def _open(c, live_, run_id, sym, qty, price, side, lev, pair=None, skip_margin=False):
    """One new position: `lev`x isolated, MARKET SELL (SHORT) or BUY (LONG). Returns reply()."""
    is_long = side == "LONG"
    side_th = "long" if is_long else "short"
    cid, name = order_id("open", run_id, sym, pair=pair), coin(sym)
    label = f"{name} (คู่ {pair})" if pair else name
    params = {"symbol": sym, "side": "BUY" if is_long else "SELL", "type": "MARKET", "quantity": num(qty),
              "newClientOrderId": cid, "newOrderRespType": "RESULT"}
    if not live_:
        c.call("POST", "/fapi/v1/order/test", signed=True, **params)
        return reply("tested", None, f"{label}: {side_th} {lev}x {num(qty)} {name} (~{money(qty * price)} USDT) Binance ตรวจคำสั่งผ่าน (ยังไม่ได้เปิดจริง)", cid)
    c.call("POST", "/fapi/v1/leverage", signed=True, symbol=sym, leverage=lev)
    if not skip_margin:
        try:
            c.call("POST", "/fapi/v1/marginType", signed=True, symbol=sym, marginType="ISOLATED")
        except BinanceError as e:
            if e.code != -4046:   # -4046 "No need to change margin type": already isolated
                raise
    extra = {**({"side": "LONG"} if is_long else {}), **({"pair": pair} if pair else {})}
    status, r, line = _send(c, run_id, sym, "open", params, **extra)
    if pair and line.startswith(f"{name}:"):
        line = f"{label}:" + line[len(name) + 1:]
    return reply(status, None, line, cid, r)


def open_one(run_id, sym, side, usdt, lev, today, pair=None, req_qty=None):
    """POST /open: one position the team decided (coin, side, USDT notional, leverage), sent only past every guard. Returns reply()."""
    cid = order_id("open", run_id, sym, pair=pair)
    try:
        return _open_one(cid, run_id, sym, side, usdt, lev, today, pair=pair, req_qty=req_qty)
    except Exception as e:  # noqa: BLE001 — the ledger decides what happens next time; never crash the service
        return _failed(cid, coin(sym), e)


def _open_one(cid, run_id, sym, side, usdt, lev, today, pair=None, req_qty=None):
    name = f"{coin(sym)} (คู่ {pair})" if pair else coin(sym)
    cfg, why, line = gate(run_id, today)
    if not cfg:
        return reply("refused", why, line, cid)
    if finals().get(cid, {}).get("how", "rejected") != "rejected":   # never resent: Binance ids are unique only among open orders
        return reply("refused", "sent", f"ข้าม {name}: เปิดของแผน {run_id} ไปแล้ว ({cid})", cid)
    pos = [p for p in positions().values() if not p["closed"]]
    if pair:
        if any(p["symbol"] == sym and not (p.get("side") == "LONG" and p.get("pair")) for p in pos):
            return reply("refused", "mine", f"ข้าม {name}: บอทถือ {coin(sym)} แบบเดี่ยวอยู่แล้ว", cid)
        active_legs = [p for p in pos if p["symbol"] == sym and p.get("side") == "LONG" and p.get("pair")
                       and not (p["close"] and p["close"]["status"] == "FILLED")]
        if any(p.get("pair") == pair for p in active_legs):
            return reply("refused", "mine", f"ข้าม {name}: บอทถือ hedge คู่นี้อยู่แล้ว", cid)
        if len(active_legs) >= MAX_HEDGE_LEGS:
            return reply("refused", "cap", f"ข้าม {name}: บอทถือ hedge BTC ครบ {MAX_HEDGE_LEGS} คู่แล้ว", cid)
    elif any(p["symbol"] == sym for p in pos):   # one-way mode merges same-coin positions: ledger qty and isolated margin would break
        return reply("refused", "mine", f"ข้าม {name}: บอทถือเหรียญนี้อยู่แล้ว", cid)
    if usdt > cfg["lot"]:
        return reply("refused", "cap", f"ข้าม {name}: ขอ {money(usdt)} USDT เกินเพดานไม้ละ {money(cfg['lot'])} USDT ของ server", cid)
    if opens_on(today) >= DAILY_OPEN_CAP:
        return reply("refused", "daily_cap", f"ข้าม {name}: วันนี้ (UTC) server เปิดครบ {DAILY_OPEN_CAP} ไม้แล้ว", cid)
    c = Client(cfg["key"], cfg["secret"])
    c.sync_clock()
    if c.call("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"]:
        return reply("refused", "hedge", "บัญชี Futures เป็น Hedge mode — บอทใช้ได้เฉพาะ One-way mode (แอป Binance: Futures → ⚙️ → Position Mode)", cid)
    if c.call("GET", "/fapi/v1/multiAssetsMargin", signed=True)["multiAssetsMargin"]:
        return reply("refused", "multi", "บัญชี Futures เปิด Multi-Assets mode — isolated ใช้ได้เฉพาะ Single-Asset mode (แอป Binance: Futures → ⚙️ → Asset Mode)", cid)
    held = on_binance(c)
    if not pair and sym in held:
        return reply("refused", "held", f"ข้าม {name}: มี position บน Binance อยู่แล้ว (ลุงจืดเปิดเอง) — ไม่ยุ่ง", cid)
    # Binance answers every MARKET order FILLED with cumQuote 0: ask again first, so one round's opens do not block each other
    journal(run_id, settle(c))
    pos_after = [p for p in positions().values() if not p["closed"]]
    bare = []
    for p in pos_after:
        if p["unclear"] and (p["symbol"] in held or (pair and p["symbol"] == sym)) and coin(p["symbol"]) not in bare:
            bare.append(coin(p["symbol"]))
    if bare:
        return reply("refused", "unclear", f"ข้าม {name}: {', '.join(bare)} มีคำสั่งที่ยังไม่รู้ผล — ไม่เปิดไม้ใหม่จนกว่าจะตรวจใน Binance", cid)
    actual_btc = Decimal(0)
    if pair:
        h_sym = held.get(sym)
        actual_btc = dec(h_sym["positionAmt"]) if h_sym else Decimal(0)
        bot_btc = active_hedge_qty(sym, pos_after)
        if actual_btc != bot_btc:
            return reply("refused", "held", f"ข้าม {name}: มี position {coin(sym)} บน Binance ({num(actual_btc)}) ไม่ตรงกับ hedge ของบอท ({num(bot_btc)}) — ไม่ยุ่ง", cid)
        if h_sym and str(h_sym.get("marginType", "isolated")).lower() != "isolated":
            return reply("refused", "held", f"ข้าม {name}: {coin(sym)} บน Binance ไม่ใช่โหมด Isolated — ไม่ยุ่ง", cid)
    if sym in {a["symbol"] for a in our_algos(c).values()}:
        return reply("refused", "stale", f"ข้าม {name}: stop/take-profit ของไม้เก่ายังค้างบน Binance — ตรวจใน Binance", cid)
    info = symbols_info(c).get(sym)
    why = can_short(info)
    if why:
        return reply("refused", "not_tradable", f"ข้าม {name}: {why}", cid)
    book = c.call("GET", "/fapi/v1/ticker/bookTicker", symbol=sym)
    bid, ask_ = dec(book["bidPrice"]), dec(book["askPrice"])
    if bid <= 0 or (ask_ - bid) / bid > MAX_SPREAD:
        return reply("refused", "spread", f"ข้าม {name}: ราคาเสนอซื้อ {num(bid)} เสนอขาย {num(ask_)} ห่างเกิน {num(MAX_SPREAD * 100)}% (สภาพคล่องบาง)", cid)
    # ponytail: sized from this book read; a MARKET order fills there or worse (leverage/marginType go first), so the
    # cap is approximate, bounded by MAX_SPREAD and the 1.01 room the team keeps per lot
    price = ask_ if side == "LONG" else bid
    step, min_qty = lot(info)
    qty = floor_to(req_qty, step) if req_qty is not None else floor_to(usdt / price, step)
    if req_qty is not None and qty != req_qty:
        return reply("refused", "min_qty", f"ข้าม {name}: จำนวน {num(req_qty)} ไม่ตรง step {num(step)} ของ Binance", cid)
    if qty * price > cfg["lot"]:
        return reply("refused", "cap", f"ข้าม {name}: มูลค่า {money(qty * price)} USDT เกินเพดานไม้ละ {money(cfg['lot'])} USDT ของ server", cid)
    if qty < min_qty or qty * price < dec(filters(info).get("MIN_NOTIONAL", {}).get("notional", "0")):
        return reply("refused", "min_qty", f"ข้าม {name}: ไม้ละ {money(usdt)} USDT ต่ำกว่าขั้นต่ำของ Binance", cid)
    return _open(c, cfg["live"], run_id, sym, qty, price, side, lev, pair=pair, skip_margin=bool(pair and actual_btc > 0))


def close_one(pid, why):
    """POST /close: the whole of one bot position at market, reduceOnly. Needs only the key (Auto Short off still lets
    the AI exit) and sends only when SHORT_LIVE=1. Returns reply()."""
    cid = sibling(pid, "close")
    try:
        return _close_one(cid, pid, why)
    except Exception as e:  # noqa: BLE001
        return _failed(cid, pid, e)


def _close_one(cid, pid, why):
    k = keys()
    if not k:
        return reply("refused", "setup", f"ยังไม่ได้ใส่ Binance API key บน server ({SETUP})", cid)
    all_pos = positions()
    p = all_pos.get(pid)
    if not p:
        return reply("refused", "unknown_position", f"ไม่ปิด {pid}: ไม่ใช่ไม้ของบอท", cid)
    name, side_th = coin(p["symbol"]), "long" if p.get("side") == "LONG" else "short"
    if p["closed"]:
        return reply("refused", "closed", f"{name}: ไม้ {pid} ปิดและบันทึกแล้ว", cid)
    if p["unclear"]:
        return reply("refused", "unclear", f"{name}: มีคำสั่งที่ยังไม่รู้ผล — ไม่ปิดจนกว่าจะตรวจประวัติคำสั่งใน Binance ({pid})", cid)
    c = Client(k["key"], k["secret"])
    c.sync_clock()
    open_pos = [q for q in all_pos.values() if not q["closed"]]
    amt = held_amt(p, on_binance(c).get(p["symbol"]))
    if gone(p, amt):
        return reply("refused", "gone", f"{name}: {side_th} ไม่อยู่บน Binance แล้ว — รอบันทึกผลตอนตรวจบัญชี (sync)", cid)
    s = stuck(p, amt, open_pos)
    if s:
        return reply("refused", s[0], s[1], cid)
    if not live():
        return reply("tested", None, f"{name}: {why} — โหมดทดสอบไม่ส่งคำสั่ง", cid)
    status, r, line = _close(c, p, why)
    return reply(status, None, f"{name}: ปิด {side_th} ({why}) → {line}", cid, r)


def spot(cfg, offset):
    s = Client(cfg["key"], cfg["secret"], SPOT)
    s.offset = offset
    return s


def spot_usdt(cfg, offset):
    """USDT still in the spot wallet: the bot only uses the futures wallet, so the app reminds ลุงจืด to transfer it."""
    return next((dec(b["free"]) for b in spot(cfg, offset).call("GET", "/api/v3/account", signed=True, omitZeroBalances="true")["balances"]
                 if b["asset"] == "USDT"), Decimal(0))


def check():
    """Server health check (setup script + the PC's `python binance.py`): Binance reachable from this IP, key rights,
    account modes, money."""
    c = Client()
    c.sync_clock()
    out = [f"server ต่อ Binance Futures ได้ (นาฬิกาต่างกัน {c.offset} ms)"]
    cfg, why = settings()
    if not cfg:
        return out + [why]
    out.append(f"ไม้ละไม่เกิน {money(cfg['lot'])} USDT (isolated, leverage ไม่เกิน {MAX_LEVERAGE}x) · เปิดได้ไม่เกิน {DAILY_OPEN_CAP} ไม้ต่อวัน (UTC) · {mode(cfg['live'])}"
               f" · บอทถือ position อยู่ {sum(1 for p in positions().values() if not p['closed'])} เหรียญ")
    if not (cfg["key"] and cfg["secret"]):
        return out + [f"ยังไม่ได้ใส่ Binance API key ({SETUP})"]
    f = Client(cfg["key"], cfg["secret"])
    f.offset = c.offset
    try:
        rights = spot(cfg, c.offset).call("GET", "/sapi/v1/account/apiRestrictions", signed=True)
        acct = f.call("GET", "/fapi/v3/account", signed=True)
        dual = f.call("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"]
        multi = f.call("GET", "/fapi/v1/multiAssetsMargin", signed=True)["multiAssetsMargin"]
        spot_free = spot_usdt(cfg, c.offset)
    except BinanceError as e:
        return out + [f"key ใช้ไม่ได้: {e} — เช็คว่า key/secret ถูก, ล็อก IP ไว้ที่ IP ของ server นี้ และเปิด Enable Futures"]
    if rights.get("enableWithdrawals"):
        out.append("⚠️ key นี้ถอนเงินได้ — ปิด Enable Withdrawals ที่ Binance ทันที")
    if not rights.get("ipRestrict"):
        out.append("⚠️ key นี้ไม่ได้ล็อก IP — ตั้ง Restrict access to trusted IPs only เป็น IP ของ server")
    if not rights.get("enableFutures"):
        out.append("key นี้ยังเทรด Futures ไม่ได้ — เปิด Enable Futures ที่ Binance (API Management → Edit restrictions)")
    if dual:
        out.append("บัญชี Futures เป็น Hedge mode — เปลี่ยนเป็น One-way mode (แอป Binance: Futures → ⚙️ → Position Mode)")
    if multi:
        out.append("บัญชี Futures เปิด Multi-Assets mode — เปลี่ยนเป็น Single-Asset mode (แอป Binance: Futures → ⚙️ → Asset Mode)")
    out.append(f"USDT ใน Futures: ว่าง {money(dec(acct['availableBalance']))} · ทั้งหมด {money(dec(acct['totalWalletBalance']))}")
    if spot_free >= 1:
        out.append(f"มี USDT ใน Spot {money(spot_free)} — บอทใช้ได้เฉพาะเงินใน USDⓈ-M Futures: โอนในแอป Binance (Assets → Transfer → Spot → USDⓈ-M Futures)")
    return out


def portfolio():
    """Read-only snapshot for the Windows app: the futures wallet, every open bot position (SHORT or LONG: entry, mark,
    PnL, liquidation price, funding so far, days held), closed positions with their real PnL and the
    latest ledger lines. Money as strings."""
    cfg, why = settings()   # Auto Short off (setup not finished) still shows the account: reading needs only the key
    k = keys()
    if not k:
        return {"error": f"ยังไม่ได้ใส่ Binance API key ({SETUP})"}
    lot = cfg["lot"] if cfg else None
    c = Client(k["key"], k["secret"])
    c.sync_clock()
    acct, held, pos = c.call("GET", "/fapi/v3/account", signed=True), on_binance(c), positions()
    all_pos = list(pos.values())
    live_pos = [p for p in all_pos if not p["closed"]]
    fund = income(c, min(opened_ms(p) for p in live_pos), kind="FUNDING_FEE") if live_pos else []
    today, shorts, done = utcnow().date(), [], []
    for p in live_pos:
        h, name, side = held.get(p["symbol"]) or {}, coin(p["symbol"]), p.get("side") or "SHORT"
        is_long = side == "LONG"
        close_filled = bool(p["close"] and p["close"]["status"] == "FILLED")
        has_pos = bool(not close_filled and h and (dec(h["positionAmt"]) > 0 if is_long else dec(h["positionAmt"]) < 0))
        if p.get("pair"):
            bot_qty = active_hedge_qty(p["symbol"], live_pos)
            pnl = (dec(h["markPrice"]) - p["entry"]) * p["qty"] if has_pos and p["entry"] and h.get("markPrice") else None
            margin = as_str(dec(h["isolatedWallet"]) * p["qty"] / bot_qty, CENT) if has_pos and bot_qty > 0 and h.get("isolatedWallet") else None
            fund_u = as_str(hedge_funding_share(p, all_pos, fund), CENT)
        else:
            pnl = dec(h["unRealizedProfit"]) if has_pos else None
            margin = h.get("isolatedWallet") if has_pos else None
            fund_u = as_str(sum((dec(r["income"]) for r in fund if r["symbol"] == p["symbol"] and r["time"] >= opened_ms(p)),
                                Decimal(0)), CENT)
        opened = datetime.fromisoformat(p["at"]).date()
        row = {"id": p["id"], "symbol": p["symbol"], "coin": name, "side": side, "qty": str(p["qty"]), "entry": as_str(p["entry"]),
               "mark": h.get("markPrice") if has_pos else None, "liq_price": h.get("liquidationPrice") if has_pos else None,
               "margin_usdt": margin,
               "pnl_usdt": as_str(pnl, CENT),
               "pnl_pct": as_str(pnl / (p["qty"] * p["entry"]) * 100, CENT) if pnl is not None and p["qty"] and p["entry"] else None,
               "funding_usdt": fund_u,
               "run_id": p["run_id"], "opened_at": p["at"], "days": (today - opened).days,
               "on_binance": has_pos, "unclear": p["unclear"]}
        if p.get("pair"):
            row["pair"] = p["pair"]
            row["entry_signal"] = f"Hedge คู่ {p['pair']} (LONG 1:1)"
        shorts.append(row)
    for p in all_pos:
        if p["closed"]:
            r = p["closed"]
            d_row = {"symbol": p["symbol"], "coin": coin(p["symbol"]), "side": p.get("side") or "SHORT",
                     "qty": str(p["qty"]), "entry": as_str(p["entry"]),
                     "opened_at": p["at"], "closed_at": r["at"], "why": r["why"], "pnl": r["pnl"], "pnl_usdt": as_str(dec(r["pnl"]), CENT),
                     "funding_usdt": as_str(dec(r["funding"]), CENT), "fees_usdt": as_str(dec(r["fees"]), CENT)}
            if p.get("pair"):
                d_row["pair"] = p["pair"]
            done.append(d_row)
    realized = as_str(sum((dec(r["pnl"]) for r in done), Decimal(0)), CENT)   # unrounded sum, rounded once (cents first drifted 3 cents)
    since = utcnow() - timedelta(days=CLOSED_DAYS)   # the team's cooldown reads these: never fewer than the last 30 days
    done = [r for i, r in enumerate(sorted(done, key=lambda r: r["closed_at"], reverse=True))
            if i < CLOSED_MIN or datetime.fromisoformat(r["closed_at"]) >= since]
    # before 2026-11-06 settle wrote found-after-error for every re-read: one whose POST was answered ('placed') had no
    # error, so it is shown as 'settled' (display only; ledger lines are never rewritten)
    answered = {r["client_order_id"] for r in ledger() if r["how"] == "placed"}
    orders = [{**{k: r.get(k) for k in ("at", "kind", "symbol", "how", "status", "qty", "quote_qty", "avg_price")},
               "side": r.get("side") or "SHORT",
               **({"how": "settled"} if r["how"] == "found-after-error" and r["client_order_id"] in answered else {})}
              for r in list(finals().values())[::-1][:30]]
    try:
        spot_free = as_str(spot_usdt(k, c.offset), CENT)
    except Exception:  # noqa: BLE001 — only a reminder
        spot_free = None
    return {"at": utcnow().isoformat(), "mode": mode(cfg["live"]) if cfg else why, "lot_usdt": as_str(lot),
            "rule": "โค้ดบนเครื่องทีม (.88) เลือกเหรียญเข้า (SHORT/LONG ตามเทรนด์) และปิดตามกติกา · AI เขียนแค่ความเห็น · server เป็นแค่ตัวส่งคำสั่งที่มีด่านกัน:"
                    f" ไม้ละไม่เกิน {money(lot) if lot else '(ยังไม่ได้ตั้ง)'} USDT (isolated ไม่เกิน {MAX_LEVERAGE}x, ราคาตลาด) · ไม่เกิน {DAILY_OPEN_CAP} ไม้ต่อวัน (UTC)"
                    f" · ไม่เปิดเหรียญที่มี position อยู่แล้ว หรือราคาซื้อ-ขายห่างเกิน {num(MAX_SPREAD * 100)}% · ปิดได้เฉพาะไม้ของบอท"
                    " · ไม่มี stop-loss/take-profit บน Binance: โค้ดตรวจกติกาออกทุกรอบ (ทุก 1 ชม.)",
            "wallet_usdt": as_str(dec(acct["totalWalletBalance"]), CENT), "available_usdt": as_str(dec(acct["availableBalance"]), CENT),
            "unrealized_usdt": as_str(dec(acct["totalUnrealizedProfit"]), CENT), "equity_usdt": as_str(dec(acct["totalMarginBalance"]), CENT),
            "spot_usdt": spot_free, "shorts": shorts, "held_symbols": sorted(held), "closed": done,
            "realized_usdt": realized, "orders": orders}


PLAN_SYMBOL = re.compile(r"[A-Z0-9\u4e00-\u9fff]{1,20}USDT")   # Chinese-named perps too (Binance, 2025-10); longer ASCII bases do not fit an order id


def run_id_of(m):
    run_id = m.get("run_id")
    if not (isinstance(run_id, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_id)):
        raise ValueError("run_id ต้องเป็นวันที่ YYYY-MM-DD")
    date.fromisoformat(run_id)   # 2026-13-45 raises here: a 400, not an error mid-order
    return run_id


def open_from(m):
    """/open body, checked at the trust boundary: (run_id, symbol, side, usdt, leverage). usdt is the notional as a string
    (money is never float); leverage an int 1..MAX_LEVERAGE, 1 when left out. Optional pair + qty are accepted only for
    paired BTCUSDT LONG hedge orders."""
    sym, side, usdt, lev = m.get("symbol"), m.get("side"), m.get("usdt"), m.get("leverage", 1)
    if not (isinstance(sym, str) and PLAN_SYMBOL.fullmatch(sym)):
        raise ValueError("symbol ต้องเป็นคู่ USDT (A-Z 0-9 หรือชื่อจีน ลงท้าย USDT)")
    if side not in ("LONG", "SHORT"):
        raise ValueError("side ต้องเป็น LONG หรือ SHORT")
    if not (isinstance(usdt, str) and re.fullmatch(r"\d{1,9}(\.\d{1,2})?", usdt) and Decimal(usdt) > 0):
        raise ValueError('usdt ต้องเป็นตัวเลข USDT แบบข้อความ เช่น "100" หรือ "50.25"')
    if type(lev) is not int or not 1 <= lev <= MAX_LEVERAGE:   # not bool, not "2", not 2.0
        raise ValueError(f"leverage ต้องเป็นเลขจำนวนเต็ม 1-{MAX_LEVERAGE}")
    pair, qty = m.get("pair"), m.get("qty")
    if pair is not None:
        if not (sym == "BTCUSDT" and side == "LONG" and isinstance(pair, str) and pair != "BTC" and PLAN_SYMBOL.fullmatch(pair + "USDT")):
            raise ValueError("pair ใช้ได้เฉพาะไม้ hedge BTCUSDT LONG คู่เหรียญที่ short")
    if qty is not None:
        if pair is None or not (isinstance(qty, str) and re.fullmatch(r"\d{1,6}(\.\d{1,6})?", qty) and Decimal(qty) > 0):
            raise ValueError('qty ใช้ได้เฉพาะไม้ hedge BTCUSDT LONG เช่น "0.001"')
    return run_id_of(m), sym, side, Decimal(usdt), lev


def close_from(m):
    """/close body: (open id of a bot position, why). The server picks side, qty and symbol from its own ledger."""
    pid, why = m.get("id"), m.get("why", "ไม่ระบุเหตุผล")   # team.py always sends the rule's why; the server cannot know one
    if not (isinstance(pid, str) and re.fullmatch(r"bto-[0-9A-Za-z-]{1,32}", pid)):
        raise ValueError("id ต้องเป็นรหัสไม้ bto-... จาก /portfolio")
    if not (isinstance(why, str) and 0 < len(why) <= 200):
        raise ValueError("why ต้องเป็นข้อความไม่เกิน 200 ตัว")
    return pid, why


def authorized(headers):
    """Only the team machine (.88, DCA_PLAN_TOKEN in its .env) and root on the server may use the service."""
    token = os.environ.get("DCA_PLAN_TOKEN") or ""
    return len(token) >= 32 and hmac.compare_digest((headers.get("X-BotTread-Token") or "").encode(), token.encode())


class _Service(http.server.BaseHTTPRequestHandler):
    timeout = 30   # a stalled client cannot hold the (single-threaded) service forever

    def do_GET(self):
        if not authorized(self.headers):
            journal("-", [f"ปฏิเสธ GET {self.path[:40]}: token ไม่ตรง"])
            return self._send(403, {"lines": ["server ไม่รับ: DCA_PLAN_TOKEN ไม่ตรง"], "error": "server ไม่รับ: DCA_PLAN_TOKEN ไม่ตรง"})
        if self.path == "/check":
            try:
                lines = check()
            except Exception as e:  # noqa: BLE001
                log.exception("check failed")
                lines = [f"ต่อ Binance จาก server ไม่ได้: {str(e)[:200]}"]
            return self._send(200, {"lines": lines})
        if self.path == "/portfolio":
            try:
                p = portfolio()
            except Exception as e:  # noqa: BLE001
                log.exception("portfolio failed")
                p = {"error": f"ดึงพอร์ตจาก Binance ไม่ได้: {str(e)[:200]}"}
            return self._send(200, {**p, "log": ledger(log_path())[-100:][::-1]})
        self._not_found()

    def do_POST(self):
        if not authorized(self.headers):
            journal("-", [f"ปฏิเสธ POST {self.path[:40]}: token ไม่ตรง"])
            return self._send(403, {"lines": ["server ไม่รับ: DCA_PLAN_TOKEN ไม่ตรง"]})
        if self.path not in ("/open", "/close", "/sync"):   # e.g. /plan from a team.py older than this server
            return self._not_found()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:   # a non-numeric header is a bad request, not a crashed handler
            n = 0
        if not 0 < n <= 65536:
            journal("-", [f"ไม่รับ {self.path}: ข้อมูลว่างหรือใหญ่เกิน"])
            return self._send(400, {"lines": ["ข้อมูลว่างหรือใหญ่เกิน"]})
        try:
            m = json.loads(self.rfile.read(n))
            args = open_from(m) if self.path == "/open" else close_from(m) if self.path == "/close" else run_id_of(m)
        except (ValueError, TypeError, AttributeError, RecursionError) as e:   # RecursionError: '[' nested thousands deep
            journal("-", [f"server ไม่รับ {self.path}: {e}"])
            return self._send(400, {"lines": [f"server ไม่รับ: {e}"]})
        # the order is sent and journaled before the reply: a client that hangs up meanwhile only loses the answer
        if self.path == "/open":
            run_id, sym, side, usdt, lev = args
            pair, req_qty = m.get("pair"), dec(m["qty"]) if m.get("qty") is not None else None
            out = open_one(run_id, sym, side, usdt, lev, utcnow().date(), pair=pair, req_qty=req_qty)
            tag = f"{coin(sym)}({pair})" if pair else coin(sym)
            journal(run_id, [f"/open {tag} {side} {lev}x {money(usdt)} USDT → {out['status']}" + (f" ({out['why']})" if out["why"] else "")
                             + f": {out['line']}"])
        elif self.path == "/close":
            out = close_one(*args)
            journal(args[0][4:14], [f"/close {args[0]} ({args[1]}) → {out['status']}" + (f" ({out['why']})" if out["why"] else "")
                                    + f": {out['line']}"])
        else:
            out = sync()
            journal(args, [f"/sync → {out['status']} · ไม่รู้ผล {out['unclear']} ไม้"] + out["lines"])
        self._send(200, out)

    def _not_found(self):
        journal("-", [f"ปฏิเสธ {self.command} {self.path[:40]}: ไม่มีหน้านี้"])
        self._send(404, {"lines": ["ไม่มีหน้านี้"]})

    def _send(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def serve(port=PORT):
    # ponytail: single-threaded on purpose — the ledger check and the POST of one request never interleave with another's
    if os.environ.get("LISTEN_FDS") == "1" and os.environ.get("LISTEN_PID") == str(os.getpid()):
        # bottread-dca.socket: systemd holds 127.0.0.1:8787 even while this restarts, so no other local user can
        # bind it and collect X-BotTread-Token; it hands the listening socket over as fd 3
        srv = http.server.HTTPServer(("127.0.0.1", port), _Service, bind_and_activate=False)
        srv.socket.close()
        srv.socket = socket.socket(fileno=3)
    else:
        srv = http.server.HTTPServer(("127.0.0.1", port), _Service)
    srv.serve_forever()


# ---------------------------------------------------------------- client side (.88 / PC): no key here, only the tunnel

def load_env(path=os.path.join(HERE, ".env")):
    """The PC's .env (DCA_PROXY_HOST, DCA_PLAN_TOKEN …) when this file runs on its own; the server has none."""
    with contextlib.suppress(FileNotFoundError), open(path, encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


@contextlib.contextmanager
def tunnel():
    """Yields a local port that reaches the server's service: `ssh -L` to DCA_PROXY_HOST, or PORT itself on the server."""
    host = os.environ.get("DCA_PROXY_HOST")
    if not host:
        yield PORT
        return
    with socket.socket() as sk:   # ponytail: free port by bind-and-release; a clash makes ssh exit -> reported below
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    key = os.path.expanduser(os.environ.get("DCA_PROXY_KEY") or "~/.ssh/bottread_proxy_ed25519")
    p = subprocess.Popen(["ssh", "-N", "-T", "-i", key, "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                          "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15",
                          "-L", f"127.0.0.1:{port}:127.0.0.1:{PORT}", f"{os.environ.get('DCA_PROXY_USER') or 'dcaproxy'}@{host}"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(60):
            if p.poll() is not None:
                raise RuntimeError("เปิดทางไป server ไม่ได้")
            try:
                socket.create_connection(("127.0.0.1", port), 1).close()
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("เปิดทางไป server ไม่สำเร็จใน 30 วินาที")
        yield port
    except Exception as e:   # the local listener can be up while the server side fails: show what ssh said
        p.kill()
        err = p.stderr.read().decode(errors="replace").strip()[-300:]
        raise RuntimeError(f"{e}" + (f" · ssh: {err}" if err else "")) from e
    finally:
        p.kill()
        p.wait()


def ask(method, path, body=None, timeout=60):
    """One request to the server's service (through the tunnel). Returns its JSON object; raises unless HTTP 200."""
    headers = {"X-BotTread-Token": os.environ.get("DCA_PLAN_TOKEN") or ""}
    if body:
        headers["Content-Type"] = "application/json"
    with tunnel() as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=headers)
            res = conn.getresponse()
            out = json.loads(res.read())
            if res.status != 200:   # 403 token, 404 an older server, 400 a bad body: never read as an answer
                raise RuntimeError(f"server ตอบ HTTP {res.status}: {' · '.join(out.get('lines') or [])[:200]}")
            return out
        finally:
            conn.close()


def post(path, payload, timeout=300):
    """One order request (/open, /close, /sync) to the server. Raises unless the answer has every field team.py reads
    (status + lines for /sync; status, why, line for /open and /close). ponytail: 300s covers Binance's 30s worst case several times; a timeout here never re-sends: the server's
    ledger holds the order and /sync tells what happened."""
    if not os.environ.get("DCA_PROXY_HOST"):
        raise RuntimeError("Auto Short ปิดอยู่ (ยังไม่ได้ตั้ง DCA_PROXY_HOST ใน .env)")
    out = ask("POST", path, json.dumps(payload), timeout=timeout)
    if any(k not in out for k in (("status", "lines") if path == "/sync" else ("status", "why", "line"))):
        raise RuntimeError(f"server ตอบ {path} ไม่ครบ (server รุ่นเก่า?)")
    return out


def fetch_portfolio():
    if not os.environ.get("DCA_PROXY_HOST"):
        return {"error": "Auto Short ปิดอยู่ (ยังไม่ได้ตั้ง DCA_PROXY_HOST ใน .env)"}
    try:
        return ask("GET", "/portfolio")
    except Exception as e:  # noqa: BLE001 — the app shows it; the last good snapshot is kept by save_portfolio
        return {"error": f"ต่อ server ไม่ได้: {str(e)[:200]}"}


def save_portfolio(p):
    """cache/portfolio.json for the app. A failed refresh keeps the last good numbers and adds the error on top."""
    if "error" in p:
        with contextlib.suppress(FileNotFoundError, ValueError), open(PORTFOLIO, encoding="utf-8") as f:
            p = {**{k: v for k, v in json.load(f).items() if k not in ("error", "error_at")}, **p}
        p["error_at"] = utcnow().isoformat()
    os.makedirs(os.path.dirname(PORTFOLIO), exist_ok=True)
    tmp = f"{PORTFOLIO}.{os.getpid()}.tmp"   # team.py and the app's 5-min refresh may write at the same moment
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
    os.replace(tmp, PORTFOLIO)
    return p


if __name__ == "__main__":   # server: `serve` | PC: `portfolio`, or nothing = health check
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        serve()
    else:
        load_env()
        if cmd == "portfolio":
            p = save_portfolio(fetch_portfolio())
            print(p.get("error") or f"Futures {p.get('wallet_usdt')} USDT · บอทถือ position อยู่ {len(p.get('shorts', []))} เหรียญ")
        else:
            print("\n".join(ask("GET", "/check")["lines"]))
