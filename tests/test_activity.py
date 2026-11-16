"""Your day at the laptop: "remind me when I'm back", time at the laptop, the evening wrap-up, the week and streak.
A scratch database and a fake idle clock — no real keyboard or mouse needed."""
import asyncio
import sqlite3
import time
from datetime import date, datetime, timedelta

import pytest

from arya.core import activity as A
from arya.tools import tasks

TODAY = date(2026, 11, 16)


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    db = tmp_path / "arya.db"
    monkeypatch.setattr(A, "DB_OVERRIDE", db)
    monkeypatch.setattr(tasks, "DB_OVERRIDE", db)
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE study_sessions (id INTEGER PRIMARY KEY, course TEXT, started REAL, planned_min INTEGER, ended REAL)")
        c.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, card_id INTEGER, ts REAL, quality INTEGER)")
    monkeypatch.setattr(A, "_loop_state", {"last_tick": 0.0, "pending_active": 0.0, "away": False, "wrapped": ""})
    monkeypatch.setattr(A, "_episodes", lambda day: [])
    return db


def _at(d: date, h: int, m: int = 0) -> float:
    return datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m).timestamp()


# ── "Remind me when I'm back" ────────────────────────────────────────────────

@pytest.mark.parametrize("text, what", [
    ("remind me to email Ben when I'm back at the laptop", "email Ben"),
    ("remind me when I'm back to call mom", "call mom"),
    ("when I get back, remind me to water the plants", "water the plants"),
    ("ARYA, remind me about the lab report when I come back", "the lab report"),
])
def test_back_reminder_phrases(text, what):
    assert A.handle(text)
    assert [r["text"] for r in A.back_reminders()] == [what]


def test_goes_through_the_reminder_handler_not_a_clock_time():
    from arya.tools import alarms
    reply = asyncio.run(alarms.handle("remind me to email Ben when I'm back at the laptop"))
    assert "when you're back" in reply and A.back_reminders()


def test_fires_only_after_being_away_and_coming_back():
    said = []

    async def say(t):
        said.append(t)
    A.add_back_reminder("email Ben")

    async def go():
        t = 1000.0
        await A.tick(say, idle=5, mode="default", now=t)            # you're here: nothing yet
        await A.tick(say, idle=A.AWAY_S + 1, mode="default", now=t + 10)   # away
        await A.tick(say, idle=A.AWAY_S + 20, mode="default", now=t + 20)
        await A.tick(say, idle=3, mode="default", now=t + 30)       # back
        await A.tick(say, idle=3, mode="default", now=t + 40)
    asyncio.run(go())
    assert said == ["Welcome back! You asked me to remind you: email Ben."]
    assert A.back_reminders() == []


def test_set_from_the_phone_fires_on_the_first_return():
    said = []

    async def say(t):
        said.append(t)
    A.handle("remind me to plug in the charger when I'm back", remote=True)

    async def go():
        await A.tick(say, idle=A.AWAY_S * 3, mode="default", now=5000.0)
        await A.tick(say, idle=1, mode="default", now=5010.0)
    asyncio.run(go())
    assert said and "plug in the charger" in said[0]


def test_laptop_sleep_counts_as_away():
    said = []

    async def say(t):
        said.append(t)
    A.add_back_reminder("take the pills out of the bag")

    async def go():
        await A.tick(say, idle=2, mode="default", now=100.0)
        await A.tick(say, idle=2, mode="default", now=100.0 + A.AWAY_S + 60)   # the clock jumped: it slept
    asyncio.run(go())
    assert said


# ── Time at the laptop ───────────────────────────────────────────────────────

def test_active_minutes_per_mode():
    async def go():
        t = 10_000.0
        for i in range(13):                                          # 2 minutes of activity in work mode
            await A.tick(lambda s: None, idle=5, mode="work", now=t + i * 10)
        await A.tick(lambda s: None, idle=900, mode="work", now=t + 140)   # idle: not counted
    asyncio.run(go())
    assert A.active_seconds(date.today(), date.today()) == {"work": 120}     # 12 ticks of 10 s; idle not counted


# ── Wrap-up, week, streak ────────────────────────────────────────────────────

def _study(db, day, minutes, course="Dynamics"):
    with sqlite3.connect(db) as c:
        s = _at(day, 10)
        c.execute("INSERT INTO study_sessions (course, started, planned_min, ended) VALUES (?, ?, ?, ?)",
                  (course, s, minutes, s + minutes * 60))


def test_wrap_up(scratch, monkeypatch):
    A.add_active(4 * 3600, "work", TODAY)
    A.add_active(3600, "default", TODAY)
    _study(scratch, TODAY, 50)
    _study(scratch, TODAY - timedelta(days=1), 30)
    tasks.add("email Ben", "work")
    row = tasks.open_tasks("work")[0]
    with tasks._db() as c:
        c.execute("UPDATE tasks SET done = ? WHERE id = ?", (_at(TODAY, 15), row["id"]))
    tasks.add("book the lab", "work")
    monkeypatch.setattr(A, "_episodes", lambda day: ["Battery modelling in Python."])
    text = A.wrap_up(TODAY, now=datetime.combine(TODAY, datetime.min.time()).replace(hour=21))
    assert "You were at the laptop for 5 hours today, mostly in work mode." in text
    assert "You studied Dynamics for 50 minutes." in text and "Done: email Ben." in text
    assert "We talked about Battery modelling in Python." in text and "Still open: book the lab." in text
    assert "2-day study streak" in text and text.endswith("Good night when you get there.")


def test_quiet_day():
    assert A.wrap_up(TODAY, now=datetime.combine(TODAY, datetime.min.time()).replace(hour=15)) == \
        "A quiet day on the laptop."


def test_streak_counts_study_or_cards(scratch):
    for d in (TODAY, TODAY - timedelta(days=1)):
        _study(scratch, d, 15)
    with sqlite3.connect(scratch) as c:                              # day 3 by practice cards only
        c.executemany("INSERT INTO reviews (card_id, ts, quality) VALUES (1, ?, 4)",
                      [(_at(TODAY - timedelta(days=2), 9, i),) for i in range(5)])
    assert A.streak(TODAY) == 3
    assert A.streak(TODAY + timedelta(days=1)) == 3                  # tomorrow before studying: still counts


def test_week_answer(scratch, monkeypatch):
    A.add_active(2 * 3600, "study", TODAY)
    _study(scratch, TODAY, 40)
    monkeypatch.setattr(A, "date", type("D", (), {"today": staticmethod(lambda: TODAY)}))
    reply = A.handle("how was my week?")
    assert reply.startswith("This week: 2 hours at the laptop, 40 minutes of study.")


def test_scheduled_wrap_up_once_after_the_time():
    said = []

    async def say(t):
        said.append(t)
    assert "21:00" in A.handle("do my wrap-up every evening at 9")

    async def go():
        base = _at(date.today(), 20, 50)
        await A.tick(say, idle=1, mode="default", now=base)                # before 21:00
        await A.tick(say, idle=1, mode="default", now=base + 15 * 60)      # 21:05
        await A.tick(say, idle=1, mode="default", now=base + 16 * 60)      # once a day
    asyncio.run(go())
    assert len(said) == 1 and said[0].startswith("Here's your day.")
    assert "no evening wrap-up" in A.handle("no evening wrap-up").lower()


@pytest.mark.parametrize("text", ["remind me at 5 to call Ben", "remind me tomorrow to send the hours",
                                  "how was the meeting", "what's my name", "I'll be back in 5 minutes"])
def test_not_activity(text):
    assert not A.matches(text)
