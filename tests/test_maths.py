"""Numbers come from code, not from the model: the calculator tool and exact course formulas."""
import asyncio

import pytest

from arya.study.formulas import quick_formula_answer
from arya.tools.knowledge import calculate


@pytest.mark.parametrize("expr, result", [
    ("17*23", "Result: 391"),
    ("15 percent of 240", "Result: 36"),
    ("100*exp(-0.3*pi/sqrt(1-0.3^2))", "Result: 37.2326"),
    ("sqrt(1000/2.5)", "Result: 20"),
    ("sin(pi/6)", "Result: 0.5"),
    ("solve x^2 - 5x + 6 = 0", "x = 2; x = 3"),
    ("solve 2*t + 4 = 10 for t", "t = 3"),
    ("integral of x^2 from 0 to 3", "Integral from 0 to 3 = 9"),
    ("90 km/h to m/s", "90 km/h is 25 m/s."),
    ("convert 3 feet to cm", "3 feet is 91.44 cm."),
    ("1500 rpm in rad/s", "1500 rpm is 157.08 rad/s."),
    ("20 C to F", "20 C is 68 F."),
])
def test_calculator(expr, result):
    assert asyncio.run(calculate(expr)) == result


def test_derivative():
    out = asyncio.run(calculate("derivative of x^3*sin(x)"))
    assert out.startswith("d/dx = ") and "cos(x)" in out and "sin(x)" in out


@pytest.mark.parametrize("expr", ['__import__("os")', 'open("x")', "x.__class__", "lambda: 1", "exec(1)",
                                  "globals()", "os.system(1)", "breakpoint()"])
def test_calculator_refuses_code(expr):
    assert asyncio.run(calculate(expr)).startswith("Could not calculate")


def test_incompatible_units():
    assert "can't convert" in asyncio.run(calculate("5 kg to m"))


@pytest.mark.parametrize("question, must", [
    ("What is the percent overshoot for a damping ratio of 0.3?", ["37.2 %", "e^(−ζπ/√(1 − ζ²))"]),
    ("What is the formula for percent overshoot? Give an example with zeta 0.2.", ["52.7 %"]),
    ("overshoot for zeta 1.2", ["no overshoot"]),
    ("settling time for zeta 0.5 and natural frequency 4", ["4/(ζωₙ)", "2 seconds"]),
    ("peak time with damping ratio 0.3 and natural frequency 2", ["1.65 seconds"]),
    ("natural frequency of a mass spring system with k = 200 and m = 2", ["10 rad/s", "1.59 Hz"]),
    ("time constant of an RC circuit with R = 10 kOhm and C = 100 uF", ["1 second"]),
    ("time constant with R = 50 ohm and L = 10 mH", ["200 microseconds"]),
])
def test_exact_course_formulas(question, must):
    answer = quick_formula_answer(question)
    assert answer and all(m in answer for m in must), answer


@pytest.mark.parametrize("question", ["What is the Coriolis acceleration?", "Why does a system overshoot?",
                                      "Plot the step response with damping ratio 0.3 and natural frequency 2"])
def test_no_formula_answer_for_other_questions(question):
    assert quick_formula_answer(question) is None


def test_overshoot_without_numbers_gets_the_formula_card():
    assert "100 · e^(−ζπ/√(1 − ζ²))" in quick_formula_answer("What is the percent overshoot?")


def test_overshoot_question_is_answered_instantly(monkeypatch):
    import arya.core.llm as llm

    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)

    async def turn():
        return [s async for s in llm.stream_with_tools("What is the percent overshoot for a damping ratio of 0.3?")]

    said = asyncio.run(turn())
    assert len(said) == 1 and "37.2 %" in said[0]


@pytest.mark.parametrize("raw, clean", [
    ("### Problem Statement:", "Problem Statement:"),
    ("- **P (Proportional)**: responds to the error.", "P (Proportional): responds to the error."),
    ("1. Damping Ratio (ζ): it damps.", "Damping Ratio (ζ): it damps."),
    ("x = 3*4*5 and a*b", "x = 3*4*5 and a*b"),
    ("-5 V is negative", "-5 V is negative"),
])
def test_markdown_is_removed(raw, clean):
    import arya.core.llm as llm

    assert llm.plain_text(raw) == clean


