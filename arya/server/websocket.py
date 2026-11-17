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
import time
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from arya.core import addressee, policy
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
# A little variety (all pre-synthesized, so still instant); late at night ARYA notices once.
WAKE_ACKS = ["Yes?", "I'm here.", "Listening.", "What's up?", "Mm-hm?"]
THANKS_ACKS = ["Anytime.", "Happy to help.", "You're welcome!", "Anytime, that's what I'm here for."]
NIGHT_ACK = "Still up? I'm listening."
_last_ack = {"wake": "", "thanks": "", "night_day": ""}


def _pick(kind: str, options: list[str]) -> str:
    import random
    choice = random.choice([o for o in options if o != _last_ack[kind]] or options)
    _last_ack[kind] = choice
    return choice


def wake_ack() -> str:
    now = datetime.now()
    if now.hour < 4 and _last_ack["night_day"] != now.date().isoformat():
        _last_ack["night_day"] = now.date().isoformat()
        return NIGHT_ACK
    return _pick("wake", WAKE_ACKS)


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
    global _loop
    loop = asyncio.get_event_loop()
    _loop = loop

    from arya.core.stt import WHISPER_MODEL
    print(f"STT: loading Whisper {WHISPER_MODEL} (CUDA)...")
    await loop.run_in_executor(None, load_model)
    print("STT: model ready.")

    await loop.run_in_executor(None, load_embedder)      # prints its own loading / ready lines

    async def on_wake(command: str, wake_triggered: bool) -> None:
        """Called by WakeWordEngine for wake-word hits and follow-up speech.
        Follow-ups (no wake word) must pass the addressee check first."""
        named = False
        if not wake_triggered and command:
            # "Area." / "Uh, Aria?" = you called ARYA; "Area, play …" / "I'll play …" = a request to ARYA.
            command, named = addressee.split_name(command)
            if named and not command:
                wake_triggered = True                    # just the name: answer like a wake word ("Yes?")
        if not wake_triggered and command and not named and _smart_listening and not _answers_bare_wake(command):
            # A request split by a pause ("… by Kishore Kumar." + "on YouTube music."): judge the pieces together.
            now = time.monotonic()
            joined = (_fragment["text"] + " " + command) if _fragment["text"] and \
                now - _fragment["at"] < FRAGMENT_JOIN_S else None
            if joined and await _meant_for_arya(joined, quiet=True):
                command = joined
            elif not await _meant_for_arya(command):
                _fragment.update(text=(joined or command)[-300:], at=now)
                return
            _fragment.update(text="", at=0.0)
        await _dispatch(command, wake_triggered=wake_triggered, announce_transcript=True)

    wake_engine.set_callback(on_wake, loop)
    if hasattr(wake_engine, "set_interrupt_callback"):
        # Dedicated detector: go quiet the instant "Hey ARYA" is heard mid-reply.
        wake_engine.set_interrupt_callback(interrupt)
    security.install_log_redaction()             # no ?token=… in the console log
    # Sources / visuals produced while answering go to the app window.
    import json as _json
    import arya.core.llm as _llm

    _llm.set_artifact_sink(lambda kind, payload: emit_status(f"ARYA_{kind}:{_json.dumps(payload)}"))
    _start_knowledge_indexing()

    if hasattr(wake_engine, "set_event_callback"):
        wake_engine.set_event_callback(emit_status)
    wake_engine.start()

    set_notify_callback(announce)

    async def _restore_scheduled() -> None:
        await asyncio.sleep(5)                       # let the voice finish loading before announcing
        from arya.tools.productivity import restore_scheduled
        await restore_scheduled()

    asyncio.create_task(_restore_scheduled())        # timers/reminders that survived a restart

    from arya.tools import music                     # "Hey ARYA" pauses the music so the mic hears you

    def _duck_music(event: str) -> None:
        if event == "ARYA_LISTENING" and _loop is not None:
            asyncio.run_coroutine_threadsafe(music.duck(busy=_voice_lock.locked), _loop)

    add_status_listener(_duck_music)

    from arya.core import proactive                  # battery, breaks, due cards — spoken only when it helps
    asyncio.create_task(proactive.run(announce, busy=_voice_lock.locked))

    from arya.knowledge import lectures              # lecture recordings -> library, while you're away
    asyncio.create_task(lectures.run(busy=_voice_lock.locked))

    from arya.core import upkeep                     # daily backup of arya.db and your notes (last 7 days)
    asyncio.create_task(upkeep.run())

    from arya.core import intercom                   # "say … on the laptop" from the phone, "where's my laptop?"
    intercom.set_speaker(speak_on_laptop)

    from arya.tools import remote                    # "put the laptop to sleep in 10 minutes": warned aloud first
    remote.set_announcer(announce)

    from arya.core import activity                   # time at the laptop, "remind me when I'm back", evening wrap-up
    asyncio.create_task(activity.run(announce, busy=_voice_lock.locked))

    from arya.core import quiet                      # meeting mode, and quiet during Teams / Zoom calls
    asyncio.create_task(quiet.run(lambda text: emit_status("NOTIFY:" + text)))

    from arya.eda import projects as eda             # KiCad: checks each time you save, speaks only about new errors
    asyncio.create_task(eda.run(announce, busy=_voice_lock.locked))

    from arya.core import capture                    # "record a voice note" … "stop recording" (the mic is the engine's)
    capture.set_engine(wake_engine)
    capture.set_announcer(announce)

    from arya.core import attachments                # uploads: transcription progress goes to the app
    attachments.set_event_sink(emit_status)

    from arya.memory import learning                 # learns from finished conversations while ARYA is idle
    asyncio.create_task(learning.run(busy=_voice_lock.locked))

    from arya.core import modes                      # everyday / study / work: the app and phone show the mode
    modes.on_change(lambda m: emit_status(f"ARYA_AMODE:{m}"))

    from arya.server import phone                    # your phone on home Wi-Fi (only when turned on, Private networks)
    phone.set_turn_handler(phone_turn)
    phone.set_upload_handler(receive_upload)         # photos and files sent from the phone
    add_status_listener(phone.note_event)
    asyncio.create_task(phone.run())

    # Pre-synthesize the phrases ARYA says instantly (cached in tts.py).
    async def _warm_cache() -> None:
        for phrase in (*WAKE_ACKS, *THANKS_ACKS, NIGHT_ACK):
            await _synthesize(phrase)

    asyncio.create_task(_warm_cache())
    if ARYA_MODE == "offline":
        asyncio.create_task(_warm_up_or_keep_trying())    # load the local model into VRAM now
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


