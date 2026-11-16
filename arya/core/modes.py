"""
Modes (roadmap: from study tutor to personal assistant). A mode decides what ARYA is like and what it
reaches for first; it never limits what you can ask.

  default  a warm, quick-witted personal assistant: your day, plans, ideas, music, life admin, the laptop
  study    today's tutor: course material first, Socratic, exam-minded; cards and pomodoros
  work     a concise technical peer for research / lab work and your software projects

Voice: "work mode", "study mode", "back to normal" (or "default mode"), "what mode are you in?",
"add <folder> to work mode", "which folders are in work mode?". The app and the phone page have a switch.
Starting a study session switches to study mode; ending it goes back to the mode you were in.

Suggestions: when your last requests clearly belong to another mode, ARYA asks once ("Sounds like work —
switch to work mode?"); it never switches by itself, never during a study session, and asks again about
the same mode only after 30 minutes. The judgement is a nearest-prototype vote with the embedder ARYA
already loads (all-MiniLM), over the last three requests — no extra model.

State lives in %LOCALAPPDATA%\\ARYA\\modes.json.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

NAMES = ("default", "study", "work", "brainstorm")
SPOKEN = {"default": "normal", "study": "study", "work": "work", "brainstorm": "brainstorm"}


@dataclass(frozen=True)
class Mode:
    name: str
    persona: str                     # the personality / focus part of the system prompt
    capabilities: str                # "What can you do?"
    floors: dict = field(default_factory=dict)   # retrieval: minimum similarity per source (missing = never)
    nudges: frozenset = frozenset()   # which proactive nudges may speak
    briefing: str = ""               # what the morning briefing should cover


_COMMON_CAPS = ("I keep your calendar, check your email, remember what you tell me, run your own routines, set "
                "timers, alarms and reminders, take notes and keep a to-do list, find and tidy your files, check the "
                "weather and battery, open apps, control Chrome tabs, search Google or YouTube, and play music. "
                "Say \"study mode\", \"work mode\" or \"back to normal\" to change how I work. Everything runs on "
                "this laptop unless you say \"ask the cloud\".")

MODES: dict[str, Mode] = {
    "default": Mode(
        name="default",
        persona=(
            "Mode: everyday. You are their personal assistant and companion, like a brilliant, warm friend who "
            "happens to run their laptop: their day, plans, ideas, music, people, errands and the PC, not only "
            "their studies. Be genuinely useful first. You are quick-witted: a light joke or a friendly tease "
            "now and then is welcome, but never at the expense of the answer and never more than one line. "
            "Don't steer the conversation towards coursework, exams or studying unless they bring it up. "
            "Have an opinion when asked. When it fits, end with one short natural follow-up question; not every "
            "time. Never lecture.\n"
        ),
        capabilities="I'm your personal assistant. " + _COMMON_CAPS
        + " In study mode I'm your tutor for your courses; in work mode a concise partner for research and code.",
        floors={"notes": "base", "uploads": "base", "core": "base", "course": 0.55, "work": 0.55},
        nudges=frozenset({"battery", "calendar", "break"}),
        briefing="the time, the weather, today's calendar, reminders and timers, and any to-dos",
    ),
    "study": Mode(
        name="study",
        persona=(
            "Mode: study. They are studying for their Mechatronics engineering courses and you are their tutor: "
            "explain in connected sentences, like a tutor talking; ground answers in their course excerpts; check "
            "understanding with a short question when it helps. Precision comes first: the complete, exact answer "
            "with the proper technical terms and conditions. After that you may add at most one short playful or "
            "caring line when it fits, for example when they are up late, stressed, or just got something right.\n"
        ),
        capabilities=(
            "I'm your study companion: I answer from your own course material and show you the page, explain "
            "exercises by number, plot responses, draw block diagrams and do exact maths; say \"start studying\" "
            "and a course for pomodoros, and I quiz you on practice cards and run timed mock exams. "
            + _COMMON_CAPS),
        floors={"notes": "base", "uploads": "base", "core": "base", "course": "base", "work": 0.55},
        nudges=frozenset({"battery", "calendar", "break", "cards"}),
        briefing="the time, the weather, the study line (next exam, cards due), today's calendar and timers",
    ),
    "work": Mode(
        name="work",
        persona=(
            "Mode: work. You are a concise, technical peer for their research and lab work and their software "
            "projects. Answer directly with precise terms and specifics: names, numbers, file names, next steps. "
            "No small talk, no jokes unless they joke first. When an answer comes from their work documents, say "
            "which one. For substantial code or debugging, say briefly that the local model is limited for code "
            "and offer to ask the cloud.\n"
        ),
        capabilities="I'm your work partner: I answer from your work folders (papers, notes, reports), keep your "
                     "work tasks, prepare you for meetings from your calendar, and keep answers short and "
                     "technical. " + _COMMON_CAPS,
        floors={"notes": "base", "uploads": "base", "work": "base", "core": "base", "course": 0.55},
        nudges=frozenset({"battery", "calendar", "break"}),
        briefing="the time, the weather, today's meetings, open to-dos and unread mail that looks important",
    ),
    "brainstorm": Mode(
        name="brainstorm",
        persona=(
            "Mode: brainstorm. You are an expert brainstorming partner. First go wide: give three to five "
            "short, clearly different ideas, from practical to bold, each in one sentence (say 'First', "
            "'Second' instead of a list). Build on the user's ideas ('yes, and'), never shoot them down early, "
            "and end with one provocative question that opens a new angle. When ideas run dry, use a method "
            "and name it briefly: SCAMPER (substitute, combine, adapt, modify, put to another use, eliminate, "
            "reverse), first principles, reversing the problem, borrowing from another field, or adding a "
            "constraint. When they ask to narrow down or pick, switch to converging: two or three criteria, the "
            "best two or three ideas with one reason each, and the very next step. Mention that they can say "
            "'save that idea' to keep one.\n"
        ),
        capabilities="I'm your brainstorming partner: I throw out varied ideas, build on yours, ask questions "
                     "that open new angles, use methods like SCAMPER and first principles when we're stuck, and "
                     "help you narrow down to a next step when you're ready. Say \"save that idea\" to keep one. "
                     + _COMMON_CAPS,
        floors={"notes": "base", "uploads": "base", "core": "base", "course": 0.55, "work": 0.50},
        nudges=frozenset({"battery", "calendar"}),
        briefing="the time, the weather, today's calendar, reminders and timers, and any to-dos",
    ),
}


# ── State ─────────────────────────────────────────────────────────────────────

def _file() -> Path:
    from arya.knowledge.library import DATA_DIR
    return DATA_DIR / "modes.json"


_lock = threading.Lock()
_listeners: list[Callable[[str], None]] = []


_mem: dict | None = None          # evals and tests: state in memory, the saved mode is never touched


def use_memory(mode: str = "default") -> None:
    """For evals and tests: from now on keep the state in memory, starting in `mode`."""
    global _mem
    _mem = {"mode": mode}
    _recent.clear()


def _load() -> dict:
    if _mem is not None:
        return json.loads(json.dumps(_mem))
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    global _mem
    if _mem is not None:
        _mem = json.loads(json.dumps(data))
        return
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=1), encoding="utf-8")


def current_name() -> str:
    name = _load().get("mode", "default")
    return name if name in MODES else "default"


def current() -> Mode:
    return MODES[current_name()]


def on_change(callback: Callable[[str], None]) -> None:
    """The server: tell the app and the phone ('ARYA_AMODE:work')."""
    _listeners.append(callback)


def set_mode(name: str, *, reason: str = "") -> bool:
    """True if it changed."""
    if name not in MODES:
        raise ValueError(name)
    with _lock:
        data = _load()
        if data.get("mode", "default") == name:
            return False
        data["mode"] = name
        _save(data)
    print(f"Mode: {name}" + (f" ({reason})" if reason else ""))
    _recent.clear()
    for cb in list(_listeners):
        try:
            cb(name)
        except Exception as exc:
            print(f"Mode listener error: {exc}")
    return True


def enter_study() -> None:
    """A study session started: study mode, remembering where to go back to."""
    with _lock:
        data = _load()
        before = data.get("mode", "default")
        if before != "study":
            data["before_study"] = before
            _save(data)
    set_mode("study", reason="study session")


def leave_study() -> None:
    """The study session ended: back to the mode from before it (if still in study mode)."""
    with _lock:
        data = _load()
        back = data.pop("before_study", None)
        _save(data)
    if back and back in MODES and current_name() == "study":
        set_mode(back, reason="study session ended")


# ── Work folders ──────────────────────────────────────────────────────────────

def work_dirs() -> list[Path]:
    env = [Path(p).expanduser() for p in os.getenv("ARYA_WORK_DIRS", "").split(os.pathsep) if p.strip()]
    saved = [Path(p) for p in _load().get("work_dirs", [])]
    return list(dict.fromkeys(env + saved))


def add_work_dir(path: Path) -> None:
    with _lock:
        data = _load()
        dirs = data.setdefault("work_dirs", [])
        if str(path) not in dirs:
            dirs.append(str(path))
        _save(data)


def _resolve_folder(spoken: str) -> Path | None:
    """'my PycharmProjects folder', 'documents', 'C:\\Lab' -> an existing folder in the user's sandbox."""
    from arya.core.policy import PolicyError, check_path, resolve_user_path
    name = re.sub(r"^(?:the|my)\s+|\s+(?:folder|directory)$", "", spoken.strip(" .\"'"), flags=re.IGNORECASE).strip()
    candidates = []
    try:
        candidates.append(resolve_user_path(name))
    except Exception:
        pass
    home = Path.home()
    for base in (home, home / "Documents", home / "OneDrive", home / "OneDrive" / "Documents", home / "Desktop",
                 home / "OneDrive" / "Desktop"):
        candidates.append(base / name)
    for c in candidates:
        try:
            if c.is_dir():
                return check_path(c)
        except (OSError, PolicyError):
            continue
    return None


