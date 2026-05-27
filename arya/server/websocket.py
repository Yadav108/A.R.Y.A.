import asyncio
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from arya.core.llm import stream_with_tools
from arya.core.tts import speak_sentence
from arya.tools.productivity import set_notify_callback


app = FastAPI()
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    print("ARYA connected")

    async def notify_callback(notification: str) -> None:
        try:
            await websocket.send_text(notification)
            try:
                audio_bytes = await asyncio.wait_for(speak_sentence(notification), timeout=10.0)
            except asyncio.TimeoutError:
                print(f"TTS timeout for: {notification[:30]}")
                audio_bytes = b""
            if audio_bytes:
                await websocket.send_bytes(audio_bytes)
        except Exception:
            return

    set_notify_callback(notify_callback)

    try:
        while True:
            message = await websocket.receive_text()
            try:
                print(f"Processing: {message[:50]}")
                async for sentence in stream_with_tools(message):
                    await websocket.send_text(sentence)
                    try:
                        audio_bytes = await asyncio.wait_for(speak_sentence(sentence), timeout=10.0)
                    except asyncio.TimeoutError:
                        print(f"TTS timeout for: {sentence[:30]}")
                        audio_bytes = b""
                    if audio_bytes:
                        await websocket.send_bytes(audio_bytes)
                await websocket.send_text("ARYA_DONE")
                print("Response complete")
            except Exception as exc:
                error_message = str(exc)
                print(f"ARYA websocket error: {error_message}")
                if "rate_limit_exceeded" in error_message or "Rate limit" in error_message:
                    rate_limit_text = "I've hit my daily token limit. Please wait a few minutes and try again."
                    await websocket.send_text(rate_limit_text)
                    audio_bytes = await speak_sentence(rate_limit_text)
                    if audio_bytes:
                        await websocket.send_bytes(audio_bytes)
                else:
                    await websocket.send_text("ARYA encountered an error")
    except WebSocketDisconnect:
        print("ARYA disconnected, waiting for reconnection")
    finally:
        set_notify_callback(None)
