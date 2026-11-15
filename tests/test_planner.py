"""M3: exam planner — exam dates, a daily plan from your data, and the briefing line."""
import asyncio
from datetime import date, timedelta

import pytest

import arya.core.llm as llm
from arya.study import planner as P
from arya.study import practice

TODAY = date(2026, 11, 15)


@pytest.mark.parametrize("text, expected", [
    ("my exam is on 12 February", date(2027, 2, 12)),
    ("on Feb 12th", date(2027, 2, 12)),
    ("on 12.02.2027", date(2027, 2, 12)),
    ("2027-02-12", date(2027, 2, 12)),
    ("on 12/2", date(2027, 2, 12)),
    ("the 3rd of March 2027", date(2027, 3, 3)),
    ("in 3 weeks", date(2026, 12, 6)),
    ("next Monday", date(2026, 11, 16)),
    ("tomorrow", date(2026, 11, 16)),
    ("on 10 November", date(2027, 11, 10)),             # already passed this year -> next year
    ("it takes 2.5 m/s", None),
    ("on 31 February", None),
])
def test_parse_date(text, expected):
    assert P.parse_date(text, TODAY) == expected


@pytest.mark.parametrize("message, course", [
    ("My Dynamics exam is on 12 February", "Dynamics"),
    ("my control systems exam is in 3 weeks", "Control"),            # mapped to the quiz's Control cards
    ("the final exam for Thermodynamics is on 20.2.", "Thermodynamics"),
    ("my exam is next Monday", None),
])
def test_course_in(kb, message, course):
    assert P.course_in(message) == course


@pytest.fixture
def clean(kb, monkeypatch):
    with P._db() as c:
        c.execute("DELETE FROM exams")
    with practice._db() as c:
        c.execute("DELETE FROM cards")
        c.execute("DELETE FROM reviews")
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    yield
    with P._db() as c:
        c.execute("DELETE FROM exams")


def test_add_list_remove(clean):
    when = date.today() + timedelta(days=21)
    reply = P.handle(f"My Dynamics exam is on {when.day}.{when.month}.{when.year}")
    assert reply.startswith("Saved: Dynamics exam on") and "in 21 days" in reply
    P.handle(f"my Dynamics exam is on {when.day + 1 if when.day < 28 else 1}.{when.month}.{when.year}")
    assert len(P.upcoming()) == 1                                       # a new date replaces the old one
    assert P.handle("When are my exams?").startswith("Your exams: Dynamics on")
    assert P.handle("Remove the Dynamics exam") == "Removed the Dynamics exam."
    assert "no exams saved" in P.handle("when are my exams?")


def test_past_date_and_missing_course(clean):
    assert "Which course" in P.handle("my exam is next Monday")
    past = date.today() - timedelta(days=3)
    assert "has already passed" in P.handle(f"my Dynamics exam is on {past.isoformat()}")


def test_plan_focuses_on_the_next_exam(clean):
    soon = date.today() + timedelta(days=10)
    P.add_exam("Dynamics", soon)
    P.add_exam("Control", soon + timedelta(days=30))
    today = date.today().isoformat()
    with practice._db() as c:
        for i in range(3):
            c.execute("INSERT INTO cards (kind, course, key, question, answer, due) VALUES "
                      "('formula','Dynamics',?,?,'x',?)", (f"k{i}", f"q{i}", today))
        c.execute("INSERT INTO cards (kind, course, key, question, answer, name, value, unit) VALUES "
                  "('exercise','Dynamics','exercise:2-4:a','Exercise 2-4: what is a?','x','a',1.0,'m/s2')")
    plan = P.plan_today()
    assert plan.startswith("Your next exam is Dynamics on") and "in 10 days" in plan
    assert "review the 3 Dynamics cards due" in plan and "exercise 2-4" in plan
    assert "Control follows on" in plan


def test_close_to_the_exam_means_review_only(clean):
    P.add_exam("Dynamics", date.today() + timedelta(days=2))
    with practice._db() as c:
        c.execute("INSERT INTO cards (kind, course, key, question, answer, due) VALUES "
                  "('formula','Dynamics','k','q','x',?)", (date.today().isoformat(),))
        c.execute("INSERT INTO cards (kind, course, key, question, answer) VALUES ('formula','Dynamics','n','q2','x')")
    plan = P.plan_today()
    assert "review the 1 Dynamics card due" in plan and "practise" not in plan and "stick to reviewing" in plan


def test_briefing_line(clean):
    P.add_exam("Dynamics", date.today() + timedelta(days=12))
    assert P.briefing_line().startswith("Dynamics exam in 12 days (") and "no practice cards due today" in P.briefing_line()


def test_planner_runs_inside_aryas_answer_flow(clean):
    async def turn(m):
        return "".join([s async for s in llm.stream_with_tools(m)])
    assert asyncio.run(turn("What should I study today?")).startswith("You have no exams saved")
    assert asyncio.run(turn("I have an exam in control systems in 3 weeks")).startswith("Saved: Control exam on")


# ── Progress (the app's Study card) ──────────────────────────────────────────

def test_progress_summary(clean):
    import time as _time
    from datetime import datetime

    P.add_exam("Dynamics", date.today() + timedelta(days=12))
    today = date.today().isoformat()
    with practice._db() as c:
        c.execute("INSERT INTO cards (kind, course, key, question, answer, due, reps) VALUES "
                  "('exercise','Dynamics','exercise:1-1:t','q1','x',?,1)", (today,))
        c.execute("INSERT INTO cards (kind, course, key, question, answer) VALUES ('exercise','Dynamics','e2','q2','x')")
        c.execute("INSERT INTO cards (kind, course, key, question, answer) VALUES ('formula','Control','f1','q3','x')")
        cid = c.execute("SELECT id FROM cards WHERE key = 'exercise:1-1:t'").fetchone()[0]
        now = _time.time()
        for days_ago, quality in [(0, 5), (0, 1), (1, 5), (2, 4)]:          # practised 3 days in a row
            c.execute("INSERT INTO reviews (card_id, ts, quality) VALUES (?, ?, ?)", (cid, now - days_ago * 86400, quality))
    p = P.progress()
    assert p["exams"][0]["course"] == "Dynamics" and p["exams"][0]["days"] == 12
    dyn = p["courses"][0]
    assert dyn["course"] == "Dynamics" and dyn["due"] == 1 and dyn["exercises"] == 2 and dyn["exercises_done"] == 1
    assert dyn["accuracy"] == 0.75 and dyn["answers"] == 4 and dyn["exam_days"] == 12
    assert p["courses"][1]["course"] == "Control" and p["courses"][1]["accuracy"] is None
    assert p["streak"] == 3 and p["due_today"] == 1
    assert len(p["week"]) == 7 and p["week"][-1] == {"day": datetime.now().strftime("%a"), "date": today,
                                                     "answers": 2, "right": 1}


def test_progress_api_needs_the_token(clean):
    from fastapi.testclient import TestClient

    import arya.server.websocket as ws
    from arya.server import security

    client = TestClient(ws.app, base_url="http://127.0.0.1:8000")
    assert client.get("/api/progress").status_code == 401
    r = client.get("/api/progress", headers={"X-ARYA-Token": security.TOKEN})
    assert r.status_code == 200 and set(r.json()) == {"exams", "courses", "due_today", "streak", "week"}
