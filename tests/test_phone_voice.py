"""Talking to ARYA from the phone app: the recording in (16 kHz WAV), the answer out as a stream of JSON lines —
heard, then each sentence with ARYA's voice, then done. Whisper, the model and Piper are faked."""
import asyncio
import base64
import io
import json
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from arya.server import phone as P

AUTH = {"X-ARYA": "1"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "TOKEN_FILE", tmp_path / "phone_token")
    P._state.hosts = {"testserver"}
    monkeypatch.setattr(P, "client_ok", lambda host: True)
    AUTH["Authorization"] = f"Bearer {P.token()}"


def wav(seconds=1.0, rate=16000, channels=1, width=2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes((np.sin(np.arange(int(seconds * rate)) / 8) * 8000).astype("<i2").tobytes() * channels)
    return buf.getvalue()


def lines(response) -> list[dict]:
    return [json.loads(l) for l in response.text.splitlines() if l.strip()]


# ── The endpoint ──────────────────────────────────────────────────────────────

def test_voice_streams_heard_say_done(monkeypatch):
    got = {}

    async def fake(audio):
        got["n"] = len(audio)
        yield {"type": "heard", "text": "what time is it"}
        yield {"type": "say", "text": "It's ten past ten.", "audio": "UklGRg=="}
        yield {"type": "done", "confirm": False}
    monkeypatch.setattr(P, "_voice", fake)
    r = TestClient(P.app).post("/api/voice", content=wav(1.5), headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    assert [e["type"] for e in lines(r)] == ["heard", "say", "done"]
    assert got["n"] == 24000


@pytest.mark.parametrize("body, code", [
    (wav(1, rate=44100), 400), (wav(1, channels=2), 400), (b"not a wav file at all", 400), (wav(0.1), 400),
    (wav(31), 413),
])
def test_bad_recordings_are_refused(monkeypatch, body, code):
    async def fake(audio):
        yield {"type": "done"}
    monkeypatch.setattr(P, "_voice", fake)
    assert TestClient(P.app).post("/api/voice", content=body, headers=AUTH).status_code == code


def test_needs_the_key_and_a_running_arya(monkeypatch):
    c = TestClient(P.app)
    assert c.post("/api/voice", content=wav(1), headers={"X-ARYA": "1"}).status_code == 401
    monkeypatch.setattr(P, "_voice", None)
    assert c.post("/api/voice", content=wav(1), headers=AUTH).status_code == 503


def test_wav_to_array():
    a = P.wav_to_array(wav(2))
    assert a.dtype == np.float32 and len(a) == 32000 and float(np.abs(a).max()) <= 1.0


# ── The turn itself (websocket.phone_voice_turn) ──────────────────────────────

@pytest.fixture
def ws(monkeypatch):
    import arya.server.websocket as ws
    from arya.core import stt
    monkeypatch.setattr(stt, "transcribe_array", lambda audio: "Tell me a joke.")

    async def stream(text):
        for s in ["Why did the capacitor blush?", "It saw the inductor's current.", "Ha."]:
            yield s

    async def synth(sentence):
        return b"RIFF" + sentence.encode()
    monkeypatch.setattr(ws, "stream_with_tools", stream)
    monkeypatch.setattr(ws, "_synthesize", synth)
    return ws


def run_turn(ws, audio=None):
    async def go():
        return [e async for e in ws.phone_voice_turn(audio if audio is not None else np.zeros(16000, np.float32))]
    return asyncio.run(go())


def test_turn_heard_then_each_sentence_spoken(ws):
    events = run_turn(ws)
    assert events[0] == {"type": "heard", "text": "Tell me a joke."}
    says = [e for e in events if e["type"] == "say"]
    assert [e["text"] for e in says] == ["Why did the capacitor blush?", "It saw the inductor's current.", "Ha."]
    assert base64.b64decode(says[0]["audio"]) == b"RIFFWhy did the capacitor blush?" and says[0]["mime"] == "audio/wav"
    assert events[-1]["type"] == "done"


def test_turn_long_answers_speak_only_the_first_sentences(ws, monkeypatch):
    monkeypatch.setattr(ws, "PHONE_SPOKEN_SENTENCES", 2)
    says = [e for e in run_turn(ws) if e["type"] == "say"]
    assert [bool(e["audio"]) for e in says] == [True, True, False]                # the rest is text on the phone


def test_turn_nothing_heard(ws, monkeypatch):
    from arya.core import stt
    monkeypatch.setattr(stt, "transcribe_array", lambda audio: "  ")
    events = run_turn(ws)
    assert events[-1] == {"type": "done", "error": "I didn't catch that.", "confirm": False}


def test_turn_runs_as_a_phone_turn(ws, monkeypatch):
    from arya.core import policy
    seen = []

    async def stream(text):
        seen.append(policy.remote_turn.get())
        yield "Okay."
    monkeypatch.setattr(ws, "stream_with_tools", stream)
    run_turn(ws)
    assert seen == [True]                                                         # files, clipboard, commands: blocked
