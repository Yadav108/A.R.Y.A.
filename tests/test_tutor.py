"""M2: exercise tutor — results read off the answers page, answers checked by code, walkthrough sessions."""
import asyncio

import pytest

from arya.study import tutor as T

REAL_1_13 = ("1-13) \ntA = π/ω1 = 1.57 s; α2 = 2 π/tA2 = 2.55/s2; ω2(tA) = 4/s; \nr = 1.41 m; vL = r ω1 = 2.83 m/s; "
             "aL = r ω12 = 5.66 m/s2; \nvR = L2 ω2(tA) = 4 m/s; aRt = L2 α2 = 2.55 m/s2; aRn = L2 ω2(tA)2 = 16 m/s2; \n"
             "aCo = 2 ω1 vR = 16 m/s2; \nv = ඥ(vR − vL sin45°)2 + (vL cos45°)2 = 2.83 m/s; βv = 45°; \n"
             "a = ඥ(aL cos 45° −aRt)2 + (aL sin 45° + aRn −aCo)2 = 4.26 m/s2; βa = 70°")
REAL_3_14 = "3-14) \nPHR = η P = 142.5 W = FHR v = (m g sin β + µR m g cos β + k v2) v \n→ (e.g. by iteration) v = 2.61 m/s = 9.4 km/h"


def _results(text):
    return {r.name: (r.value, r.unit) for r in T.parse_results(text)}


def test_parse_real_answers_page():
    r = _results(REAL_1_13)
    assert r["tA"] == (1.57, "s") and r["α2"] == (2.55, "/s2") and r["v"] == (2.83, "m/s")
    assert r["a"] == (4.26, "m/s2") and r["βa"] == (70.0, "°") and len(r) == 14


def test_only_final_values_count():
    assert _results("3-3) dW = F r sin ϕ dϕ; W = ʃ dW = 2 F r = 255 J") == {"W": (255.0, "J")}
    res = T.parse_results(REAL_3_14)
    v = next(x for x in res if x.name == "v")
    assert (v.value, v.unit) == (9.4, "km/h") and (2.61, "m/s") in v.alternates


@pytest.mark.parametrize("claim, verdict", [
    ("I got v = 2.83 m/s", "Correct! v = 2.83 m/s"),
    ("alpha 2 is 2.55", "Correct! α2 = 2.55/s2"),
    ("I got 4.26", "Correct! a = 4.26 m/s2"),
    ("I think it is 70 degrees for beta a", "Correct! βa = 70 °"),
    ("v is 2.8 m/s", "Close: that's within 1 %"),
    ("a = 4.4 m/s²", "Close: that's within 3 %"),
    ("a = 4.26 m/s", "Check your unit: the answer sheet gives a in metres per second squared"),
    ("I got a = 6 m/s2", "Not quite: that doesn't match the answer sheet for a"),
    ("my answer is 3.5 m/s", "Not quite: that doesn't match any speed on the answer sheet"),
])
def test_check_against_answers(claim, verdict):
    assert T.check(T.parse_claim(claim), T.parse_results(REAL_1_13)).startswith(verdict)


@pytest.mark.parametrize("claim, verdict", [
    ("v = 2.61 m/s", "Correct! v = 2.61 m/s"),                   # answered in the student's unit
    ("I got 9.4 km/h", "Correct! v = 9.4 km/h"),
    ("the speed is 9 km/h", "Close"),
])
def test_check_converts_units(claim, verdict):
    assert T.check(T.parse_claim(claim), T.parse_results(REAL_3_14)).startswith(verdict)


def test_wrong_answer_never_reveals_the_value():
    verdict = T.check(T.parse_claim("I got a = 6 m/s2"), T.parse_results(REAL_1_13))
    assert "4.26" not in verdict


def test_no_answers_page():
    assert "don't have official results" in T.check(("v", 1.0, "m/s"), [])


# ── Sessions (model replies faked) ──────────────────────────────────────────

@pytest.fixture
def session(kb, monkeypatch):
    calls = []

    async def fake_model(s, mode, message=""):
        calls.append(mode)
        return {"start": "Exercise 1-13 is about an industrial robot. Which law would you start with?",
                "hint": "Start with the time tA it takes the first arm to turn half a revolution."}.get(
            mode, "Good idea, what follows from it?")

    monkeypatch.setattr(T, "_ask_model", fake_model)
    T.end()
    yield calls
    T.end()


def _turn(message):
    return asyncio.run(T.handle(message))


def test_walkthrough_session(session):
    reply, sources = _turn("Walk me through exercise 1-13")
    assert "industrial robot" in reply and sources and T.active().ref == "1-13"
    assert _turn("I got v = 2.83 m/s")[0] == "Correct! v = 2.83 m/s matches the answer sheet. What's the next step?"
    assert _turn("I'm stuck")[0].startswith("Start with the time tA")
    assert _turn("We use the relative motion with a rotating frame")[0] == "Good idea, what follows from it?"
    solution = _turn("show me the solution")[0]
    assert "tA = 1.57 s" in solution and "a = 4.26 m/s2" in solution
    assert _turn("what time is it?") is None and T.active()          # other requests pass through
    assert _turn("stop")[0].startswith("Okay, we'll stop exercise 1-13") and T.active() is None
    assert session == ["start", "hint", "turn"]


def test_check_outside_a_session(session):
    reply, sources = _turn("For exercise 1-13 I got a = 4.26 m/s2")
    assert reply.startswith("Correct! a = 4.26 m/s2") and sources and T.active() is None


def test_other_exercise_ends_the_session(session):
    _turn("Walk me through exercise 1-13")
    assert _turn("explain exercise 1-14") is None and T.active() is None


def test_unknown_exercise(session):
    assert _turn("walk me through exercise 9-99")[0] == "I couldn't find that exercise in your material."


def test_tutor_runs_inside_aryas_answer_flow(session, monkeypatch):
    import arya.core.llm as llm

    emitted = []
    llm.set_artifact_sink(lambda kind, payload: emitted.append(kind))
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])

    async def turn(m):
        return [s async for s in llm.stream_with_tools(m)]

    said = asyncio.run(turn("Walk me through exercise 1-13"))
    assert "industrial robot" in "".join(said) and emitted == ["SOURCES"]
    assert "".join(asyncio.run(turn("I got tA = 1.57 s"))).startswith("Correct! tA = 1.57 s")


@pytest.mark.parametrize("question, answer", [
    ("What is the angular acceleration alpha 2 in exercise 1-13?", "In exercise 1-13, the answer sheet gives a2 = 2.55/s2."),
    ("What is v in exercise 1-13?", "In exercise 1-13, the answer sheet gives v = 2.83 m/s."),
])
def test_value_questions_read_the_answers_page(session, question, answer):
    assert _turn(question)[0] == answer


@pytest.mark.parametrize("question", ["Explain how to get alpha 2 in exercise 1-13", "What is exercise 1-13 about?"])
def test_explanations_are_not_value_lookups(session, question):
    assert _turn(question) is None


def test_lookup_by_symbol_and_word():
    results = T.parse_results(REAL_1_13)
    assert T.lookup("what is alpha 2", results).name == "α2"
    assert T.lookup("what is the velocity v(tA)", results).name == "v"
    assert T.lookup("what is beta a", results).name == "βa"
