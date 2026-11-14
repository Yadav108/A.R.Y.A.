"""
Corrections memory: ARYA learns from "that's wrong".

  "That's wrong — ζ = 0 is undamped, not critically damped."
      -> saved with the question it corrects, in %LOCALAPPDATA%\\ARYA\\corrections.md
      -> indexed at once as "Your corrections", a source that outranks everything else
  "That's wrong."            -> "What's the correct version? I'll remember it." (next sentence is saved)
  "What have I corrected?"   -> the list
  "Forget correction 2."     -> removed

The file is plain Markdown: you can read and edit it yourself.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from arya.knowledge import library

_CORRECTION = re.compile(
    r"^\W*(?:(?:hey\s+)?(?:arya|aria)\W+)?(?:no\W+)?"
    r"(?:that'?s|that is|this is|you'?re|you are)\s+(?:wrong|not (?:right|correct|true)|incorrect|false)"
    r"\b[\s,.:;!—–-]*(?P<fact>.*)$"
    r"|^\W*(?:(?:hey\s+)?(?:arya|aria)\W+)?correction\s*[:,—–-]\s*(?P<fact2>.+)$",
    re.IGNORECASE | re.DOTALL)
_LIST = re.compile(r"\b(?:what|which|show|list)\b.{0,30}\bcorrections?\b|\bwhat have i corrected\b", re.IGNORECASE)
_FORGET = re.compile(r"\b(?:forget|delete|remove)\s+(?:the\s+)?correction\s*(?:number\s*)?(\d+)\b", re.IGNORECASE)
_HEADER = re.compile(r"(?m)^## Correction (\d+)")
WAIT_FOR_FACT_S = 120.0

_awaiting: dict = {"until": 0.0, "question": "", "answer": ""}


def detect(message: str) -> str | None:
    """The corrected fact ('' when only "that's wrong" was said), or None if this isn't a correction."""
    m = _CORRECTION.match(message.strip())
    if not m:
        return None
    fact = (m.group("fact") or m.group("fact2") or "").strip()
    fact = re.sub(r"^(?:it'?s|it is|actually)\s+", "", fact, flags=re.IGNORECASE).strip(" ,.")
    return fact


def _entries() -> list[tuple[int, str]]:
    if not library.CORRECTIONS.exists():
        return []
    text = library.CORRECTIONS.read_text(encoding="utf-8")
    parts = _HEADER.split(text)
    # parts = [preamble, n1, body1, n2, body2, ...]
    return [(int(parts[i]), parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]


def save(fact: str, question: str = "", wrong_answer: str = "") -> int:
    """Append a correction and index it immediately. Returns its number."""
    entries = _entries()
    number = (max(n for n, _ in entries) + 1) if entries else 1
    lines = [f"## Correction {number}", f"Date: {datetime.now():%Y-%m-%d}"]
    if question:
        lines.append(f"Question: {question.strip()[:300]}")
    lines.append(f"Correct: {fact.strip()}")
    if wrong_answer:
        said_before = re.sub(r"\s+", " ", wrong_answer.strip())[:300]
        lines.append(f"Not: {said_before}")
    block = "\n".join(lines) + "\n\n"
    library.CORRECTIONS.parent.mkdir(parents=True, exist_ok=True)
    if not library.CORRECTIONS.exists():
        library.CORRECTIONS.write_text(
            "# Your corrections to ARYA\n\nARYA treats these as more reliable than anything else. "
            "Edit or delete freely.\n\n", encoding="utf-8")
    with open(library.CORRECTIONS, "a", encoding="utf-8") as f:
        f.write(block)
    library.index_file(library.CORRECTIONS)
    return number


def forget(number: int) -> bool:
    entries = _entries()
    if not any(n == number for n, _ in entries):
        return False
    text = library.CORRECTIONS.read_text(encoding="utf-8")
    text = re.sub(rf"(?ms)^## Correction {number}\n.*?(?=^## Correction \d+|\Z)", "", text)
    library.CORRECTIONS.write_text(text, encoding="utf-8")
    library.index_file(library.CORRECTIONS)
    return True


def listing() -> str:
    entries = _entries()
    if not entries:
        return "You haven't corrected me yet. If I get something wrong, just say \"that's wrong\" and the right version."
    said = []
    for n, body in entries[-5:]:
        fact = next((l[len("Correct: "):] for l in body.splitlines() if l.startswith("Correct: ")), body)
        said.append(f"number {n}: {fact.rstrip('.')}")
    more = f" You've given me {len(entries)} in total; they're in your corrections file." if len(entries) > 5 else ""
    return "Your corrections are " + "; ".join(said) + "." + more


def handle(message: str, last_question: str = "", last_answer: str = "") -> str | None:
    """ARYA's reply when the message is about corrections, else None."""
    if _awaiting["until"] and time.monotonic() < _awaiting["until"] and detect(message) is None:
        _awaiting["until"] = 0.0
        if re.match(r"^\W*(?:never ?mind|forget it|cancel|no|nothing|skip it|doesn'?t matter)\W*$", message, re.I):
            return "Okay, nothing saved."
        n = save(message.strip(), _awaiting["question"], _awaiting["answer"])
        return f"Got it, I'll remember that as correction {n}."
    m = _FORGET.search(message)
    if m:
        n = int(m.group(1))
        return f"Correction {n} is forgotten." if forget(n) else f"I don't have a correction number {n}."
    if _LIST.search(message):
        return listing()
    fact = detect(message)
    if fact is None or not last_answer:                # a correction only makes sense right after an answer
        return None
    if len(fact) < 4:                                  # only "that's wrong": ask for the right version
        _awaiting.update(until=time.monotonic() + WAIT_FOR_FACT_S, question=last_question, answer=last_answer)
        return "Sorry about that. What's the correct version? I'll remember it."
    n = save(fact, last_question, last_answer)
    return f"Thanks, noted as correction {n}: {fact.rstrip('.')}. I'll use that from now on."
