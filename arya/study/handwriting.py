"""
Handwritten worked solutions, read by a cloud vision model — only after asking.

  "Explain my handwritten solution of exercise 1-13"
     -> finds the scanned page (a PDF page with no text layer) that holds the exercise
     -> "I can read your handwritten solution by sending that one page image to the cloud. Okay?"
     -> yes: that page image only goes to Groq's vision model; ARYA speaks the explanation and
        shows the transcription; no: the exercise is explained locally from its printed text.

Every transcription is saved on the laptop (transcriptions.md next to the knowledge index) and
indexed, so the same handwritten page never has to be sent again: search and the tutor use it.
"""
from __future__ import annotations

import base64
import os
import re
from datetime import datetime
from pathlib import Path

VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
RENDER_DPI = 150

_TRIGGER = re.compile(r"\b(?:hand-?written|my (?:own )?(?:solution|working|notes)|worked solution|solution page|"
                      r"scanned (?:solution|page))\b", re.IGNORECASE)

_PROMPT = (
    "This is a page from a student's mechatronics exercise solutions: a printed task and a HANDWRITTEN worked "
    "solution. Subscripts in this course: R = relative, L = leading/carrier motion, Co = Coriolis, t = tangential, "
    "n = normal. First explain the solution method in at most five short plain sentences (no lists or markdown). "
    "Then write a line 'Transcription:' followed by the handwritten equations line by line in plain text "
    "(symbols like α, ω, φ, √, ²; fractions as a/b). Only use what is on the page; mark unreadable parts with [?]."
)


def transcriptions_file() -> Path:
    from arya.knowledge import library
    return library.TRANSCRIPTIONS


def is_scanned(doc_id: int, page: int) -> bool:
    """A PDF page whose text came from OCR (no text layer) — likely handwriting."""
    from arya.knowledge import library

    row = library.document(doc_id)
    if row is None or row["kind"] != "pdf":
        return False
    try:
        import pymupdf
        with pymupdf.open(row["path"]) as doc:
            return len(doc[page - 1].get_text("text").strip()) < library.SCANNED_PAGE_CHARS
    except Exception:
        return False


def find_scan(message: str):
    """(passage, ref) for the scanned solution page of the exercise named in the message, else None."""
    from arya.knowledge import library

    if not _TRIGGER.search(message):
        return None
    refs = library.exercise_refs(message)
    if not refs:
        return None
    for p in library.find_exercise(refs, message, k=6):
        if is_scanned(p.doc_id, p.page):
            return p, p.note.replace("exercise ", "")
    return None


def already_read(ref: str) -> bool:
    path = transcriptions_file()
    return path.exists() and f"## Exercise {ref} —" in path.read_text(encoding="utf-8")


def offer(message: str) -> str | None:
    """Ask before sending: registers a pending yes/no and returns the question, or None."""
    from arya.core import policy, router

    if not router.cloud_available():
        return None
    found = find_scan(message)
    if not found:
        return None
    p, ref = found
    if already_read(ref):
        return None                     # transcribed before: answered locally from transcriptions.md
    policy.offer("cloud_vision", {"doc": p.doc_id, "page": p.page, "ref": ref, "label": p.label(),
                                  "question": message},
                 f"I can read your handwritten solution of exercise {ref} ({p.label()}) by sending that one page "
                 f"image to the cloud. Okay? Say yes or no.")
    return policy._state.pending.question


def render(doc_id: int, page: int) -> bytes:
    from arya.knowledge import library
    import pymupdf

    row = library.document(doc_id)
    with pymupdf.open(row["path"]) as doc:
        return doc[page - 1].get_pixmap(dpi=RENDER_DPI).tobytes("png")


async def _vision_call(png: bytes) -> str:
    import httpx

    body = {"model": VISION_MODEL, "max_tokens": 900, "temperature": 0.2,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]}
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(GROQ_URL, json=body, headers={"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '')}"})
    if r.status_code != 200:
        raise RuntimeError(f"vision model error {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


def _save(ref: str, label: str, text: str) -> None:
    from arya.knowledge import library

    path = transcriptions_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# Handwritten solutions (transcribed)\n\nRead from your scanned pages by the cloud vision "
                        "model, after you said yes. Kept on this laptop.\n\n", encoding="utf-8")
    old = path.read_text(encoding="utf-8")
    old = re.sub(rf"(?ms)^## Exercise {re.escape(ref)} — .*?(?=^## |\Z)", "", old)      # newest reading wins
    path.write_text(old + f"## Exercise {ref} — {label}\nRead: {datetime.now():%Y-%m-%d}\n\n{text.strip()}\n\n",
                    encoding="utf-8")
    library.index_file(path)


async def read(args: dict) -> str:
    """Send the page, save + index the transcription, return the reply to speak/show."""
    from arya.core.audit import log_event

    png = render(args["doc"], args["page"])
    print(f"Cloud vision request: one page image ({len(png) // 1024} KB), {args['label']}")
    log_event("cloud_vision_request", bytes=len(png))
    text = await _vision_call(png)
    _save(args["ref"], args["label"], text)
    return text
