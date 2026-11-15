"""
Calendar (Phase B), local first.

Where events come from:
  - ARYA's own events: "add Dynamics lecture on Tuesday at 10", "add lab every Thursday at 14:00".
    Kept in SQLite on this laptop.
  - .ics files in %LOCALAPPDATA%\\ARYA\\calendar\\ — export from Google Calendar / Outlook / your
    university timetable and drop them there. Recurring events (weekly lectures) are expanded.
  - Optionally ARYA_CALENDAR_URLS (comma-separated "secret iCal address" links): fetched every
    30 minutes into that folder. Your own calendar coming *in*; nothing is sent out.

Voice: "what's on today / tomorrow / on Friday", "what's next", "when is my next lecture",
"add …", "remove … from my calendar". The briefing mentions today's events and the nudge loop
gives a heads-up ten minutes before a timed event.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA")
ICS_DIR = DATA_DIR / "calendar"
FETCH_EVERY_S = 30 * 60
DEFAULT_LENGTH = timedelta(hours=1)


@dataclass(frozen=True)
class Event:
    title: str
    start: datetime                  # naive local time
    end: datetime
    all_day: bool = False
    location: str = ""
    source: str = "ARYA"

    @property
    def key(self) -> str:
        return f"{self.title}|{self.start.isoformat()}"


# ── ARYA's own events ─────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, "
                 "start TEXT NOT NULL, end TEXT NOT NULL, weekly INTEGER DEFAULT 0, location TEXT DEFAULT '')")
    return conn


def add_event(title: str, start: datetime, end: datetime | None = None, weekly: bool = False,
              location: str = "") -> Event:
    end = end or start + DEFAULT_LENGTH
    with _db() as c:
        c.execute("INSERT INTO events (title, start, end, weekly, location) VALUES (?, ?, ?, ?, ?)",
                  (title, start.isoformat(), end.isoformat(), int(weekly), location))
    return Event(title, start, end, location=location)


def remove_events(title: str) -> int:
    with _db() as c:
        return c.execute("DELETE FROM events WHERE lower(title) LIKE ?", (f"%{title.lower()}%",)).rowcount


def _own_between(start: datetime, end: datetime) -> list[Event]:
    out = []
    with _db() as c:
        rows = c.execute("SELECT * FROM events").fetchall()
    for r in rows:
        s, e = datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])
        if r["weekly"]:
            if s < start:                                       # jump to the first week at or after `start`
                weeks = (start - s).days // 7
                s, e = s + timedelta(weeks=weeks), e + timedelta(weeks=weeks)
            while s < end:
                if e > start:
                    out.append(Event(r["title"], s, e, location=r["location"]))
                s, e = s + timedelta(weeks=1), e + timedelta(weeks=1)
        elif s < end and e > start:
            out.append(Event(r["title"], s, e, location=r["location"]))
    return out


# ── .ics calendars ────────────────────────────────────────────────────────────

_ics_cache: dict[Path, tuple[float, object]] = {}


def _local(value) -> tuple[datetime, bool]:
    """An iCalendar date/datetime as naive local time, plus 'all day'."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value, False
    return datetime.combine(value, datetime.min.time()), True


def _ics_between(start: datetime, end: datetime, folder: Path | None = None) -> list[Event]:
    import icalendar
    import recurring_ical_events

    folder = folder or ICS_DIR
    out = []
    for path in sorted(folder.glob("*.ics")) if folder.is_dir() else []:
        try:
            mtime = path.stat().st_mtime
            cached = _ics_cache.get(path)
            if not cached or cached[0] != mtime:
                cached = (mtime, icalendar.Calendar.from_ical(path.read_bytes()))
                _ics_cache[path] = cached
            for ev in recurring_ical_events.of(cached[1]).between(start, end):
                s, all_day = _local(ev.get("DTSTART").dt)
                e_raw = ev.get("DTEND")
                e = _local(e_raw.dt)[0] if e_raw else s + (timedelta(days=1) if all_day else DEFAULT_LENGTH)
                out.append(Event(str(ev.get("SUMMARY") or "Untitled event"), s, e, all_day,
                                 str(ev.get("LOCATION") or ""), path.stem))
        except Exception as exc:
            print(f"Calendar: couldn't read {path.name} ({type(exc).__name__}: {exc})")
    return out


