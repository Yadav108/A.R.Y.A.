"""Learning from conversations (PR 3): the code-side checks, storage, recall and voice commands.
The model is faked here; `python -m arya.eval --learning` measures the real one."""
import asyncio
import json
import sqlite3
import time

import pytest

from arya.memory import learning as L


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "DB_OVERRIDE", tmp_path / "learn.db")
    with sqlite3.connect(tmp_path / "learn.db") as c:
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp TEXT)")
    from arya.memory import profile
    monkeypatch.setattr(profile, "items", lambda: [])
    yield tmp_path


def _cv(*lines, first=1):
    return L.Conversation(first=first, last=first + len(lines) - 1, ended=time.time(), lines=list(lines))


def _fake_model(monkeypatch, memories, summary="A chat."):
    async def propose(text):
        return {"memories": memories, "summary": summary}
    monkeypatch.setattr(L, "propose", propose)


ROBOT = ("user", "I'm working on a line-following robot for my project course, how does PID work?")


# ── The checks ───────────────────────────────────────────────────────────────

def test_good_memory_passes():
    item = {"kind": "project", "text": "The user is building a line-following robot.",
            "evidence": "working on a line-following robot", "confidence": 0.9}
    assert L.check(item, [ROBOT[1]]) == (True, "")


@pytest.mark.parametrize("item, why", [
    ({"kind": "fact", "text": "The user likes jazz.", "evidence": "You might enjoy some jazz", "confidence": 0.9},
     "evidence not in the user's words"),                                   # ARYA said it
    ({"kind": "fact", "text": "The user has a toothache.", "evidence": "my tooth has been hurting", "confidence": 0.9},
     "sensitive"),
    ({"kind": "fact", "text": "The user is tired.", "evidence": "I'm so tired today", "confidence": 0.9},
     "passing state or one-off request"),
    ({"kind": "fact", "text": "The user asked about the Coriolis effect.", "evidence": "What is the Coriolis",
      "confidence": 0.9}, "passing state or one-off request"),
    ({"kind": "fact", "text": "Likes robots.", "evidence": "line-following robot", "confidence": 0.9},
     "not a sentence about the user"),
    ({"kind": "fact", "text": "The user maybe likes robots.", "evidence": "line-following robot", "confidence": 0.3},
     "not confident"),
    ({"kind": "mood", "text": "The user is fine.", "evidence": "line-following robot", "confidence": 0.9},
     "unknown kind"),
])
def test_bad_memories_are_rejected(item, why):
    user = [ROBOT[1], "I'm so tired today", "my tooth has been hurting all week", "What is the Coriolis effect"]
    assert L.check(item, user) == (False, why)


def test_someone_elses_trait_is_not_the_users():
    line = "My sister Priya is visiting next month, she loves hiking"
    wrong = {"kind": "fact", "text": "The user enjoys hiking.", "evidence": "she loves hiking", "confidence": 0.9}
    right = {"kind": "person", "text": "The user's sister Priya loves hiking.", "evidence": "she loves hiking",
             "confidence": 0.9}
    assert L.check(wrong, [line]) == (False, "about someone else")
    assert L.check(right, [line]) == (True, "")


def test_replacing_someone_drops_memories_about_them():
    L.store({"kind": "person", "text": "The user's lab partner is Anna.", "evidence": "x", "confidence": 0.9}, "1-2")
    L.store({"kind": "routine", "text": "The user does the control lab with Anna every Wednesday.", "evidence": "x",
             "confidence": 0.9}, "1-2")
    L.store({"kind": "person", "text": "The user's lab partner is now Ben.", "evidence": "x", "confidence": 0.9}, "3-4")
    assert [m["text"] for m in L.memories(None)] == ["The user's lab partner is now Ben."]


def test_evidence_must_be_in_one_line():
    assert L.evidence_ok("line-following robot for my project", [ROBOT[1]])
    assert not L.evidence_ok("robot for my sister", [ROBOT[1], "my sister is visiting"])
    assert not L.evidence_ok("robot", [ROBOT[1]])                          # one word proves nothing


# ── Storing ──────────────────────────────────────────────────────────────────

