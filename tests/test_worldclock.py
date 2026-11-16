"""World clock: Berlin here, India as the second zone. A fixed 'now' (Berlin, winter, UTC+1)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from arya.tools import worldclock as W

EVENING = datetime(2026, 11, 16, 20, 45, tzinfo=ZoneInfo("Europe/Berlin"))      # = 01:15 in India
MORNING = datetime(2026, 11, 17, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))        # = 13:30 in India


@pytest.mark.parametrize("q", ["what time is it?", "What's the time", "Hey ARYA, what time is it now?", "time please"])
def test_time_here(q):
    assert W.handle(q, now=EVENING) == "It's 8:45 in the evening."


@pytest.mark.parametrize("q, a", [
    ("what time is it in India?", "In India it's 1:15 at night tomorrow."),
    ("what's the time in Mumbai", "In Mumbai it's 1:15 at night tomorrow."),
    ("time in New York", "In New York it's 2:45 in the afternoon."),
    ("what time is it in the UK", "In the UK it's 7:45 in the evening."),
])
def test_time_there(q, a):
    assert W.handle(q, now=EVENING) == a


def test_time_at_home():
    assert W.handle("what's the time at home?", now=EVENING) == "At home it's 8:45 in the evening."


def test_good_time_to_call_home():
    late = W.handle("is it a good time to call home?", now=EVENING)
    assert late.startswith("Better not, it's 1:15 at night in India.")
    assert "that's 3:30 at night here tomorrow" in late                   # 8:00 in India = 3:30 in Berlin
    assert W.handle("good time to call my family?", now=MORNING) == \
        "Yes, it's 1:30 in the afternoon in India, a good time to call."
    assert W.handle("can I call mom now", now=MORNING).startswith("Yes")
    assert W.handle("is it a good time to call New York?", now=MORNING).startswith("Better not, it's 3 at night")


def test_time_difference():
    assert W.handle("what's the time difference with India?", now=EVENING) == "India is 4 hours 30 minutes ahead of you."
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert W.handle("time difference with India", now=summer) == "India is 3 hours 30 minutes ahead of you."
    assert W.handle("time difference with New York", now=EVENING) == "New York is 6 hours behind you."


@pytest.mark.parametrize("q", ["what time is the lecture?", "what time does the train leave", "is it time to go",
                                "how much time is left?", "set a timer", "what's the time constant of an RC circuit",
                                "is it a good time to call?"])
def test_not_world_clock(q):
    assert not W.matches(q)


def test_spoken():
    t = datetime(2026, 1, 1, 0, 5)
    assert W.spoken(t) == "12:05 at night" and W.spoken(t.replace(hour=12, minute=0)) == "12 in the afternoon"
