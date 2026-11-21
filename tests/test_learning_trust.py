"""Learning reads only what was clearly said to ARYA (arya/memory/learning.py + arya/core/addressee.py)."""
import asyncio
import time

import pytest

from arya.core import addressee
from arya.memory import learning
from arya.memory.database import note_source, sources


@pytest.mark.parametrize("source,p,ok", [
    ("wake", None, True), ("name", None, True), ("typed", None, True), ("phone", None, True),
    ("push-to-talk", None, True), ("follow:rule", None, True), ("follow:answer", None, True),
    ("follow:classifier", 0.93, True), ("follow:classifier", 0.55, False), ("follow:classifier", None, False),
    ("follow:model", None, False), ("follow:unfiltered", None, False), ("side-talk", None, False),
    (None, None, None),
])
def test_trusted(source, p, ok):
    assert addressee.trusted(source, p) is ok


def test_saved_lines_carry_their_source():
    import arya.core.llm as llm
    addressee.set_origin("follow:classifier", 0.81)
    try:
        mid = llm.save_message("user", "and what about tomorrow?")
    finally:
        addressee.set_origin("typed")
    aid = llm.save_message("assistant", "Tomorrow it's sunny.")
    got = sources([mid, aid])
    assert got[mid] == ("follow:classifier", pytest.approx(0.81)) and aid not in got


def _conv(lines, srcs):
    import arya.core.llm as llm
    ids = []
    for (role, text), src in zip(lines, srcs):
        mid = llm.save_message(role, text)
        if src:
            note_source(mid, *src)
        ids.append(mid)
    return learning.Conversation(ids[0], ids[-1], time.time(), lines, ids)


def test_side_talk_and_its_answer_are_left_out():
    cv = _conv([("user", "open YouTube Music"), ("assistant", "Opening YouTube Music."),
                ("user", "Does even plan, but really."), ("assistant", "Then perhaps a light walk?"),
                ("user", "I'm building a robot for my course"), ("assistant", "Nice.")],
               [("wake", None), None, ("follow:model", None), None, ("typed", None), None])
    clean, dropped = learning.said_to_arya(cv)
    assert dropped == 1
    assert [t for _r, t in clean.lines] == ["open YouTube Music", "Opening YouTube Music.",
                                            "I'm building a robot for my course", "Nice."]


def test_lines_without_a_source_are_judged_again(monkeypatch):
    from arya.research import addressee_model as M
    monkeypatch.setattr(M, "load_model", lambda path=M.MODEL_PATH: {"lo": 0.4, "hi": 0.7})
    monkeypatch.setattr(M, "featurize", lambda *a, **k: None)
    monkeypatch.setattr(M, "probability", lambda x, m: 0.2)
    assert learning._judge_again("pause the music", "") is True          # a rule
    assert learning._judge_again("NJ, my sister is called Mira", "") is True   # the name
    assert learning._judge_again("So I'll just put it in there.", "Okay.") is False
    monkeypatch.setattr(M, "load_model", lambda path=M.MODEL_PATH: None)
    assert learning._judge_again("So I'll just put it in there.", "Okay.") is True   # no classifier: as before


def test_conversations_from_the_eval_stay_trusted():
    cv = learning.Conversation(1, 2, time.time(), [("user", "hi"), ("assistant", "hello")])
    assert learning.line_trust(cv) == [True, True]


def test_learn_from_never_sees_side_talk(monkeypatch):
    seen = {}

    async def propose(transcript_text):
        seen["t"] = transcript_text
        return {"memories": [{"kind": "preference", "text": "The user prefers to be spontaneous and not plan ahead.",
                              "evidence": "Does even plan, but really", "confidence": 0.9}], "summary": ""}
    monkeypatch.setattr(learning, "propose", propose)
    import tempfile
    from pathlib import Path
    monkeypatch.setattr(learning, "DB_OVERRIDE", Path(tempfile.mkdtemp()) / "learn.db")
    cv = _conv([("user", "I want to set up my week, tell me what's on my calendar for Monday and Tuesday please"),
                ("assistant", "Monday: lab. Tuesday: free."),
                ("user", "Does even plan, but really."), ("assistant", "Then perhaps a light walk?")],
               [("wake", None), None, ("follow:model", None), None])
    stats = asyncio.run(learning.learn_from(cv))
    assert "Does even plan" not in seen["t"]
    assert stats["kept"] == 0 and stats["side talk dropped"] == 1


@pytest.mark.parametrize("why,origin", [
    ("rule", ("follow:rule", None)), ("classifier p=0.82", ("follow:classifier", 0.82)),
    ("model (0.3s)", ("follow:model", None)), ("answer to its question", ("follow:answer", None)),
])
def test_follow_up_origin(why, origin):
    from arya.server.websocket import _follow_up_origin
    assert _follow_up_origin(why) == origin


def test_not_for_you_marks_the_line_as_side_talk(tmp_path, monkeypatch):
    import arya.core.llm as llm
    monkeypatch.setenv("ARYA_DATA_DIR", str(tmp_path))
    addressee.set_origin("follow:model")
    mid = llm.save_message("user", "Now we'll talk.")
    addressee.set_origin("typed")
    llm.save_message("assistant", "Sure.")

    async def run():
        return "".join([s async for s in llm.stream_with_tools("That wasn't for you.")])
    asyncio.run(run())
    assert sources([mid])[mid][0] == "side-talk"
