"""'Use visuals' / 'where is the diagram?' about an exercise -> its figure, cropped from the sheet."""
import asyncio
import time

import pytest

import arya.core.llm as llm
from arya.study import figures as F


@pytest.fixture
def sheet(kb, monkeypatch):
    """A printed exercise sheet (PDF with a text layer and a drawn figure) for exercise 4-2."""
    import pymupdf
    from arya.knowledge import library

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 80), "4-1) A ball is thrown upwards with 12 m/s. Determine the maximum height.", fontsize=11)
    page.insert_text((60, 160), "4-2) A pendulum of length L = 0.5 m swings from rest at 30 degrees.", fontsize=11)
    page.insert_text((60, 175), "Determine its speed at the lowest point.", fontsize=11)
    page.draw_line((300, 200), (360, 300))                     # the "figure"
    page.draw_circle((360, 300), 8)
    page.insert_text((60, 420), "4-3) A spring is compressed by 3 cm. Determine the stored energy.", fontsize=11)
    path = kb["root"] / "Dynamics" / "Sheet 4.pdf"
    doc.save(path)
    library.index_file(path)

    async def fake_complete(model, messages, **kw):
        import types
        return types.SimpleNamespace(content="The figure shows the pendulum at 30 degrees, starting from rest.",
                                     tool_calls=None)

    monkeypatch.setattr(llm, "_complete", fake_complete)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    monkeypatch.setitem(llm._last_exercise, "until", 0.0)
    shown = []
    llm.set_artifact_sink(lambda kind, payload: shown.append((kind, payload)))
    yield shown
    path.unlink(missing_ok=True)
    library.index_file(path)


def _say(m):
    async def turn():
        return [s async for s in llm.stream_with_tools(m)]
    return asyncio.run(turn())


def test_crop_holds_only_that_exercise(sheet):
    from arya.knowledge import library
    import pymupdf

    p = next(x for x in library.find_exercise(["4-2"], "exercise 4-2") if x.title == "Sheet 4")
    png = F.crop_exercise(p.doc_id, p.page, "4-2")
    pix = pymupdf.Pixmap(png)
    assert pix.height < pix.width                    # a strip of the page, not the whole page
    assert pix.height > 100                          # but it includes the drawn figure below the text


def test_use_visuals_shows_the_exercise_figure(sheet):
    said = _say("Could you use visuals to explain exercise 4-2 better?")
    kinds = [k for k, _ in sheet]
    assert "VISUAL" in kinds and "SOURCES" in kinds
    visual = next(p for k, p in sheet if k == "VISUAL")
    assert visual["type"] == "image" and visual["caption"] == "Exercise 4-2: Sheet 4, p. 1"
    text = "".join(said)
    assert text.startswith("Here's exercise 4-2 with its figure on screen, from Sheet 4, page 1.")
    assert "starting from rest" in text


def test_where_is_the_diagram_follows_the_conversation(sheet):
    llm._last_exercise.update(question="explain exercise 4-2", until=time.monotonic() + 60)
    _say("where is the diagram?")
    assert any(k == "VISUAL" for k, _ in sheet)


def test_own_diagrams_are_not_hijacked(sheet):
    llm._last_exercise.update(question="explain exercise 4-2", until=time.monotonic() + 60)
    assert F.figure_for("Draw a block diagram of a temperature control loop with a heater and a sensor") is None
    assert F.figure_for("plot sin x") is None
    assert F.figure_for("what time is it") is None


def test_no_exercise_in_focus_means_no_figure(sheet):
    assert F.figure_for("can you show me a picture?") is None


@pytest.mark.parametrize("text, parts", [
    ("It is on Exercises, p. 3. The figure shows it.", ["It is on Exercises, p. 3.", " The figure shows it."]),
    ("See Fig. 2 for that. Next one.", ["See Fig. 2 for that.", " Next one."]),
    ("Use e.g. the energy method. Then stop.", ["Use e.g. the energy method.", " Then stop."]),
])
def test_abbreviations_do_not_end_sentences(text, parts):
    assert llm.split_sentences(text) == parts


@pytest.mark.parametrize("raw, clean", [
    ("two arms of the robot.### Visual Explanation", "two arms of the robot. Visual Explanation"),
    ("starts from rest, so ω₂ = 0 rad/s.2.  Angular Displacement", "starts from rest, so ω₂ = 0 rad/s. Angular Displacement"),
])
def test_glued_markdown_is_removed(raw, clean):
    assert llm.plain_text(raw) == clean
