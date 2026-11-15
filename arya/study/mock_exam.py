"""
Timed mock exams from your own exercises (roadmap C3).

"Give me a 60-minute Dynamics mock exam" picks one task per exercise (about 12 minutes each), preferring
exercises from old exams, shows the task pages and starts the clock. You work on paper and tell ARYA your
results — "task 2: 2.8 metres per second", or "my answer is 2.8 m/s" for the current task. "Next task",
"show task 3", "how much time is left?". "Finish the exam" (or the clock running out) grades every answer
against the answer sheets: score, and for each miss the official result and the exercise to revisit.

Every graded task is recorded in your practice history (spaced repetition), so weak topics flow into
"what should I study today?" and the progress view. Only exercises with an answer sheet can be graded, so a
course needs those (today: Dynamics).
"""
from __future__ import annotations

import asyncio
import math
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime

MINUTES_PER_TASK = 12
DEFAULT_MINUTES = 60
MAX_TASKS = 8
_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        "a": 1, "an": 1, "half an": 0.5}


@dataclass
class Task:
    card: sqlite3.Row
    ref: str                                   # "1-13"
    doc: int | None = None                     # the page with the task statement (not a solution sheet)
    page: int | None = None
    answer: str = ""                           # what you said
    quality: int | None = None
    verdict: str = ""

    @property
    def wanted(self) -> str:
        return self.card["name"]


@dataclass
class Exam:
    course: str
    tasks: list[Task]
    minutes: int
    started: float = field(default_factory=time.time)
    current: int = 0
    timer: object = None

    @property
    def ends(self) -> float:
        return self.started + self.minutes * 60


_exam: Exam | None = None


def active() -> Exam | None:
    return _exam


# ── Building an exam ──────────────────────────────────────────────────────────

def _exam_docs() -> set[int]:
    from arya.knowledge import library
    with library._lock, library._connect() as c:
        return {r["id"] for r in c.execute("SELECT id, title FROM documents")
                if re.search(r"exam|klausur|prüfung", r["title"] or "", re.IGNORECASE)}


def _solution_docs() -> set[int]:
    """Solution sheets: showing one as the task page would give the answer away."""
    from arya.knowledge import library
    with library._lock, library._connect() as c:
        return {r["id"] for r in c.execute("SELECT id, title FROM documents")
                if re.search(r"solution|lösung|loesung|answers?\b|muster", r["title"] or "", re.IGNORECASE)}


def pick_tasks(course: str, n: int, seed: int | None = None) -> list[Task]:
    """One card per exercise: exam exercises first, then ones you got wrong or haven't done, then the rest."""
    from arya.study import practice

    with practice._db() as c:
        rows = c.execute("SELECT * FROM cards WHERE kind = 'exercise' AND lower(course) = ? ORDER BY id",
                         (course.lower(),)).fetchall()
    spoilers = _solution_docs()
    by_ref: dict[str, sqlite3.Row] = {}
    for r in rows:
        by_ref.setdefault(r["key"].split(":")[1], r)
    pages = {ref: page for ref in by_ref if (page := task_page(ref, course, by_ref[ref], spoilers))}
    exam_docs = _exam_docs()
    rng = random.Random(seed)
    refs = list(pages)
    rng.shuffle(refs)
    refs.sort(key=lambda ref: (pages[ref][0] not in exam_docs,                       # old-exam tasks first
                               by_ref[ref]["reps"] > 0 and by_ref[ref]["lapses"] == 0))  # then new / missed ones
    return [Task(by_ref[ref], ref, doc=pages[ref][0], page=pages[ref][1]) for ref in refs[:n]]


def task_page(ref: str, course: str, card, spoilers: set[int]) -> tuple[int, int] | None:
    """The page with the task statement — never a solution sheet (that would give the answer away)."""
    if card["doc_id"] and card["doc_id"] not in spoilers:
        return card["doc_id"], card["page"]
    from arya.study import tutor
    ex = tutor.load_exercise(f"exercise {ref} {course}")
    for s in (ex.sources if ex else []):
        if s.get("kind") == "pdf" and s.get("doc") not in spoilers and not tutor._is_answers(s.get("snippet", "")):
            return s["doc"], s["page"]
    return None


def _minutes_in(message: str) -> int:
    m = re.search(r"\b(\d{1,3}|an?|one|two|three|half an)[\s-]*(hours?|minutes?|mins?|h)\b", message, re.IGNORECASE)
    if not m:
        return DEFAULT_MINUTES
    raw = m.group(1).lower()
    n = float(_NUM.get(raw, raw)) if raw in _NUM or raw.isdigit() else 1
    return max(10, int(n * 60 if m.group(2).lower().startswith("h") else n))


