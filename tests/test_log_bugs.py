"""Bugs found in the real-use log of 2026-11-14 … 16 (see the PR). The model, audio and media are faked."""
import asyncio
import time
from datetime import datetime, timedelta

import pytest

import arya.core.llm as llm
from arya.core import addressee, policy
from arya.tools import alarms


# ── 1. What was blocked is not remembered ────────────────────────────────────

def test_blocked_claims_are_not_saved_as_said():
    policy.begin_turn("is there any time around?")                    # nothing acted this turn
    said = llm._as_said("The Study Timer is set for 1 hour and 30 minutes. Anything else? Alarm set for 08:00.")
    assert "Study Timer is set" not in said and "Alarm set" not in said
    assert said.count(llm._NOT_DONE) == 1 or "Anything else?" in said
    assert llm._as_said("Newton's second law is F = m a.") == "Newton's second law is F = m a."


# ── 2. Timers: hours, lead-ins, labels ───────────────────────────────────────

@pytest.mark.parametrize("text, seconds", [
    ("set a timer of one hour", 3600),                                 # from-log: became 1 minute
    ("set a 1 hours decision timer for…", 3600),                       # from-log
    ("Just set a timer for 10 minutes.", 600),                         # from-log: reached the model
    ("So set a timer for one and a half hours", 5400),
    ("set a timer for an hour and a half", 5400),
    ("set a timer for 1 hour and 30 minutes", 5400),
    ("set a timer for half an hour", 1800),
    ("start a 90 minute study timer", 5400),
])
def test_timer_durations(text, seconds, monkeypatch):
    set_ = []

    async def fake_set_timer(duration_seconds, label="Timer"):
        set_.append((duration_seconds, label))
        return "ok"
    import arya.tools.productivity as productivity
    monkeypatch.setattr(productivity, "set_timer", fake_set_timer)
    assert alarms._TIMER.search(text), text
    reply = asyncio.run(alarms.handle(text))
    assert set_ and set_[0][0] == seconds, (text, set_, reply)


def test_timer_labels():
    assert alarms._timer_label("set a 1 hours decision timer") == "Decision"
    assert alarms._timer_label("start a 90 minute study timer") == "Study"
    assert alarms._timer_label("set a timer for 10 minutes for the pasta") == "Pasta"
    assert alarms._timer_label("set a timer for 10 minutes") == "Timer"


def test_model_timer_uses_the_users_words(monkeypatch):
    got = []

    async def fake_set_timer(duration_seconds, label="Timer"):
        got.append(duration_seconds)
        return "ok"
    import arya.tools.productivity as productivity
    monkeypatch.setattr(productivity, "set_timer", fake_set_timer)
    policy.begin_turn("we're studying, set a timer of one hour")
    asyncio.run(llm._dispatch_tool("set_timer", {"minutes": 1}))      # what the model sent (from the log)
    assert got == [3600]
    policy.begin_turn("set a timer")
    asyncio.run(llm._dispatch_tool("set_timer", {"hours": 2}))
    assert got[-1] == 7200


# ── 3. Timer status comes from the real timers ───────────────────────────────

@pytest.mark.parametrize("text", ["Is the timer set?", "how much time is left?", "any timers running?",
                                  "is my study timer still running", "what timers do I have"])
def test_timer_status_is_answered_from_real_timers(text, monkeypatch):
    async def fake_list():
        return "Timer 'Study': 1 hour 29 minutes left."
    import arya.tools.productivity as productivity
    monkeypatch.setattr(productivity, "list_timers", fake_list)
    assert asyncio.run(alarms.handle(text)) == "Timer 'Study': 1 hour 29 minutes left."


def test_status_not_confused_with_exam_time():
    assert not alarms._STATUS.search("how much time is left until my exam")


# ── 4. Conversations end by the wall clock too ───────────────────────────────

def test_conversation_ends_after_sleep(monkeypatch):
    from arya.core import hotword
    eng = hotword.HotwordEngine.__new__(hotword.HotwordEngine)
    eng._followup_active = False
    eng._followup_deadline = 0.0
    eng._refresh_followup_mode()
    assert eng._followup_mode_enabled()
    # monotonic time stood still during sleep; the wall clock moved 13 hours
    real_time = time.time
    monkeypatch.setattr(hotword.time, "time", lambda: real_time() + 13 * 3600)
    assert not eng._followup_mode_enabled()


# ── 5. Whisper: smaller model on the CPU, stale speech dropped ───────────────

