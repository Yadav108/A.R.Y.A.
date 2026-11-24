"""
Your own voice routines (roadmap C4).

  "When I say start my day, give me the briefing, today's calendar and play focus music."
  "Start my day."                      -> the three steps, one after another, as if you'd said each
  "What routines do I have?" / "Delete the start my day routine."

Steps are split at commas, "then" and "and <verb>"; a step without its own verb borrows the previous one
("give me the briefing, today's calendar" -> "give me today's calendar"). Each step is an ordinary request,
so everything ARYA can do can be a step — anything that changes files still asks first, as always.
Kept in arya.db on this laptop.

On a schedule:
  "Run start my day every weekday at 8."            "Every Sunday at 7 pm do my weekly review."
  "Run start my day on Monday and Thursday at 7:30."  "What runs automatically?"
  "Stop running start my day automatically."
A scheduled routine runs exactly as if you'd said its trigger. It isn't run in meeting mode / do not disturb:
it waits until that ends. If the laptop was asleep (or ARYA was off) at that time, it runs when it wakes —
if that's less than two hours late; otherwise it's skipped, and the evening wrap-up says so. A late run never
starts inside quiet hours (ARYA_QUIET_HOURS); a time you chose inside quiet hours still runs, like an alarm.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from datetime import date, datetime, timedelta

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
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS routines (trigger TEXT PRIMARY KEY, spoken TEXT, steps TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS routine_schedules (trigger TEXT PRIMARY KEY, days TEXT NOT NULL, at TEXT NOT NULL,
                                                      next_at REAL NOT NULL, held TEXT NOT NULL DEFAULT '',
                                                      created TEXT);
        CREATE TABLE IF NOT EXISTS routine_skips (day TEXT NOT NULL, trigger TEXT NOT NULL, at TEXT NOT NULL,
                                                  why TEXT NOT NULL);
    """)
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


def handle(message: str, now: datetime | None = None) -> str | None:
    """Define / list / delete / schedule routines; running one is match() + the pipeline."""
    scheduled = handle_schedule(message, now)
    if scheduled:
        return scheduled
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
            c.execute("DELETE FROM routine_schedules WHERE trigger = ?", (key,))
        return f"Deleted the \"{key}\" routine." if n else f"I don't have a routine called \"{key}\"."
    return None


# ── On a schedule ─────────────────────────────────────────────────────────────

LATE_LIMIT_S = 2 * 3600          # asleep / off at the time: run on wake only if less late than this
ON_TIME_S = 5 * 60               # later than this counts as a late run (no late runs inside quiet hours)
TICK_S = 30

_DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ALL, _WEEKDAYS, _WEEKEND = frozenset(range(7)), frozenset(range(5)), frozenset({5, 6})
_WHEN_WORDS = {"every", "each", "on", "at", "and", "the", "in", "daily", "day", "days", "weekday", "weekdays",
               "weekend", "weekends", "morning", "mornings", "evening", "evenings", "night", "nights", "afternoon",
               "afternoons", "am", "pm", "noon", "midnight", "o'clock", "oclock", "automatically",
               *_DAY_NAMES, *(d + "s" for d in _DAY_NAMES)}
_TOKEN = re.compile(r"\d{1,2}(?:[:.]\d{2})?|[a-z']+")
_TIME = re.compile(r"\b(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>am|pm)?\b|\b(?P<word>noon|midnight)\b")
_LEAD = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:arya|aria)\W+|^\W*(?:please\s+)?", re.IGNORECASE)
_RUN_VERB = re.compile(r"^(?:please\s+)?(?:run|do|trigger)\s+", re.IGNORECASE)
_UNSCHEDULE = re.compile(r"^\W*(?:(?:hey\s+)?(?:arya|aria)\W+)?(?:please\s+)?(?:(?:stop|quit|don'?t|do\s+not|no\s+longer)"
                         r"\s+(?:running|doing|run|do)\s+(?:the\s+|my\s+)?(?P<t>.+?)(?:\s+routine)?\s+(?:automatically|"
                         r"on\s+(?:a|its\s+own|its)\s+schedule|by\s+itself)|(?:unschedule|cancel\s+the\s+schedule\s+"
                         r"(?:for|of))\s+(?:the\s+|my\s+)?(?P<t2>.+?)(?:\s+routine)?|(?:cancel|remove|delete|stop|clear)"
                         r"\s+(?:the\s+|my\s+)?(?P<t3>.+?)(?:\s+routine)?(?:'s)?\s+schedule)\W*$", re.IGNORECASE)
