"""Smart listening (was that for ARYA?) and bare 'stop' barge-in while ARYA is talking."""
import asyncio
import time
import types

import numpy as np
import pytest

from arya.core import addressee as A
from arya.core import hotword as H

# Real follow-ups from ARYA's log. True = answer at once, None = local model decides, False = ignore.
FOR_ARYA = ["What is the Coriolis acceleration?", "explain sliding friction", "Can you explain the direction of sliding?",
            "Plot the step response of a second order system with damping ratio 0.2 and natural frequency 3",
            "Decrease the volume by 15%", "Go ahead.", "What's the weather in Munich?", "How much is 15 times 3?"]
UNCLEAR = ["Where is it? Why didn't you tell me?", "Can you explain that?", "Cover.", "local.", "dynamics.",
           "If I stay at home at night, you stay here at night and I stay here at night, then I will sleep.",
           "That would work, wouldn't it?", "Hey, what are you doing? What's wrong with you?", "What the hell was that?",
           "Do you want me to turn off the camera?", "What is it?"]


@pytest.mark.parametrize("text", FOR_ARYA)
def test_clear_requests_are_answered(text):
    assert A.quick_verdict(text) is True


@pytest.mark.parametrize("text", UNCLEAR)
def test_vague_or_background_talk_goes_to_the_model(text):
    assert A.quick_verdict(text) is None


@pytest.mark.parametrize("text", ["Mom, where is my charger?", "Sarah, can you explain that?"])
def test_talking_to_someone_else_is_ignored(text):
    assert A.quick_verdict(text) is False


@pytest.mark.parametrize("mode, text, expected", [
    ("offline", "local.", False),                         # model down + offline: unclear speech ignored
    ("offline", "What is the Coriolis acceleration?", True),
    ("cloud", "local.", True),                            # Groq can still answer
])
def test_model_down_behaviour(monkeypatch, mode, text, expected):
    import arya.core.llm as llm
    import arya.core.llm_local as local

    async def down(*a, **k):
        raise local.LocalLLMUnavailable("Cannot reach Ollama")

    monkeypatch.setattr(local, "chat_local", down)
    monkeypatch.setattr(llm, "ARYA_MODE", mode)
    assert asyncio.run(A.is_addressed(text))[0] is expected


# ── Bare "stop" ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("Stop.", True), ("Uh, stop.", True), ("Okay, let's stop audio.", True), ("Aria stop.", True),
    ("Stop, stop!", True), ("Be quiet please.", True), ("That's enough.", True), ("Cancel that.", True),
    ("Never mind.", True), ("No, no, stop.", True), ("No, no, no.", True), ("Hey ARYA, stop it.", True),
    ("No.", False), ("Bro...", False), ("For a second...", False), ("The rest is up to you.", False),
    ("Stop the motor.", False), ("Don't stop.", False), ("Stop talking about it", False),
    ("It will stop oscillating.", False), ("", False),
])
def test_stop_phrases(text, expected):
    assert H.is_stop_phrase(text) is expected


def _engine(bg_loop, monkeypatch, heard):
    e = H.HotwordEngine()
    e._fake_score = 0.0
    e._detector = types.SimpleNamespace(threshold=0.6, trigger_frames=3, score=lambda c: e._fake_score)
    calls, transcribed = [], []

    async def cb(cmd, is_wake):
        calls.append(cmd)

    e.set_callback(cb, bg_loop)

    def fake_transcribe(audio):
        transcribed.append(len(audio) / H.SR)
        return heard[0]

    monkeypatch.setattr(H, "_transcribe", fake_transcribe)
    return e, calls, transcribed


_rng = np.random.default_rng(0)


def _chunk(level):
    return (_rng.standard_normal(H.CHUNK) * level).astype(np.float32)


def _arya_voice(e, seconds, level=0.05):
    for i in range(int(seconds * H.SR / H.CHUNK)):
        lvl = 0.006 if i % 30 in (28, 29) else 0.012 if i % 4 == 3 else level * _rng.uniform(0.6, 1.4)
        e._on_chunk(_chunk(lvl))


def test_bare_stop_interrupts_but_arya_never_stops_itself(bg_loop, monkeypatch):
    heard = ["Stop."]
    e, calls, transcribed = _engine(bg_loop, monkeypatch, heard)
    e.set_speaking(True)
    _arya_voice(e, 8)                                     # ARYA talking alone, from a cold start
    time.sleep(0.3)
    assert calls == [] and len(transcribed) <= 2
    transcribed.clear()
    for _ in range(5):                                    # you say "stop", louder than ARYA's bleed
        e._on_chunk(_chunk(0.06) + _chunk(0.25))
    _arya_voice(e, 1.0)
    time.sleep(0.3)
    assert calls == ["stop"] and len(transcribed) == 1 and transcribed[0] < 1.5


def test_long_loud_speech_is_not_transcribed(bg_loop, monkeypatch):
    e, calls, transcribed = _engine(bg_loop, monkeypatch, ["Stop."])
    e.set_speaking(True)
    _arya_voice(e, 4)
    time.sleep(0.3)
    transcribed.clear()
    for _ in range(38):
        e._on_chunk(_chunk(0.25))
    _arya_voice(e, 0.6)
    time.sleep(0.3)
    assert transcribed == [] and calls == []


def test_wake_word_takes_over_a_stop_burst(bg_loop, monkeypatch):
    e, _, _ = _engine(bg_loop, monkeypatch, ["Stop."])
    e.set_speaking(True)
    _arya_voice(e, 4)
    e._on_chunk(_chunk(0.3))
    e._on_chunk(_chunk(0.3))
    assert e._capture and e._capture["kind"] == "stop"
    e._fake_score = 0.95
    for _ in range(3):
        e._on_chunk(_chunk(0.3))
    assert e._capture and e._capture["kind"] == "wake"


def test_arya_saying_stop_itself_is_ignored(bg_loop, monkeypatch):
    heard = ["Uh, stop."]
    e, calls, _ = _engine(bg_loop, monkeypatch, heard)
    burst = {"audio": [np.zeros(1280, np.float32)], "levels": [0.2]}
    e.note_spoken("If the controller saturates, the motor will stop.")
    e._finish_stop(dict(burst))
    time.sleep(0.2)
    assert calls == []                                    # ARYA just said "stop" itself
    heard[0] = "Be quiet."
    e._finish_stop(dict(burst))
    time.sleep(0.2)
    assert calls == ["stop"]                              # a different stop word still works
