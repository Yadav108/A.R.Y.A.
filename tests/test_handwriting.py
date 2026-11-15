"""M2 part 2: handwritten solution pages read by the cloud vision model — only after asking.
The cloud call is faked: these tests never send anything."""
import asyncio

import pytest

import arya.core.llm as llm
from arya.core import policy, router
from arya.study import handwriting as H


@pytest.fixture
def scanned_exercise(kb, tmp_path, monkeypatch):
    """A scanned (image-only) PDF holding exercise 2-7, added to the test library and removed afterwards."""
    import pymupdf
    from arya.knowledge import library

    text_doc = pymupdf.open()
    page = text_doc.new_page()
    page.insert_text((72, 100), "2-7) A wheel rolls without slipping on a horizontal plane.", fontsize=14)
    page.insert_text((72, 125), "Determine the velocity of its top point.", fontsize=14)
    scan = pymupdf.open()
    scan.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect,
                                                                                pixmap=page.get_pixmap(dpi=200))
    path = kb["root"] / "Dynamics" / "Solutions scan.pdf"
    scan.save(path)
    if not library.ocr_available():
        pytest.skip("Windows OCR not available")
    library.index_file(path)
    sent = []

    async def fake_vision(png):
        sent.append(len(png))
        return ("The top point moves at twice the centre's speed. Transcription:\nv_top = 2 v_C\n"
                "ω = v_C / r")

    monkeypatch.setattr(H, "_vision_call", fake_vision)
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    llm.set_artifact_sink(lambda kind, payload: None)
    policy.cancel_pending()
    library.TRANSCRIPTIONS.unlink(missing_ok=True)
    yield {"sent": sent, "library": library}
    policy.cancel_pending()
    path.unlink(missing_ok=True)
    library.index_file(path)
    library.TRANSCRIPTIONS.unlink(missing_ok=True)
    library.index_file(library.TRANSCRIPTIONS)


def _say(m):
    async def turn():
        return [s async for s in llm.stream_with_tools(m)]
    return asyncio.run(turn())


def test_finds_the_scanned_page_only_when_asked_about_handwriting(scanned_exercise):
    found = H.find_scan("Explain my handwritten solution of exercise 2-7")
    assert found and found[1] == "2-7" and found[0].title == "Solutions scan"
    assert H.find_scan("explain exercise 2-7") is None
    assert H.find_scan("Explain my handwritten solution of exercise 1-13") is None     # no scan for 1-13 here


def test_asks_first_then_yes_sends_one_page_and_keeps_the_transcription(scanned_exercise):
    said = _say("Explain my handwritten solution of exercise 2-7")
    assert len(said) == 1 and "sending that one page image to the cloud" in said[0]
    assert scanned_exercise["sent"] == [] and policy.has_pending()           # nothing sent before "yes"
    said = _say("yes")
    assert len(scanned_exercise["sent"]) == 1
    assert said[0].startswith("The top point moves at twice") and any("v_top = 2 v_C" in s for s in said)
    lib = scanned_exercise["library"]
    saved = lib.TRANSCRIPTIONS.read_text(encoding="utf-8")
    assert "## Exercise 2-7 — Solutions scan, p. 1" in saved and "v_top = 2 v_C" in saved
    hits = lib.search("top point of a rolling wheel twice the centre speed", k=8)   # the scan itself also matches
    assert any(h.title == "Handwritten solutions" and "v_top" in h.text for h in hits)


def test_no_keeps_everything_local(scanned_exercise, monkeypatch):
    async def local(m):
        yield "Local explanation."
    monkeypatch.setattr(llm, "_stream_with_tools", local)
    _say("Explain my handwritten solution of exercise 2-7")
    said = _say("no")
    assert said == ["Okay, nothing leaves the laptop. Here's the exercise from its printed task.", "Local explanation."]
    assert scanned_exercise["sent"] == []


def test_no_offer_without_a_cloud_key(scanned_exercise, monkeypatch):
    monkeypatch.setattr(router, "cloud_available", lambda: False)
    assert H.offer("Explain my handwritten solution of exercise 2-7") is None


def test_reading_the_same_page_again_replaces_the_old_transcription(scanned_exercise):
    H._save("2-7", "Solutions scan, p. 1", "first reading")
    H._save("2-7", "Solutions scan, p. 1", "second reading")
    saved = scanned_exercise["library"].TRANSCRIPTIONS.read_text(encoding="utf-8")
    assert saved.count("## Exercise 2-7") == 1 and "second reading" in saved and "first reading" not in saved


# ── Follow-ups and reuse of a transcription ──────────────────────────────────

def test_saved_transcription_is_used_and_not_sent_again(scanned_exercise):
    H._save("2-7", "Solutions scan, p. 1", "Explanation. Transcription:\nv_top = 2 v_C")
    lib = scanned_exercise["library"]
    found = lib.find_exercise(["2-7"], "exercise 2-7")
    assert any(p.title == "Handwritten solutions" and "v_top = 2 v_C" in p.text for p in found)
    assert H.offer("Explain my handwritten solution of exercise 2-7") is None      # answered locally now
    assert scanned_exercise["sent"] == []


def test_follow_up_stays_on_the_exercise(kb, monkeypatch):
    import arya.core.llm as llm
    llm.set_artifact_sink(lambda kind, payload: None)
    monkeypatch.setitem(llm._last_exercise, "until", 0.0)
    first = asyncio.run(llm.knowledge_for("explain exercise 1-13"))
    assert first and first[0].note == "exercise 1-13"
    again = asyncio.run(llm.knowledge_for("continue, explain it for a beginner"))
    assert again and all(p.note == "exercise 1-13" for p in again)
    assert asyncio.run(llm.knowledge_for("what is the weather like")) == []         # not a follow-up


def test_detailed_exercise_walkthrough_offers_the_cloud(kb, monkeypatch):
    import arya.core.llm as llm
    monkeypatch.setattr(llm, "ARYA_MODE", "offline")
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    monkeypatch.setattr(router, "ENABLED", True)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    llm.set_artifact_sink(lambda kind, payload: None)
    policy.cancel_pending()
    monkeypatch.setitem(llm._last_exercise, "question", "explain exercise 1-13")
    monkeypatch.setitem(llm._last_exercise, "until", __import__("time").monotonic() + 60)
    said = _say("continue explaining, in detail for a beginner")
    assert "ask the cloud" in said[0] and policy.has_pending()
    assert policy._state.pending.args["question"].endswith("(exercise 1-13)")
    assert "industrial robot" in policy._state.pending.args["context"]
    policy.cancel_pending()
