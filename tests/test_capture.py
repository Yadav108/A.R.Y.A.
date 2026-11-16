"""Quick capture and voice notes: sorting rules, the inbox, the voice-note recorder and the phone's voice notes.
A scratch database, a fake microphone and fake Whisper — no audio hardware or model needed."""
import asyncio
import time

import numpy as np
import pytest

from arya.core import capture as C
from arya.tools import tasks


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    db = tmp_path / "arya.db"
    monkeypatch.setattr(C, "DB_OVERRIDE", db)
    monkeypatch.setattr(tasks, "DB_OVERRIDE", db)
    saved = []

    async def fake_save(content, tag="general"):
        saved.append((content, tag))
        return "Saved."
    import arya.tools.notes as notes
    monkeypatch.setattr(notes, "save_note", fake_save)
    from arya.core import modes
    monkeypatch.setattr(modes, "current_name", lambda: "default")
    from arya.knowledge import library
    monkeypatch.setattr(library, "index_file", lambda path: None)
    from arya.core import attachments
    monkeypatch.setattr(attachments, "uploads_dir", lambda: tmp_path / "uploads")
    monkeypatch.setattr(C, "_engine", None)
    return saved


def run(coro):
    return asyncio.run(coro)


# ── Sorting ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, mode, kind", [
    ("buy resistors", "default", "todo-personal"),
    ("call the dentist on Monday", "default", "todo-personal"),
    ("email my supervisor the measurement data", "default", "todo-work"),
    ("buy resistors", "work", "todo-work"),
    ("I need to finish the thesis chapter", "default", "todo-work"),
    ("don't forget to water the plants", "default", "todo-personal"),
    ("remember to tell Ben my idea", "default", "todo-personal"),
    ("what if the gripper had suction cups", "default", "idea"),
    ("idea: a study timer that dims the screen", "default", "idea"),
    ("a podcast about lab mishaps would be fun", "default", "idea"),
    ("the café on Bahnhofstraße has good wifi", "default", "note"),
    ("Ben's birthday is 3 March", "default", "note"),
])
def test_sort(text, mode, kind):
    assert C.sort(text, mode) == kind


def test_clean_todo():
    assert C._clean_todo("remember to call the dentist") == "call the dentist"
    assert C._clean_todo("I need to Buy milk") == "Buy milk" or C._clean_todo("I need to Buy milk") == "buy milk"
    assert C._clean_todo("PR review for Ben") == "PR review for Ben"


# ── Matching what you say ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "note: buy resistors", "capture: ask Ben about the battery data", "jot down what if the gripper had suction",
    "Arya, remember to call the dentist", "don't forget to renew the visa", "what's in my inbox?",
    "what did I capture today?", "record a voice note", "Hey ARYA, start recording", "stop recording",
    "save the voice note", "take a voice memo please", "add to inbox: lab key is in drawer 2",
])
def test_matches(text):
    assert C.matches(text)


@pytest.mark.parametrize("text", [
    "capture the screen", "capture a screenshot of this window", "take a note that I'm tired",
    "what do my notes say about torque?", "remember that I like tea", "remind me to call mom at 5",
    "record the lecture", "what's a good note-taking app?", "stop the music", "start the timer",
    "how do I record my screen?", "notes on chapter 3 please",
])
def test_not_matches(text):
    assert not C.matches(text)


# ── Filing ────────────────────────────────────────────────────────────────────

def test_capture_files_a_todo_and_lists_it(scratch):
    reply = run(C.handle("note: buy resistors"))
    assert "personal to-dos" in reply
    assert [r["text"] for r in tasks.open_tasks("personal")] == ["buy resistors"]
    shown = run(C.handle("what's in my inbox?"))
    assert "buy resistors (to-do)" in shown


def test_capture_files_an_idea_and_a_note(scratch):
    assert "ideas" in run(C.handle("jot down: what if the gripper had suction"))
    assert [r["text"] for r in tasks.open_tasks("ideas")] == ["what if the gripper had suction"]
    assert "notes" in run(C.handle("capture: the café on Bahnhofstraße has good wifi"))
    assert scratch == [("the café on Bahnhofstraße has good wifi", "inbox")]


def test_one_word_capture_is_left_alone():
    assert run(C.handle("note: hello")) is None


def test_remember_to_from_the_phone(scratch):
    reply = run(C.handle("remember to email my supervisor the report", remote=True))
    assert "work to-dos" in reply
    assert C.captured_since(0)[0]["source"] == "phone"


def test_inbox_empty():
    assert "Nothing captured today" in run(C.handle("what did I capture today?"))


def test_briefing_line(scratch):
    assert C.briefing_line() == ""
    run(C.handle("note: buy resistors"))
    assert C.briefing_line().startswith("Captured since yesterday (1): buy resistors")


