"""
World clock (2026-11-17). ARYA's own time is the laptop's: Windows was set to India time while you live in Germany,
so reminders at a clock time and quiet hours were 4.5 hours off; it's now Berlin, and with "set time zone
automatically" on in Windows it follows you when you travel (no online lookup of your IP needed).

  "what time is it?"                          -> here (exact, instantly, not the model's guess)
  "what time is it in India / New York?"      -> anywhere on the list below
  "what time is it at home?"                  -> your home zone (ARYA_HOME_TZ, default Europe/Berlin)
  "is it a good time to call home / India / my family?"  -> daytime there (8:00-22:00)? if not, when it is, in your time
  "what's the time difference with India?"

ARYA_SECOND_TZ (default Asia/Kolkata, "India") is the place "call home" / "my family" means.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HOME_TZ = os.getenv("ARYA_HOME_TZ", "Europe/Berlin")
SECOND_TZ = os.getenv("ARYA_SECOND_TZ", "Asia/Kolkata")
SECOND_NAME = os.getenv("ARYA_SECOND_NAME", "India")
CALL_FROM, CALL_UNTIL = 8, 22

PLACES = {
    "india": "Asia/Kolkata", "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata", "kolkata": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata", "pune": "Asia/Kolkata", "jaipur": "Asia/Kolkata", "lucknow": "Asia/Kolkata",
    "germany": "Europe/Berlin", "berlin": "Europe/Berlin", "munich": "Europe/Berlin", "frankfurt": "Europe/Berlin",
    "cologne": "Europe/Berlin", "hamburg": "Europe/Berlin", "schweinfurt": "Europe/Berlin", "julich": "Europe/Berlin",
    "jülich": "Europe/Berlin", "würzburg": "Europe/Berlin", "wurzburg": "Europe/Berlin",
    "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London", "paris": "Europe/Paris",
    "france": "Europe/Paris", "amsterdam": "Europe/Amsterdam", "zurich": "Europe/Zurich", "vienna": "Europe/Vienna",
    "rome": "Europe/Rome", "madrid": "Europe/Madrid", "moscow": "Europe/Moscow", "istanbul": "Europe/Istanbul",
    "dubai": "Asia/Dubai", "uae": "Asia/Dubai", "kathmandu": "Asia/Kathmandu", "nepal": "Asia/Kathmandu",
    "singapore": "Asia/Singapore", "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "china": "Asia/Shanghai",
    "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo", "seoul": "Asia/Seoul", "korea": "Asia/Seoul",
    "sydney": "Australia/Sydney", "australia": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "new york": "America/New_York", "nyc": "America/New_York", "boston": "America/New_York",
    "washington": "America/New_York", "toronto": "America/Toronto", "chicago": "America/Chicago",
    "texas": "America/Chicago", "denver": "America/Denver", "california": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles", "los angeles": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "utc": "UTC", "gmt": "UTC",
}
_HOME_WORDS = r"(?:home|my family|family|mom|mum|mummy|dad|papa|my parents|parents)"


def spoken(t: datetime) -> str:
    """'8:45 in the evening' — the way a person says it."""
    h, m = t.hour, t.minute
    part = ("at night" if h < 5 or h >= 22 else "in the morning" if h < 12 else "in the afternoon" if h < 17
            else "in the evening")
    h12 = h % 12 or 12
    return f"{h12}{f':{m:02d}' if m else ''} {part}"


def now_in(tz: str, now: datetime | None = None) -> datetime:
    base = now or datetime.now().astimezone()
    return base.astimezone(ZoneInfo(tz))


def _day_note(there: datetime, here: datetime) -> str:
    d = (there.date() - here.date()).days
    return " tomorrow" if d == 1 else " yesterday" if d == -1 else ""


def _difference(tz: str, now: datetime) -> str:
    here = now.utcoffset() or timedelta(0)
    there = now_in(tz, now).utcoffset() or timedelta(0)
    mins = int((there - here).total_seconds() // 60)
    if mins == 0:
        return "the same time as here"
    h, m = divmod(abs(mins), 60)
    amount = f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "") if h else f"{m} minutes"
    return f"{amount} {'ahead of' if mins > 0 else 'behind'} you"


# ── Voice ─────────────────────────────────────────────────────────────────────

_PLACE = "(?P<place>" + "|".join(sorted((re.escape(p) for p in PLACES), key=len, reverse=True)) + ")"
_TIME_HERE = re.compile(r"^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?(?:(?:can you\s+)?tell me\s+)?what(?:'s|\s+is)\s+the\s+time"
                        r"(?:\s+now)?\W*$|^\W*(?:(?:hey|ok|okay)\W+)?(?:arya\W+)?what\s+time\s+is\s+it(?:\s+now)?\W*$|"
                        r"^\W*(?:the\s+)?time(?:\s+please)?\W*$", re.IGNORECASE)
_TIME_THERE = re.compile(rf"\b(?:what(?:'s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|time)\s+(?:is\s+it\s+)?(?:right\s+now\s+)?"
                         rf"(?:in|at)\s+(?:the\s+)?{_PLACE}\b", re.IGNORECASE)
_TIME_HOME = re.compile(rf"\bwhat(?:'s|\s+is)?\s+(?:the\s+)?time\s+(?:is\s+it\s+)?(?:back\s+)?(?:at|in)\s+(?:my\s+)?home\b|"
                        r"\btime\s+at\s+home\b", re.IGNORECASE)
_CALL = re.compile(rf"\b(?:good|right|ok|okay|fine|bad)\s+time\s+to\s+(?:call|ring|phone|video\s*call)\s+(?:back\s+)?"
                   rf"(?:{_HOME_WORDS}|{_PLACE.replace('place', 'cplace')})\b|\bcan\s+i\s+(?:call|ring)\s+"
                   rf"(?:{_HOME_WORDS}|{_PLACE.replace('place', 'cplace2')})\s+(?:now|right\s+now)\b", re.IGNORECASE)
_DIFF = re.compile(rf"\btime\s+difference\s+(?:with|to|between\s+here\s+and)\s+(?:the\s+)?{_PLACE.replace('place', 'dplace')}\b",
                   re.IGNORECASE)


def matches(message: str) -> bool:
    return bool(_TIME_HERE.match(message) or _TIME_THERE.search(message) or _TIME_HOME.search(message)
                or _CALL.search(message) or _DIFF.search(message))


def _zone_name(place: str) -> str:
    return {"uk": "the UK", "uae": "the UAE", "nyc": "New York", "utc": "UTC", "gmt": "UTC"}.get(
        place.lower(), place.title())


def handle(message: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now().astimezone()
    m = _CALL.search(message)
    if m:
        place = m.group("cplace") or m.group("cplace2")
        tz, name = (PLACES[place.lower()], _zone_name(place)) if place else (SECOND_TZ, SECOND_NAME)
        there = now_in(tz, now)
        if CALL_FROM <= there.hour < CALL_UNTIL:
            return f"Yes, it's {spoken(there)} in {name}, a good time to call."
        start = there.replace(hour=CALL_FROM, minute=0, second=0, microsecond=0)
        if there.hour >= CALL_UNTIL:
            start += timedelta(days=1)
        here_then = start.astimezone(now.tzinfo)
        return (f"Better not, it's {spoken(there)} in {name}. From {CALL_FROM} in the morning there works, "
                f"that's {spoken(here_then)} here{_day_note(here_then, now)}.")
    m = _DIFF.search(message)
    if m:
        tz = PLACES[m.group("dplace").lower()]
        return f"{_zone_name(m.group('dplace'))} is {_difference(tz, now)}."
    if _TIME_HOME.search(message):
        there = now_in(HOME_TZ, now)
        return f"At home it's {spoken(there)}{_day_note(there, now)}."
    m = _TIME_THERE.search(message)
    if m:
        place = m.group("place")
        there = now_in(PLACES[place.lower()], now)
        return f"In {_zone_name(place)} it's {spoken(there)}{_day_note(there, now)}."
    if _TIME_HERE.match(message):
        return f"It's {spoken(now)}."
    return None
