"""
To-do lists by voice: a work list and a personal list (the current mode picks which, unless you say).

  "add 'email Dr. Weber the results' to my work tasks"   "put buy milk on my list"   "new task: book the lab"
  "what's on my list?"   "what are my work tasks?"
  "mark book the lab as done"   "tick off buy milk"   "remove buy milk from my list"

Stored in arya.db (table `tasks`); the morning briefing mentions what's open.
"""
from __future__ import annotations

import re
import sqlite3
import time

MAX_OPEN_LISTED = 7
DB_OVERRIDE = None                 # evals and tests: a scratch database instead of arya.db


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH
    c = sqlite3.connect(DB_OVERRIDE or DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, list TEXT NOT NULL, text TEXT NOT NULL, "
              "created REAL NOT NULL, done REAL)")
    return c


def add(text: str, which: str) -> int:
    with _db() as c:
        return c.execute("INSERT INTO tasks (list, text, created) VALUES (?, ?, ?)", (which, text, time.time())).lastrowid


def open_tasks(which: str) -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM tasks WHERE list = ? AND done IS NULL ORDER BY created", (which,)).fetchall()


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 or w.isdigit()}


def find(spoken: str, which: str | None = None) -> sqlite3.Row | None:
    """The open task that best matches what was said (most shared words), or None."""
    with _db() as c:
        rows = c.execute("SELECT * FROM tasks WHERE done IS NULL" + (" AND list = ?" if which else ""),
                         (which,) if which else ()).fetchall()
    said = _words(spoken)
    best, score = None, 0.0
    for r in rows:
        w = _words(r["text"])
        s = len(said & w) / max(1, len(w))
        if s > score:
            best, score = r, s
    return best if score >= 0.5 else None


def finish(task_id: int) -> None:
    with _db() as c:
        c.execute("UPDATE tasks SET done = ? WHERE id = ?", (time.time(), task_id))


def remove(task_id: int) -> None:
    with _db() as c:
        c.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def briefing_line(which: str) -> str:
    rows = open_tasks(which)
    if not rows:
        return ""
    names = [r["text"] for r in rows[:4]]
    more = f" and {len(rows) - 4} more" if len(rows) > 4 else ""
    return f"{len(rows)} open {which} to-do{'s' if len(rows) != 1 else ''}: " + "; ".join(names) + more


# ── Voice ─────────────────────────────────────────────────────────────────────

_LIST = r"(?:my\s+|the\s+)?(?:(?P<w>work|personal)\s+)?(?:to-?do\s+list|to-?dos?|tasks?\s+list|tasks|task list|list)"
_ADD = re.compile(rf"^\W*(?:arya\W+)?(?:please\s+)?(?:add|put)\s+(?P<t>.+?)\s+(?:to|on)\s+{_LIST}\W*$|"
                  r"^\W*(?:arya\W+)?(?:new|add a)\s+(?P<w2>work\s+|personal\s+)?(?:task|to-?do)\s*[:,-]?\s+(?P<t2>.+?)\W*$",
                  re.IGNORECASE)
# Showing needs "my" ("what is the list of Laplace transforms" is a question, not your to-dos).
_SHOW = re.compile(r"\bwhat(?:'s| is| are)\s+(?:on\s+)?my\s+(?:(?P<w3>work|personal)\s+)?(?:to-?do\s+list|to-?dos?|"
                   r"tasks?\s+list|tasks|list)\b|^\W*(?:show|read|list)\s+(?:me\s+)?my\s+(?:(?P<w4>work|personal)\s+)?"
                   r"(?:to-?do\s+list|to-?dos?|tasks?\s+list|tasks|list)\W*$|"
                   r"^\W*what do i (?:still )?(?:have|need) to do(?: today)?\W*$", re.IGNORECASE)
_DONE = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:mark|tick off|check off|cross off)\s+(?P<t>.+?)"
                   r"(?:\s+(?:as\s+)?(?:done|complete|completed|finished))?\W*$", re.IGNORECASE)
_REMOVE = re.compile(rf"^\W*(?:arya\W+)?(?:please\s+)?(?:remove|delete)\s+(?P<t>.+?)\s+from\s+{_LIST}\W*$", re.IGNORECASE)


# Brainstorm mode: "save that idea", "save the second idea", "add a podcast about robots to my ideas"
_SAVE_IDEA = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:save|keep|note|remember)\s+(?:that|this|the\s+"
                        r"(?P<ord>first|second|third|fourth|fifth|last)|it)\s+(?:one|idea)\b(?:\s*[:,-]\s*(?P<t>.+?))?"
                        r"\W*$|^\W*(?:arya\W+)?(?:add|put)\s+(?P<t2>.+?)\s+(?:to|on)\s+(?:my|the)\s+ideas?(?:\s+list)?\W*$",
                        re.IGNORECASE)