async def _warm_up_or_keep_trying() -> None:
    """Load the model now; if memory is short, keep trying in the background and say when it's back."""
    if await warm_up_local_llm():
        return
    from arya.core.llm_local import keep_trying

    def ready() -> None:
        print("LLM: the local model loaded after all — answers work again")
        emit_status("NOTIFY:My brain is back: the language model just loaded, so I can answer questions again.")
    await keep_trying(busy=_voice_lock.locked, on_ready=ready)


def _page(request: Request, filename: str) -> HTMLResponse:
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    # The page gets the WebSocket token only when loaded on this machine.
    # (Other web pages can't read this response — the browser's same-origin
    # rule and the Host check above stop them.)
    if security.is_local_client(request.client.host if request.client else None):
        tag = f'<script>window.ARYA_TOKEN = "{security.TOKEN}";</script>'
        html = html.replace("</head>", tag + "\n</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/")
async def index(request: Request) -> HTMLResponse:
    return _page(request, "app.html")


@app.get("/classic")
async def classic(request: Request) -> HTMLResponse:
    return _page(request, "index.html")


@app.get("/phone")
async def phone_pairing(request: Request) -> HTMLResponse:
    """The QR code that pairs your phone — like the token in the app page, only for this machine."""
    from arya.server import phone

    if not security.is_local_client(request.client.host if request.client else None):
        return HTMLResponse("Not here.", status_code=403)
    return HTMLResponse(await asyncio.to_thread(phone.pairing_page),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# ── App API (token-protected) ─────────────────────────────────────────────────

def _authorised(request: Request) -> bool:
    token = request.headers.get("x-arya-token") or request.query_params.get("token")
    return security.token_ok(token) and security.origin_ok(request.headers.get("origin"))


def _deny() -> JSONResponse:
    return JSONResponse({"error": "unauthorised"}, status_code=401)


@app.get("/api/status")
async def api_status(request: Request) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    import arya.core.llm as llm
    from arya.core import tts

    engine = type(wake_engine).__name__
    return JSONResponse({
        "mode": llm.ARYA_MODE,
        "brain": OLLAMA_MODEL,
        "cloud_brain": llm.INTENT_MODEL_NAME,
        "cloud_configured": bool(os.getenv("GROQ_API_KEY")),
        "voice": tts.PIPER_VOICE if tts.TTS_ENGINE != "edge" else tts.VOICE_NAME,
        "voice_engine": "Piper (local)" if tts.TTS_ENGINE != "edge" else "Microsoft Edge (online)",
        "wake_engine": "Dedicated detector + verification" if engine == "HotwordEngine" else "Whisper-based",
        "muted": bool(getattr(wake_engine, "is_muted", lambda: False)()),
        "conversation_seconds_left": int(getattr(wake_engine, "followup_seconds_left", lambda: 0)()),
        "local_only": security.is_loopback_bind(),
        "pending_confirmation": policy.has_pending(),
        "smart_listening": _smart_listening,
        "assistant_mode": _assistant_mode(),
    })


def _assistant_mode() -> str:
    from arya.core import modes
    return modes.current_name()


@app.post("/api/assistant-mode")
async def api_assistant_mode(request: Request) -> JSONResponse:
    """The mode switch in the app header: default / study / work (arya/core/modes.py)."""
    if not _authorised(request):
        return _deny()
    from arya.core import modes
    body = await request.json()
    mode = str(body.get("mode", "")).lower()
    if mode not in modes.MODES:
        return JSONResponse({"error": "mode must be default, study, work or brainstorm"}, status_code=400)
    modes.set_mode(mode, reason="app")
    return JSONResponse({"ok": True, "mode": mode})


@app.post("/api/mute")
async def api_mute(request: Request) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    body = await request.json()
    ok = set_microphone_muted(bool(body.get("muted")))
    return JSONResponse({"ok": ok})


@app.post("/api/mode")
async def api_mode(request: Request) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    body = await request.json()
    mode = str(body.get("mode", "")).lower()
    if mode not in ("offline", "cloud"):
        return JSONResponse({"error": "mode must be offline or cloud"}, status_code=400)
    set_privacy_mode(mode)
    return JSONResponse({"ok": True, "mode": mode})


# ── Knowledge library ─────────────────────────────────────────────────────────

_kb_last_emit = 0.0


def _kb_progress(st) -> None:
    """Throttled progress events while indexing (and one when finished)."""
    global _kb_last_emit
    import json as _json
    from arya.knowledge import library

    now = time.monotonic()
    if st.running and now - _kb_last_emit < 1.5:
        return
    _kb_last_emit = now
    emit_status("ARYA_KB:" + _json.dumps(library.stats()))


def _start_knowledge_indexing() -> None:
    try:
        from arya.knowledge import library

        library.start_background_indexing(progress=_kb_progress)
    except Exception as exc:
        print(f"Knowledge indexing not started: {exc}")


MAX_PHOTO_BYTES = 15 * 1024 * 1024


@app.post("/api/photo")
async def api_photo(request: Request) -> JSONResponse:
    """A photo dropped or pasted into the app window: read with OCR on this laptop, shown, kept as context."""
    if not _authorised(request):
        return _deny()
    import base64
    import binascii

    body = await request.json()
    data = str(body.get("data") or "")
    data = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return JSONResponse({"error": "That isn't an image."}, status_code=400)
    if not raw or len(raw) > MAX_PHOTO_BYTES:
        return JSONResponse({"error": "The photo is empty or larger than 15 MB."}, status_code=413)
    name = re.sub(r"[^\w .-]", "", str(body.get("name") or "photo"))[:60] or "photo"
    from arya.tools import screen
    import arya.core.llm as _llm

    try:
        info = await asyncio.to_thread(screen.add_photo, raw, name)
    except Exception as exc:
        return JSONResponse({"error": f"I couldn't open that image ({type(exc).__name__})."}, status_code=400)
    _llm.emit_artifact("VISUAL", {"type": "image", "id": info["image_id"], "caption": info["title"]})
    log_event("photo_added", chars=info["chars"])
    return JSONResponse(info)


@app.get("/api/stats")
async def api_stats(request: Request) -> JSONResponse:
    """The week panel in the app: time at the laptop by mode, study, cards, to-dos done, streak."""
    if not _authorised(request):
        return _deny()
    from arya.core import activity
    return JSONResponse(await asyncio.to_thread(activity.week_stats))


@app.post("/api/upload")
async def api_upload(request: Request, name: str = "") -> JSONResponse:
    """A document, photo, audio or video from the app (📎, drop, paste): streamed to disk, then read
    (arya/core/attachments.py). Raw body, so a 400 MB video never sits in memory."""
    if not _authorised(request):
        return _deny()
    return await receive_upload(request, name)


async def receive_upload(request: Request, name: str) -> JSONResponse:
    """Shared by the app and the phone (arya/server/phone.py)."""
    import uuid
    from arya.core import attachments

    kind = attachments.kind_of(name or "")
    if kind is None:
        return JSONResponse({"error": "I can read PDFs, Word, PowerPoint, text, photos, audio and video."},
                            status_code=415)
    limit = attachments.limit_for(kind)
    declared = int(request.headers.get("content-length") or 0)
    if declared > limit:
        return JSONResponse({"error": f"That file is larger than {limit // (1024 * 1024)} MB."}, status_code=413)
    if not attachments.room_for(declared or limit):
        return JSONResponse({"error": "There isn't enough free disk space for that file."}, status_code=507)
    incoming = attachments.uploads_dir() / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp = incoming / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise ValueError("too large")
                f.write(chunk)
    except ValueError:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": f"That file is larger than {limit // (1024 * 1024)} MB."}, status_code=413)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": f"The upload broke off ({type(exc).__name__})."}, status_code=400)
    if size == 0:
        tmp.unlink(missing_ok=True)
        return JSONResponse({"error": "The file is empty."}, status_code=400)
    try:
        info = await attachments.accept(tmp, name, as_note=request.query_params.get("note") == "1")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"Upload failed: {type(exc).__name__}: {exc}")
        return JSONResponse({"error": f"I couldn't read that file ({type(exc).__name__})."}, status_code=400)
    if "error" in info:
        return JSONResponse(info, status_code=400)
    if info.get("kind") == "photo":
        import arya.core.llm as _llm
        _llm.emit_artifact("VISUAL", {"type": "image", "id": info["image_id"], "caption": info["title"]})
    log_event("upload", what=info.get("kind"), bytes=size)                 # never the file name or content
    return JSONResponse(info)


@app.get("/api/progress")
async def api_progress(request: Request) -> JSONResponse:
    """The Study card: exams, per-course practice, streak, last 7 days."""
    if not _authorised(request):
        return _deny()
    from arya.study import planner

    return JSONResponse(await asyncio.to_thread(planner.progress))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """For the watchdog (arya/desktop/supervisor.py): the server answers, the event loop is alive. No data."""
    return JSONResponse({"ok": True})


@app.get("/api/kb/status")
async def api_kb_status(request: Request) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    from arya.knowledge import library

    return JSONResponse(library.stats())


@app.post("/api/kb/reindex")
async def api_kb_reindex(request: Request) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    _start_knowledge_indexing()
    log_event("knowledge_reindex")
    return JSONResponse({"ok": True})


@app.get("/api/kb/page")
async def api_kb_page(request: Request, doc: int, page: int, q: str = "") -> Response:
    """A page of a course PDF as an image, with the cited passage highlighted."""
    if not _authorised(request):
        return _deny()
    from arya.knowledge import library

    try:
        png = await asyncio.to_thread(library.render_page, doc, page, q)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=404)
    if png is None:
        return JSONResponse({"error": "no preview for this page"}, status_code=404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/kb/passage")
async def api_kb_passage(request: Request, doc: int, page: int) -> JSONResponse:
    """Text of a page (for Word / PowerPoint / notes sources that have no page image)."""
    if not _authorised(request):
        return _deny()
    from arya.knowledge import library

    with library._connect() as c:
        rows = c.execute("SELECT text FROM chunks WHERE doc_id = ? AND page = ? ORDER BY id", (doc, page)).fetchall()
        meta = library.document(doc)
    if meta is None:
        return JSONResponse({"error": "unknown document"}, status_code=404)
    return JSONResponse({"title": meta["title"], "kind": meta["kind"], "pages": meta["pages"],
                         "text": "\n\n".join(r["text"] for r in rows)})


@app.get("/api/visual/{image_id}")
async def api_visual(request: Request, image_id: str) -> Response:
    if not _authorised(request):
        return _deny()
    from arya.knowledge import visuals

    png = visuals.image(image_id)
    if png is None:
        return JSONResponse({"error": "expired"}, status_code=404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/smart")
async def api_smart(request: Request) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    body = await request.json()
    set_smart_listening(bool(body.get("enabled")))
    return JSONResponse({"ok": True, "enabled": _smart_listening})


@app.get("/api/history")
async def api_history(request: Request, limit: int = 30) -> JSONResponse:
    if not _authorised(request):
        return _deny()
    from arya.memory.database import get_history

    return JSONResponse({"messages": get_history(limit=max(1, min(limit, 100)))})


_ACTIVITY_KINDS = {"tool", "confirmation_requested", "confirmation_approved", "confirmation_declined", "ignored_speech",
                   "blocked", "cloud_request", "connection_rejected"}


@app.get("/api/activity")
async def api_activity(request: Request, limit: int = 20) -> JSONResponse:
    """Recent entries from the local action log — what ARYA actually did."""
    if not _authorised(request):
        return _deny()
    import json as _json

    items = []
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-400:]
    except OSError:
        lines = []
    for line in reversed(lines):
        try:
            entry = _json.loads(line)
        except ValueError:
            continue
        if entry.get("kind") in _ACTIVITY_KINDS:
            items.append(entry)
            if len(items) >= max(1, min(limit, 100)):
                break
    return JSONResponse({"items": items})


@app.post("/api/window/show")
async def api_window_show(request: Request) -> JSONResponse:
    """A second launch (e.g. the desktop shortcut) asks the running app to show its window."""
    if not _authorised(request):
        return _deny()
    emit_status("__SHOW_WINDOW__", to_browsers=False)
    return JSONResponse({"ok": True})


# ── Fan-out to browser windows ────────────────────────────────────────────────

# In-process status listeners (the tray icon / overlay). They receive every
# text event the browser gets, plus local-only ones (ARYA_READY, ARYA_LISTENING)
# that the browser page doesn't understand. Called from any thread — listeners
# must be thread-safe (the desktop app just puts them on a queue).
_status_listeners: list = []


def add_status_listener(callback) -> None:
    _status_listeners.append(callback)


_loop: asyncio.AbstractEventLoop | None = None


def emit_status(event: str, to_browsers: bool = True) -> None:
    """Tell in-process listeners and (optionally) every open app window.
    Safe to call from any thread (audio thread, tray menu, event loop)."""
    for callback in list(_status_listeners):
        try:
            callback(event)
        except Exception as exc:
            print(f"Status listener error: {exc}")
    if to_browsers and _loop is not None and _connections:
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(event), _loop)
        except RuntimeError:
            pass