def test_length_cap_drops_the_unfinished_sentence(monkeypatch):
    import arya.core.llm as llm

    async def tokens(model, messages, max_tokens=None):
        assert max_tokens == llm.ANSWER_MAX_TOKENS
        for t in ["First sentence. ", "Second one. ", "And the third is cut off mid"]:
            yield t

    async def no_ctx(*a, **k):
        return ""

    async def no_kb(*a, **k):
        return []

    async def no_tool(*a, **k):
        return False

    monkeypatch.setattr(llm, "_stream_tokens", tokens)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "_get_semantic_context", no_ctx)
    monkeypatch.setattr(llm, "knowledge_for", no_kb)
    monkeypatch.setattr(llm, "needs_tool", no_tool, raising=False)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])

    async def turn():
        return [s async for s in llm._stream_with_tools("tell me a story about bridges")]

    said = asyncio.run(turn())
    assert "".join(said).strip() == "First sentence. Second one."


# ── Formula cards, capabilities, length trimming, exercise routing ───────────

@pytest.mark.parametrize("question, must", [
    ("What is the formula for percent overshoot of a second order system?", "100 · e^(−ζπ/√(1 − ζ²))"),
    ("What is the 2 percent settling time of a second-order system?", "4/(ζωₙ)"),
    ("What is the transfer function of a first-order system?", "K/(τs + 1)"),
    ("What is the natural frequency of a mass-spring system?", "√(k/m)"),
    ("What is the time constant of an RC circuit?", "R·C"),
])
def test_formula_cards(question, must):
    answer = quick_formula_answer(question)
    assert answer and must in answer


@pytest.mark.parametrize("question", ["Why does a system overshoot?", "Explain the settling time.",
                                      "What is the Coriolis acceleration?", "What is the difference between rise time and peak time?"])
def test_no_card_for_explanations(question):
    assert quick_formula_answer(question) is None


@pytest.mark.parametrize("question, is_cap", [("What can you do?", True), ("what are your capabilities", True),
                                             ("How can you help me?", True), ("What can you do with a Bode plot?", False)])
def test_capabilities_question(question, is_cap):
    import arya.core.llm as llm

    assert bool(llm._CAPABILITIES.match(question)) is is_cap


def test_capabilities_answer_is_true_and_short():
    import arya.core.llm as llm

    assert "translat" not in llm.CAPABILITIES.lower() and len(llm.split_sentences(llm.CAPABILITIES)) <= 5


@pytest.mark.parametrize("text, trimmed", [
    ("One. Two. And three is cut", "One. Two."),
    ("All complete.", "All complete."),
    ("No punctuation at all", "No punctuation at all"),
])
def test_trim_unfinished(text, trimmed):
    import arya.core.llm as llm

    assert llm.trim_unfinished(text) == trimmed


def test_exercise_question_skips_the_tool_route(monkeypatch):
    import arya.core.llm as llm

    async def yes(*a, **k):
        return True

    async def no_ctx(*a, **k):
        return ""

    async def no_kb(*a, **k):
        return []

    async def tool_route(*a, **k):
        raise AssertionError("exercise question went down the tool route")

    async def tokens(model, messages, max_tokens=None):
        yield "Exercise 1-13 is about an industrial robot."

    monkeypatch.setattr(llm, "needs_tool", yes)
    monkeypatch.setattr(llm, "_get_semantic_context", no_ctx)
    monkeypatch.setattr(llm, "knowledge_for", no_kb)
    monkeypatch.setattr(llm, "_tool_pipeline", tool_route)
    monkeypatch.setattr(llm, "_stream_tokens", tokens)
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [])

    async def turn():
        return [s async for s in llm._stream_with_tools("Can you explain dynamics exercise 113?")]

    assert "robot" in "".join(asyncio.run(turn()))
