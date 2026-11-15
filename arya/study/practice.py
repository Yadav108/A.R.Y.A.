"""
Quizzes from your own material, with spaced repetition (roadmap M3).

  "Quiz me on dynamics"     -> due cards first, then up to 5 new ones; 10 questions per session
  (your answer)             -> graded, the right answer said, the card rescheduled (SM-2)
  "skip" / "stop the quiz"  -> next question / end with the score
  "How am I doing?"         -> score in this session, weakest topic overall
  "What should I practise?" -> cards due, weakest topics

Cards come from the material, not from the model's memory:
  * exercise cards — results on your answers pages ("Exercise 1-13: what is α2?"), graded by
    code exactly like the tutor's answer check (units, rounding);
  * formula cards — the checked formula cards; your spoken answer is compared with the reference
    by the local model (correct / partly / wrong) and the reference is always read out.
Everything is stored in arya.db, so progress survives restarts.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

SESSION_LEN = 10
NEW_PER_SESSION = 5
MAX_EXERCISES_SCANNED = 60
SESSION_IDLE_S = 20 * 60

FORMULA_QUESTIONS = [
    ("What is the formula for the percent overshoot of a second-order system?", "Control"),
    ("What is the formula for the 2 percent settling time of a second-order system?", "Control"),
    ("What is the formula for the peak time?", "Control"),
    ("What is the formula for the damped frequency?", "Control"),
    ("What is the transfer function of a first-order system?", "Control"),
    ("What is the transfer function of a second-order system?", "Control"),
    ("What is the formula for the phase margin?", "Control"),
    ("What is the formula for the gain margin?", "Control"),
    ("What is the natural frequency of a mass-spring system?", "Dynamics"),
    ("What is the impedance of a capacitor?", "Electrical"),
    ("What is the impedance of an inductor?", "Electrical"),
    ("What is the time constant of an RC circuit?", "Electrical"),
    ("What is the time constant of an RL circuit?", "Electrical"),
]


# ── Storage ───────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, course TEXT NOT NULL, key TEXT UNIQUE,
            question TEXT NOT NULL, answer TEXT NOT NULL, name TEXT, value REAL, unit TEXT,
            doc_id INTEGER, page INTEGER,
            ease REAL DEFAULT 2.5, interval INTEGER DEFAULT 0, reps INTEGER DEFAULT 0, lapses INTEGER DEFAULT 0,
            due TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, card_id INTEGER NOT NULL, ts REAL NOT NULL, quality INTEGER NOT NULL);
    """)
    return conn


def _today() -> date:
    return date.today()


