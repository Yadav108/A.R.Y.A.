"""Modes (PR 2): everyday / study / work — persona, retrieval, nudges, switching, suggestions, to-dos.
State is kept in memory (modes.use_memory) and to-dos in a scratch database; no model is called."""
import asyncio
from datetime import datetime

import pytest

import arya.core.llm as llm
from arya.core import modes, policy
from arya.tools import tasks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    modes.use_memory("default")
    monkeypatch.setattr(tasks, "DB_OVERRIDE", tmp_path / "tasks.db")
    monkeypatch.setattr(llm, "save_message", lambda role, text: 1)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])
    policy.cancel_pending()
    yield
    policy.cancel_pending()


def _say(text):
    async def go():
        return "".join([s async for s in llm.stream_with_tools(text)])
    return asyncio.run(go())


# ── Persona ──────────────────────────────────────────────────────────────────

def test_everyday_prompt_is_not_a_tutor():
    p = llm._system_prompt()
    assert "Mode: everyday" in p and "personal assistant" in p
    assert "Mechatronics engineering student" not in p and "like a tutor" not in p
    assert "Don't steer the conversation towards coursework" in p


def test_study_and_work_prompts():
    modes.set_mode("study")
    assert "Mode: study" in llm._system_prompt() and "tutor" in llm._system_prompt()
    modes.set_mode("work")
    p = llm._system_prompt()
    assert "Mode: work" in p and "concise, technical" in p


def test_persona_comes_before_the_clock():
    """The cached prompt prefix must not change between turns in the same mode."""
    p = llm._system_prompt()
    assert p.index("Mode: everyday") < p.index("Current local date and time")


@pytest.mark.parametrize("mode, start", [("default", "I'm your personal assistant"),
                                         ("study", "I'm your study companion"), ("work", "I'm your work partner")])
def test_capabilities_per_mode(mode, start):
    modes.set_mode(mode)
    assert _say("What can you do?").startswith(start)


# ── Switching by voice ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text, mode", [("work mode", "work"), ("switch to study mode", "study"),
                                        ("Hey ARYA, go into work mode please", "work"), ("study mode on", "study"),
                                        ("back to normal", "default"), ("normal mode", "default"),
                                        ("leave work mode", "default")])
def test_switch_by_voice(text, mode):
    if mode == "default":
        modes.set_mode("work")
    assert modes.handle(text)
    assert modes.current_name() == mode


def test_what_mode():
    modes.set_mode("work")
    assert modes.handle("what mode are you in?") == "I'm in work mode."
    assert modes.current_name() == "work"


@pytest.mark.parametrize("text", ["what mode of vibration is the first one", "what is the list of Laplace pairs",
                                  "I work in the lab", "the study found that", "explain the normal force"])
def test_not_mode_commands(text):
    assert not modes.matches(text)


@pytest.mark.parametrize("text", ["work mode", "back to normal", "what mode are you in?", "what's on my list?",
                                  "add call the plumber to my list"])
def test_smart_listening_counts_them_as_for_arya(text):
    from arya.core import addressee
    assert addressee.quick_verdict(text) is True


def test_already_in_mode():
    modes.set_mode("work")
    assert modes.handle("work mode") == "I'm already in work mode."


def test_listeners_hear_switches():
    heard = []
    modes.on_change(heard.append)
    try:
        modes.set_mode("work")
        modes.set_mode("work")                   # no change: no event
        assert heard == ["work"]
    finally:
        modes._listeners.remove(heard.append)


# ── Study sessions ───────────────────────────────────────────────────────────

def test_study_session_switches_and_comes_back():
    modes.set_mode("work")
    modes.enter_study()
    assert modes.current_name() == "study"
    modes.leave_study()
    assert modes.current_name() == "work"


def test_leaving_study_does_not_override_a_manual_switch():
    modes.enter_study()
    modes.set_mode("work")                       # you switched during the session
    modes.leave_study()
    assert modes.current_name() == "work"


# ── Work folders ─────────────────────────────────────────────────────────────

def test_add_work_folder(tmp_path, monkeypatch):
    lab = tmp_path / "Lab"
    lab.mkdir()
    monkeypatch.setattr(modes, "_resolve_folder", lambda spoken: lab if "lab" in spoken.lower() else None)
    from arya.knowledge import library
    monkeypatch.setattr(library, "start_background_indexing", lambda progress=None: None)
    assert "Added Lab to work mode" in modes.handle("add my Lab folder to work mode")
    assert lab in modes.work_dirs()
    assert "Lab" in modes.handle("which folders are in work mode?")
    assert "couldn't find" in modes.handle("add the Nowhere folder to work mode")


def test_sources(tmp_path, monkeypatch):
    from arya.knowledge import library
    lab = tmp_path / "Lab"
    assert modes.source_of(str(library.CORE_NOTES)) == "core"
    assert modes.source_of(str(library.CORRECTIONS)) == "notes"
    assert modes.source_of(str(lab / "paper.pdf"), [lab]) == "work"
    assert modes.source_of(str(tmp_path / "Dynamics" / "ex.pdf"), [lab]) == "course"


# ── Retrieval per mode ───────────────────────────────────────────────────────

def _passage(source, sim, title="Doc"):
    from arya.knowledge.library import Passage
    return Passage(1 if source == "course" else 2, 1, title, "c", "pdf", 1, "text " * 80, sim, False, source=source)


