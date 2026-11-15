"""C3: a photo of your handwritten solution, checked — asking before the cloud, and the verdict from the
answer sheet, not from the cloud model. The vision call and the exercise lookup are faked."""
import asyncio
import types

import pytest

import arya.core.llm as llm
from arya.core import policy, router
from arya.study import practice, solution_check as S, tutor
from arya.tools import screen

EX = types.SimpleNamespace(ref="2-2", statement="A car accelerates from rest... Determine the acceleration a.",
                           results=[tutor.Result("a", 8.93, "m/s2")], sources=[])


@pytest.fixture
def photo(kb, monkeypatch):
    sent = {}

    async def fake_vision(png, prompt=""):
        sent["png"], sent["prompt"] = png, prompt
        return sent.get("reply", "Your approach with v squared equals 2 a s is right, but in the second line you used "
                                 "the wrong distance.\nFINAL: a = 7.1 m/s2")

    from arya.knowledge import visuals
    from arya.study import handwriting
    monkeypatch.setattr(handwriting, "_vision_call", fake_vision)
    monkeypatch.setattr(tutor, "load_exercise", lambda q: EX if "2-2" in q else None)
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    image_id = visuals._store(b"\x89PNG my handwriting")
    screen._context.update(text="a = ...", title="Photo: page.jpg", image_id=image_id,
                           until=__import__("time").monotonic() + 60, kind="photo")
    with practice._db() as c:
        c.execute("DELETE FROM reviews")
        c.execute("DELETE FROM cards")
        practice._add(c, kind="exercise", course="Dynamics", key="exercise:2-2:a", question="q",
                      answer="The answer sheet gives a = 8.93 m/s2.", name="a", value=8.93, unit="m/s2")
    policy.cancel_pending()
    yield sent
    policy.cancel_pending()
    screen._context.update(text="", until=0.0, kind="screen")


def _say(m):
    async def turn():
        return "".join([s async for s in llm.stream_with_tools(m)])
    return asyncio.run(turn())


def test_asks_first_then_checks_with_the_answer_sheet(photo):
    offer = _say("check my solution for exercise 2-2")
    assert offer.startswith("To read your handwriting I'd send this one photo to the cloud, together with exercise "
                            "2-2's task text and its official result.")
    assert "png" not in photo and policy.has_pending()                     # nothing sent yet
    reply = _say("yes")
    assert photo["png"] == b"\x89PNG my handwriting"
    assert "Exercise 2-2" in photo["prompt"] and "a = 8.93 m/s2" in photo["prompt"]
    assert reply.startswith("Your final result, a = 7.1 m/s2: Not quite")          # ARYA's own check, not the cloud's
    assert "wrong distance" in reply and "FINAL" not in reply
    with practice._db() as c:
        assert c.execute("SELECT quality FROM reviews").fetchone()[0] == 1


def test_correct_final_result(photo):
    photo["reply"] = "Everything is right.\nFINAL: a = 8.93 m/s2"
    _say("is my solution for exercise 2-2 correct?")
    assert _say("yes").startswith("Your final result, a = 8.93 m/s2: Correct")


def test_no_keeps_the_photo_local(photo):
    _say("check my solution for exercise 2-2")
    assert _say("no").startswith("Okay, your photo stays on the laptop.")
    assert "png" not in photo


def test_needs_a_photo_and_an_exercise(photo, monkeypatch):
    monkeypatch.setattr(S, "_which_exercise", lambda m: None)
    assert S.handle("check my solution") == "Which exercise is it? Say for example \"check my solution for exercise 2-2\"."
    screen._context.update(until=0.0)
    assert S.handle("check my handwritten solution").startswith("Drop or paste a photo of your solution")
    assert S.handle("check my answer: a = 8.9 m/s2") is None                 # the tutor's number check instead


def test_without_cloud(photo, monkeypatch):
    monkeypatch.setattr(router, "cloud_available", lambda: False)
    assert S.handle("check my solution for exercise 2-2").startswith("Reading handwriting needs the cloud")
