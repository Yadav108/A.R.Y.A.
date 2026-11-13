"""
ARYA server — one assistant, one voice, any number of browser windows.

The conversation lives here on the server, not in a browser tab:
  * Wake word / follow-ups / push-to-talk all feed the same turn pipeline.
  * Speech plays through the PC speakers (AUDIO_OUTPUT=local, default), so
    ARYA is fully hands-free with no tab open. Set AUDIO_OUTPUT=browser to get
    the old behaviour of streaming MP3 to the page instead.
  * Browser windows are mirrors: they get transcript/response/state events and
    can send push-to-talk audio or typed text.
"""
import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from arya.core.embedder import load_embedder
from arya.core.llm import LLMUnavailableError, stream_with_tools
from arya.core.speaker import speaker
from arya.core.stt import load_model, transcribe
from arya.core.tts import speak_sentence
from arya.core.wakeword import engine as wake_engine
from arya.tools.productivity import set_notify_callback


LOCAL_AUDIO = os.getenv("AUDIO_OUTPUT", "local").strip().lower() != "browser"

app = FastAPI()
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_connections: set[WebSocket] = set()

# The one in-flight turn. A new trigger (wake word, follow-up, push-to-talk)
# cancels it so the user can barge in instead of queuing behind ARYA.
_current_turn: asyncio.Task | None = None

# Only one thing speaks at a time: a user turn or a timer/reminder alert.
_voice_lock = asyncio.Lock()

# Bare "stop" commands short-circuit straight to cancellation instead of
# round-tripping through the LLM + TTS to generate an acknowledgement.
_STOP_PHRASES = {
    "stop", "stop talking", "shut up", "be quiet", "quiet", "quiet please",
    "cancel", "cancel that", "never mind", "nevermind", "stop it", "silence",
    "that's enough", "thats enough", "enough",
}

# Phrases that close the hands-free follow-up window.
_THANKS_PHRASES = {"thanks", "thank you", "thanks arya", "thank you arya", "cheers", "perfect thanks"}
_END_PHRASES = _THANKS_PHRASES | {
    "that's all", "thats all", "that's it", "thats it", "goodbye", "bye",
    "no thanks", "no thank you", "nothing", "i'm good", "im good", "all good",
}

WAKE_ACK = "Yes?"
THANKS_ACK = "Anytime."


def _normalise(text: str) -> str:
    return text.strip().lower().strip(" .!?,")


# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def preload_models() -> None:
    loop = asyncio.get_event_loop()

    print("STT: loading Whisper medium (CUDA)...")
    await loop.run_in_executor(None, load_model)
    print("STT: model ready.")

    print("Embedder: loading all-MiniLM-L6-v2 (CPU)...")
    await loop.run_in_executor(None, load_embedder)
    print("Embedder: model ready.")

    async def on_wake(command: str, wake_triggered: bool) -> None:
        """Called by WakeWordEngine for wake-word hits and follow-up speech."""
        await _dispatch(command, wake_triggered=wake_triggered, announce_transcript=True)

    wake_engine.set_callback(on_wake, loop)
    wake_engine.start()

    set_notify_callback(announce)

    # Pre-synthesize the phrases ARYA says instantly (cached in tts.py).
    async def _warm_cache() -> None:
        for phrase in (WAKE_ACK, THANKS_ACK):
            await _synthesize(phrase)

    asyncio.create_task(_warm_cache())
    output = "PC speakers" if LOCAL_AUDIO else "browser"
    print(f"ARYA ready — voice output: {output}. Say 'ARYA ...' (no browser tab needed).")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── Fan-out to browser windows ────────────────────────────────────────────────

async def _send_all(payload: str | bytes) -> None:
    for ws in list(_connections):
        try:
            if isinstance(payload, bytes):
                await ws.send_bytes(payload)
            else:
                await ws.send_text(payload)
        except Exception:
            _connections.discard(ws)


# ── Speech pipeline ───────────────────────────────────────────────────────────

async def _synthesize(sentence: str) -> bytes:
    try:
        return await asyncio.wait_for(speak_sentence(sentence), timeout=10.0)
    except asyncio.TimeoutError:
        print(f"TTS timeout: {sentence[:30]}")
    except Exception as exc:
        print(f"TTS error: {exc}")
    return b""


