"""Server-side scheduler that replaced the Windows app's timers (runs in the `team` container, see docker-compose.yml):
- a round (`team.py once`) at the top of the hour after the last one started (cache/team.pid mtime), 10 min after a failed start;
- `binance.py portfolio` every minute (cache/portfolio.json);
- the console's buttons: an empty file in cache/requests/ (run / stop / portfolio / kafka), deleted once handled;
- the console's ปิดไม้ button: cache/requests/close-<position id> -> `team.py close <id>...` within one tick, beside a round too (`close-in-round`);
- `team.py watch` (the exit rule alone) once per quarter hour, only between rounds and hand closes;
- cache/runner.json every 5 s = what the console shows (team state, countdown, last action)."""
import datetime as dt
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
REQUESTS = os.path.join(CACHE, "requests")
PID_FILE = os.path.join(CACHE, "team.pid")
ROUND, RETRY, PORTFOLIO_EVERY = dt.timedelta(minutes=60), dt.timedelta(minutes=10), 60
CLOSE = re.compile(r"close-(bto-[0-9A-Za-z-]{1,32})")   # = the position id binance.py /close accepts


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def next_auto(pid_mtime, last_attempt):
    """UTC time of the next automatic round: the top of the hour after the last round started (None = never ran = due now;
    ลุงจืด 2026-10-18: rounds start at minute 0), 10 min after the last attempt (a failed start or a round stopped by hand
    waits out its hour)."""
    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    hour = pid_mtime.replace(minute=0, second=0, microsecond=0) + ROUND if pid_mtime else epoch
    retry = last_attempt + RETRY if last_attempt else epoch
    return max(hour, retry)


