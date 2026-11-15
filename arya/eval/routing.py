"""
Routing benchmark (roadmap C1): does each spoken command reach the right part of ARYA?

    python -m arya.eval --routes          # just the routes (a few seconds, no model needed)
    python -m arya.eval                   # golden questions, then the routes

Each entry in routes.yaml says what you'd say and which handler must answer it ("browser",
"calendar", "study mode", … or "model" for questions that must NOT be caught by a handler). Optional:
`reply` (a piece of the answer), `action` (what ARYA must have done) and `addressed: true` (ARYA's
"is this for me?" rules must accept it without asking the model).

Everything with side effects is replaced by a recorder: no keys are pressed, no tabs or links opened,
no windows moved, no timers started, no mail fetched, no screenshot taken. History, exams, events and
study sessions go to a throwaway database (set up by arya.eval before ARYA is imported).
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Recorder:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def add(self, kind: str, *args):
        self.actions.append(kind)
        return args


def _sandbox(rec: Recorder) -> None:
    """Replace every side effect with the recorder (module attributes, for the whole run)."""
    import arya.core.llm as llm
    import arya.core.llm_local as llm_local
    import arya.tools.os_tools as os_tools
    from arya.core import audit
    from arya.study import session
    from arya.tools import agenda, browser, file_agent, media, music, productivity, screen, windows

    tmp = Path(tempfile.mkdtemp(prefix="arya_routes_"))
    audit.LOG_DIR, audit.LOG_FILE = tmp, tmp / "actions.log"
    agenda.ICS_DIR = tmp / "calendar"
    file_agent._undo_file = lambda: tmp / "tidy-undo.json"
    for key in ("ARYA_MAIL_HOST", "ARYA_MAIL_USER", "ARYA_MAIL_PASSWORD", "ARYA_FOCUS_MUSIC_URL"):
        os.environ.pop(key, None)

    browser._chrome_window = lambda: 1
    browser._focus = lambda hwnd: True
    browser._press = lambda *keys: rec.add("keys")

    async def first_video(q):
        return "VIDEOID0000"
    browser.first_video = first_video

    async def open_url(url):
        rec.add("open_url")
        return f"Opened {url}"

    async def open_app(name):
        rec.add("open_app")
        return f"Opened {name}"
    os_tools.open_url, os_tools.open_app = open_url, open_app
    os.startfile = lambda *a, **k: rec.add("startfile")
    session.open_folder = lambda path: rec.add("open_folder")

    windows._app_windows = lambda app: [1]
    windows._foreground = lambda: 1
    windows._show = lambda hwnds, mode: rec.add("window")
    windows._show_desktop = lambda: rec.add("desktop")
    media._press = lambda vk, times=1: rec.add("media_key")

    async def no_media():
        raise RuntimeError("no media session in the routing run")
    music._manager = no_media

    async def pomodoro(sessions=1, label="Study"):
        rec.add("pomodoro")
        return "Pomodoro started"

    async def cancel_timer(label=None):
        rec.add("cancel_timer")
        return "Cancelled"
    productivity.pomodoro, productivity.cancel_timer = pomodoro, cancel_timer
    screen.capture = lambda: None

    # The model is not under test here: a stub that answers instantly, so a mis-route shows up as "model".
    async def stream_tokens(model, messages, max_tokens=None):
        yield "Model answer."

    async def tool_pipeline(*a, **k):
        return "Tool answer."

    async def complete(*a, **k):
        import types
        return types.SimpleNamespace(content="Model answer.", tool_calls=None)

    async def chat_local(*a, **k):
        return {"content": "Model answer."}
    llm._stream_tokens, llm._tool_pipeline, llm._complete = stream_tokens, tool_pipeline, complete
    llm_local.chat_local = chat_local
    llm.set_artifact_sink(lambda kind, payload: None)


def _reset() -> None:
    import arya.core.llm as llm
    from arya.core import policy
    from arya.study import practice, tutor
    from arya.tools import screen

    policy.cancel_pending()
    practice._quiz = None
    tutor._session = None
    llm._last_exercise["until"] = 0.0
    screen._context.update(text="", until=0.0)


def route_of(output: str) -> str:
    from arya.report import _ROUTES

    for line in output.splitlines():
        for pattern, name in _ROUTES:
            m = pattern.match(line.strip())
            if m:
                return name(m)
    return "(none)"


async def _turn(text: str) -> tuple[str, str]:
    import arya.core.llm as llm

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reply = "".join([s async for s in llm.stream_with_tools(text)])
    return route_of(buf.getvalue()), reply


def run(verbose: bool = False) -> tuple[int, int, list[str]]:
    import yaml

    from arya.core import addressee

    from arya.core.embedder import load_embedder
    from arya.knowledge import library

    load_embedder()                                    # warm like the running app: a cold library lookup
    library.relevant("warm up")                        # takes ~12 s and would hit the handlers' timeouts
    rec = Recorder()
    _sandbox(rec)
    items = yaml.safe_load((HERE / "routes.yaml").read_text(encoding="utf-8"))["routes"]
    passed, failures = 0, []
    for i, item in enumerate(items, 1):
        _reset()
        rec.actions.clear()
        say = item["say"]
        try:
            route, reply = asyncio.run(_turn(say))
        except Exception as exc:
            route, reply = "(error)", f"{type(exc).__name__}: {exc}"
        problems = []
        expected = item["route"]
        if expected == "model" and route in ("model", "model + tools"):
            expected = route                                   # "not a handler": either model path is right
        if route != expected:
            problems.append(f"went to {route!r}, expected {item['route']!r}")
        if item.get("reply") and item["reply"].lower() not in reply.lower():
            problems.append(f"reply lacks {item['reply']!r}")
        if item.get("action") and item["action"] not in rec.actions:
            problems.append(f"no {item['action']} action (did: {rec.actions or 'nothing'})")
        if item.get("addressed") and addressee.quick_verdict(say) is not True:
            problems.append("ARYA's rules don't treat it as a command for ARYA")
        ok = not problems
        passed += ok
        line = f"{i:3}. {'PASS' if ok else 'FAIL'}  {say[:55]:55}  → {route}"
        if not ok:
            failures.append(f"{say!r}: " + "; ".join(problems))
            line += "  — " + "; ".join(problems)
        if verbose or not ok:
            print(line + (f"\n        {reply[:150]}" if verbose else ""))
    _reset()
    return passed, len(items), failures
