"""The webcam, only when asked: what triggers it, the phone refusal, busy/covered cameras, text read locally,
a picture offered to the cloud only after asking. A fake camera — the real one never switches on in tests."""
import asyncio

import pytest
from PIL import Image

from arya.core import policy, router
from arya.tools import screen
from arya.tools import webcam as W


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    policy.cancel_pending()
    screen._context.update(text="", title="", image_id="", until=0.0, kind="screen")
    events = []
    import arya.core.audit as audit
    monkeypatch.setattr(audit, "log_event", lambda *a, **k: events.append((a, k)))
    yield events
    policy.cancel_pending()


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("text", [
    "take a photo with the webcam", "snap a picture using the camera", "take a selfie with the webcam",
    "what do you see?", "Hey ARYA, what am I holding?", "look at this", "can you see me?",
    "use the webcam to read this page", "read what I'm holding up", "open the webcam",
    "can you read this with the camera?", "look through the laptop camera",
])
def test_matches(text):
    assert W.matches(text)


@pytest.mark.parametrize("text", [
    "what do you see on my screen?", "look at this window", "take a screenshot", "how do I open the camera app?",
    "webcam settings", "my webcam isn't working", "what's a good camera for vlogging?", "take a photo",
    "explain this photo", "what do you think?", "I see what you mean", "look at this error on the screen",
    "what camera does the iPhone 15 have?",
])
def test_not_matches(text):
    assert not W.matches(text)


def test_never_from_the_phone(monkeypatch):
    monkeypatch.setattr(W, "grab", lambda: pytest.fail("the camera must not open for the phone"))
    reply, artifacts = run(W.handle("take a photo with the webcam", remote=True))
    assert "only switch on the laptop's camera when you ask at the laptop" in reply and artifacts == []


def test_busy_camera(monkeypatch):
    def busy():
        raise RuntimeError("busy")
    monkeypatch.setattr(W, "grab", busy)
    reply, _ = run(W.handle("what do you see?"))
    assert "video call" in reply


def test_covered_lens(monkeypatch):
    monkeypatch.setattr(W, "grab", lambda: Image.new("RGB", (64, 48), (2, 2, 2)))
    reply, artifacts = run(W.handle("what do you see?"))
    assert "shutter" in reply and artifacts == []


def _frame():
    return Image.new("RGB", (64, 48), (120, 110, 100))


def test_text_is_read_locally(monkeypatch):
    monkeypatch.setattr(W, "grab", _frame)
    page = "Kirchhoff's voltage law: the sum of voltages around any closed loop is zero. " * 2
    monkeypatch.setattr(screen, "ocr_image", lambda img: page)
    asked = {}

    async def answer(question, text, title):
        asked.update(question=question, text=text, title=title)
        return "It states Kirchhoff's voltage law.", []
    monkeypatch.setattr(screen, "answer", answer)
    reply, artifacts = run(W.handle("can you read what I'm holding?"))
    assert reply == "It states Kirchhoff's voltage law."
    assert asked["title"] == "Photo: webcam" and asked["text"] == page
    assert artifacts[0][0] == "VISUAL"
    assert screen._context["kind"] == "photo" and screen.context_fresh()     # follow-ups and "add it to my library"
    assert not policy.has_pending()


def test_just_taking_a_photo_with_text(monkeypatch):
    monkeypatch.setattr(W, "grab", _frame)
    monkeypatch.setattr(screen, "ocr_image", lambda img: "word " * 40)
    monkeypatch.setattr(screen, "answer", lambda *a: pytest.fail("no question was asked"))
    reply, _ = run(W.handle("take a photo with the webcam"))
    assert reply.startswith("Got it; the photo is on the screen. I can read about 40 words")


def test_picture_asks_before_the_cloud(monkeypatch, clean):
    monkeypatch.setattr(W, "grab", _frame)
    monkeypatch.setattr(screen, "ocr_image", lambda img: "")
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    reply, artifacts = run(W.handle("what am I holding?"))
    assert "send this one photo to the cloud" in reply and artifacts[0][0] == "VISUAL"
    pending = policy.take_pending()
    assert pending.tool == "screen_vision" and pending.args["title"] == "Photo: webcam"
    assert pending.args["question"] == "what am I holding?"
    assert any(a[0] == "webcam_photo" for a, k in clean)


def test_take_photo_cloud_question_is_a_description(monkeypatch):
    monkeypatch.setattr(W, "grab", _frame)
    monkeypatch.setattr(screen, "ocr_image", lambda img: "")
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    run(W.handle("take a photo with the webcam"))
    pending = policy.take_pending()
    assert pending.args["question"] == "Describe briefly what this photo shows."


def test_no_cloud_no_offer(monkeypatch):
    monkeypatch.setattr(W, "grab", _frame)
    monkeypatch.setattr(screen, "ocr_image", lambda img: "")
    monkeypatch.setattr(router, "cloud_available", lambda: False)
    reply, _ = run(W.handle("what do you see?"))
    assert "isn't set up" in reply and not policy.has_pending()


def test_cloud_prompt_says_webcam_photo(monkeypatch):
    from arya.knowledge import visuals
    from arya.study import handwriting
    image_id = visuals._store(b"png")
    sent = {}

    async def vision(png, prompt):
        sent["prompt"] = prompt
        return "A mug."
    monkeypatch.setattr(handwriting, "_vision_call", vision)
    assert run(screen.read_with_cloud({"image_id": image_id, "question": "what is it?", "title": "Photo: webcam"})) == "A mug."
    assert sent["prompt"].startswith("This is a photo the user just took with their laptop's webcam.")


def test_grab_waits_for_exposure(monkeypatch):
    """Frames get brighter as auto exposure settles; grab stops once brightness is steady."""
    import sys
    import types

    levels = [5, 15, 30, 45, 55, 60, 62, 62, 62, 62, 62, 62, 62, 62, 62, 62] + [62] * 60
    closed = []

    class Frame:
        def __init__(self, v):
            self.v = v

        def to_image(self):
            return Image.new("RGB", (8, 8), (self.v,) * 3)

    class Container:
        streams = types.SimpleNamespace(video=[object()])

        def decode(self, stream):
            return (Frame(v) for v in levels)

        def close(self):
            closed.append(True)
    fake_av = types.SimpleNamespace(open=lambda *a, **k: Container())
    monkeypatch.setitem(sys.modules, "av", fake_av)
    monkeypatch.setattr(W, "device_name", lambda: "Test Camera")
    img = W.grab()
    assert round(W._brightness(img)) == 62 and closed == [True]
