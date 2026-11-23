"""
Share -> ARYA on the phone: a link, some text, a PDF or a photo, plus one action.

  summarize / explain / ask  -> the item becomes an uploaded document (a link: the laptop reads the page itself,
                                arya/tools/webpage.py; text: saved as a small document), then ARYA answers about
                                "the document I just added" like any upload — so shared content is untrusted
                                content, never instructions (attachments + policy)
  note / todo / remind       -> saved directly (notes, to-dos, a reminder tonight or tomorrow morning); no model

The phone uploads files first (/api/upload) and then sends {"upload": {"kind", "title"}} here.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

ACTIONS = ("summarize", "explain", "ask", "note", "todo", "remind")
LABELS = {"summarize": "Summary", "explain": "Explained", "ask": "Question", "note": "Saved as note",
          "todo": "Added to to-dos", "remind": "Reminder"}
MAX_TEXT = 50_000
EVENING = 20                    # "remind me tonight": at 20:00, or tomorrow at 09:00 when it's already late
MORNING = 9


class ShareError(Exception):
    """A sentence for the phone ("That page couldn't be reached.")."""


def remind_time(now: datetime) -> datetime:
    if now.hour < EVENING - 1:
        return now.replace(hour=EVENING, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(hour=MORNING, minute=0, second=0, microsecond=0)


def remind_label(now: datetime) -> str:
    return "Remind me tonight" if now.hour < EVENING - 1 else "Remind me tomorrow"


def short(text: str, n: int = 90) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def title_for_text(text: str) -> str:
    first = next((ln for ln in text.splitlines() if ln.strip()), text)
    return short(first, 60) or "Shared text"


def prompt_for(action: str, kind: str, question: str = "") -> str:
    """What ARYA is asked once the item is an upload (kind: 'document' or 'photo')."""
    thing = "the photo I just added" if kind == "photo" else "the document I just added"
    if action == "summarize":
        return ("What's in the photo I just added? Describe it briefly." if kind == "photo"
                else "Summarize the document I just added.")
    if action == "explain":
        return f"Explain {thing} in simple words: the main points and why they matter."
    if action == "ask":
        q = question.strip().rstrip("?") or "What is this about"
        return f"{q}? (About {thing}.)"
    raise ShareError("I don't know that action.")


async def _as_document(name: str, content: bytes) -> dict:
    """Store `content` as an upload named `name` (the same path as a file sent from the phone)."""
    from arya.core import attachments
    incoming = attachments.uploads_dir() / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    if not attachments.room_for(len(content) + 1024 * 1024):
        raise ShareError("There isn't enough free disk space on the laptop.")
    tmp = incoming / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
    tmp.write_bytes(content)
    try:
        info = await attachments.accept(tmp, name)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ShareError(f"I couldn't read that ({type(exc).__name__}).") from None
    if "error" in info:
        raise ShareError(info["error"])
    return info


def _safe_title(t: str) -> str:
    t = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", t).strip(" .")
    return short(t, 70) or "Shared"


async def handle(body: dict, turn: Callable[[str], Awaitable[dict]], now: datetime | None = None) -> dict:
    """One share from the phone. Returns {"reply", "title", "action", "url"?, "confirm"}; ShareError otherwise."""
    now = now or datetime.now()
    action = str(body.get("action", "")).lower()
    if action not in ACTIONS:
        raise ShareError("I don't know that action.")
    url = str(body.get("url") or "").strip()
    text = str(body.get("text") or "").strip()[:MAX_TEXT]
    subject = str(body.get("subject") or "").strip()
    upload = body.get("upload") if isinstance(body.get("upload"), dict) else None
    question = str(body.get("question") or "").strip()[:500]
    if not (url or text or upload):
        raise ShareError("Nothing was shared.")
    if url and not re.match(r"^https?://", url, re.IGNORECASE):
        raise ShareError("I can only open web links (http or https).")

    title = (str(upload.get("title", "")) if upload else "") or subject or (url and _host(url)) or title_for_text(text)
    result = {"action": action, "title": title, "url": url, "confirm": False}

    # ── Saving: no model, nothing fetched ──
    if action in ("note", "todo", "remind"):
        what = title if (url or upload) else short(text, 200)
        if url and subject and subject != title:
            what = subject
        link = f" {url}" if url else ""
        if action == "note":
            from arya.tools.notes import save_note
            await save_note(f"{what}{link}" + (f"\n\n{text}" if text and url and text != url else ""), tag="shared")
            result["reply"] = f"Saved to your notes: {short(what, 80)}."
        elif action == "todo":
            from arya.core import modes
            from arya.tools import tasks
            tasks.add(f"Look at: {short(what, 120)}{link}", "work" if modes.current_name() == "work" else "personal")
            result["reply"] = f"Added to your to-dos: look at {short(what, 80)}."
        else:
            from arya.core import policy
            from arya.tools.productivity import set_reminder
            at = remind_time(now)
            await set_reminder(f"look at “{short(what, 80)}”{link}", in_minutes=(at - now).total_seconds() / 60)
            policy.note_result("set_reminder")
            day = "tonight" if at.date() == now.date() else "tomorrow"
            result["reply"] = f"I'll remind you {day} at {at:%H:%M} to look at {short(what, 80)}."
        return result

    # ── Answering: the item becomes an upload first ──
    kind = "document"
    if upload:
        kind = "photo" if upload.get("kind") == "photo" else "document"
    elif url:
        from arya.tools import webpage
        try:
            page = await webpage.fetch(url)
        except webpage.PageError as exc:
            raise ShareError(str(exc)) from None
        title = page.title or title
        name = _safe_title(title)
        if page.pdf:
            await _as_document(name + ".pdf", page.pdf)
        else:
            md = f"# {title}\n\nSource: {page.url}\n\n{page.text}\n"
            if text and text != url:                      # what the sharing app sent along (often a headline)
                md = f"# {title}\n\nSource: {page.url}\n\nShared with the comment: {short(text, 300)}\n\n{page.text}\n"
            await _as_document(name + ".md", md.encode("utf-8"))
        result["title"] = title
    else:
        name = _safe_title(subject or title_for_text(text))
        await _as_document(name + ".md", f"# {name}\n\n{text}\n".encode("utf-8"))
        result["title"] = name

    started = time.monotonic()
    answer = await turn(prompt_for(action, kind, question))
    result["reply"] = answer.get("reply") or "Done."
    result["confirm"] = bool(answer.get("confirm"))
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or url).removeprefix("www.")
