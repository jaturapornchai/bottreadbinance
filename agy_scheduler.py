"""Antigravity Dev-on-the-Fly 15-Minute Scheduler (runs on local PC in D:\\bottreadbinance).

Automatically:
  1. Wakes every 15 minutes aligned to :00:05, :15:15, :30:15, :45:15 (TH / UTC).
  2. Checks SHA256 of local `agy_trader.py` -> runs local `py_compile` -> if changed & valid,
     hot-syncs `agy_trader.py` to `$BOTTREAD_SSH_TARGET:~/bottread/agy_trader.py` via `scp`
     ("dev on the fly": edit locally, next 15m tick or `once` deploys automatically).
     If local syntax is invalid mid-edit, skips sync safely without breaking the server.
  3. Executes `python3 agy_trader.py auto-trade` on the remote trading server via `ssh`
     (where ClickHouse + Proxy SSH key + 5-second `fast-guard` live).
  4. Logs compact results to `reports/agy-scheduler.log` and `cache/agy-scheduler-state.json`.

CLI:
  .venv\\Scripts\\python.exe agy_scheduler.py start [--interval 15] [--run-immediately]
  .venv\\Scripts\\python.exe agy_scheduler.py stop
  .venv\\Scripts\\python.exe agy_scheduler.py status
  .venv\\Scripts\\python.exe agy_scheduler.py once [--dry-run] [--no-sync] [--max-opens 3]
  .venv\\Scripts\\python.exe agy_scheduler.py run [--interval 15] [--dry-run] [--no-sync]
"""
import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import py_compile
import re
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "cache")
REPORTS = os.path.join(ROOT, "reports")
TRADER_PATH = os.path.join(ROOT, "agy_trader.py")
PID_PATH = os.path.join(CACHE, "agy-scheduler.pid")
RUN_LOCK_PATH = os.path.join(CACHE, "agy-scheduler-run.lock")
STATE_PATH = os.path.join(CACHE, "agy-scheduler-state.json")
LOG_PATH = os.path.join(REPORTS, "agy-scheduler.log")
WIN_TASK_NAME = "BotTread-Agy15m"