_SCHEDULE_LIST = re.compile(r"\bwhat\s+(?:runs|happens|do\s+you\s+(?:run|do))\s+(?:automatically|on\s+(?:its|their)\s+own|"
                            r"on\s+a\s+schedule)\b|\b(?:scheduled|automatic)\s+routines?\b|\broutines?\s+(?:are\s+|do\s+you\s+)?"
                            r"(?:scheduled|run(?:ning)?\s+automatically|automatic)\b", re.IGNORECASE)


def _ampm(text: str) -> str:
    return re.sub(r"\b([ap])\.\s?m\b\.?", r"\1m", text.lower())


def parse_when(text: str) -> tuple[frozenset, str] | None:
    """'every weekday at 8' -> ({0..4}, '08:00'); 'every Sunday at 7 pm' -> ({6}, '19:00'). None unless the text
    is only a schedule (days and one clock time)."""
    t = _ampm(text).strip(" ,.!?")
    tokens = _TOKEN.findall(t)
    if not tokens or any(not (w[0].isdigit() or w in _WHEN_WORDS) for w in tokens):
        return None
    days: set[int] = set()
    every = bool(re.search(r"\b(?:every|each|daily)\b", t))
    for w in tokens:
        if w in ("day", "days", "daily") or (every and w.rstrip("s") in ("morning", "evening", "night", "afternoon")):
            days |= _ALL
        elif w in ("weekday", "weekdays"):
            days |= _WEEKDAYS
        elif w in ("weekend", "weekends"):
            days |= _WEEKEND
        elif w.rstrip("s") in _DAY_NAMES:
            days.add(_DAY_NAMES.index(w.rstrip("s")))
    times = list(_TIME.finditer(t))
    if not days or len(times) != 1:
        return None
    m = times[0]
    if m.group("word"):
        return frozenset(days), "12:00" if m.group("word") == "noon" else "00:00"
    h, mi, ampm = int(m.group("h")), int(m.group("m") or 0), m.group("ampm") or ""
    if not ampm and not m.group("m") and not re.search(r"\bat\s+" + m.group("h") + r"\b", t):
        return None                                           # a bare number with no "at": not a time
    if not ampm:                                              # no am/pm: the part of the day, else the likely one
        ampm = ("am" if re.search(r"\bmornings?\b", t) else
                "pm" if re.search(r"\b(?:evenings?|nights?|afternoons?)\b", t) else
                "pm" if 1 <= h <= 6 else "")                  # "at 3" is 15:00, "at 8" is 08:00
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return frozenset(days), f"{h:02d}:{mi:02d}"


def next_time(days: frozenset, at: str, after: datetime) -> datetime:
    """The first time on one of `days` at `at` strictly after `after`."""
    h, m = (int(x) for x in at.split(":"))
    for i in range(8):
        t = (after + timedelta(days=i)).replace(hour=h, minute=m, second=0, microsecond=0)
        if t.weekday() in days and t > after:
            return t
    raise ValueError("no days")


def spoken_days(days: frozenset) -> str:
    if days == _ALL:
        return "every day"
    if days == _WEEKDAYS:
        return "every weekday"
    if days == _WEEKEND:
        return "every weekend"
    names = [_DAY_NAMES[d].title() for d in sorted(days)]
    return "every " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def _core(text: str) -> str:
    """'do my weekly review routine' -> 'weekly review': what's left once verbs and 'my'/'the' are dropped."""
    t = _norm(text)
    t = re.sub(r"\s+(?:routine|automatically)$", "", t)
    while True:
        s = re.sub(r"^(?:run|do|trigger|the|my)\s+", "", t)
        if s == t:
            return t
        t = s