# ── Voice notes ───────────────────────────────────────────────────────────────

class FakeMic:
    def __init__(self, seconds=3.0):
        self.on, self.seconds = False, seconds

    def start_note(self):
        self.on = True

    def recording_note(self):
        return self.on

    def stop_note(self):
        self.on = False
        return np.zeros(int(16000 * self.seconds), dtype=np.float32)


def test_voice_note_short_is_filed_like_a_capture(monkeypatch, scratch):
    mic = FakeMic()
    C.set_engine(mic)
    monkeypatch.setattr(C, "_transcribe", lambda audio: "Buy resistors and a soldering tip. Hey Arya, stop recording.")

    async def go():
        started = await C.handle("record a voice note")
        assert started.startswith("Recording")
        assert C.recording()
        assert "already recording" in await C.handle("start recording")
        return await C.handle("stop recording")
    reply = run(go())
    assert not C.recording()
    assert "personal to-dos" in reply
    assert tasks.open_tasks("personal")[0]["text"] == "buy resistors and a soldering tip"


def test_voice_note_long_is_kept_as_a_note_and_in_the_library(monkeypatch, scratch, tmp_path):
    C.set_engine(FakeMic(seconds=90))
    words = " ".join(["the gripper slips when the battery is cold"] * 6)
    monkeypatch.setattr(C, "_transcribe", lambda audio: words)
    C._started["at"] = time.monotonic() - 90

    reply = run(C.stop_and_file("voice note"))
    assert reply.startswith("Voice note saved (1 min")
    assert scratch == [(words, "voice note")]
    files = list((tmp_path / "uploads" / "voice-notes").glob("*.md"))
    assert len(files) == 1 and "gripper slips" in files[0].read_text(encoding="utf-8")
    assert C.captured_since(0)[0]["filed_as"] == "voice note"


def test_stop_when_not_recording():
    C.set_engine(FakeMic())
    assert run(C.handle("stop recording")) == "I'm not recording anything."
    assert run(C.handle("stop recording", remote=True)) is None


def test_start_without_a_microphone_and_from_the_phone():
    assert "microphone" in run(C.handle("record a voice note"))
    assert "🎙" in run(C.handle("record a voice note", remote=True))


def test_empty_voice_note(monkeypatch):
    mic = FakeMic(seconds=0.1)
    C.set_engine(mic)
    assert run(C.stop_and_file("voice note")) == "That voice note was empty."
    monkeypatch.setattr(C, "_transcribe", lambda audio: "Stop recording.")
    assert run(C.finish_note("Stop recording.", 4, "phone")) == "I didn't catch anything in that voice note."


def test_ten_minute_limit_stops_and_announces(monkeypatch, scratch):
    C.set_engine(FakeMic())
    monkeypatch.setattr(C, "_transcribe", lambda audio: "buy milk on the way home")
    said = []

    async def say(text):
        said.append(text)
    C.set_announcer(say)

    async def go():
        await C.handle("record a voice note")
        await C._stop_after(0)
    run(go())
    assert said and said[0].startswith("Ten minutes is the limit") and "to-dos" in said[0]
    C.set_announcer(None)


# ── The recorder inside the wake-word engine ─────────────────────────────────

def test_hotword_note_buffer_trims_the_stop_command():
    from arya.core.hotword import HotwordEngine
    eng = HotwordEngine.__new__(HotwordEngine)
    assert not eng.recording_note()
    eng.start_note()
    assert eng.recording_note()
    eng._note["chunks"] = [np.full(1280, i, dtype=np.float32) for i in range(100)]
    eng._note["wake_at"], eng._note["wake_time"] = 80, time.monotonic()
    audio = eng.stop_note()
    assert not eng.recording_note()
    assert audio.size == 80 * 1280 and audio[-1] == 79
    eng.start_note()
    assert eng.stop_note() is None


# ── A voice note from the phone (🎙) ─────────────────────────────────────────

def test_phone_voice_note_is_filed(monkeypatch, scratch, tmp_path):
    from arya.core import attachments as A
    events = []
    monkeypatch.setattr(A, "_events", events.append)
    monkeypatch.setattr(A, "_transcript_text", lambda media, duration, u: "[00:00] Remember to buy\n[00:03] resistors.")
    media = tmp_path / "note.m4a"
    media.write_bytes(b"x")
    u = A.Upload("recording", "note", status="transcribing")
    run(A._transcribe_upload(u, media, "note.m4a", tmp_path, 5.0, as_note=True))
    assert u.status == "filed"
    assert any(e.startswith("NOTIFY:🎙 Voice note (5 s) filed in your personal to-dos") for e in events)
    assert tasks.open_tasks("personal")[0]["text"] == "buy resistors"
    assert not media.exists()