def _load_dotenv(path=os.path.join(ROOT, ".env")):
    """Load KEY=VALUE defaults from local `.env` without overriding explicit environment variables."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    os.environ.setdefault(k, v)
    except OSError:
        pass


_load_dotenv()

SSH_TARGET = (os.environ.get("BOTTREAD_SSH_TARGET") or "user@192.0.2.10").strip()
SSH_HOST_ALIAS = (os.environ.get("BOTTREAD_SSH_ALIAS") or "").strip()
REMOTE_DIR = "~/bottread"
REMOTE_TRADER = f"{REMOTE_DIR}/agy_trader.py"
DEFAULT_INTERVAL_MIN = 15
CLICKHOUSE_SETTLE_MIN = 5              # Run at :05, :20, :35, :50 so ClickHouse finishes ingesting 15m bars first
DEFAULT_MAX_OPENS = 0                  # 0 = unlimited opens per hour/round (up to MAX_OPEN=8)
MAX_LOG_LINES = 2000
MAX_RECENT_ROUNDS = 20
TH = dt.timezone(dt.timedelta(hours=7))

_NON_LAN_IPV4_RE = re.compile(r"(?<![\d.])(?!192\.168\.)(?!127\.0\.0\.)(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_STOP_REQUESTED = False


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def fmt_th(t):
    if t is None:
        return None
    return f"{t.astimezone(TH):%Y-%m-%d %H:%M:%S}"


def scrub_text(text):
    return _NON_LAN_IPV4_RE.sub("[REDACTED_IP]", str(text or ""))


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_log(line):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    clean = scrub_text(line).rstrip("\r\n")
    try:
        lines = []
        if os.path.exists(LOG_PATH):
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        lines.append(clean)
        if len(lines) > MAX_LOG_LINES:
            lines = lines[-MAX_LOG_LINES:]
        tmp = f"{LOG_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, LOG_PATH)
    except OSError:
        pass


def next_slot_utc(now_utc, interval_min=DEFAULT_INTERVAL_MIN):
    """Return the next UTC wall-clock slot aligned to `interval_min` + `CLICKHOUSE_SETTLE_MIN` (:05, :20, :35, :50)."""
    iv = max(1, min(60, int(interval_min)))
    settle = CLICKHOUSE_SETTLE_MIN if iv >= 10 else 0
    now_u = now_utc.astimezone(dt.timezone.utc)
    slot_base = (now_u.minute // iv) * iv
    cand = now_u.replace(minute=slot_base, second=0, microsecond=0) + dt.timedelta(minutes=settle)
    if cand <= now_u:
        cand = cand + dt.timedelta(minutes=iv)
    return cand


def is_pid_running(pid):
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_i <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_i)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok and code.value == STILL_ACTIVE)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid_i, 0)
        return True
    except OSError:
        return False


def _ssh_base():
    cmd = ["ssh", "-o", "BatchMode=yes"]
    if SSH_HOST_ALIAS:
        cmd += ["-o", f"HostKeyAlias={SSH_HOST_ALIAS}"]
    cmd += ["-o", "ConnectTimeout=15", SSH_TARGET]
    return cmd


def _scp_base():
    cmd = ["scp", "-o", "BatchMode=yes"]
    if SSH_HOST_ALIAS:
        cmd += ["-o", f"HostKeyAlias={SSH_HOST_ALIAS}"]
    cmd += ["-o", "ConnectTimeout=15"]
    return cmd


def _creationflags_hidden():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def sync_trader_if_changed(state, force=False):
    """Hot-sync local `agy_trader.py` to server when SHA256 changed and local syntax compiles cleanly."""
    if not os.path.isfile(TRADER_PATH):
        return {"synced": False, "status": "missing_local", "error": "ไม่พบ agy_trader.py"}
    local_sha = file_sha256(TRADER_PATH)
    prev_sha = (state or {}).get("last_synced_sha256")
    if not force and prev_sha == local_sha:
        return {"synced": False, "status": "unchanged", "sha256": local_sha}

    try:
        py_compile.compile(TRADER_PATH, doraise=True)
    except Exception as e:  # noqa: BLE001
        return {"synced": False, "status": "syntax_error", "sha256": local_sha, "error": scrub_text(e)[:200]}

    src_posix = TRADER_PATH.replace("\\", "/")
    try:
        cp = subprocess.run(
            [*_scp_base(), src_posix, f"{SSH_TARGET}:{REMOTE_TRADER}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_creationflags_hidden(),
        )
        if cp.returncode != 0:
            err = scrub_text((cp.stderr or cp.stdout or "").strip())[:240]
            return {"synced": False, "status": "scp_failed", "sha256": local_sha, "error": err}
        chk = subprocess.run(
            [*_ssh_base(), f"cd {REMOTE_DIR} && /usr/bin/python3 -m py_compile agy_trader.py && sha256sum agy_trader.py"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
            creationflags=_creationflags_hidden(),
        )
        if chk.returncode != 0:
            err = scrub_text((chk.stderr or chk.stdout or "").strip())[:240]
            return {"synced": False, "status": "remote_compile_failed", "sha256": local_sha, "error": err}
        remote_sha = (chk.stdout.strip().split() or [""])[0].lower()
        if remote_sha != local_sha.lower():
            return {"synced": False, "status": "sha_mismatch", "sha256": local_sha, "remote_sha256": remote_sha}
    except Exception as e:  # noqa: BLE001
        return {"synced": False, "status": "sync_exception", "sha256": local_sha, "error": scrub_text(e)[:240]}

    return {"synced": True, "status": "synced", "sha256": local_sha}


def run_remote_autotrade(max_opens=DEFAULT_MAX_OPENS, dry_run=False):
    """Execute one `agy_trader.py auto-trade` round on the remote trading server and parse its JSON output."""
    dry_flag = " --dry-run" if dry_run else ""
    remote_cmd = (
        f"cd {REMOTE_DIR} && /usr/bin/python3 agy_trader.py auto-trade --max-opens {int(max_opens)}{dry_flag}"
    )
    if not dry_run:
        remote_cmd += f" | tee -a {REMOTE_DIR}/reports/agy-autotrade.log"
    try:
        cp = subprocess.run(
            [*_ssh_base(), remote_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            creationflags=_creationflags_hidden(),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": scrub_text(e)[:240]}

    stdout = (cp.stdout or "").strip()
    stderr = scrub_text((cp.stderr or "").strip())
    parsed = None
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                break
            except ValueError:
                pass
    if parsed is not None:
        return parsed
    return {
        "ok": cp.returncode == 0,
        "rc": cp.returncode,
        "raw": scrub_text(stdout[-300:]),
        **({"error": stderr[:240]} if stderr else {}),
    }


def _acquire_run_lock(stale_sec=360):
    """Best-effort atomic lock file preventing overlapping `once` invocations."""
    os.makedirs(os.path.dirname(RUN_LOCK_PATH), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(RUN_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "at_th": fmt_th(utcnow())}, f)
            return True
        except FileExistsError:
            rec = read_json(RUN_LOCK_PATH, default={})
            holder = rec.get("pid")
            try:
                age = time.time() - os.path.getmtime(RUN_LOCK_PATH)
            except OSError:
                age = stale_sec + 1
            if (holder and not is_pid_running(holder)) or age > stale_sec:
                try:
                    os.remove(RUN_LOCK_PATH)
                except OSError:
                    return False
                continue
            return False
        except OSError:
            return False
    return False


def _release_run_lock():
    rec = read_json(RUN_LOCK_PATH, default={})
    if rec.get("pid") == os.getpid():
        try:
            os.remove(RUN_LOCK_PATH)
        except OSError:
            pass


def compute_align_delay_sec(now_utc):
    """Return seconds to wait if triggered right on a 15m boundary (:00/:15/:30/:45) before ClickHouse settles; 0 at :05/:20/:35/:50."""
    now_u = now_utc.astimezone(dt.timezone.utc)
    rem_min = now_u.minute % 15
    if rem_min == CLICKHOUSE_SETTLE_MIN:
        return 0.0
    if rem_min == 0:
        cur_sec = now_u.second + now_u.microsecond / 1_000_000.0
        return max(0.0, float(CLICKHOUSE_SETTLE_MIN * 60) - cur_sec)
    return 0.0


def execute_round(state, *, max_opens=DEFAULT_MAX_OPENS, dry_run=False, auto_sync=True, force_sync=False):
    """Run one complete Dev-on-the-Fly tick: optional hot-sync + remote auto-trade + state/log persistence."""
    if not _acquire_run_lock():
        return dict(state or {}), {
            "sync": {"synced": False, "status": "lock_busy"},
            "trade": {"ok": True, "skipped": "lock_busy"},
            "summary": {"at_th": fmt_th(utcnow()), "ok": True, "skipped": "lock_busy"},
        }
    try:
        return _execute_round_unlocked(
            state,
            max_opens=max_opens,
            dry_run=dry_run,
            auto_sync=auto_sync,
            force_sync=force_sync,
        )
    finally:
        _release_run_lock()


def _execute_round_unlocked(state, *, max_opens=DEFAULT_MAX_OPENS, dry_run=False, auto_sync=True, force_sync=False):
    now_u = utcnow()
    st = dict(state or {})
    sync_res = {"synced": False, "status": "skipped"}
    if auto_sync:
        sync_res = sync_trader_if_changed(st, force=force_sync)
        st["last_sync_status"] = sync_res.get("status")
        if sync_res.get("synced"):
            st["last_synced_sha256"] = sync_res["sha256"]
            st["last_sync_at_th"] = fmt_th(now_u)
            append_log(f"[{fmt_th(now_u)}] SYNC_OK sha256={sync_res['sha256'][:12]}")
        elif sync_res.get("status") not in ("unchanged", "skipped"):
            append_log(f"[{fmt_th(now_u)}] SYNC_{sync_res.get('status', 'ERR').upper()} err={sync_res.get('error')}")

    trade_res = run_remote_autotrade(max_opens=max_opens, dry_run=dry_run)
    ok = bool(trade_res.get("ok"))
    st["last_run_at_th"] = fmt_th(utcnow())
    if ok:
        st["rounds_ok"] = int(st.get("rounds_ok") or 0) + 1
        st["last_error"] = None
    else:
        st["rounds_err"] = int(st.get("rounds_err") or 0) + 1
        st["last_error"] = scrub_text(trade_res.get("error") or trade_res.get("raw") or "unknown_error")[:240]

    compact_round = {
        "at_th": trade_res.get("at_th") or st["last_run_at_th"],
        "ok": ok,
        "dry_run": bool(dry_run),
        "sync": sync_res.get("status"),
        "strategy": trade_res.get("strategy"),
        "regime": trade_res.get("regime"),
        "cands": trade_res.get("candidates_found"),
        "opened": len([x for x in (trade_res.get("opened") or []) if x.get("ok") or x.get("dry_run")]),
        "closed": len([x for x in (trade_res.get("closed") or []) if x.get("ok") or x.get("dry_run")]),
        "equity_usdt": trade_res.get("equity_usdt"),
        "open_count": trade_res.get("open_count"),
        **({"error": st["last_error"]} if not ok else {}),
    }
    st["last_result"] = compact_round
    recent = list(st.get("recent_rounds") or [])
    recent.append(compact_round)
    st["recent_rounds"] = recent[-MAX_RECENT_ROUNDS:]
    write_json_atomic(STATE_PATH, st)
    append_log(f"[{compact_round['at_th']}] ROUND {json.dumps(compact_round, ensure_ascii=False, separators=(',', ':'))}")
    return st, {"sync": sync_res, "trade": trade_res, "summary": compact_round}


def _handle_sig(_signum, _frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def _write_pid_file(interval_min):
    write_json_atomic(PID_PATH, {
        "pid": os.getpid(),
        "started_at_th": fmt_th(utcnow()),
        "interval_min": int(interval_min),
        "python": sys.executable,
    })


def _clear_pid_file_if_mine():
    rec = read_json(PID_PATH, default={})
    if rec.get("pid") == os.getpid():
        try:
            os.remove(PID_PATH)
        except OSError:
            pass


def cmd_run(args):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    signal.signal(signal.SIGINT, _handle_sig)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sig)

    existing = read_json(PID_PATH, default={})
    if existing.get("pid") and existing.get("pid") != os.getpid() and is_pid_running(existing.get("pid")):
        print(json.dumps({"ok": False, "error": f"scheduler กำลังรันอยู่แล้ว (PID {existing['pid']})"}, ensure_ascii=False))
        return 2

    iv = max(1, min(60, int(args.interval)))
    _write_pid_file(iv)
    st = read_json(STATE_PATH, default={})
    st["status"] = "running"
    st["pid"] = os.getpid()
    st["started_at_th"] = fmt_th(utcnow())
    st["interval_min"] = iv
    st["auto_sync"] = not args.no_sync
    st["dry_run"] = bool(args.dry_run)
    write_json_atomic(STATE_PATH, st)
    append_log(f"[{fmt_th(utcnow())}] SCHEDULER_START pid={os.getpid()} interval={iv}m auto_sync={not args.no_sync} dry_run={args.dry_run}")

    try:
        if args.run_immediately:
            st, _ = execute_round(st, max_opens=args.max_opens, dry_run=args.dry_run, auto_sync=(not args.no_sync))

        while not _STOP_REQUESTED:
            nxt = next_slot_utc(utcnow(), interval_min=iv)
            st["next_run_at_th"] = fmt_th(nxt)
            st["status"] = "running"
            st["pid"] = os.getpid()
            write_json_atomic(STATE_PATH, st)

            while not _STOP_REQUESTED:
                rem = (nxt - utcnow()).total_seconds()
                if rem <= 0:
                    break
                time.sleep(min(1.0, rem))

            if _STOP_REQUESTED:
                break

            st, _ = execute_round(st, max_opens=args.max_opens, dry_run=args.dry_run, auto_sync=(not args.no_sync))
    finally:
        st = read_json(STATE_PATH, default=st)
        if st.get("pid") == os.getpid():
            st["status"] = "stopped"
            st["stopped_at_th"] = fmt_th(utcnow())
            st["next_run_at_th"] = None
            write_json_atomic(STATE_PATH, st)
        append_log(f"[{fmt_th(utcnow())}] SCHEDULER_STOP pid={os.getpid()}")
        _clear_pid_file_if_mine()
    return 0


def query_win_task(task_name=WIN_TASK_NAME):
    """Inspect Windows Scheduled Task status via `schtasks` (returns dict; safe on non-Windows)."""
    if os.name != "nt":
        return {"installed": False, "name": task_name}
    try:
        cp = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "CSV"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=_creationflags_hidden(),
        )
        if cp.returncode != 0:
            return {"installed": False, "name": task_name}
        import csv
        import io
        rows = list(csv.DictReader(io.StringIO(cp.stdout)))
        if not rows:
            return {"installed": True, "name": task_name}
        r0 = rows[0]
        return {
            "installed": True,
            "name": task_name,
            "status": r0.get("Status") or r0.get("Scheduled Task State"),
            "next_run": r0.get("Next Run Time"),
            "last_run": r0.get("Last Run Time"),
            "last_result": r0.get("Last Result"),
        }
    except Exception as e:  # noqa: BLE001
        return {"installed": False, "name": task_name, "error": scrub_text(e)[:120]}


def cmd_install_task(args):
    if os.name != "nt":
        print(json.dumps({"ok": False, "error": "install-task ใช้ได้เฉพาะบน Windows"}, ensure_ascii=False))
        return 1
    iv = max(1, min(60, int(args.interval)))
    py_exe = sys.executable
    pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
    if os.path.isfile(pyw):
        py_exe = pyw
    script = os.path.abspath(__file__)
    dry_s = " --dry-run" if args.dry_run else ""
    nosync_s = " --no-sync" if args.no_sync else ""
    tr = f'"{py_exe}" "{script}" once --align-delay --max-opens {int(args.max_opens)}{dry_s}{nosync_s}'
    cmd = [
        "schtasks", "/Create",
        "/TN", WIN_TASK_NAME,
        "/TR", tr,
        "/SC", "MINUTE",
        "/MO", str(iv),
        "/ST", f"00:{CLICKHOUSE_SETTLE_MIN:02d}" if iv >= 10 else "00:00",
        "/F",
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=_creationflags_hidden())
    if cp.returncode != 0:
        err = scrub_text((cp.stderr or cp.stdout or "").strip())[:240]
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return 1
    st = read_json(STATE_PATH, default={})
    st["status"] = "scheduled_task"
    st["interval_min"] = iv
    st["task_name"] = WIN_TASK_NAME
    st["auto_sync"] = not args.no_sync
    st["dry_run"] = bool(args.dry_run)
    write_json_atomic(STATE_PATH, st)
    append_log(f"[{fmt_th(utcnow())}] WIN_TASK_INSTALLED name={WIN_TASK_NAME} interval={iv}m")
    info = query_win_task(WIN_TASK_NAME)
    print(json.dumps({"ok": True, "task": info, "tr": tr}, ensure_ascii=False, indent=2))
    return 0


def cmd_uninstall_task(_args):
    if os.name != "nt":
        print(json.dumps({"ok": False, "error": "uninstall-task ใช้ได้เฉพาะบน Windows"}, ensure_ascii=False))
        return 1
    cp = subprocess.run(["schtasks", "/Delete", "/TN", WIN_TASK_NAME, "/F"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=_creationflags_hidden())
    ok = cp.returncode == 0
    if ok:
        append_log(f"[{fmt_th(utcnow())}] WIN_TASK_REMOVED name={WIN_TASK_NAME}")
    print(json.dumps({"ok": ok, "name": WIN_TASK_NAME, "msg": scrub_text((cp.stdout or cp.stderr or "").strip())[:200]}, ensure_ascii=False))
    return 0 if ok else 1


def cmd_once(args):
    if getattr(args, "align_delay", False):
        wait_s = compute_align_delay_sec(utcnow())
        if wait_s > 0:
            time.sleep(wait_s)
    st = read_json(STATE_PATH, default={})
    _, res = execute_round(
        st,
        max_opens=args.max_opens,
        dry_run=args.dry_run,
        auto_sync=(not args.no_sync),
        force_sync=args.force_sync,
    )
    print(json.dumps({"ok": bool(res["trade"].get("ok")), **res["summary"], "sync_detail": res["sync"]}, ensure_ascii=False))
    return 0 if res["trade"].get("ok") else 1


def cmd_start(args):
    existing = read_json(PID_PATH, default={})
    if existing.get("pid") and is_pid_running(existing.get("pid")):
        st = read_json(STATE_PATH, default={})
        print(json.dumps({
            "ok": True,
            "already_running": True,
            "pid": existing["pid"],
            "interval_min": existing.get("interval_min"),
            "next_run_at_th": st.get("next_run_at_th"),
        }, ensure_ascii=False))
        return 0

    py_exe = sys.executable
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(py_exe), "pythonw.exe")
        if os.path.isfile(pyw):
            py_exe = pyw

    cmd = [py_exe, os.path.abspath(__file__), "run", "--interval", str(args.interval), "--max-opens", str(args.max_opens)]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.no_sync:
        cmd.append("--no-sync")
    if args.run_immediately:
        cmd.append("--run-immediately")

    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=(os.name != "nt"),
    )

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        rec = read_json(PID_PATH, default={})
        real_pid = rec.get("pid")
        if real_pid and is_pid_running(real_pid):
            st = read_json(STATE_PATH, default={})
            print(json.dumps({
                "ok": True,
                "started": True,
                "pid": real_pid,
                "interval_min": args.interval,
                "next_run_at_th": st.get("next_run_at_th"),
                "log_path": LOG_PATH,
            }, ensure_ascii=False))
            return 0
        time.sleep(0.2)

    alive = is_pid_running(proc.pid)
    print(json.dumps({"ok": alive, "pid": proc.pid, "warning": "started_but_pidfile_slow"}, ensure_ascii=False))
    return 0 if alive else 1


def cmd_stop(_args):
    rec = read_json(PID_PATH, default={})
    pid = rec.get("pid")
    if not pid or not is_pid_running(pid):
        try:
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except OSError:
            pass
        st = read_json(STATE_PATH, default={})
        st["status"] = "stopped"
        st["next_run_at_th"] = None
        write_json_atomic(STATE_PATH, st)
        print(json.dumps({"ok": True, "stopped": False, "note": "scheduler ไม่ได้รันอยู่"}, ensure_ascii=False))
        return 0

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=_creationflags_hidden())
    else:
        os.kill(int(pid), signal.SIGTERM)

    time.sleep(0.5)
    still = is_pid_running(pid)
    if not still:
        try:
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except OSError:
            pass
        st = read_json(STATE_PATH, default={})
        st["status"] = "stopped"
        st["stopped_at_th"] = fmt_th(utcnow())
        st["next_run_at_th"] = None
        write_json_atomic(STATE_PATH, st)
        append_log(f"[{fmt_th(utcnow())}] SCHEDULER_STOPPED_BY_CMD pid={pid}")
    print(json.dumps({"ok": not still, "stopped": not still, "pid": pid}, ensure_ascii=False))
    return 0 if not still else 1


def cmd_status(_args):
    rec = read_json(PID_PATH, default={})
    st = read_json(STATE_PATH, default={})
    pid = rec.get("pid") or st.get("pid")
    running = is_pid_running(pid)
    task_info = query_win_task(WIN_TASK_NAME)
    local_sha = file_sha256(TRADER_PATH) if os.path.isfile(TRADER_PATH) else None
    synced_sha = st.get("last_synced_sha256")
    out = {
        "ok": True,
        "active": bool(running or task_info.get("installed")),
        "daemon_running": running,
        "pid": pid if running else None,
        "win_task": task_info,
        "interval_min": rec.get("interval_min") or st.get("interval_min") or DEFAULT_INTERVAL_MIN,
        "next_run_at_th": (st.get("next_run_at_th") if running else task_info.get("next_run")),
        "last_run_at_th": st.get("last_run_at_th"),
        "local_sha256": local_sha[:12] if local_sha else None,
        "synced_sha256": synced_sha[:12] if synced_sha else None,
        "pending_sync": bool(local_sha and synced_sha and local_sha != synced_sha),
        "rounds_ok": int(st.get("rounds_ok") or 0),
        "rounds_err": int(st.get("rounds_err") or 0),
        "last_error": st.get("last_error"),
        "recent_rounds": (st.get("recent_rounds") or [])[-5:],
        "log_path": LOG_PATH,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Antigravity Dev-on-the-Fly 15-Minute Scheduler (PC -> remote trading server)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_inst = sub.add_parser("install-task", help="Register persistent Windows Scheduled Task (every 15m, survives reboot)")
    p_inst.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN)
    p_inst.add_argument("--max-opens", type=int, default=DEFAULT_MAX_OPENS)
    p_inst.add_argument("--dry-run", action="store_true")
    p_inst.add_argument("--no-sync", action="store_true")

    sub.add_parser("uninstall-task", help="Remove Windows Scheduled Task")

    p_run = sub.add_parser("run", help="Run 15-minute scheduler loop in foreground")
    p_run.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN)
    p_run.add_argument("--max-opens", type=int, default=DEFAULT_MAX_OPENS)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--no-sync", action="store_true")
    p_run.add_argument("--run-immediately", action="store_true")

    p_start = sub.add_parser("start", help="Start detached background 15-minute scheduler on Windows")
    p_start.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN)
    p_start.add_argument("--max-opens", type=int, default=DEFAULT_MAX_OPENS)
    p_start.add_argument("--dry-run", action="store_true")
    p_start.add_argument("--no-sync", action="store_true")
    p_start.add_argument("--run-immediately", action="store_true")

    sub.add_parser("stop", help="Stop running background scheduler")
    sub.add_parser("status", help="Show scheduler status, next run, sync hash, and recent rounds")

    p_once = sub.add_parser("once", help="Sync agy_trader.py (if changed) and run 1 round immediately")
    p_once.add_argument("--max-opens", type=int, default=DEFAULT_MAX_OPENS)
    p_once.add_argument("--dry-run", action="store_true")
    p_once.add_argument("--no-sync", action="store_true")
    p_once.add_argument("--force-sync", action="store_true")
    p_once.add_argument("--align-delay", action="store_true")

    args = p.parse_args(argv)
    if args.cmd == "install-task":
        return cmd_install_task(args)
    if args.cmd == "uninstall-task":
        return cmd_uninstall_task(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "once":
        return cmd_once(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