# ── Retrieval ─────────────────────────────────────────────────────────────────

def source_of(path: str, dirs: list[Path] | None = None) -> str:
    """Where a library passage comes from: 'core' / 'notes' / 'uploads' / 'work' / 'course'.
    Pass `dirs` (work_dirs()) when classifying many paths at once."""
    from arya.knowledge import library as L
    p = str(path)
    if p == str(L.CORE_NOTES):
        return "core"
    if p in (str(L.CORRECTIONS), str(L.PHOTO_NOTES), str(L.TRANSCRIPTIONS)):
        return "notes"
    if p.startswith(str(L.DATA_DIR / "uploads")):
        return "uploads"
    for d in (work_dirs() if dirs is None else dirs):
        try:
            Path(p).relative_to(d)
            return "work"
        except ValueError:
            continue
    return "course"


def floor(source: str, mode: str | None = None) -> float | None:
    """Minimum similarity for a passage from `source` to be used in `mode` (None = never)."""
    return MODES[mode or current_name()].floors.get(source)


SOURCE_LABEL = {"course": "their course material", "work": "their work documents", "notes": "their own notes",
                "uploads": "files they gave you", "core": "ARYA's core notes"}


# ── Suggestions ───────────────────────────────────────────────────────────────

PROTOTYPES = {
    "study": ["explain this exercise from the lecture", "help me prepare for my exam", "quiz me on my flashcards",
              "what is the transfer function of a second order system", "solve this dynamics problem step by step",
              "what does the Coriolis acceleration mean", "summarise the chapter on control loops for my exam"],
    "work": ["debug this python error in my code", "summarise this research paper", "plan the lab experiment",
             "write an email to my supervisor about the results", "refactor this function", "analyse the "
             "measurement data", "prepare for the group meeting", "why does my script crash", "review my pull "
             "request", "what are the next steps in my project"],
    "brainstorm": ["give me ideas for a project name", "help me think of ways to market my app", "what are some "
                   "creative ideas for a birthday gift", "let's come up with possible features for the robot",
                   "I need fresh ideas for my presentation", "what could we try instead", "think of unusual "
                   "uses for a drone"],
    "default": ["how was your day", "recommend a movie for tonight", "what should I cook for dinner", "tell me a "
                "joke", "plan my weekend", "remind me to call my mom", "I'm bored", "what do you think about "
                "travelling to Italy", "good morning how are you", "let's chat about music"],
}
SUGGEST_MIN = 0.45            # the winning prototype must be at least this similar
SUGGEST_MARGIN = 0.08         # … and beat the current mode by this much
SUGGEST_AGAIN_S = 30 * 60
_recent: deque = deque(maxlen=3)
_proto_vecs: dict = {}


