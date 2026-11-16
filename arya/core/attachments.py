"""
Uploads (roadmap PR 4): documents, photos, audio and video you give ARYA as context — from the app window
(📎 button, drag and drop, paste) and, later, from the phone.

  document  .pdf .docx .pptx .txt .md   saved in %LOCALAPPDATA%\\ARYA\\uploads\\<date>\\ and added to the library
                                        (source "uploads"): ARYA answers from it and cites the page
  photo     .png .jpg .jpeg .webp .gif .bmp   read with OCR on this laptop, kept in memory for 30 minutes;
                                        a photo with little text is described by the cloud only after you say yes;
                                        "add it to my library" keeps its text
  audio / video  .mp3 .m4a .wav .ogg .flac .aac .mp4 .mkv .webm .mov .avi .wmv
                                        transcribed in the background with ARYA's own Whisper (progress in the
                                        app), plus text read from a few frames of a video; the transcript is
                                        kept, the copy of the recording is deleted (disk space)

After an upload, questions about "the document", "the video", "this file" — or any question in the next ten
minutes — are answered from it first. Uploaded text is untrusted: instructions written inside a file are never
followed, and actions after reading it need your "yes" (arya/core/policy.py).

Voice: "what files did I give you?", "how far is the video?", "delete that upload".
"""
from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DOC_EXT = {".pdf", ".docx", ".pptx", ".txt", ".md"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_DOC_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_MEDIA_BYTES = 400 * 1024 * 1024
MAX_MEDIA_S = 90 * 60
KEEP_FREE_BYTES = 1_500_000_000           # never fill the disk: Whisper, Ollama and Windows need room
ACTIVE_S = 2 * 60 * 60                    # an upload stays "the file we're talking about" this long
RECENT_S = 10 * 60                        # … and any question in the first ten minutes is about it
PHOTO_KEEP_S = 30 * 60
VIDEO_FRAMES = 6


def media_ext() -> set[str]:
    from arya.knowledge.lectures import MEDIA_EXTENSIONS
    return MEDIA_EXTENSIONS


def kind_of(name: str) -> str | None:
    ext = Path(name).suffix.lower()
    if ext in DOC_EXT:
        return "document"
    if ext in IMAGE_EXT:
        return "photo"
    if ext in media_ext():
        return "recording"
    return None


def limit_for(kind: str) -> int:
    return {"document": MAX_DOC_BYTES, "photo": MAX_IMAGE_BYTES, "recording": MAX_MEDIA_BYTES}[kind]


def uploads_dir() -> Path:
    from arya.knowledge.library import DATA_DIR
    return DATA_DIR / "uploads"


def safe_name(name: str) -> str:
    stem = re.sub(r"[^\w .()-]", "", Path(name).stem).strip(" .")[:80] or "upload"
    return stem + Path(name).suffix.lower()


def room_for(size: int) -> bool:
    d = uploads_dir()
    d.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(d).free - size > KEEP_FREE_BYTES


def _unique(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p, n = folder / name, 2
    while p.exists():
        p = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    return p


# ── What we're talking about ──────────────────────────────────────────────────

@dataclass
class Upload:
    kind: str
    title: str
    path: str = ""                 # the file in uploads/ (a recording: its transcript)
    doc_id: int | None = None
    added: float = field(default_factory=time.time)
    status: str = "ready"          # ready / transcribing / failed
    progress: float = 1.0
    detail: str = ""


_uploads: list[Upload] = []        # this session's uploads, newest last
_events = None                     # the server's emit_status


def set_event_sink(sink) -> None:
    global _events
    _events = sink


def _emit(u: Upload) -> None:
    if _events is not None:
        import json
        _events("ARYA_UPLOAD:" + json.dumps({"kind": u.kind, "title": u.title, "status": u.status,
                                              "progress": round(u.progress, 2), "detail": u.detail}))


def active(now: float | None = None) -> Upload | None:
    """The upload the conversation is about: the newest ready document/recording of the last two hours."""
    now = now or time.time()
    for u in reversed(_uploads):
        if u.doc_id is not None and u.status == "ready" and now - u.added < ACTIVE_S:
            return u
    return None


_REFERS = re.compile(r"\b(?:th(?:is|e|at)|my)\s+(?:uploaded\s+)?(?:file|document|doc|pdf|paper|slides?|deck|report|"
                     r"upload|video|recording|audio|clip|transcript|lecture|meeting|podcast|text)\b|"
                     r"\b(?:i (?:just )?(?:added|uploaded|gave you|sent you)|in it|from it|about it)\b", re.IGNORECASE)
_SUMMARY = re.compile(r"\b(?:summar\w*|overview|what(?:'s| is)\s+(?:it|this|the \w+)\s+about|tl;?dr|key points|"
                      r"main points|gist)\b", re.IGNORECASE)


def about_upload(question: str, now: float | None = None) -> Upload | None:
    """The upload this question is about, if any."""
    u = active(now)
    if u is None:
        return None
    now = now or time.time()
    if _REFERS.search(question) or now - u.added < RECENT_S:
        return u
    return None


def wants_summary(question: str) -> bool:
    return bool(_SUMMARY.search(question))


# ── Handling an upload ────────────────────────────────────────────────────────

async def accept(tmp: Path, name: str) -> dict:
    """A file the app (or phone) streamed to `tmp`. Returns what the app shows; may start background work."""
    kind = kind_of(name)
    if kind is None:
        tmp.unlink(missing_ok=True)
        return {"error": "I can read PDFs, Word, PowerPoint, text, photos, audio and video."}
    if kind == "photo":
        from arya.tools import screen
        data = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        info = await asyncio.to_thread(screen.add_photo, data, Path(name).stem, PHOTO_KEEP_S)
        _uploads.append(Upload("photo", info["title"]))
        return {"kind": "photo", **info}
    folder = uploads_dir() / date.today().isoformat()
    if kind == "document":
        dest = _unique(folder, safe_name(name))
        shutil.move(str(tmp), dest)
        from arya.knowledge import library
        await asyncio.to_thread(library.index_file, dest)
        doc = await asyncio.to_thread(library.doc_id_for, dest)
        if doc is None:
            return {"error": "I couldn't read any text in that document."}
        u = Upload("document", Path(name).stem, str(dest), doc)
        _uploads.append(u)
        pages = await asyncio.to_thread(library.page_count, doc)
        print(f"Upload: document {dest.name}, {pages} page(s)")
        return {"kind": "document", "title": u.title, "pages": pages}
    # a recording: transcribe in the background
    duration = await asyncio.to_thread(_duration, tmp)
    if duration > MAX_MEDIA_S:
        tmp.unlink(missing_ok=True)
        return {"error": f"That recording is {int(duration // 60)} minutes long; I take up to {MAX_MEDIA_S // 60}."}
    u = Upload("recording", Path(name).stem, status="transcribing", progress=0.0)
    _uploads.append(u)
    asyncio.create_task(_transcribe_upload(u, tmp, name, folder, duration))
    return {"kind": "recording", "title": u.title, "minutes": round(duration / 60, 1), "status": "transcribing"}


def _duration(path: Path) -> float:
    from arya.knowledge.lectures import _duration as d
    return d(path)


_transcribing: asyncio.Lock | None = None             # created inside the running loop, on first use


async def _transcribe_upload(u: Upload, media: Path, name: str, folder: Path, duration: float) -> None:
    global _transcribing
    if _transcribing is None:
        _transcribing = asyncio.Lock()
    async with _transcribing:                                  # one recording at a time
        try:
            _emit(u)
            text = await asyncio.to_thread(_transcript_text, media, duration, u)
            if not text.strip():
                u.status, u.detail = "failed", "no speech or text found"
                _emit(u)
                return
            dest = _unique(folder, safe_name(Path(name).stem + " (transcript).md"))
            dest.write_text(f"# {Path(name).stem} — transcript\n\n{text}\n", encoding="utf-8")
            from arya.knowledge import library
            await asyncio.to_thread(library.index_file, dest)
            u.doc_id = await asyncio.to_thread(library.doc_id_for, dest)
            u.path, u.status, u.progress, u.added = str(dest), "ready", 1.0, time.time()
            print(f"Upload: transcribed {name} ({int(duration // 60)} min) -> {dest.name}")
        except Exception as exc:
            u.status, u.detail = "failed", f"{type(exc).__name__}"
            print(f"Upload: transcription failed for {name}: {type(exc).__name__}: {exc}")
        finally:
            media.unlink(missing_ok=True)                      # the transcript stays; the copy goes (disk)
            _emit(u)


def _transcript_text(media: Path, duration: float, u: Upload) -> str:
    """'[mm:ss] what was said' lines from Whisper, plus 'On screen at mm:ss: …' from a few video frames."""
    from arya.knowledge import lectures
    lines, language = [], None
    for t0, window in lectures._windows(media, 0.0):
        segs, language = lectures._transcribe_window(window, language, "")
        for a, _b, text in segs:
            lines.append(f"[{lectures.clock(t0 + a)}] {text}")
        if duration:
            u.progress = min(0.99, (t0 + window.size / lectures.SR) / duration)
            _emit(u)
    screen_text = _frame_text(media, duration) if media.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".avi",
                                                                            ".wmv"} else []
    return "\n".join(lines + screen_text)


def _frame_text(media: Path, duration: float) -> list[str]:
    """Text read (Windows OCR) from a few evenly spaced frames — slides and code in a screen recording."""
    import av

    from arya.knowledge.lectures import clock
    from arya.tools.screen import ocr_image
    out, seen = [], set()
    if duration <= 0:
        return out
    try:
        with av.open(str(media)) as c:
            stream = next((s for s in c.streams if s.type == "video"), None)
            if stream is None:
                return out
            for i in range(VIDEO_FRAMES):
                t = duration * (i + 0.5) / VIDEO_FRAMES
                c.seek(int(t * 1_000_000), any_frame=False, backward=True)
                frame = next(c.decode(stream), None)
                if frame is None:
                    continue
                text = " ".join(ocr_image(frame.to_image()).split())
                key = text[:120].lower()
                if len(text) >= 30 and key not in seen:
                    seen.add(key)
                    out.append(f"On screen at {clock(t)}: {text[:600]}")
    except Exception as exc:
        print(f"Upload: frames not read ({type(exc).__name__})")
    return out


# ── Voice ─────────────────────────────────────────────────────────────────────

_LIST = re.compile(r"\bwhat (?:files|documents|uploads) (?:did i|have i) (?:give|gave|sent|send|upload\w*)\b|"
                   r"\b(?:list|show) (?:my )?uploads\b|\bwhich files did i (?:give|send) you\b", re.IGNORECASE)
_PROGRESS = re.compile(r"\bhow far (?:is|are) (?:the |my )?(?:video|recording|audio|transcri\w+|upload)\b|"
                       r"\bis (?:the |my )?(?:video|recording|audio|transcript) (?:ready|done)\b", re.IGNORECASE)
_DELETE = re.compile(r"\b(?:delete|remove|forget) (?:th(?:at|is|e)|my) (?:last )?(?:upload|uploaded file|file I "
                     r"(?:uploaded|gave you|sent you)|document I (?:uploaded|gave you))\b", re.IGNORECASE)


def handle(message: str) -> str | None:
    if _PROGRESS.search(message):
        busy = [u for u in _uploads if u.kind == "recording"]
        if not busy:
            return "You haven't given me a recording in this session."
        u = busy[-1]
        if u.status == "transcribing":
            return f"{u.title}: about {int(u.progress * 100)} percent transcribed."
        if u.status == "failed":
            return f"I couldn't transcribe {u.title} ({u.detail})."
        return f"{u.title} is ready; ask me anything about it."
    if _LIST.search(message):
        if not _uploads:
            return "You haven't given me any files in this session. Drop one into the window or use the paperclip."
        names = [f"{u.title} ({u.kind}{', ' + u.status if u.status != 'ready' else ''})" for u in _uploads[-6:]]
        return "This session: " + "; ".join(names) + "."
    if _DELETE.search(message):
        target = next((u for u in reversed(_uploads) if u.path), None)
        if target is None:
            return "There's no uploaded file to delete."
        from arya.knowledge import library
        p = Path(target.path)
        if p.exists() and uploads_dir() in p.parents:          # only ARYA's own copy in uploads/
            p.unlink()
        if target.doc_id is not None:
            library.remove_doc(target.doc_id)
        _uploads.remove(target)
        return f"Deleted my copy of {target.title}; your original file is untouched."
    return None
