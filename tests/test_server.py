"""Server behaviour: stopping a reply, the spoken-sentence limit, bare wake, honest errors, log redaction."""
import asyncio
import logging
import time
import types

import pytest

ws = pytest.importorskip("arya.server.websocket")


@pytest.fixture
def quiet(monkeypatch):
    sent, events, synth = [], [], []

    async def send_all(p):
        sent.append(p)

    async def synthesize(s):
        synth.append(s)
        return b"x"

    async def play(audio):
        pass

    monkeypatch.setattr(ws, "_send_all", send_all)
    monkeypatch.setattr(ws, "_synthesize", synthesize)
    monkeypatch.setattr(ws, "LOCAL_AUDIO", True)
    monkeypatch.setattr(ws.speaker, "play", play)
    monkeypatch.setattr(ws.speaker, "stop", lambda: events.append("speaker.stop"))
    monkeypatch.setattr(ws.wake_engine, "end_conversation", lambda: events.append("end_conversation"))
    monkeypatch.setattr(ws.wake_engine, "set_speaking", lambda s: None)
    monkeypatch.setattr(ws.wake_engine, "followup_active", lambda: False)
    return types.SimpleNamespace(sent=sent, events=events, synth=synth)


def test_stop_cancels_the_reply_in_progress(quiet):
    async def main():
        async def long_reply():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                quiet.events.append("reply cancelled")
                raise
        ws._current_turn = asyncio.create_task(long_reply())
        await asyncio.sleep(0.05)
        assert await ws._meant_for_arya("stop")
        await ws._dispatch("stop", wake_triggered=False, announce_transcript=True)
    asyncio.run(main())
    assert "reply cancelled" in quiet.events and "speaker.stop" in quiet.events
    assert "ARYA_INTERRUPT" in quiet.sent and "ARYA_DONE" in quiet.sent


ANSWER = ["Stability depends on ζ and ωₙ.", " The characteristic equation is:", "\ns² + 2ζωₙ s + ωₙ² = 0",
          " Its poles have negative real parts when ζ > 0.", " For ζ < 1 it is underdamped.",
          " At ζ = 1 it is critically damped.", " For ζ > 1 it is overdamped.", " ωₙ sets the speed.",
          " Higher ωₙ is faster."]


def _speak(message):
    async def gen():
        for s in ANSWER:
            yield s
    return ws._speak_stream(gen(), max_spoken=ws.spoken_limit(message))


def test_spoken_limit_shows_everything_but_speaks_about_five(quiet):
    asyncio.run(_speak("what decides stability?"))
    shown = [x for x in quiet.sent if x != "ARYA_SPEAKING"]
    assert shown == ANSWER
    assert quiet.synth[-1] == ws.REST_ON_SCREEN and len(quiet.synth) == 7      # 5 sentences + formula line + note


def test_detail_request_is_read_in_full(quiet):
    asyncio.run(_speak("explain stability in detail"))
    assert len(quiet.synth) == len(ANSWER) and ws.REST_ON_SCREEN not in quiet.synth


def test_sentence_after_bare_wake_skips_the_check(quiet, monkeypatch):
    async def say(t):
        pass
    monkeypatch.setattr(ws, "_say", say)
    asyncio.run(ws._run_turn("", wake_triggered=True, announce_transcript=False))
    assert ws._answers_bare_wake() is True
    assert ws._answers_bare_wake() is False                # only the first sentence
    asyncio.run(ws._run_turn("", wake_triggered=True, announce_transcript=False))
    ws._bare_wake_until = time.time() - 1                  # the window passed
    assert ws._answers_bare_wake() is False


def test_filler_after_bare_wake_keeps_the_window_open(quiet, monkeypatch):
    """From the log: "Hey ARYA" → "That's what, um." → the request 23 s later was ignored."""
    async def say(t):
        pass
    monkeypatch.setattr(ws, "_say", say)
    asyncio.run(ws._run_turn("", wake_triggered=True, announce_transcript=False))
    assert ws.BARE_WAKE_WINDOW_S >= 20
    assert ws._answers_bare_wake("That's what, um.") is False
    assert ws._answers_bare_wake("open the lab report") is True
    assert ws._is_filler("Um, so, like…") and not ws._is_filler("next song")


@pytest.mark.parametrize("free_gb, procs, detail, expect", [
    (0.8, ["ollama.exe"], "Cannot reach Ollama", "low on memory"),
    (4.0, ["python.exe"], "Cannot reach Ollama", "Start Ollama"),
    (4.0, ["ollama.exe"], "Cannot reach Ollama", "restarting"),
    (4.0, ["ollama.exe"], "Ollama error 500: model requires more system memory (2.9 GiB) than is available",
     "low on memory"),
])
def test_model_down_message_says_why(monkeypatch, free_gb, procs, detail, expect):
    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: types.SimpleNamespace(available=free_gb * 2**30))
    monkeypatch.setattr(psutil, "process_iter",
                        lambda attrs=None: [types.SimpleNamespace(info={"name": n}) for n in procs])
    assert expect in ws._error_message(ws.LLMUnavailableError(detail))


def test_token_is_redacted_from_logs():
    from arya.server import security

    assert security.redact("/ws?token=SECRET") == "/ws?token=***"
    assert security.redact("/api/kb/page?doc=1&token=XYZ&q=a") == "/api/kb/page?doc=1&token=***&q=a"
    security.install_log_redaction()
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("127.0.0.1", "GET",
                               "/api/visual/1?token=SECRET"), None)
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    assert "SECRET" not in record.getMessage()
