"""
Check a photo of your own handwritten solution (roadmap C3).

Drop or paste a photo of your work into the app, then: "check my solution for exercise 2-2" (or just "check my
solution" while a tutor session or mock-exam task is open). Handwriting can't be read on the laptop, so ARYA
asks first: only that one photo, the exercise's task text and its official result go to the cloud vision model.

The cloud model checks the steps and names the first wrong one. It also writes out the final result it reads,
and ARYA itself compares that with the answer sheet (the same check as "I got 2.8 m/s"), so whether you are
right never depends on the cloud model's own judgement. The outcome is recorded for spaced repetition.
"""
from __future__ import annotations

import re

_CHECK = re.compile(r"\b(?:check|correct|grade|look (?:at|over)|is)\b.{0,25}\b(?:my|this)\s+(?:hand-?written\s+)?"
                    r"(?:solution|working|work|answer|calculation|attempt)\b|\bis (?:my|this) (?:solution|working|"
                    r"answer) (?:right|correct)\b|\bwhere did i go wrong\b", re.IGNORECASE)

PROMPT = (
    "The photo shows a mechanical-engineering student's handwritten solution to this exercise.\n\n"
    "Exercise {ref}:\n{statement}\n\nOfficial result(s) from the answer sheet: {results}\n\n"
    "Check the solution step by step. Answer in at most five short plain spoken sentences (no lists, no markdown, "
    "no LaTeX): say whether the approach is right, and if something is wrong, name the FIRST wrong step, why it is "
    "wrong and what to do instead. If you cannot read part of it, say so instead of guessing.\n"
    "Then, on a separate last line, write exactly: FINAL: <symbol> = <value> <unit> for the student's final "
    "result as written on the page, or FINAL: none if there is no final result.")
_FINAL = re.compile(r"^\s*FINAL:\s*(.*)$", re.IGNORECASE | re.MULTILINE)


def _which_exercise(message: str) -> str | None:
    from arya.knowledge.library import exercise_refs
    from arya.study import mock_exam, tutor

    refs = exercise_refs(message)
    if refs:
        return refs[0]
    if tutor.active():
        return tutor.active().ref
    exam = mock_exam.active()
    if exam:
        return exam.tasks[exam.current].ref
    return None


def handle(message: str) -> str | None:
    """The offer (asks before anything leaves the laptop), or a reason it can't; None if not a solution check."""
    from arya.core import policy, router
    from arya.tools import screen

    if not _CHECK.search(message):
        return None
    if not (screen.context_fresh() and screen._context.get("kind") == "photo"):
        if re.search(r"\b(?:photo|picture|handwrit\w*|this)\b", message, re.IGNORECASE):
            return ("Drop or paste a photo of your solution into the ARYA window first, then ask me again. "
                    "Or tell me your final result, like \"I got 2.8 metres per second\".")
        return None                                       # "check my answer: 2.8 m/s" — the tutor's job
    ref = _which_exercise(message)
    if not ref:
        return "Which exercise is it? Say for example \"check my solution for exercise 2-2\"."
    from arya.study import tutor
    ex = tutor.load_exercise(f"exercise {ref}")
    if not ex:
        return f"I can't find exercise {ref} in your material, so I have nothing to check it against."
    if not router.cloud_available():
        return ("Reading handwriting needs the cloud vision model, and the cloud isn't set up. Tell me your final "
                "result instead, like \"I got 2.8 metres per second\", and I'll check that.")
    results = "; ".join(r.spoken() for r in ex.results) if ex.results else "not on the answer sheet"
    return policy.offer("check_solution",
                        {"image_id": screen._context["image_id"], "ref": ref, "statement": ex.statement[:1500],
                         "results": results, "has_results": bool(ex.results)},
                        f"To read your handwriting I'd send this one photo to the cloud, together with exercise "
                        f"{ref}'s task text and its official result. Okay? Say yes or no.")


async def check_with_cloud(args: dict) -> str:
    """After "yes": photo + task + official result to the vision model; ARYA checks the final value itself."""
    from arya.core.audit import log_event
    from arya.knowledge import visuals
    from arya.study import handwriting, tutor

    png = visuals.image(args["image_id"])
    if not png:
        return "That photo has expired. Add it again and ask me once more."
    log_event("cloud_vision_request", bytes=len(png), source="solution_check", exercise=args["ref"])
    out = await handwriting._vision_call(png, prompt=PROMPT.format(ref=args["ref"], statement=args["statement"],
                                                                   results=args["results"]))
    m = _FINAL.search(out)
    feedback = _FINAL.sub("", out).strip()
    final = (m.group(1).strip() if m else "")
    verdict = ""
    ex = tutor.load_exercise(f"exercise {args['ref']}")
    if final and final.lower() != "none" and ex and ex.results:
        claim = tutor.parse_claim(final)
        if claim:
            verdict = tutor.check(claim, ex.results)
            _record(args["ref"], verdict)
    head = f"Your final result, {final}: {verdict}" if verdict else ""
    return (head + " " + feedback).strip() if head else feedback


def _record(ref: str, verdict: str) -> None:
    from arya.study import practice
    quality = 5 if verdict.startswith("Correct") else 3 if verdict.startswith("Close") else 1
    with practice._db() as c:
        row = c.execute("SELECT id FROM cards WHERE key LIKE ? ORDER BY id LIMIT 1", (f"exercise:{ref}:%",)).fetchone()
    if row:
        practice.record(row["id"], quality)