def schedule(ease: float, interval: int, reps: int, quality: int) -> tuple[float, int, int]:
    """SM-2: quality 0-5 -> (ease, interval in days, reps). Missed (< 3): back tomorrow."""
    if quality < 3:
        return max(1.3, ease - 0.2), 1, 0
    reps += 1
    interval = 1 if reps == 1 else 6 if reps == 2 else max(1, round(interval * ease))
    ease = max(1.3, ease + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    return ease, interval, reps


def record(card_id: int, quality: int) -> None:
    with _db() as c:
        row = c.execute("SELECT ease, interval, reps, lapses FROM cards WHERE id = ?", (card_id,)).fetchone()
        ease, interval, reps = schedule(row["ease"], row["interval"], row["reps"], quality)
        due = (_today() + timedelta(days=interval)).isoformat()
        c.execute("UPDATE cards SET ease=?, interval=?, reps=?, lapses=?, due=? WHERE id=?",
                  (ease, interval, reps, row["lapses"] + (quality < 3), due, card_id))
        c.execute("INSERT INTO reviews (card_id, ts, quality) VALUES (?, ?, ?)", (card_id, time.time(), quality))


# ── Building cards from the material ──────────────────────────────────────────

def _add(c, **card) -> None:
    c.execute("INSERT OR IGNORE INTO cards (kind, course, key, question, answer, name, value, unit, doc_id, page, "
              "created) VALUES (:kind, :course, :key, :question, :answer, :name, :value, :unit, :doc_id, :page, "
              ":created)", {"name": None, "value": None, "unit": None, "doc_id": None, "page": None,
                            "created": _today().isoformat(), **card})


def _card_results(ex, limit: int = 2) -> list:
    """Results worth a card: ones the task asks for ('Determine … α2 … v(tA)'), never zeros or steps;
    if none can be matched, the final result on the answers page."""
    from arya.study import tutor

    asked_text = " ".join(re.findall(r"[^.!?]*\b(?:determine|find|calculate|compute|what|which|how)\b[^.!?]*",
                                     ex.statement, re.IGNORECASE))
    tokens = {tutor._norm_name(t) for t in re.findall(r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω]*", asked_text)}
    useful = [r for r in ex.results if r.value != 0 and not re.fullmatch(r"[a-z]{4,}", r.name)]  # not "velocity ="
    asked = [r for r in useful if r.key in tokens]
    return (asked or useful[-1:])[:limit]


def build_cards(course: str | None = None) -> int:
    """Create missing cards (idempotent). Returns how many cards exist for the course (or all)."""
    from arya.knowledge import library
    from arya.study import tutor
    from arya.study.formulas import formula_card

    with _db() as c:
        for q, crs in FORMULA_QUESTIONS:
            if course and crs.lower() != course.lower():
                continue
            card = formula_card(q)
            if card:
                _add(c, kind="formula", course=crs, key=f"formula:{q}", question=q, answer=card)

    with library._lock, library._connect() as kc:
        docs = kc.execute("SELECT id, collection FROM documents WHERE status = 'indexed' AND path NOT IN (?, ?, ?)",
                          (str(library.CORE_NOTES), str(library.CORRECTIONS), str(library.TRANSCRIPTIONS))).fetchall()
        wanted = {d["id"]: d["collection"] for d in docs if not course or (d["collection"] or "").lower() == course.lower()}
        refs: list[str] = []
        for r in kc.execute("SELECT doc_id, text FROM chunks"):
            if r["doc_id"] not in wanted:
                continue
            for a, b in re.findall(r"(?<![\d.,(/])(\d{1,2})\s*[-–]\s*(\d{1,2})\s*\)", r["text"]):
                ref = f"{int(a)}-{int(b)}"
                if ref not in refs:
                    refs.append(ref)
    with _db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS card_scans (ref TEXT PRIMARY KEY)")      # exercises already looked at
        seen = {r["ref"] for r in c.execute("SELECT ref FROM card_scans")}
        for ref in [r for r in refs if r not in seen][:MAX_EXERCISES_SCANNED]:
            c.execute("INSERT OR IGNORE INTO card_scans (ref) VALUES (?)", (ref,))
            ex = tutor.load_exercise(f"exercise {ref} " + (course or ""))
            if not ex or not ex.results:
                continue                                   # no answers page: nothing to check against
            sheets = [s for s in ex.sources if s.get("kind") == "pdf" and not tutor._is_answers(s.get("snippet", ""))]
            sheet = sheets[0] if sheets else ex.sources[0]
            crs = sheet.get("collection") or course or "Exercises"
            for res in _card_results(ex):                    # the quantities the task asks for
                _add(c, kind="exercise", course=crs, key=f"exercise:{ref}:{res.key}",
                     question=f"Exercise {ref}: what is {res.name}?",
                     answer=f"The answer sheet gives {res.spoken()}.", name=res.name, value=res.value,
                     unit=res.unit, doc_id=sheet.get("doc"), page=sheet.get("page"))
    with _db() as c:
        if course:
            return c.execute("SELECT COUNT(*) FROM cards WHERE lower(course) = ?", (course.lower(),)).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def deck(course: str | None = None, size: int = SESSION_LEN) -> list[sqlite3.Row]:
    """Due cards first (most overdue first), then new ones."""
    today = _today().isoformat()
    where, args = ("AND lower(course) = ?", [course.lower()]) if course else ("", [])
    with _db() as c:
        due = c.execute(f"SELECT * FROM cards WHERE due IS NOT NULL AND due <= ? {where} ORDER BY due, lapses DESC",
                        [today, *args]).fetchall()
        new = c.execute(f"SELECT * FROM cards WHERE due IS NULL {where} ORDER BY kind DESC, id LIMIT ?",
                        [*args, NEW_PER_SESSION]).fetchall()
    return (list(due) + list(new))[:size]


def due_count(course: str | None = None) -> int:
    where, args = ("AND lower(course) = ?", [course.lower()]) if course else ("", [])
    with _db() as c:
        return c.execute(f"SELECT COUNT(*) FROM cards WHERE due IS NOT NULL AND due <= ? {where}",
                         [_today().isoformat(), *args]).fetchone()[0]


def weakest(limit: int = 2) -> list[tuple[str, float, int]]:
    """[(course, share right, reviews)] — lowest share first, over the last 50 reviews per course."""
    with _db() as c:
        rows = c.execute("SELECT cards.course, reviews.quality FROM reviews JOIN cards ON cards.id = reviews.card_id "
                         "ORDER BY reviews.ts DESC LIMIT 500").fetchall()
    by: dict[str, list[int]] = {}
    for r in rows:
        if len(by.setdefault(r["course"], [])) < 50:
            by[r["course"]].append(r["quality"])
    scored = [(crs, sum(q >= 3 for q in qs) / len(qs), len(qs)) for crs, qs in by.items() if len(qs) >= 3]
    return sorted(scored, key=lambda x: x[1])[:limit]


# ── Grading ───────────────────────────────────────────────────────────────────

def grade_exercise(card, answer: str) -> tuple[int, str]:
    from arya.study import tutor

    answer = re.sub(r"\b(?:minus|negative)\s+(?=\d)", "-", answer, flags=re.IGNORECASE)   # spoken sign
    claim = tutor.parse_claim(answer) if re.search(r"\d", answer) else None
    if not claim:
        return 1, f"I didn't hear a number. {card['answer']}"
    result = tutor.Result(card["name"], card["value"], card["unit"] or "")
    verdict = tutor.check((None, claim[1], claim[2]), [result])
    if verdict.startswith("Correct"):
        return 5, verdict
    if verdict.startswith("Close"):
        return 3, verdict.split(" Check how")[0] + f" {card['answer']}"
    if verdict.startswith("Check your unit"):
        return 2, f"{verdict} {card['answer']}"
    return 1, f"Not quite. {card['answer']}"


async def grade_formula(card, answer: str) -> tuple[int, str]:
    """The local model compares with the reference; the reference is always read out."""
    from arya.core.llm import INTENT_MODEL_NAME, _complete

    prompt = ("You grade a student's spoken answer against the reference from their course. Ignore wording and "
              "notation differences; judge only whether the formula or fact is the same. Reply with exactly one "
              "word: CORRECT, PARTIAL or WRONG.")
    msg = f"Question: {card['question']}\nReference: {card['answer']}\nStudent: {answer}"
    try:
        out = await _complete(INTENT_MODEL_NAME, [{"role": "system", "content": prompt},
                                                  {"role": "user", "content": msg}], temperature=0, max_tokens=4)
        word = (out.content or "").strip().upper()
    except Exception as exc:
        print(f"Quiz grading failed: {exc}")
        word = ""
    if word.startswith("CORRECT"):
        return 5, f"Correct! {card['answer']}"
    if word.startswith("PARTIAL"):
        return 3, f"Partly right. {card['answer']}"
    if word.startswith("WRONG"):
        return 1, f"Not quite. {card['answer']}"
    return 3, f"Here's the reference: {card['answer']} Did you have that? I'll count it as partly right."


# ── Sessions ──────────────────────────────────────────────────────────────────

_START = re.compile(r"\b(?:quiz me|test me|practi[cs]e (?:with me|session|now|some)?|flash ?cards?|review session|"
                    r"ask me (?:some )?questions|let'?s (?:practi[cs]e|revise|review))\b", re.IGNORECASE)
_STOP = re.compile(r"^\W*(?:ok(?:ay)?\W+)?(?:stop|quit|end|exit|done|finish|enough)(?:\s+(?:the\s+)?(?:quiz|practi[cs]e|"
                   r"session|this|it|now))?\W*$", re.IGNORECASE)
# "I don't know, I think that's what we're trying to do." (from the log) is a skip too, not a new question.
_SKIP = re.compile(r"^\W*(?:skip|pass|next|i don'?t know|no idea|don'?t know|i'?m not sure|no clue)\b"
                   r"(?:\W*$|[,.!]\s*(?!.*\b(?:it'?s|is|equals?|maybe)\b))", re.IGNORECASE)
_HOW = re.compile(r"\bhow am i doing\b|\bmy (?:score|progress)\b", re.IGNORECASE)
_WHAT = re.compile(r"\bwhat should i (?:practi[cs]e|study|revise|review)\b|\bwhat'?s due\b|\bcards? due\b",
                   re.IGNORECASE)


@dataclass
class Quiz:
    course: str | None
    cards: list
    index: int = 0
    right: int = 0
    asked: int = 0
    last: float = field(default_factory=time.monotonic)

    def current(self):
        return self.cards[self.index] if self.index < len(self.cards) else None


_quiz: Quiz | None = None


def active() -> Quiz | None:
    global _quiz
    if _quiz and time.monotonic() - _quiz.last > SESSION_IDLE_S:
        _quiz = None
    return _quiz


def _course_in(message: str) -> str | None:
    from arya.knowledge import library

    with library._lock, library._connect() as c:
        cols = [r["collection"] for r in c.execute("SELECT DISTINCT collection FROM documents") if r["collection"]]
    for col in sorted(set(cols) | {"Control", "Electrical", "Dynamics"}, key=len, reverse=True):
        if re.search(rf"\b{re.escape(col)}\b", message, re.IGNORECASE):
            return col
    return None


def _ask(q: Quiz) -> tuple[str, list | None]:
    card = q.current()
    n = f"Question {q.index + 1} of {len(q.cards)}: {card['question']}"
    sources = None
    if card["kind"] == "exercise" and card["doc_id"]:
        sources = [{"doc": card["doc_id"], "page": card["page"], "title": "", "collection": card["course"],
                    "kind": "pdf", "label": f"Exercise {card['key'].split(':')[1]}", "snippet": card["question"],
                    "chunk": 0}]
        n += " The task is on screen."
    return n, sources


def _summary(q: Quiz) -> str:
    return f"You got {q.right} of {q.asked} right." if q.asked else "We didn't get to any questions."


def _topic_line(weak: list) -> str:
    """'Your weakest topic is X …' only makes sense with more than one topic practised."""
    if not weak:
        return ""
    crs, share, n = weak[0]
    if len(weakest(limit=99)) > 1:
        return f"Your weakest topic is {crs}: {round(share * 100)} percent right over the last {n} answers."
    return f"In {crs} you're at {round(share * 100)} percent right over the last {n} answers."


def new_count(course: str | None = None) -> int:
    where, args = ("AND lower(course) = ?", [course.lower()]) if course else ("", [])
    with _db() as c:
        return c.execute(f"SELECT COUNT(*) FROM cards WHERE due IS NULL {where}", args).fetchone()[0]


def _what_to_practise() -> str:
    build_cards()
    due, new = due_count(), new_count()
    parts = [f"You have {due} card{'s' if due != 1 else ''} due today." if due else "Nothing is due for review today."]
    if new:
        parts.append(f"There are {new} new cards from your exercises and formulas.")
    line = _topic_line(weakest())
    if line:
        parts.append(line)
    parts.append("Say \"quiz me\", or \"quiz me on\" a topic, to start.")
    return " ".join(parts)


async def handle(message: str) -> tuple[str, list | None] | None:
    """(reply, sources) when this is about practising, else None."""
    global _quiz
    q = active()

    if _WHAT.search(message) and not q:
        return _what_to_practise(), None

    if _START.search(message):
        course = _course_in(message)
        import asyncio
        total = await asyncio.to_thread(build_cards, course)
        cards = await asyncio.to_thread(deck, course)
        if not cards:
            what = f"for {course}" if course else "yet"
            return (f"I don't have practice cards {what}. They come from exercises with answer pages and the "
                    f"checked formulas.", None) if not total else ("Nothing is due right now, well done. "
                                                                    "Come back tomorrow.", None)
        _quiz = Quiz(course=course, cards=cards)
        intro = f"Let's practise{' ' + course if course else ''}: {len(cards)} questions."
        text, sources = _ask(_quiz)
        return f"{intro} {text}", sources

    if q is None:
        return None

    q.last = time.monotonic()
    if _STOP.match(message):
        _quiz = None
        return f"Okay, stopping here. {_summary(q)}", None
    if _HOW.search(message):
        line = _topic_line(weakest())
        return f"{_summary(q)} {line}".strip(), None

    card = q.current()
    skipping = bool(_SKIP.match(message))              # before media commands: "skip" means the question here
    from arya.core.llm import quick_media_command, quick_status_command
    if not skipping and (quick_status_command(message) or quick_media_command(message)):
        return None                                    # "what time is it" mid-quiz

    if skipping:
        quality, verdict = 1, f"Skipped. {card['answer']}"
    elif card["kind"] == "exercise":
        quality, verdict = grade_exercise(card, message)
    else:
        quality, verdict = await grade_formula(card, message)
    import asyncio
    await asyncio.to_thread(record, card["id"], quality)
    q.asked += 1
    q.right += quality >= 3
    q.index += 1
    if q.current() is None:
        _quiz = None
        return f"{verdict} That was the last one. {_summary(q)}", None
    text, sources = _ask(q)
    return f"{verdict} {text}", sources