def _find(name: str) -> sqlite3.Row | None:
    key, core = _norm(name), _core(name)
    if not core:
        return None
    for r in _all():
        if r["trigger"] in (key, core) or _core(r["trigger"]) == core:
            return r
    return None


_BOUNDARY = re.compile(r"(?:every|each|on|daily|at)\b", re.IGNORECASE)


def _split_schedule(message: str) -> tuple[str, tuple[frozenset, str], bool] | None:
    """(the routine part, (days, time), schedule-first?) when the message is '<routine> <schedule>' or
    '<schedule>, <routine>'. Of the possible splits, the first whose routine part names one of your routines."""
    text = _LEAD.sub("", message.strip()).strip(" .!?")
    words = list(re.finditer(r"\S+", text))
    splits = []
    for w in words[1:]:                                       # "<routine> every weekday at 8"
        when = parse_when(text[w.start():]) if _BOUNDARY.match(w.group()) else None
        if when:
            splits.append((text[:w.start()].strip(" ,"), when, False))
    if words and _BOUNDARY.match(words[0].group()):
        for w in reversed(words[1:]):                         # "every Sunday at 7 pm, <routine>"
            when = parse_when(text[:w.start()])
            if when:
                splits.append((text[w.start():].strip(" ,"), when, True))
    splits = [s for s in splits if s[0]]
    return next((s for s in splits if _find(s[0]) is not None), splits[0] if splits else None)


def _schedules() -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT s.*, r.spoken FROM routine_schedules s JOIN routines r ON r.trigger = s.trigger "
                         "ORDER BY s.at").fetchall()


def schedule(trigger: str, days: frozenset, at: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    first = next_time(days, at, now)
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO routine_schedules (trigger, days, at, next_at, held, created) "
                  "VALUES (?, ?, ?, ?, '', ?)", (trigger, json.dumps(sorted(days)), at, first.timestamp(),
                                                 now.isoformat(timespec="minutes")))
    return first


def _in_quiet_hours(hour: int) -> bool:
    try:
        from arya.core import proactive
        return proactive.nudger._quiet(hour)
    except Exception:
        return False


