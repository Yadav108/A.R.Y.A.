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
