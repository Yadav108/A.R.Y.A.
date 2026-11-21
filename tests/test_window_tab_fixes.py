"""From the user (2026-11-17): "it cannot minimize any open app and cannot switch between chrome tabs".
Log: "minimize window explorer" did nothing (explorer.exe is protected from closing, so the shared lookup never
found it), "Can you restore the tab?" was taken by window control, "switch to 'claude' tab …" went to the model.
Windows, keys and Chrome are faked."""
import asyncio

import pytest

from arya.tools import browser as B
from arya.tools import windows as W

WINDOWS = [  # (hwnd, pid, class, title) — what EnumWindows showed on this laptop
    (1, 10, "Chrome_WidgetWin_1", "YouTube Music - Google Chrome"),
    (2, 20, "CabinetWClass", "Downloads - File Explorer"),
    (3, 20, "Progman", "Program Manager"),                         # the desktop: never touched
    (4, 20, "Shell_TrayWnd", "Taskbar"),
    (5, 30, "ApplicationFrameWindow", "WhatsApp"),                 # a Store app: the host owns the window
]


@pytest.fixture
def screen(monkeypatch):
    shown = []
    monkeypatch.setattr(W, "_visible_windows", lambda: WINDOWS)
    import arya.tools.files as files
    procs = {"chrome": [type("P", (), {"pid": 10})()]}
    monkeypatch.setattr(files, "_match_processes", lambda app: procs.get(app.lower(), []))
    monkeypatch.setattr(W, "_show", lambda hwnds, mode: shown.append((hwnds, mode)))
    return shown


@pytest.mark.parametrize("app", ["explorer", "file explorer", "windows explorer", "window explorer", "folder"])
def test_file_explorer_means_its_folder_windows_only(app, screen):
    assert W._app_windows(app) == [2]


def test_minimize_window_explorer_from_the_log(screen):
    assert asyncio.run(W.handle("minimize window explorer")) == "Minimized File Explorer."
    assert screen == [([2], W.SW_MINIMIZE)]


def test_by_title_when_no_process_matches(screen):
    assert W._app_windows("whatsapp") == [5]                   # Store app
    assert W._app_windows("downloads") == [2]
    assert W._app_windows("chrome") == [1]                     # by process first
    assert W._app_windows("photoshop") == []


def test_restore_the_tab_is_for_the_browser(screen):
    assert asyncio.run(W.handle("Can you restore the tab?")) is None
    assert B._REOPEN.search("Can you restore the tab?")


# ── Tabs by name ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, name", [
    ("switch to \"claude\" tab opened in chrome under JCNS group", "claude"),      # from the log
    ("switch to the Gmail tab", "Gmail"), ("go to my YouTube Music tab", "YouTube Music"),
    ("take me to the GitHub tab please", "GitHub"), ("show me the Overleaf tab", "Overleaf"),
])
def test_named_tabs(text, name):
    assert B.named_tab(text) == name


@pytest.mark.parametrize("text", ["switch to the next tab", "go to the last tab", "go to tab 3",
                                  "switch to the second tab", "open a new tab", "switch to the previous tab",
                                  "go to the kitchen"])
def test_positions_are_not_names(text):
    assert B.named_tab(text) is None


def test_switch_uses_chrome_tab_search(monkeypatch):
    keys, typed = [], []
    monkeypatch.setattr(B, "_chrome_window", lambda: 7)
    monkeypatch.setattr(B, "_focus", lambda hwnd: True)
    monkeypatch.setattr(B, "_press", lambda *k: keys.append(k))
    monkeypatch.setattr(B, "_type", lambda text: typed.append(text))
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    monkeypatch.setattr(B, "_foreground", lambda: 99)            # the tab-search popup is open
    monkeypatch.setattr(B, "_wait_for", lambda test, seconds: 99)
    import win32gui
    monkeypatch.setattr(win32gui, "GetWindowText", lambda h: "Claude - Google Chrome")
    assert B._switch_to_named_tab("claude") == "Switched to “Claude”."
    assert keys == [(B.VK_CONTROL, B.VK_SHIFT, ord("A")), (0x0D,)] and typed == ["claude"]
    monkeypatch.setattr(win32gui, "GetWindowText", lambda h: "Inbox - Gmail - Google Chrome")
    assert "Chrome is showing “Inbox - Gmail”" in B._switch_to_named_tab("overleaf")


def test_named_tab_through_handle(monkeypatch):
    monkeypatch.setattr(B, "_switch_to_named_tab", lambda q: f"Switched to “{q}”.")
    assert asyncio.run(B.handle("switch to the Claude tab")) == "Switched to “Claude”."