def _course_in(message: str) -> str | None:
    from arya.study import planner, practice

    plain = _START.sub(" ", message)                    # "a mock exam" must not make "Mock" a course
    course = practice._course_in(plain) or planner.course_in(plain)
    if course:
        return course
    with practice._db() as c:
        courses = [r["course"] for r in c.execute(
            "SELECT course, COUNT(*) n FROM cards WHERE kind = 'exercise' GROUP BY course ORDER BY n DESC")]
    exams = [crs for crs, _ in planner.upcoming() if crs in courses]
    return exams[0] if exams else (courses[0] if len(courses) == 1 else None)


# ── Talking about tasks ──────────────────────────────────────────────────────

def _task_line(i: int, t: Task) -> str:
    return f"Task {i + 1}: exercise {t.ref}, find {t.wanted}"


def _source(t: Task, i: int) -> dict | None:
    if not t.doc:
        return None
    return {"doc": t.doc, "page": t.page, "title": "", "collection": t.card["course"],
            "kind": "pdf", "label": f"Task {i + 1}: exercise {t.ref}", "snippet": t.card["question"], "chunk": 0}


def _left(exam: Exam) -> str:
    secs = max(0, int(exam.ends - time.time()))
    m = math.ceil(secs / 60)
    return "Time is up" if secs <= 0 else f"{m} minute{'s' if m != 1 else ''} left"


async def _time_up(exam: Exam) -> None:
    from arya.tools.productivity import _notify
    await asyncio.sleep(max(0, exam.ends - time.time() - 300))
    if _exam is exam:
        await _notify(f"Five minutes left in your {exam.course} mock exam.")
    await asyncio.sleep(max(0, exam.ends - time.time()))
    if _exam is exam:
        await _notify("Time is up for the mock exam. Tell me your remaining answers, then say finish.")


# ── Grading ───────────────────────────────────────────────────────────────────

def grade(exam: Exam) -> str:
    from arya.study import practice

    graded = [t for t in exam.tasks if t.answer]
    for t in graded:
        if t.quality is None:
            t.quality, t.verdict = practice.grade_exercise(t.card, t.answer)
            practice.record(t.card["id"], t.quality)
    for t in exam.tasks:
        if not t.answer:
            practice.record(t.card["id"], 1)                    # not answered counts as not known yet
    right = sum(t.quality is not None and t.quality >= 5 for t in exam.tasks)
    close = sum(t.quality is not None and 3 <= t.quality < 5 for t in exam.tasks)
    n = len(exam.tasks)
    minutes = max(1, round((time.time() - exam.started) / 60))
    parts = [f"Mock exam over after {minutes} minute{'s' if minutes != 1 else ''}: {right} of {n} right"
             + (f", {close} close" if close else "") + f", that's {round(100 * right / n)} percent."]
    for i, t in enumerate(exam.tasks):
        if not t.answer:
            parts.append(f"Task {i + 1}, exercise {t.ref}: no answer. {t.card['answer']}")
        elif t.quality < 5:
            parts.append(f"Task {i + 1}, exercise {t.ref}: you said {t.answer.strip(' .')}. {t.card['answer']}")
    missed = [t.ref for t in exam.tasks if not t.quality or t.quality < 5]
    if missed:
        parts.append("Revisit exercise " + ", ".join(missed[:-1]) + (" and " if len(missed) > 1 else "")
                     + missed[-1] + "; say \"walk me through exercise " + missed[0] + "\" to start.")
    else:
        parts.append("Excellent, everything right.")
    _save_result(exam, right, n)
    return " ".join(parts)


def _save_result(exam: Exam, right: int, n: int) -> None:
    from arya.study import practice
    with practice._db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS mock_exams (id INTEGER PRIMARY KEY AUTOINCREMENT, course TEXT, "
                  "taken TEXT, minutes INTEGER, right INTEGER, total INTEGER)")
        c.execute("INSERT INTO mock_exams (course, taken, minutes, right, total) VALUES (?, ?, ?, ?, ?)",
                  (exam.course, datetime.now().isoformat(timespec="minutes"), exam.minutes, right, n))


# ── Voice ─────────────────────────────────────────────────────────────────────

_START = re.compile(r"\b(?:mock|practice|trial|sample|test|simulated?)\s+exam\b|\bexam\s+simulation\b",
                    re.IGNORECASE)
_FINISH = re.compile(r"\b(?:finish|end|submit|stop|done with)\b.{0,15}\b(?:the\s+|my\s+)?(?:mock\s+)?exam\b|"
                     r"^\W*(?:i'?m\s+)?(?:done|finished)\W*$|\bgrade (?:it|my (?:exam|answers))\b", re.IGNORECASE)
_CANCEL = re.compile(r"\b(?:cancel|abort|quit)\b.{0,15}\b(?:mock\s+)?exam\b", re.IGNORECASE)
_TIME = re.compile(r"\bhow (?:much|long)\b.{0,15}\b(?:time|left)\b|\btime left\b", re.IGNORECASE)
_SHOW = re.compile(r"\b(?:show|go to|open|back to)\s+(?:me\s+)?(?:task|question|number)\s+(\d+|one|two|three|four|"
                   r"five|six|seven|eight)\b|\b(?:next|following)\s+(?:task|question)\b|"
                   r"\b(?:previous|last)\s+(?:task|question)\b", re.IGNORECASE)