def set_microphone_muted(muted: bool) -> bool:
    """Returns False if the active wake engine can't be muted."""
    if not hasattr(wake_engine, "set_muted"):
        return False
    wake_engine.set_muted(muted)
    emit_status("ARYA_MUTED" if muted else "ARYA_UNMUTED")
    return True


# ── Smart listening (addressee detection) ────────────────────────────────────

_smart_listening = addressee.SMART_LISTENING_DEFAULT
_ignored_in_a_row = 0
ANSWER_WINDOW_S = 15.0       # ARYA just asked a question: what you say in the next 15 s is the answer
FRAGMENT_JOIN_S = 4.0        # an ignored piece + the next one within 4 s are judged as one sentence
_fragment = {"text": "", "at": 0.0}
_last_spoke_at = 0.0          # when ARYA last finished speaking (monotonic)
# After a bare "Hey ARYA" ("Yes?"), the next sentence within this window is the
# request itself — it skips the "was that for me?" check.
# (Wall clock: monotonic time can stand still while the laptop sleeps. 20 s, and filler like "That's
# what, um." doesn't use it up — from the log, the real request came 23 s after "Hey ARYA" and was ignored.)
BARE_WAKE_WINDOW_S = 20.0
_bare_wake_until = 0.0


_FILLER_WORDS = addressee._VAGUE | {"that's", "what's", "it's", "i'm", "um", "uh", "er", "erm", "hmm", "mm", "huh",
                                    "like", "so", "oh", "ah", "i", "a", "an", "uhm", "umm"}


