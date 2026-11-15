"""M3: quizzes from your own material with spaced repetition (SM-2)."""
import asyncio
import types
from datetime import date, timedelta

import pytest

import arya.core.llm as llm
from arya.study import practice as P


def test_sm2_schedule():
    assert P.schedule(2.5, 0, 0, 5)[1:] == (1, 1)            # first right answer: tomorrow
    assert P.schedule(2.6, 1, 1, 5)[1:] == (6, 2)            # second: in 6 days
    ease, interval, reps = P.schedule(2.7, 6, 2, 4)
    assert interval == round(6 * 2.7) and reps == 3
    ease, interval, reps = P.schedule(2.5, 16, 3, 1)         # missed: back tomorrow, ease drops
    assert (interval, reps) == (1, 0) and ease == pytest.approx(2.3)
    assert P.schedule(1.3, 1, 0, 0)[0] == 1.3                # ease never below 1.3


@pytest.fixture
def quiz(kb, monkeypatch):
    with P._db() as c:
        c.execute("DELETE FROM cards")
        c.execute("DELETE FROM reviews")
        c.execute("CREATE TABLE IF NOT EXISTS card_scans (ref TEXT PRIMARY KEY)")
        c.execute("DELETE FROM card_scans")
    P._quiz = None
    judged = []

    async def fake_complete(model, messages, **kw):
        judged.append(messages[-1]["content"])
        student = messages[-1]["content"].split("Student:", 1)[1]
        word = "CORRECT" if "k over m" in student or "√(k/m)" in student else "WRONG"
        return types.SimpleNamespace(content=word, tool_calls=None)

    monkeypatch.setattr(llm, "_complete", fake_complete)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    llm.set_artifact_sink(lambda kind, payload: None)
    yield judged
    P._quiz = None


def _turn(m):
    return asyncio.run(P.handle(m))


def test_cards_come_from_answers_pages_and_formulas(quiz):
    P.build_cards()
    with P._db() as c:
        cards = {r["question"]: r for r in c.execute("SELECT * FROM cards")}
    assert "Exercise 1-13: what is a2?" in cards                                  # asked in the task text
    assert cards["Exercise 1-13: what is a2?"]["value"] == 2.55
    assert "What is the impedance of a capacitor?" in cards
    assert P.build_cards() == len(cards)                                          # idempotent


def test_quiz_session_grades_and_schedules(quiz):
    with P._db() as c:
        c.execute("INSERT INTO cards (kind, course, key, question, answer, name, value, unit) VALUES "
                  "('exercise','Dynamics','exercise:1-13:a2','Exercise 1-13: what is a2?',"
                  "'The answer sheet gives a2 = 2.55/s2.','a2',2.55,'/s2')")
        c.execute("INSERT INTO cards (kind, course, key, question, answer) VALUES ('formula','Dynamics',"
                  "'formula:What is the natural frequency of a mass-spring system?',"
                  "'What is the natural frequency of a mass-spring system?',"
                  "'The natural frequency of a mass-spring system is ωₙ = √(k/m).')")
        c.execute("CREATE TABLE IF NOT EXISTS card_scans (ref TEXT PRIMARY KEY)")
        c.execute("INSERT OR IGNORE INTO card_scans (ref) VALUES ('1-13'), ('1-12'), ('1-14')")
    reply, _ = _turn("quiz me on dynamics")
    assert reply.startswith("Let's practise Dynamics:") and "Question 1 of" in reply
    first = P.active().current()["question"]
    answer = "omega n is the square root of k over m" if "natural" in first else "2.55"
    assert _turn(answer)[0].startswith("Correct!")
    second = P.active().current()["question"]
    wrong = "it is 9 per second squared" if "Exercise" in second else "it is m over k"
    reply, _ = _turn(wrong)
    assert reply.startswith("Not quite.")
    with P._db() as c:
        rows = {r["question"]: r for r in c.execute("SELECT * FROM cards")}
        reviews = c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert reviews == 2
    assert rows[first]["due"] == (date.today() + timedelta(days=1)).isoformat() and rows[first]["reps"] == 1
    assert rows[second]["due"] == (date.today() + timedelta(days=1)).isoformat() and rows[second]["reps"] == 0
    assert rows[second]["lapses"] == 1


def test_skip_is_the_quiz_not_the_music(quiz):
    P.build_cards()
    _turn("quiz me")
    q = P.active().current()["question"]
    reply, _ = _turn("skip")
    assert reply.startswith("Skipped.") and P.active().current()["question"] != q


def test_minus_sign_and_stop(quiz):
    with P._db() as c:
        c.execute("INSERT INTO cards (kind, course, key, question, answer, name, value, unit) VALUES "
                  "('exercise','Dynamics','exercise:1-2:a1','Exercise 1-2: what is a1?',"
                  "'The answer sheet gives a1 = -3.86 m/s2.','a1',-3.86,'m/s2')")
        c.execute("CREATE TABLE IF NOT EXISTS card_scans (ref TEXT PRIMARY KEY)")
        c.execute("INSERT OR IGNORE INTO card_scans (ref) VALUES ('1-13'), ('1-12'), ('1-14')")
    monkey_deck = P.deck("dynamics")
    assert monkey_deck[0]["question"] == "Exercise 1-2: what is a1?"
    _turn("quiz me on dynamics")
    while "a1" not in P.active().current()["question"]:
        _turn("skip")
    assert _turn("minus 3.86 metres per second squared")[0].startswith("Correct!")


def test_stop_and_progress(quiz):
    P.build_cards()
    _turn("quiz me")
    _turn("skip")
    reply, _ = _turn("how am I doing?")
    assert reply.startswith("You got 0 of 1 right.")
    assert _turn("stop the quiz")[0] == "Okay, stopping here. You got 0 of 1 right." and P.active() is None
    assert _turn("what time is it?") is None                                   # no quiz: normal ARYA


def test_what_should_i_practise(quiz):
    reply, _ = _turn("what should I practise?")
    assert "new cards" in reply and "quiz me" in reply


def test_quiz_runs_inside_aryas_answer_flow(quiz):
    async def turn(m):
        return "".join([s async for s in llm.stream_with_tools(m)])
    assert asyncio.run(turn("quiz me")).startswith("Let's practise")
    assert P.active() is not None
