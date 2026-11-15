"""
Exam planner (roadmap M3).

  "My Dynamics exam is on 12 February"      -> saved (one date per course; a new date replaces it)
  "When are my exams?"                      -> the list, with days left
  "Remove the Dynamics exam"                -> deleted
  "What should I study today?"              -> a plan from your data: the nearest exam, cards due per
                                               course, your weakest topic, the next exercise to try
  daily briefing                            -> one study line ("Dynamics exam in 12 days; 8 cards due.")

Courses with an exam get priority, more so as it gets closer; within 3 days the plan is review only.
Everything is computed here — the model only ever phrases these numbers in the briefing.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

_MONTHS = {m: i for i, names in enumerate([
    ("january", "jan", "januar"), ("february", "feb", "februar"), ("march", "mar", "märz", "maerz"),
    ("april", "apr"), ("may", "mai"), ("june", "jun", "juni"), ("july", "jul", "juli"),
    ("august", "aug"), ("september", "sep", "sept"), ("october", "oct", "oktober", "okt"),
    ("november", "nov"), ("december", "dec", "dezember", "dez")], start=1) for m in names}
_WEEKDAYS = {d: i for i, d in enumerate(["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                                          "sunday"])}
_EXAM = r"(?:exam|examination|test|klausur|prüfung|pruefung|midterm|final)"


# ── Dates ─────────────────────────────────────────────────────────────────────

def parse_date(text: str, today: date | None = None) -> date | None:
    """'12 February', 'Feb 12th', '12.02.2027', '2027-02-12', '12/2', 'in 3 weeks', 'next Monday',
    'tomorrow'. Without a year: the next time that date comes round."""
    today = today or date.today()
    t = text.lower()
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b(?!\s*(?:m|s|kg|%|°))", t)
    if m and int(m.group(2)) <= 12:
        return _with_year(today, int(m.group(2)), int(m.group(1)), m.group(3))
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+(?:of\s+)?({month_names})\.?(?:\s+(\d{{4}}))?\b", t)
    if m:
        return _with_year(today, _MONTHS[m.group(2)], int(m.group(1)), m.group(3))
    m = re.search(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", t)
    if m:
        return _with_year(today, _MONTHS[m.group(1)], int(m.group(2)), m.group(3))
    m = re.search(r"\bin\s+(\d+|a|one|two|three|four|five|six|seven|eight)\s+(day|days|week|weeks|month|months)\b", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}.get(
            m.group(1), None) or int(m.group(1))
        days = n * (1 if m.group(2).startswith("day") else 7 if m.group(2).startswith("week") else 30)
        return today + timedelta(days=days)
    if re.search(r"\btomorrow\b", t):
        return today + timedelta(days=1)
    m = re.search(r"\b(?:next|on|this)\s+(" + "|".join(_WEEKDAYS) + r")\b", t)
    if m:
        ahead = (_WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    return None


def _safe(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _with_year(today: date, month: int, day: int, year: str | None) -> date | None:
    if year:
        y = int(year)
        return _safe(y + 2000 if y < 100 else y, month, day)
    d = _safe(today.year, month, day)
    if d and d < today:
        d = _safe(today.year + 1, month, day)
    return d


def spoken_date(d: date) -> str:
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')}"


def _days(d: date, today: date | None = None) -> int:
    return (d - (today or date.today())).days


def _in_days(n: int) -> str:
    return "today" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"


# ── Storage ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS exams (id INTEGER PRIMARY KEY AUTOINCREMENT, course TEXT UNIQUE NOT NULL, "
                 "exam_date TEXT NOT NULL, added TEXT NOT NULL)")
    return conn


def add_exam(course: str, when: date) -> None:
    with _db() as c:
        c.execute("INSERT INTO exams (course, exam_date, added) VALUES (?, ?, ?) ON CONFLICT(course) DO UPDATE "
                  "SET exam_date = excluded.exam_date", (course, when.isoformat(), date.today().isoformat()))


def remove_exam(course: str) -> bool:
    with _db() as c:
        return c.execute("DELETE FROM exams WHERE lower(course) = ?", (course.lower(),)).rowcount > 0


def upcoming(today: date | None = None) -> list[tuple[str, date]]:
    today = today or date.today()
    with _db() as c:
        rows = c.execute("SELECT course, exam_date FROM exams ORDER BY exam_date").fetchall()
    return [(r["course"], date.fromisoformat(r["exam_date"])) for r in rows
            if date.fromisoformat(r["exam_date"]) >= today]


# ── Courses ───────────────────────────────────────────────────────────────────

def _collections() -> list[str]:
    from arya.knowledge import library

    with library._lock, library._connect() as c:
        cols = {r["collection"] for r in c.execute("SELECT DISTINCT collection FROM documents") if r["collection"]}
    return sorted(cols - {"ARYA core notes", library.CORRECTIONS_TITLE, library.TRANSCRIPTIONS_TITLE},
                  key=len, reverse=True)


def course_in(message: str) -> str | None:
    """A course named in the message: a library collection if one matches, else the words before 'exam'."""
    for col in _collections():
        if re.search(rf"\b{re.escape(col)}\b", message, re.IGNORECASE):
            return col
    name = ""
    for m in (re.search(rf"\b(?:my|the|an?)\s+((?:[A-Za-zÄÖÜäöüß0-9]+\s+){{0,3}}?[A-Za-zÄÖÜäöüß0-9]+)\s+{_EXAM}\b",
                        message, re.IGNORECASE),
              re.search(rf"\b{_EXAM}\s+(?:in|for|on)\s+((?:[A-Za-zÄÖÜäöüß]+\s+){{0,3}}?[A-Za-zÄÖÜäöüß]+)"
                        rf"(?=\s+(?:is|on|in|next|this|at)\b|\s*[,.?!]|$)", message, re.IGNORECASE)):
        if not m:
            continue
        candidate = re.sub(r"^(?:next|final|first|second|big)\s+", "", m.group(1).strip(), flags=re.IGNORECASE)
        if candidate and candidate.lower() not in ("next", "final", "first", "an", "a", "the", "my", "big"):
            name = candidate                          # "the final exam for Thermodynamics": skip "final"
            break
    if not name:
        return None
    from arya.study.practice import FORMULA_QUESTIONS
    for col in _collections() + sorted({crs for _, crs in FORMULA_QUESTIONS}):   # "control systems" -> "Control"
        if col.lower().split()[0] == name.lower().split()[0]:
            return col
    return name[:1].upper() + name[1:]


# ── Plan ──────────────────────────────────────────────────────────────────────

def _course_stats(course: str) -> tuple[int, int]:
    from arya.study import practice
    return practice.due_count(course), practice.new_count(course)


def _next_exercise(course: str) -> str | None:
    from arya.study import practice
    with practice._db() as c:
        row = c.execute("SELECT key FROM cards WHERE kind = 'exercise' AND reps = 0 AND lower(course) = ? "
                        "ORDER BY id LIMIT 1", (course.lower(),)).fetchone()
    return row["key"].split(":")[1] if row else None


def plan_today(today: date | None = None) -> str:
    from arya.study import practice

    today = today or date.today()
    exams = upcoming(today)
    for course, _ in exams[:3]:
        practice.build_cards(course)
    acc = {crs: share for crs, share, _ in practice.weakest(limit=99)}
    parts = []
    focus = None
    if exams:
        course, when = exams[0]
        n = _days(when, today)
        parts.append(f"Your next exam is {course} on {spoken_date(when)}, {_in_days(n)}.")
        focus = course
    else:
        due_by_course = {}
        with practice._db() as c:
            for r in c.execute("SELECT course, COUNT(*) n FROM cards WHERE due IS NOT NULL AND due <= ? "
                               "GROUP BY course ORDER BY n DESC", (today.isoformat(),)):
                due_by_course[r["course"]] = r["n"]
        if due_by_course:
            focus = next(iter(due_by_course))
        elif acc:
            focus = min(acc, key=acc.get)
        parts.append("You have no exams saved; tell me a date like \"my Dynamics exam is on 12 February\".")

    if focus:
        due, new = _course_stats(focus)
        close = exams and exams[0][0] == focus and _days(exams[0][1], today) <= 3
        steps = []
        if due:
            steps.append(f"review the {due} {focus} card{'s' if due != 1 else ''} due")
        if new and not close:
            k = min(new, practice.NEW_PER_SESSION)
            steps.append(f"practise {k} new {'ones' if due else focus + ' card' + ('s' if k != 1 else '')}")
        ex = None if close else _next_exercise(focus)
        if ex:
            steps.append(f"then work through exercise {ex} with me")
        if steps:
            parts.append("Today: " + ", ".join(steps) + ".")
        if close:
            parts.append("The exam is close, so stick to reviewing what you know rather than new material.")
    weak = sorted((share, crs) for crs, share in acc.items() if crs != focus and share < 0.7)
    if weak:
        share, crs = weak[0]
        parts.append(f"Also spend a little time on {crs}, where you're at {round(share * 100)} percent.")
    for course, when in exams[1:3]:
        parts.append(f"{course} follows on {spoken_date(when)}.")
    if not focus:
        parts.append("Say \"quiz me\" to start practising with your exercises and formulas.")
    return " ".join(parts)


def briefing_line(today: date | None = None) -> str:
    """One line of facts for the morning briefing."""
    from arya.study import practice

    today = today or date.today()
    exams = upcoming(today)
    due = practice.due_count()
    bits = []
    if exams:
        course, when = exams[0]
        bits.append(f"{course} exam {_in_days(_days(when, today))} ({spoken_date(when)})")
    bits.append(f"{due} practice card{'s' if due != 1 else ''} due today" if due else "no practice cards due today")
    return "; ".join(bits) + "."


# ── Voice ─────────────────────────────────────────────────────────────────────

_LIST = re.compile(rf"\b(?:when are|list|what are|show|tell me)\b.{{0,20}}\b{_EXAM}s\b|\bmy {_EXAM}s\b|"
                   rf"\b{_EXAM} (?:dates|schedule|plan)\b", re.IGNORECASE)
_REMOVE = re.compile(rf"\b(?:remove|delete|cancel|forget|drop)\b.{{0,40}}\b{_EXAM}\b", re.IGNORECASE)
_PLAN = re.compile(r"\bwhat should i (?:study|learn|revise|review|practi[cs]e|work on|do)\b|\bstudy plan\b|"
                   r"\bplan (?:for )?(?:today|my (?:day|study|studies|revision))\b|\bhow should i (?:study|prepare)\b",
                   re.IGNORECASE)
_ADD = re.compile(rf"\b{_EXAM}\b", re.IGNORECASE)


def handle(message: str) -> str | None:
    """ARYA's reply for exam/plan requests, else None."""
    if _REMOVE.search(message):
        course = course_in(message)
        if not course:
            return "Which exam should I remove?"
        return f"Removed the {course} exam." if remove_exam(course) else f"I don't have a {course} exam saved."
    if _LIST.search(message):
        exams = upcoming()
        if not exams:
            return "You have no exams saved. Tell me one, like \"my Dynamics exam is on 12 February\"."
        return "Your exams: " + "; ".join(f"{c} on {spoken_date(d)}, {_in_days(_days(d))}" for c, d in exams) + "."
    if _PLAN.search(message):
        return plan_today()
    if _ADD.search(message):
        when = parse_date(message)
        if when is None:
            return None
        course = course_in(message)
        if not course:
            return f"Which course is the exam on {spoken_date(when)} for?"
        if when < date.today():
            return f"{spoken_date(when)} has already passed. Which date is the {course} exam?"
        add_exam(course, when)
        return f"Saved: {course} exam on {spoken_date(when)}, {_in_days(_days(when))}. I'll plan your practice around it."
    return None