def _is_filler(text: str) -> bool:
    """'That's what, um.' -> True; 'open it' / 'next song' -> False."""
    words = re.findall(r"[a-z][a-z']*", text.lower().replace("’", "'"))
    return all(w in _FILLER_WORDS for w in words)


def _answers_bare_wake(command: str = "") -> bool:
    """True once for the first real follow-up right after a bare wake word."""
    global _bare_wake_until
    if _bare_wake_until and time.time() < _bare_wake_until:
        if command and _is_filler(command):
            return False                     # filler: keep waiting for the actual request
        _bare_wake_until = 0.0
        return True
    return False


def set_smart_listening(enabled: bool) -> None:
    global _smart_listening
    _smart_listening = bool(enabled)
    log_event("smart_listening", enabled=_smart_listening)
    print(f"Smart listening: {'on' if _smart_listening else 'off'}")
    emit_status(f"ARYA_SMART:{'on' if _smart_listening else 'off'}")


async def _meant_for_arya(command: str, quiet: bool = False) -> bool:
    """Decide whether a follow-up was said to ARYA. Ignored speech is never
    answered, never saved to history, and doesn't keep the conversation open;
    a few ignored in a row end the conversation."""
    global _ignored_in_a_row
    from arya.memory.database import get_history

    recent = get_history(limit=4)
    last_reply = next((m["content"] for m in reversed(recent) if m["role"] == "assistant"), "")
    since = time.monotonic() - _last_spoke_at if _last_spoke_at else None
    from arya.tools import alarms, browser
    # ARYA is waiting for an answer: a yes/no, the song or the reminder time it asked for, or any question it asked.
    expecting = (policy.has_pending() or browser.awaiting_song() or alarms.awaiting()
                 or (last_reply.rstrip().endswith("?") and since is not None and since < ANSWER_WINDOW_S))
    ok, why = await addressee.is_addressed(command, last_reply=last_reply, seconds_since_reply=since,
                                           recent=recent, pending_confirmation=expecting)
    if ok:
        _ignored_in_a_row = 0
        return True
    if quiet:                                    # only a trial (two fragments joined): no side effects
        return False
    _ignored_in_a_row += 1
    print(f"Not for ARYA ({why}) — staying quiet: {command!r}")
    log_event("ignored_speech", chars=len(command), reason=why)      # what was said is not logged
    await _send_all(f"ARYA_IGNORED:{command}")
    if _ignored_in_a_row >= addressee.IGNORED_LIMIT:
        _ignored_in_a_row = 0
        wake_engine.end_conversation()
        await _send_all("ARYA_CONVERSATION_ENDED")
    return False


