"""Intercom both ways, find my phone / laptop. The speaker, the phone feed and the clipboard are faked."""
import asyncio
import io
import wave

import pytest

from arya.core import intercom as I


@pytest.fixture
def spoken(monkeypatch):
    said = []

    async def speak(text, sound, shown):
        said.append((text, sound, shown))
    I.set_speaker(speak)
    yield said
    I.set_speaker(None)


@pytest.fixture
def phone(monkeypatch):
    from arya.server import phone as P
    pushed = []
    monkeypatch.setattr(P, "enabled", lambda: True)
    monkeypatch.setattr(P, "push", lambda text, kind="nudge", url=None: pushed.append((text, kind, url)))
    return pushed


def _run(message, remote):
    async def go():
        reply = I.handle(message, remote=remote)
        await asyncio.sleep(0)                                   # let the scheduled speech run
        return reply
    return asyncio.run(go())


# ── Phone → laptop ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("message, text", [
    ('Say "bharat don\'t cry" on laptop', "bharat don't cry"),        # from the phone log, tried three times
    ("say dinner is ready on the laptop", "dinner is ready"),
    ("announce: dinner is ready", "dinner is ready"),
    ("tell everyone I'm on my way", "I'm on my way"),
    ("please read out 'take the washing out' through the speakers", "take the washing out"),
])
def test_phone_makes_the_laptop_speak(message, text, spoken):
    reply = _run(message, remote=True)
    assert text in reply
    assert spoken == [(text, I.CHIME, f"📣 From your phone: {text}")]


def test_find_my_laptop(spoken):
    assert "beeping" in _run("where's my laptop?", remote=True)
    assert spoken[0][1] == I.BEEPS and "I'm here" in spoken[0][0]


# ── Laptop → phone ───────────────────────────────────────────────────────────

def test_send_the_last_answer(phone, monkeypatch):
    monkeypatch.setattr(I, "_last_answer", lambda: "The lab is booked for Friday at 10. Details: https://lab.example/b")
    assert _run("send this to my phone", remote=False) == "Sent to your phone."
    assert phone == [("The lab is booked for Friday at 10. Details: https://lab.example/b", "message",
                      "https://lab.example/b")]


def test_send_a_link_from_the_clipboard(phone, monkeypatch):
    monkeypatch.setattr(I, "_clipboard_url", lambda: "https://youtube.com/watch?v=abc")
    _run("send this link to my phone", remote=False)
    assert phone == [("https://youtube.com/watch?v=abc", "message", "https://youtube.com/watch?v=abc")]


def test_send_text(phone):
    _run("send 'buy resistors' to my phone", remote=False)
    assert phone == [("buy resistors", "message", None)]


def test_ring_my_phone(phone):
    assert "Ringing your phone" in _run("ring my phone", remote=False)
    assert phone[0][1] == "ring"


def test_phone_access_off(monkeypatch):
    from arya.server import phone as P
    monkeypatch.setattr(P, "enabled", lambda: False)
    assert "Phone access is off" in _run("send this to my phone", remote=False)


def test_from_the_phone_itself(phone):
    assert "on your phone right now" in _run("ring my phone", remote=True)
    assert phone == []


# ── Not intercom ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["say hello", "tell me a joke", "where is the laptop charger?",
                                  "where is my phone charger", "send an email to Ben", "what did you say?",
                                  "connect my phone", "call my phone", "find my files"])
def test_not_intercom(text):
    assert not I.matches(text)


# ── Sounds and the phone feed ────────────────────────────────────────────────

def test_tones_are_valid_wav():
    with wave.open(io.BytesIO(I.CHIME)) as w:
        assert w.getframerate() == I.SR and 0.3 < w.getnframes() / I.SR < 0.5
    with wave.open(io.BytesIO(I.BEEPS)) as w:
        assert w.getnframes() / I.SR > 1.0


def test_feed_items_keep_kind_and_only_web_links(monkeypatch):
    from arya.server import phone as P
    monkeypatch.setattr(P, "_state", P._State())
    P.push("hi", kind="message", url="https://example.org")
    P.push("bad", kind="message", url="javascript:alert(1)")
    P.push("🔔 ring", kind="ring")
    items = P.feed_after(0)
    assert [(i["kind"], i.get("url")) for i in items] == [("message", "https://example.org"), ("message", None),
                                                          ("ring", None)]


def test_through_the_phone_turn(spoken, monkeypatch):
    """End to end: the phone's request goes through stream_with_tools and comes back as text."""
    import arya.core.llm as llm
    import arya.server.websocket as ws
    monkeypatch.setattr(llm, "save_message", lambda role, text: 1)

    async def go():
        out = await ws.phone_turn('Say "bharat don\'t cry" on laptop')
        await asyncio.sleep(0)
        return out
    out = asyncio.run(go())
    assert "bharat don't cry" in out["reply"] and spoken[0][0] == "bharat don't cry"
