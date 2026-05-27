import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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


@app.on_event("startup")
async def preload_models() -> None:
    loop = asyncio.get_event_loop()

    print("STT: loading Whisper medium (CUDA)...")
    await loop.run_in_executor(None, load_model)
    print("STT: model ready.")

    # Wake word callback — runs in the background thread, posts to asyncio
    async def on_wake(command: str) -> None:
        """Called by WakeWordEngine when 'arya' is detected."""
        if not _active_connections:
            print("WakeWord: triggered but no browser connected — open http://localhost:8000")
            return
        # Pick any connected client (last one wins; most setups have one)
        ws = next(iter(_active_connections))
        await _handle_message(ws, command, wake_triggered=True)

    wake_engine.set_callback(on_wake, loop)
    wake_engine.start()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── Shared message pipeline ───────────────────────────────────────────────────

async def _handle_message(
    websocket: WebSocket,
    message: str,
    wake_triggered: bool = False,
) -> None:
    """Run LLM + TTS pipeline for one message, streaming results back."""
    if not message:
        # Wake word with no command → ask the user what they need
        message = "I heard you. How can I help?"

    if wake_triggered:
        await websocket.send_text("ARYA_WAKE")
        if message != "I heard you. How can I help?":
            await websocket.send_text(f"TRANSCRIPT:{message}")

    try:
        print(f"Processing: {message[:60]}")
        wake_engine.mute(True)

        async for sentence in stream_with_tools(message):
            await websocket.send_text(sentence)
            try:
                audio = await asyncio.wait_for(speak_sentence(sentence), timeout=10.0)
            except asyncio.TimeoutError:
                print(f"TTS timeout: {sentence[:30]}")
                audio = b""
            if audio:
                await websocket.send_bytes(audio)

        await websocket.send_text("ARYA_DONE")
        print("Response complete")

    except Exception as exc:
        err = str(exc)
        print(f"Pipeline error: {err}")
        msg = (
            "I've hit my daily token limit. Please wait a few minutes."
            if "rate_limit" in err.lower() or "rate limit" in err.lower()
            else "ARYA encountered an error."
        )
        await websocket.send_text(msg)
        audio = await speak_sentence(msg)
        if audio:
            await websocket.send_bytes(audio)
    finally:
        wake_engine.mute(False)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _active_connections.add(websocket)
    print(f"ARYA connected  ({len(_active_connections)} client(s))")

    async def notify_callback(notification: str) -> None:
        try:
            await websocket.send_text(notification)
            try:
                audio = await asyncio.wait_for(speak_sentence(notification), timeout=10.0)
            except asyncio.TimeoutError:
                audio = b""
            if audio:
                await websocket.send_bytes(audio)
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
                await _handle_message(websocket, transcript)

            # ── Text → direct (test / fallback) ──────────────────────────
            elif raw_text:
                await _handle_message(websocket, raw_text)

    except WebSocketDisconnect:
        print("ARYA disconnected")
    finally:
        _active_connections.discard(websocket)
        set_notify_callback(None)
        print(f"ARYA disconnected  ({len(_active_connections)} client(s) remaining)")