def set_privacy_mode(mode: str) -> None:
    """offline / cloud — from the app window or the tray; everyone is told."""
    import arya.core.llm as llm

    llm.ARYA_MODE = mode
    log_event("privacy_mode", mode=mode)
    print(f"Privacy mode: {mode}")
    emit_status(f"ARYA_MODE:{mode}")


async def _send_all(payload: str | bytes) -> None:
    if isinstance(payload, str):
        emit_status(payload, to_browsers=False)
    await _broadcast(payload)


async def _broadcast(payload: str | bytes) -> None:
    for ws in list(_connections):
        try:
            if isinstance(payload, bytes):
                await ws.send_bytes(payload)
            else:
                await ws.send_text(payload)
        except Exception:
            _connections.discard(ws)


# ── Speech pipeline ───────────────────────────────────────────────────────────

# One sentence is synthesized at a time, in order (asyncio's semaphore wakes waiters first-come), and its
# 10 s timeout starts when *its* synthesis starts. From the log: a long answer arrived at once, every
# sentence's task queued on Piper's lock while its own timeout ran, and every sentence timed out.
_synth_slot: asyncio.Semaphore | None = None


async def _synthesize(sentence: str) -> bytes:
    global _synth_slot
    if _synth_slot is None:
        _synth_slot = asyncio.Semaphore(1)
    try:
        async with _synth_slot:
            return await asyncio.wait_for(speak_sentence(sentence), timeout=10.0)
    except asyncio.TimeoutError:
        print(f"TTS timeout: {sentence[:30]}")
    except Exception as exc:
        print(f"TTS error: {exc}")
    return b""


