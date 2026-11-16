"""
Laptop remote control (roadmap C) — from the phone or at the laptop. Safe actions only: nothing here touches
files, closes apps or runs commands.

  "what's running?" / "which apps are open?"   -> the apps with open windows (Chrome, VS Code, Spotify…)
  "how much memory is free?"                   -> free RAM and the biggest apps
  "lock the laptop"                            -> Windows' lock screen (your password to get back in)
  "put the laptop to sleep" / "… in 10 minutes"  -> sleep, after a spoken warning on the laptop
                                                  (30 seconds for "now", one minute before a later time)
  "cancel sleep" / "stay awake"
Battery: "battery?" is already answered instantly.
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import re
import time

MAX_SLEEP_DELAY_S = 12 * 3600
NOW_GRACE_S = 30
WARN_BEFORE_S = 60

# Apps a person would recognise (process name -> what to call it). Shared with the low-memory reply.
FRIENDLY_APP = {"code": "VS Code", "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "claude": "Claude Code",
                "vmmemwsl": "WSL", "vmmem": "WSL", "spotify": "Spotify", "teams": "Teams", "ms-teams": "Teams",
                "discord": "Discord", "slack": "Slack", "pycharm64": "PyCharm", "idea64": "IntelliJ",
                "whatsapp": "WhatsApp", "zoom": "Zoom", "obs64": "OBS", "steam": "Steam", "winword": "Word",
                "excel": "Excel", "powerpnt": "PowerPoint", "outlook": "Outlook", "olk": "Outlook",
                "notepad": "Notepad", "acrord32": "Acrobat", "acrobat": "Acrobat", "vlc": "VLC",
                "explorer": "File Explorer", "matlab": "MATLAB", "windowsterminal": "Terminal",
                "applicationframehost": None}
NOT_YOURS = {"pythonw", "python", "ollama", "llama-server", "svchost", "dwm", "msmpeng", "system", "registry",
             "memory compression", "msedgewebview2", "searchhost", "csrss", "lsass", "wininit", "textinputhost",
             "shellexperiencehost", "startmenuexperiencehost", "systemsettings", "lockapp"}


def memory_hogs(top: int = 3) -> str:
    """'VS Code (3.3 GB), Chrome (1.5 GB) and WSL (0.8 GB)' — apps you could close (summed over their processes;
    ARYA, Ollama and Windows itself are left out). Read on this laptop only."""
    try:
        import psutil
        totals: dict[str, float] = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            name = (p.info["name"] or "").lower().removesuffix(".exe")
            mem = p.info["memory_info"]
            label = FRIENDLY_APP.get(name)
            if not name or name in NOT_YOURS or mem is None or label is None or label == "File Explorer":
                continue
            totals[label] = totals.get(label, 0.0) + getattr(mem, "private", mem.rss) / 2**30
        big = sorted(((gb, n) for n, gb in totals.items() if gb >= 0.3), reverse=True)[:top]
    except Exception:
        return ""
    parts = [f"{n} ({gb:.1f} GB)" for gb, n in big]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def running_apps() -> list[str]:
    """Apps with an open window, by name: ['Chrome (2 windows)', 'File Explorer', 'Spotify']."""
    import psutil

    from arya.tools.windows import _NOT_APP_WINDOWS, _visible_windows

    counts: dict[str, int] = {}
    names: dict[int, str] = {}
    for _hwnd, pid, cls, title in _visible_windows():
        if pid == os.getpid() or cls in _NOT_APP_WINDOWS:
            continue
        if pid not in names:
            try:
                names[pid] = psutil.Process(pid).name().lower().removesuffix(".exe")
            except Exception:
                names[pid] = ""
        exe = names[pid]
        if not exe or exe in NOT_YOURS:
            continue
        label = FRIENDLY_APP[exe] if exe in FRIENDLY_APP else exe.title()
        if label is None:                                # a Store app's host: call it by its window title
            label = title.split(" - ")[-1].strip()[:30] or "an app"
        counts[label] = counts.get(label, 0) + 1
    return [f"{n} ({c} windows)" if c > 1 else n for n, c in sorted(counts.items(), key=lambda kv: -kv[1])]


def free_ram_gb() -> float:
    import psutil
    return psutil.virtual_memory().available / 2**30


def _lock() -> None:
    ctypes.windll.user32.LockWorkStation()


def _suspend() -> None:
    ctypes.windll.powrprof.SetSuspendState(False, True, False)       # sleep (not hibernate), wake events allowed


# ── Sleep later ───────────────────────────────────────────────────────────────

_sleep: dict = {"task": None, "at": 0.0}
_say = None                      # websocket.py: async announce(text) on the laptop (and in the phone feed)


def set_announcer(say) -> None:
    global _say
    _say = say


async def _sleep_after(delay: float, warn_before: float) -> None:
    try:
        if delay > warn_before:
            await asyncio.sleep(delay - warn_before)
        if _say is not None:
            left = min(delay, warn_before)
            await _say(f"The laptop goes to sleep in {int(left)} seconds. Say \"cancel sleep\" to stop it.")
            await asyncio.sleep(left)
        else:
            await asyncio.sleep(min(delay, warn_before))
        print("Remote: putting the laptop to sleep")
        _sleep["task"], _sleep["at"] = None, 0.0
        await asyncio.to_thread(_suspend)
    except asyncio.CancelledError:
        pass


def schedule_sleep(seconds: float) -> None:
    cancel_sleep()
    warn = NOW_GRACE_S if seconds <= NOW_GRACE_S else WARN_BEFORE_S
    _sleep["at"] = time.time() + max(seconds, NOW_GRACE_S)
    _sleep["task"] = asyncio.get_running_loop().create_task(_sleep_after(max(seconds, NOW_GRACE_S), warn))


def cancel_sleep() -> bool:
    task = _sleep["task"]
    _sleep["task"], _sleep["at"] = None, 0.0
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


# ── Voice ─────────────────────────────────────────────────────────────────────

_DEVICE = r"(?:the\s+|my\s+)?(?:laptop|computer|pc|machine|notebook)"
# Whole requests only: "what's running in the background of this video" is a question, not this
_RUNNING = re.compile(r"^\W*(?:arya\W+)?(?:what(?:'s|\s+is|\s+apps\s+are)\s+(?:running|open)|which\s+(?:apps|programs|"
                      r"windows)\s+(?:are\s+)?(?:open|running))(?:\s+(?:on|in)\s+" + _DEVICE + r")?(?:\s+(?:now|right\s+now))?"
                      r"\W*$", re.IGNORECASE)
_MEMORY = re.compile(r"\bhow\s+much\s+(?:memory|ram)\s+(?:is\s+)?(?:free|left|available)\b|\bfree\s+(?:memory|ram)\b|"
                     r"\bwhy\s+is\s+" + _DEVICE + r"\s+(?:so\s+)?slow\b", re.IGNORECASE)
_LOCK = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?lock\s+(?:" + _DEVICE + r"|(?:the\s+)?screen|windows)(?:\s+now)?"
                   r"(?:\s+please)?\W*$", re.IGNORECASE)
_SLEEP = re.compile(r"\b(?:put|send)\s+" + _DEVICE + r"\s+to\s+sleep(?:\s+(?:in|after)\s+(?P<d>.+?))?\W*$|"
                    r"^\W*(?:arya\W+)?(?:please\s+)?" + _DEVICE +
                    r"\s+(?:should\s+)?(?:go\s+to\s+)?sleep(?:\s+(?:in|after)\s+(?P<d2>.+?))?\W*$|"
                    r"^\W*(?:arya\W+)?(?:please\s+)?sleep\s+(?:in|after)\s+(?P<d3>.+?)\W*$", re.IGNORECASE)
_CANCEL = re.compile(r"\b(?:cancel|stop|abort)\s+(?:the\s+)?(?:sleep|going\s+to\s+sleep)\b|\bstay\s+awake\b|"
                     r"\bdon'?t\s+(?:go\s+to\s+)?sleep\b", re.IGNORECASE)


def matches(message: str) -> bool:
    return any(p.search(message) for p in (_RUNNING, _MEMORY, _LOCK, _SLEEP, _CANCEL))


def _sleep_delay(spoken: str | None) -> float | None:
    if not spoken:
        return 0.0
    from arya.tools.alarms import duration_seconds
    s = duration_seconds("in " + spoken)
    return s if s and s <= MAX_SLEEP_DELAY_S else None


async def handle(message: str) -> str | None:
    from arya.core import policy

    if _CANCEL.search(message):
        return "Okay, the laptop stays awake." if cancel_sleep() else "There's no sleep planned."
    m = _SLEEP.search(message)
    if m:
        delay = _sleep_delay(m.group("d") or m.group("d2") or m.group("d3"))
        if delay is None:
            return "How long until it should sleep? For example \"in 20 minutes\"."
        schedule_sleep(delay)
        policy.note_result("window_control")
        if delay <= NOW_GRACE_S:
            return f"Putting the laptop to sleep in {NOW_GRACE_S} seconds. Say \"cancel sleep\" to stop it."
        from arya.tools.productivity import _speak_duration
        return f"The laptop will sleep in {_speak_duration(delay)}; I'll warn you a minute before."
    if _LOCK.match(message):
        await asyncio.to_thread(_lock)
        policy.note_result("window_control")
        return "Locked. Your password gets you back in."
    if _RUNNING.search(message):
        apps = await asyncio.to_thread(running_apps)
        if not apps:
            return "No apps with open windows right now."
        listed = apps[0] if len(apps) == 1 else ", ".join(apps[:-1]) + " and " + apps[-1]
        return f"Open on the laptop: {listed}."
    if _MEMORY.search(message):
        free = await asyncio.to_thread(free_ram_gb)
        hogs = await asyncio.to_thread(memory_hogs)
        return f"{free:.1f} gigabytes of memory are free." + (f" The biggest apps: {hogs}." if hogs else "")
    return None
