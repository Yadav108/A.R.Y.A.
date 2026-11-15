"""Phase B: file agent — recent files, space, tidying (asks first, never deletes) and undo.
Works on a throwaway folder inside tests/ (the sandbox refuses AppData, where tmp_path lives)."""
import asyncio
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

import arya.core.llm as llm
from arya.core import policy
from arya.tools import file_agent as F

NOW = time.mktime((2026, 11, 16, 15, 0, 0, 0, 0, -1))            # Monday 15:00
DAY = 24 * 3600


def _touch(folder: Path, name: str, age_s: float, size: int = 10) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (NOW - age_s, NOW - age_s))
    return p


@pytest.fixture
def home(monkeypatch):
    root = Path(__file__).parent / "_tmp_files" / uuid.uuid4().hex[:8]
    dl, desk, docs = root / "Downloads", root / "Desktop", root / "Documents"
    for d in (dl, desk, docs):
        d.mkdir(parents=True)
    _touch(dl, "lecture5_dynamics.pdf", 2 * 3600)                 # today 13:00
    _touch(dl, "holiday.jpg", DAY + 3600, size=5_000_000)          # yesterday 14:00
    _touch(dl, "control_slides.pptx", DAY + 7200)                  # yesterday 13:00
    _touch(dl, "setup.exe", 3 * DAY, size=90_000_000)
    _touch(dl, "data.zip", 4 * DAY, size=20_000_000)
    _touch(dl, "notes.txt", 5 * DAY)
    _touch(dl, "big_movie.mkv.crdownload", 60)                     # still downloading
    _touch(desk, "todo.md", 600)
    _touch(docs / "Uni" / "Dynamics", "sheet3.pdf", 3 * 3600)
    monkeypatch.setattr(F, "_folders", lambda: {"downloads": dl, "desktop": desk, "documents": docs})
    monkeypatch.setattr(F, "_undo_file", lambda: root / "tidy-undo.json")
    opened = []
    monkeypatch.setattr(os, "startfile", lambda p: opened.append(p), raising=False)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    policy.cancel_pending()
    yield {"dl": dl, "desk": desk, "docs": docs, "opened": opened}
    policy.cancel_pending()
    shutil.rmtree(root, ignore_errors=True)


def test_recent_downloads(home):
    reply = F.handle("what did I download today?", NOW)
    assert reply == "One file: lecture5_dynamics.pdf in Downloads, today at 13:00."
    reply = F.handle("what did I download recently", NOW)
    assert reply.startswith("Your newest files: lecture5_dynamics.pdf in Downloads, today at 13:00; holiday.jpg")
    assert "crdownload" not in reply


def test_find_by_kind_and_day(home):
    assert F.handle("find the slides I downloaded yesterday", NOW) == (
        "One slide deck: control_slides.pptx in Downloads, yesterday at 13:00.")
    assert F.handle("where are the pdfs I saved today", NOW).startswith("Your newest PDFs: lecture5_dynamics.pdf")


def test_open_last(home):
    assert F.handle("open the last PDF I downloaded", NOW) == ("Opening lecture5_dynamics.pdf in Downloads, "
                                                                "from today at 13:00.")
    assert home["opened"][0].endswith("lecture5_dynamics.pdf")
    assert F.handle("open the latest presentation", NOW).startswith("Opening control_slides.pptx")
    assert F.handle("open the last tab", NOW) is None


def test_space(home):
    reply = F.handle("what's taking up space in my downloads?", NOW)
    assert "free of" in reply and reply.endswith(
        "The biggest files in Downloads: setup.exe (85.8 MB), data.zip (19.1 MB), holiday.jpg (4.8 MB).")


def _say(m):
    async def turn():
        return "".join([s async for s in llm.stream_with_tools(m)])
    return asyncio.run(turn())


def test_tidy_asks_first_then_sorts_and_undoes(home, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    dl = home["dl"]
    reply = _say("tidy up my downloads")
    assert reply.startswith("Your Downloads has 5 loose files older than a day. Shall I sort them by type:")
    assert "2 into Documents" in reply and "Nothing gets deleted" in reply
    assert (dl / "setup.exe").exists() and policy.has_pending()                # nothing moved yet
    assert _say("yes") == "Done, I sorted 5 files in Downloads into folders by type."
    assert (dl / "Installers" / "setup.exe").exists() and (dl / "Images" / "holiday.jpg").exists()
    assert (dl / "Documents" / "notes.txt").exists() and (dl / "Archives" / "data.zip").exists()
    assert (dl / "lecture5_dynamics.pdf").exists() and (dl / "big_movie.mkv.crdownload").exists()   # today's: left
    assert _say("undo the tidy") == "Done, I moved 5 files back where they were."
    assert (dl / "setup.exe").exists() and not (dl / "Installers").exists()
    assert sorted(p.name for p in dl.iterdir()) == sorted(["lecture5_dynamics.pdf", "holiday.jpg",
                                                           "control_slides.pptx", "setup.exe", "data.zip",
                                                           "notes.txt", "big_movie.mkv.crdownload"])


def test_tidy_no_keeps_everything(home, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    _say("organise my downloads")
    assert _say("no") == "Okay, I left everything where it is."
    assert (home["dl"] / "setup.exe").exists()


def test_tidy_never_overwrites(home):
    dl = home["dl"]
    _touch(dl / "Documents", "notes.txt", DAY, size=3)
    F.apply_moves(F.tidy_plan(dl, NOW))
    assert (dl / "Documents" / "notes.txt").stat().st_size == 3
    assert (dl / "Documents" / "notes (2).txt").stat().st_size == 10


def test_tidy_when_already_tidy(home):
    assert F.handle("tidy my desktop", NOW) == "Your Desktop is already tidy: only 0 loose files older than a day."


@pytest.mark.parametrize("message", ["open the last exercise sheet", "what is free space in maths",
                                     "explain the Coriolis acceleration", "open spotify",
                                     "where is the exam", "find the derivative of x squared"])
def test_not_file_chores(home, message):
    assert F.handle(message, NOW) is None
