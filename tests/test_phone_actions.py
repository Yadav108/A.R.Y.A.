"""ARYA as the phone's assistant: phone tasks become actions the app carries out (arya/server/phone_actions.py)."""
import asyncio
from datetime import datetime

import numpy as np
import pytest

from arya.server import phone_actions as A

NOW = datetime(2026, 11, 21, 22, 0)


@pytest.fixture(autouse=True)
def fresh():
    A._awaiting.update(kind="", until=0.0)
    yield
    A._awaiting.update(kind="", until=0.0)


@pytest.mark.parametrize("text,want", [
    ("set an alarm for 6:45", {"action": "alarm", "hour": 6, "minute": 45}),
    ("wake me up at 7", {"action": "alarm", "hour": 7, "minute": 0}),
    ("alarm at 7:30 pm", {"action": "alarm", "hour": 19, "minute": 30}),
    ("set a timer for 10 minutes", {"action": "timer", "seconds": 600}),
    ("pasta timer 8 minutes", {"action": "timer", "seconds": 480, "label": "Pasta"}),
    ("call Mom", {"action": "call", "who": "Mom", "app": "phone"}),
    ("call 0176 1234567", {"action": "call", "who": "0176 1234567"}),
    ("call Anna on WhatsApp", {"action": "call", "who": "Anna", "app": "whatsapp"}),
    ("text Anna I am running late", {"action": "sms", "who": "Anna", "text": "I am running late", "app": "sms"}),
    ("send a message to Ben saying see you at 8", {"action": "sms", "who": "Ben", "text": "see you at 8"}),
    ("WhatsApp Mom I reached home", {"action": "sms", "who": "Mom", "text": "I reached home", "app": "whatsapp"}),
    ("text Anna Schmidt see you soon", {"action": "sms", "who": "Anna Schmidt", "text": "see you soon"}),
    ("navigate to the main station", {"action": "navigate", "to": "the main station"}),
    ("how do I get to the university", {"action": "navigate", "to": "the university"}),
    ("take me home", {"action": "navigate", "to": "home"}),
    ("turn on the flashlight", {"action": "torch", "on": True}),
    ("torch off", {"action": "torch", "on": False}),
    ("take a selfie", {"action": "camera", "selfie": True, "video": False}),
    ("record a video", {"action": "camera", "video": True}),
    ("turn on wifi", {"action": "settings", "what": "wifi", "state": "on"}),
    ("turn bluetooth off", {"action": "settings", "what": "bluetooth", "state": "off"}),
    ("volume up", {"action": "volume", "dir": "up"}),
    ("put the phone on vibrate", {"action": "volume", "dir": "vibrate"}),
    ("play Kesariya", {"action": "play", "query": "Kesariya", "app": "youtube music"}),
    ("play lofi beats on Spotify", {"action": "play", "query": "lofi beats", "app": "spotify"}),
    ("open WhatsApp", {"action": "open_app", "app": "WhatsApp"}),
    ("NJ, launch Spotify", {"action": "open_app", "app": "Spotify"}),
])
def test_phone_tasks(text, want):
    got = A.parse(text, NOW)
    assert got is not None, text
    for k, v in want.items():
        assert got[k] == v, (text, k, got)
    assert got["say"]


@pytest.mark.parametrize("text", [
    "what time is it", "call me Aryan", "call it a day", "remind me at 7 to call mom", "what is a timer in electronics",
    "open a new tab", "open the Gmail tab", "call an Uber", "text me later", "I have to call Mom later",
    "open YouTube on the laptop", "set an alarm on the laptop for 7", "play it", "play the music", "message",
    "explain the Coriolis acceleration", "how does a flashlight work in detail, explain the circuit please",
])
def test_not_phone_tasks(text):
    assert A.parse(text, NOW) is None


def test_asks_then_takes_the_answer():
    assert A.parse("set a timer", NOW) == {"action": "ask", "say": "For how long?"}
    assert A.parse("ten minutes", NOW)["seconds"] == 600
    assert A.parse("set an alarm", NOW)["action"] == "ask"
    got = A.parse("6:45", NOW)
    assert (got["action"], got["hour"], got["minute"]) == ("alarm", 6, 45)
    assert A.parse("ten minutes", NOW) is None                      # no longer waiting


# ── The phone voice turn ─────────────────────────────────────────────────────

@pytest.fixture
def ws(monkeypatch):
    import arya.server.websocket as ws

    async def stream(text):
        yield "The laptop answered."

    async def synth(sentence):
        return b"RIFF" + sentence.encode()
    monkeypatch.setattr(ws, "stream_with_tools", stream)
    monkeypatch.setattr(ws, "_synthesize", synth)
    return ws


def run_turn(ws, monkeypatch, heard):
    from arya.core import stt
    monkeypatch.setattr(stt, "transcribe_array", lambda audio: heard)

    async def go():
        return [e async for e in ws.phone_voice_turn(np.zeros(16000, np.float32))]
    return asyncio.run(go())


def test_phone_task_becomes_an_action(ws, monkeypatch):
    events = run_turn(ws, monkeypatch, "Call Mom.")
    kinds = [e["type"] for e in events]
    assert kinds == ["heard", "action", "say", "done"]
    act = events[1]
    assert act["action"] == "call" and act["who"] == "Mom" and "say" not in act
    assert events[2]["text"] == "Calling Mom." and events[2]["audio"]
    assert events[-1]["listen"] is False


def test_question_still_goes_to_the_laptop(ws, monkeypatch):
    events = run_turn(ws, monkeypatch, "Why is the sky blue?")
    assert [e["type"] for e in events] == ["heard", "say", "done"]
    assert events[1]["text"] == "The laptop answered."


def test_ask_keeps_listening(ws, monkeypatch):
    events = run_turn(ws, monkeypatch, "Set a timer.")
    assert events[1] == {"type": "action", "action": "ask"} and events[-1]["listen"] is True


def test_contacts_and_messages_are_not_logged(ws, monkeypatch, tmp_path):
    from arya.core import audit
    logged = []
    monkeypatch.setattr(audit, "log_event", lambda kind, **kw: logged.append((kind, kw)))
    run_turn(ws, monkeypatch, "Text Anna I am running late.")
    assert ("phone_action", {"action": "sms"}) in logged
    assert "Anna" not in repr(logged) and "late" not in repr(logged)
