"""
What ARYA remembers about you (roadmap C4): standing preferences and facts, applied to every answer.

  "Remember that I prefer short answers."          -> preference
  "Remember my lab partner is Anna."               -> fact
  "Always answer in metric units." / "Never use emojis."   -> preference
  "What do you remember about me?"                 -> the list
  "Forget that my lab partner is Anna." / "Forget everything about me."

They go into the local model's instructions on every turn ("follow these"). They never go to the cloud:
in cloud mode, and for "ask the cloud" questions, ARYA leaves them out. "Remember this: <text>" (a note to
look up later) is still a note — this is only for things about you and how you want ARYA to answer.
Stored in arya.db on this laptop.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

MAX_ITEMS = 12
MAX_PROMPT_CHARS = 900

_ABOUT_ME = r"(?:i\b|i'm\b|i am\b|i've\b|i'd\b|my\b|me\b|mine\b|to (?:always|never)\b|you should\b|that i\b|that my\b)"
_REMEMBER = re.compile(rf"^\W*(?:arya\W+)?(?:please\s+)?(?:remember|keep in mind|note|don'?t forget)\s+(?:that\s+)?"
                       rf"(?P<what>{_ABOUT_ME}.+?)\W*$", re.IGNORECASE)
_STANDING = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:from now on,?\s+)?(?P<what>(?:always|never)\s+.{6,}?|"
                       r"call me\s+.+?|from now on,?\s+.{6,}?)\W*$", re.IGNORECASE)
_LIST = re.compile(r"\bwhat do you (?:remember|know) about me\b|\bwhat have i (?:asked|told) you to remember\b|"
                   r"\bmy (?:preferences|profile)\b", re.IGNORECASE)
_FORGET = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?forget\s+(?:that\s+|about\s+)?(?P<what>.+?)\W*$", re.IGNORECASE)
_PREFERENCE = re.compile(r"\b(?:prefer|like|want|always|never|don'?t|do not|please|call me|answer|reply|speak|talk|"
                         r"explain|units?|language|shorter|longer|short|brief|detailed|slow(?:er)?|fast(?:er)?)\b",
                         re.IGNORECASE)


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS profile (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                 "text TEXT NOT NULL, created TEXT NOT NULL)")
    return conn


def items() -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM profile ORDER BY id").fetchall()


def _second_person(text: str) -> str:
    """'I prefer short answers' -> 'The user prefers short answers' (how the model reads it best)."""
    t = text.strip().rstrip(".!")
    t = re.sub(r"^(?:that\s+)?", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^to\s+(always|never)\b", r"\1", t, flags=re.IGNORECASE)          # "to always use …" -> "always use …"
    rules = [(r"^i am\b|^i'm\b", "the user is"), (r"^i've\b", "the user has"), (r"^i'd\b", "the user would"),
             (r"^i\s+(\w+?)(e?s)?\b", None), (r"^my\b", "the user's"), (r"^call me\b", "call the user")]
    for pattern, repl in rules:
        if re.match(pattern, t, re.IGNORECASE):
            if repl is None:                                     # "I prefer" -> "the user prefers"
                verb = re.match(r"^i\s+(\w+)", t, re.IGNORECASE).group(1)
                third = verb if verb.lower() in ("can", "will", "should", "must", "may", "might", "could", "would")\
                    else verb + ("es" if re.search(r"(?:s|sh|ch|x|o)$", verb) else "s")
                return "The user " + third + t[len("i " + verb):]
            return re.sub(pattern, repl, t, count=1, flags=re.IGNORECASE)[:1].upper() + \
                re.sub(pattern, repl, t, count=1, flags=re.IGNORECASE)[1:]
    t = re.sub(r"\bmy\b", "the user's", t, flags=re.IGNORECASE)
    t = re.sub(r"\bme\b", "the user", t, flags=re.IGNORECASE)
    return t[:1].upper() + t[1:]


def _to_you(text: str) -> str:
    """'The user prefers short answers' -> 'You prefer short answers' (for reading the list back)."""
    irregular = {"is": "are", "has": "have", "was": "were", "does": "do"}

    def verb(m: re.Match) -> str:
        v = m.group(1)
        if v in irregular:
            return "You " + irregular[v]
        if v in ("can", "will", "should", "must", "may", "might", "could", "would"):
            return "You " + v
        return "You " + (v[:-2] if re.search(r"(?:s|sh|ch|x|o)es$", v) else v[:-1] if v.endswith("s") else v)

    t = re.sub(r"^The user's\b", "Your", text)
    t = re.sub(r"^The user (\w+)", verb, t)
    return re.sub(r"\bthe user's\b", "your", re.sub(r"\bthe user\b", "you", t))


def add(text: str) -> str:
    kind = "preference" if _PREFERENCE.search(text) else "fact"
    stored = _second_person(text)
    with _db() as c:
        if c.execute("SELECT 1 FROM profile WHERE lower(text) = ?", (stored.lower(),)).fetchone():
            return stored
        c.execute("INSERT INTO profile (kind, text, created) VALUES (?, ?, ?)",
                  (kind, stored, datetime.now().isoformat(timespec="minutes")))
        n = c.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
        if n > MAX_ITEMS:                                          # oldest first out
            c.execute("DELETE FROM profile WHERE id IN (SELECT id FROM profile ORDER BY id LIMIT ?)", (n - MAX_ITEMS,))
    return stored


def forget(what: str) -> int:
    with _db() as c:
        if re.fullmatch(r"(?:everything|all)(?:\s+(?:about me|you know about me|of it))?", what.strip(), re.IGNORECASE):
            return c.execute("DELETE FROM profile").rowcount
        words = [w for w in re.findall(r"[a-zA-Z0-9äöüß]{3,}", what.lower())
                 if w not in {"that", "the", "about", "you", "remember", "prefer", "user", "this", "with"}]
        gone = 0
        for r in c.execute("SELECT id, text FROM profile").fetchall():
            text = r["text"].lower()
            if words and sum(w in text for w in words) >= max(1, (len(words) + 1) // 2):
                c.execute("DELETE FROM profile WHERE id = ?", (r["id"],))
                gone += 1
        return gone


def prompt_block() -> str:
    """For the local model's instructions; empty when there's nothing (or in cloud mode — never sent out)."""
    rows = items()
    if not rows:
        return ""
    lines, size = [], 0
    for r in reversed(rows):                                       # newest first if it has to be cut
        line = f"- {r['text']}"
        if size + len(line) > MAX_PROMPT_CHARS:
            break
        lines.insert(0, line)
        size += len(line)
    return "What the user asked you to remember about them (follow these):\n" + "\n".join(lines) + "\n"


def handle(message: str) -> str | None:
    if _LIST.search(message):
        rows = items()
        if not rows:
            return ("Nothing yet. Tell me things like \"remember that I prefer short answers\" or "
                    "\"remember my lab partner is Anna\".")
        return "I remember: " + "; ".join(_to_you(r["text"]) for r in rows) + "."
    m = _FORGET.match(message)
    if m and not re.search(r"\b(?:it|this|the (?:timer|reminder|note))\b$", m.group("what"), re.IGNORECASE):
        n = forget(m.group("what"))
        if n:
            return f"Okay, I forgot {'that' if n == 1 else f'{n} things'}."
        return None if not items() else "I don't have anything like that remembered about you."
    m = _REMEMBER.match(message) or _STANDING.match(message)
    if m:
        what = m.group("what")
        if len(what.split()) < 2:
            return None
        add(what)
        return "Got it, I'll keep that in mind from now on."
    return None
