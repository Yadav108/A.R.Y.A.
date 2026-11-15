"""
Proactive nudges (Phase B): ARYA speaks up on its own — rarely, and only when it helps.

  - Battery: unplugged and at 20 %, again at 10 %.
  - Breaks: after 90 minutes of continuous keyboard/mouse use (not during a study session,
    whose pomodoros already have breaks).
  - Practice: once a day, if cards are due and you haven't practised yet.

ARYA stays quiet when it would be rude: during quiet hours (22:00-08:00, ARYA_QUIET_HOURS="22-8"),
when you're away from the laptop, while ARYA is already talking, and whenever Windows says not to
notify (full-screen app, presentation, Do Not Disturb). "Do not disturb for an hour" / "no nudges"
mutes them; "nudges back on" turns them on again. ARYA_NUDGES=0 switches the whole thing off.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime

CHECK_EVERY_S = 60
BATTERY_LEVELS = (20, 10)              # warn once at each, while unplugged
BREAK_AFTER_S = 90 * 60                # continuous activity before suggesting a break
BREAK_RESET_IDLE_S = 5 * 60            # this much idle time counts as a break
AWAY_IDLE_S = 10 * 60                  # nobody there to hear it
CARDS_AFTER_HOUR = 10                  # the daily practice nudge comes after 10:00
MIN_GAP_S = 10 * 60                    # never two nudges within ten minutes (battery excepted)


@dataclass
class Sensors:
    """What the checks look at — read from the laptop by read_sensors(), faked in tests."""
    now: datetime
    battery: tuple[int, bool] | None = None      # (percent, plugged in)
    idle_s: float = 0.0                          # seconds since the last key press / mouse move
    windows_ok: bool = True                      # Windows accepts notifications right now
    studying: bool = False
    due_cards: int = 0
    practised_today: bool = False


@dataclass
class Nudger:
    quiet_hours: tuple[int, int] = (22, 8)
    warned_levels: set = field(default_factory=set)
    active_since: float | None = None            # start of the current stretch of activity
    break_nudged: bool = False
    cards_day: str = ""                          # date of the last practice nudge
    muted_until: float = 0.0
    last_spoken: float = 0.0
    last_check: float = 0.0

    def _quiet(self, hour: int) -> bool:
        start, end = self.quiet_hours
        return start <= hour or hour < end if start > end else start <= hour < end

    def check(self, s: Sensors) -> str | None:
        """At most one thing to say now, or None. Updates the bookkeeping either way."""
        t = s.now.timestamp()
        # Bookkeeping first, so a muted or quiet stretch still counts towards (and resets) things.
        if s.battery and s.battery[1]:
            self.warned_levels.clear()                                   # plugged in: re-arm
        asleep = self.last_check and t - self.last_check > BREAK_RESET_IDLE_S     # laptop slept / ARYA was off
        self.last_check = t
        if s.idle_s >= BREAK_RESET_IDLE_S or asleep:
            self.active_since, self.break_nudged = None, False           # had a break
        elif self.active_since is None:
            self.active_since = t - s.idle_s

        if t < self.muted_until or not s.windows_ok or s.idle_s >= AWAY_IDLE_S or self._quiet(s.now.hour):
            return None

        if s.battery and not s.battery[1]:                               # battery beats the gap rule
            due = [lvl for lvl in BATTERY_LEVELS if s.battery[0] <= lvl and lvl not in self.warned_levels]
            if due:
                self.warned_levels.update(lvl for lvl in BATTERY_LEVELS if s.battery[0] <= lvl)
                self.last_spoken = t
                if s.battery[0] <= min(BATTERY_LEVELS):
                    return f"Battery is down to {s.battery[0]} percent. Please plug in soon, or I'll go quiet."
                return f"Battery is at {s.battery[0]} percent. Time to plug in the charger."

        if t - self.last_spoken < MIN_GAP_S:
            return None

        if (not s.studying and not self.break_nudged and self.active_since is not None
                and t - self.active_since >= BREAK_AFTER_S):
            self.break_nudged = True
            self.last_spoken = t
            mins = int((t - self.active_since) // 60)
            return (f"You've been at the laptop for {_spoken(mins)} without a break. "
                    "Stand up, drink some water and rest your eyes for five minutes.")

        today = s.now.date().isoformat()
        if (s.due_cards and not s.practised_today and not s.studying and self.cards_day != today
                and s.now.hour >= CARDS_AFTER_HOUR):
            self.cards_day = today
            self.last_spoken = t
            n = s.due_cards
            return (f"You have {n} practice card{'s' if n != 1 else ''} due today. "
                    "Say \"quiz me\" when you have ten minutes.")
        return None


def _spoken(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} minutes"
    if 25 <= m <= 35:
        return "an hour and a half" if h == 1 else f"{h} and a half hours"
    hours = "an hour" if h == 1 else f"{h} hours"
    return hours + (f" and {m} minutes" if m else "")


# ── Reading the laptop ───────────────────────────────────────────────────────

def _idle_seconds() -> float:
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        pass
    return 0.0


def _windows_accepts_notifications() -> bool:
    """False in full-screen apps, presentations, Do Not Disturb / quiet time (SHQueryUserNotificationState)."""
    try:
        import ctypes

        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value == 5                                    # QUNS_ACCEPTS_NOTIFICATIONS
    except Exception:
        pass
    return True


def _practice_today() -> tuple[int, bool]:
    from arya.study import practice

    start = datetime.combine(date.today(), datetime.min.time()).timestamp()
    with practice._db() as c:
        done = c.execute("SELECT COUNT(*) FROM reviews WHERE ts >= ?", (start,)).fetchone()[0]
    return practice.due_count(), bool(done)


def read_sensors() -> Sensors:
    import psutil

    battery = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = (round(b.percent), bool(b.power_plugged))
    except Exception:
        pass
    try:
        from arya.study import session
        studying = session.current() is not None
    except Exception:
        studying = False
    try:
        due, practised = _practice_today()
    except Exception:
        due, practised = 0, True
    return Sensors(now=datetime.now(), battery=battery, idle_s=_idle_seconds(),
                   windows_ok=_windows_accepts_notifications(), studying=studying,
                   due_cards=due, practised_today=practised)


# ── State that survives a restart (mute, today's practice nudge) ─────────────

def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS proactive_state (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _load(n: Nudger) -> None:
    with _db() as c:
        rows = dict(c.execute("SELECT key, value FROM proactive_state").fetchall())
    n.muted_until = float(rows.get("muted_until", 0) or 0)
    n.cards_day = rows.get("cards_day", "")


def _save(n: Nudger) -> None:
    with _db() as c:
        c.executemany("INSERT OR REPLACE INTO proactive_state (key, value) VALUES (?, ?)",
                      [("muted_until", str(n.muted_until)), ("cards_day", n.cards_day)])


def _quiet_hours() -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", os.getenv("ARYA_QUIET_HOURS", "22-8"))
    return (int(m.group(1)) % 24, int(m.group(2)) % 24) if m else (22, 8)


nudger = Nudger(quiet_hours=_quiet_hours())


# ── Voice: mute / unmute ─────────────────────────────────────────────────────

_THINGS = r"(?:nudges?|check-?ins?|proactive (?:reminders?|messages?)|break reminders?|battery warnings?)"
_MUTE = re.compile(rf"\b(?:no more|stop|turn off|disable|pause|mute|no)\s+(?:the\s+|your\s+)?{_THINGS}|"
                   r"\b(?:do not|don'?t) disturb(?: me)?\b|\bdnd\b", re.IGNORECASE)
_UNMUTE = re.compile(rf"\b{_THINGS}\s+(?:back\s+)?on\b|\b(?:turn|switch)\s+(?:the\s+|your\s+)?{_THINGS}\s+"
                     r"(?:back\s+)?on\b|\b(?:enable|resume|unmute)\s+(?:the\s+|your\s+)?" + _THINGS +
                     r"|\byou can (?:disturb me|talk) again\b", re.IGNORECASE)
_FOR = re.compile(r"\bfor\s+(?:(an?|one|two|three|four|five|six|\d+)\s+)?(hours?|minutes?|mins?)\b|"
                  r"\b(today|for the rest of the day|until tomorrow)\b", re.IGNORECASE)
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _mute_seconds(message: str, now: datetime) -> float | None:
    """'for two hours' -> 7200; 'today' -> until midnight; nothing -> None (until turned back on)."""
    m = _FOR.search(message)
    if not m:
        return None
    if m.group(3):
        end = datetime.combine(now.date(), datetime.max.time())
        return (end - now).total_seconds()
    raw = (m.group(1) or "one").lower()
    n = _NUM.get(raw) or int(raw)
    return n * (3600 if m.group(2).lower().startswith("h") else 60)


def handle(message: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now()
    if _UNMUTE.search(message):
        nudger.muted_until = 0.0
        _save(nudger)
        return "Okay, I'll speak up again for battery, breaks and practice."
    if _MUTE.search(message):
        secs = _mute_seconds(message, now)
        if secs is None:
            nudger.muted_until = float("inf")
            reply = "Okay, no more nudges until you say \"nudges back on\". Timers and reminders still ring."
        else:
            nudger.muted_until = now.timestamp() + secs
            until = datetime.fromtimestamp(nudger.muted_until).strftime("%H:%M")
            reply = f"Okay, I won't disturb you until {until}. Timers and reminders still ring."
        _save(nudger)
        return reply
    return None


# ── The loop ─────────────────────────────────────────────────────────────────

async def run(say, busy) -> None:
    """Started by the server. `say(text)` speaks; `busy()` is True while ARYA is talking or answering."""
    if os.getenv("ARYA_NUDGES", "1") == "0":
        print("Nudges: off (ARYA_NUDGES=0)")
        return
    try:
        await asyncio.to_thread(_load, nudger)
    except Exception as exc:
        print(f"Nudges: couldn't load state ({exc})")
    print("Nudges: on (battery, breaks, practice) — say \"no nudges\" to mute")
    while True:
        await asyncio.sleep(CHECK_EVERY_S)
        try:
            sensors = await asyncio.to_thread(read_sensors)
            if busy():
                continue
            text = nudger.check(sensors)
            if text:
                print(f"Nudge: {text}")
                await asyncio.to_thread(_save, nudger)
                await say(text)
        except Exception as exc:
            print(f"Nudge check failed: {type(exc).__name__}: {exc}")