def test_confident_kept_unsure_waits_for_a_second_mention():
    sure = {"kind": "routine", "text": "The user goes to the gym on Mondays.", "evidence": "gym", "confidence": 0.9}
    unsure = {"kind": "preference", "text": "The user likes long walks.", "evidence": "walks", "confidence": 0.6}
    assert L.store(sure, "1-2") == "new" and L.store(unsure, "1-2") == "new"
    assert [m["text"] for m in L.memories("active")] == ["The user goes to the gym on Mondays."]
    assert [m["text"] for m in L.memories("candidate")] == ["The user likes long walks."]
    L.store(unsure, "1-2")                                                  # same conversation: not a second mention
    assert L.memories("candidate")
    L.store(unsure, "7-9")                                                  # another conversation: now it counts
    assert {m["text"] for m in L.memories("active")} >= {"The user likes long walks."}


def test_newer_wording_wins():
    L.store({"kind": "person", "text": "The user's lab partner is Anna.", "evidence": "x", "confidence": 0.9}, "1-2")
    assert L.store({"kind": "person", "text": "The user's lab partner is Ben.", "evidence": "x",
                    "confidence": 0.9}, "5-6") == "updated"
    assert [m["text"] for m in L.memories(None)] == ["The user's lab partner is Ben."]


def test_explicit_memories_are_not_duplicated(monkeypatch):
    from arya.memory import profile
    monkeypatch.setattr(profile, "items", lambda: [{"text": "The user prefers short answers"}])
    assert L.store({"kind": "preference", "text": "The user prefers short answers.", "evidence": "x",
                    "confidence": 0.9}, "1-2") == "duplicate of explicit"
    assert not L.memories(None)


# ── A whole conversation ─────────────────────────────────────────────────────

def test_learn_from_keeps_only_what_passes(monkeypatch):
    _fake_model(monkeypatch, [
        {"kind": "project", "text": "The user is building a line-following robot.",
         "evidence": "working on a line-following robot", "confidence": 0.9},
        {"kind": "fact", "text": "The user likes jazz.", "evidence": "enjoy some jazz", "confidence": 0.9},
    ], summary="PID for the user's robot.")
    cv = _cv(ROBOT, ("assistant", "You might enjoy some jazz while you work."), ("user", "keep it short please"))
    stats = asyncio.run(L.learn_from(cv))
    assert stats["proposed"] == 2 and stats["kept"] == 1
    assert [m["text"] for m in L.memories()] == ["The user is building a line-following robot."]
    with L._db() as c:
        assert c.execute("SELECT summary FROM episodes").fetchone()[0] == "PID for the user's robot."


def test_tiny_conversations_are_not_read(monkeypatch):
    called = []

    async def propose(text):
        called.append(text)
        return {"memories": [], "summary": ""}
    monkeypatch.setattr(L, "propose", propose)
    asyncio.run(L.learn_from(_cv(("user", "louder"), ("assistant", "Volume up."))))
    assert called == []


def test_transcript_marks_who_said_what():
    t = L.transcript(_cv(("user", "hi there"), ("assistant", "Hello! " * 100)))
    assert t.startswith("USER: hi there\nARYA: ") and len(t.splitlines()[1]) <= 170


def test_garbled_model_output_is_harmless(monkeypatch):
    from arya.core import llm_local

    async def chat_local(*a, **k):
        return {"content": "not json at all"}
    monkeypatch.setattr(llm_local, "chat_local", chat_local)
    assert asyncio.run(L.propose("USER: hi")) == {"memories": [], "summary": ""}


# ── Conversations from the history ───────────────────────────────────────────

def test_conversations_split_at_quiet_gaps(scratch):
    rows = [(1, "user", "a", "2026-11-16 10:00:00"), (2, "assistant", "b", "2026-11-16 10:00:05"),
            (3, "user", "c", "2026-11-16 10:30:00"), (4, "assistant", "d", "2026-11-16 10:30:05"),
            (5, "user", "e", "2026-11-16 11:00:00")]
    with sqlite3.connect(scratch / "learn.db") as c:
        c.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", rows)
    now = L._parse_ts("2026-11-16 11:05:00")
    convs = L.finished_conversations(0, now=now)
    assert [(cv.first, cv.last) for cv in convs] == [(1, 2), (3, 4)]         # the last one isn't over yet
    assert [(cv.first, cv.last) for cv in L.finished_conversations(2, now=now)] == [(3, 4)]


