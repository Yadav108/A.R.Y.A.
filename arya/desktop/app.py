"""
ARYA desktop app.

  * App window — ARYA's interface (arya/static/app.html) in a native window
    (Edge WebView2): the living orb, the conversation, Yes/No confirmations,
    microphone and privacy controls, system status and recent actions.
    Closing it hides it to the tray; ARYA keeps listening for "Hey ARYA".
  * Tray icon — colour shows ARYA's state; right-click for open, mute,
    privacy mode, overlay, start with Windows, action log, quit.
  * Status overlay — when the window isn't open, a small card above the
    taskbar shows what ARYA heard and is saying, then fades away.
  * The voice server runs in a background thread of this same process.

Threads: main = window (WinForms/WebView2, must be main), "overlay" = Tk,
pystray = its own message loop, "arya-server" = uvicorn + models.

Run:  .venv\\Scripts\\pythonw.exe arya_desktop.py [--background]
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path

from arya.desktop import system

ROOT = system.ROOT
DATA_DIR = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA")
SETTINGS_FILE = DATA_DIR / "desktop.json"
LOG_FILE = DATA_DIR / "logs" / "arya.log"
ICON_FILE = ROOT / "arya" / "static" / "arya.ico"
APP_ID = "ARYA.VoiceAssistant"
WINDOW_TITLE = "ARYA"

# state -> (colour, tooltip) — same palette as the app window
STATES = {
    "starting": ("#64748b", "ARYA — starting up…"),
    "idle": ("#6366f1", "ARYA — say \"Hey ARYA\""),
    "listening": ("#22c55e", "ARYA — listening"),
    "thinking": ("#f59e0b", "ARYA — thinking"),
    "speaking": ("#3b82f6", "ARYA — speaking"),
    "conversation": ("#14b8a6", "ARYA — in conversation (just talk)"),
    "muted": ("#ef4444", "ARYA — microphone muted"),
    "failed": ("#7f1d1d", "ARYA — failed to start (see log)"),
}

BG, FG, MUTED_FG = "#0b0d14", "#f8fafc", "#94a3b8"

SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{height:100%;margin:0;background:#06070b;color:#eef0f6;font:15px "Segoe UI",system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;flex-direction:column;overflow:hidden}
.orb{width:120px;height:120px;border-radius:50%;position:relative;
background:radial-gradient(circle at 34% 28%,#fff 0%,#a5b4fc 14%,#6366f1 48%,#1e1b4b 100%);
box-shadow:0 0 60px #6366f180;animation:b 2.4s ease-in-out infinite}
.orb::after{content:"";position:absolute;inset:-14px;border-radius:50%;
background:conic-gradient(from 0deg,transparent 0 55%,#a5b4fc 80%,transparent);
-webkit-mask:radial-gradient(farthest-side,transparent calc(100% - 2.5px),#000 calc(100% - 2px));animation:s 1.4s linear infinite}
h1{font:600 22px "Segoe UI Variable Display","Segoe UI";letter-spacing:.14em;margin:36px 0 6px}
p{color:#9aa2b6;margin:0}
@keyframes s{to{transform:rotate(360deg)}}@keyframes b{50%{transform:scale(1.04)}}
</style></head><body><div class="orb"></div><h1>ARYA</h1><p id="m">Waking up — loading voice and brain on this laptop…</p></body></html>"""

FAILED = SPLASH.replace("Waking up — loading voice and brain on this laptop…",
                        "ARYA couldn't start — usually low memory. Close some apps and start ARYA again. "
                        "Details: %LOCALAPPDATA%\\\\ARYA\\\\logs\\\\arya.log")


def find_app_window(pid: int | None = None) -> int:
    """ARYA's main window handle. Several hidden helper windows (the tray icon's
    message window, Tk) can share the title, so pick this process's largest
    top-level window called "ARYA"."""
    import win32gui
    import win32process

    pid = os.getpid() if pid is None else pid
    best, best_area = 0, 0

    def visit(hwnd, _):
        nonlocal best, best_area
        if win32gui.GetWindowText(hwnd) != WINDOW_TITLE:
            return True
        if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        area = (right - left) * (bottom - top)
        if area > best_area:
            best, best_area = hwnd, area
        return True

    win32gui.EnumWindows(visit, None)
    return best


# ── Settings & logging ────────────────────────────────────────────────────────

