"""
Keeps ARYA running (roadmap C5): the desktop shortcut and "Start with Windows" start this small watchdog,
which starts ARYA itself as a child process and

  - restarts it if it crashes (any exit except your own "Quit ARYA"),
  - restarts it if its server stops answering for 3 minutes while the window lives on (a hung model or
    GPU driver), checked once a minute at http://127.0.0.1:<port>/healthz,
  - gives up after 5 restarts in 30 minutes instead of looping, and logs every restart.

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
CHECK_EVERY_S = 60
STARTUP_GRACE_S = 180            # models take a while to load
HUNG_AFTER_FAILS = 3             # 3 missed checks in a row -> hung
MAX_RESTARTS, WINDOW_S = 5, 30 * 60
BACKOFF_S = 5


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


def _child_command(args: list[str]) -> list[str]:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else exe), str(LAUNCHER), "--child", *args]


def supervise(args: list[str], *, spawn=subprocess.Popen, check=healthy, sleep=time.sleep, clock=time.monotonic,
              port: int | None = None) -> int:
    """Run ARYA and keep it running. Returns the exit code to leave with."""
    port = port or int(os.getenv("ARYA_PORT", "8000"))
    restarts: deque[float] = deque()
    while True:
        started = clock()
        child = spawn(_child_command(args), cwd=str(ROOT))
        _log(f"started ARYA (pid {getattr(child, 'pid', '?')})")
        fails, reason = 0, ""
        while True:
            code = child.poll()
            if code is not None:
                if code == 0:
                    _log("ARYA quit normally")
                    return 0
                reason = f"exited with code {code}"
                break
            sleep(CHECK_EVERY_S)
            if clock() - started < STARTUP_GRACE_S:
                continue
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
            _log(f"ARYA {reason}; {MAX_RESTARTS} restarts in 30 minutes — giving up")
            return 1
        restarts.append(now)
        _log(f"ARYA {reason} — restarting in {BACKOFF_S} s")
        sleep(BACKOFF_S)
