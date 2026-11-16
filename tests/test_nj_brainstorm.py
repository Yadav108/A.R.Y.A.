"""'Hey NJ' as a second wake word, and brainstorm mode. The detector heads are synthetic; no audio."""
import asyncio

import numpy as np
import pytest

import arya.core.llm as llm
from arya.core import addressee, modes, policy
from arya.tools import tasks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    modes.use_memory("default")
    monkeypatch.setattr(tasks, "DB_OVERRIDE", tmp_path / "tasks.db")
    policy.cancel_pending()


# ── Brainstorm mode ──────────────────────────────────────────────────────────

def test_brainstorm_persona():
    modes.set_mode("brainstorm")
    p = llm._system_prompt()
    assert "Mode: brainstorm" in p and "SCAMPER" in p and "first principles" in p
    assert p.index("Mode: brainstorm") < p.index("Current local date and time")


@pytest.mark.parametrize("text", ["brainstorm mode", "switch to brainstorming mode", "brainstorm mode please"])
def test_switch_to_brainstorm(text):
    assert modes.handle(text).startswith("Brainstorm mode")
    assert modes.current_name() == "brainstorm"


def test_leave_brainstorm():
    modes.set_mode("brainstorm")
    modes.handle("leave brainstorm mode")
    assert modes.current_name() == "default"


@pytest.mark.parametrize("text", ["let's brainstorm names for my robot", "help me brainstorm a gift for my sister",
                                  "Hey ARYA, can we brainstorm features for the app?", "brainstorm with me"])
def test_lets_brainstorm_switches_then_answers(text):
    assert not modes.handle(text) if modes.matches(text) else True       # not an instant reply …
    assert modes.auto_switch(text) is True                               # … it switches, the model answers
    assert modes.current_name() == "brainstorm"


def test_brainstorm_is_suggested_by_the_prototypes(monkeypatch):
    monkeypatch.setattr(modes, "scores", lambda m: {"default": 0.3, "study": 0.2, "work": 0.3, "brainstorm": 0.8})
    modes.suggestion("give me ideas for a startup name", now=1)
    assert modes.suggestion("what else could we call it", now=2) == "brainstorm"
    assert "brainstorm mode" in modes.suggestion_question("brainstorm")


def test_capabilities_short():
    caps = modes.MODES["brainstorm"].capabilities
    assert caps.startswith("I'm your brainstorming partner") and len(llm.split_sentences(caps)) <= 5


# ── Saving ideas ─────────────────────────────────────────────────────────────

IDEAS = ("First, a line-following robot that also maps the room. Second, a plant-watering bot with a soil "
         "sensor. Third, a robot arm that sorts Lego by colour. Which of these excites you most?")


def _last_answer(monkeypatch, text):
    import arya.memory.database as db
    monkeypatch.setattr(db, "get_history", lambda limit=6: [{"role": "user", "content": "ideas?"},
                                                             {"role": "assistant", "content": text}])


def test_save_the_second_idea(monkeypatch):
    _last_answer(monkeypatch, IDEAS)
    assert tasks.handle("save the second idea") == "Saved to your ideas: a plant-watering bot with a soil sensor."
    assert tasks.handle("save the last idea").startswith("Saved to your ideas: a robot arm that sorts Lego")
    assert "plant-watering" in tasks.handle("what are my ideas?")


def test_save_that_idea_asks_which_when_there_are_several(monkeypatch):
    _last_answer(monkeypatch, IDEAS)
    assert tasks.handle("save that idea").startswith("Which one should I save")
    _last_answer(monkeypatch, "A podcast where robots review other robots. Want more like that?")
    assert tasks.handle("save that idea") == "Saved to your ideas: A podcast where robots review other robots."


def test_save_an_idea_in_my_own_words():
    assert tasks.handle("add a robot that folds laundry to my ideas") == \
        "Saved to your ideas: a robot that folds laundry."
    assert tasks.handle("save that idea: solar charging dock") == "Saved to your ideas: solar charging dock."


# ── "NJ" as a name ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["NJ, what's next?", "Hey NJ.", "N.J. play some music", "okay en jay what time is it"])
def test_nj_counts_as_the_name(text):
    assert addressee.quick_verdict(text) is True


