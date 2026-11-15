"""C3: timed mock exams from exercises with answer sheets — picking, answering, grading, recording.
Cards are made up for the test; no library or model is needed and nothing is spoken."""
import asyncio

import pytest

import arya.core.llm as llm
from arya.study import mock_exam as M
from arya.study import practice
from arya.tools import productivity

CARDS = [  # (ref, name, value, unit, doc)
    ("1-13", "α2", 2.55, "/s2", 7), ("1-7", "t", 2.83, "s", 3), ("2-4", "v", 4.2, "m/s", 3),
    ("3-1", "F", 120.0, "N", 3), ("3-5", "ω", 6.0, "/s", 7), ("4-2", "x", 0.35, "m", 3),
]


@pytest.fixture
def cards(kb, monkeypatch):
    with practice._db() as c:
        c.execute("DELETE FROM cards")
        c.execute("DELETE FROM reviews")
        for ref, name, value, unit, doc in CARDS:
            practice._add(c, kind="exercise", course="Dynamics", key=f"exercise:{ref}:{name}",
                          question=f"Exercise {ref}: what is {name}?",
                          answer=f"The answer sheet gives {name} = {value} {unit}.", name=name, value=value,
                          unit=unit, doc_id=doc, page=1)
    monkeypatch.setattr(M, "_exam_docs", lambda: {7})                 # doc 7 is an old exam
    monkeypatch.setattr(practice, "build_cards", lambda course=None: len(CARDS))
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    monkeypatch.setattr(M, "_exam", None)
    yield
    with practice._db() as c:
        c.execute("DELETE FROM cards")
        c.execute("DELETE FROM reviews")


def _run(*messages):
    """Say each message in one event loop (the exam clock is an asyncio task)."""
    async def go():
        out = []
        for m in messages:
            out.append(await M.handle(m))
        if M.active():
            M._stop_clock(M.active())
        await productivity.cancel_timer()
        return out
    return asyncio.run(go())


def test_tasks_prefer_old_exams_and_one_per_exercise(cards):
    tasks = M.pick_tasks("Dynamics", 3, seed=1)
    assert len(tasks) == 3 and len({t.ref for t in tasks}) == 3
    assert {t.ref for t in tasks[:2]} == {"1-13", "3-5"}                     # the two exam exercises first


def test_a_whole_mock_exam(cards):
    async def go():
        start = await M.handle("give me a 36 minute Dynamics mock exam")
        exam = M.active()
        right = exam.tasks[0].card
        a1 = await M.handle(f"task 1: {right['value']} {right['unit']}")
        a2 = await M.handle("my answer is 9.9")
        show = await M.handle("show task 3")
        left = await M.handle("how much time is left?")
        fin = await M.handle("finish the exam")
        await productivity.cancel_timer()
        return start, a1, a2, show, left, fin, exam
    start, a1, a2, show, left, fin, exam = asyncio.run(go())
    reply, sources = start
    assert reply.startswith("Dynamics mock exam: 3 tasks in 36 minutes, the clock is running.")
    assert "Task 1: exercise" in reply and len(sources) == 3
    assert a1[0].startswith("Got it for task 1. Next: Task 2")
    assert a2[0].startswith("Got it for task 2. Next: Task 3")
    assert show[0].startswith("Task 3: exercise") and show[1][0]["label"].startswith("Task 3")
    assert left[0].endswith("You've answered 2 of 3 tasks.")
    text = fin[0]
    assert text.startswith("Mock exam over after 1 minute: 1 of 3 right, that's 33 percent.")
    assert f"Task 2, exercise {exam.tasks[1].ref}: you said 9.9" in text
    assert f"Task 3, exercise {exam.tasks[2].ref}: no answer" in text and "Revisit exercise" in text
    assert M.active() is None
    with practice._db() as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 3        # all three tasks recorded
        assert tuple(c.execute("SELECT right, total FROM mock_exams ORDER BY id DESC").fetchone()) == (1, 3)


def test_cancel_grades_nothing(cards):
    start, cancel = _run("start a mock exam", "cancel the mock exam")
    assert start[0].startswith("Dynamics mock exam")                       # the only course with exercises
    assert cancel[0] == "Mock exam cancelled, nothing graded."
    with practice._db() as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_no_exercises_for_a_course(cards):
    reply, _ = _run("mock exam for Control")[0]
    assert reply.startswith("I don't have Control exercises with answer sheets yet")


@pytest.mark.parametrize("message", ["when is my exam", "what is the Coriolis acceleration", "task 2"])
def test_not_mock_exam_requests(cards, message):
    assert _run(message)[0] is None


def test_other_questions_during_the_exam_go_to_normal_arya(cards):
    _, other = _run("mock exam", "what is the formula for the Coriolis acceleration?")
    assert other is None
