"""Phase B: study mode — one sentence opens the course, starts pomodoros and says the plan; "stop studying" sums up.
Explorer is never opened and the pomodoro timers are cancelled at the end of each test."""
import asyncio
import os
import time

import pytest

import arya.core.llm as llm
from arya.study import planner, practice
from arya.study import session as S
from arya.tools import productivity


@pytest.mark.parametrize("message, yes", [
    ("ARYA, start studying Dynamics", True),
    ("start a study session for control", True),
    ("study mode", True),
    ("let's study Dynamics for an hour", True),
    ("let's study now", True),
    ("time to revise control", True),
    ("I want to study the Coriolis effect", False),             # a question about a topic, not a session
    ("what should I study today?", False),                      # the planner's job
    ("let's practise", False),                                  # the quiz's job
    ("explain the damping ratio", False),
])
def test_trigger(kb, message, yes):
    assert S.wants_session(message) is yes


@pytest.mark.parametrize("message, minutes", [
    ("start studying", 60), ("let's study for an hour", 60), ("study for 90 minutes", 90),
    ("study for two hours", 120), ("study for half an hour", 30), ("for an hour and a half", 90),
    ("study for 3 pomodoros", 90), ("for two and a half hours", 150),
])
def test_length(message, minutes):
    assert S.minutes_in(message) == minutes


@pytest.fixture
def studying(kb, monkeypatch):
    opened = []
    monkeypatch.setattr(S, "open_folder", opened.append)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    with S._db() as c:
        c.execute("DELETE FROM study_sessions")
    with planner._db() as c:
        c.execute("DELETE FROM exams")
    yield opened
    with S._db() as c:
        c.execute("DELETE FROM study_sessions")


def _say(message):
    async def turn():
        reply = "".join([s async for s in llm.stream_with_tools(message)])
        await productivity.cancel_timer()                       # don't leave pomodoro tasks behind
        return reply
    return asyncio.run(turn())


def test_start_opens_folder_and_starts_pomodoros(studying):
    async def go():
        reply = await S.handle("ARYA, start studying Dynamics for an hour")
        names = [(i.kind, i.label) for i in productivity._SCHEDULED]
        await productivity.cancel_timer()
        return reply, names
    reply, names = asyncio.run(go())
    assert reply.startswith("Study mode for Dynamics: I opened your Dynamics folder and started 2 pomodoros")
    assert names == [("pomodoro", "Dynamics")]
    assert len(studying) == 1 and os.path.basename(studying[0]) == "Dynamics"
    row = S.current()
    assert row["course"] == "Dynamics" and row["planned_min"] == 60


def test_no_course_uses_the_next_exam_or_asks(studying):
    assert _say("start studying").startswith("Which course do you want to study")
    from datetime import date, timedelta
    planner.add_exam("Control", date.today() + timedelta(days=10))
    reply = _say("start studying")
    assert reply.startswith("Study mode for Control") and "Your Control exam is in 10 days" in reply


def test_status_and_end_summarise_the_session(studying):
    assert _say("stop studying").startswith("There's no study session running")
    _say("let's study Dynamics")
    with S._db() as c:                                           # pretend it started 47 minutes ago
        c.execute("UPDATE study_sessions SET started = ?", (time.time() - 47 * 60,))
    with practice._db() as c:
        c.execute("DELETE FROM reviews")
        for q in (5, 4, 1):
            c.execute("INSERT INTO reviews (card_id, ts, quality) VALUES (0, ?, ?)", (time.time() - 60, q))
    assert _say("how long have I been studying?") == "So far: 47 minutes of Dynamics, 3 cards answered, 2 right."
    reply = _say("I'm done studying")
    assert reply == "Study session over: 47 minutes of Dynamics, 3 cards answered, 2 right. Nice work."
    assert S.current() is None
    with practice._db() as c:
        c.execute("DELETE FROM reviews")


def test_a_question_about_studying_still_goes_to_the_model(studying, monkeypatch):
    assert asyncio.run(S.handle("I want to study the Coriolis effect, what is it?")) is None
