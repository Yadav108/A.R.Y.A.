"""M1: asking before using the cloud for hard questions, and learning from corrections."""
import asyncio

import pytest

import arya.core.llm as llm
from arya.core import policy, router


@pytest.mark.parametrize("message, reason", [
    ("Derive the step response of a second-order system", "derivation"),
    ("Prove that the poles of a stable system lie in the left half plane", "derivation"),
    ("A cyclist of 90 kg rides with 150 W up a 5 % slope with efficiency 0.95, find the speed", "problem"),
    ("Solve exercise 1-13 step by step", "worked"),
    ("Walk me through exercise 3-14", "worked"),
    ("What is the Coriolis acceleration?", None),
    ("What is 17 times 23?", None),
    ("Why does a system overshoot?", None),
    ("explain exercise 113", None),
    ("Calculate 15 percent of 240", None),
])
def test_hard_question_detection(message, reason):
    assert router.hard_reason(message) == reason


@pytest.fixture
def offline_with_key(monkeypatch):
    monkeypatch.setattr(llm, "ARYA_MODE", "offline")
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    monkeypatch.setattr(router, "ENABLED", True)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])

    async def no_kb(*a, **k):
        return []
    monkeypatch.setattr(llm, "knowledge_for", no_kb)
    calls = {}

    async def fake_cloud(question, context="", model=None):
        calls["cloud"] = (question, context, model)
        yield "From the cloud: the result is \\(\\zeta\\)."

    async def fake_local(message):
        calls["local"] = message
        yield "Local answer."

    monkeypatch.setattr(llm, "_stream_cloud_question", fake_cloud)
    monkeypatch.setattr(llm, "_stream_with_tools", fake_local)
    policy.cancel_pending()
    yield calls
    policy.cancel_pending()


def _say(message):
    async def turn():
        return [s async for s in llm.stream_with_tools(message)]
    return asyncio.run(turn())


def test_hard_question_asks_first_then_yes_goes_to_the_cloud(offline_with_key):
    q = "Derive the step response of a second-order system"
    said = _say(q)
    assert len(said) == 1 and "ask the cloud" in said[0] and "Only your question and the course page" in said[0]
    assert policy.has_pending() and "cloud" not in offline_with_key and "local" not in offline_with_key
    said = _say("yes")
    question, context, model = offline_with_key["cloud"]
    assert question == q and model == llm.MODEL_NAME
    assert said == ["From the cloud: the result is ζ."]           # cleaned like every other reply


def test_no_means_a_local_answer(offline_with_key):
    _say("Derive the step response of a second-order system")
    said = _say("no")
    assert said == ["Okay, I'll answer it myself.", "Local answer."] and "cloud" not in offline_with_key


def test_no_offer_for_ordinary_questions_or_in_cloud_mode(offline_with_key, monkeypatch):
    assert _say("Why does a system overshoot?") == ["Local answer."] and not policy.has_pending()
    monkeypatch.setattr(llm, "ARYA_MODE", "cloud")
    assert _say("Derive the step response of a second-order system") == ["Local answer."]
    monkeypatch.setattr(llm, "ARYA_MODE", "offline")
    monkeypatch.setattr(router, "cloud_available", lambda: False)        # no Groq key
    assert _say("Derive the step response of a second-order system") == ["Local answer."]


# ── Corrections memory ───────────────────────────────────────────────────────

@pytest.fixture
def fresh_corrections(kb):
    from arya.knowledge import corrections, library

    library.CORRECTIONS.unlink(missing_ok=True)
    library.index_file(library.CORRECTIONS)
    corrections._awaiting.update(until=0.0)
    yield corrections, library
    library.CORRECTIONS.unlink(missing_ok=True)          # don't leak into other tests' searches
    library.index_file(library.CORRECTIONS)


@pytest.mark.parametrize("message, fact", [
    ("That's wrong — ζ = 0 is undamped, not critically damped.", "ζ = 0 is undamped, not critically damped"),
    ("No, that's not right. It's 2.55 per second squared.", "2.55 per second squared"),
    ("Correction: the settling time is 4/(ζωₙ)", "the settling time is 4/(ζωₙ)"),
    ("That's wrong.", ""),
    ("It's wrong to cheat?", None),
    ("What is wrong with my code?", None),
])
def test_detect_correction(message, fact):
    from arya.knowledge import corrections

    assert corrections.detect(message) == fact


def test_correction_is_saved_and_outranks_everything(fresh_corrections):
    corrections, library = fresh_corrections
    reply = corrections.handle("That's wrong — the percent overshoot depends only on the damping ratio zeta.",
                               "What does overshoot depend on?", "It depends on the natural frequency.")
    assert reply.startswith("Thanks, noted as correction 1")
    text = library.CORRECTIONS.read_text(encoding="utf-8")
    assert "Question: What does overshoot depend on?" in text and "Not: It depends on the natural frequency." in text
    hits = library.relevant("what does the percent overshoot depend on?")
    assert hits and hits[0].title == "Your corrections"
    g = llm.grounding_prompt(hits, "what does the percent overshoot depend on?")
    assert "they override everything else" in g


def test_bare_thats_wrong_asks_then_remembers(fresh_corrections):
    corrections, library = fresh_corrections
    assert "What's the correct version?" in corrections.handle("That's wrong.", "q", "a")
    assert corrections.handle("Critical damping is zeta equal to one.", "", "") == "Got it, I'll remember that as correction 1."
    assert "Correct: Critical damping is zeta equal to one." in library.CORRECTIONS.read_text(encoding="utf-8")


def test_never_mind_cancels(fresh_corrections):
    corrections, library = fresh_corrections
    corrections.handle("That's wrong.", "q", "a")
    assert corrections.handle("never mind", "", "") == "Okay, nothing saved."
    assert not library.CORRECTIONS.exists()


def test_list_and_forget(fresh_corrections):
    corrections, library = fresh_corrections
    assert "haven't corrected me" in corrections.handle("What have I corrected?", "", "")
    corrections.handle("That's wrong — ζ = 1 is critically damped.", "q", "a")
    corrections.handle("That's wrong — ω_d = ωₙ·√(1 − ζ²).", "q", "a")
    listing = corrections.handle("show my corrections", "", "")
    assert "number 1: ζ = 1 is critically damped" in listing and "number 2" in listing
    assert corrections.handle("forget correction 1", "", "") == "Correction 1 is forgotten."
    assert "critically damped" not in library.CORRECTIONS.read_text(encoding="utf-8")
    assert corrections.handle("forget correction 7", "", "") == "I don't have a correction number 7."


def test_correction_needs_a_previous_answer(fresh_corrections):
    corrections, _ = fresh_corrections
    assert corrections.handle("That's wrong — something.", "", "") is None