async def refresh_subscriptions() -> int:
    """Download ARYA_CALENDAR_URLS into the calendar folder. Returns how many were updated."""
    urls = [u.strip() for u in os.getenv("ARYA_CALENDAR_URLS", "").split(",") if u.strip()]
    if not urls:
        return 0
    import httpx

    ICS_DIR.mkdir(parents=True, exist_ok=True)
    done = 0
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for i, url in enumerate(urls, 1):
            try:
                r = await client.get(url.replace("webcal://", "https://", 1))
                r.raise_for_status()
                if b"BEGIN:VCALENDAR" not in r.content[:2000]:
                    raise ValueError("not an iCalendar file")
                (ICS_DIR / f"subscription-{i}.ics").write_bytes(r.content)
                done += 1
            except Exception as exc:                      # the link is a secret: never print it
                print(f"Calendar: subscription {i} not updated ({type(exc).__name__})")
    return done


# ── Queries ───────────────────────────────────────────────────────────────────

def events_between(start: datetime, end: datetime) -> list[Event]:
    seen, out = set(), []
    for ev in sorted(_own_between(start, end) + _ics_between(start, end), key=lambda e: (not e.all_day, e.start)):
        if ev.key not in seen:
            seen.add(ev.key)
            out.append(ev)
    return out


def day_events(day: date) -> list[Event]:
    start = datetime.combine(day, datetime.min.time())
    return events_between(start, start + timedelta(days=1))


def next_event(now: datetime | None = None, matching: str = "", within_days: int = 60) -> Event | None:
    now = now or datetime.now()
    for ev in events_between(now, now + timedelta(days=within_days)):
        if ev.all_day or ev.start < now:
            continue
        if matching and not re.search(rf"\b{re.escape(matching)}", ev.title, re.IGNORECASE):
            continue
        return ev
    return None


# ── Speaking about events ────────────────────────────────────────────────────

def _clock(t: datetime) -> str:
    return t.strftime("%H:%M")


