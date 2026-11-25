"""
ARYA looks after itself (self-healing laptop).

  "health check" / "system check" / "run diagnostics" / "how's the laptop holding up?"
      -> one spoken line: problems first (model not loaded, disk nearly full, little free RAM, restarts today),
         otherwise "all good" with uptime, free RAM and disk.

In the background, once a minute:
  - Waking from sleep: a jump in the wall clock means the laptop slept. ARYA logs it and checks the model is
    still answering, so the first question after a sleep isn't the one that finds out.
  - Disk: below 1.5 GB free on the system drive, uploads start failing and Windows gets slow. ARYA says so
    once a day (on the laptop if you're there, otherwise on the phone).

The watchdog (arya/desktop/supervisor.py) restarts ARYA after crashes and hangs; its log is where the restart
count comes from. Nothing here deletes, closes or changes anything.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CHECK_EVERY_S = 60
SLEPT_AFTER_S = 3 * CHECK_EVERY_S          # a tick this late means the laptop was asleep (or hibernating)
DISK_LOW_GB = 1.5
RAM_LOW_GB = 0.5
STARTED = time.time()

_warned_disk: date | None = None


# ── Measurements (each one cheap and never raising) ───────────────────────────

def disk_free_gb(path: str | None = None) -> float:
    try:
        return shutil.disk_usage(path or Path.home().anchor).free / 2**30
    except OSError:
        return -1.0


def ram_free_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / 2**30
    except Exception:
        return -1.0


def model_ready(timeout: float = 2.0) -> bool:
    """The local model's server answers (Ollama). Says nothing about whether the model is in VRAM."""
    try:
        from arya.core.llm_local import OLLAMA_HOST
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


_RESTART = re.compile(r"^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] ARYA (?!quit normally).*(?:restarting|pausing)")


def restarts_since(since: datetime, log: Path | None = None) -> int:
    """How often the watchdog had to restart ARYA since `since` (from supervisor.log)."""
    try:
        if log is None:
            from arya.knowledge.library import DATA_DIR
            log = DATA_DIR / "logs" / "supervisor.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
    except OSError:
        return 0
    n = 0
    for line in lines:
        m = _RESTART.match(line)
        if m and datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S") >= since:
            n += 1
    return n


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours // 24} days"


def _gb(x: float) -> str:
    return f"{x:.1f} GB"


def snapshot(now: float | None = None) -> dict:
    now = now or time.time()
    return {"uptime_s": round(now - STARTED), "model": model_ready(), "ram_gb": round(ram_free_gb(), 2),
            "disk_gb": round(disk_free_gb(), 2),
            "restarts_24h": restarts_since(datetime.fromtimestamp(now) - timedelta(hours=24))}


def report(s: dict | None = None) -> str:
    """One spoken line, problems first."""
    s = s or snapshot()
    problems = []
    if not s["model"]:
        problems.append("my local model isn't answering — I'll keep trying, and the cloud still works if you ask")
    if 0 <= s["disk_gb"] < DISK_LOW_GB:
        problems.append(f"the disk is nearly full ({_gb(s['disk_gb'])} free) — uploads will fail; "
                        "clearing Downloads or the recycle bin helps")
    if 0 <= s["ram_gb"] < RAM_LOW_GB:
        problems.append(f"memory is tight ({_gb(s['ram_gb'])} free)")
    if s["restarts_24h"]:
        n = s["restarts_24h"]
        problems.append(f"I had to restart myself {n} time{'s' if n != 1 else ''} in the last day")
    up = _duration(s["uptime_s"])
    if problems:
        return "Mostly fine, but " + "; ".join(problems) + f". I've been up {up}."
    ram = f", {_gb(s['ram_gb'])} of memory" if s["ram_gb"] >= 0 else ""
    return (f"All good: up {up}, the model is ready{ram} and {_gb(s['disk_gb'])} of disk free, "
            "no restarts in the last day.")


# ── Wake-on-LAN ───────────────────────────────────────────────────────────────