# ── Recall ───────────────────────────────────────────────────────────────────

def test_recall_brings_the_related_memory():
    L.store({"kind": "project", "text": "The user is building a line-following robot.", "evidence": "x",
             "confidence": 0.9}, "1-2")
    L.store({"kind": "person", "text": "The user's sister Priya loves hiking.", "evidence": "x",
             "confidence": 0.9}, "1-2")
    L.store({"kind": "preference", "text": "The user is vegetarian.", "evidence": "x", "confidence": 0.9}, "1-2")
    # preferences apply to every answer (MiniLM rates "dinner" ~ "vegetarian" at only 0.16)
    assert "The user is vegetarian." in L.recall("any recipe ideas for dinner tonight?")
    robot = L.recall("how should I tune the controller of my robot?")
    assert "The user is building a line-following robot." in robot
    assert not any("Priya" in t for t in robot)                       # unrelated facts stay out
    block = L.prompt_block("any recipe ideas for dinner tonight?")
    assert "learned about the user" in block and "vegetarian" in block


def test_slot_updates():
    assert L._slot("The user's lab partner is Ben.") == "lab partner"
    assert L._slot("The user's new lab partner is Ben.") == "lab partner"
    assert L._slot("The user likes jazz.") is None
    L.store({"kind": "fact", "text": "The user likes jazz.", "evidence": "x", "confidence": 0.9}, "1-2")
    L.store({"kind": "fact", "text": "The user likes rock music.", "evidence": "x", "confidence": 0.9}, "3-4")
    assert len(L.memories(None)) == 2                                   # two likes, not an update


def test_learned_memories_never_go_to_the_cloud(monkeypatch):
    import arya.core.llm as llm
    L.store({"kind": "preference", "text": "The user is vegetarian.", "evidence": "x", "confidence": 0.9}, "1-2")
    monkeypatch.setattr(llm, "semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(llm, "_groq_allowed", lambda: True)
    assert "vegetarian" not in asyncio.run(llm._get_semantic_context("dinner ideas?", set()))
    monkeypatch.setattr(llm, "_groq_allowed", lambda: False)
    assert "vegetarian" in asyncio.run(llm._get_semantic_context("dinner ideas?", set()))


# ── Voice ────────────────────────────────────────────────────────────────────

def test_what_have_you_learned():
    assert L.handle("what have you learned about me?").startswith("Nothing yet")
    L.store({"kind": "preference", "text": "The user is vegetarian.", "evidence": "x", "confidence": 0.9}, "1-2")
    reply = L.handle("what have you learned about me?")
    assert "You are vegetarian" in reply and "forget" in reply


def test_forget_learned(monkeypatch):
    from arya.memory import profile
    L.store({"kind": "fact", "text": "The user is vegetarian.", "evidence": "x", "confidence": 0.9}, "1-2")
    monkeypatch.setattr(profile, "forget", lambda what: 0)
    assert profile.handle("forget that I'm vegetarian") == "Okay, I forgot that."
    assert not L.memories(None)


def test_pause_and_resume():
    assert "paused" in L.handle("pause learning")
    assert L.paused()
    assert "back on" in L.handle("resume learning")
    assert not L.paused()


def test_off_the_record(scratch):
    with sqlite3.connect(scratch / "learn.db") as c:
        c.executemany("INSERT INTO messages VALUES (?, ?, ?, datetime('now'))",
                      [(1, "user", "a"), (2, "assistant", "b")])
    L.store({"kind": "fact", "text": "The user is vegetarian.", "evidence": "x", "confidence": 0.9}, "1-2")
    assert "won't learn" in L.handle("don't remember this conversation")
    assert not L.memories(None)                                        # what it learned from it is gone too
    assert json.loads(L._get("off_record")) == [2]


def test_what_did_we_talk_about():
    assert "don't have a summary" in L.handle("what did we talk about yesterday?")
    L.save_episode(_cv(ROBOT), "PID for the user's robot.", "study")
    assert "PID for the user's robot." in L.handle("what did we talk about last time?")


@pytest.mark.parametrize("text", ["remember that I prefer short answers", "what is a learning rate",
                                  "explain machine learning", "I'm learning German"])
def test_not_learning_commands(text):
    assert L.handle(text) is None