async def _aiter(items: Iterable[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


async def _speak_stream(sentences: AsyncIterator[str]) -> None:
    """Text goes to the UI the moment each sentence exists, its TTS starts
    immediately, and audio plays in order as soon as it is ready — so ARYA
    starts talking after the *first* sentence, not after the whole reply."""
    queue: asyncio.Queue[asyncio.Task | None] = asyncio.Queue()

    async def produce() -> None:
        try:
            async for sentence in sentences:
                await _send_all(sentence)
                await queue.put(asyncio.create_task(_synthesize(sentence)))
        finally:
            await queue.put(None)

    producer = asyncio.create_task(produce())
    announced = False
    try:
        while True:
            task = await queue.get()
            if task is None:
                break
            audio = await task
            if not audio:
                continue
            if LOCAL_AUDIO:
                if not announced:
                    await _send_all("ARYA_SPEAKING")
                    announced = True
                await speaker.play(audio)
            else:
                await _send_all(audio)
        await producer  # surface LLM errors raised while producing
    except BaseException:
        producer.cancel()
        while not queue.empty():
            pending = queue.get_nowait()
            if pending is not None:
                pending.cancel()
        raise


async def _say(text: str) -> None:
    await _speak_stream(_aiter([text]))


def _error_message(exc: Exception) -> str:
    if isinstance(exc, LLMUnavailableError):
        return "I can't reach Groq and my local model isn't running. Start Ollama and try again."
    err = str(exc).lower()
    if "rate_limit" in err or "rate limit" in err:
        return "I've hit my daily token limit. Please wait a few minutes."
    return "Sorry, something went wrong on my end."


# ── Turns ─────────────────────────────────────────────────────────────────────

async def _dispatch(message: str, wake_triggered: bool = False, announce_transcript: bool = False) -> None:
    """Cancel whatever ARYA is doing, then start the new turn."""
    global _current_turn
    if _current_turn is not None and not _current_turn.done():
        speaker.stop()
        _current_turn.cancel()
        try:
            await _current_turn
        except (asyncio.CancelledError, Exception):
            pass
        await _send_all("ARYA_INTERRUPT")

    if _normalise(message) in _STOP_PHRASES:
        speaker.stop()
        wake_engine.end_conversation()
        if announce_transcript:
            await _send_all(f"TRANSCRIPT:{message}")
        await _send_all("ARYA_DONE")
        return

    _current_turn = asyncio.create_task(
        _run_turn(message, wake_triggered=wake_triggered, announce_transcript=announce_transcript)
    )


async def _run_turn(message: str, wake_triggered: bool, announce_transcript: bool) -> None:
    if wake_triggered:
        await _send_all("ARYA_WAKE")

    async with _voice_lock:
        wake_engine.set_speaking(True)
        try:
            if not message:
                # Bare "ARYA" — acknowledge and let the follow-up window catch
                # the actual request. Never sent to the LLM or saved to history.
                await _say(WAKE_ACK)
                return

            if announce_transcript:
                await _send_all(f"TRANSCRIPT:{message}")

            normalised = _normalise(message)
            if normalised in _END_PHRASES:
                wake_engine.end_conversation()
                if normalised in _THANKS_PHRASES:
                    await _say(THANKS_ACK)
                return

            print(f"Processing: {message[:60]}")
            try:
                await _speak_stream(stream_with_tools(message))
                print("Response complete")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Pipeline error: {exc}")
                await _say(_error_message(exc))
        finally:
            wake_engine.set_speaking(False)
            try:
                await _send_all("ARYA_DONE")
                if wake_engine.followup_active():
                    await _send_all("ARYA_FOLLOWUP")
            except Exception:
                pass


async def announce(text: str) -> None:
    """Speak a timer/reminder/pomodoro alert — works with no browser open."""
    async with _voice_lock:
        wake_engine.set_speaking(True)
        try:
            await _send_all("NOTIFY:" + text)
            if LOCAL_AUDIO:
                audio = await _synthesize(text)
                await speaker.play(audio)
            else:
                audio = await _synthesize(text)
                if audio:
                    await _send_all(audio)
        except Exception as exc:
            print(f"Announcement failed: {exc}")
        finally:
            wake_engine.set_speaking(False)
            await _send_all("ARYA_DONE")


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _connections.add(websocket)
    print(f"ARYA connected  ({len(_connections)} client(s))")

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            raw_bytes: bytes | None = msg.get("bytes")
            raw_text:  str   | None = msg.get("text")

            # ── Binary → push-to-talk audio, transcribe with Whisper medium ──
            if raw_bytes:
                print(f"Audio received: {len(raw_bytes):,} bytes — transcribing...")
                transcript = await transcribe(raw_bytes)
                if not transcript:
                    await websocket.send_text("ARYA_NO_SPEECH")
                    continue
                print(f"Transcript: {transcript}")
                await _dispatch(transcript, announce_transcript=True)

            # ── Text → typed message ─────────────────────────────────────────
            elif raw_text:
                await _dispatch(raw_text, announce_transcript=True)

    except WebSocketDisconnect:
        pass
    finally:
        _connections.discard(websocket)
        print(f"ARYA disconnected  ({len(_connections)} client(s) remaining)")
