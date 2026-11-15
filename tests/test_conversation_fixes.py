"""Fixes from the conversations of 2026-11-16 (all phrasings are real transcripts from the log):
alarms and reminders that were only *claimed*, "want me to …?" -> "yes" that did nothing, commands said over
ARYA's "Yes?", requests split by a pause, "Windows Explorer", songs on YouTube Music, and the personality."""
import asyncio
from datetime import datetime

import pytest

import arya.core.llm as llm
from arya.core import addressee, policy
from arya.tools import alarms as A
from arya.tools import browser as B
from arya.tools import productivity

NIGHT = datetime(2026, 11, 16, 1, 57)
EVENING = datetime(2026, 11, 16, 21, 0)


@pytest.fixture
def clock(monkeypatch):
    set_ = []

    async def fake_reminder(message, at_time=None, in_minutes=None):
        set_.append(("reminder", round(in_minutes), message))
        return "ok"

    async def fake_timer(seconds, label="Timer"):
        set_.append(("timer", seconds, label))
        return "ok"
    monkeypatch.setattr(productivity, "set_reminder", fake_reminder)
    monkeypatch.setattr(productivity, "set_timer", fake_timer)
    monkeypatch.setattr(A, "_slot", None)
    policy.begin_turn("x")
    return set_


def _h(m, now):
    return asyncio.run(A.handle(m, now))


def test_the_reminder_from_last_night_is_actually_set(clock):
    assert _h("Remind me tomorrow at...", NIGHT).startswith("At what time?")
    assert _h("13:00", NIGHT) == "What should I remind you about?"
    assert _h("to send working hours to supervisor Good night", NIGHT) == \
        "Okay, I'll remind you at 13:00 this afternoon to send working hours to supervisor."
    assert clock == [("reminder", 663, "send working hours to supervisor")]          # 01:57 -> 13:00 = 663 min


@pytest.mark.parametrize("said, now, reply, minutes", [
    ("Set an alarm for tomorrow's morning 8 a.m.", NIGHT, "Alarm set at 08:00 this morning.", 363),
    ("Set an alarm for tomorrow's morning 8 a.m.", EVENING, "Alarm set at 08:00 tomorrow.", 660),
    ("wake me up at 7", EVENING, "Alarm set at 07:00 tomorrow.", 600),
    ("remind me in 20 minutes to take the laundry out", EVENING,
     "Okay, I'll remind you at 21:20 today to take the laundry out.", 20),
])
def test_alarms_and_reminders(clock, said, now, reply, minutes):
    assert _h(said, now) == reply
    assert clock[0][1] == minutes


def test_alarm_without_a_time_asks(clock):
    assert _h("Set an alarm.", NIGHT).startswith("At what time?")
    assert _h("8 a.m.", NIGHT) == "Alarm set at 08:00 this morning."
    assert _h("Set an alarm.", NIGHT).startswith("At what time?")
    assert _h("never mind", NIGHT) == "Okay, no reminder." and not A.awaiting()


def test_tomorrow_carries_over(clock):
    _h("remind me tomorrow to buy milk", EVENING)
    assert _h("at 9", EVENING) == "Okay, I'll remind you at 09:00 tomorrow to buy milk."


def test_timers(clock):
    assert _h("Set a timer for 23 minutes.", EVENING) == "Timer set for 23 minutes."
    assert _h("set a 5 minute timer for the pasta", EVENING) == "Pasta timer set for 5 minutes."
    assert clock[-1] == ("timer", 300, "Pasta")


@pytest.mark.parametrize("message", ["what is an alarm clock circuit", "explain the timer interrupt in a microcontroller",
                                     "I set an alarm yesterday", "play some music"])
def test_not_alarms(clock, message):
    assert _h(message, EVENING) is None


@pytest.mark.parametrize("claim", ["I will set a reminder for 13:00 tomorrow to send your working hours.",
                                   "I'll play it for you now.", "I will take note of a reminder for you."])
def test_promises_need_a_real_action(claim):
    policy.begin_turn("x")
    assert llm._guard_claim(claim) == llm._NOT_DONE


# ── "Want me to …?" -> "yes" ─────────────────────────────────────────────────

