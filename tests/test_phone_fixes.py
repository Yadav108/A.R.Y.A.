"""Fixes from the phone conversations of 2026-11-16/17: blocked requests answered clearly, the model reloading
by itself after a failed start, the low-memory reply naming what to close, 'Play, the song is paused'."""
import asyncio
from types import SimpleNamespace

import pytest

import arya.core.llm as llm
import arya.server.websocket as ws
from arya.core import policy


# ── Blocked from the phone: a clear answer, not a guessed app ─────────────────

@pytest.mark.parametrize("text, about", [
    ("Close the window", "minimise"),                         # from the phone log: the model guessed Spotify
    ("close Chrome", "minimise"), ("quit spotify", "minimise"),
    ("delete the file in my downloads", "files"), ("move this document to the desktop", "files"),
    ("what's on my clipboard?", "clipboard"), ("copy that", "clipboard"),
    ("run the command ipconfig", "commands"),
])
def test_phone_refusals(text, about):
    assert about in policy.remote_refusal(text)


@pytest.mark.parametrize("text", ["close this tab", "close the tab", "exit study mode", "minimise the window",
                                  "what's the weather", "play some music", "close enough, what's next?",
                                  "how do I move a file in python", "what is a clipboard manager",
                                  "how do I run a script in python"])
def test_not_refused(text):
    assert policy.remote_refusal(text) is None


def test_phone_turn_answers_refusals_without_the_model(monkeypatch):
    called = []

    async def fail(*a, **k):
        called.append(1)
        yield "should not happen"
    monkeypatch.setattr(ws, "stream_with_tools", fail)
    out = asyncio.run(ws.phone_turn("Close the window"))
    assert "minimise" in out["reply"] and not called and out["confirm"] is False


# ── The model loads itself again after a failed start ────────────────────────

def test_keep_trying_until_it_loads(monkeypatch):
    from arya.core import llm_local
    tries, ready = [], []

    async def warm_up(attempts=2):
        tries.append(attempts)
        return len(tries) >= 3
    monkeypatch.setattr(llm_local, "warm_up", warm_up)
    busy = iter([True, False, False, False])
    asyncio.run(llm_local.keep_trying(busy=lambda: next(busy), every_s=0, on_ready=lambda: ready.append(1)))
    assert tries == [1, 1, 1] and ready == [1]                 # the busy round was skipped


# ── The low-memory reply names what to close ─────────────────────────────────

def test_memory_hogs(monkeypatch):
    import psutil

    def proc(name, gb):
        return SimpleNamespace(info={"name": name, "memory_info": SimpleNamespace(private=gb * 2**30, rss=0)})
    procs = [proc("Code.exe", 2.0), proc("Code.exe", 1.3), proc("chrome.exe", 1.5), proc("pythonw.exe", 3.5),
             proc("vmmemWSL", 0.8), proc("svchost.exe", 0.6), proc("notepad.exe", 0.1)]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: procs)
    assert ws.memory_hogs() == "VS Code (3.3 GB), Chrome (1.5 GB) and WSL (0.8 GB)"     # not ARYA, not Windows


def test_low_memory_reply(monkeypatch):
    monkeypatch.setattr(ws, "memory_hogs", lambda top=3: "VS Code (3.3 GB) and Chrome (1.5 GB)")
    msg = ws._model_down_message("cudaMalloc failed: out of memory")
    assert "VS Code (3.3 GB)" in msg and "keep trying" in msg


# ── "Play, the song is paused" ───────────────────────────────────────────────

@pytest.mark.parametrize("text, action", [("Play, the song is paused", "play"), ("Pause, the music is too loud", "pause"),
                                          ("resume - it stopped", "play")])
def test_media_with_a_reason(text, action):
    assert llm.quick_media_command(text) == {"action": action}


def test_songs_with_commas_are_still_songs():
    assert llm.quick_media_command("play, despacito") is None