def _prototype_vectors():
    if not _proto_vecs:
        import numpy as np
        from arya.core.embedder import embed_sync
        for mode, phrases in PROTOTYPES.items():
            _proto_vecs[mode] = np.vstack([embed_sync(p) for p in phrases])
    return _proto_vecs


def scores(message: str) -> dict[str, float]:
    from arya.core.embedder import embed_sync
    vec = embed_sync(message)
    return {mode: float((m @ vec).max()) for mode, m in _prototype_vectors().items()}


def suggestion(message: str, now: float | None = None) -> str | None:
    """After a request: the mode to suggest switching to, or None. Needs 2 of the last 3 requests to agree."""
    from arya.study import session
    if len(message.split()) < 3 or _CMD.search(message) or session.current() is not None:
        return None
    now = now or time.time()
    s = scores(message)
    here = current_name()
    best = max(s, key=s.get)
    _recent.append(best if best != here and s[best] >= SUGGEST_MIN and s[best] - s[here] >= SUGGEST_MARGIN else None)
    votes = [m for m in _recent if m]
    if not votes:
        return None
    winner = max(set(votes), key=votes.count)
    if votes.count(winner) < 2:
        return None
    data = _load()
    asked = data.get("suggested", {})
    if winner in asked and now - float(asked[winner]) < SUGGEST_AGAIN_S:
        return None
    asked[winner] = now
    data["suggested"] = asked
    _save(data)
    _recent.clear()
    return winner


