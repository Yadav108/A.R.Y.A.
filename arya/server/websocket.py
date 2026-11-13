import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from arya.core.embedder import load_embedder
from arya.core.llm import stream_with_tools
from arya.core.stt import load_model, transcribe
from arya.core.tts import speak_sentence
from arya.core.wakeword import engine as wake_engine
from arya.tools.productivity import set_notify_callback


app = FastAPI()
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# All active WebSocket connections — wake word broadcasts to all of them
_active_connections: set[WebSocket] = set()

# One in-flight response per connection. A new trigger (wake word said again,
# or tap-to-talk) cancels whatever is currently running so it can barge in —
# see _dispatch().
_response_tasks: dict[WebSocket, asyncio.Task] = {}

# Bare "stop" commands short-circuit straight to cancellation instead of
# round-tripping through the LLM + TTS to generate an acknowledgement — that
# extra generate-then-speak cycle is exactly what made "stop" look broken
# (ARYA would say "okay, I'll stop" while still finishing the response it was
# supposedly stopping).
_STOP_PHRASES = {
    "stop", "stop talking", "shut up", "be quiet", "quiet", "quiet please",
    "cancel", "cancel that", "never mind", "nevermind", "stop it", "silence",
    "that's enough", "thats enough", "enough",
}


def _is_stop_command(text: str) -> bool:
    return text.strip().lower().strip(" .!?,") in _STOP_PHRASES


@app.on_event("startup")
async def preload_models() -> None:
    loop = asyncio.get_event_loop()

    print("STT: loading Whisper medium (CUDA)...")
    await loop.run_in_executor(None, load_model)
    print("STT: model ready.")

    print("Embedder: loading all-MiniLM-L6-v2 (CPU)...")
    await loop.run_in_executor(None, load_embedder)
    print("Embedder: model ready.")

    # Wake word callback — runs in the background thread, posts to asyncio
    async def on_wake(command: str, wake_triggered: bool) -> None:
        """Called by WakeWordEngine for wake-word hits and follow-up speech."""
        if not _active_connections:
            print("WakeWord: triggered but no browser connected — open http://localhost:8000")
            return
        # Pick any connected client (last one wins; most setups have one)
        ws = next(iter(_active_connections))
        await _dispatch(
            ws,
            command,
            wake_triggered=wake_triggered,
            announce_transcript=True,
        )

    wake_engine.set_callback(on_wake, loop)
    wake_engine.start()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── Shared message pipeline ───────────────────────────────────────────────────

async def _synthesize(sentence: str) -> bytes:
    try:
        return await asyncio.wait_for(speak_sentence(sentence), timeout=10.0)
    except asyncio.TimeoutError:
        print(f"TTS timeout: {sentence[:30]}")
        return b""


async def _dispatch(
    websocket: WebSocket,
    message: str,
    wake_triggered: bool = False,
    announce_transcript: bool = False,
) -> None:
    """Cancel any response already in flight on this connection, then start a
    new one. This is what lets a repeated wake word (or a new tap-to-talk)
    interrupt ARYA mid-sentence instead of queuing up behind her."""
    existing = _response_tasks.get(websocket)
    if existing is not None and not existing.done():
        existing.cancel()
        try:
            await existing
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await websocket.send_text("ARYA_INTERRUPT")
        except Exception:
            pass

    if _is_stop_command(message):
        task = asyncio.create_task(
            _handle_stop(websocket, message, announce_transcript=announce_transcript)
        )
    else:
        task = asyncio.create_task(
            _handle_message(
                websocket,
                message,
                wake_triggered=wake_triggered,
                announce_transcript=announce_transcript,
            )
        )
    _response_tasks[websocket] = task


async def _handle_stop(
    websocket: WebSocket,
    message: str,
    announce_transcript: bool = False,
) -> None:
    """Silently acknowledge a stop command — no LLM call, no TTS. The barge-in
    cancellation + ARYA_INTERRUPT already happened in _dispatch(); this just
    closes out the turn cleanly instead of speaking over the cancellation."""
    if announce_transcript:
        await websocket.send_text(f"TRANSCRIPT:{message}")
    try:
        await websocket.send_text("ARYA_DONE")
    except Exception:
        pass