_SHOW_IDEAS = re.compile(r"\bwhat(?:'s| is| are)\s+(?:on\s+)?my\s+ideas?(?:\s+list)?\b|^\W*(?:show|read|list)\s+"
                         r"(?:me\s+)?my\s+ideas\W*$", re.IGNORECASE)
_ORDINAL = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}


def _idea_from_last_answer(ordinal: str | None) -> str | None:
    """The idea ARYA just said: 'First, …' / 'Second, …' sentences of its last answer."""
    from arya.memory.database import get_history
    last = next((m["content"] for m in reversed(get_history(limit=6)) if m["role"] == "assistant"), "")
    from arya.core.llm import split_sentences
    sentences = [s.strip() for s in split_sentences(last) if s.strip()]
    ideas = [s for s in sentences if re.match(r"^(?:first|second|third|fourth|fifth|another|or)\b", s, re.IGNORECASE)]
    ideas = ideas or [s for s in sentences if not s.endswith("?")]
    if not ideas:
        return None
    if ordinal == "last":
        pick = ideas[-1]
    elif ordinal:
        if _ORDINAL[ordinal] >= len(ideas):
            return None
        pick = ideas[_ORDINAL[ordinal]]
    elif len(ideas) == 1:
        pick = ideas[0]
    else:
        return None                                        # "that idea" after several: ask which one
    return re.sub(r"^(?:first|second|third|fourth|fifth|another|or)\W+(?:idea\W+)?", "", pick,
                  flags=re.IGNORECASE).strip().rstrip(".")


def ideas_handle(message: str) -> str | None:
    from arya.core import policy
    m = _SAVE_IDEA.match(message)
    if m:
        text = (m.group("t") or m.group("t2") or "").strip(" '\"“”.,")
        if not text:
            text = _idea_from_last_answer(m.group("ord").lower() if m.group("ord") else None) or ""
        if not text:
            return "Which one should I save — the first, the second, or another?"
        add(text[:300], "ideas")
        policy.note_result("save_note")
        return f"Saved to your ideas: {text[:120]}."
    if _SHOW_IDEAS.search(message):
        rows = open_tasks("ideas")
        if not rows:
            return "Your ideas list is empty. In brainstorm mode, say \"save that idea\"."
        return "Your ideas: " + "; ".join(r["text"] for r in rows[-MAX_OPEN_LISTED:]) + "."
    return None


def matches(message: str) -> bool:
    """A to-do command (no side effects) — smart listening counts it as said to ARYA."""
    return bool(_ADD.match(message) or _SHOW.search(message) or _REMOVE.match(message) or _SAVE_IDEA.match(message)
                or _SHOW_IDEAS.search(message))


def _which(m: re.Match | None, mode: str) -> str:
    said = None
    if m:
        for g in ("w", "w2", "w3", "w4"):
            if g in m.re.groupindex and m.group(g):
                said = m.group(g).strip().lower()
                break
    return said or ("work" if mode == "work" else "personal")


def handle(message: str, mode: str = "default") -> str | None:
    from arya.core import policy

    idea = ideas_handle(message)
    if idea:
        return idea
    m = _ADD.match(message)
    if m:
        text = (m.group("t") or m.group("t2") or "").strip(" '\"“”.,")
        if not text:
            return None
        which = _which(m, mode)
        add(text, which)
        policy.note_result("save_note")
        return f"Added to your {which} list: {text}."
    m = _SHOW.search(message)
    if m:
        which = _which(m, mode)
        rows = open_tasks(which)
        if not rows:
            return f"Your {which} list is empty."
        items = [r["text"] for r in rows[:MAX_OPEN_LISTED]]
        more = f", and {len(rows) - MAX_OPEN_LISTED} more" if len(rows) > MAX_OPEN_LISTED else ""
        return f"On your {which} list: " + "; ".join(items) + more + "."
    m = _REMOVE.match(message)
    if m:
        row = find(m.group("t"))
        if row is None:
            return f"I can't find {m.group('t').strip()} on your lists."
        remove(row["id"])
        policy.note_result("save_note")
        return f"Removed {row['text']} from your {row['list']} list."
    m = _DONE.match(message)
    if m:
        row = find(m.group("t"))
        if row is None:
            return None                       # "mark this as read" etc.: not a to-do, let the rest of ARYA try
        finish(row["id"])
        policy.note_result("save_note")
        left = len(open_tasks(row["list"]))
        return f"Done: {row['text']}. " + (f"{left} left on your {row['list']} list." if left else
                                            f"Your {row['list']} list is clear.")
    return None
