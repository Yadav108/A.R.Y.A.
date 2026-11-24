"""
Habits: things you want to do every day, or a few times a week, and your streaks.

  "Track a habit: drink 2 litres of water every day."   "Track a new habit: gym 3 times a week."
  "I went to the gym." / "Done with water." / "Water done." / "I meditated yesterday."   -> logged for that day
  "How are my habits?"                     -> streaks and this week
  "What's my gym streak?"                  -> just that one
  "Stop tracking gym." / "Delete the water habit."
A daily habit's streak is days in a row; an "N times a week" habit's streak is weeks in a row (Monday to Sunday)
with N or more. The morning briefing and the evening wrap-up mention them when there's something to say, and
in the evening ARYA nudges you once about a daily habit not done yet (on the phone if you're away).
Kept in arya.db on this laptop.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

MAX_HABITS = 12

_NUMBERS = {"one": 1, "once": 1, "two": 2, "twice": 2, "three": 3, "thrice": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7}
_LEAD = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:(?:arya|aria)\W+)?(?:please\s+)?", re.IGNORECASE)
_TRACK = re.compile(r"^(?:(?:start\s+|begin\s+)?track(?:ing)?\s+(?:a\s+(?:new\s+)?|my\s+|the\s+|this\s+)?habit|"
                    r"(?:add|create|make|start)\s+(?:a\s+)?(?:new\s+)?habit|new\s+habit|"
                    r"i\s+want\s+to\s+(?:build|start|form)\s+(?:a\s+|the\s+)?habit)\s*(?:[:,\-]|called|of|to)?\s*"
                    r"(?P<what>.+?)\W*$", re.IGNORECASE)
_TRACK_BARE = re.compile(r"^(?:start\s+|begin\s+)?track(?:ing)?\s+(?:my\s+)?(?P<what>.+?)\W*$", re.IGNORECASE)   # + a target
_TARGET = re.compile(r"\s*,?\s*(?:(?P<daily>every\s*day|each\s+day|daily|a\s+day|per\s+day)|"
                     r"(?P<n>\d|one|once|two|twice|three|thrice|four|five|six|seven)(?:\s+times?)?\s+(?:a|per|each|"
                     r"every)\s+week|(?P<weekdays>(?:every|on)\s+weekdays?))\b", re.IGNORECASE)
_STOP = re.compile(r"^(?:(?:stop|quit|no\s+longer|don'?t)\s+track(?:ing)?\s+(?:my\s+|the\s+)?(?P<t>.+?)(?:\s+habit)?|"
                   r"(?:delete|remove|drop|forget)\s+(?:the\s+|my\s+)?(?P<t2>.+?)\s+habit)\W*$", re.IGNORECASE)
_STATUS = re.compile(r"\b(?:how\s+(?:are|'re)|show(?:\s+me)?|list|check|what\s+are)\s+my\s+habits\b|"
                     r"\bhow\s+am\s+i\s+doing\s+(?:with|on)\s+(?:my\s+)?habits\b|\bhabits?\s+(?:streaks?|tracker|report|"
                     r"update|check)\b|\bwhat\s+habits\s+(?:am\s+i|do\s+i)\b", re.IGNORECASE)
_ONE = re.compile(r"\b(?:what(?:'s|\s+is)|how(?:'s|\s+is))\s+my\s+(?P<t>.+?)\s+(?:streak|habit)\b", re.IGNORECASE)
_LOG = re.compile(r"^(?:(?:i\s+(?:just\s+|already\s+)?(?:have\s+)?|i'?ve\s+(?:just\s+|already\s+)?)(?:went|gone|been|did|done|finished|completed|"
                  r"drank|drunk|had|made|took|taken|ran|run|read|wrote|written)\b|"
                  r"(?:all\s+)?done\s+(?:with\s+)?|finished\s+(?:with\s+)?|(?:log|mark|tick\s+off|check\s+off)\s+)"
                  r"(?P<rest>.*)$|^(?P<what>.+?)\s+(?:is\s+)?(?:done|finished|complete|completed|logged)"
                  r"(?:\s+for\s+today|\s+today|\s+for\s+yesterday|\s+yesterday)?$", re.IGNORECASE)
_LOG_VERB = re.compile(r"^(?:i\s+(?:just\s+|already\s+)?(?:have\s+)?|i'?ve\s+(?:just\s+|already\s+)?)(?P<verb>[a-z]+ed)\b",
                       re.IGNORECASE)   # "I meditated"
_NOT_A_LOG = re.compile(r"\b(?:but|because|should|could|would|how|what|why|when|where|can|will|tomorrow|later|"
                        r"and|going\s+to|want|need|forgot|didn'?t|haven'?t|not|never)\b", re.IGNORECASE)
_STOPWORDS = {"i", "a", "an", "the", "to", "of", "my", "me", "with", "for", "at", "in", "on", "and", "it", "some",
              "every", "each", "day", "daily", "week", "times", "time", "per", "today", "yesterday", "done", "did",
              "do", "went", "go", "gone", "have", "had", "has", "just", "already", "finished", "completed",
              "complete", "log", "mark", "tick", "check", "off", "as", "is", "was", "be", "drink", "drank", "get",
              "got", "make", "made", "take", "took", "litres", "liters", "litre", "liter", "glasses", "glass",
              "minutes", "minute", "mins", "hours", "hour", "km", "least", "habit", "all", "ve", "now", "this",
              "morning", "evening", "night", "once", "twice"}


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS habits (name TEXT PRIMARY KEY, spoken TEXT NOT NULL, per_week INTEGER NOT NULL,
                                           created TEXT);
        CREATE TABLE IF NOT EXISTS habit_log (name TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY (name, day));
    """)
    return conn


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "es", "s", "e"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[:-len(suffix)]
    return w


