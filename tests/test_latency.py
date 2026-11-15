"""M5: latency — a long first sentence is voiced clause-first, so ARYA starts talking sooner."""
import pytest

from arya.server.websocket import split_first_for_voice


@pytest.mark.parametrize("sentence, parts", [
    ("Hello!", ["Hello!"]),
    ("The current time is 17:42 on Sunday, 15th November 2026.",
     ["The current time is 17:42 on Sunday, 15th November 2026."]),                   # short: one piece
    ("A PID controller, short for Proportional Integral Derivative controller, keeps a system at its set point.",
     ["A PID controller, short for Proportional Integral Derivative controller,", "keeps a system at its set point."]),
    ("Coriolis acceleration appears in a rotating frame of reference when a body also moves relative to that frame.",
     ["Coriolis acceleration appears in a rotating frame of reference when a body also moves relative to that frame."]),
    ("The damping ratio decides the response — below one it oscillates and above one it creeps back slowly.",
     ["The damping ratio decides the response", "— below one it oscillates and above one it creeps back slowly."]),
    ("It costs 1,000 euros and takes two weeks to arrive at the lab in Jülich after you order it.",
     ["It costs 1,000 euros and takes two weeks to arrive at the lab in Jülich after you order it."]),
])
def test_first_sentence_split(sentence, parts):
    assert split_first_for_voice(sentence) == parts
    assert " ".join(parts) == sentence                                    # nothing lost


@pytest.mark.parametrize("model, thinks", [("qwen3:4b", True), ("qwen3:1.7b", True), ("deepseek-r1:7b", True),
                                          ("qwen2.5:3b", False), ("llama3.2:3b", False), ("phi4-mini", False)])
def test_thinking_is_switched_off_for_reasoning_models(monkeypatch, model, thinks):
    import arya.core.llm_local as local
    monkeypatch.setattr(local, "OLLAMA_MODEL", model)
    payload = local._payload([{"role": "user", "content": "hi"}], True, None, None, None)
    assert payload["model"] == model
    assert (payload.get("think") is False) is thinks and ("think" in payload) is thinks


def test_history_the_model_rereads(monkeypatch):
    import arya.core.llm as llm
    monkeypatch.setattr(llm, "HISTORY_MESSAGES", 6)
    monkeypatch.setattr(llm, "HISTORY_OLD_CHARS", 20)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"message {i} " + "word " * 10}
               for i in range(10)]
    out = llm.chat_history_for_model(history)
    assert [m["content"].split()[1] for m in out] == ["4", "5", "6", "7", "8", "9"]     # the last six
    assert all(m["content"].endswith(" …") and len(m["content"]) <= 22 for m in out[:4])   # older ones shortened
    assert out[-2]["content"] == history[-2]["content"] and out[-1]["content"] == history[-1]["content"]
