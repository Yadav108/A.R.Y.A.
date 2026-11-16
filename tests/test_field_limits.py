"""Limits found in the field log (2026-11-16/17) and their fixes: misheard "NJ" forms, a filler before a command,
"window explorer", and the local model loading partly on the CPU when the GPU is full."""
import asyncio

import httpx
import pytest

from arya.core import addressee, hotword
from arya.core import llm_local as L


@pytest.fixture
def nj(monkeypatch):
    monkeypatch.setattr(hotword, "extra_models", lambda: ["hey_nj.npz"])


@pytest.mark.parametrize("text, rest", [
    ("Hey, engine.", ""), ("Hi, NG.", ""), ("Hey Anjay, tell me a joke.", "tell me a joke."),
    ("Enjoy open window explorer.", "open window explorer."), ("Enjoy! Open Windows Explorer.", "Open Windows Explorer."),
    ("Engine pause the music", "pause the music"),
])
def test_misheard_nj_is_the_name(nj, text, rest):
    assert addressee.split_name(text) == (rest, True)


@pytest.mark.parametrize("text", [
    "Enjoy your meal.", "Enjoy the show, guys.", "Enjoy!", "the engine is hot", "Engine trouble again.",
    "Enjoy it while it lasts.", "Hey, enjoy your weekend.",
])
def test_ordinary_sentences_stay_ordinary(nj, text):
    assert addressee.split_name(text) == (text, False)


def test_without_hey_nj_set_up_nothing_changes(monkeypatch):
    monkeypatch.setattr(hotword, "extra_models", lambda: [])
    assert addressee.split_name("Hey, engine.") == ("Hey, engine.", False)


@pytest.mark.parametrize("text, stripped", [
    ("Hurry up, pause the music.", "pause the music."),
    ("Come on, set a timer for 10 minutes", "set a timer for 10 minutes"),
    ("Okay so, what's the weather?", "what's the weather?"),
    ("No, that's not it.", "No, that's not it."),           # not a command: left alone
    ("Yeah, I know.", "Yeah, I know."),
    ("pause the music", "pause the music"),
])
def test_filler_before_a_command(text, stripped):
    assert addressee.strip_filler(text) == stripped


def test_filler_command_is_for_arya():
    assert addressee.quick_verdict("Hurry up, pause the music.") is True


def test_window_explorer_alias():
    from arya.tools.os_tools import _APP_ALIASES
    assert _APP_ALIASES["window explorer"] == "explorer.exe"


# ── The GPU is full: partly on the CPU instead of silence ─────────────────────

@pytest.fixture
def ollama(monkeypatch):
    """A fake Ollama: fails with a CUDA out-of-memory error while more than `fits` layers are on the GPU."""
    state = {"fits": 18, "seen": []}

    def reply(request: httpx.Request) -> httpx.Response:
        import json
        options = json.loads(request.content).get("options", {})
        layers = options.get("num_gpu", 99)
        state["seen"].append(layers)
        if layers > state["fits"]:
            return httpx.Response(500, json={"error": "llama-server reported out-of-memory during startup: cudaMalloc failed"})
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text='{"message":{"content":"Hi"},"done":false}\n{"done":true}\n')
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "Hi"}})

    monkeypatch.setattr(L, "_gpu", {"layers": None, "until": 0.0})
    monkeypatch.setattr(L, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    return state


def test_chat_steps_down_to_half_the_gpu(ollama):
    msg = asyncio.run(L.chat_local([{"role": "user", "content": "hi"}]))
    assert msg["content"] == "Hi" and ollama["seen"] == [99, 18]
    asyncio.run(L.chat_local([{"role": "user", "content": "again"}]))
    assert ollama["seen"][-1] == 18                    # remembered: no failed load first


def test_stream_steps_down_to_the_cpu(ollama):
    ollama["fits"] = 0

    async def go():
        return [t async for t in L.stream_local([{"role": "user", "content": "hi"}])]
    assert asyncio.run(go()) == ["Hi"] and ollama["seen"] == [99, 18, 0]


def test_gives_up_when_even_the_cpu_fails(ollama):
    ollama["fits"] = -1
    with pytest.raises(L.LocalLLMUnavailable):
        asyncio.run(L.chat_local([{"role": "user", "content": "hi"}]))
    assert ollama["seen"] == [99, 18, 0]


def test_warm_up_steps_down(ollama):
    assert asyncio.run(L.warm_up(attempts=1)) is True and ollama["seen"] == [99, 18]


def test_back_to_the_whole_gpu_later(ollama, monkeypatch):
    asyncio.run(L.chat_local([{"role": "user", "content": "hi"}]))
    L._gpu["until"] = 0.0                               # ten minutes later
    ollama["fits"] = 99
    asyncio.run(L.chat_local([{"role": "user", "content": "hi"}]))
    assert ollama["seen"][-1] == 99 and L._gpu["layers"] is None


def test_other_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(L, "_gpu", {"layers": None, "until": 0.0})
    assert not L._step_down(L.LocalLLMUnavailable("Ollama model 'x' not found"))
