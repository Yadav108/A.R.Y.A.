"""Hindi / Hinglish understanding and Hinglish replies, plus the poetic touch. Whisper is faked."""
import asyncio
from pathlib import Path

import pytest
import yaml

import arya.core.llm as llm
from arya.core import language as L
from arya.core import modes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(L, "_state", {"sticky_until": 0.0, "always": False})
    monkeypatch.setattr(L, "_spoken", {})
    modes.use_memory("default")


# ── Recognising Hinglish ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Yaar, ek baar hit rahi ho chala hai Gisar bhai",          # from the log (ARYA heard it as side talk)
    "Kal ek bajhe mujhe yaad dilana",                          # Whisper's romanized Hindi (measured)
    "yaar aaj kya plan hai, kuch mazedaar batao", "mujhe PID controller simple mein samjhao",
    "kal mera exam hai thoda dar lag raha hai", "gaana chalao bhai",
])
def test_hinglish(text):
    assert L.is_hinglish(text)


def test_no_english_sentence_in_the_benchmarks_looks_hinglish():
    golden = yaml.safe_load((ROOT / "arya/eval/golden.yaml").read_text(encoding="utf-8"))["questions"]
    routes = yaml.safe_load((ROOT / "arya/eval/routes.yaml").read_text(encoding="utf-8"))["routes"]
    english = [q["q"] for q in golden if "hinglish" not in q.get("tags", [])] + [r["say"] for r in routes]
    assert len(english) > 150
    assert [t for t in english if L.is_hinglish(t)] == []


# ── Which language the answer is in ──────────────────────────────────────────

def test_reply_in_kind_and_sticky_for_the_conversation():
    assert L.turn_language("what's the weather like?", now=0) == "en"
    assert L.turn_language("yaar aaj kya plan hai", now=10) == "hinglish"
    assert L.turn_language("and tomorrow?", now=20) == "hinglish"                  # same conversation
    assert L.turn_language("and tomorrow?", now=20 + L.STICKY_S + 1) == "en"       # a new one


def test_devanagari_typed_gets_hinglish():
    assert L.turn_language("आज मौसम कैसा है?", now=0) == "hinglish"


def test_always_hinglish_until_english_please():
    assert L.handle("Hindi mein baat karo").startswith("Bilkul")
    assert L.turn_language("what time is it", now=10 ** 9) == "hinglish"
    assert L.handle("English please") == "Sure, English from now on."
    assert L.turn_language("what time is it", now=10 ** 9) == "en"


def test_speech_translated_from_hindi_counts_as_hinglish():
    L.note_spoken("Kal ek bajhe mujhe yaad dilana", "Remind me tomorrow at 1 o'clock")
    assert L.said("Remind me tomorrow at 1 o'clock") == "Kal ek bajhe mujhe yaad dilana"
    assert L.turn_language("Remind me tomorrow at 1 o'clock", now=0) == "hinglish"


# ── Speech: romanized Hindi is translated for the commands ───────────────────

def test_romanized_hindi_speech_is_translated(monkeypatch):
    from arya.core import stt
    calls = []

    def run(source, prompt=None, **kw):
        calls.append(kw)
        return "Remind me tomorrow at 1 o'clock" if kw.get("task") == "translate" else "Kal ek bajhe mujhe yaad dilana"
    monkeypatch.setattr(stt, "_run_with_cpu_fallback", run)
    import numpy as np
    assert stt.transcribe_array(np.zeros(16000, np.float32)) == "Remind me tomorrow at 1 o'clock"
    assert calls == [{}, {"task": "translate", "language": "hi"}]
    assert L.said("Remind me tomorrow at 1 o'clock") == "Kal ek bajhe mujhe yaad dilana"


def test_english_speech_costs_one_pass(monkeypatch):
    from arya.core import stt
    calls = []
    monkeypatch.setattr(stt, "_run_with_cpu_fallback", lambda s, prompt=None, **kw: calls.append(kw) or "Set a timer")
    import numpy as np
    assert stt.transcribe_array(np.zeros(16000, np.float32)) == "Set a timer" and calls == [{}]


def test_transcript_shows_what_was_said():
    import arya.server.websocket as ws
    L.note_spoken("gaana chalao bhai", "Play a song, brother")
    assert ws._as_heard("Play a song, brother") == "gaana chalao bhai"
    assert ws._as_heard("hello") == "hello"


# ── The prompt ───────────────────────────────────────────────────────────────

def test_hinglish_rule_only_for_hinglish_turns():
    token = llm._turn_language.set("hinglish")
    try:
        p = llm._system_prompt()
        assert "Hinglish" in p and "Never use Devanagari" in p
        assert p.index("Current local date and time") < p.index("Language: the user is talking Hinglish")
    finally:
        llm._turn_language.reset(token)
    assert "Language: the user is talking Hinglish" not in llm._system_prompt()


def test_switch_through_the_pipeline(monkeypatch):
    monkeypatch.setattr(llm, "save_message", lambda role, text: 1)

    async def go():
        return "".join([s async for s in llm.stream_with_tools("talk to me in Hindi")])
    assert asyncio.run(go()).startswith("Bilkul")


# ── Devanagari safety net ────────────────────────────────────────────────────

@pytest.mark.parametrize("dev, latin", [("कल", "kal"), ("का", "kaa"), ("परीक्षा", "pareekshaa"), ("नमस्ते", "namaste"),
                                        ("मैं ठीक हूँ।", "main theek hoon."), ("१० मिनट", "10 minat")])
def test_romanize(dev, latin):
    assert L.romanize(dev) == latin


def test_devanagari_never_reaches_the_voice_in_a_hinglish_turn(monkeypatch):
    async def inner():
        yield "कल का परीक्षा तुम्हारी बहुत दूर नहीं है."

    async def go():
        token = llm._turn_language.set("hinglish")
        try:
            return "".join([s async for s in llm._clean_stream(inner())])
        finally:
            llm._turn_language.reset(token)
    out = asyncio.run(go())
    assert not L.has_devanagari(out) and out.startswith("kal kaa")


# ── Poetic ───────────────────────────────────────────────────────────────────

def test_poetic_touch_in_everyday_mode_only():
    assert "poet's soul" in modes.MODES["default"].persona and "shayari" in modes.MODES["default"].persona
    assert "Never in technical explanations" in modes.MODES["default"].persona
    assert "poet" not in modes.MODES["work"].persona and "poet" not in modes.MODES["study"].persona
