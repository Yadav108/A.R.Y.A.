"""C3: lecture recordings -> transcripts -> library, with the minute in every answer.
Uses a generated WAV file and a fake Whisper: nothing is decoded from your lectures, no GPU needed."""
import asyncio
import types
import wave
from pathlib import Path

import numpy as np
import pytest

from arya.knowledge import lectures as L


def _wav(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(int(seconds * L.SR)) / L.SR
    pcm = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(L.SR)
        w.writeframes(pcm.tobytes())
    return path


@pytest.fixture
def course(kb, tmp_path, monkeypatch):
    root = Path(kb["root"])
    rec = _wav(root / "Dynamics" / "Lecture 5 rigid bodies.wav", 290)          # 4:50 of "lecture"
    copy = root / "Control" / "Lecture 5 rigid bodies.wav"                       # same name + size = a copy
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(rec.read_bytes())
    monkeypatch.setattr(L, "STORE", tmp_path / "lectures")
    calls = []

    def fake_window(audio, language, prompt):
        calls.append((len(audio) / L.SR, prompt))
        n = len(calls)
        words = {3: "the Coriolis acceleration is two omega cross v relative",
                 9: "the parallel axis theorem adds m d squared"}.get(n, f"part {n} of the lecture")
        filler = " so let us look at this step by step and write it down on the board carefully"
        return [(1.0, 8.0, words + filler * 3)], "en"

    monkeypatch.setattr(L, "_transcribe_window", fake_window)
    import arya.knowledge.library as library
    monkeypatch.setattr(library, "default_roots", lambda: [root])
    yield {"root": root, "rec": rec, "copy": copy, "calls": calls}


def test_recordings_skip_copies(course):
    recs = L.recordings([course["root"]])
    assert len(recs) == 1


def test_transcribe_resume_and_pages(course):
    rec = course["rec"]
    stop_after = iter([True, True, True, False])
    assert L.transcribe(rec, keep_going=lambda: next(stop_after), roots=[course["root"]]) is False     # paused
    assert L.load(rec)["done_s"] == 90 and not L.transcript_ready(rec)
    assert L.transcribe(rec, roots=[course["root"]]) is True                                          # resumed
    assert [round(d) for d, _ in course["calls"]] == [30] * 9 + [20]       # 10 windows: never one twice
    assert course["calls"][0][1] == "Dynamics lecture: Lecture 5 rigid bodies."
    pages = L.pages(rec)
    assert [n for n, _ in pages] == [1, 2, 3]                               # two-minute blocks
    assert pages[0][1].startswith("[0:00] part 1") and pages[1][1].startswith("[2:00] part 5")
    assert "Coriolis" in pages[0][1] and "parallel axis" in pages[2][1]


def test_library_answers_with_the_minute(course):
    from arya.knowledge import library
    L.transcribe(course["rec"], roots=[course["root"]])
    library.index_file(course["rec"])
    hits = library.search("parallel axis theorem m d squared", k=3)
    hit = next(h for h in hits if h.kind == "wav")
    assert hit.label() == "Lecture 5 rigid bodies, at 4:00" and hit.collection == "Dynamics"
    library.index_file(course["rec"].with_suffix(".gone"))


def test_untitled_recordings_get_a_name(course):
    from arya.knowledge.library import _title_and_collection
    p = course["root"] / "Dynamics" / "CS1 Lecture Videos" / "_ (11).mp4"
    assert _title_and_collection(p, [course["root"]]) == ("CS1 Lecture Videos, recording 11", "Dynamics")


def test_voice(course):
    assert L.handle("how far are the lecture transcriptions?").startswith("0 of 1 lecture recordings are transcribed")
    assert L.handle("transcribe my lectures now").startswith("Starting on the 1 lecture recording")
    assert L._force.is_set()
    assert L.handle("pause transcribing").startswith("Paused the lecture transcription")
    assert L._stop.is_set() and not L._force.is_set()
    L._stop.clear()
    assert L.handle("explain the Coriolis acceleration") is None


def test_worker_waits_while_you_are_here(course, monkeypatch):
    monkeypatch.setattr(L, "_user_away", lambda: False)
    monkeypatch.setattr(L, "transcribe", lambda *a, **k: pytest.fail("must not transcribe while you're here"))

    async def go():
        sleeps = 0

        async def fake_sleep(s):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError
        monkeypatch.setattr(L.asyncio, "sleep", fake_sleep)
        try:
            await L.run(busy=lambda: False)
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
