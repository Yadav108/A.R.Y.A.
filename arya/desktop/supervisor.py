"""
Keeps ARYA running (roadmap C5): the desktop shortcut and "Start with Windows" start this small watchdog,
which starts ARYA itself as a child process and

  - restarts it if it crashes (any exit except your own "Quit ARYA"), noticed within seconds; that includes
    its server failing to start while the window stays open (the child then exits with SERVER_STOPPED),
    and a start that failed early waits 30 s before the next try (usually memory),
  - restarts it if its server stops answering for 3 minutes while the window lives on (a hung model or
    GPU driver), checked once a minute at http://127.0.0.1:<port>/healthz,
  - after 5 restarts in 30 minutes it pauses for 15 minutes instead of looping (memory or a driver usually
    recovers by then) and tries again, so a bad hour never leaves ARYA off for good; every restart is logged.

"Quit ARYA" exits normally (code 0) and the watchdog quits with it. ARYA_SUPERVISE=0 turns it off.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "arya_desktop.py"
POLL_S = 2                       # has ARYA exited? (cheap — noticed within seconds)
CHECK_EVERY_S = 60               # does its server answer?
STARTUP_GRACE_S = 180            # models take a while to load
HUNG_AFTER_FAILS = 3             # 3 missed checks in a row -> hung
MAX_RESTARTS, WINDOW_S = 5, 30 * 60
BACKOFF_S = 5
COOLDOWN_S = 15 * 60             # after too many restarts: pause, then try again (not give up for good)
# A start that failed within the grace period was usually short of memory (2026-11-16: "mkl_malloc: failed
# to allocate memory" while another model was still loaded) — give memory time to free up first.
FAILED_START_BACKOFF_S = 30


def _log(text: str) -> None:
    try:
        from arya.knowledge.library import DATA_DIR
        log = DATA_DIR / "logs" / "supervisor.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text + "\n")
    except Exception:
        pass


def healthy(port: int, timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


SERVER_STOPPED = 3               # exit code of a child whose server died while its window lived on
ENV_FLAG = "ARYA_SUPERVISED"     # set for the child: "exit when your server stops, I'll restart you"


def _spawn(cmd: list[str], cwd: str | None = None):
    return subprocess.Popen(cmd, cwd=cwd, env={**os.environ, ENV_FLAG: "1"})


def _child_command(args: list[str]) -> list[str]:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else exe), str(LAUNCHER), "--child", *args]


def supervise(args: list[str], *, spawn=_spawn, check=healthy, sleep=time.sleep, clock=time.monotonic,
              port: int | None = None, max_cooldowns: int | None = None) -> int:
    """Run ARYA and keep it running. Returns the exit code to leave with."""
    port = port or int(os.getenv("ARYA_PORT", "8000"))
    restarts: deque[float] = deque()
    cooldowns = 0
    while True:
        started = clock()
        child = spawn(_child_command(args), cwd=str(ROOT))
        _log(f"started ARYA (pid {getattr(child, 'pid', '?')})")
        fails, reason, last_check = 0, "", started
        while True:
            code = child.poll()
            if code is not None:
                if code == 0:
                    _log("ARYA quit normally")
                    return 0
                reason = f"exited with code {code}"
                if code == SERVER_STOPPED:
                    reason = "server stopped (often low memory)"
                break
            sleep(POLL_S)
            now = clock()
            if now - started < STARTUP_GRACE_S or now - last_check < CHECK_EVERY_S:
                continue
            last_check = now
            fails = 0 if check(port) else fails + 1
            if fails >= HUNG_AFTER_FAILS:
                reason = "stopped answering for 3 minutes"
                try:
                    child.kill()
                    child.wait(timeout=30)
                except Exception:
                    pass
                break
        now = clock()
        while restarts and now - restarts[0] > WINDOW_S:
            restarts.popleft()
        if len(restarts) >= MAX_RESTARTS:
            if max_cooldowns is not None and cooldowns >= max_cooldowns:
                _log(f"ARYA {reason}; {MAX_RESTARTS} restarts in 30 minutes — giving up")
                return 1
            cooldowns += 1
            _log(f"ARYA {reason}; {MAX_RESTARTS} restarts in 30 minutes — pausing {COOLDOWN_S // 60} min, then trying again")
            sleep(COOLDOWN_S)
            restarts.clear()
            continue
        restarts.append(now)
        wait = FAILED_START_BACKOFF_S if now - started < STARTUP_GRACE_S else BACKOFF_S
        _log(f"ARYA {reason} — restarting in {wait} s")
        sleep(wait)
