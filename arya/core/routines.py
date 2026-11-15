"""
Your own voice routines (roadmap C4).

  "When I say start my day, give me the briefing, today's calendar and play focus music."
  "Start my day."                      -> the three steps, one after another, as if you'd said each
  "What routines do I have?" / "Delete the start my day routine."

Steps are split at commas, "then" and "and <verb>"; a step without its own verb borrows the previous one
("give me the briefing, today's calendar" -> "give me today's calendar"). Each step is an ordinary request,
so everything ARYA can do can be a step — anything that changes files still asks first, as always.
Kept in arya.db on this laptop.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

MAX_STEPS = 8
_VERBS = (r"play|open|close|start|stop|set|tell|give|show|read|check|what|turn|search|google|put|minimi[sz]e|"
          r"maximi[sz]e|pause|resume|quiz|remind|summari[sz]e|do not|don'?t|mute|unmute|go to|launch|switch|"
          r"take|add|list|explain|how|when|where|is|are|transcribe|tidy|find|plan|let'?s")
_LEADING_VERB = re.compile(rf"^((?:please\s+)?(?:{_VERBS})(?:\s+me)?)\b", re.IGNORECASE)
_DEFINE = re.compile(r"^\W*(?:arya\W+)?(?:(?:when|whenever|if)\s+i\s+say|(?:make|create|add|save)\s+(?:a\s+)?routine"
                     r"(?:\s+(?:called|named))?)\s+[\"'“]?(?P<trigger>[^,:\"'”]+?)[\"'”]?\s*(?:[,:;.]|\s+then\b|"
                     r"\s+(?:you\s+should|please|do)\b)\s*(?P<steps>.+?)\W*$", re.IGNORECASE)
_LIST = re.compile(r"\b(?:what|which|list|show)\b.{0,15}\broutines?\b|\bmy routines\b", re.IGNORECASE)
_DELETE = re.compile(r"\b(?:delete|remove|forget|drop|cancel)\s+(?:the\s+|my\s+)?[\"'“]?(?P<trigger>.+?)[\"'”]?\s+"
                     r"routine\b|\b(?:delete|remove)\s+(?:the\s+)?routine\s+[\"'“]?(?P<trigger2>.+?)[\"'”]?\W*$",
                     re.IGNORECASE)


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS routines (trigger TEXT PRIMARY KEY, spoken TEXT, steps TEXT, created TEXT)")
    return conn


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\s']", " ", text.lower())
    t = re.sub(r"^\s*(?:(?:hey|ok|okay)\s+)?(?:arya|aria)\s+", "", t)
    t = re.sub(r"\s+(?:please|now)\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def split_steps(text: str) -> list[str]:
    parts = re.split(r"\s*[,;]\s*(?:and\s+|then\s+|and\s+then\s+)?|\s+and\s+then\s+|\s+then\s+|"
                     rf"\s+and\s+(?=(?:{_VERBS})\b)", text.strip(), flags=re.IGNORECASE)
    steps, verb = [], ""
    for p in (x.strip(" .") for x in parts):
        if not p:
            continue
        p = re.sub(r"^and\s+", "", p, flags=re.IGNORECASE)
        m = _LEADING_VERB.match(p)
        if m:
            verb = m.group(1)
        elif verb:
            p = f"{verb} {p}"                         # "today's calendar" -> "give me today's calendar"
        steps.append(p)
    return steps[:MAX_STEPS]


def define(trigger: str, steps: list[str]) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO routines (trigger, spoken, steps, created) VALUES (?, ?, ?, ?)",
                  (_norm(trigger), trigger.strip(), json.dumps(steps), datetime.now().isoformat(timespec="minutes")))


def match(message: str) -> list[str] | None:
    """The steps when the message is a routine's trigger phrase, else None."""
    key = _norm(message)
    if not key:
        return None
    with _db() as c:
        row = c.execute("SELECT steps FROM routines WHERE trigger = ?", (key,)).fetchone()
    return json.loads(row["steps"]) if row else None


def _all() -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM routines ORDER BY created").fetchall()


def _spoken_steps(steps: list[str]) -> str:
    s = [re.sub(r"\bmy\b", "your", re.sub(r"\bme\b", "you", x[:1].lower() + x[1:], flags=re.IGNORECASE),
                flags=re.IGNORECASE) for x in steps]                    # "give me the briefing" -> "give you …"
    return s[0] if len(s) == 1 else ", ".join(s[:-1]) + " and then " + s[-1]


def handle(message: str) -> str | None:
    """Define / list / delete routines; running one is match() + the pipeline."""
    m = _DEFINE.match(message)
    if m:
        trigger, steps = m.group("trigger").strip(" '\""), split_steps(m.group("steps"))
        if len(_norm(trigger).split()) > 6 or not steps:
            return None
        if match(trigger) is None and any(match(s) is not None for s in steps):
            return "A routine can't run another routine. Tell me the steps themselves."
        define(trigger, steps)
        return f"Okay. When you say \"{trigger}\", I'll {_spoken_steps(steps)}."
    if _LIST.search(message):
        rows = _all()
        if not rows:
            return ("You have no routines yet. Make one like this: \"when I say start my day, give me the briefing, "
                    "today's calendar and play focus music\".")
        return "Your routines: " + "; ".join(f"\"{r['spoken']}\": {_spoken_steps(json.loads(r['steps']))}"
                                             for r in rows) + "."
    m = _DELETE.search(message)
    if m:
        key = _norm(m.group("trigger") or m.group("trigger2") or "")
        with _db() as c:
            n = c.execute("DELETE FROM routines WHERE trigger = ?", (key,)).rowcount
        return f"Deleted the \"{key}\" routine." if n else f"I don't have a routine called \"{key}\"."
    return None