def _day_name(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if 0 < (day - today).days < 7:
        return "on " + day.strftime("%A")
    return f"on {day.strftime('%A')}, {day.day} {day.strftime('%B')}"


def _one(ev: Event, with_day: date | None = None, today: date | None = None) -> str:
    where = f" in {ev.location}" if ev.location else ""
    if ev.all_day:
        return f"{ev.title} (all day){where}"
    when = f"at {_clock(ev.start)}"
    if with_day:
        when = f"{_day_name(ev.start.date(), today or date.today())} {when}"
    return f"{ev.title} {when}{where}"


def describe_day(day: date, today: date | None = None) -> str:
    today = today or date.today()
    events = day_events(day)
    label = _day_name(day, today)
    if not events:
        return f"Nothing on your calendar {label}."
    if len(events) == 1:
        return f"{label[:1].upper() + label[1:]} you have one thing: {_one(events[0])}."
    items = [_one(e) for e in events[:6]]
    more = f", and {len(events) - 6} more" if len(events) > 6 else ""
    return (f"{label[:1].upper() + label[1:]} you have {len(events)} things: "
            + ", ".join(items[:-1]) + f" and {items[-1]}{more}.")


def briefing_line(now: datetime | None = None) -> str:
    now = now or datetime.now()
    events = [e for e in day_events(now.date()) if e.all_day or e.end > now]
    if not events:
        return "No calendar events left today."
    return "Today: " + "; ".join(_one(e) for e in events[:5]) + "."


def heads_up(now: datetime, minutes: int = 10) -> Event | None:
    """A timed event starting within the next `minutes` (for the nudge loop)."""
    for ev in events_between(now, now + timedelta(minutes=minutes)):
        if not ev.all_day and now <= ev.start <= now + timedelta(minutes=minutes):
            return ev
    return None


# ── Voice ─────────────────────────────────────────────────────────────────────

_CAL = r"(?:calendar|schedule|agenda|timetable|diary|plans?)"
_ASK_DAY = re.compile(
    rf"\bwhat(?:'s| is| do i have| have i got)\b.{{0,25}}\b(?:on|in)\s+(?:my\s+)?{_CAL}\b|"
    rf"\bwhat(?:'s| is)\s+on\s+(?:for\s+)?(?:today|tomorrow|tonight|this (?:morning|afternoon|evening)|"
    rf"(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b|"
    rf"\bwhat do i have\s+(?:on\s+)?(?:today|tomorrow|this|on|next)\b|"
    rf"\bmy\s+{_CAL}\s+(?:for\s+)?(?:today|tomorrow|this week|on\s+\w+day)\b|"
    rf"\b(?:am i|are we) (?:free|busy)\b|\bany (?:events|meetings|lectures|classes) (?:today|tomorrow)\b",
    re.IGNORECASE)
_ASK_NEXT = re.compile(r"\bwhat(?:'s| is)\s+next\b(?!\s+(?:step|song|track|question|exercise))|"
                       r"\b(?:when(?:'s| is)|what time is)\s+my\s+next\s+(?P<what>[a-z]+(?:\s+[a-z]+)?)\b|"
                       r"\bnext\s+(?:event|meeting|appointment)\b", re.IGNORECASE)
_ADD = re.compile(rf"^\W*(?:arya\W+)?(?:please\s+)?(?:add|put|schedule|create|book)\s+(?:an?\s+(?:event|"
                  rf"appointment|meeting)\s+(?:called|for)\s+)?(?P<title>.+?)\s*(?:to|in|on|into)\s+my\s+"
                  rf"{_CAL}\b(?P<rest>.*)$|^\W*(?:arya\W+)?(?:please\s+)?(?:add|schedule|book)\s+(?P<title2>.+?)"
                  rf"\s+(?P<rest2>(?:on|at|every|tomorrow|today|next|this)\b.*)$", re.IGNORECASE)
_REMOVE = re.compile(rf"\b(?:remove|delete|cancel)\s+(?:the\s+)?(?P<title>.+?)\s+from\s+my\s+{_CAL}\b",
                     re.IGNORECASE)
_TIME = re.compile(r"\b(?:at|from)\s+(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?)"
                   r"(?:\s*(?:-|to|until|till)\s*(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?))?", re.IGNORECASE)
_EVERY = re.compile(r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE)
_AT_PLACE = re.compile(r"\b(?:in|at)\s+(room\s+\S+|[A-Z]\d+\S*|the\s+[a-z]+\s+(?:hall|room|building|lab))",
                       re.IGNORECASE)


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _day_in(message: str, today: date) -> date | None:
    """'today', 'tomorrow', 'on Friday' / 'Friday' (the coming one, today included), 'next Friday',
    '12 March', 'in 2 weeks'."""
    from arya.study.planner import parse_date

    t = message.lower()
    if re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", t):
        return today
    m = re.search(r"\b(?<!next )(" + "|".join(_DAYS) + r")\b", t)
    if m:
        return today + timedelta(days=(_DAYS.index(m.group(1)) - today.weekday()) % 7)
    return parse_date(t, today)


def _at(day: date, clock: str, now: datetime) -> datetime | None:
    from arya.tools.productivity import _parse_clock

    base = datetime.combine(day, datetime.min.time())
    t = _parse_clock(clock, base - timedelta(seconds=1))            # an hour on that day, not "the next one"
    if t is None:
        return None
    if day == now.date() and t < now and not re.search(r"am|pm", clock, re.IGNORECASE) and t.hour < 12:
        t += timedelta(hours=12)                                     # "today at 5" at 14:00 means 17:00
    return t.replace(year=day.year, month=day.month, day=day.day)


def _add(message: str, now: datetime) -> str:
    m = _ADD.search(message)
    title = (m.group("title") or m.group("title2") or "").strip(" ,.")
    rest = message
    title = re.sub(r"\s+(?:on|at|every|tomorrow|today|next|this)\b.*$", "", title, flags=re.IGNORECASE).strip(" ,.")
    title = re.sub(r"^(?:an?|the|my)\s+", "", title, flags=re.IGNORECASE)
    if not title:
        return "What should I call the event?"
    weekly = _EVERY.search(rest)
    day = _day_in(rest if not weekly else rest.replace(weekly.group(0), "on " + weekly.group(1)), now.date())
    tm = _TIME.search(rest)
    if day is None:
        return f"Which day is {title}?"
    if not tm:
        return f"What time is {title}?"
    start = _at(day, tm.group(1), now)
    end = _at(day, tm.group(2), now) if tm.group(2) else None
    if start is None:
        return f"I didn't catch the time for {title}. Say something like \"at 10\" or \"at 14:30\"."
    if end is not None and end <= start:
        end = end + timedelta(hours=12) if end + timedelta(hours=12) > start else None
    place = _AT_PLACE.search(rest[tm.end():] if tm else rest)
    ev = add_event(title[:1].upper() + title[1:], start, end, weekly=bool(weekly),
                   location=place.group(1) if place else "")
    when = (f"every {start.strftime('%A')} at {_clock(start)}" if weekly
            else f"{_day_name(start.date(), now.date())} at {_clock(start)}")
    return f"Added {ev.title} {when}{' in ' + ev.location if ev.location else ''}."


def handle(message: str, now: datetime | None = None) -> str | None:
    """ARYA's reply for calendar requests, else None."""
    now = now or datetime.now()
    if _REMOVE.search(message):
        title = re.sub(r"^(?:my|the|an?)\s+", "", _REMOVE.search(message).group("title"), flags=re.IGNORECASE)
        n = remove_events(title)
        return (f"Removed {title} from your calendar." if n == 1 else
                f"Removed {n} events called {title}." if n else
                f"I only manage events added here, and none is called {title}. "
                "Events from your other calendars have to be changed there.")
    if _ADD.search(message) and (_TIME.search(message) or re.search(_CAL, message, re.IGNORECASE)):
        return _add(message, now)
    m = _ASK_NEXT.search(message)
    if m:
        what = (m.group("what") or "").strip()
        what = "" if what.lower() in ("event", "meeting", "appointment", "thing") else what
        if what.lower() in ("exam",) or what.lower().startswith(("step", "question", "exercise")):
            return None                                                # the planner / tutor answer those
        stem = re.sub(r"(?:es|s)$", "", what.split()[0]) if what else ""
        ev = next_event(now, stem)
        if not ev:
            return f"I don't see {'a ' + what if what else 'anything'} coming up on your calendar."
        return f"Next: {_one(ev, with_day=ev.start.date(), today=now.date())}."
    if _ASK_DAY.search(message):
        day = _day_in(message, now.date()) or now.date()
        if re.search(r"\b(?:am i|are we) (?:free|busy)\b", message, re.IGNORECASE):
            tm = _TIME.search(message)
            if tm:
                at = _at(day, tm.group(1), now)
                if at:
                    clash = [e for e in day_events(day) if not e.all_day and e.start <= at < e.end]
                    return (f"No, you have {_one(clash[0])}." if clash
                            else f"Yes, nothing is on your calendar at {_clock(at)} {_day_name(day, now.date())}.")
        return describe_day(day, now.date())
    return None
