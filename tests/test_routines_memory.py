"""C4: what ARYA remembers about you, your own routines, and the morning briefing.
The model and the briefing are faked; nothing is spoken."""
import asyncio
from datetime import datetime, timedelta

import pytest

import arya.core.llm as llm
from arya.core import proactive as P
from arya.core import routines as R
from arya.memory import profile as M


@pytest.fixture
def clean(kb, monkeypatch):
    with M._db() as c:
        c.execute("DELETE FROM profile")
    with R._db() as c:
        c.execute("DELETE FROM routines")
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    yield
    with M._db() as c:
        c.execute("DELETE FROM profile")
    with R._db() as c:
        c.execute("DELETE FROM routines")


def _say(m):
    async def turn():
        return "".join([s async for s in llm.stream_with_tools(m)])
    return asyncio.run(turn())


# ── Memory ─────────────────────────────────────────────────────────────────────

def test_remember_list_and_forget(clean):
    assert M.handle("Remember that I prefer short answers.") == "Got it, I'll keep that in mind from now on."
    assert M.handle("remember my lab partner is Anna") is not None
    assert M.handle("Always use metric units") is not None
    assert [r["text"] for r in M.items()] == ["The user prefers short answers", "The user's lab partner is Anna",
                                              "Always use metric units"]
    assert [r["kind"] for r in M.items()] == ["preference", "fact", "preference"]
    assert M.handle("what do you remember about me?") == ("I remember: You prefer short answers; Your lab partner "
                                                          "is Anna; Always use metric units.")
    assert M.handle("forget that my lab partner is Anna") == "Okay, I forgot that."
    assert len(M.items()) == 2
    assert M.handle("forget everything about me") == "Okay, I forgot 2 things."


@pytest.mark.parametrize("message", ["remember this: the lab is in room B12", "remember to buy milk",
                                     "do you remember the formula for torque?", "forget it",
                                     "I always forget the parallel axis theorem"])
def test_not_profile(clean, message):
    assert M.handle(message) is None


def test_memory_goes_into_the_local_prompt_only(clean, monkeypatch):
    M.handle("remember that I prefer short answers")
    assert "The user prefers short answers" in llm._system_prompt()
    monkeypatch.setattr(llm, "_groq_allowed", lambda: True)                  # cloud mode: never sent out
    assert "prefers short answers" not in llm._system_prompt()


def test_memory_is_capped(clean):
    for i in range(M.MAX_ITEMS + 3):
        M.add(f"I like topic number {i}")
    assert len(M.items()) == M.MAX_ITEMS and M.items()[0]["text"].endswith("number 3")


# ── Routines ───────────────────────────────────────────────────────────────────

def test_define_list_delete(clean):
    reply = R.handle("When I say start my day, give me the briefing, today's calendar and play focus music.")
    assert reply == ("Okay. When you say \"start my day\", I'll give you the briefing, give you today's calendar "
                     "and then play focus music.")
    assert R.match("Start my day.") == ["give me the briefing", "give me today's calendar", "play focus music"]
    assert R.match("ARYA, start my day please") is not None
    assert R.match("start my day tomorrow") is None
    assert R.handle("what routines do I have?").startswith("Your routines: \"start my day\"")
    assert R.handle("delete the start my day routine") == "Deleted the \"start my day\" routine."
    assert R.match("start my day") is None


def test_running_a_routine_runs_each_step(clean, monkeypatch):
    R.define("study time", ["what time is it", "what is the damping ratio"])
    seen = []
    real = llm.stream_with_tools

    async def spy(message):
        seen.append(message)
        if message != "study time":
            yield f"[{message}]"
            return
        async for s in real(message):
            yield s
    monkeypatch.setattr(llm, "stream_with_tools", spy)
    out = "".join(asyncio.run(_collect(real("study time"))))
    assert seen == ["what time is it", "what is the damping ratio"] and out == "[what time is it][what is the damping ratio]"


async def _collect(gen):
    return [s async for s in gen]


def test_a_routine_cannot_run_a_routine(clean):
    R.define("a", ["say hi"])
    assert R.handle("when I say b, a") == "A routine can't run another routine. Tell me the steps themselves."


# ── Morning briefing ───────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 17, 8, 30)


def S(minutes=0.0, idle=5.0, **kw):
    return P.Sensors(now=T0 + timedelta(minutes=minutes), idle_s=idle, **kw)


def test_morning_briefing_once_when_you_come_back():
    n = P.Nudger(quiet_hours=(22, 8))
    assert n.morning(S(0, idle=3600)) is False                   # still away
    assert n.morning(S(1, idle=10)) is True                      # back at the laptop
    assert n.morning(S(2, idle=10)) is False                     # once a day
    assert n.morning(S(60 * 24, idle=10)) is True                # next morning (laptop slept in between)


def test_morning_briefing_rules():
    first_thing = P.Nudger(quiet_hours=(22, 8))
    assert first_thing.morning(S(0, idle=10)) is True            # ARYA started this morning, you're active
    for blocked in ({"morning_off": True}, {"muted_until": T0.timestamp() + 3600}):
        n = P.Nudger(quiet_hours=(22, 8), **blocked)
        assert n.morning(S(0, idle=10)) is False
    early = P.Nudger(quiet_hours=(22, 8))
    assert early.morning(P.Sensors(now=datetime(2026, 11, 17, 7, 30), idle_s=10)) is False     # quiet hours
    late = P.Nudger(quiet_hours=(22, 8))
    assert late.morning(P.Sensors(now=datetime(2026, 11, 17, 13, 0), idle_s=10)) is False     # afternoon
    busy = P.Nudger(quiet_hours=(22, 8))
    assert busy.morning(S(0, idle=10, windows_ok=False)) is False


def test_morning_voice(kb, monkeypatch):
    monkeypatch.setattr(P, "nudger", P.Nudger())
    assert P.handle("no morning briefing").startswith("Okay, no morning briefing")
    assert P.nudger.morning_off
    assert P.handle("turn on the morning briefing").startswith("Okay, I'll give you the briefing")
    assert not P.nudger.morning_off


def test_briefing_on_request(clean, monkeypatch):
    async def fake():
        return "It is Monday, 3 degrees and cloudy."
    monkeypatch.setattr(llm, "get_daily_briefing", fake)
    assert _say("give me the briefing") == "It is Monday, 3 degrees and cloudy."
    assert llm._BRIEFING.search("what is a briefing in project management") is None