@pytest.mark.parametrize("mode, kept", [("study", ["course"]), ("default", []), ("work", [])])
def test_course_material_needs_a_clear_match_outside_study(mode, kept, monkeypatch):
    from arya.knowledge import library
    monkeypatch.setattr(library, "search", lambda q, k=5: [_passage("course", 0.48)])
    assert [p.source for p in library.relevant("how's it going", mode=mode)] == kept


def test_work_documents_first_in_work_mode(monkeypatch):
    from arya.knowledge import library
    monkeypatch.setattr(library, "search", lambda q, k=5: [_passage("work", 0.47), _passage("course", 0.5)])
    assert [p.source for p in library.relevant("methods section", mode="work")] == ["work"]
    assert [p.source for p in library.relevant("methods section", mode="study")] == ["course"]


def test_excerpts_are_labelled_by_source():
    text = llm.grounding_prompt([_passage("work", 0.6)], "methods")
    assert "Excerpts from their work documents" in text and "course material" not in text.split("\n\n")[1]


# ── Nudges ───────────────────────────────────────────────────────────────────

def test_cards_nudge_only_in_study_mode():
    from arya.core import proactive as P
    s = P.Sensors(now=datetime(2026, 11, 16, 18, 0), due_cards=5, practised_today=False,
                  nudges=modes.MODES["default"].nudges)
    assert P.Nudger(last_spoken=0).check(s) is None
    s.nudges = modes.MODES["study"].nudges
    assert "practice card" in (P.Nudger(last_spoken=0).check(s) or "")


# ── Suggestions ──────────────────────────────────────────────────────────────

def _fake_scores(mapping):
    return lambda message: mapping.get(message, {"default": 0.6, "study": 0.3, "work": 0.3})


def test_suggests_after_two_of_three(monkeypatch):
    work_like = {"default": 0.3, "study": 0.3, "work": 0.7}
    monkeypatch.setattr(modes, "scores", lambda m: work_like)
    assert modes.suggestion("why does my script crash on startup", now=1000) is None      # one isn't enough
    assert modes.suggestion("review the measurement data for the lab", now=1001) == "work"
    # asked: not again for 30 minutes
    assert modes.suggestion("refactor the parser module please", now=1002) is None
    assert modes.suggestion("refactor the parser module please", now=1003) is None


def test_no_suggestion_when_it_fits(monkeypatch):
    monkeypatch.setattr(modes, "scores", lambda m: {"default": 0.7, "study": 0.3, "work": 0.4})
    for i in range(4):
        assert modes.suggestion("what should we cook tonight", now=2000 + i) is None


def test_no_suggestion_during_a_study_session(monkeypatch):
    from arya.study import session
    monkeypatch.setattr(session, "current", lambda: {"id": 1})
    monkeypatch.setattr(modes, "scores", lambda m: {"default": 0.3, "study": 0.3, "work": 0.8})
    for i in range(3):
        assert modes.suggestion("debug the python script now", now=3000 + i) is None


def test_saying_yes_to_a_suggestion_switches():
    policy.offer("switch_mode", {"mode": "work"}, modes.suggestion_question("work"))
    assert _say("yes") .startswith("Work mode")
    assert modes.current_name() == "work"
    policy.offer("switch_mode", {"mode": "study"}, modes.suggestion_question("study"))
    assert _say("no thanks") == "Okay, staying in work mode."
    assert modes.current_name() == "work"


# ── To-do lists ──────────────────────────────────────────────────────────────

def test_todo_list():
    assert tasks.handle("add email Dr. Weber the results to my work tasks") == \
        "Added to your work list: email Dr. Weber the results."
    assert tasks.handle("put buy milk on my list") == "Added to your personal list: buy milk."
    assert tasks.handle("new task: book the lab", mode="work") == "Added to your work list: book the lab."
    assert tasks.handle("what's on my list?") == "On your personal list: buy milk."
    assert tasks.handle("what are my work tasks?") == \
        "On your work list: email Dr. Weber the results; book the lab."
    assert tasks.handle("what's on my list?", mode="work").startswith("On your work list")
    assert tasks.handle("mark book the lab as done") == "Done: book the lab. 1 left on your work list."
    assert tasks.handle("remove buy milk from my list") == "Removed buy milk from your personal list."
    assert tasks.handle("what's on my list?") == "Your personal list is empty."
    assert "work" in tasks.briefing_line("work")


@pytest.mark.parametrize("text", ["what is the list of Laplace transform pairs", "mark this email as read",
                                  "what do I have to do in exercise 3", "add salt to the soup"])
def test_not_todos(text):
    assert tasks.handle(text) is None


# ── App and phone switches ───────────────────────────────────────────────────

def test_phone_mode_switch(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from arya.server import phone as P
    monkeypatch.setattr(P, "SETTINGS_FILE", tmp_path / "phone.json")
    monkeypatch.setattr(P, "TOKEN_FILE", tmp_path / "phone_token")
    monkeypatch.setattr(P, "_state", P._State())
    P._state.hosts = {"testserver"}
    monkeypatch.setattr(P, "client_ok", lambda host: True)
    c = TestClient(P.app)
    assert c.post("/api/mode", json={"mode": "work"}, headers={"X-ARYA": "1"}).status_code == 401
    c.post("/api/pair", json={"key": P.token()}, headers={"X-ARYA": "1"})
    assert c.post("/api/mode", json={"mode": "work"}, headers={"X-ARYA": "1"}).json()["mode"] == "work"
    assert modes.current_name() == "work"
    assert c.get("/api/hello").json()["mode"] == "work"
    assert c.post("/api/mode", json={"mode": "party"}, headers={"X-ARYA": "1"}).status_code == 400
