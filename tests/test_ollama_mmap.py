"""Every request to Ollama memory-maps the model (7.0 GB -> 2.9 GB committed on this laptop), and always with the
same value — a request with different load options would make Ollama reload the model."""
from arya.core import llm_local as L


def test_every_payload_asks_for_mmap():
    for payload in (L._payload([{"role": "user", "content": "hi"}], False, None, None, 1),
                    L._payload([{"role": "user", "content": "hi"}], True, None, 0.3, 200),
                    L._payload([{"role": "user", "content": "hi"}], False, [{"type": "function"}], 0, None, "json")):
        assert payload["options"]["use_mmap"] is True
        assert payload["options"]["num_ctx"] == L.NUM_CTX


def test_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(L, "USE_MMAP", False)
    assert L._payload([], False, None, None, None)["options"]["use_mmap"] is False


# ── The prompt-cache trim ─────────────────────────────────────────────────────

import asyncio

import pytest


@pytest.fixture
def runner(monkeypatch):
    state = {"mb": 6200, "unloaded": 0, "warmed": 0, "clock": 10_000.0}

    async def unload():
        state["unloaded"] += 1
        state["mb"] = 0

    async def warm_up(attempts=1):
        state["warmed"] += 1
        state["mb"] = 2900
        return True

    async def no_sleep(s):
        if s < 60:                                     # the short wait after unloading
            return
        if state.pop("stop", False):                   # the second 5-minute wait: end the test
            raise asyncio.CancelledError
        state["stop"] = True
    monkeypatch.setattr(L, "runner_memory_mb", lambda: state["mb"])
    monkeypatch.setattr(L, "unload", unload)
    monkeypatch.setattr(L, "warm_up", warm_up)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    return state


def _one_round(state, busy=False):
    try:
        asyncio.run(L.trim(busy=lambda: busy, now=lambda: state["clock"]))
    except asyncio.CancelledError:
        pass


def test_trims_when_idle_and_bloated(runner, capsys):
    L._last_used["t"] = runner["clock"] - 20 * 60
    _one_round(runner)
    assert runner["unloaded"] == 1 and runner["warmed"] == 1
    assert "prompt cache had grown to 6.1 GB" in capsys.readouterr().out


@pytest.mark.parametrize("why", ["busy", "recent", "small"])
def test_leaves_it_alone(runner, why):
    L._last_used["t"] = runner["clock"] - (60 if why == "recent" else 20 * 60)
    if why == "small":
        runner["mb"] = 3100
    _one_round(runner, busy=why == "busy")
    assert runner["unloaded"] == 0