async def _handle_message(
    websocket: WebSocket,
    message: str,
    wake_triggered: bool = False,
    announce_transcript: bool = False,
) -> None:
    """Run LLM + TTS pipeline for one message, streaming results back.

    TTS synthesis for each sentence is kicked off the moment its text is sent,
    without waiting for earlier sentences' audio — edge_tts calls are
    network-bound, so several run concurrently. Audio is still sent to the
    client in sentence order, but by the time we get to sentence N's task it
    has usually been synthesizing since sentence N's text first went out,
    instead of only starting after every earlier sentence finished.
    """
    if wake_triggered:
        await websocket.send_text("ARYA_WAKE")

    if not message:
        # Wake word fired with no command attached. Answer directly and skip
        # the LLM/history entirely — this placeholder used to get passed into
        # stream_with_tools() and saved as if the user had said it, which
        # polluted conversation history with a fake turn and confused every
        # reply after it.
        try:
            wake_engine.set_speaking(True)
            greeting = "I heard you. How can I help?"
            await websocket.send_text(greeting)
            audio = await _synthesize(greeting)
            if audio:
                await websocket.send_bytes(audio)
            await websocket.send_text("ARYA_DONE")
        finally:
            wake_engine.set_speaking(False)
        return

    if announce_transcript:
        await websocket.send_text(f"TRANSCRIPT:{message}")

    audio_tasks: list[asyncio.Task] = []

    try:
        print(f"Processing: {message[:60]}")
        wake_engine.set_speaking(True)

        async for sentence in stream_with_tools(message):
            await websocket.send_text(sentence)
            audio_tasks.append(asyncio.create_task(_synthesize(sentence)))

        for task in audio_tasks:
            audio = await task
            if audio:
                await websocket.send_bytes(audio)

        await websocket.send_text("ARYA_DONE")
        print("Response complete")

    except asyncio.CancelledError:
        for task in audio_tasks:
            task.cancel()
        raise
    except Exception as exc:
        for task in audio_tasks:
            task.cancel()
        err = str(exc)
        print(f"Pipeline error: {err}")
        msg = (
            "I've hit my daily token limit. Please wait a few minutes."
            if "rate_limit" in err.lower() or "rate limit" in err.lower()
            else "ARYA encountered an error."
        )
        await websocket.send_text(msg)
        audio = await _synthesize(msg)
        if audio:
            await websocket.send_bytes(audio)
    finally:
        wake_engine.set_speaking(False)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _active_connections.add(websocket)
    print(f"ARYA connected  ({len(_active_connections)} client(s))")

    async def notify_callback(notification: str) -> None:
        try:
            await websocket.send_text(notification)
            wake_engine.set_speaking(True)
            try:
                audio = await _synthesize(notification)
                if audio:
                    await websocket.send_bytes(audio)
            finally:
                wake_engine.set_speaking(False)
        except Exception:
            pass

    set_notify_callback(notify_callback)

    try:
        while True:
            msg = await websocket.receive()

            if msg.get("type") == "websocket.disconnect":
                break

            raw_bytes: bytes | None = msg.get("bytes")
            raw_text:  str   | None = msg.get("text")

            # ── Binary → transcribe with Whisper medium ───────────────────
            if raw_bytes:
                print(f"Audio received: {len(raw_bytes):,} bytes — transcribing...")
                transcript = await transcribe(raw_bytes)
                if not transcript:
                    await websocket.send_text("ARYA_NO_SPEECH")
                    continue
                print(f"Transcript: {transcript}")
                await websocket.send_text(f"TRANSCRIPT:{transcript}")
                await _dispatch(websocket, transcript, announce_transcript=False)

            # ── Text → direct (test / fallback) ──────────────────────────
            elif raw_text:
                await _dispatch(websocket, raw_text, announce_transcript=False)

    except WebSocketDisconnect:
        print("ARYA disconnected")
    finally:
        _active_connections.discard(websocket)
        task = _response_tasks.pop(websocket, None)
        if task is not None and not task.done():
            task.cancel()
        set_notify_callback(None)
        print(f"ARYA disconnected  ({len(_active_connections)} client(s) remaining)")