async def _aiter(items: Iterable[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


MAX_SPOKEN_SENTENCES = int(os.getenv("ARYA_MAX_SPOKEN_SENTENCES", "5"))      # 0 = no limit
DETAIL_SPOKEN_SENTENCES = 12
REST_ON_SCREEN = "The rest is on screen."


def spoken_limit(message: str) -> int:
    """How many sentences ARYA says aloud; the whole reply is always shown on screen."""
    from arya.core.llm import wants_detail

    if MAX_SPOKEN_SENTENCES <= 0:
        return 0
    return max(MAX_SPOKEN_SENTENCES, DETAIL_SPOKEN_SENTENCES) if wants_detail(message) else MAX_SPOKEN_SENTENCES


FIRST_CHUNK_MIN_WORDS = 12   # a first sentence longer than this is voiced in two parts


def split_first_for_voice(sentence: str) -> list[str]:
    """Split a long first sentence at its first clause break (comma, semicolon, dash) so its first
    half is synthesised — and heard — while the second half is still being voiced. Shown text is
    unchanged; Piper pauses at the comma anyway, so it sounds the same."""
    words = sentence.split()
    if len(words) <= FIRST_CHUNK_MIN_WORDS:
        return [sentence]
    for m in re.finditer(r"(?<=[,;:])\s+|\s+(?=[—–-]\s)", sentence):
        head, tail = sentence[:m.start()].strip(), sentence[m.end():].strip()
        if len(head.split()) >= 4 and len(tail.split()) >= 4:
            return [head, tail]
    return [sentence]


async def _speak_stream(sentences: AsyncIterator[str], max_spoken: int = 0, started: float | None = None) -> None:
    """Text goes to the UI the moment each sentence exists, its TTS starts
    immediately, and audio plays in order as soon as it is ready — so ARYA
    starts talking after the *first* sentence, not after the whole reply.
    After `max_spoken` sentences (0 = no limit) the rest is only shown.
    With `started`, logs how long the first sentence and the first sound took."""
    queue: asyncio.Queue[asyncio.Task | None] = asyncio.Queue()
    marks: dict[str, float] = {}
    from arya.core import quiet
    silent = quiet.active()                            # meeting mode: the answer is shown, not said

    async def produce() -> None:
        spoken, cut = 0, False
        try:
            async for sentence in sentences:
                marks.setdefault("text", time.monotonic())
                await _send_all(sentence)
                if silent:
                    continue
                if max_spoken and spoken >= max_spoken:
                    if not cut and re.search(r"\w", sentence):
                        cut = True
                        await queue.put((REST_ON_SCREEN, asyncio.create_task(_synthesize(REST_ON_SCREEN))))
                    continue
                parts = split_first_for_voice(sentence) if spoken == 0 else [sentence]
                for part in parts:                     # a long opener is voiced clause-first: sound sooner
                    await queue.put((part, asyncio.create_task(_synthesize(part))))
                if re.search(r"[A-Za-z]{2,}", sentence):     # a formula line or "1." isn't a sentence
                    spoken += 1
        finally:
            await queue.put(None)

    producer = asyncio.create_task(produce())
    announced = False
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            sentence, task = item
            audio = await task
            if not audio:
                continue
            if hasattr(wake_engine, "note_spoken"):
                wake_engine.note_spoken(sentence)       # so ARYA's own "stop" isn't taken as yours
            if "audio" not in marks:
                marks["audio"] = time.monotonic()
                if started is not None and "text" in marks:
                    print(f"Latency: first words after {marks['audio'] - started:.2f}s "
                          f"(answer {marks['text'] - started:.2f}s + voice {marks['audio'] - marks['text']:.2f}s)")
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
                pending[1].cancel()
        raise


async def _say(text: str) -> None:
    await _speak_stream(_aiter([text]))


LOW_MEMORY_GB = 1.5   # below this the local model (~2.5 GB) usually can't load, or its server crashes


from arya.tools.remote import memory_hogs   # the biggest closable apps (shared with remote control)


def _model_down_message(detail: str) -> str:
    """Say *why* the local model failed: low memory, Ollama not running, or a model missing."""
    d = detail.lower()
    free_gb = ollama_running = None
    try:
        import psutil

        free_gb = psutil.virtual_memory().available / 2**30
        ollama_running = any((p.info["name"] or "").lower().startswith("ollama")
                             for p in psutil.process_iter(["name"]))
    except Exception:
        pass
    print(f"Model unavailable — free RAM: {free_gb if free_gb is None else round(free_gb, 2)} GB, "
          f"Ollama running: {ollama_running}")
    if ("more system memory" in d or "out of memory" in d or "cuda error" in d
            or (free_gb is not None and free_gb < LOW_MEMORY_GB)):
        free = f", only {free_gb:.1f} gigabytes are free" if free_gb is not None else ""
        hogs = memory_hogs()
        which = (f" The biggest right now: {hogs}." if hogs else " Close a few apps or browser tabs.")
        return (f"My local model couldn't run because the computer is low on memory{free}.{which} "
                "I keep trying in the background and will tell you when I'm back.")
    if "not found" in d and "ollama pull" in d:
        return "My local model isn't downloaded yet. Run ollama pull, then ask me again."
    if ollama_running is False:
        return "My local model isn't running. Start Ollama and ask me again."
    return "My local model isn't responding right now; it may be restarting. Ask me again in a moment."


def _error_message(exc: Exception) -> str:
    if isinstance(exc, LLMUnavailableError):
        return _model_down_message(str(exc))
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


def _resume_music() -> None:
    """Music ARYA paused for 'Hey ARYA' comes back a few seconds after the answer."""
    try:
        from arya.tools import music
        music.resume_later()
    except Exception as exc:
        print(f"Music: resume skipped ({type(exc).__name__})")


async def _dispatch(message: str, wake_triggered: bool = False, announce_transcript: bool = False) -> None:
    """Cancel whatever ARYA is doing, then start the new turn."""
    global _current_turn
    await interrupt()

    # While ARYA waits for "yes"/"no" on an action, "stop"/"cancel"/"no thanks"
    # are answers to that question — let the turn pipeline handle them.
    if not policy.has_pending() and _classify_closing(message) == "stop":
        print(f"Stopped by request: {message[:40]!r}")
        speaker.stop()
        wake_engine.end_conversation()
        _resume_music()
        if announce_transcript:
            await _send_all(f"TRANSCRIPT:{_as_heard(message)}")
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
                global _bare_wake_until
                await _say(wake_ack())
                _bare_wake_until = time.time() + BARE_WAKE_WINDOW_S   # the next sentence IS the request
                return

            if announce_transcript:
                await _send_all(f"TRANSCRIPT:{_as_heard(message)}")

            closing = None if policy.has_pending() else _classify_closing(message)
            if closing in ("end", "thanks"):
                wake_engine.end_conversation()
                if closing == "thanks":
                    await _say(_pick("thanks", THANKS_ACKS))
                return
            if closing == "ack":
                return          # "Okay." — nothing to answer; the conversation stays open

            print(f"Processing: {message[:60]}")
            try:
                await _speak_stream(stream_with_tools(message), max_spoken=spoken_limit(message),
                                    started=time.monotonic())
                print("Response complete")
                await _maybe_suggest_mode(message)
                if policy.has_pending():
                    await _send_all("ARYA_CONFIRM")     # app shows Yes / No buttons
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Pipeline error: {exc}")
                await _say(_error_message(exc))
        finally:
            global _last_spoke_at
            _last_spoke_at = time.monotonic()
            wake_engine.set_speaking(False)
            _resume_music()
            try:
                await _send_all("ARYA_DONE")
                if wake_engine.followup_active():
                    left = getattr(wake_engine, "followup_seconds_left", lambda: 0)() or 12
                    await _send_all(f"ARYA_FOLLOWUP:{int(left)}")
            except Exception:
                pass


async def speak_on_laptop(text: str, sound: bytes, shown: str) -> None:
    """Intercom / find my laptop (arya/core/intercom.py): a chime or beeps, then the words, aloud on the laptop.
    Waits for the turn that asked for it to finish (it holds the voice lock). Shown in the app only — the phone
    that sent it doesn't need its own message back."""
    from arya.core import quiet
    if quiet.active():                                 # meeting mode: on the laptop screen, not out loud
        await _broadcast("NOTIFY:" + shown)
        return
    async with _voice_lock:
        wake_engine.set_speaking(True)
        try:
            await _broadcast("NOTIFY:" + shown)
            if LOCAL_AUDIO:
                await speaker.play(sound)
                await speaker.play(await _synthesize(text))
            else:
                await _send_all(sound)
                audio = await _synthesize(text)
                if audio:
                    await _send_all(audio)
        except Exception as exc:
            print(f"Intercom failed: {exc}")
        finally:
            wake_engine.set_speaking(False)
            await _send_all("ARYA_DONE")


def _as_heard(message: str) -> str:
    """The transcript shown in the app: what you said ('Kal ek bajhe mujhe yaad dilana'), not ARYA's English."""
    from arya.core import language
    return language.said(message) or message


async def _maybe_suggest_mode(message: str) -> None:
    """After an answer: if your last requests clearly belong to another mode, ask once (arya/core/modes.py).
    Never instead of a question ARYA is already waiting on."""
    if policy.has_pending():
        return
    try:
        from arya.core import modes
        target = await asyncio.to_thread(modes.suggestion, message)
    except Exception as exc:
        print(f"Mode suggestion skipped: {type(exc).__name__}: {exc}")
        return
    if target:
        question = modes.suggestion_question(target)
        print(f"Mode: suggesting {target}")
        policy.offer("switch_mode", {"mode": target}, question)
        await _say(question)


async def phone_turn(message: str) -> dict:
    """A request typed on the phone (arya/server/phone.py): answered as text, nothing spoken on the laptop.
    Waits for ARYA to finish speaking here first (one turn at a time); files, clipboard and commands are
    blocked for it (policy.REMOTE_BLOCKED)."""
    refusal = policy.remote_refusal(message)
    if refusal:
        print(f"Phone request: {message[:60]}")
        print("Instant answer: blocked from the phone")
        return {"reply": refusal, "confirm": False}
    async with _voice_lock:
        flag = policy.remote_turn.set(True)
        try:
            print(f"Phone request: {message[:60]}")
            sentences = [s async for s in stream_with_tools(message)]
        except Exception as exc:
            print(f"Phone pipeline error: {exc}")
            sentences = [_error_message(exc)]
        finally:
            policy.remote_turn.reset(flag)
    reply = re.sub(r"[ \t]+\n", "\n", " ".join(s.strip() for s in sentences if s.strip()))
    return {"reply": reply or "Done.", "confirm": policy.has_pending()}


async def announce(text: str) -> None:
    """Speak a timer/reminder/pomodoro alert — works with no browser open."""
    from arya.core import quiet
    if quiet.active():                                 # meeting mode: the phone (vibrates) and the app, no sound
        await _send_all("NOTIFY:" + text)
        return
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