def test_nj_is_stripped_like_arya():
    from arya.core.hotword import _strip_wake_words, is_wake_word
    assert _strip_wake_words("Hey NJ, what's the weather?") == "what's the weather?"
    assert is_wake_word("Hey NJ.") and is_wake_word("Hey, N.J.") and is_wake_word("hey enjay")
    assert not is_wake_word("I enjoy jazz") and is_wake_word("Hey ARYA")


# ── Two wake words on one detector ───────────────────────────────────────────

def _head_file(path, z_bias, threshold, frames=2):
    """A tiny head whose output logit is exactly z_bias, whatever the features."""
    np.savez(path, W1=np.zeros((4, 2), np.float32), b1=np.zeros(2, np.float32), W2=np.zeros((2, 1), np.float32),
             b2=np.array([z_bias], np.float32), threshold=np.float32(threshold), trigger_frames=np.int32(frames),
             n_frames=np.int32(1))
    return path


class _Features:
    def __call__(self, pcm):
        pass

    def get_features(self, n):
        return np.zeros((n, 4), np.float32)


def _detector(heads):
    from arya.core import hotword
    d = hotword.HotwordDetector.__new__(hotword.HotwordDetector)
    d.features, d.heads = _Features(), heads
    d.threshold = heads[0].threshold if len(heads) == 1 else 0.5
    d.trigger_frames = heads[0].trigger_frames if len(heads) == 1 else min(h.trigger_frames for h in heads)
    d.last_word = heads[0].word
    return d


def test_one_head_scores_exactly_as_before(tmp_path, monkeypatch):
    from arya.core import hotword
    monkeypatch.delenv("WAKE_THRESHOLD", raising=False)
    head = hotword._Head(_head_file(tmp_path / "hey_arya.npz", z_bias=2.0, threshold=0.7), main=True)
    d = _detector([head])
    assert d.score(np.zeros(1280, np.float32)) == pytest.approx(1 / (1 + np.exp(-2.0)))
    assert d.threshold == pytest.approx(0.7)


def test_each_word_is_judged_against_its_own_threshold(tmp_path, monkeypatch):
    from arya.core import hotword
    monkeypatch.delenv("WAKE_THRESHOLD", raising=False)
    # ARYA scores p=0.6 (below its 0.7), NJ scores p=0.6 (above its 0.5): NJ should win and fire
    z = float(np.log(0.6 / 0.4))
    arya = hotword._Head(_head_file(tmp_path / "hey_arya.npz", z, 0.7), main=True)
    nj = hotword._Head(_head_file(tmp_path / "hey_nj.npz", z, 0.5, frames=3))
    d = _detector([arya, nj])
    s = d.score(np.zeros(1280, np.float32))
    assert d.last_word == "nj" and s > d.threshold == 0.5
    assert arya.relative(d.features) < 0.5 < nj.relative(d.features)
    assert d.trigger_frames == 2


def test_whisper_priming_names_both_words_only_when_nj_is_trained(monkeypatch):
    from arya.core import hotword, stt
    monkeypatch.setattr(hotword, "extra_models", lambda: [])
    assert stt.wake_prompt() == "Hey Arya."                    # exactly as before
    monkeypatch.setattr(hotword, "extra_models", lambda: ["hey_nj.npz"])
    assert stt.wake_prompt() == "Arya, NJ"


def test_the_shipped_nj_model_uses_the_measured_setting():
    from arya.core import hotword
    head = hotword._Head(hotword.MODEL_DIR / "hey_nj.npz")
    assert (round(head.threshold, 2), head.trigger_frames) == (0.85, 2) and head.word == "nj"


def test_backups_are_not_extra_words(tmp_path, monkeypatch):
    from arya.core import hotword
    for name in ("hey_arya.npz", "hey_nj.npz", "hey_arya.prev.npz", "hey_nj.prev.npz"):
        _head_file(tmp_path / name, 0.0, 0.5)
    monkeypatch.setattr(hotword, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(hotword, "MODEL_FILE", tmp_path / "hey_arya.npz")
    assert [p.name for p in hotword.extra_models()] == ["hey_nj.npz"]
    monkeypatch.setenv("WAKE_EXTRA_WORDS", "0")
    assert hotword.extra_models() == []
