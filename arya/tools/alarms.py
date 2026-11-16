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
# Spoken lead-ins: "Just set a timer…", "So set a timer…", "Okay, now set…" (from the log: these reached the model)
_ASK = (r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:(?:just|so|now|and|also|then|alright|right)\W+)*"
        r"(?:(?:can|could|would|will) you\s+|please\s+|i want you to\s+)?(?:please\s+)?(?:just\s+)?")
# "set a timer", "set a 1 hours decision timer for…", "start a ninety minute study countdown"
_TIMER = re.compile(_ASK + r"(?:(?:set|start|make|put on)\s+(?:me\s+)?(?:a|an|the)?\s*(?:[\w.'-]+\s+){0,4}?"
                    r"(?:timer|countdown)\b|timer\s+for\b)", re.IGNORECASE)
_HALF = re.compile(r"\b(?P<n>\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten)\s+and\s+a\s+half\s+"
                   r"(?P<u>hours?|hrs?|minutes?|mins?)\b|\b(?P<n2>an?|one|\d+)\s+(?P<u2>hours?|hrs?|minutes?|mins?)"
                   r"\s+and\s+a\s+half\b", re.IGNORECASE)
# "Is the timer set?", "how much time is left?", "any timers running?" — answered from the real timers. (From the
# log: the model answered these from memory, and the claim guard then blocked even a true answer.)
_STATUS = re.compile(r"\b(?:is|are)\s+(?:the|my|a|any|there\s+(?:a|any))\s+(?:\w+\s+)?(?:timers?|alarms?|reminders?)"
                     r"(?:\s+(?:still\s+)?(?:set|running|on|going|active))?\b|"
                     r"\bhow\s+(?:much|long)\s+(?:time\s+)?(?:is\s+)?(?:left|remaining|to go)\b"
                     r"(?!.*\b(?:until|till|before|for (?:the|my) exam|in the (?:exam|semester|lecture))\b)|"
                     r"\b(?:what|which|any)\s+(?:\w+\s+)?(?:timers?|alarms?|reminders?)\s+(?:are|do i have|is|"
                     r"running|set)\b|\b(?:list|show)\s+(?:my\s+|the\s+)?(?:timers?|alarms?|reminders?)\b|"
                     r"\btime\s+left\s+on\b", re.IGNORECASE)
_NOT_A_LABEL = {"a", "an", "the", "me", "my", "set", "start", "make", "new", "another", "quick", "countdown", "and",
                "half", "minute", "minutes", "hour", "hours", "second", "seconds", "min", "mins", "hr", "hrs"}
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


def duration_seconds(message: str) -> float | None:
    """'1 hour and 30 minutes' -> 5400, 'one and a half hours' / 'an hour and a half' -> 5400,
    'half an hour' -> 1800, '90 minutes' -> 5400. None if no duration is said."""
    total, rest = 0.0, message
    for m in _HALF.finditer(message):
        n, u = (m.group("n"), m.group("u")) if m.group("n") else (m.group("n2"), m.group("u2"))
        total += (_number(n) + 0.5) * _seconds("1", u)
    rest = _HALF.sub(" ", message)
    for m in _DUR.finditer(rest):
        total += _seconds(m.group("n"), m.group("u"))
    return total or None


def _timer_label(message: str) -> str:
    """'…decision timer…' -> 'Decision'; '…timer for the pasta' -> 'Pasta'; else 'Timer'."""
    plain = _HALF.sub(" ", _DUR.sub(" ", message))
    before = re.search(r"\b(?P<l>[a-z]+)\s+(?:timer|countdown)\b", plain, re.IGNORECASE)
    if before and before.group("l").lower() not in _NOT_A_LABEL and not before.group("l").isdigit():
        return before.group("l").capitalize()
    after = re.search(r"\b(?:timer|countdown)\b.*?\bfor (?:the |my )?(?P<l>[a-z][a-z ]{2,30}?)\s*[.!?]?\s*$", plain,
                      re.IGNORECASE)
    if after and not re.search(r"\b(?:timer|a|an)\b", after.group("l")):
        return after.group("l").strip().capitalize()
    return "Timer"


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
    from arya.core import activity
    if activity._BACK_REMIND.match(message):             # "remind me … when I'm back": not a clock time
        return activity.handle(message, remote=policy.remote_turn.get())
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

    if _STATUS.search(message) and not (_TIMER.search(message) or _ALARM.search(message) or _REMIND.search(message)):
        from arya.tools.productivity import list_timers
        policy.note_result("list_timers")
        status = await list_timers()
        back = [r["text"] for r in activity.back_reminders()]
        back_text = (" When you're back at the laptop: " + "; ".join(back) + ".") if back else ""
        if status.startswith("No active"):
            return ("You have no timers or clock reminders." + back_text) if back else \
                "You have no timers, alarms or reminders running."
        return status + back_text
    if _TIMER.search(message) and not _REMIND.search(message):
        seconds = duration_seconds(message)
        if not seconds:
            return "How long should the timer be?"
        from arya.tools.productivity import set_timer
        label = _timer_label(message)
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
