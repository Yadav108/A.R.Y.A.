"""
Your day at the laptop (roadmap D): reminders for when you're back, the evening wrap-up, and streaks & stats.

  "remind me to email Ben when I'm back (at the laptop)" / "when I get back, remind me to call mom"
      -> said when you're next back after being away (5+ minutes without keyboard or mouse, or the laptop
         slept); works from the phone too, and survives a restart
  "wrap up my day" / "how was my day?"
      -> time at the laptop (by mode), study, practice cards, to-dos done, what we talked about, what's still
         open, and tomorrow's first things
  "do my wrap-up every evening at 9" / "no evening wrap-up"
  "how was my week?" / "my stats" / "what's my streak?"   -> this week, and a study streak
The app shows the week in a small panel; the morning briefing mentions the streak.

Time at the laptop is counted from keyboard/mouse activity, a minute at a time, per mode — on this laptop only.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from datetime import date, datetime, timedelta

TICK_S = 10
AWAY_S = 5 * 60                  # this long without keyboard or mouse = away
BACK_S = 30                      # input within this = you're here
ACTIVE_IDLE_S = 120              # a minute counts as active if there was input in the last two
STREAK_MIN_STUDY_S = 10 * 60
STREAK_MIN_CARDS = 5

DB_OVERRIDE = None


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH
    c = sqlite3.connect(DB_OVERRIDE or DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS activity (day TEXT NOT NULL, mode TEXT NOT NULL, seconds INTEGER NOT NULL,
                                             PRIMARY KEY (day, mode));
        CREATE TABLE IF NOT EXISTS back_reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL,
                                                   armed INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS activity_settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    return c


def _setting(key: str, default: str = "") -> str:
    with _db() as c:
        r = c.execute("SELECT value FROM activity_settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _set(key: str, value: str) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO activity_settings (key, value) VALUES (?, ?)", (key, value))


def add_active(seconds: int, mode: str, day: date | None = None) -> None:
    d = (day or date.today()).isoformat()
    with _db() as c:
        c.execute("INSERT INTO activity (day, mode, seconds) VALUES (?, ?, ?) "
                  "ON CONFLICT(day, mode) DO UPDATE SET seconds = seconds + excluded.seconds", (d, mode, seconds))


def active_seconds(start: date, end: date) -> dict[str, int]:
    """{mode: seconds} for start..end inclusive."""
    with _db() as c:
        rows = c.execute("SELECT mode, SUM(seconds) s FROM activity WHERE day BETWEEN ? AND ? GROUP BY mode",
                         (start.isoformat(), end.isoformat())).fetchall()
    return {r["mode"]: int(r["s"]) for r in rows}


# ── Reminders for when you're back ────────────────────────────────────────────

def add_back_reminder(text: str, armed: bool = False) -> None:
    with _db() as c:
        c.execute("INSERT INTO back_reminders (text, created, armed) VALUES (?, ?, ?)", (text, time.time(), int(armed)))


def back_reminders() -> list[sqlite3.Row]:
    with _db() as c:
        return c.execute("SELECT * FROM back_reminders ORDER BY id").fetchall()


def _arm_all() -> None:
    with _db() as c:
        c.execute("UPDATE back_reminders SET armed = 1")


def _take_armed() -> list[str]:
    with _db() as c:
        rows = c.execute("SELECT id, text FROM back_reminders WHERE armed = 1 ORDER BY id").fetchall()
        c.executemany("DELETE FROM back_reminders WHERE id = ?", [(r["id"],) for r in rows])
    return [r["text"] for r in rows]


# ── The day and the week ──────────────────────────────────────────────────────

def _hm(seconds: float) -> str:
    m = int(seconds // 60)
    h, m = divmod(m, 60)
    if h and m:
        return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{m} minute{'s' if m != 1 else ''}"


def _span(day: date) -> tuple[float, float]:
    start = datetime.combine(day, datetime.min.time()).timestamp()
    return start, start + 86400


def _study_seconds(start: date, end: date) -> dict[str, float]:
    """{course: seconds} of study sessions that started in start..end."""
    from arya.memory.database import DB_PATH
    a, _ = _span(start)
    _, b = _span(end)
    out: dict[str, float] = {}
    try:
        with sqlite3.connect(DB_OVERRIDE or DB_PATH) as c:
            for course, started, planned, ended in c.execute(
                    "SELECT course, started, planned_min, ended FROM study_sessions WHERE started BETWEEN ? AND ?", (a, b)):
                stop = ended or min(time.time(), started + (planned or 0) * 60)
                out[course] = out.get(course, 0.0) + max(0.0, stop - started)
    except sqlite3.OperationalError:
        pass
    return out


def _count(sql: str, args: tuple) -> int:
    from arya.memory.database import DB_PATH
    try:
        with sqlite3.connect(DB_OVERRIDE or DB_PATH) as c:
            return int(c.execute(sql, args).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0


def _cards(start: date, end: date) -> int:
    a, _ = _span(start)
    _, b = _span(end)
    return _count("SELECT COUNT(*) FROM reviews WHERE ts BETWEEN ? AND ?", (a, b))


def _done_tasks(day: date) -> list[str]:
    from arya.tools import tasks
    a, b = _span(day)
    try:
        with tasks._db() as c:
            return [r["text"] for r in c.execute("SELECT text FROM tasks WHERE done BETWEEN ? AND ? ORDER BY done", (a, b))]
    except sqlite3.OperationalError:
        return []


def _episodes(day: date) -> list[str]:
    a, b = _span(day)
    try:
        from arya.memory import learning
        with learning._db() as c:
            return [r["summary"] for r in c.execute("SELECT summary FROM episodes WHERE ended BETWEEN ? AND ? "
                                                     "ORDER BY ended", (a, b))]
    except Exception:
        return []


def streak(today: date | None = None) -> int:
    """Days in a row (ending today, or yesterday if today hasn't started yet) with a study session of 10+ minutes
    or 5+ practice cards."""
    today = today or date.today()

    def studied(d: date) -> bool:
        return sum(_study_seconds(d, d).values()) >= STREAK_MIN_STUDY_S or _cards(d, d) >= STREAK_MIN_CARDS
    d = today if studied(today) else today - timedelta(days=1)
    n = 0
    while studied(d) and n < 366:
        n += 1
        d -= timedelta(days=1)
    return n


def week_stats(today: date | None = None) -> dict:
    """For the app panel and "how was my week?": the last 7 days."""
    today = today or date.today()
    start = today - timedelta(days=6)
    active = active_seconds(start, today)
    from arya.tools import tasks
    a, _ = _span(start)
    _, b = _span(today)
    try:
        with tasks._db() as c:
            done = c.execute("SELECT COUNT(*) FROM tasks WHERE done BETWEEN ? AND ?", (a, b)).fetchone()[0]
    except sqlite3.OperationalError:
        done = 0
    return {"laptop_s": sum(active.values()), "by_mode": active, "study_s": sum(_study_seconds(start, today).values()),
            "cards": _cards(start, today), "todos_done": int(done), "streak": streak(today)}


_MODE_WORDS = {"default": "everyday", "study": "study", "work": "work", "brainstorm": "brainstorm"}


def wrap_up(today: date | None = None, now: datetime | None = None) -> str:
    today = today or date.today()
    now = now or datetime.now()
    parts = []
    active = active_seconds(today, today)
    total = sum(active.values())
    if total >= 60:
        top = max(active, key=active.get)
        parts.append(f"You were at the laptop for {_hm(total)} today"
                     + (f", mostly in {_MODE_WORDS.get(top, top)} mode" if len(active) > 1 else "") + ".")
    study = _study_seconds(today, today)
    if study:
        parts.append("You studied " + ", ".join(f"{course} for {_hm(s)}" for course, s in study.items()) + ".")
    cards = _cards(today, today)
    if cards:
        parts.append(f"You practised {cards} card{'s' if cards != 1 else ''}.")
    done = _done_tasks(today)
    if done:
        parts.append(f"Done: {'; '.join(done[:4])}{' and more' if len(done) > 4 else ''}.")
    talked = _episodes(today)
    if talked:
        parts.append("We talked about " + "; ".join(t.rstrip('.') for t in talked[-3:]) + ".")
    if not parts:
        parts.append("A quiet day on the laptop.")
    from arya.tools import tasks
    open_ = [r["text"] for w in ("work", "personal") for r in tasks.open_tasks(w)]
    if open_:
        parts.append(f"Still open: {'; '.join(open_[:3])}{' and more' if len(open_) > 3 else ''}.")
    try:
        from arya.tools import agenda
        tomorrow = [e for e in agenda.day_events(today + timedelta(days=1))]
        if tomorrow:
            first = tomorrow[0]
            parts.append(f"Tomorrow starts with {first.title} at {first.start:%H:%M}.")
    except Exception:
        pass
    s = streak(today)
    if s >= 2:
        parts.append(f"That's a {s}-day study streak.")
    if now.hour >= 20:
        parts.append("Good night when you get there.")
    return " ".join(parts)


def briefing_line(today: date | None = None) -> str:
    today = today or date.today()
    y = today - timedelta(days=1)
    bits = []
    active = sum(active_seconds(y, y).values())
    if active >= 600:
        bits.append(f"yesterday {_hm(active)} at the laptop")
    s = streak(today)
    if s >= 2:
        bits.append(f"a {s}-day study streak")
    return ("Stats: " + ", ".join(bits)) if bits else ""


# ── The loop: count active minutes, notice you're back, evening wrap-up ───────

_loop_state = {"last_tick": 0.0, "pending_active": 0.0, "away": False, "wrapped": ""}


async def run(say, busy=lambda: False, idle_seconds=None, mode_now=None) -> None:
    """Started by the server. `say(text)` speaks (and shows on the phone)."""
    if idle_seconds is None:
        from arya.core.proactive import _idle_seconds as idle_seconds
    if mode_now is None:
        from arya.core import modes
        mode_now = modes.current_name
    while True:
        try:
            await tick(say, idle_seconds(), mode_now(), time.time())
        except Exception as exc:
            print(f"Activity: {type(exc).__name__}: {exc}")
        await asyncio.sleep(TICK_S)


async def tick(say, idle: float, mode: str, now: float) -> None:
    st = _loop_state
    gap = now - st["last_tick"] if st["last_tick"] else 0.0
    st["last_tick"] = now
    if gap > AWAY_S:                                      # the laptop slept, or ARYA was off
        st["away"] = True
    if idle < ACTIVE_IDLE_S and 0 < gap <= 3 * TICK_S:
        st["pending_active"] += gap
        if st["pending_active"] >= 60:
            add_active(int(st["pending_active"]), mode)
            st["pending_active"] = 0.0
    if idle >= AWAY_S:
        if not st["away"]:
            _arm_all()
        st["away"] = True
    elif idle <= BACK_S and st["away"]:
        st["away"] = False
        _arm_all()
        due = _take_armed()
        if due:
            await say("Welcome back! You asked me to remind you: " + "; and ".join(t.rstrip(".") for t in due) + ".")
    at = _setting("wrap_up_at")
    today = datetime.fromtimestamp(now)
    if at and st["wrapped"] != today.date().isoformat() and idle <= BACK_S:
        h, m = (int(x) for x in at.split(":"))
        if (h, m) <= (today.hour, today.minute) and today.hour < h + 3:     # not at 2 a.m. for a 21:00 wrap-up
            st["wrapped"] = today.date().isoformat()
            await say("Here's your day. " + wrap_up(today.date(), today))


# ── Voice ─────────────────────────────────────────────────────────────────────

_BACK = r"when\s+i(?:'m|\s+am|\s+get|\s+come|'ll\s+be)\s+back(?:\s+(?:at|to|on)\s+(?:the\s+|my\s+)?(?:laptop|computer|pc|desk|home))?"
_BACK_REMIND = re.compile(rf"^\W*(?:arya\W+)?(?:please\s+)?(?:remind\s+me\s+(?:to\s+|about\s+|that\s+)?(?P<t>.+?)\s+{_BACK}"
                          rf"|remind\s+me\s+{_BACK}\s*(?:,\s*)?(?:to\s+|about\s+|that\s+)?(?P<t2>.+?)"
                          rf"|{_BACK}\s*,?\s*remind\s+me\s+(?:to\s+|about\s+|that\s+)?(?P<t3>.+?))\W*$", re.IGNORECASE)
_WRAP = re.compile(r"\bwrap\s+(?:up\s+)?my\s+day\b|\bwrap\s+up\s+(?:the\s+|my\s+)?day\b|\bevening\s+wrap-?\s*up\b|"
                   r"\bhow\s+was\s+my\s+day\b|\bwhat\s+did\s+i\s+(?:do|get\s+done)\s+today\b", re.IGNORECASE)
_WRAP_AT = re.compile(r"\b(?:do|give\s+me)\s+(?:my\s+|the\s+|an?\s+)?(?:evening\s+)?wrap-?\s*up\s+(?:every\s+(?:evening|night|day)\s+)?"
                      r"at\s+(?P<t>\d{1,2}(?:[:.]\d{2})?\s*(?:pm|p\.m\.|am|a\.m\.)?)\b", re.IGNORECASE)
_NO_WRAP = re.compile(r"\bno\s+(?:more\s+)?(?:evening\s+)?wrap-?\s*ups?\b|\bstop\s+the\s+(?:evening\s+)?wrap-?\s*up\b", re.IGNORECASE)
_WEEK = re.compile(r"\bhow\s+was\s+my\s+week\b|\bmy\s+(?:weekly\s+)?stats\b|\bweekly\s+stats\b|\bwhat(?:'s|\s+is)\s+my\s+"
                   r"(?:study\s+)?streak\b|\bhow\s+much\s+did\s+i\s+(?:study|work)\s+this\s+week\b", re.IGNORECASE)


def matches(message: str) -> bool:
    return any(p.search(message) for p in (_BACK_REMIND, _WRAP, _WRAP_AT, _NO_WRAP, _WEEK))


def _clock(spoken: str) -> str | None:
    m = re.match(r"(\d{1,2})(?:[:.](\d{2}))?\s*(pm|p\.m\.|am|a\.m\.)?", spoken.strip(), re.IGNORECASE)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower().replace(".", "")
    if ampm == "pm" and h < 12:
        h += 12
    elif not ampm and h < 12:
        h += 12 if h >= 5 else 0                      # "at 9" for an evening wrap-up means 21:00
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def handle(message: str, remote: bool = False) -> str | None:
    m = _BACK_REMIND.match(message)
    if m:
        text = (m.group("t") or m.group("t2") or m.group("t3") or "").strip(" ,.")
        if not text:
            return "What should I remind you about when you're back?"
        add_back_reminder(text, armed=remote)             # from the phone you're away already
        return f"Okay, when you're back at the laptop I'll remind you to {text}." if not re.match(
            r"(?:that|about)\b", text) else f"Okay, I'll remind you when you're back at the laptop."
    if _NO_WRAP.search(message):
        _set("wrap_up_at", "")
        return "Okay, no evening wrap-up. Say \"wrap up my day\" whenever you want one."
    m = _WRAP_AT.search(message)
    if m:
        at = _clock(m.group("t"))
        if at is None:
            return "At what time? For example \"at 9 pm\"."
        _set("wrap_up_at", at)
        return f"Done: every evening at {at} I'll wrap up your day, when you're at the laptop."
    if _WRAP.search(message):
        return wrap_up()
    if _WEEK.search(message):
        w = week_stats()
        bits = []
        if w["laptop_s"] >= 60:
            bits.append(f"{_hm(w['laptop_s'])} at the laptop")
        if w["study_s"] >= 60:
            bits.append(f"{_hm(w['study_s'])} of study")
        if w["cards"]:
            bits.append(f"{w['cards']} cards practised")
        if w["todos_done"]:
            bits.append(f"{w['todos_done']} to-dos done")
        week = ("This week: " + ", ".join(bits) + ".") if bits else "Not much recorded this week yet."
        return week + (f" You're on a {w['streak']}-day study streak." if w["streak"] >= 2 else
                       " Study ten minutes today to start a streak.")
    return None