def test_cpu_fallback_uses_a_smaller_model(monkeypatch):
    from arya.core import stt
    loaded = []

    class FakeWhisper:
        def __init__(self, name, device, compute_type):
            loaded.append((name, device))
            if name == "small":
                raise RuntimeError("mkl_malloc: failed to allocate memory")
    monkeypatch.setattr(stt, "WhisperModel", FakeWhisper)
    stt._load_on_cpu()
    assert loaded == [("small", "cpu"), ("base", "cpu")]


def test_stale_speech_is_dropped(monkeypatch):
    from arya.core import hotword
    assert hotword.STALE_SPEECH_S <= 10
    gate = hotword._STT_GATE
    assert gate.acquire(timeout=1)
    try:
        monkeypatch.setattr(hotword, "STALE_SPEECH_S", 0.05)
        eng = hotword.HotwordEngine.__new__(hotword.HotwordEngine)
        called = []
        monkeypatch.setattr(hotword, "_transcribe", lambda audio: called.append(1) or "hello")
        import numpy as np
        cap = {"kind": "followup", "voiced": 10, "speech": True, "audio": [np.zeros(160, dtype="float32")]}
        eng._finish(cap)
        assert called == []                                                # dropped, not answered late
    finally:
        gate.release()


# ── 7. Logging can't kill a thread ───────────────────────────────────────────

def test_log_writer_survives_a_broken_stream():
    from arya.desktop.app import StampedStream

    class Broken:
        def write(self, s):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")
    s = StampedStream(Broken())
    assert s.write("WakeWord error: MemoryError\n") > 0
    s.flush()


# ── 8. Play is play, pause is pause ──────────────────────────────────────────

@pytest.mark.parametrize("text, action", [("pause the music", "pause"), ("resume the song", "play"),
                                          ("play", "play"), ("play the song", "play"), ("continue", "play")])
def test_media_words(text, action):
    assert llm.quick_media_command(text) == {"action": action}


def test_play_does_not_toggle_when_already_playing(monkeypatch):
    from arya.tools import media, music
    pressed = []
    monkeypatch.setattr(media, "_press", lambda vk, times=1: pressed.append(vk))

    async def already(play, app=None):
        return "already"
    monkeypatch.setattr(music, "set_playing", already)
    assert asyncio.run(media.media_control("play")) == "It's already playing."
    assert pressed == []                                                   # never toggled it off

    async def none(play, app=None):
        return None
    monkeypatch.setattr(music, "set_playing", none)
    asyncio.run(media.media_control("pause"))
    assert pressed == [media._VK["play_pause"]]                            # no media controls: the key


# ── 9. Smart listening rules ─────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["Okay, go ahead.", "Yes, sir.", "Thank you very much.",
                                  "Yeah, thank you sweetheart.", "Hey, Ariel."])
def test_said_to_arya(text):
    assert addressee.quick_verdict(text) is True or addressee.split_name(text)[1], text


@pytest.mark.parametrize("text", ["Plus, set a timer for 5 minutes", "Basically, open Chrome",
                                  "Tomorrow, remind me to call the lab"])
def test_capitalised_first_word_is_not_a_name(text):
    assert addressee.quick_verdict(text) is not False, text


def test_still_side_talk():
    assert addressee.quick_verdict("Mom, where is my charger?") is False
    assert addressee.quick_verdict("Sarah, did you see that?") is False


def test_bare_app_name():
    assert addressee.quick_verdict("Windows Explorer") is True             # from-log: overridden


# ── 10. Report: overrides only right after ───────────────────────────────────

def test_override_needs_to_be_recent(tmp_path):
    from arya import report
    t0 = datetime(2026, 11, 16, 0, 0, 0)
    log = tmp_path / "arya.log"
    lines = [
        (t0, "Not for ARYA (model (0.2s)) — staying quiet: 'Yes. Yeah, bro.'"),
        (t0 + timedelta(minutes=33), "Processing: Yes"),
        (t0 + timedelta(minutes=40), "Not for ARYA (model (0.2s)) — staying quiet: 'Windows Explorer'"),
        (t0 + timedelta(minutes=40, seconds=20), "Processing: Windows Explorer"),
    ]
    log.write_text("".join(f"[{t:%Y-%m-%d %H:%M:%S}] {s}\n" for t, s in lines), encoding="utf-8")
    rep = report.parse(days=30, paths=[log], now=t0 + timedelta(hours=1))
    assert [t.text for t in rep.turns if t.overridden] == ["Windows Explorer"]
