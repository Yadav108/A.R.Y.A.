"""2026-11-21: "resume the music" was claimed, not done; "It did not." got "I am checking right now"."""
import asyncio

import pytest

import arya.core.llm as llm
from arya.core import addressee, policy


@pytest.mark.parametrize("text,rest", [("First, resume the music.", "resume the music."),
                                       ("Then, next song.", "next song."), ("Okay, pause the music.", "pause the music.")])
def test_lead_ins_before_a_command(text, rest):
    assert addressee.strip_filler(text) == rest
    assert addressee.quick_verdict(text) is True


def test_lead_in_before_chat_stays():
    assert addressee.strip_filler("First, I think so.") == "First, I think so."


@pytest.mark.parametrize("text", ["It did not.", "it didn't work", "Did you check?", "No, it's still paused.",
                                  "nothing happened", "it isn't playing", "the music didn't resume", "still not playing"])
def test_complaints(text):
    assert llm._COMPLAINT.match(text)


@pytest.mark.parametrize("text", ["It did not rain yesterday in Berlin", "Did you check the weather in Delhi?",
                                  "it's still raining", "resume the music"])
def test_not_complaints(text):
    assert not llm._COMPLAINT.match(text)


def _history(monkeypatch, *users):
    rows = []
    for u in users:
        rows += [{"role": "user", "content": u}, {"role": "assistant", "content": "Okay."}]
    monkeypatch.setattr(llm, "get_history", lambda limit=10: rows[-limit:])


def _player(monkeypatch, playing_after):
    calls = []

    async def execute(tool, args, confirmed=False):
        calls.append((tool, args))
        return "ok"

    async def now_playing():
        return {"title": "Tumhen Dillagi", "artist": "", "app": "Chrome", "playing": playing_after}

    async def no_sleep(s):
        return None
    from arya.tools import music
    monkeypatch.setattr(llm, "_execute_tool", execute)
    monkeypatch.setattr(music, "now_playing", now_playing)
    monkeypatch.setattr(llm.asyncio, "sleep", no_sleep)
    return calls


def test_it_did_not_presses_play_and_checks(monkeypatch):
    _history(monkeypatch, "First, resume the music.")
    calls = _player(monkeypatch, playing_after=True)
    reply = asyncio.run(llm._retry_after_complaint("It did not."))
    assert calls == [("media_control", {"action": "play"})]
    assert "Tumhen Dillagi is playing" in reply


def test_did_you_check_after_two_complaints(monkeypatch):
    _history(monkeypatch, "resume the music", "It did not.")
    calls = _player(monkeypatch, playing_after=False)
    reply = asyncio.run(llm._retry_after_complaint("Did you check?"))
    assert calls and "nothing is playing" in reply


def test_complaint_about_something_else_goes_to_the_model(monkeypatch):
    _history(monkeypatch, "what's the capital of Peru?")
    calls = _player(monkeypatch, playing_after=True)
    assert asyncio.run(llm._retry_after_complaint("It did not.")) is None and calls == []


@pytest.mark.parametrize("claim", ["Okay, the music will now resume.", "The music should be playing shortly.",
                                   "I see. Let me check that for you.", "I am checking right now, Aryan."])
def test_claims_blocked(claim):
    policy.begin_turn("It did not.")
    assert llm._guard_claim(claim, quiet=True) == llm._NOT_DONE


@pytest.mark.parametrize("text", ["Let me check the formula in your notes: F equals m a.",
                                  "The library is open until 8."])
def test_not_claims(text):
    policy.begin_turn("question")
    assert llm._guard_claim(text, quiet=True) != llm._NOT_DONE


@pytest.mark.parametrize("text,lone", [("Enjoy.", True), ("Engine?", True), ("NJ.", True), ("Hey, NJ.", True),
                                       ("Enjoy your meal.", False), ("resume the music", False)])
def test_lone_name(text, lone):
    assert addressee.lone_nj(text) is lone
