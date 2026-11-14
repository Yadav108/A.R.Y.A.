"""
File and app control inside the sandbox defined in arya/core/policy.py.

Destructive operations are recoverable or conservative by design:
  * delete_file sends to the Recycle Bin (never a permanent delete), files only.
  * move_file never overwrites an existing file.
  * close_app asks the app to close (like clicking X), never force-kills, and
    refuses system processes and ARYA itself.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime
from pathlib import Path

import psutil

from arya.core.policy import PolicyError, check_path, describe_path

_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".py", ".tex", ".m", ".c", ".cpp", ".h",
                    ".java", ".js", ".ts", ".html", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".srt"}
_MAX_READ_CHARS = 3000

# Never closed: Windows itself, security software, and ARYA's own engine.
_PROTECTED_PROCESSES = {
    "system", "idle", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe", "fontdrvhost.exe", "sihost.exe",
    "ctfmon.exe", "msmpeng.exe", "securityhealthservice.exe", "taskhostw.exe", "conhost.exe",
    "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe", "audiodg.exe",
    "python.exe", "pythonw.exe", "ollama.exe", "ollama app.exe", "ollama_llama_server.exe",
}


def _human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


async def list_folder(path: str = "Downloads", limit: int = 15) -> str:
    folder = check_path(path)
    if not folder.is_dir():
        return f"{folder.name or folder} isn't a folder I can find."

    def _scan():
        entries = []
        for entry in os.scandir(folder):
            if entry.name.startswith(".") or entry.name.lower() == "desktop.ini":
                continue
            try:
                st = entry.stat()
                entries.append((st.st_mtime, entry.name, entry.is_dir(), st.st_size))
            except OSError:
                continue
        return sorted(entries, reverse=True)

    entries = await asyncio.to_thread(_scan)
    if not entries:
        return f"{folder.name} is empty."
    limit = max(1, min(int(limit), 40))
    lines = [
        f"{name}{'/' if is_dir else ''} ({'folder' if is_dir else _human_size(size)}, "
        f"modified {datetime.fromtimestamp(mtime):%d %b %H:%M})"
        for mtime, name, is_dir, size in entries[:limit]
    ]
    more = f" Showing the {limit} most recent." if len(entries) > limit else ""
    return f"{folder.name} has {len(entries)} items.{more}\n" + "\n".join(lines)


async def read_text_file(path: str) -> str:
    file = check_path(path)
    if not file.is_file():
        return f"I can't find {describe_path(file)}."
    if file.suffix.lower() not in _TEXT_EXTENSIONS:
        return f"{file.name} isn't a plain-text file, so I can't read it out."

    def _read():
        with open(file, "r", encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_READ_CHARS + 1)

    text = await asyncio.to_thread(_read)
    cut = " (first part only)" if len(text) > _MAX_READ_CHARS else ""
    return f"Contents of {file.name}{cut}:\n{text[:_MAX_READ_CHARS]}"


async def move_file(source: str, destination: str) -> str:
    src = check_path(source)
    if not src.exists():
        return f"I can't find {describe_path(src)}."
    dest_raw = destination.strip()
    # A bare new name ("budget-final.xlsx") means rename in place.
    if not any(sep in dest_raw for sep in ("/", "\\")) and dest_raw.lower() not in (
            "desktop", "documents", "downloads", "pictures", "music", "videos") and "." in dest_raw:
        dest = check_path(src.parent / dest_raw)
    else:
        dest = check_path(dest_raw)
    if dest.is_dir():
        dest = check_path(dest / src.name)
    if dest.exists():
        return f"{describe_path(dest)} already exists, so I didn't overwrite it."
    if not dest.parent.is_dir():
        return f"The folder {dest.parent.name} doesn't exist."
    await asyncio.to_thread(shutil.move, str(src), str(dest))
    verb = "Renamed" if dest.parent == src.parent else "Moved"
    return f"{verb} {src.name} to {describe_path(dest)}."


def _recycle(path: Path) -> None:
    from win32com.shell import shell, shellcon

    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI
    result, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, str(path), None, flags, None, None))
    if result != 0 or aborted:
        raise RuntimeError(f"Windows refused to recycle it (code {result}).")


async def delete_file(path: str) -> str:
    file = check_path(path)
    if not file.exists():
        return f"I can't find {describe_path(file)}."
    if file.is_dir():
        return "I only delete single files, not folders — do that one yourself in Explorer."
    await asyncio.to_thread(_recycle, file)
    return f"Moved {file.name} to the Recycle Bin. You can restore it from there."


def _match_processes(app_name: str) -> list[psutil.Process]:
    from arya.tools.os_tools import _APP_ALIASES

    key = app_name.strip().lower()
    exe = _APP_ALIASES.get(key, key).lower()
    if not exe.endswith(".exe"):
        exe_guess = exe.replace(" ", "") + ".exe"
    else:
        exe_guess = exe
    matches = []
    for proc in psutil.process_iter(["name", "pid"]):
        name = (proc.info.get("name") or "").lower()
        if not name or name in _PROTECTED_PROCESSES or proc.info["pid"] == os.getpid():
            continue
        if name == exe_guess or name == exe or name.removesuffix(".exe") == key.replace(" ", ""):
            matches.append(proc)
    return matches


async def close_app(app_name: str) -> str:
    key = app_name.strip().lower()
    if not key:
        return "Which app should I close?"
    if key in _PROTECTED_PROCESSES or f"{key}.exe" in _PROTECTED_PROCESSES:
        raise PolicyError(f"{app_name} is part of Windows or ARYA itself, so I won't close it.")
    procs = await asyncio.to_thread(_match_processes, app_name)
    if not procs:
        return f"{app_name} doesn't seem to be running."

    def _close():
        # Ask each top-level window to close (exactly like clicking X), so the
        # app can save or prompt. psutil's terminate() would be a force kill on
        # Windows and lose unsaved work — never used here.
        import win32con
        import win32gui
        import win32process

        pids = {p.pid for p in procs}
        windows = []

        def _collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                if win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                    windows.append(hwnd)
            return True

        win32gui.EnumWindows(_collect, None)
        for hwnd in windows:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        gone, alive = psutil.wait_procs(procs, timeout=4)
        return len(windows), len(alive)

    windows, alive = await asyncio.to_thread(_close)
    if not windows:
        return f"{app_name} is running in the background with no window to close."
    if alive:
        return f"Asked {app_name} to close — it's still open, probably asking you to save something."
    return f"Closed {app_name}."
