"""Dialogue state (arya/core/dialogue.py) and finding a file by roughly its name (arya/tools/file_agent.py)."""
import asyncio
import time

import pytest

from arya.core import dialogue as D
from arya.tools import file_agent as F


@pytest.fixture(autouse=True)
def fresh():
    D.reset()
    yield
    D.reset()


def test_again_repeats_a_safe_action():
    D.record("media keys", "next song", "Next track.")
    for text in ("again", "Again.", "one more time", "do it again", "try again", "No, do that again"):
        assert D.resolve(text) == "next song", text


@pytest.mark.parametrize("text", ["it didn't open", "that didn't work", "No it haven't", "nothing happened",
                                  "it's still not open", "it did not work"])
def test_failed_repeats_the_last_action(text):
    D.record("browser", "open YouTube", "Opening YouTube.")
    assert D.resolve(text) == "open YouTube"


def test_failed_after_music_is_left_to_the_music_check():
    D.record("media keys", "resume the music", "Okay.")
    assert D.resolve("It did not.") is None          # llm._retry_after_complaint presses play AND checks


@pytest.mark.parametrize("route", ["alarms", "model", "tasks", "intercom"])
def test_unsafe_or_chat_turns_are_never_repeated(route):
    D.record("browser", "open YouTube", "Opening YouTube.")
    D.record(route, "set a timer for 10 minutes", "Timer set for 10 minutes.")
    assert D.resolve("again") is None and D.resolve("that didn't work") is None


def test_failed_action_is_not_remembered():
    D.record("browser", "switch to the Taal tab", "I couldn't find a tab matching Taal. Which tab?")
    assert D.resolve("again") is None


@pytest.mark.parametrize("text", ["it didn't rain today", "explain it again more simply", "say that again slowly",
                                  "I didn't know that", "again and again it fails in the exam"])
def test_not_references(text):
    D.record("media keys", "pause the music", "Paused.")
    assert D.resolve(text) is None


def test_play_it_again_uses_the_song():
    D.record("browser", "play Kon Tujhe by Kishore Kumar", "Playing Kon Tujhe by Kishore Kumar on YouTube Music.")
    D.record("media keys", "pause", "Paused.")
    assert D.resolve("play it again") == "play Kon Tujhe by Kishore Kumar"


def test_switch_to_it_uses_the_tab():
    D.record("browser", "switch to the dynamics tab", "Switched to “Dynamics Ex Solutions.pdf”.")
    assert D.resolve("switch to it") == "switch to the Dynamics Ex Solutions.pdf tab"
    D.reset()
    D.record("browser", "switch to the Gmail tab", "I searched your tabs …")
    assert D.resolve("go back to it") == "switch to the Gmail tab"


def test_bring_it_back_restores_the_window():
    D.record("window control", "minimize the Chrome window", "Minimized Chrome.")
    assert D.resolve("bring it back") == "restore the Chrome window"


def test_document_follow_ups():
    D.note_entity("document", "Nico's MCP phase 1 plan")
    assert D.resolve("It is in downloads.") == "find Nico's MCP phase 1 plan file in downloads"
    assert D.resolve("look in the desktop") == "find Nico's MCP phase 1 plan file in desktop"
    assert D.resolve("Find it in the Windows Explorer.") == "show Nico's MCP phase 1 plan file in explorer"
    assert D.resolve("open it") is None                      # not found yet: nothing to open
    D.note_entity("document", "Nico's MCP phase 1 plan", path="C:/x/NICOS_MCP_Phase1_Plan.docx")
    assert D.resolve("open it") == "open Nico's MCP phase 1 plan file"


def test_references_expire(monkeypatch):
    D.record("media keys", "next song", "Next.")
    real = time.monotonic
    monkeypatch.setattr(D.time, "monotonic", lambda: real() + D.WINDOW_S + 1)
    assert D.resolve("again") is None


def test_wrapper_resolves_and_records(monkeypatch):
    import arya.core.llm as llm
    seen = []

    async def pipeline(message):
        seen.append(message)
        llm._instant("browser")
        yield "Opening YouTube."
    monkeypatch.setattr(llm, "_turn_pipeline", pipeline)

    async def turn(text):
        return "".join([s async for s in llm.stream_with_tools(text)])
    asyncio.run(turn("open YouTube"))
    asyncio.run(turn("it didn't open"))
    assert seen == ["open YouTube", "open YouTube"]


# ── Finding a file by name ───────────────────────────────────────────────────

@pytest.mark.parametrize("query,name,ok", [
    ("Nico's MCP phase 1 plan", "NICOS_MCP_Phase1_Plan.docx", True),
    ("MCP phase one plan", "NICOS_MCP_Phase1_Plan.docx", True),
    ("dynamics exercise solutions", "Dynamics Ex Solutions.pdf", True),
    ("lab report", "Lab report draft.docx", True),
    ("thermodynamics notes", "NICOS_MCP_Phase1_Plan.docx", False),
])
def test_name_score(query, name, ok):
    assert (F.name_score(query, name) >= 0.6) is ok


@pytest.fixture
def folders(tmp_path, monkeypatch):
    f = {k: tmp_path / k for k in ("downloads", "desktop", "documents")}
    for p in f.values():
        p.mkdir()
    (f["downloads"] / "NICOS_MCP_Phase1_Plan.docx").write_text("x")
    (f["documents"] / "Uni").mkdir()
    (f["documents"] / "Uni" / "Lab report draft.docx").write_text("x")
    monkeypatch.setattr(F, "_folders", lambda: f)
    from arya.core import policy
    monkeypatch.setattr(policy, "check_path", lambda p, *a, **k: p)
    opened = []
    monkeypatch.setattr(F.os, "startfile", lambda p: opened.append(p))
    return opened


def test_find_by_name(folders):
    reply = F.handle("find Nico's MCP phase 1 plan")
    assert "NICOS_MCP_Phase1_Plan.docx in downloads" in reply
    assert D.entity("document")["path"].endswith("NICOS_MCP_Phase1_Plan.docx")
    assert "Lab report draft.docx" in F.handle("where is my lab report")
    assert "couldn't find" in F.handle("find the thesis draft document in desktop")


def test_open_and_explorer(folders):
    assert F.handle("open the lab report file").startswith("Opening Lab report draft.docx")
    assert folders[-1].endswith("Lab report draft.docx")
    assert "Opened the folder" in F.handle("show MCP phase one plan file in explorer")
    assert folders[-1].endswith("downloads")


@pytest.mark.parametrize("text", ["find the Coriolis formula", "open YouTube", "where is the Coriolis acceleration in "
                                  "my notes?", "show me the Gmail tab", "find exercise 113", "look at this",
                                  "open a new tab", "where is my phone"])
def test_not_file_requests(folders, text):
    assert F.handle(text) is None