def keywords(text: str) -> set[str]:
    """The words that identify a habit: 'drink 2 litres of water' -> {'water'}; 'meditated' -> {'meditat'}."""
    words = re.findall(r"[a-z]+", text.lower().replace("'", ""))
    return {_stem(w) for w in words if w not in _STOPWORDS and len(w) > 1}


def _key(spoken: str) -> str:
    return " ".join(sorted(keywords(spoken))) or spoken.lower().strip()


def parse(what: str) -> tuple[str, int] | None:
    """'drink 2 litres of water every day' -> ('drink 2 litres of water', 7); 'gym 3 times a week' -> ('gym', 3)."""
    m = _TARGET.search(what)
    per_week = 7
    if m:
        if m.group("n"):
            n = m.group("n").lower()
            per_week = int(n) if n.isdigit() else _NUMBERS[n]
        elif m.group("weekdays"):
            per_week = 5
        what = (what[:m.start()] + what[m.end():])
    name = re.sub(r"^(?:to|of)\s+", "", what.strip(" ,.:-"), flags=re.IGNORECASE)
    name = re.sub(r"^(?:go(?:ing)?\s+to\s+(?:the\s+)?)(?=gym\b)", "", name, flags=re.IGNORECASE)
    if not name or not keywords(name) or not 1 <= per_week <= 7:
        return None
    return name, per_week


def add(spoken: str, per_week: int, today: date | None = None) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO habits (name, spoken, per_week, created) VALUES (?, ?, ?, ?)",
                  (_key(spoken), spoken, per_week, (today or date.today()).isoformat()))


def habits() -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM habits ORDER BY created, spoken").fetchall()


def find(text: str) -> sqlite3.Row | None:
    """The habit these words are about: the one sharing the most keywords (ties: none, unless one is exact)."""
    said = keywords(text)
    if not said:
        return None
    scored = sorted(((len(said & set(r["name"].split())), r) for r in habits()), key=lambda x: -x[0])
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        exact = [r for n, r in scored if n and set(r["name"].split()) == said]
        return exact[0] if len(exact) == 1 else None
    return scored[0][1]


