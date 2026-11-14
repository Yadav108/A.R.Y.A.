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
import re
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from arya.core import policy
from arya.core.audit import LOG_FILE, log_event
from arya.core.embedder import load_embedder
from arya.core.llm import ARYA_MODE, LLMUnavailableError, stream_with_tools
from arya.core.llm_local import OLLAMA_MODEL, warm_up as warm_up_local_llm
from arya.core.tts import TTS_ENGINE
from arya.core.speaker import speaker
from arya.core.stt import load_model, transcribe
from arya.core.tts import speak_sentence
from arya.core.wakeword import engine as wake_engine
from arya.tools.productivity import set_notify_callback
from arya.server import security


LOCAL_AUDIO = os.getenv("AUDIO_OUTPUT", "local").strip().lower() != "browser"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)   # no API explorer
# Reject any request whose Host isn't this machine (blocks DNS rebinding).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=sorted(security.ALLOWED_HOSTS | {"[::1]"}))
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

# Closing / acknowledgement phrases. An utterance made up *only* of these
# ("Okay. Thank you. Goodbye.", "thanks, bye", "alright cool") is classified
# as a whole by _classify_closing() — it never goes to the LLM.
_THANKS_PHRASES = {
    "thanks", "thank you", "thank you so much", "thank you very much", "thanks a lot",
    "thanks so much", "many thanks", "cheers", "appreciate it", "much appreciated",
}
_END_PHRASES = {
    "that's all", "thats all", "that's it", "thats it", "goodbye", "good bye", "bye", "bye bye",
    "see you", "see ya", "see you later", "talk to you later", "later", "good night", "goodnight",
    "no thanks", "no thank you", "nothing", "nothing else", "i'm good", "im good", "all good",
    "i'm done", "im done", "we're done", "were done", "that will be all", "that'll be all",
}
# Bare acknowledgements: keep the conversation open, but don't reply.
# (No "yes"/"sure"/"yeah" — those can answer a question ARYA just asked.)
_ACK_PHRASES = {
    "okay", "ok", "okey", "alright", "all right", "cool", "got it", "nice", "great", "perfect",
    "awesome", "good", "very good", "fine", "understood", "noted", "i see", "oh", "ah", "hmm",
}
# Words that may appear anywhere in such an utterance without changing its meaning.
_NEUTRAL_WORDS = {"arya", "aria", "so", "then", "well", "and", "very", "much", "again"}

WAKE_ACK = "Yes?"
THANKS_ACK = "Anytime."


def _normalise(text: str) -> str:
    return text.strip().lower().strip(" .!?,")


def _classify_closing(text: str) -> str | None:
    """'stop' / 'end' / 'thanks' (end + say "Anytime.") / 'ack' (stay quiet, keep
    listening) when the utterance consists only of such phrases, else None."""
    words = re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'")).split()
    if not words:
        return None
    phrase_sets = [("stop", _STOP_PHRASES), ("thanks", _THANKS_PHRASES), ("end", _END_PHRASES), ("ack", _ACK_PHRASES)]
    found: set[str] = set()
    i = 0
    while i < len(words):
        if words[i] in _NEUTRAL_WORDS:
            i += 1
            continue
        for n in (4, 3, 2, 1):                       # longest phrase first
            chunk = " ".join(words[i:i + n])
            kind = next((k for k, ps in phrase_sets if chunk in ps), None)
            if kind and len(words[i:i + n]) == n:
                found.add(kind)
                i += n
                break
        else:
            return None                              # a real request in there
    for kind in ("stop", "thanks", "end", "ack"):
        if kind in found:
            return kind
    return None


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
    if hasattr(wake_engine, "set_interrupt_callback"):
        # Dedicated detector: go quiet the instant "Hey ARYA" is heard mid-reply.
        wake_engine.set_interrupt_callback(interrupt)
    if hasattr(wake_engine, "set_event_callback"):
        wake_engine.set_event_callback(emit_status)
    wake_engine.start()

    set_notify_callback(announce)

    # Pre-synthesize the phrases ARYA says instantly (cached in tts.py).
    async def _warm_cache() -> None:
        for phrase in (WAKE_ACK, THANKS_ACK):
            await _synthesize(phrase)

    asyncio.create_task(_warm_cache())
    if ARYA_MODE == "offline":
        asyncio.create_task(warm_up_local_llm())    # load the local model into VRAM now
    brain = f"local {OLLAMA_MODEL}" + (" (say 'ask the cloud, …' to use Groq for one question)"
                                       if ARYA_MODE == "offline" else ", Groq first")
    voice = "local Piper" if TTS_ENGINE != "edge" else "Microsoft Edge (online)"
    print(f"Privacy: {ARYA_MODE} mode — brain: {brain}; voice: {voice}")
    emit_status("ARYA_READY")
    output = "PC speakers" if LOCAL_AUDIO else "browser"
    print(f"ARYA ready — voice output: {output}. Say 'ARYA ...' (no browser tab needed).")
    where = "this computer only" if security.is_loopback_bind() else f"NETWORK ({security.HOST}) — token required"
    print(f"Security: accepting connections from {where}; actions logged to {LOG_FILE}")
    log_event("server_start", host=security.HOST, port=security.PORT)


