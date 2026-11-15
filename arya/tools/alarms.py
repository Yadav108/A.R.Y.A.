"""
Timers, alarms and reminders by voice — handled directly, never left to the model (which used to *say*
"Alarm set for 08:00" without setting anything).

  "Set a timer for 23 minutes"                     -> timer
  "Set an alarm for 8 a.m. tomorrow" / "wake me up at 7"   -> alarm (a reminder that says it's time)
  "Remind me tomorrow at 13:00 to send my working hours to my supervisor"
  "Remind me in 20 minutes to take the laundry out"

Missing pieces are asked for and filled in from your next answer, over several turns:
  "Remind me tomorrow at…" -> "At what time?" -> "13:00" -> "What should I remind you about?" -> "to send …"
Said after midnight, "tomorrow" means the coming morning (at 2 a.m., "tomorrow at 13:00" is today 13:00).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SLOT_TTL_S = 180.0
NIGHT_OWL_UNTIL = 5            # before 5 a.m., "tomorrow" is the day that's just started

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "half an": 0.5}
_WHEN = re.compile(
    r"\b(?:at|for|by)?\s*(?P<clock>\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?\s*m\.?|p\.?\s*m\.?)?|noon|midnight)"
    r"(?=\s|$|[,.!?])(?!\s*(?:minutes?|mins?|hours?|hrs?|seconds?|secs?|percent|%))", re.IGNORECASE)
_IN = re.compile(r"\bin\s+(?P<n>\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|"
                 r"thirty|forty|forty-five|half an)\s*(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)
_DUR = re.compile(r"\b(?P<n>\d+(?:\.\d+)?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|"
                  r"forty|forty-five|half an)[\s-]*(?P<u>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)
_ASK = r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:(?:can|could|would|will) you\s+|please\s+|i want you to\s+)?(?:please\s+)?"
_TIMER = re.compile(_ASK + r"(?:(?:set|start|make|put on)\s+(?:me\s+)?(?:a|an|the)?\s*(?:\d+[\s-]*(?:minute|min|second|"
                    r"hour)s?\s+)?(?:timer|countdown)\b|timer\s+for\b)", re.IGNORECASE)
_ALARM = re.compile(_ASK + r"(?:(?:set|create|make|put)\s+(?:me\s+)?(?:an?|the|my)?\s*alarm\b|wake me(?: up)?\b|"
                    r"alarm\s+(?:for|at)\b)", re.IGNORECASE)
_REMIND = re.compile(r"\bremind me\b|\bset (?:a|an) reminder\b|\bdon'?t let me forget\b", re.IGNORECASE)
_WHAT = re.compile(r"\b(?:to|about|that|of)\s+(?P<what>.+)$", re.IGNORECASE)
_FAREWELL = re.compile(r"[\s,.!]*(?:good ?night|bye(?:-bye)?|thanks?(?: you)?|thank you|see you|cheers)[\s,.!]*$",
                       re.IGNORECASE)
_CANCEL = re.compile(r"^\W*(?:no|nope|never ?mind|cancel|forget it|stop)\W*$", re.IGNORECASE)


@dataclass
class Slot:
    kind: str                         # "alarm" | "reminder"
    when: datetime | None = None
    what: str = ""
    tomorrow: bool = False
    until: float = field(default_factory=lambda: time.monotonic() + SLOT_TTL_S)


_slot: Slot | None = None


def awaiting() -> bool:
    return _slot is not None and time.monotonic() < _slot.until


def _number(raw: str) -> float:
    raw = raw.lower()
    return float(_NUM[raw]) if raw in _NUM else float(raw)


def _seconds(n: str, unit: str) -> float:
    u = unit.lower()
    return _number(n) * (1 if u.startswith("s") else 3600 if u.startswith("h") else 60)


def _clock_text(raw: str, message: str) -> str:
    t = raw.lower().replace(" ", "").replace("a.m.", "am").replace("p.m.", "pm").replace("a.m", "am").replace("p.m", "pm")
    if t == "noon":
        return "12:00"
    if t == "midnight":
        return "0:00"
    if not re.search(r"am|pm", t):
        if re.search(r"\b(?:morning)\b", message, re.IGNORECASE):
            t += "am"
        elif re.search(r"\b(?:afternoon|evening|tonight|night)\b", message, re.IGNORECASE) and \
                int(re.match(r"\d+", t).group()) < 12:
            t += "pm"
    return t


def when_in(message: str, now: datetime) -> tuple[datetime | None, bool]:
    """(target time, 'tomorrow' was said). Relative ('in 20 minutes') or a clock time with an optional day."""
    from arya.tools.productivity import _parse_clock

    m = _IN.search(message)
    if m:
        return now + timedelta(seconds=_seconds(m.group("n"), m.group("u"))), False
    tomorrow = bool(re.search(r"\btomorrow'?s?\b", message, re.IGNORECASE))
    m = _WHEN.search(message)
    if not m:
        return None, tomorrow
    target = _parse_clock(_clock_text(m.group("clock"), message), now)
    if target is None:
        return None, tomorrow
    if tomorrow:
        day = now.date() + timedelta(days=0 if now.hour < NIGHT_OWL_UNTIL else 1)
        target = datetime.combine(day, target.time())
        if target <= now:
            target += timedelta(days=1)
    return target, tomorrow


def _what_in(message: str, kind: str) -> str:
    text = _FAREWELL.sub("", message).strip(" .,!?")
    text = re.sub(r"\b(?:in\s+\S+\s+(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)|(?:at|for|by)\s+\d{1,2}(?:[:.]\d{2})?"
                  r"\s*(?:a\.?\s*m\.?|p\.?\s*m\.?)?|tomorrow'?s?(?:\s+morning|\s+evening)?|tonight|today|"
                  r"this (?:morning|afternoon|evening))\b", " ", text, flags=re.IGNORECASE)
    m = _WHAT.search(re.sub(r"^.*?\bremind me\b", "", text, flags=re.IGNORECASE)) if kind == "reminder" else None
    if m:
        return re.sub(r"\s+", " ", m.group("what")).strip(" .,")
    return ""


def _spoken_when(target: datetime, now: datetime) -> str:
    clock = target.strftime("%H:%M")
    if target.date() == now.date():
        if now.hour < NIGHT_OWL_UNTIL:                  # at 2 a.m., "today" sounds wrong: "this morning"
            part = "this morning" if target.hour < 12 else "this afternoon" if target.hour < 18 else "this evening"
            return f"at {clock} {part}"
        return f"at {clock} today"
    if target.date() == now.date() + timedelta(days=1):
        return f"at {clock} tomorrow"
    return f"at {clock} on {target.strftime('%A')}"


async def _set(slot: Slot, now: datetime) -> str:
    from arya.core import policy
    from arya.tools.productivity import set_reminder

    minutes = (slot.when - now).total_seconds() / 60
    if slot.kind == "alarm":
        await set_reminder(f"Wake up, it's {slot.when.strftime('%H:%M')}. This is your alarm.", in_minutes=minutes)
        policy.note_result("set_reminder")
        return f"Alarm set {_spoken_when(slot.when, now)}."
    await set_reminder(slot.what, in_minutes=minutes)
    policy.note_result("set_reminder")
    return f"Okay, I'll remind you {_spoken_when(slot.when, now)} to {slot.what}."


async def _continue(slot: Slot, message: str, now: datetime) -> str:
    """Ask for what's missing, or set it."""
    global _slot
    if slot.when is None:
        _slot = slot
        return "At what time? For example \"at 8 a.m.\" or \"in 20 minutes\"."
    if slot.kind == "reminder" and not slot.what:
        _slot = slot
        return "What should I remind you about?"
    _slot = None
    return await _set(slot, now)