@pytest.fixture
def offers(monkeypatch):
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    policy.cancel_pending()
    yield
    policy.cancel_pending()


@pytest.mark.parametrize("reply, act", [
    ("I see. How about I search for \"Kon Tujhe by Kishore Kumar\" and play the first result? Would you like that?",
     "search for \"Kon Tujhe by Kishore Kumar\" and play the first result"),
    ("Do you want me to set a reminder for you at 13:00?", "set a reminder for me at 13:00"),
    ("Would you like me to explain the Coriolis term step by step?", "explain the Coriolis term step by step"),
])
def test_offers_are_remembered(offers, reply, act):
    llm.remember_offer(reply)
    assert policy.has_pending()
    assert policy._state.pending.args["request"] == act


@pytest.mark.parametrize("reply", ["Is there anything else I can help you with?", "Would you like to know more?",
                                   "The damping ratio is 0.5.", "Can you tell me more about the system?"])
def test_not_offers(offers, reply):
    llm.remember_offer(reply)
    assert not policy.has_pending()


def test_yes_runs_the_offer(offers, monkeypatch):
    llm.remember_offer("Shall I open Chrome?")
    ran = []

    async def fake_open(name):
        ran.append(name)
        return f"Opened {name}"
    import arya.tools.os_tools as os_tools
    monkeypatch.setattr(os_tools, "open_app", fake_open)

    async def turn():
        return "".join([s async for s in llm.stream_with_tools("yes")])
    assert asyncio.run(turn()) == "Opening Chrome."
    assert ran == ["chrome"]


# ── Listening ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("heard, command", [
    ("Decrease the volume by 50 percent.", True), ("ARYA, open YouTube", True),
    ("according to", False), ("Thank you.", False), ("that's the natural frequency of the system", False),
])
def test_commands_said_over_arya(heard, command):
    from arya.core.hotword import HotwordEngine
    eng = HotwordEngine.__new__(HotwordEngine)
    from collections import deque
    eng._recent_spoken = deque([(__import__("time").monotonic(), "The natural frequency of the system is ten.")])
    assert eng._is_barge_in_command(heard) is command


def test_arya_own_words_are_not_a_command():
    import time
    from collections import deque

    from arya.core.hotword import HotwordEngine
    eng = HotwordEngine.__new__(HotwordEngine)
    eng._recent_spoken = deque([(time.monotonic(), "Opening YouTube Music. Tell me a song or an artist.")])
    assert eng._is_barge_in_command("opening YouTube music") is False


@pytest.mark.parametrize("said, app", [("Windows Explorer", "windows explorer"), ("open file explorer", "file explorer"),
                                       ("launch the calculator app", "calculator")])
def test_bare_app_names_open(offers, monkeypatch, said, app):
    ran = []

    async def fake_open(name):
        ran.append(name)
        return f"Opened {name}"
    import arya.tools.os_tools as os_tools
    monkeypatch.setattr(os_tools, "open_app", fake_open)

    async def turn():
        return "".join([s async for s in llm.stream_with_tools(said)])
    assert asyncio.run(turn()).startswith("Opening") and ran == [app]


# ── Music ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, query", [("the song \"Kaun Tujhe\" by Kishore Kumar", "Kaun Tujhe by Kishore Kumar"),
                                        ("kaun tujhe song", "kaun tujhe"), ("song Kesariya", "Kesariya")])
def test_song_query(raw, query):
    assert B._song_query(raw) == query


def test_what_was_the_music():
    from arya.tools import music
    assert music._WHAT.search("What was the music?") and music._WHAT.search("what was that song")


# ── Personality ────────────────────────────────────────────────────────────

def test_personality_in_the_prompt():
    p = llm._system_prompt()
    assert "playful" in p and "caring" in p and "Precision comes first" in p and "Never promise an action" in p


def test_wake_acks_vary_and_notice_late_nights(monkeypatch):
    import arya.server.websocket as ws
    acks = {ws._pick("wake", ws.WAKE_ACKS) for _ in range(30)}
    assert len(acks) >= 3
    first = [ws._pick("wake", ws.WAKE_ACKS) for _ in range(20)]
    assert all(a != b for a, b in zip(first, first[1:]))              # never the same twice in a row