def log(name: str, day: date | None = None) -> bool:
    """True if newly logged (False: already logged that day)."""
    with _db() as c:
        return c.execute("INSERT OR IGNORE INTO habit_log (name, day) VALUES (?, ?)",
                         (name, (day or date.today()).isoformat())).rowcount > 0


def _days(name: str) -> set[date]:
    with _db() as c:
        return {date.fromisoformat(r["day"]) for r in c.execute("SELECT day FROM habit_log WHERE name = ?", (name,))}


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def this_week(name: str, today: date | None = None, days: set[date] | None = None) -> int:
    today = today or date.today()
    days = _days(name) if days is None else days
    return sum(1 for d in days if _monday(today) <= d <= today)


def streak(per_week: int, days: set[date], today: date) -> int:
    """Daily: days in a row, ending today (or yesterday, if today isn't done yet). N a week: weeks in a row with N
    or more, ending this week (or last week, if this week's N isn't reached yet)."""
    n = 0
    if per_week >= 7:
        d = today if today in days else today - timedelta(days=1)
        while d in days and n < 3660:
            n += 1
            d -= timedelta(days=1)
        return n
    week = _monday(today)

    def met(monday: date) -> bool:
        return sum(1 for d in days if monday <= d <= monday + timedelta(days=6)) >= per_week
    if not met(week):
        week -= timedelta(days=7)
    while met(week) and n < 520:
        n += 1
        week -= timedelta(days=7)
    return n


def _target(per_week: int) -> str:
    return "every day" if per_week >= 7 else ("once a week" if per_week == 1 else
                                               "twice a week" if per_week == 2 else f"{per_week} times a week")


def _status(r, today: date) -> str:
    days = _days(r["name"])
    s = streak(r["per_week"], days, today)
    if r["per_week"] >= 7:
        done = "done today" if today in days else "not yet today"
        return f"{r['spoken']}: {done}" + (f", {s} days in a row" if s >= 2 else "")
    n = this_week(r["name"], today, days)
    return (f"{r['spoken']}: {n} of {r['per_week']} this week"
            + (f", {s} weeks in a row" if s >= 2 else ""))


def summary(today: date | None = None) -> str:
    today = today or date.today()
    rows = habits()
    if not rows:
        return "You're not tracking any habits. Say, for example, \"track a habit: gym 3 times a week\"."
    return "; ".join(_status(r, today)[:1].upper() + _status(r, today)[1:] for r in rows) + "."


def open_today(today: date | None = None) -> list[str]:
    """Daily habits not logged yet today."""
    today = today or date.today()
    return [r["spoken"] for r in habits() if r["per_week"] >= 7 and today not in _days(r["name"])]


def briefing_line(today: date | None = None) -> str:
    """Streaks worth keeping, and weekly habits still to do this week (only when there's something to say)."""
    today = today or date.today()
    bits = []
    for r in habits():
        days = _days(r["name"])
        s = streak(r["per_week"], days, today)
        if r["per_week"] >= 7:
            if s >= 2:
                bits.append(f"{r['spoken']}, {s} days in a row")
        else:
            left = r["per_week"] - this_week(r["name"], today, days)
            if left > 0:
                bits.append(f"{r['spoken']}, {left} more this week" + (f" ({s}-week streak)" if s >= 2 else ""))
    return ("Habits: " + "; ".join(bits)) if bits else ""


def wrap_up_line(today: date | None = None) -> str:
    today = today or date.today()
    done, todo, weekly = [], [], []
    for r in habits():
        days = _days(r["name"])
        if r["per_week"] >= 7:
            (done if today in days else todo).append(r["spoken"])
        elif today in days:
            weekly.append(f"{r['spoken']} ({this_week(r['name'], today, days)} of {r['per_week']} this week)")
    parts = []
    if done or weekly:
        parts.append("Habits done today: " + ", ".join(done + weekly) + ".")
    if todo:
        parts.append("Still to do today: " + ", ".join(todo) + ".")
    return " ".join(parts)


