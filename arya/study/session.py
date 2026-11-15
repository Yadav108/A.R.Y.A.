"""
Study mode (Phase B): one sentence sets up a whole study session.

"ARYA, start studying Dynamics" / "let's study control for an hour":
  - opens the course folder in Explorer,
  - starts pomodoros (25 min focus + 5 min break) for the requested time (default: one hour),
  - says what this session should cover: cards due, new cards, the next exercise — and the exam countdown.
"How long have I been studying?" -> time so far and the cards answered.
"Stop studying" / "I'm done studying" -> ends the pomodoros (and a running quiz) with a short summary.

Sessions are stored in SQLite (arya.db), so a restart doesn't lose the one in progress.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import sqlite3
import time

DEFAULT_MINUTES = 60
POMODORO_MINUTES = 30                       # 25 focus + 5 break
MAX_SESSION_H = 12                          # an older open session is treated as forgotten

_START = re.compile(
    r"\b(?:start|begin)\s+(?:a\s+)?(?:studying|study(?:ing)?\s+(?:session|mode)|revising|revision)\b|"
    r"\b(?:start|begin)\s+(?:to\s+)?study\b|\bstudy mode\b", re.IGNORECASE)
# "let's study", "time to revise": study mode only when nothing but a course / length / "now" follows —
# "I want to study the Coriolis effect" is a question, not a session.
_START_SOFT = re.compile(r"\b(?:let'?s|let us|time to|i(?:'m| am) going to|i want to|i'?d like to)\s+"
                         r"(?:study|revise|do some studying)\b(?P<rest>.*)$", re.IGNORECASE)
_FILLER = re.compile(r"\b(?:now|today|tonight|a bit|a little|some|for|please|then|together|with you|arya|"
                     r"an?|one|two|three|four|five|six|\d+(?:\.\d+)?|and|half|hours?|hrs?|minutes?|mins?|"
                     r"pomodoros?|sessions?|the|my|course|material)\b", re.IGNORECASE)
_END = re.compile(
    r"\b(?:stop|end|finish|quit)\s+(?:the\s+|my\s+)?(?:studying|study(?:ing)?\s+(?:session|mode)|revision)\b|"
    r"\bi'?m\s+done\s+(?:studying|with\s+(?:studying|the\s+study\s+session))\b|"
    r"\b(?:done|finished)\s+studying\b|\bexit study mode\b", re.IGNORECASE)
_STATUS = re.compile(r"\bhow long have i (?:been )?(?:studying|studied)\b|\bhow(?:'s| is) my study session\b",
                     re.IGNORECASE)
_LENGTH = re.compile(r"\bfor\s+(half\s+an\s+hour|an\s+hour\s+and\s+a\s+half)\b|"          # checked first
                     r"\bfor\s+(?:(an?|one|two|three|four|five|six|\d+(?:\.\d+)?)\s+(?:and\s+a\s+half\s+)?)?"
                     r"(hours?|hrs?|minutes?|mins?|pomodoros?|sessions?)\b", re.IGNORECASE)
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


# ── Storage ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS study_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "course TEXT NOT NULL, started REAL NOT NULL, planned_min INTEGER, ended REAL)")
    return conn


def current() -> sqlite3.Row | None:
    """The session in progress, if any."""
    with _db() as c:
        row = c.execute("SELECT * FROM study_sessions WHERE ended IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row and time.time() - row["started"] > MAX_SESSION_H * 3600:
            c.execute("UPDATE study_sessions SET ended = started + planned_min * 60 WHERE id = ?", (row["id"],))
            return None
        return row


def _start_row(course: str, minutes: int) -> None:
    with _db() as c:
        c.execute("UPDATE study_sessions SET ended = ? WHERE ended IS NULL", (time.time(),))
        c.execute("INSERT INTO study_sessions (course, started, planned_min) VALUES (?, ?, ?)",
                  (course, time.time(), minutes))


def _end_row(row_id: int) -> None:
    with _db() as c:
        c.execute("UPDATE study_sessions SET ended = ? WHERE id = ?", (time.time(), row_id))


# ── Pieces ────────────────────────────────────────────────────────────────────

def minutes_in(message: str) -> int:
    """'for an hour' -> 60, 'for 90 minutes' -> 90, 'for two pomodoros' -> 60, nothing -> DEFAULT_MINUTES."""
    m = _LENGTH.search(message)
    if not m:
        return DEFAULT_MINUTES
    if m.group(1):
        return 30 if m.group(1).lower().startswith("half") else 90
    raw, unit = (m.group(2) or "one").lower(), m.group(3).lower()
    n = float(_WORDS.get(raw, raw)) if raw in _WORDS or re.fullmatch(r"\d+(?:\.\d+)?", raw) else 1.0
    if "and a half" in m.group(0).lower():
        n += 0.5
    if unit.startswith("h"):
        return int(n * 60)
    if unit.startswith(("p", "s")):
        return int(n * POMODORO_MINUTES)
    return int(n)


def course_folder(course: str) -> str | None:
    """The folder holding most of this course's documents in the library."""
    from arya.knowledge import library

    with library._lock, library._connect() as c:
        paths = [r["path"] for r in c.execute("SELECT path FROM documents WHERE lower(collection) = ?",
                                              (course.lower(),)) if r["path"]]
    dirs = [os.path.dirname(p) for p in paths if os.path.isabs(p)]
    if not dirs:
        return None
    try:
        folder = os.path.commonpath(dirs)
    except ValueError:                                      # different drives
        folder = max(set(dirs), key=dirs.count)
    return folder if os.path.isdir(folder) else None