def _routed_ip() -> str:
    """The laptop's own address on the network it uses to go out (a UDP 'connect' sends nothing)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))           # TEST-NET address: never contacted, only routed
            ip = s.getsockname()[0]
    except OSError:
        return ""
    return "" if ip.startswith(("100.", "127.")) else ip


def wake_target(lan_ips, addrs=None) -> dict | None:
    """{"mac": "80:38:FB:8A:6C:D6", "broadcast": "192.168.0.255"} of the adapter that has one of the laptop's home
    network addresses — what the phone app needs to wake a sleeping laptop with a magic packet (on the same Wi-Fi;
    Tailscale can't carry it). None when there's no home network address. `addrs` = psutil.net_if_addrs()."""
    import ipaddress
    import socket
    if addrs is None:
        try:
            import psutil
            addrs = psutil.net_if_addrs()
        except Exception:
            return None
    wanted = {ip for ip in lan_ips if not ip.startswith("100.")}           # 100.64/10 = Tailscale
    if not wanted:                                 # phone page only on Tailscale: the adapter Windows routes through
        wanted = {_routed_ip()} - {""}             # (not VirtualBox / Hyper-V adapters)
    link = getattr(socket, "AF_LINK", -1)
    for entries in addrs.values():
        v4 = next((a for a in entries if a.family == socket.AF_INET and a.address in wanted), None)
        mac = next((a.address for a in entries if a.family in (link, -1) and a.address
                    and re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", a.address)), None)
        if v4 and mac:
            try:
                net = ipaddress.IPv4Network(f"{v4.address}/{v4.netmask}", strict=False)
                broadcast = str(net.broadcast_address)
            except ValueError:
                broadcast = "255.255.255.255"
            return {"mac": mac.replace("-", ":").upper(), "broadcast": broadcast}
    return None


# ── Voice ─────────────────────────────────────────────────────────────────────

_ASK = re.compile(r"\b(?:health|system|self)[\s-]?check\b|\brun\s+(?:a\s+)?diagnostics?\b|\bdiagnose\s+yourself\b|"
                  r"\bhow(?:'s|\s+is)\s+(?:the\s+|my\s+)?laptop\s+(?:doing|holding\s+up)\b|"
                  r"\b(?:arya|system)\s+status\b|\bstatus\s+report\b", re.IGNORECASE)


def matches(message: str) -> bool:
    return bool(_ASK.search(message))


async def handle(message: str) -> str | None:
    if not matches(message):
        return None
    return await asyncio.to_thread(report)


# ── Background ────────────────────────────────────────────────────────────────

def slept_for(last_tick: float, now: float, interval: float = CHECK_EVERY_S) -> float:
    """Seconds the laptop was asleep between two ticks of a loop that runs every `interval` s (0 = it wasn't)."""
    gap = now - last_tick
    return gap - interval if gap > SLEPT_AFTER_S else 0.0


def disk_warning(free_gb: float, today: date) -> str | None:
    """Once a day, when the system drive is nearly full."""
    global _warned_disk
    if free_gb < 0 or free_gb >= DISK_LOW_GB or _warned_disk == today:
        return None
    _warned_disk = today
    return (f"Heads-up: only {_gb(free_gb)} is free on the laptop's disk. Uploads and backups will start failing — "
            "clearing Downloads or emptying the recycle bin would help.")


async def run(announce, busy=lambda: False) -> None:
    """Started by the server. `announce(text)` (async) speaks at the laptop; warnings also go to the phone."""
    await asyncio.sleep(90)
    last = time.time()
    while True:
        await asyncio.sleep(CHECK_EVERY_S)
        now = time.time()
        try:
            slept = slept_for(last, now)
            if slept:
                print(f"Health: laptop woke up after {_duration(slept)} asleep")
                if not await asyncio.to_thread(model_ready):
                    print("Health: local model not answering after the sleep — it is reloaded on the next question")
            warning = disk_warning(await asyncio.to_thread(disk_free_gb), date.today())
            if warning:
                print(f"Health: disk low ({disk_free_gb():.1f} GB free)")
                try:                                   # on the phone too: you may not be at the laptop
                    from arya.server import phone
                    phone.push(warning)
                except Exception:
                    pass
                if not busy():
                    await announce(warning)
        except Exception as exc:
            print(f"Health: {type(exc).__name__}: {exc}")
        last = now