_ANSWER = re.compile(r"^\W*(?:(?:for\s+)?(?:task|question|number)\s+(?P<n>\d+|one|two|three|four|five|six|seven|eight)"
                     r"\s*[:,.-]?\s*(?:(?:i\s+(?:got|have)|the answer is|it'?s|is)\s+)?|"
                     r"(?:my answer is|i got|i have|answer:?)\s+)(?P<a>.*\d.*)$", re.IGNORECASE)


def _num(word: str) -> int:
    return int(_NUM.get(word.lower(), word)) if not word.isdigit() else int(word)


async def handle(message: str) -> tuple[str, list | None] | None:
    """(reply, task pages to show) for mock-exam requests, else None."""
    global _exam
    exam = _exam

    if exam is None:
        if not _START.search(message):
            return None
        course = await asyncio.to_thread(_course_in, message)
        if not course:
            return ("Which course? I can build a mock exam where I have exercises with answer sheets.", None)
        from arya.study import practice
        await asyncio.to_thread(practice.build_cards, course)
        minutes = _minutes_in(message)
        n = max(1, min(MAX_TASKS, round(minutes / MINUTES_PER_TASK)))
        tasks = await asyncio.to_thread(pick_tasks, course, n)
        if not tasks:
            return (f"I don't have {course} exercises with answer sheets yet, so I can't grade a mock exam for it.",
                    None)
        _exam = exam = Exam(course=course, tasks=tasks, minutes=minutes)
        from arya.tools import productivity
        exam.timer = productivity._register("timer", f"{course} mock exam", minutes * 60, _time_up(exam))
        from arya.core import policy
        policy.note_result("set_timer")
        lines = "; ".join(_task_line(i, t) for i, t in enumerate(tasks))
        sources = [s for i, t in enumerate(tasks) if (s := _source(t, i))]
        return (f"{course} mock exam: {len(tasks)} task{'s' if len(tasks) != 1 else ''} in {minutes} minutes, "
                f"the clock is running. {lines}. The task pages are on screen. Work on paper and tell me each "
                f"result, like \"task 1: 2.8 metres per second\". Say \"finish the exam\" when you're done.", sources)

    if _CANCEL.search(message):
        _stop_clock(exam)
        _exam = None
        return ("Mock exam cancelled, nothing graded.", None)
    if _FINISH.search(message):
        _stop_clock(exam)
        reply = await asyncio.to_thread(grade, exam)
        _exam = None
        return (reply, None)
    if _TIME.search(message):
        done = sum(bool(t.answer) for t in exam.tasks)
        return (f"{_left(exam)}. You've answered {done} of {len(exam.tasks)} tasks.", None)
    m = _ANSWER.match(message)
    if m:
        i = _num(m.group("n")) - 1 if m.group("n") else exam.current
        if not 0 <= i < len(exam.tasks):
            return (f"There are only {len(exam.tasks)} tasks.", None)
        exam.tasks[i].answer, exam.tasks[i].quality = m.group("a").strip(), None
        nxt = next((j for j in range(len(exam.tasks)) if not exam.tasks[j].answer), None)
        if nxt is None:
            return (f"Got it for task {i + 1}. That's all tasks answered; say \"finish the exam\" to grade, "
                    "or change an answer first.", None)
        exam.current = nxt
        return (f"Got it for task {i + 1}. Next: {_task_line(nxt, exam.tasks[nxt])}. {_left(exam)}.",
                [s] if (s := _source(exam.tasks[nxt], nxt)) else None)
    s = _SHOW.search(message)
    if s:
        if s.group(1):
            i = _num(s.group(1)) - 1
        elif re.search(r"previous|last", s.group(0), re.IGNORECASE):
            i = max(0, exam.current - 1)
        else:
            i = min(len(exam.tasks) - 1, exam.current + 1)
        if not 0 <= i < len(exam.tasks):
            return (f"There are only {len(exam.tasks)} tasks.", None)
        exam.current = i
        return (_task_line(i, exam.tasks[i]) + ".", [src] if (src := _source(exam.tasks[i], i)) else None)
    return None                                          # anything else during the exam: normal ARYA


def _stop_clock(exam: Exam) -> None:
    """Stop the exam clock and forget it now (not on the next loop turn), so a restart never 'misses' it."""
    if exam.timer is None:
        return
    exam.timer.task.cancel()
    from arya.tools import productivity
    if exam.timer in productivity._SCHEDULED:
        productivity._SCHEDULED.remove(exam.timer)
    productivity._forget(getattr(exam.timer, "row_id", None))
    exam.timer = None
