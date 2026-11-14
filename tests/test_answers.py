"""How answers are grounded, saved and spoken (no language model needed)."""
import asyncio
import json
import types

import pytest

import arya.core.llm as llm


@pytest.fixture(autouse=True)
def _no_ui(monkeypatch):
    monkeypatch.setattr(llm, "_artifact_sink", None, raising=False)
    llm.set_artifact_sink(lambda kind, payload: None)


def test_exercise_grounding(kb):
    q = "Can you look dynamics exercise 113 and explain me?"
    passages = asyncio.run(llm.knowledge_for(q))
    g = llm.grounding_prompt(passages, q)
    assert "exercise 1-13 from their own material" in g and "step by step" in g
    assert "never claim you can't access" in g


def test_short_exercise_question_is_still_looked_up(kb):
    assert len(asyncio.run(llm.knowledge_for("exercise 113"))) == 2


def test_missing_exercise_is_admitted(kb):
    q = "in case you have a solution in chapter one, question 30."
    g = llm.grounding_prompt(asyncio.run(llm.knowledge_for(q)), q)
    assert "no exercise with that number" in g and "1-30" in g


def test_plain_chat_gets_no_grounding():
    assert llm.grounding_prompt([], "how are you today") == ""


def test_system_prompt_rules():
    p = llm._system_prompt()
    assert "never calculate it in your head" in p and "Never write LaTeX" in p and "about five sentences" in p


def test_saved_answers_have_no_latex(monkeypatch):
    saved = []
    monkeypatch.setattr(llm, "_db_save_message", lambda role, content: saved.append((role, content)) or 0)
    llm.save_message("assistant", r"So \(\omega_n = \sqrt{\frac{k}{m}}\).")
    llm.save_message("user", r"what is \(x\)?")
    assert saved == [("assistant", "So ωₙ = √(k/m)."), ("user", r"what is \(x\)?")]


def test_model_drawn_loop_is_closed_and_not_read_back(monkeypatch):
    """Model calls draw_diagram, then echoes the connections (as it did in the logs)."""
    emitted = []
    llm.set_artifact_sink(lambda kind, payload: emitted.append((kind, payload)))
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)
    conns = ["Setpoint -> PID Controller", "PID Controller -> Heater", "Heater -> Oven", "Oven -> Thermocouple"]

    async def fake_complete(model, messages, tools=None, temperature=0, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            call = types.SimpleNamespace(id="c1", name="draw_diagram",
                                         arguments=json.dumps({"title": "Temperature Control Loop", "connections": conns}))
            return types.SimpleNamespace(content="", tool_calls=[call])
        echo = "Here is the block diagram:\nSetpoint -" + chr(92) + "u003e PID Controller"
        return types.SimpleNamespace(content=echo, tool_calls=None)

    async def no_ctx(*a, **k):
        return ""

    monkeypatch.setattr(llm, "_complete", fake_complete)
    monkeypatch.setattr(llm, "_parse_args", lambda tc: json.loads(tc.arguments))
    monkeypatch.setattr(llm, "_get_semantic_context", no_ctx)
    reply = asyncio.run(llm._tool_pipeline("draw the temperature control loop", history=[], mem_context=""))
    assert reply == "Here's the block diagram of the temperature control loop, on screen."
    mermaid = emitted[-1][1]["mermaid"]
    assert "-->|feedback|" in mermaid and mermaid.count("-->") == 5


def test_instant_diagram_turn(monkeypatch):
    said, emitted = [], []
    llm.set_artifact_sink(lambda kind, payload: emitted.append(kind))
    monkeypatch.setattr(llm, "save_message", lambda role, content: 0)

    async def turn(msg):
        return [s async for s in llm.stream_with_tools(msg)]

    said = asyncio.run(turn("Draw a block diagram of a temperature control loop with a PID controller, heater, "
                            "oven and thermocouple"))
    assert said == ["Here's the block diagram of the temperature control loop, on screen."] and emitted == ["VISUAL"]
