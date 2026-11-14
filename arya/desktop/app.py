"""
ARYA desktop app — no browser, no console window.

  * Tray icon: its colour shows what ARYA is doing (starting, ready, listening,
    thinking, speaking, in conversation, muted). Right-click for the menu:
    open the chat page, mute the microphone, privacy mode (offline / cloud),
    status overlay on/off, start with Windows, open the action log, quit.
  * Status overlay: a small always-on-top card above the taskbar that appears
    when you talk to ARYA — what it heard, what it's saying — then fades away.
    Click it to dismiss.
  * The voice server runs in a background thread of this same process.

Run:  .venv\\Scripts\\pythonw.exe arya_desktop.py   (or tick "Start with Windows")
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

# state -> (colour, tooltip)
STATES = {
    "starting": ("#94a3b8", "ARYA — starting up…"),
    "idle": ("#475569", "ARYA — say \"Hey ARYA\""),
    "listening": ("#22c55e", "ARYA — listening"),
    "thinking": ("#f59e0b", "ARYA — thinking"),
    "speaking": ("#3b82f6", "ARYA — speaking"),
    "conversation": ("#14b8a6", "ARYA — in conversation (just talk)"),
    "muted": ("#ef4444", "ARYA — microphone muted"),
    "failed": ("#7f1d1d", "ARYA — failed to start (see log)"),
}

BG, FG, MUTED_FG = "#0f172a", "#f8fafc", "#94a3b8"


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
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 2, size - 2), fill=colour)
    try:
        font = ImageFont.truetype("segoeuib.ttf", 38)
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), "A", font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), "A", font=font, fill="white")
    return img


# ── Status overlay ────────────────────────────────────────────────────────────

class Overlay:
    WIDTH = 380

    def __init__(self, root: tk.Tk):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.0)
        root.configure(bg=BG)
        card = tk.Frame(root, bg=BG, padx=16, pady=12, highlightthickness=1, highlightbackground="#334155")
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
    def __init__(self) -> None:
        self.settings = load_settings()
        self.events: queue.Queue[str] = queue.Queue()
        self.state = "starting"
        self.muted = False
        self.quitting = False
        self.server = None
        self.server_thread: threading.Thread | None = None
        self._reply = ""
        self._conversation_job = None
        self._icons = {name: make_icon(colour) for name, (colour, _) in STATES.items()}

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on scaled displays
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("ARYA")
        self.overlay = Overlay(self.root)
        self.icon = self._build_tray()

    # ── tray ──
    def _build_tray(self):
        import pystray
        from pystray import Menu, MenuItem as Item

        def mode_is(mode):
            return lambda _item: self._current_mode() == mode

        menu = Menu(
            Item("Open ARYA", self._open_page, default=True),
            Item("Mute microphone", self._toggle_mute, checked=lambda _i: self.muted),
            Menu.SEPARATOR,
            Item("Privacy", Menu(
                Item("Offline — everything stays on this laptop", lambda: self._set_mode("offline"),
                     checked=mode_is("offline"), radio=True),
                Item("Cloud — Groq first (faster, less private)", lambda: self._set_mode("cloud"),
                     checked=mode_is("cloud"), radio=True),
            )),
            Item("Show status overlay", self._toggle_overlay, checked=lambda _i: self.settings["overlay"]),
            Item("Start with Windows", self._toggle_autostart, checked=lambda _i: system.autostart_enabled()),
            Item("Open action log", self._open_action_log),
            Menu.SEPARATOR,
            Item("Quit ARYA", self._quit),
        )
        colour, tip = STATES["starting"]
        return pystray.Icon("ARYA", self._icons["starting"], tip, menu)

    def _set_state(self, state: str) -> None:
        if self.muted and state not in ("failed",):
            state = "muted"
        self.state = state
        self.icon.icon = self._icons[state]
        self.icon.title = STATES[state][1]

    # Menu callbacks run on pystray's thread → hand work to the Tk thread.
    def _open_page(self, *_):
        from arya.server import security
        webbrowser.open(f"http://localhost:{security.PORT}")

    def _toggle_mute(self, *_):
        self.events.put("__TOGGLE_MUTE__")

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
        import arya.core.llm as llm
        llm.ARYA_MODE = mode
        self.settings["mode"] = mode
        save_settings(self.settings)
        print(f"Privacy mode switched to {mode}")
        if mode == "cloud" and not os.getenv("GROQ_API_KEY"):
            self.icon.notify("No GROQ_API_KEY in .env — ARYA will keep answering locally.", "ARYA")

    def _quit(self, *_):
        self.events.put("__QUIT__")

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

    # ── events (Tk thread) ──
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        if not self.quitting:
            self.root.after(50, self._poll)

    def _overlay(self, **kw) -> None:
        if self.settings["overlay"]:
            self.overlay.set(**kw)
            self.overlay.show()

    def _handle(self, ev: str) -> None:
        colour = lambda s: STATES[s][0]
        if ev == "__QUIT__":
            return self._shutdown()
        if ev == "__TOGGLE_MUTE__":
            ws = getattr(self, "_ws", None)
            if ws is not None and ws.set_microphone_muted(not self.muted):
                return
            return
        if ev == "__HIDE_OVERLAY__":
            return self.overlay.hide()
        if ev == "__FAILED__":
            self._set_state("failed")
            self.icon.notify("ARYA couldn't start — usually low memory. See the log in %LOCALAPPDATA%\\ARYA\\logs.",
                             "ARYA")
            return
        if ev == "ARYA_READY":
            self._set_state("idle")
            self.icon.notify("Ready — say \"Hey ARYA\".", "ARYA")
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
        elif ev.startswith("NOTIFY:"):
            self._overlay(status="Reminder", colour=colour("speaking"), heard="", reply=ev[len("NOTIFY:"):])
            self.overlay.hide_later(8000)
        elif ev.startswith("ARYA_"):
            pass                                         # other control messages
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
        ws = getattr(self, "_ws", None)
        if ws is not None:
            try:
                ws.wake_engine.stop()
            except Exception:
                pass
        if self.server is not None:
            self.server.should_exit = True
        if self.server_thread is not None:
            self.server_thread.join(timeout=6)
        self.icon.stop()
        self.root.destroy()

    def run(self) -> None:
        self.icon.run_detached()
        self.server_thread = threading.Thread(target=self._run_server, name="arya-server", daemon=True)
        self.server_thread.start()
        self.root.after(50, self._poll)
        self.root.mainloop()


def main() -> None:
    os.chdir(ROOT)
    if sys.stdout is None or os.getenv("ARYA_LOG_TO_FILE") == "1" or Path(sys.executable).name.lower() == "pythonw.exe":
        redirect_output_to_log()
    if not system.acquire_single_instance():
        print("ARYA is already running — opening it instead of starting a second copy.")
        from arya.server import security
        webbrowser.open(f"http://localhost:{security.PORT}")
        return
    DesktopApp().run()