@app.get("/")
async def index(request: Request) -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # The page gets the WebSocket token only when loaded on this machine.
    # (Other web pages can't read this response — the browser's same-origin
    # rule and the Host check above stop them.)
    if security.is_local_client(request.client.host if request.client else None):
        tag = f'<script>window.ARYA_TOKEN = "{security.TOKEN}";</script>'
        html = html.replace("</head>", tag + "\n</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# ── Fan-out to browser windows ────────────────────────────────────────────────

# In-process status listeners (the tray icon / overlay). They receive every
# text event the browser gets, plus local-only ones (ARYA_READY, ARYA_LISTENING)
# that the browser page doesn't understand. Called from any thread — listeners
# must be thread-safe (the desktop app just puts them on a queue).
_status_listeners: list = []


def add_status_listener(callback) -> None:
    _status_listeners.append(callback)


def emit_status(event: str) -> None:
    for callback in list(_status_listeners):
        try:
            callback(event)
        except Exception as exc:
            print(f"Status listener error: {exc}")


def set_microphone_muted(muted: bool) -> bool:
    """Returns False if the active wake engine can't be muted."""
    if not hasattr(wake_engine, "set_muted"):
        return False
    wake_engine.set_muted(muted)
    emit_status("ARYA_MUTED" if muted else "ARYA_UNMUTED")
    return True


async def _send_all(payload: str | bytes) -> None:
    if isinstance(payload, str):
        emit_status(payload)
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

async def interrupt() -> None:
    """Silence ARYA and cancel the in-flight turn (barge-in)."""
    if _current_turn is not None and not _current_turn.done():
        speaker.stop()
        _current_turn.cancel()
        try:
            await _current_turn
        except (asyncio.CancelledError, Exception):
            pass
        await _send_all("ARYA_INTERRUPT")


async def _dispatch(message: str, wake_triggered: bool = False, announce_transcript: bool = False) -> None:
    """Cancel whatever ARYA is doing, then start the new turn."""
    global _current_turn
    await interrupt()

    # While ARYA waits for "yes"/"no" on an action, "stop"/"cancel"/"no thanks"
    # are answers to that question — let the turn pipeline handle them.
    if not policy.has_pending() and _classify_closing(message) == "stop":
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

            closing = None if policy.has_pending() else _classify_closing(message)
            if closing in ("end", "thanks"):
                wake_engine.end_conversation()
                if closing == "thanks":
                    await _say(THANKS_ACK)
                return
            if closing == "ack":
                return          # "Okay." — nothing to answer; the conversation stays open

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
                    left = getattr(wake_engine, "followup_seconds_left", lambda: 0)() or 12
                    await _send_all(f"ARYA_FOLLOWUP:{int(left)}")
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
    client = websocket.client.host if websocket.client else "?"
    origin = websocket.headers.get("origin")
    if not security.origin_ok(origin):
        log_event("connection_rejected", client=client, origin=origin, reason="foreign origin")
        print(f"Security: rejected WebSocket from foreign origin {origin!r}")
        await websocket.close(code=1008)
        return
    if not security.token_ok(websocket.query_params.get("token")):
        log_event("connection_rejected", client=client, origin=origin, reason="bad or missing token")
        print(f"Security: rejected WebSocket from {client} (bad or missing token)")
        await websocket.close(code=1008)
        return
    await websocket.accept()
    log_event("connected", client=client)
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
