"""
Dialogue state: what just happened in the conversation, so follow-ups that refer back are understood.

  "play Kon Tujhe" … "play it again"            -> the same song again
  "next song" … "again" / "one more time"       -> the last action again
  "open YouTube" … "it didn't open"             -> the last action again (only actions safe to repeat)
  "switch to the Gmail tab" … "switch to it"    -> that tab
  "minimize the Chrome window" … "bring it back" -> restore that window
  "find Nico's MCP plan" … "It is in downloads." -> find it in Downloads
  "look at my plan document" … "open it" / "find it in the Windows Explorer"

Before a turn is handled, resolve() rewrites a sentence that refers back into the explicit request, which then
goes through ARYA's usual handlers. After the turn, record() notes which part of ARYA answered and what was named.
Only actions that are safe to repeat are repeated (music, tabs, windows, opening apps / sites / files) — never a
delete, a move, a message or a timer.

Measured with `python -m arya.eval --dialogues` (arya/eval/dialogues.yaml).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

WINDOW_S = 10 * 60                 # a reference reaches back this far
REPEATABLE = {"browser", "media keys", "window control", "open app", "file agent", "music"}


@dataclass
class Action:
    route: str
    request: str
    reply: str
    at: float


@dataclass
class State:
    last: Action | None = None
    entities: dict = field(default_factory=dict)          # kind -> {"value", "at", **extra}


_state = State()


def reset() -> None:
    global _state
    _state = State()


def note_entity(kind: str, value: str, **extra) -> None:
    _state.entities[kind] = {"value": value, "at": time.monotonic(), **extra}


def entity(kind: str) -> dict | None:
    e = _state.entities.get(kind)
    return e if e and time.monotonic() - e["at"] < WINDOW_S else None


def last_action() -> Action | None:
    a = _state.last
    return a if a and time.monotonic() - a.at < WINDOW_S else None


# ── After a turn ─────────────────────────────────────────────────────────────

_PLAYING = re.compile(r"\bPlaying (?P<song>.+?) on (?:YouTube Music|YouTube)\b")
_SWITCHED = re.compile(r"\bSwitched to “(?P<tab>[^”]+)”")
_OPENING = re.compile(r"^Opening (?P<what>[^.,]+?)\.?$")
_WINDOW = re.compile(r"^(?:Minimi[sz]ed|Maximi[sz]ed|Restored)\s+(?P<app>.+?)\.?$")


def record(route: str | None, request: str, reply: str) -> None:
    """Which part of ARYA answered `request` and what it said. Called once per turn."""
    if not route:
        return
    if route in REPEATABLE and not reply.startswith(("I couldn't", "Chrome isn't", "I can't")):
        _state.last = Action(route, request, reply, time.monotonic())
    elif route not in REPEATABLE:
        # A question or chat in between: "it didn't work" now means the answer, not the action before it.
        _state.last = None
    m = _PLAYING.search(reply)
    if m:
        note_entity("song", m.group("song"))
    m = _SWITCHED.search(reply)
    if m:
        note_entity("tab", m.group("tab"))
    if route == "browser":
        from arya.tools import browser
        name = browser.named_tab(request)
        if name and not m:
            note_entity("tab", name)
    if route == "open app":
        m = _OPENING.match(reply.strip())
        if m:
            note_entity("app", m.group("what"))
    if route == "window control":
        m = _WINDOW.match(reply.strip())
        if m:
            note_entity("window", m.group("app"))


# ── Before a turn ────────────────────────────────────────────────────────────

_LEAD = r"^\W*(?:(?:no|nope|but|well|hey|ok|okay|so|and|nj|arya)\W+)*"
# "again", "do it again", "one more time", "same again", "repeat that" — the whole sentence
_AGAIN = re.compile(_LEAD + r"(?:(?:do|try)\s+(?:it|that)\s+)?(?:again|once more|one more time|same again|"
                    r"(?:do|repeat)\s+(?:it|that)(?:\s+again)?|try again)(?:\s+please)?\W*$", re.IGNORECASE)
_VERB_AGAIN = re.compile(_LEAD + r"(?P<verb>play|open|switch to|go to|show)\s+(?:it|that)(?:\s+(?:one|tab|song))?\s+"
                         r"again(?:\s+please)?\W*$", re.IGNORECASE)
# "it didn't open / work", "that didn't work", "No it haven't", "nothing happened", "it's still not open"
_FAILED = re.compile(
    _LEAD + r"(?:(?:it|that|you|the\s+\w+)\s+(?:did\s*n[o']?t|didn'?t|did not|does\s*n[o']?t|doesn'?t|does not|"
    r"has\s*n[o']?t|hasn'?t|have\s*n[o']?t|haven'?t|has not|have not|is\s*n[o']?t|isn'?t|is not)"
    r"(?:\s+(?:work|open|play|switch|change|happen|do anything|resume|start|load|come up|show up|working|"
    r"open(?:ed)?|done))?(?:\s+(?:yet|anything))?"
    r"|nothing(?:'s|\s+has|\s+is)?\s+(?:happened|happening|opened|changed)"
    r"|(?:it'?s|it\s+is)\s+still\s+not\s+(?:open|there|playing|working))\W*$", re.IGNORECASE)
_TO_IT = re.compile(_LEAD + r"(?:please\s+)?(?:switch|go|jump|move)\s+(?:back\s+)?(?:to|over to)\s+(?:it|that|that one|"
                    r"this one|that tab|it again)\W*$", re.IGNORECASE)
_BRING_BACK = re.compile(_LEAD + r"(?:please\s+)?(?:bring\s+(?:it|that)\s+back|restore\s+(?:it|that)|show\s+(?:it|that)"
                         r"\s+again|unminimi[sz]e\s+(?:it|that)|maximi[sz]e\s+(?:it|that))\W*$", re.IGNORECASE)
_OPEN_IT = re.compile(_LEAD + r"(?:please\s+)?(?:open|show)\s+(?:it|that|that one|this one)(?:\s+(?:up|for me))?\W*$",
                      re.IGNORECASE)
_FIND_IT = re.compile(_LEAD + r"(?:please\s+)?(?:find|show|open|locate)\s+(?:it|that)\s+(?:in|on)\s+(?:the\s+|my\s+)?"
                      r"(?P<where>downloads|documents|desktop|(?:windows\s+|file\s+)?explorer)\W*$", re.IGNORECASE)
# "It is in downloads.", "it's in my documents", "look in downloads", "check the desktop"
_IT_IS_IN = re.compile(_LEAD + r"(?:(?:it|that)(?:'s|\s+is|\s+was|\s+should\s+be)\s+(?:in|on)|(?:look|check|search)\s+"
                       r"(?:in|on)?|try)\s+(?:the\s+|my\s+)?(?P<where>downloads|documents|desktop)(?:\s+folder)?\W*$",
                       re.IGNORECASE)


def resolve(message: str) -> str | None:
    """The explicit request a referring follow-up stands for, or None (then the sentence is handled as said)."""
    text = (message or "").strip()
    if not text or len(text.split()) > 10:
        return None
    last = last_action()

    m = _VERB_AGAIN.match(text)
    if m:
        verb = m.group("verb").lower()
        song, tab = entity("song"), entity("tab")
        if verb == "play" and song:
            return f"play {song['value']}"
        if verb in ("switch to", "go to") and tab:
            return f"switch to the {tab['value']} tab"
        if last:
            return last.request
    if _FAILED.match(text) and last and last.route == "media keys":
        return None                  # the music retry presses play again AND checks what's playing (llm.py)
    if (_AGAIN.match(text) or _FAILED.match(text)) and last:
        return last.request
    if _TO_IT.match(text):
        tab = entity("tab")
        if tab:
            return f"switch to the {tab['value']} tab"
    if _BRING_BACK.match(text):
        win = entity("window")
        if win:
            return f"restore the {win['value']} window"
    doc = entity("document")
    m = _FIND_IT.match(text)
    if m and doc:
        where = m.group("where").lower()
        return f"show {doc['value']} file in {'explorer' if 'explorer' in where else where}"
    m = _IT_IS_IN.match(text)
    if m and doc:
        return f"find {doc['value']} file in {m.group('where').lower()}"
    if _OPEN_IT.match(text):
        if doc and doc.get("path"):
            return f"open {doc['value']} file"
        app = entity("app")
        if app:
            return f"open {app['value']}"
    return None