def suggestion_question(mode: str) -> str:
    return {"work": "Sounds like work. Want me to switch to work mode?",
            "study": "Sounds like studying. Switch to study mode?",
            "brainstorm": "Sounds like we're brainstorming. Switch to brainstorm mode?",
            "default": "Shall I switch back to normal mode?"}[mode]


# ── Voice ─────────────────────────────────────────────────────────────────────

_MODE_WORD = r"(?P<m>study(?:ing)?|work(?:ing)?|brainstorm(?:ing)?|normal|default|everyday|regular|casual|personal)"
_SWITCH = re.compile(rf"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:(?:please|can you|could you|let'?s)\s+)?"
                     rf"(?:(?:switch|go|change|get|put yourself)\s+(?:back\s+)?(?:in)?to\s+|enter\s+|back\s+to\s+|"
                     rf"(?:turn on|start)\s+)?(?:the\s+)?{_MODE_WORD}\s+mode(?:\s+(?:on|please|now))?\W*$", re.IGNORECASE)
_BACK = re.compile(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:(?:go|switch|get)\s+)?back\s+to\s+normal(?:\s+mode)?\W*$|"
                   r"^\W*(?:leave|exit|stop|turn off)\s+(?:the\s+)?(?:study|work|brainstorm(?:ing)?)\s+mode\W*$",
                   re.IGNORECASE)
# "Let's brainstorm names for my robot" — switch to brainstorm mode and answer it there (not an instant reply).
_BRAINSTORM_START = re.compile(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:let'?s|can we|could we|help me|i want to|"
                               r"i'?d like to)\s+brainstorm\b|\bbrainstorm with me\b", re.IGNORECASE)


def auto_switch(message: str) -> bool:
    """'Let's brainstorm X': into brainstorm mode before the model answers X. True if it switched."""
    if _BRAINSTORM_START.search(message) and current_name() != "brainstorm":
        return set_mode("brainstorm", reason="let's brainstorm")
    return False
# "what mode are you in?" — not "what mode of vibration…"
_WHICH = re.compile(r"\b(?:what|which)\s+mode\s+are\s+you\b|\bwhat(?:'s| is)\s+your\s+(?:current\s+)?mode\b|"
                    r"\bwhich mode is (?:on|active)\b", re.IGNORECASE)
_ADD_DIR = re.compile(r"\badd\s+(?P<f>.+?)\s+to\s+(?:the\s+)?work\s+(?:mode|folders?)\b", re.IGNORECASE)
_LIST_DIRS = re.compile(r"\b(?:which|what)\s+folders?\b.*\bwork\b|\bwork\s+folders?\b\W*$", re.IGNORECASE)
_CMD = re.compile(rf"{_SWITCH.pattern}|{_BACK.pattern}", re.IGNORECASE)
_NORMALISE = {"studying": "study", "working": "work", "brainstorming": "brainstorm", "normal": "default",
              "everyday": "default",
              "regular": "default", "casual": "default", "personal": "default"}


def matches(message: str) -> bool:
    return bool(_SWITCH.match(message) or _BACK.match(message) or _WHICH.search(message)
                or _ADD_DIR.search(message) or _LIST_DIRS.search(message))


def _switched_reply(name: str) -> str:
    from arya.study import session
    if name == "work":
        extra = "" if work_dirs() else " Tell me your work folders with \"add <folder> to work mode\"."
        return "Work mode. Short and technical from here." + extra
    if name == "study":
        return "Study mode. I'm your tutor again; say \"start studying\" and a course for a session."
    if name == "brainstorm":
        return "Brainstorm mode. No bad ideas yet. What are we thinking about?"
    tail = " Your study session keeps running." if session.current() is not None else ""
    return "Back to normal. What's up?" + tail


def handle(message: str) -> str | None:
    m = _ADD_DIR.search(message)
    if m:
        folder = _resolve_folder(m.group("f"))
        if folder is None:
            return f"I couldn't find a folder called {m.group('f').strip()} in your user folder."
        add_work_dir(folder)
        from arya.knowledge import library
        library.start_background_indexing()
        return f"Added {folder.name} to work mode. I'm reading its documents in the background."
    if _LIST_DIRS.search(message):
        dirs = work_dirs()
        if not dirs:
            return "No work folders yet. Say \"add\" and a folder name, \"to work mode\"."
        return "Work mode uses: " + ", ".join(d.name for d in dirs) + "."
    if _WHICH.search(message) and not _SWITCH.match(message):
        return f"I'm in {SPOKEN[current_name()]} mode."
    if _BACK.match(message):
        set_mode("default", reason="asked")
        return _switched_reply("default")
    m = _SWITCH.match(message)
    if m:
        word = m.group("m").lower()
        name = _NORMALISE.get(word, word)
        if not set_mode(name, reason="asked"):
            return f"I'm already in {SPOKEN[name]} mode."
        return _switched_reply(name)
    return None