def handle_schedule(message: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now()
    if _SCHEDULE_LIST.search(message):
        rows = _schedules()
        if not rows:
            return ("Nothing runs automatically yet. Say, for example, \"run start my day every weekday at 8\".")
        return "Automatically: " + "; ".join(f"{spoken_days(frozenset(json.loads(r['days'])))} at {r['at']}, "
                                             f"\"{r['spoken']}\"" for r in rows) + "."
    m = _UNSCHEDULE.match(message)
    if m:
        name = m.group("t") or m.group("t2") or m.group("t3") or ""
        r = _find(name)
        if r is None:
            return f"I don't have a routine called \"{_core(name)}\"."
        with _db() as c:
            n = c.execute("DELETE FROM routine_schedules WHERE trigger = ?", (r["trigger"],)).rowcount
        return (f"Okay, \"{r['spoken']}\" won't run automatically any more. You can still say it." if n else
                f"\"{r['spoken']}\" wasn't running automatically.")
    split = _split_schedule(message)
    if split is None:
        return None
    name, (days, at), first = split
    r = _find(name)
    if r is None:
        explicit = (re.match(r"(?:please\s+)?(?:run|trigger)\b", name, re.IGNORECASE)
                    or re.search(r"\b(?:routine|automatically)\b", name, re.IGNORECASE)
                    or (first and _RUN_VERB.match(name)))
        if not explicit:
            return None                          # "remind me to stretch every day at 5": not a routine
        return (f"I don't have a routine called \"{_core(name)}\". Make it first, like \"when I say "
                f"{_core(name)}, …\" and the steps, then tell me when to run it.")
    schedule(r["trigger"], days, at, now)
    note = ""
    if _in_quiet_hours(int(at[:2])):
        note = " That's inside your quiet hours, but you picked the time, so it'll still run."
    when = spoken_days(days)
    return f"Okay. {when[0].upper() + when[1:]} at {at} I'll run \"{r['spoken']}\".{note}"


def _skip(row, why: str, now: datetime) -> None:
    due = datetime.fromtimestamp(row["next_at"])
    print(f"Routines: skipped {row['trigger']!r} due {due:%a %H:%M} ({why})")
    with _db() as c:
        c.execute("INSERT INTO routine_skips (day, trigger, at, why) VALUES (?, ?, ?, ?)",
                  (now.date().isoformat(), row["trigger"], f"{due:%H:%M}", why))
        c.execute("UPDATE routine_schedules SET next_at = ?, held = '' WHERE trigger = ?",
                  (next_time(frozenset(json.loads(row["days"])), row["at"], now).timestamp(), row["trigger"]))


async def tick(fire, now: datetime, quiet: str = "", busy: bool = False, quiet_hours=_in_quiet_hours) -> list[tuple]:
    """Run what's due. `quiet` is why ARYA mustn't speak now ("you were in a meeting"), or "". Returns
    [(trigger, 'ran' | 'held' | 'skipped' | 'waiting')] for what was due."""
    out = []
    with _db() as c:
        rows = c.execute("SELECT * FROM routine_schedules WHERE next_at <= ? ORDER BY next_at",
                         (now.timestamp(),)).fetchall()
    for row in rows:
        late = now.timestamp() - row["next_at"]
        if late > LATE_LIMIT_S:
            _skip(row, row["held"] or "the laptop was asleep or I wasn't running", now)
            out.append((row["trigger"], "skipped"))
            continue
        if quiet:
            if row["held"] != quiet:
                print(f"Routines: holding {row['trigger']!r} ({quiet})")
                with _db() as c:
                    c.execute("UPDATE routine_schedules SET held = ? WHERE trigger = ?", (quiet, row["trigger"]))
            out.append((row["trigger"], "held"))
            continue
        if busy:
            out.append((row["trigger"], "waiting"))
            continue
        if late > ON_TIME_S and quiet_hours(now.hour) and not quiet_hours(int(row["at"][:2])):
            _skip(row, "it was quiet hours by then", now)
            out.append((row["trigger"], "skipped"))
            continue
        steps = match(row["trigger"])
        with _db() as c:
            c.execute("UPDATE routine_schedules SET next_at = ?, held = '' WHERE trigger = ?",
                      (next_time(frozenset(json.loads(row["days"])), row["at"], now).timestamp(), row["trigger"]))
        if steps is None:                                    # the routine was deleted
            continue
        r = _find(row["trigger"])
        print(f"Routines: running {row['trigger']!r} (scheduled {row['at']}, {int(late // 60)} min late)")
        await fire(r["spoken"] if r else row["trigger"], late)
        out.append((row["trigger"], "ran"))
        busy = True                                          # one at a time: the next one waits for this one
    return out


def _quiet_reason() -> str:
    try:
        from arya.core import quiet
        why = quiet.reason()
    except Exception:
        return ""
    if not why:
        return ""
    return "you were on a call" if why.startswith("call") else "you were in meeting mode"


async def run(fire, busy=lambda: False, quiet_reason=_quiet_reason) -> None:
    """Started by the server. `fire(trigger, late_seconds)` runs the routine as if you'd said its trigger."""
    while True:
        try:
            await tick(fire, datetime.now(), quiet=quiet_reason(), busy=busy())
        except Exception as exc:
            print(f"Routines: schedule check failed ({type(exc).__name__}: {exc})")
        await asyncio.sleep(TICK_S)


def wrap_up_line(today: date | None = None) -> str:
    """For the evening wrap-up: scheduled routines that didn't run today, and why."""
    today = today or date.today()
    with _db() as c:
        rows = c.execute("SELECT s.trigger, s.at, s.why, r.spoken FROM routine_skips s LEFT JOIN routines r "
                         "ON r.trigger = s.trigger WHERE s.day = ?", (today.isoformat(),)).fetchall()
    if not rows:
        return ""
    return "I skipped " + "; ".join(f"\"{r['spoken'] or r['trigger']}\" at {r['at']}, because {r['why']}"
                                    for r in rows) + "."