def load_settings() -> dict:
    try:
        return {"overlay": True, "mode": None, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return {"overlay": True, "mode": None}


def save_settings(settings: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def redirect_output_to_log() -> None:
    """pythonw has no console: send prints/tracebacks to a rotating log file."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
        LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
    stream = open(LOG_FILE, "a", buffering=1, encoding="utf-8", errors="replace")
    stream.write(f"\n===== ARYA desktop started {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    sys.stdout = sys.stderr = stream


# ── Tray icon images ──────────────────────────────────────────────────────────

def make_icon(colour: str):
    """A small glowing orb in the state colour (matches the app's orb)."""
    from PIL import Image, ImageDraw, ImageFilter

    big = 256
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((20, 20, big - 20, big - 20), fill=(r, g, b, 140))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(14)))
    sphere = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sphere)
    for i in range(60, 0, -1):          # light from the top-left
        t = i / 60
        col = (int(r + (255 - r) * (1 - t) ** 3), int(g + (255 - g) * (1 - t) ** 3), int(b + (255 - b) * (1 - t) ** 3), 255)
        inset = 34 + (60 - i) * 0.9
        draw.ellipse((inset - (60 - i) * 0.45, inset - (60 - i) * 0.55,
                      big - 34 - (60 - i) * 1.35, big - 34 - (60 - i) * 1.25), fill=col)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((34, 34, big - 34, big - 34), fill=255)
    img.paste(sphere, (0, 0), mask)
    return img.resize((64, 64), Image.LANCZOS)


# ── Status overlay (Tk, own thread) ───────────────────────────────────────────

class Overlay:
    WIDTH = 380

    def __init__(self, root: tk.Tk):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        root.configure(bg=BG)
        card = tk.Frame(root, bg=BG, padx=16, pady=12, highlightthickness=1, highlightbackground="#272b3a")
        card.pack(fill="both", expand=True)
        top = tk.Frame(card, bg=BG)
        top.pack(fill="x")
        self.dot = tk.Canvas(top, width=12, height=12, bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=STATES["idle"][0], outline="")
        self.dot.pack(side="left", padx=(0, 8))
        self.status = tk.Label(top, text="", bg=BG, fg=FG, font=("Segoe UI Semibold", 11), anchor="w")
        self.status.pack(side="left", fill="x")
        self.heard = tk.Label(card, text="", bg=BG, fg=MUTED_FG, font=("Segoe UI", 10, "italic"),
                              wraplength=self.WIDTH - 40, justify="left", anchor="w")
        self.reply = tk.Label(card, text="", bg=BG, fg=FG, font=("Segoe UI", 10),
                              wraplength=self.WIDTH - 40, justify="left", anchor="w")
        for widget in (root, card, top, self.status, self.heard, self.reply, self.dot):
            widget.bind("<Button-1>", lambda _e: self.hide())
        self.visible = False
        self._hide_job = None
        self._fade_job = None
        root.withdraw()

    def set(self, status: str | None = None, colour: str | None = None,
            heard: str | None = None, reply: str | None = None) -> None:
        if status is not None:
            self.status.config(text=status)
        if colour is not None:
            self.dot.itemconfig(self.dot_id, fill=colour)
        if heard is not None:
            self.heard.config(text=f"“{heard}”" if heard else "")
            (self.heard.pack(fill="x", pady=(6, 0)) if heard else self.heard.pack_forget())
        if reply is not None:
            text = reply if len(reply) <= 260 else "…" + reply[-259:]
            self.reply.config(text=text)
            (self.reply.pack(fill="x", pady=(6, 0)) if reply else self.reply.pack_forget())
        if self.visible:
            self._place()

    def _place(self) -> None:
        self.root.update_idletasks()
        try:
            import win32api

            monitor = win32api.MonitorFromPoint((0, 0), 2)
            left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
        except Exception:
            right, bottom = self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 48
        height = self.root.winfo_reqheight()
        self.root.geometry(f"{self.WIDTH}x{height}+{right - self.WIDTH - 16}+{bottom - height - 16}")

    def show(self) -> None:
        self._cancel(self._hide_job)
        if not self.visible:
            self.visible = True
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        self._place()
        self._fade_to(0.95)

    def hide_later(self, ms: int) -> None:
        self._cancel(self._hide_job)
        self._hide_job = self.root.after(ms, self.hide)

    def hide(self) -> None:
        self._cancel(self._hide_job)
        self._fade_to(0.0, then=self._withdraw)

    def _withdraw(self) -> None:
        self.visible = False
        self.root.withdraw()

    def _fade_to(self, target: float, then=None) -> None:
        self._cancel(self._fade_job)

        def step():
            current = float(self.root.attributes("-alpha"))
            if abs(current - target) < 0.08:
                self.root.attributes("-alpha", target)
                if then:
                    then()
                return
            self.root.attributes("-alpha", current + (0.12 if target > current else -0.12))
            self._fade_job = self.root.after(16, step)

        step()

    def _cancel(self, job) -> None:
        if job is not None:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass


# ── App ───────────────────────────────────────────────────────────────────────

class DesktopApp:
    def __init__(self, background: bool = False) -> None:
        self.settings = load_settings()
        self.background = background
        self.events: queue.Queue[str] = queue.Queue()
        self.state = "starting"
        self.muted = False
        self.quitting = False
        self.ready = False
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.window = None
        self.window_visible = False
        self._ws = None
        self._reply = ""
        self._conversation_job = None
        self.root: tk.Tk | None = None
        self.overlay: Overlay | None = None
        self._ui_ready = threading.Event()
        self._icons = {name: make_icon(colour) for name, (colour, _) in STATES.items()}
        self.icon = self._build_tray()

    # ── tray ──
    def _build_tray(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        def mode_is(mode):
            return lambda _item: self._current_mode() == mode

        menu = Menu(
            Item("Open ARYA", lambda *_: self.events.put("__SHOW_WINDOW__"), default=True),
            Item("Mute microphone", lambda *_: self.events.put("__TOGGLE_MUTE__"), checked=lambda _i: self.muted),
            Menu.SEPARATOR,
            Item("Privacy", Menu(
                Item("Offline — everything stays on this laptop", lambda *_: self._set_mode("offline"),
                     checked=mode_is("offline"), radio=True),
                Item("Cloud — Groq first (faster, less private)", lambda *_: self._set_mode("cloud"),
                     checked=mode_is("cloud"), radio=True),
            )),
            Item("Status overlay when the window is closed", self._toggle_overlay,
                 checked=lambda _i: self.settings["overlay"]),
            Item("Start with Windows", self._toggle_autostart, checked=lambda _i: system.autostart_enabled()),
            Item("Open action log", self._open_action_log),
            Menu.SEPARATOR,
            Item("Quit ARYA", lambda *_: self.events.put("__QUIT__")),
        )
        return pystray.Icon("ARYA", self._icons["starting"], STATES["starting"][1], menu)

    def _set_state(self, state: str) -> None:
        if self.muted and state != "failed":
            state = "muted"
        self.state = state
        self.icon.icon = self._icons[state]
        self.icon.title = STATES[state][1]

    def _toggle_overlay(self, *_):
        self.settings["overlay"] = not self.settings["overlay"]
        save_settings(self.settings)
        if not self.settings["overlay"]:
            self.events.put("__HIDE_OVERLAY__")

    def _toggle_autostart(self, *_):
        system.set_autostart(not system.autostart_enabled())

    def _open_action_log(self, *_):
        from arya.core.audit import LOG_FILE as ACTIONS
        if ACTIONS.exists():
            os.startfile(ACTIONS)  # type: ignore[attr-defined]

    def _current_mode(self) -> str:
        try:
            import arya.core.llm as llm
            return llm.ARYA_MODE
        except Exception:
            return self.settings.get("mode") or "offline"

    def _set_mode(self, mode: str) -> None:
        if self._ws is not None:
            self._ws.set_privacy_mode(mode)          # tells the app window too
        if mode == "cloud" and not os.getenv("GROQ_API_KEY"):
            self.icon.notify("No GROQ_API_KEY in .env — ARYA will keep answering locally.", "ARYA")

    # ── server ──
    def _run_server(self) -> None:
        try:
            import uvicorn

            from arya.server import security
            import arya.server.websocket as ws
            import arya.core.llm as llm

            if self.settings.get("mode") in ("offline", "cloud"):
                llm.ARYA_MODE = self.settings["mode"]
            ws.add_status_listener(self.events.put)
            self._ws = ws
            config = uvicorn.Config(ws.app, host=security.HOST, port=security.PORT,
                                    server_header=False, log_level="info")
            self.server = uvicorn.Server(config)
            self.server.run()
        except Exception as exc:
            print(f"ARYA server crashed: {exc!r}")
            import traceback
            traceback.print_exc()
        finally:
            if not self.quitting:
                self.events.put("__FAILED__")

    # ── window (main thread) ──
    def _create_window(self):
        import webview

        width, height, x, y = self._fit_window(1180, 780)
        self.window = webview.create_window(
            WINDOW_TITLE, html=SPLASH, width=width, height=height, x=x, y=y, min_size=(760, 540),
            background_color="#06070b", hidden=self.background, text_select=True,
        )
        self.window_visible = not self.background
        self.window.events.closing += self._on_closing
        self.window.events.shown += self._style_window
        self.window.events.minimized += lambda: self._set_window_visible(False)
        self.window.events.restored += lambda: self._set_window_visible(True)
        return self.window

    @staticmethod
    def _fit_window(width: int, height: int) -> tuple[int, int, int | None, int | None]:
        """Preferred size, shrunk to fit the screen's work area (above the
        taskbar) and centred in it. pywebview uses scaled pixels, so undo
        display scaling."""
        try:
            import win32api

            left, top, right, bottom = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), 2))["Work"]
            scale = ctypes.windll.user32.GetDpiForSystem() / 96.0
            avail_w, avail_h = (right - left) / scale, (bottom - top) / scale
            w, h = int(min(width, avail_w - 48)), int(min(height, avail_h - 32))
            return w, h, int(left / scale + (avail_w - w) / 2), int(top / scale + (avail_h - h) / 2)
        except Exception:
            return width, height, None, None

    def _set_window_visible(self, visible: bool) -> None:
        self.window_visible = visible
        if visible:
            self.events.put("__HIDE_OVERLAY__")

    def _on_closing(self):
        """Closing the window hides it — ARYA keeps listening in the tray."""
        if self.quitting:
            return True
        self.window.hide()
        self._set_window_visible(False)
        if not self.settings.get("closed_once"):
            self.settings["closed_once"] = True
            save_settings(self.settings)
            self.icon.notify("ARYA is still listening in the tray. Right-click the orb to quit.", "ARYA")
        return False

    def _style_window(self):
        """Dark title bar and Mica backdrop, whatever the Windows theme."""
        try:
            hwnd = find_app_window()
            if hwnd:
                dwm = ctypes.windll.dwmapi
                for attr, value in ((20, 1), (38, 2)):      # immersive dark mode, Mica
                    v = ctypes.c_int(value)
                    dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
        except Exception as exc:
            print(f"Window styling skipped: {exc}")

    def _show_window(self) -> None:
        if self.window is None:
            return
        self.window.show()
        self.window.restore()
        # Bring to front: briefly on top (a plain show() can open behind other windows).
        self.window.on_top = True
        self.window.on_top = False
        self._set_window_visible(True)

    # ── events (overlay thread) ──
    def _overlay_thread(self) -> None:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("ARYA status")
        self.overlay = Overlay(self.root)
        self._ui_ready.set()
        self.root.after(50, self._poll)
        self.root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        except Exception as exc:
            print(f"Desktop event error: {exc}")
        if not self.quitting:
            self.root.after(50, self._poll)

    def _overlay(self, **kw) -> None:
        if self.settings["overlay"] and not self.window_visible:
            self.overlay.set(**kw)
            self.overlay.show()

    def _handle(self, ev: str) -> None:
        colour = lambda s: STATES[s][0]
        if ev == "__QUIT__":
            return self._shutdown()
        if ev == "__SHOW_WINDOW__":
            self.overlay.hide()
            return self._show_window()
        if ev == "__TOGGLE_MUTE__":
            if self._ws is not None:
                self._ws.set_microphone_muted(not self.muted)
            return
        if ev == "__HIDE_OVERLAY__":
            return self.overlay.hide()
        if ev == "__FAILED__":
            self._set_state("failed")
            if self.window is not None:
                self.window.load_html(FAILED)
            self.icon.notify("ARYA couldn't start — usually low memory. See %LOCALAPPDATA%\\ARYA\\logs.", "ARYA")
            return
        if ev == "ARYA_READY":
            self.ready = True
            self._set_state("idle")
            from arya.server import security
            if self.window is not None:
                self.window.load_url(f"http://localhost:{security.PORT}/")
            if not self.window_visible:
                self.icon.notify("Ready — say \"Hey ARYA\".", "ARYA")
            return
        if ev.startswith("ARYA_MODE:"):
            self.settings["mode"] = ev.split(":", 1)[1]
            save_settings(self.settings)
            return
        if ev == "ARYA_MUTED":
            self.muted = True
            self._set_state("muted")
            self._overlay(status="Microphone muted", colour=colour("muted"), heard="", reply="")
            self.overlay.hide_later(2500)
            return
        if ev == "ARYA_UNMUTED":
            self.muted = False
            self._set_state("idle")
            self._overlay(status="Microphone on — say \"Hey ARYA\"", colour=colour("idle"), heard="", reply="")
            self.overlay.hide_later(2500)
            return
        if self.state == "starting":
            return
        if ev in ("ARYA_LISTENING", "ARYA_WAKE"):
            self._cancel_conversation()
            if self.state not in ("thinking", "speaking"):
                self._reply = ""
                self._set_state("listening")
                self._overlay(status="Listening…", colour=colour("listening"), heard="", reply="")
        elif ev.startswith("TRANSCRIPT:"):
            self._cancel_conversation()
            self._reply = ""
            self._set_state("thinking")
            self._overlay(status="Thinking…", colour=colour("thinking"), heard=ev[len("TRANSCRIPT:"):], reply="")
        elif ev == "ARYA_SPEAKING":
            self._set_state("speaking")
            self._overlay(status="Speaking", colour=colour("speaking"))
        elif ev == "ARYA_DONE":
            self._set_state("idle")
            if self.overlay.visible:
                self.overlay.set(status="Done", colour=colour("idle"))
                self.overlay.hide_later(4000)
        elif ev.startswith("ARYA_FOLLOWUP"):
            try:
                seconds = int(ev.split(":", 1)[1])
            except (IndexError, ValueError):
                seconds = 12
            self._set_state("conversation")
            if self.overlay.visible:
                self.overlay.set(status="In conversation — just talk", colour=colour("conversation"))
                self.overlay.hide_later(5000)
            self._conversation_job = self.root.after(seconds * 1000, self._end_conversation)
        elif ev == "ARYA_INTERRUPT":
            if self.overlay.visible:
                self.overlay.set(status="Interrupted")
                self.overlay.hide_later(1500)
        elif ev == "ARYA_CONFIRM":
            self._overlay(status="Waiting for your yes or no", colour=colour("thinking"))
            self.overlay.hide_later(15000)
        elif ev.startswith("NOTIFY:"):
            self._overlay(status="Reminder", colour=colour("speaking"), heard="", reply=ev[len("NOTIFY:"):])
            self.overlay.hide_later(8000)
        elif ev.startswith("ARYA_") or ev.startswith("__"):
            pass
        else:                                            # a sentence of ARYA's reply
            from arya.core.tts import clean_for_speech   # same clean-up as the voice: no markdown
            self._reply = (self._reply + " " + clean_for_speech(ev)).strip()
            self._set_state("speaking")
            self._overlay(status="Speaking", colour=colour("speaking"), reply=self._reply)

    def _end_conversation(self) -> None:
        self._conversation_job = None
        if self.state == "conversation":
            self._set_state("idle")

    def _cancel_conversation(self) -> None:
        if self._conversation_job is not None:
            self.root.after_cancel(self._conversation_job)
            self._conversation_job = None

    def _shutdown(self) -> None:
        self.quitting = True
        print("ARYA desktop: shutting down")
        if self._ws is not None:
            try:
                self._ws.wake_engine.stop()
            except Exception:
                pass
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            self.server_thread.join(timeout=6)
        self.icon.stop()
        if self.window is not None:
            self.window.destroy()                        # ends webview.start() on the main thread
        self.root.quit()

    def run(self) -> None:
        import webview

        try:   # own taskbar identity, so Windows shows ARYA's icon (not Python's)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
        self.icon.run_detached()
        threading.Thread(target=self._overlay_thread, name="overlay", daemon=True).start()
        self._ui_ready.wait(10)
        self.server_thread = threading.Thread(target=self._run_server, name="arya-server", daemon=True)
        self.server_thread.start()
        self._create_window()
        webview.start(gui="edgechromium", icon=str(ICON_FILE), private_mode=False,
                      storage_path=str(DATA_DIR / "webview"))
        if not self.quitting:                            # window destroyed some other way
            self.events.put("__QUIT__")
            time.sleep(1)


def _ask_running_instance_to_show() -> bool:
    """Second launch (desktop shortcut): bring the running ARYA's window forward."""
    import urllib.request

    from arya.server import security

    for _ in range(3):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{security.PORT}/api/window/show", data=b"{}", method="POST",
                headers={"X-ARYA-Token": security.TOKEN, "Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
            return True
        except Exception:
            time.sleep(1)       # still starting up — its window shows the splash already
    return False


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)
    if sys.stdout is None or os.getenv("ARYA_LOG_TO_FILE") == "1" or Path(sys.executable).name.lower() == "pythonw.exe":
        redirect_output_to_log()
    if not system.acquire_single_instance():
        print("ARYA is already running — bringing its window forward.")
        _ask_running_instance_to_show()
        return
    DesktopApp(background="--background" in argv).run()