def team_pid():
    """pid of a live `team.py` (cache/team.pid), else None; the file outlives every round."""
    try:
        pid = int(open(PID_FILE).read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return pid if b"team.py" in f.read() else None
    except (OSError, ValueError):
        return None


def pid_mtime():
    try:
        return dt.datetime.fromtimestamp(os.path.getmtime(PID_FILE), dt.timezone.utc)
    except OSError:
        return None


def kafka_up():
    host, _, port = (os.environ.get("KAFKA_BOOTSTRAP") or "127.0.0.1:19092").split(",")[0].rpartition(":")
    try:
        socket.create_connection((host, int(port)), 1).close()
        return True
    except OSError:
        return False


def take(name):
    """True once per console request file."""
    try:
        os.remove(os.path.join(REQUESTS, name))
        return True
    except OSError:
        return False


def close_requests():
    """The console's ปิดไม้ files (cache/requests/close-<id>); [] when the folder is missing (take() never raised either)."""
    try:
        return sorted(n for n in os.listdir(REQUESTS) if CLOSE.fullmatch(n))
    except OSError:
        return []


def iso(t):
    return t.isoformat() if t else None


def watch_slot(now):
    """The quarter hour whose exit watch is due: 1 min in, so Binance has that quarter's first trades."""
    t = now - dt.timedelta(minutes=1)
    return t.replace(minute=t.minute // 15 * 15, second=0, microsecond=0)


class Runner:
    def __init__(self):
        self.team = self.pf = self.kf = self.cl = self.wt = None   # Popen of the running team / portfolio / kafka tail / hand close / exit watch
        self.wt_slot = None   # the quarter hour the last exit watch covered
        self.last_attempt = None
        self.pf_at = self.kf_at = None
        self.status = "ตัวจัดรอบเริ่มทำงาน"

    def py(self, *args, session=False):
        return subprocess.Popen([sys.executable, *args], cwd=ROOT, stdin=subprocess.DEVNULL, start_new_session=session)

    def start_round(self, auto):
        self.last_attempt = utcnow()
        if not kafka_up():
            self.status = "เริ่มรอบไม่ได้: Redpanda (Kafka) ไม่ขึ้น — ลองใหม่ใน 10 นาที"
            return
        self.team = self.py("team.py", "once", session=True)   # own process group: stop kills its Chromium children too
        self.status = "เริ่มรอบอัตโนมัติ (ทุก 1 ชม.)" if auto else "เริ่มรอบใหม่ (กดจาก console)"

    def stop_round(self):
        pid = (self.team.pid if self.team and self.team.poll() is None else None) or team_pid()
        if not pid:
            self.status = "ไม่มีรอบที่กำลังทำงาน"
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.status = "หยุดรอบกลางคันแล้ว — รอบถัดไปเริ่มตามเวลาเดิม"

    def tick(self):
        now = utcnow()
        for p in (self.pf, self.kf):
            if p:
                p.poll()   # reap a finished child
        if self.cl and self.cl.poll() is not None:
            self.cl, self.status = None, "ทำคำสั่งปิดไม้ที่กดจาก console เสร็จ — ผลอยู่ในแชตทีมและแท็บพอร์ต"
        running = (self.team is not None and self.team.poll() is None) or team_pid() is not None
        if take("stop"):
            self.stop_round()
            running = False
        watching = self.wt is not None and self.wt.poll() is None   # it may be closing a position: nothing else that closes starts beside it
        # beside a round too (ลุงจืด 2026-11-11 "กดปุ่มปิดไม้ไม่ทำงาน": a press at 22:02 waited 6 min for the round to end). Safe: the
        # order server takes one request at a time, closes reduce-only and refuses an id that is already closed or unclear.
        if not self.cl and not watching:
            names = close_requests()
            if names:
                self.cl = self.py("team.py", "close-in-round" if running else "close", *(CLOSE.fullmatch(n)[1] for n in names))
                for n in names:   # only once it started: a failed start keeps the press for the next tick
                    take(n)
                self.status = f"กำลังปิด {len(names)} ไม้ที่กดจาก console"
        asked = not self.cl and not watching and take("run")   # taken even while a round runs: the button means "now", not "after this round"
        due = now >= next_auto(pid_mtime(), self.last_attempt)
        if not running and not self.cl and not watching and (asked or due):
            self.start_round(auto=due)
            running = self.team is not None and self.team.poll() is None
        slot = watch_slot(now)
        if slot != self.wt_slot and not running and not self.cl and not watching:   # a slot missed behind a round runs right after it
            self.wt, self.wt_slot, watching = self.py("team.py", "watch"), slot, True
        for name, attr, every in (("portfolio", "pf", PORTFOLIO_EVERY), ("kafka", "kf", None)):
            proc, at = getattr(self, attr), getattr(self, attr + "_at")
            go = take(name) or (every and (at is None or (now - at).total_seconds() >= every))
            if go and (proc is None or proc.returncode is not None):   # never two at once
                setattr(self, attr, self.py("binance.py", "portfolio") if name == "portfolio" else self.py("bus.py", "tail"))
                setattr(self, attr + "_at", now)
        nxt = next_auto(pid_mtime(), self.last_attempt)
        state = {"updated": iso(now), "team_pid": team_pid(), "team_running": running, "closing": self.cl is not None or watching, "next_auto": iso(nxt),
                 "wait_s": 600 if self.last_attempt and nxt == self.last_attempt + RETRY else 3600,
                 "kafka_up": kafka_up(), "status": self.status, "portfolio_at": iso(self.pf_at), "kafka_at": iso(self.kf_at)}
        tmp = os.path.join(CACHE, "runner.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(CACHE, "runner.json"))

def main():
    os.makedirs(REQUESTS, exist_ok=True)
    r = Runner()
    while True:
        try:
            r.tick()
        except Exception as e:  # noqa: BLE001 — the scheduler must outlive any one bad tick
            r.status = f"ตัวจัดรอบผิดพลาด: {e}"
            print(r.status, flush=True)
        for _ in range(5):   # a pressed ปิดไม้ starts within a second, not at the next 5 s tick (ลุงจืด 2026-11-12 "กดปิดไม้ ปิดทันที")
            time.sleep(1)
            if close_requests():
                break


if __name__ == "__main__":
    main()
