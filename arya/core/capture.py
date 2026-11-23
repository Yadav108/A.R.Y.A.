"""
Quick capture and voice notes (roadmap E) — get a thought out of your head in one sentence, from the laptop or the
phone, and ARYA files it.

  "note: buy resistors"  "capture: ask Ben about the battery data"  "jot down: what if the gripper had suction"
  "remember to call the dentist"
      -> sorted at once: a to-do (work or personal list), an idea (ideas list) or a note (your notes), and kept in
         an inbox so "what did I capture today?" and the morning briefing can read it back
  "record a voice note" … "stop recording"
      -> everything you say in between (up to 10 minutes) is transcribed with ARYA's Whisper; while recording,
         ARYA doesn't answer what you say. A short one is filed like a capture; a longer one is kept as a note
         and in the library (so "what did I say about the gripper?" finds it).
  From the phone: 🎙 opens the phone's own recorder; the recording comes back here the same way.
The sorting is a few clear rules (measured in tests), not the model — a to-do never turns into something else.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from datetime import date, datetime

SHORT_WORDS = 25                 # a voice note this short is filed like a capture
DB_OVERRIDE = None

_VERBS = ("buy|call|email|e-mail|mail|text|message|book|send|fix|ask|pay|finish|submit|order|pick up|return|check|"
          "write|read|print|clean|cancel|renew|reply|tell|remind|bring|get|make|prepare|review|update|install|"
          "schedule|register|apply|sign|upload|download|charge|repair|wash|water|feed|post|ship|test|debug|study|"
          "learn|practise|practice|revise|plan|organize|organise|clean up|follow up|look up|figure out")
_TODO = re.compile(rf"^(?:to\s+|i\s+(?:need|have|must|should)\s+to\s+|need\s+to\s+|have\s+to\s+|must\s+|don'?t\s+forget\s+"
                   rf"(?:to\s+)?|remember\s+to\s+|todo\s*:?\s*)?(?:{_VERBS})\b", re.IGNORECASE)
_EXPLICIT_TODO = re.compile(r"^(?:i\s+(?:need|have|must)\s+to|need\s+to|have\s+to|don'?t\s+forget|remember\s+to|"
                            r"todo\b)", re.IGNORECASE)
_IDEA = re.compile(r"\bidea\b|^what\s+if\b|^how\s+about\b|^maybe\s+(?:we|i)\s+could\b|^(?:we|i)\s+could\b|"
                   r"\bwould\s+be\s+(?:cool|nice|fun|great)\b", re.IGNORECASE)
_WORK = re.compile(r"\b(?:lab|supervisor|professor|prof|paper|thesis|experiment|measurement|data|report|meeting|"
                   r"institute|PR|pull request|code|bug|deploy|server|repo|script|slides|presentation|deadline|"
                   r"colleague|group|project|review)\b", re.IGNORECASE)


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH
    c = sqlite3.connect(DB_OVERRIDE or DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS inbox (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL, "
              "source TEXT NOT NULL, filed_as TEXT NOT NULL)")
    return c


def sort(text: str, mode: str = "default") -> str:
    """'todo-work' / 'todo-personal' / 'idea' / 'note'."""
    t = text.strip()
    if _EXPLICIT_TODO.match(t):                    # "remember to tell Ben my idea" is still a to-do
        return "todo-work" if (mode == "work" or _WORK.search(t)) else "todo-personal"
    if _IDEA.search(t):
        return "idea"
    if _TODO.match(t):
        return "todo-work" if (mode == "work" or _WORK.search(t)) else "todo-personal"
    return "note"


def _clean_todo(text: str) -> str:
    t = re.sub(r"^(?:to\s+|i\s+(?:need|have|must|should)\s+to\s+|need\s+to\s+|have\s+to\s+|must\s+|don'?t\s+forget\s+(?:to\s+)?|"
               r"remember\s+to\s+|todo\s*:?\s*)", "", text.strip(), flags=re.IGNORECASE)
    return t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t


async def file(text: str, source: str = "typed", mode: str | None = None) -> str:
    """File one capture and say where it went."""
    from arya.core import policy
    from arya.tools import tasks
    if mode is None:
        from arya.core import modes
        mode = modes.current_name()
    text = text.strip().strip("\"“”").rstrip(".")
    kind = sort(text, mode)
    if kind.startswith("todo"):
        which = kind.split("-")[1]
        tasks.add(_clean_todo(text), which)
        where = f"your {which} to-dos"
    elif kind == "idea":
        tasks.add(re.sub(r"^(?:idea\s*[:,-]\s*)", "", text, flags=re.IGNORECASE), "ideas")
        where = "your ideas"
    else:
        from arya.tools.notes import save_note
        await save_note(text, tag="inbox")
        where = "your notes"
    with _db() as c:
        c.execute("INSERT INTO inbox (text, created, source, filed_as) VALUES (?, ?, ?, ?)",
                  (text, time.time(), source, kind))
    policy.note_result("save_note")
    return where


def captured_since(since: float) -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM inbox WHERE created >= ? ORDER BY created", (since,)).fetchall()


def briefing_line(today: date | None = None) -> str:
    today = today or date.today()
    since = datetime.combine(today, datetime.min.time()).timestamp() - 86400
    rows = captured_since(since)
    if not rows:
        return ""
    return f"Captured since yesterday ({len(rows)}): " + "; ".join(r["text"][:60] for r in rows[-5:])


# ── Voice notes ───────────────────────────────────────────────────────────────

_engine = None                   # the wake-word engine (it owns the microphone)
_started = {"at": 0.0}
MAX_S = 10 * 60


def set_engine(engine) -> None:
    global _engine
    _engine = engine


def recording() -> bool:
    return _engine is not None and getattr(_engine, "recording_note", lambda: False)()


def _transcribe(audio) -> str:
    from arya.core import stt
    return stt._run_with_cpu_fallback(audio.astype("float32", copy=False)).strip()


_TRAILING_STOP = re.compile(r"[\s,.]*(?:(?:hey|okay|ok)\s+)?(?:arya|aria|nj)?[\s,.]*(?:stop|end|finish|save)\s+(?:the\s+)?"
                            r"(?:voice\s+)?(?:note|memo|recording)[\s.!]*$", re.IGNORECASE)


async def finish_note(text: str, seconds: float, source: str) -> str:
    """File a transcribed voice note: short -> like a capture; long -> a note (+ the library)."""
    text = _TRAILING_STOP.sub("", text).strip()
    if not re.search(r"\w", text):
        return "I didn't catch anything in that voice note."
    length = f"{int(seconds // 60)} min {int(seconds % 60)} s" if seconds >= 60 else f"{int(seconds)} s"
    if len(text.split()) <= SHORT_WORDS:
        where = await file(text, source=source)
        return f"Voice note ({length}) filed in {where}: “{text}”."
    from arya.tools.notes import save_note
    await save_note(text, tag="voice note")
    try:                                                   # findable later: "what did I say about …?"
        from arya.core import attachments
        from arya.knowledge import library
        folder = attachments.uploads_dir() / "voice-notes"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"Voice note {datetime.now():%Y-%m-%d %H-%M}.md"
        path.write_text(f"# Voice note, {datetime.now():%d %B %Y %H:%M}\n\n{text}\n", encoding="utf-8")
        await asyncio.to_thread(library.index_file, path)
    except Exception as exc:
        print(f"Voice note: not added to the library ({type(exc).__name__})")
    with _db() as c:
        c.execute("INSERT INTO inbox (text, created, source, filed_as) VALUES (?, ?, ?, ?)",
                  (text[:500], time.time(), source, "voice note"))
    words = " ".join(text.split()[:12])
    return f"Voice note saved ({length}, {len(text.split())} words): “{words}…”"


_START_NOTE = re.compile(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:please\s+)?(?:(?:record|take|start)\s+(?:a\s+)?"
                         r"voice\s+(?:note|memo)|record\s+a\s+(?:note|memo)|start\s+recording(?:\s+a\s+(?:voice\s+)?note)?)"
                         r"(?:\s+please)?\W*$", re.IGNORECASE)
_STOP_NOTE = re.compile(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:please\s+)?(?:(?:stop|end|finish|save)\s+(?:the\s+)?"
                        r"(?:voice\s+)?(?:note|memo|recording)|(?:that'?s\s+it|done)\W+(?:save|stop)\s+(?:it|the\s+note))"
                        r"(?:\s+please)?\W*$", re.IGNORECASE)
_CAPTURE = re.compile(r"^\W*(?:arya\W+)?(?:note|capture|inbox|jot(?:\s+down)?|quick\s+note|add\s+to\s+(?:my\s+)?inbox)"
                      r"\s*[:,–-]\s*(?P<t>.+?)\s*$|^\W*(?:arya\W+)?(?:please\s+)?(?:jot\s+down|capture)\s+(?!(?:the\s+|a\s+|my\s+|this\s+)?(?:screen|screenshot|window|photo|picture|image|"
                      r"webcam|camera|video|audio|frame|moment)\b)(?P<t2>.+?)\s*$|"
                      r"^\W*(?:arya\W+)?(?:please\s+)?(?P<t3>(?:remember|don'?t\s+forget)\s+to\s+.+?)\s*$", re.IGNORECASE)
_SHOW = re.compile(r"\bwhat(?:'s|\s+is)\s+in\s+my\s+inbox\b|\bwhat\s+did\s+i\s+(?:capture|note|jot\s+down)\s+today\b|"
                   r"^\W*(?:show|read)\s+(?:me\s+)?my\s+inbox\W*$", re.IGNORECASE)


def matches(message: str) -> bool:
    return bool(_START_NOTE.match(message) or _STOP_NOTE.match(message) or _CAPTURE.match(message)
                or _SHOW.search(message))


async def handle(message: str, remote: bool = False) -> str | None:
    if _START_NOTE.match(message):
        if remote:
            return ("In the ARYA app, say \"record a voice note\" on the talk screen, or tap + and then "
                    "\"Record a voice note\".")
        if _engine is None or not hasattr(_engine, "start_note"):
            return "I can't record right now: the microphone isn't listening."
        if recording():
            return "I'm already recording. Say \"stop recording\" when you're done."
        _engine.start_note()
        _started["at"] = time.monotonic()
        asyncio.get_running_loop().create_task(_stop_after(MAX_S))
        return "Recording. Talk as long as you like; say \"Hey ARYA, stop recording\" when you're done."
    if _STOP_NOTE.match(message):
        if not recording():
            return None if remote else "I'm not recording anything."
        return await stop_and_file("voice note")
    m = _CAPTURE.match(message)
    if m:
        text = (m.group("t") or m.group("t2") or m.group("t3") or "").strip()
        if len(text.split()) < 2:
            return None
        where = await file(text, source="phone" if remote else "typed")
        return f"Got it, in {where}."
    if _SHOW.search(message):
        start = datetime.combine(date.today(), datetime.min.time()).timestamp()
        rows = captured_since(start)
        if not rows:
            return "Nothing captured today. Say \"note:\" and the thought, or \"record a voice note\"."
        names = {"todo-work": "work to-do", "todo-personal": "to-do", "idea": "idea", "note": "note",
                 "voice note": "voice note"}
        return "Today: " + "; ".join(f"{r['text'][:60]} ({names.get(r['filed_as'], r['filed_as'])})" for r in rows[-6:]) + "."
    return None


async def stop_and_file(source: str) -> str:
    audio = _engine.stop_note() if _engine is not None else None
    seconds = time.monotonic() - _started["at"]
    if audio is None or len(audio) < 16000 * 0.5:
        return "That voice note was empty."
    text = await asyncio.to_thread(_transcribe, audio)
    return await finish_note(text, seconds, source)


async def _stop_after(seconds: float) -> None:
    started = _started["at"]
    await asyncio.sleep(seconds)
    if recording() and _started["at"] == started:
        reply = await stop_and_file("voice note")
        if _say is not None:
            await _say("Ten minutes is the limit for a voice note, so I stopped. " + reply)


_say = None


def set_announcer(say) -> None:
    global _say
    _say = say
