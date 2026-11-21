"""Bugs from the conversations of 2026-11-17/19 (arya.log + history)."""
import asyncio
import time

import pytest

import arya.core.llm as llm
from arya.core import addressee, hotword, quiet
from arya.tools import alarms, screen, webcam


# ── Side talk answered ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "This is a joke. This is a joke. This is a joke. This is a joke.",
    "I don't know, I don't know, but I don't know, I don't know, but I don't know, but I don't know.",
    "Yes. I know. Cheese. Cheese. Cheese. Cheese. Cheese. Cheese. Cheese. Cheese.",
])
def test_whisper_loops_are_not_for_arya(text):
    assert addressee.repetitive(text)
    assert addressee.quick_verdict(text) is False
    assert addressee.quick_verdict(text, pending_confirmation=True) is False


@pytest.mark.parametrize("text", ["Play the song again and again and again", "I know I know what you mean",
                                  "set a timer for 10 minutes"])
def test_normal_sentences_are_not_loops(text):
    assert not addressee.repetitive(text)


@pytest.mark.parametrize("text", ["Oh yeah.", "Uh, yeah.", "Yes.", "No."])
def test_bare_yes_no_is_judged_unless_arya_waits(text):
    assert addressee.quick_verdict(text) is None
    assert addressee.quick_verdict(text, pending_confirmation=True) is True


@pytest.mark.parametrize("text", ["How are you?", "Thanks. LOL.", "Thank you very much.", "Okay, go ahead."])
def test_still_for_arya(text):
    assert addressee.quick_verdict(text) is True


@pytest.mark.parametrize("reply,real", [
    ("How can I assist you today?", False),
    ("Sure, what's up? Need help with something specific or just chatting?", False),
    ("Long days ahead, Aryan. What brings you to me today?", False),
    ("What's on your mind?", False),
    ("Shall I play it on YouTube Music?", True),
    ("Do you want the Berlin or the Delhi time?", True),
    ("That's a joke.", False),
])
def test_generic_offers_do_not_open_an_answer_window(reply, real):
    assert addressee.asks_a_real_question(reply) is real


def test_after_a_real_question_a_bare_yes_is_for_arya():
    assert asyncio.run(addressee.is_addressed("Yes, please.", asked_question=True)) == (True, "answer to its question")


# ── "NJ" misheard: the user called ARYA, ARYA called the user Andrew ─────────

@pytest.mark.parametrize("text,rest", [
    ("Andrew, you're kidding me, it's okay.", "you're kidding me, it's okay."),
    ("Uh, Yonjai, brief me.", "brief me."),
    ("ENG, remember, I am your developer.", "remember, I am your developer."),
    ("Hey, GMT, do you have any questions?", "do you have any questions?"),
])
def test_more_misheard_nj(monkeypatch, text, rest):
    monkeypatch.setattr(hotword, "extra_models", lambda: ["hey_nj.npz"])
    assert addressee.split_name(text) == (rest, True)


def test_andrew_is_a_name_without_the_nj_model(monkeypatch):
    monkeypatch.setattr(hotword, "extra_models", lambda: [])
    assert addressee.split_name("Andrew, you're kidding me.")[1] is False


def test_prompt_says_nj_is_arya_and_no_generic_closers():
    prompt = llm._system_prompt()
    assert "also calls you NJ" in prompt and "Andrew" in prompt
    assert "How can I assist you today?" in prompt          # named as the thing not to say


@pytest.mark.parametrize("hour,greeting", [(8, "Good morning"), (16, "Good afternoon"), (20, "Good evening"),
                                           (23, "Hi, still up"), (2, "Hi, still up")])
def test_briefing_greeting_fits_the_hour(hour, greeting):
    assert llm.briefing_greeting(hour) == greeting


# ── "Delete it." after a webcam photo -> delete_file('Downloads/old_setup.exe') ──

def test_file_the_user_never_named_is_not_touched():
    said = "Delete it. Got the photo; it's on the screen."
    assert llm.unnamed_file_target("delete_file", {"path": "Downloads/old_setup.exe"}, said) == "old_setup.exe"
    assert llm.unnamed_file_target("delete_file", {"path": "Downloads/old_setup.exe"},
                                   "delete old setup from downloads") is None
    assert llm.unnamed_file_target("move_file", {"source": "Desktop/report.pdf", "destination": "Documents"},
                                   "move the report to documents") is None
    assert llm.unnamed_file_target("open_app", {"name": "x"}, "") is None


def test_file_named_by_an_earlier_tool_result_is_fine(monkeypatch):
    monkeypatch.setattr(llm, "_recent_tool_text", ["Downloads: lecture_07.pdf, old_setup.exe"])
    assert llm.unnamed_file_target("delete_file", {"path": "Downloads/old_setup.exe"}, "delete the second one") is None


def test_blocked_unnamed_file_does_not_ask_for_confirmation():
    async def run():
        llm._turn_text.set("Delete it.")
        return await llm._execute_tool("delete_file", {"path": "Downloads/old_setup.exe"})
    assert "never mentioned" in asyncio.run(run())


def test_delete_it_after_a_photo_drops_the_photo():
    from arya.knowledge import visuals
    image_id = visuals._store(b"png")
    screen._context.update(text="", title=webcam.TITLE, image_id=image_id, until=time.monotonic() + 60, kind="photo")
    assert webcam.matches("Delete it.") and webcam.matches("delete the photo")
    reply, artifacts = asyncio.run(webcam.handle("Delete it."))
    assert "gone" in reply and image_id not in visuals._images
    assert not webcam.matches("Delete it.")                 # no photo any more: a normal request again


def test_delete_it_without_a_photo_is_not_the_webcam():
    screen._context.update(text="", image_id="", until=0.0, kind="screen")
    assert not webcam.matches("Delete it.")


@pytest.mark.parametrize("text,hit", [
    ("Can you take a picture of that?", True),
    ("please take a photo", True),
    ("take a photo", False),                                  # test_webcam: deliberately not enough
    ("I'll take a picture later", False),
    ("take a screenshot", False),
    ("take a picture of the screen", False),
])
def test_take_a_picture_uses_the_webcam(text, hit):
    assert webcam.matches(text) is hit


# ── Reminders and do-not-disturb ─────────────────────────────────────────────

def test_dangling_at_is_dropped_from_the_reminder():
    assert alarms._what_in("ok set a reminder to call mom at", "reminder") == "call mom"
    assert alarms._what_in("remind me to call mom", "reminder") == "call mom"


@pytest.mark.parametrize("text", ["Remainders?", "Reminders?", "my reminders", "timers"])
def test_one_word_reminder_question(text):
    assert alarms._STATUS.search(text)


@pytest.mark.parametrize("text", ["do not disturb mode.", "Do not disturb mode for an hour", "dnd",
                                  "do not disturb mode off"])
def test_do_not_disturb_mode_is_meeting_mode(text):
    assert quiet.matches(text)
