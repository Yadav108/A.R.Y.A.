"""
"Use visuals to explain it" / "where is the diagram?" about an exercise -> the exercise's own
figure, from the printed exercise sheet, on screen, with a short explanation that refers to it.

The small model used to *promise* a diagram and then describe one in text (or forget the
exercise entirely). For an exercise the right visual already exists: its sheet.
"""
from __future__ import annotations

import re
import time

_VISUAL = re.compile(r"\b(?:visuals?|visually|diagram|figure|picture|sketch|drawing|image|illustrat\w*|"
                     r"draw it|show (?:me )?(?:it|the (?:setup|situation|robot|mechanism|exercise|problem)))\b",
                     re.IGNORECASE)
# "draw a block diagram of a temperature loop with …" / "plot …" have their own subject: not the exercise.
_OWN_SUBJECT = re.compile(r"\b(?:block diagram|flow ?chart|plot|graph|bode|step response)\s+(?:of|for)\b|"
                          r"^\W*(?:plot|graph)\b", re.IGNORECASE)


def _current_exercise(message: str) -> str | None:
    """The question that names the exercise in focus: this message, a tutor session or the last exercise."""
    from arya.knowledge.library import exercise_refs

    if exercise_refs(message):
        return message
    from arya.study import tutor
    s = tutor.active()
    if s:
        return f"exercise {s.ref}"
    import arya.core.llm as llm
    if time.monotonic() < llm._last_exercise["until"]:
        return llm._last_exercise["question"]
    return None


def figure_for(message: str):
    """(ref, passage, artifact) — the rendered exercise page — or None if this isn't such a request."""
    if not _VISUAL.search(message) or _OWN_SUBJECT.search(message):
        return None
    question = _current_exercise(message)
    if not question:
        return None
    from arya.knowledge import library, visuals
    from arya.study import tutor
    from arya.study.handwriting import is_scanned

    passages = library.find_exercise(library.exercise_refs(question), question, k=6)
    sheets = [p for p in passages if p.kind == "pdf" and not tutor._is_answers(p.text)]
    if not sheets:
        return None
    printed = [p for p in sheets if not is_scanned(p.doc_id, p.page)]        # the clean printed figure first
    page = (printed or sheets)[0]
    ref = page.note.replace("exercise ", "")
    png = crop_exercise(page.doc_id, page.page, ref) or library.render_page(page.doc_id, page.page,
                                                                           highlight=page.text[:80])
    if not png:
        return None
    artifact = {"type": "image", "id": visuals._store(png), "caption": f"Exercise {ref}: {page.label()}"}
    return ref, page, artifact


def crop_exercise(doc_id: int, page_no: int, ref: str, dpi: int = 150) -> bytes | None:
    """Just this exercise: from its "1-13)" label to the next exercise label (or the page footer)."""
    from arya.core.policy import check_path
    from arya.knowledge import library
    import pymupdf

    row = library.document(doc_id)
    if row is None or row["kind"] != "pdf":
        return None
    try:
        with pymupdf.open(check_path(row["path"])) as doc:
            pg = doc[page_no - 1]
            hits = pg.search_for(f"{ref})")
            if not hits:
                return None
            top = max(0.0, hits[0].y0 - 12)
            a, b = (int(x) for x in ref.split("-"))
            below = [r for nxt in (f"{a}-{b + 1})", f"{a + 1}-1)") for r in pg.search_for(nxt) if r.y0 > hits[0].y1]
            bottom = min([r.y0 - 8 for r in below] + [pg.rect.height - 60])        # stop above the footer
            used = [b[3] for b in pg.get_text("blocks") if top <= b[1] < bottom]   # text inside the exercise
            used += [d["rect"].y1 for d in pg.get_drawings() if top <= d["rect"].y0 < bottom]   # its figure
            if used:
                bottom = min(bottom, max(used) + 12)                               # no empty space below
            if bottom - top < 60:
                return None
            clip = pymupdf.Rect(0, top, pg.rect.width, bottom)
            return pg.get_pixmap(dpi=dpi, clip=clip).tobytes("png")
    except Exception as exc:
        print(f"Exercise crop failed: {exc}")
        return None


async def describe(ref: str, page) -> str:
    """A few spoken sentences that walk through the figure, from the task text only."""
    from arya.core.llm import INTENT_MODEL_NAME, _complete, plain_text, trim_unfinished
    from arya.core.speech_math import latex_to_text

    head = f"Here's exercise {ref} with its figure on screen, from {page.title}, page {page.page}."
    prompt = (
        "The student is looking at the figure of an exercise from their course; the page is on their screen. "
        "Using ONLY the task text below, describe in at most four short spoken sentences what the figure shows "
        "(the parts, start and end positions, directions of motion or rotation) and which idea it points to for "
        "solving it. Refer to it as 'the figure'. Only describe motion the text states (e.g. if it says something "
        "starts from rest or with zero angular velocity, say exactly that). No lists, markdown, LaTeX, numbers "
        "that aren't in the text, and no results.\n\nTask:\n" + page.text[:1500])
    try:
        completion = await _complete(INTENT_MODEL_NAME, [{"role": "system", "content": prompt},
                                                         {"role": "user", "content": "Walk me through the figure."}],
                                     temperature=0.3, max_tokens=170)
        body = plain_text(latex_to_text(trim_unfinished(completion.content or ""))).strip()
    except Exception as exc:
        print(f"Figure description failed: {exc}")
        body = ""
    return f"{head} {body}".strip()