def open_folder(path: str) -> None:
    os.startfile(path)                                      # Explorer; replaced in tests


def wants_session(message: str) -> bool:
    if _START.search(message):
        return True
    m = _START_SOFT.search(message)
    if not m:
        return False
    from arya.study import planner

    rest = m.group("rest")
    course = planner.course_in(message)
    if course:
        rest = re.sub(re.escape(course), " ", rest, flags=re.IGNORECASE)
    return not re.search(r"[A-Za-zÄÖÜäöüß]{2,}", _FILLER.sub(" ", rest))


def _which_course(message: str) -> str | None:
    from arya.study import planner

    course = planner.course_in(message)
    if course:
        return course
    exams = planner.upcoming()
    return exams[0][0] if exams else None


def _session_plan(course: str) -> str:
    from arya.study import planner, practice

    practice.build_cards(course)
    due, new = practice.due_count(course), practice.new_count(course)
    steps = []
    if due:
        steps.append(f"review the {due} card{'s' if due != 1 else ''} due")
    if new:
        k = min(new, practice.NEW_PER_SESSION)
        steps.append(f"practise {k} new one{'s' if k != 1 else ''}")
    ex = planner._next_exercise(course)
    if ex:
        steps.append(f"then work through exercise {ex} with me")
    parts = []
    for crs, when in planner.upcoming():
        if crs.lower() == course.lower():
            parts.append(f"Your {course} exam is {planner._in_days(planner._days(when))}.")
            break
    if steps:
        parts.append("This session: " + ", ".join(steps) + ". Say \"quiz me\" when you're ready.")
    else:
        parts.append("No practice cards for this course yet, so ask me about the material or an exercise by number.")
    return " ".join(parts)


def _answered_since(started: float) -> tuple[int, int]:
    from arya.study import practice

    with practice._db() as c:
        r = c.execute("SELECT COUNT(*) n, SUM(quality >= 3) ok FROM reviews WHERE ts >= ?", (started,)).fetchone()
    return r["n"] or 0, r["ok"] or 0


def _spoken_minutes(minutes: float) -> str:
    m = int(round(minutes))
    if m < 1:
        return "less than a minute"
    h, m = divmod(m, 60)
    parts = ([f"{h} hour{'s' if h != 1 else ''}"] if h else []) + ([f"{m} minute{'s' if m != 1 else ''}"] if m else [])
    return " and ".join(parts)


def _so_far(row: sqlite3.Row) -> str:
    n, ok = _answered_since(row["started"])
    text = f"{_spoken_minutes((time.time() - row['started']) / 60)} of {row['course']}"
    if n:
        text += f", {n} card{'s' if n != 1 else ''} answered, {ok} right"
    return text


# ── Voice ─────────────────────────────────────────────────────────────────────

async def handle(message: str) -> str | None:
    """ARYA's reply for starting, checking or ending a study session, else None."""
    from arya.tools import productivity

    if _END.search(message):
        row = await asyncio.to_thread(current)
        if not row:
            return "There's no study session running. Say \"start studying\" and a course to begin one."
        summary = await asyncio.to_thread(_so_far, row)
        await asyncio.to_thread(_end_row, row["id"])
        await productivity.cancel_timer(row["course"])
        from arya.core import policy
        policy.note_result("cancel_timer")
        from arya.study import practice
        practice._quiz = None
        return f"Study session over: {summary}. Nice work."

    if _STATUS.search(message):
        row = await asyncio.to_thread(current)
        if not row:
            return "You're not in a study session right now."
        return f"So far: {await asyncio.to_thread(_so_far, row)}."

    if not await asyncio.to_thread(wants_session, message):
        return None
    course = await asyncio.to_thread(_which_course, message)
    if not course:
        from arya.study import planner
        names = sorted(planner._collections(), key=str.lower)[:4]
        options = (" — " + ", ".join(names[:-1]) + " or " + names[-1]) if len(names) > 1 else ""
        return f"Which course do you want to study{options}?"

    minutes = minutes_in(message)
    sessions = max(1, math.ceil(minutes / POMODORO_MINUTES))
    previous = await asyncio.to_thread(current)
    if previous:
        await productivity.cancel_timer(previous["course"])
    await asyncio.to_thread(_start_row, course, sessions * POMODORO_MINUTES)
    await productivity.pomodoro(sessions=sessions, label=course)
    from arya.core import audit, policy
    policy.note_result("pomodoro")                        # real actions: the reply may say "I started…"
    audit.log_event("study_session", course=course, minutes=sessions * POMODORO_MINUTES)

    did = []
    folder = await asyncio.to_thread(course_folder, course)
    if folder:
        try:
            open_folder(folder)
            policy.note_result("open_folder")
            did.append(f"opened your {course} folder")
        except OSError as exc:
            print(f"Study mode: couldn't open {folder}: {exc}")
    if os.getenv("ARYA_FOCUS_MUSIC_URL", "").strip():           # your focus playlist, if you set one
        from arya.tools import music
        if (await music.handle("play focus music")) == "Opening your focus music.":
            did.append("put on your focus music")
    did.append(f"started {'a pomodoro' if sessions == 1 else f'{sessions} pomodoros'}, "
               f"25 minutes of focus{' each' if sessions > 1 else ''} with a 5 minute break")
    plan = await asyncio.to_thread(_session_plan, course)
    actions = did[0] if len(did) == 1 else ", ".join(did[:-1]) + " and " + did[-1]
    return f"Study mode for {course}: I {actions}. {plan}"

