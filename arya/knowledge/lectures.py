"""
Lecture recordings → searchable transcripts (roadmap C3).

The audio and video files in your course folders (mp4, mkv, m4a, mp3, …) are transcribed with ARYA's own
Whisper model — the one already loaded for your voice, so nothing extra has to fit on the GPU — and added
to the knowledge library. Answers then cite the lecture and the minute: "Dynamics lecture 5, at 24:00".

When: only while you've been away from the keyboard for IDLE_MINUTES and ARYA isn't busy; it pauses the
moment you're back. "Transcribe my lectures now" works through them anyway (your own commands then wait a
second or two at most); "pause transcribing" stops that. Progress is saved after every 30 s of audio, so a
lecture resumes where it stopped, even after a restart.

Order: courses with an upcoming exam first, then the others; lectures in folder order. Copies of the same
recording (same name and size) are transcribed once. The language is detected per lecture.

Transcripts live in %LOCALAPPDATA%\\ARYA\\lectures\\ (one JSON per recording); nothing leaves the laptop.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

import numpy as np

from arya.knowledge.library import DATA_DIR

MEDIA_EXTENSIONS = {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv"}
STORE = DATA_DIR / "lectures"
SR = 16_000
WINDOW_S = 30                      # transcribed at a time (a live command waits at most this long's worth)
BLOCK_S = 120                      # one library "page" = two minutes of lecture
IDLE_MINUTES = float(os.getenv("ARYA_TRANSCRIBE_IDLE_MIN", "10"))
ENABLED = os.getenv("ARYA_TRANSCRIBE_LECTURES", "1") != "0"

_force = threading.Event()         # "transcribe my lectures now"
_stop = threading.Event()          # "pause transcribing"
_current: dict = {"path": "", "done_s": 0.0, "duration": 0.0}


# ── Store ─────────────────────────────────────────────────────────────────────

def _key(path: Path) -> str:
    return hashlib.sha1(str(path).lower().encode()).hexdigest()[:16]


def _store_file(path: Path) -> Path:
    return STORE / f"{_key(path)}.json"


def load(path: Path) -> dict | None:
    f = _store_file(path)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    st = path.stat() if path.exists() else None
    if st is None or data.get("size") != st.st_size:
        return None                                  # the recording changed: start over
    return data


def _save(path: Path, data: dict) -> None:
    STORE.mkdir(parents=True, exist_ok=True)
    tmp = _store_file(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_store_file(path))


def transcript_ready(path: Path) -> bool:
    data = load(path)
    return bool(data and data.get("complete"))


def pages(path: Path) -> list[tuple[int, str]]:
    """The library's view of a transcribed recording: page n = minutes 2(n-1) to 2n, each starting with its time."""
    data = load(path)
    if not data or not data.get("complete"):
        return []
    blocks: dict[int, list[str]] = {}
    for start, _end, text in data["segments"]:
        blocks.setdefault(int(start // BLOCK_S) + 1, []).append(text.strip())
    return [(n, f"[{clock((n - 1) * BLOCK_S)}] " + " ".join(parts)) for n, parts in sorted(blocks.items())]


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ── Which recordings, in which order ─────────────────────────────────────────

def recordings(roots: list[Path] | None = None) -> list[Path]:
    """Unique recordings in the course folders: exam courses first, then the rest, in folder order."""
    from arya.knowledge.library import _copy_score, _title_and_collection, default_roots

    roots = roots if roots is not None else default_roots()
    found: dict[tuple[str, int], Path] = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                try:
                    key = (name.lower(), p.stat().st_size)
                except OSError:
                    continue
                best = found.get(key)
                if best is None or (_copy_score(p), len(str(p))) < (_copy_score(best), len(str(best))):
                    found[key] = p
    try:
        from arya.study.planner import upcoming
        exam_courses = [c.lower() for c, _ in upcoming()]
    except Exception:
        exam_courses = []

    def order(p: Path):
        _, course = _title_and_collection(p, roots)
        text = str(p).lower()
        rank = next((i for i, c in enumerate(exam_courses) if c in text or c == course.lower()), len(exam_courses))
        return rank, _natural(str(p.parent).lower()), _natural(p.name.lower())

    return sorted(found.values(), key=order)


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def progress(roots: list[Path] | None = None) -> dict:
    recs = recordings(roots)
    done = sum(transcript_ready(p) for p in recs)
    return {"total": len(recs), "done": done, "current": _current["path"],
            "current_done_s": _current["done_s"], "current_duration": _current["duration"],
            "forced": _force.is_set()}


# ── Transcription ─────────────────────────────────────────────────────────────

def _windows(path: Path, skip_s: float):
    """16 kHz mono float32 windows of WINDOW_S seconds, decoded as the file is read (not all at once)."""
    import av

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SR)
        buf, have, t0 = [], 0, 0.0
        target = WINDOW_S * SR
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                pcm = out.to_ndarray().reshape(-1).astype(np.float32)
                buf.append(pcm)
                have += pcm.size
                while have >= target:
                    chunk = np.concatenate(buf)
                    window, rest = chunk[:target], chunk[target:]
                    buf, have = [rest], rest.size
                    if t0 + WINDOW_S > skip_s:
                        yield t0, window
                    t0 += WINDOW_S
        if have:
            chunk = np.concatenate(buf)
            if t0 + chunk.size / SR > skip_s and chunk.size > SR:
                yield t0, chunk


def _duration(path: Path) -> float:
    import av
    try:
        with av.open(str(path)) as c:
            return float(c.duration / 1_000_000) if c.duration else 0.0
    except Exception:
        return 0.0


def _transcribe_window(audio: np.ndarray, language: str | None, prompt: str) -> tuple[list, str | None]:
    from arya.core import stt

    model = stt.load_model()
    with stt.model_lock:
        segments, info = model.transcribe(audio, language=language, beam_size=1, best_of=1, temperature=0.0,
                                          condition_on_previous_text=False, vad_filter=True,
                                          vad_parameters={"min_silence_duration_ms": 700}, initial_prompt=prompt)
        out = [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
    return out, (language or getattr(info, "language", None))


def transcribe(path: Path, keep_going=lambda: True, roots: list[Path] | None = None) -> bool:
    """Transcribe (or resume) one recording; True when it is complete. Stops between windows when told."""
    from arya.knowledge.library import _title_and_collection

    data = load(path) or {"path": str(path), "size": path.stat().st_size, "segments": [], "done_s": 0.0,
                          "language": None, "complete": False, "duration": _duration(path)}
    if data.get("complete"):
        return True
    title, course = _title_and_collection(path, roots or [])
    prompt = f"{course} lecture: {title}."
    _current.update(path=str(path), done_s=data["done_s"], duration=data.get("duration") or 0.0)
    started = time.monotonic()
    for t0, window in _windows(path, data["done_s"]):
        if not keep_going():
            _save(path, data)
            return False
        segs, data["language"] = _transcribe_window(window, data["language"], prompt)
        data["segments"] += [[round(t0 + a, 2), round(t0 + b, 2), text] for a, b, text in segs
                             if t0 + a >= data["done_s"] - 0.5]
        data["done_s"] = t0 + window.size / SR
        _current["done_s"] = data["done_s"]
        _save(path, data)
    data["complete"] = True
    _save(path, data)
    took = time.monotonic() - started
    print(f"Lectures: transcribed {path.name} ({clock(data['done_s'])}, {data['language']}) in {took:.0f}s")
    _current.update(path="", done_s=0.0, duration=0.0)
    return True


# ── Background worker ────────────────────────────────────────────────────────

def _user_away() -> bool:
    from arya.core.proactive import _idle_seconds
    return _idle_seconds() >= IDLE_MINUTES * 60


_failed: dict[str, tuple[float, int]] = {}      # path -> (mtime, size) of a recording that could not be decoded


def _signature(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def _mark_failed(path: Path) -> None:
    _failed[str(path)] = _signature(path)


def _failed_before(path: Path) -> bool:
    """True while a recording that failed is unchanged; a re-downloaded or repaired file is tried again."""
    sig = _failed.get(str(path))
    return sig is not None and sig == _signature(path)


async def run(busy) -> None:
    """Started by the server: transcribes while you're away (or when asked), one window at a time."""
    if not ENABLED:
        print("Lectures: transcription off (ARYA_TRANSCRIBE_LECTURES=0)")
        return
    await asyncio.sleep(60)
    from arya.knowledge import library

    while True:
        try:
            allowed = lambda: not _stop.is_set() and not busy() and (_force.is_set() or _user_away())
            if allowed():
                roots = library.default_roots()
                todo = [p for p in await asyncio.to_thread(recordings, roots) if not transcript_ready(p)]
                if not todo:
                    _force.clear()
                todo = [p for p in todo if not _failed_before(p)]
                for path in todo:
                    if not allowed():
                        break
                    try:
                        done = await asyncio.to_thread(transcribe, path, allowed, roots)
                    except Exception as exc:
                        # A broken recording (half-downloaded, corrupt) used to stop every later file and log the
                        # same error every 30 s for hours. Skip it until the file changes.
                        _mark_failed(path)
                        print(f"Lectures: skipping {path.name} until it changes — {type(exc).__name__}: {exc}")
                        continue
                    if done:
                        await asyncio.to_thread(library.index_file, path)
        except Exception as exc:
            print(f"Lectures: {type(exc).__name__}: {exc}")
        await asyncio.sleep(30)


# ── Voice ─────────────────────────────────────────────────────────────────────

_START = re.compile(r"\b(?:transcribe|transcription of)\b.{0,20}\b(?:lectures?|recordings?|videos?)\b|"
                    r"\b(?:start|resume|continue)\s+(?:the\s+)?(?:lecture\s+)?transcri(?:bing|ption)\b",
                    re.IGNORECASE)
_PAUSE = re.compile(r"\b(?:pause|stop)\s+(?:the\s+)?(?:lecture\s+)?transcri(?:bing|ption)s?\b", re.IGNORECASE)
_STATUS = re.compile(r"\bhow\s+(?:far|many)\b.{0,40}\b(?:transcri\w+|lectures? (?:are|have been) transcribed)\b|"
                     r"\b(?:lecture\s+)?transcription\s+(?:status|progress)\b", re.IGNORECASE)


def _status_text(p: dict) -> str:
    if not p["total"]:
        return "I don't see any lecture recordings in your course folders."
    text = f"{p['done']} of {p['total']} lecture recordings are transcribed and searchable."
    if p["current"]:
        d = p["current_duration"]
        where = f", {clock(p['current_done_s'])} of {clock(d)}" if d else ""
        text += f" Working on {Path(p['current']).stem}{where}."
    elif p["done"] < p["total"]:
        text += (" I'm working through them now." if p["forced"]
                 else f" I work on them while you're away from the laptop for {IDLE_MINUTES:g} minutes, "
                      "or say \"transcribe my lectures now\".")
    return text


def handle(message: str) -> str | None:
    if _PAUSE.search(message):
        _force.clear()
        _stop.set()
        return "Paused the lecture transcription. Say \"transcribe my lectures\" to continue."
    if _STATUS.search(message):
        return _status_text(progress())
    if _START.search(message):
        _stop.clear()
        _force.set()
        p = progress()
        left = p["total"] - p["done"]
        if not left:
            return _status_text(p)
        return (f"Starting on the {left} lecture recording{'s' if left != 1 else ''} that aren't transcribed yet. "
                "Your commands may take a moment longer meanwhile; say \"pause transcribing\" to stop.")
    return None
