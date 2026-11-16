"""
Window control: "minimize Chrome", "maximize Word", "restore Spotify", "minimize this window",
"show the desktop" / "minimize everything".

Windows are found by the app's process (the same matching close_app uses); only visible top-level
windows with a title are touched. Nothing is closed here — minimise / maximise / restore only.
"""
from __future__ import annotations

import asyncio
import re

SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
_ACTIONS = {"minimi": ("minimize", SW_MINIMIZE, "Minimized"), "maximi": ("maximize", SW_MAXIMIZE, "Maximized"),
            "restor": ("restore", SW_RESTORE, "Restored"), "unminimi": ("restore", SW_RESTORE, "Restored")}

_CMD = re.compile(r"^\W*(?:arya\W+)?(?:(?:can you|could you|please)\s+)?(?:please\s+)?(?P<verb>minimi[sz]ed?|maximi[sz]ed?|restored?|unminimi[sz]ed?)\s+(?:the\s+|my\s+)?"
                  r"(?P<what>.+?)(?:\s+windows?)?\W*$", re.IGNORECASE)
_DESKTOP = re.compile(r"\b(?:show (?:me )?(?:the |my )?desktop|minimi[sz]e (?:everything|all(?: the)? windows|all))\b",
                      re.IGNORECASE)
_THIS = re.compile(r"^(?:this|that|the|current|active|this current)?\s*(?:window)?$", re.IGNORECASE)


# File Explorer: its process (explorer.exe) is protected from *closing* (it is also the taskbar and the desktop),
# so the close-app lookup never finds it — from the log, "minimize window explorer" did nothing while
# "Downloads - File Explorer" was open. Minimising is safe; only its folder windows (CabinetWClass) are touched.
_EXPLORER = re.compile(r"^(?:file|files|windows?)?\s*explorer$|^folders?$|^file manager$", re.IGNORECASE)
_NOT_APP_WINDOWS = {"Progman", "WorkerW", "Shell_TrayWnd", "Windows.UI.Core.CoreWindow"}


def _visible_windows() -> list[tuple[int, int, str, str]]:
    """(hwnd, pid, class, title) of visible top-level windows with a title, top of the screen first."""
    import win32gui
    import win32process

    out: list[tuple[int, int, str, str]] = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                try:
                    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                    out.append((hwnd, pid, win32gui.GetClassName(hwnd), title))
                except Exception:
                    pass
        return True

    win32gui.EnumWindows(visit, None)
    return out


def _app_windows(app: str) -> list[int]:
    """Visible top-level windows belonging to the app — by its process, else by the window title
    ('downloads', a Store app like WhatsApp or Settings, 'YouTube')."""
    import os

    from arya.tools.files import _match_processes

    windows = [w for w in _visible_windows() if w[1] != os.getpid() and w[2] not in _NOT_APP_WINDOWS]
    if _EXPLORER.match(app.strip()):
        return [h for h, _pid, cls, _t in windows if cls == "CabinetWClass"]
    pids = {p.pid for p in _match_processes(app)}
    by_process = [h for h, pid, _c, _t in windows if pid in pids]
    if by_process:
        return by_process
    words = [w for w in re.findall(r"\w+", app.lower()) if len(w) > 2]
    if not words:
        return []
    return [h for h, _pid, _c, title in windows if all(w in title.lower() for w in words)]


def _foreground() -> int | None:
    import os

    import win32gui
    import win32process

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
        return None                                    # ARYA's own window: not "this window"
    return hwnd


def _show(hwnds: list[int], mode: int) -> None:
    import win32gui
    for h in hwnds:
        win32gui.ShowWindow(h, mode)


def _show_desktop() -> None:
    import win32api
    VK_LWIN, D = 0x5B, ord("D")
    win32api.keybd_event(VK_LWIN, 0, 0, 0)
    win32api.keybd_event(D, 0, 0, 0)
    win32api.keybd_event(D, 0, 2, 0)
    win32api.keybd_event(VK_LWIN, 0, 2, 0)


async def handle(message: str) -> str | None:
    from arya.core import policy

    if _DESKTOP.search(message):
        await asyncio.to_thread(_show_desktop)
        policy.note_result("window_control")
        return "Showing the desktop."
    m = _CMD.search(message)
    if not m or re.search(r"\btabs?\b", m.group("what"), re.IGNORECASE):
        return None                                    # "restore the tab": the browser reopens it (from the log)
    verb = m.group("verb").lower()
    _, mode, done = next(v for k, v in _ACTIONS.items() if verb.startswith(k))
    what = re.sub(r"^(?:the|my)\s+", "", m.group("what").strip(), flags=re.IGNORECASE)
    what = re.sub(r"^windows?\s+(?=\S)(?!explorer\b)", "", what, flags=re.IGNORECASE)   # "window Chrome" -> "Chrome"
    if _THIS.match(what):
        hwnd = await asyncio.to_thread(_foreground)
        if hwnd is None:
            return "Which window? Say its app, like \"minimize Chrome\"."
        await asyncio.to_thread(_show, [hwnd], mode)
        policy.note_result("window_control")
        return f"{done} the window."
    app = re.sub(r"\s+(?:browser|app|application|program)$", "", what, flags=re.IGNORECASE)
    if len(app.split()) > 3:
        return None                                    # "maximize the profit of a company" isn't a window
    hwnds = await asyncio.to_thread(_app_windows, app)
    if not hwnds:
        return f"I can't find an open {app} window."
    await asyncio.to_thread(_show, hwnds, mode)
    policy.note_result("window_control")
    name = "File Explorer" if _EXPLORER.match(app.strip()) else app[:1].upper() + app[1:]
    return f"{done} {name}."
