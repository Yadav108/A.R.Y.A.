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


def _app_windows(app: str) -> list[int]:
    """Visible top-level windows (with a title) belonging to the app."""
    import win32gui
    import win32process

    from arya.tools.files import _match_processes

    pids = {p.pid for p in _match_processes(app)}
    if not pids:
        return []
    found: list[int] = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            try:
                if win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
                    found.append(hwnd)
            except Exception:
                pass
        return True

    win32gui.EnumWindows(visit, None)
    return found


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
    if not m:
        return None
    verb = m.group("verb").lower()
    _, mode, done = next(v for k, v in _ACTIONS.items() if verb.startswith(k))
    what = re.sub(r"^(?:the|my)\s+", "", m.group("what").strip(), flags=re.IGNORECASE)
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
    return f"{done} {app[:1].upper() + app[1:]}."