async def handle(message: str, now: datetime | None = None) -> str | None:
    global _slot
    from arya.core import policy

    now = now or datetime.now()
    if awaiting():                                       # the answer to "At what time?" / "about what?"
        slot = _slot
        if _CANCEL.match(message):
            _slot = None
            return "Okay, no reminder."
        if not (_TIMER.search(message) or _ALARM.search(message) or _REMIND.search(message)):
            # "remind me tomorrow …" -> "at 9": the day from the first turn still counts
            when, tomorrow = when_in(("tomorrow " if slot.tomorrow and not _IN.search(message) else "") + message, now)
            if slot.when is None and when is not None:
                slot.when = when
                what = _what_in("remind me " + message, "reminder") if slot.kind == "reminder" else ""
                slot.what = slot.what or what
                return await _continue(slot, message, now)
            if slot.kind == "reminder" and slot.when is not None and not slot.what:
                what = _FAREWELL.sub("", message).strip(" .,!?")
                slot.what = re.sub(r"^(?:to|about|that)\s+", "", what, flags=re.IGNORECASE)
                return await _continue(slot, message, now)
        _slot = None                                     # a different request: drop the half-made reminder

    if _TIMER.search(message) and not _REMIND.search(message):
        m = _DUR.search(message)
        if not m:
            return "How long should the timer be?"
        from arya.tools.productivity import set_timer
        seconds = _seconds(m.group("n"), m.group("u"))
        label = "Timer"
        lab = re.search(r"\bfor (?:the |my )?(?P<l>[a-z][a-z ]{2,30}?)\s*$", _DUR.sub("", message), re.IGNORECASE)
        if lab and not re.search(r"\b(?:timer|a|an)\b", lab.group("l")):
            label = lab.group("l").strip().capitalize()
        await set_timer(int(seconds), label)
        policy.note_result("set_timer")
        from arya.tools.productivity import _speak_duration
        return f"Timer set for {_speak_duration(seconds)}." if label == "Timer" else \
            f"{label} timer set for {_speak_duration(seconds)}."
    if _ALARM.search(message):
        when, tomorrow = when_in(message, now)
        return await _continue(Slot("alarm", when, tomorrow=tomorrow), message, now)
    if _REMIND.search(message):
        when, tomorrow = when_in(message, now)
        return await _continue(Slot("reminder", when, _what_in(message, "reminder"), tomorrow), message, now)
    return None