def _short(spoken: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z']+", spoken) if w.lower() not in _STOPWORDS]
    return words[-1].lower() if words else spoken


# ── Voice ─────────────────────────────────────────────────────────────────────

def _logged(text: str) -> sqlite3.Row | None:
    """The habit a short "I went to the gym" / "done with water" / "I meditated" is about, else None."""
    if len(text.split()) > 9 or _NOT_A_LOG.search(text):
        return None
    if _LOG.match(text):
        return find(text)
    m = _LOG_VERB.match(text)                            # "I meditated": the verb is the habit
    if m:
        r = find(m.group("verb"))
        return r if r is not None and _stem(m.group("verb").lower()) in r["name"].split() else None
    return None


def _tracked_bare(text: str) -> re.Match | None:
    """"start tracking meditation every day": no word "habit", so only with a target."""
    m = _TRACK_BARE.match(text)
    return m if m and _TARGET.search(m.group("what")) else None


def addressed(message: str) -> bool:
    """Surely said to ARYA (for "is this for me?"): setting up or asking about habits, not "I went to the gym"."""
    text = _LEAD.sub("", message.strip())
    return bool(_TRACK.match(text) or _STATUS.search(text))


def matches(message: str) -> bool:
    text = _LEAD.sub("", message.strip())
    return bool(_TRACK.match(text) or _tracked_bare(text) or _STOP.match(text.strip(" .!")) or _STATUS.search(text) or _ONE.search(text)
                or _LOG.match(text.strip(" .!")) or _LOG_VERB.match(text))


def handle(message: str, today: date | None = None) -> str | None:
    today = today or date.today()
    text = _LEAD.sub("", message.strip()).strip()
    m = _TRACK.match(text) or _tracked_bare(text)
    if m:
        parsed = parse(m.group("what"))
        if parsed is None:
            return "What's the habit? For example \"drink 2 litres of water every day\" or \"gym 3 times a week\"."
        name, per_week = parsed
        if len(habits()) >= MAX_HABITS and find(name) is None:
            return f"You're already tracking {MAX_HABITS} habits. Stop one first."
        add(name, per_week, today)
        return (f"Okay, I'm tracking \"{name}\", {_target(per_week)}. Tell me when you've done it, like "
                f"\"done with {_short(name)}\".")
    m = _STOP.match(text.strip(" .!"))
    if m:
        name = m.group("t") or m.group("t2") or ""
        r = find(name)
        if r is None:
            if m.group("t2") or re.search(r"\bhabit\b", text, re.IGNORECASE):
                return f"I'm not tracking a habit called \"{name}\"."
            return None                                  # "stop tracking my location": not a habit
        with _db() as c:
            c.execute("DELETE FROM habits WHERE name = ?", (r["name"],))
            c.execute("DELETE FROM habit_log WHERE name = ?", (r["name"],))
        return f"Okay, I've stopped tracking \"{r['spoken']}\"."
    if _STATUS.search(text):
        return summary(today)
    m = _ONE.search(text)
    if m:
        r = find(m.group("t"))
        if r is None:
            return None                                  # "what's my study streak?" is activity.py's
        return _status(r, today)[:1].upper() + _status(r, today)[1:] + "."
    clean = text.strip(" .!")
    r = _logged(clean) if "?" not in text else None
    if r is not None:
        day = today - timedelta(days=1) if re.search(r"\byesterday\b", clean, re.IGNORECASE) else today
        fresh = log(r["name"], day)
        when = "yesterday" if day != today else "today"
        if not fresh:
            return f"Already logged \"{r['spoken']}\" for {when}."
        s = streak(r["per_week"], _days(r["name"]), today)
        if r["per_week"] >= 7:
            extra = f" That's {s} days in a row." if s >= 2 else ""
        else:
            n = this_week(r["name"], today)
            extra = f" That's {n} of {r['per_week']} this week." + (" Target reached!" if n == r["per_week"] else "")
        return f"Nice, logged \"{r['spoken']}\" for {when}.{extra}"
    return None
