"""The learned "was that for me?" step: dataset from the log, features, the decision band, feedback labels.
Synthetic log lines only — the real dataset stays in the local data folder."""
import asyncio
import json

import numpy as np
import pytest

from arya.core import addressee
from arya.research import addressee_data as D
from arya.research import addressee_model as M

LOG = """\
[2026-11-21 15:00:00] WakeWord FOLLOW-UP: 'pause the music.'
[2026-11-21 15:00:00] Processing: pause the music.
[2026-11-21 15:00:10] WakeWord FOLLOW-UP: "I'll tell you later."
[2026-11-21 15:00:10] Not for ARYA (model (0.1s)) — staying quiet: "I'll tell you later."
[2026-11-21 15:00:20] WakeWord FOLLOW-UP: 'How are you?'
[2026-11-21 15:00:21] Not for ARYA (model (0.4s)) — staying quiet: 'How are you?'
[2026-11-21 15:00:25] Processing: How are you?
WakeWord FOLLOW-UP: 'Good one.'
Processing: Good one.
"""


def test_parse_log_decisions_and_rescues(tmp_path):
    p = tmp_path / "arya.log"
    p.write_text(LOG, encoding="utf-8")
    ex = D.parse_log(p)
    assert [(e.text, e.decision) for e in ex] == [("pause the music.", "answered"), ("I'll tell you later.", "ignored"),
                                                   ("How are you?", "rescued"), ("Good one.", "answered")]
    assert ex[1].why == "model (0.1s)" and ex[3].ts.startswith("line ")


def test_utc_conversion():
    from datetime import datetime
    assert D._to_utc(datetime(2026, 11, 21, 15, 0)) == datetime(2026, 11, 21, 14, 0)        # Berlin, winter
    assert D._to_utc(datetime(2026, 11, 15, 15, 0)) == datetime(2026, 11, 15, 9, 30)        # India, before 16 Nov


def _fake_embed(text: str) -> np.ndarray:
    rng = np.random.default_rng(abs(hash(text)) % (2 ** 32))
    return rng.normal(size=384).astype(np.float32)


def test_features_shape_and_meaning():
    x = M.featurize("Can you open YouTube?", "Opening it now. Shall I play something?", 4.0, _fake_embed)
    assert x.shape == (384 + len(M.FEATURE_NAMES),)
    f = dict(zip(M.FEATURE_NAMES, x[384:]))
    assert f["question_mark"] == 1 and f["q_word_start"] == 1 and f["you"] == 1 and f["asked_question"] == 1
    y = M.featurize("Cheese. Cheese. Cheese. Cheese. Cheese. Cheese.", "", None, _fake_embed)
    g = dict(zip(M.FEATURE_NAMES, y[384:]))
    assert g["repetitive"] == 1 and g["reply_age_missing"] == 1 and g["reply_sim"] == 0


def test_probability_matches_sklearn():
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    rng = np.random.default_rng(0)
    X = rng.normal(size=(60, 5)).astype(np.float32)
    y = (X[:, 0] + 0.3 * rng.normal(size=60) > 0).astype(int)
    scaler = StandardScaler().fit(X)
    clf = LogisticRegression(C=1.0).fit(scaler.transform(X), y)
    m = json.loads(json.dumps(M.to_json(scaler, clf, 0.7, {})))
    for k in ("coef", "mean", "scale"):
        m[k] = np.asarray(m[k], dtype=np.float32)
    want = clf.predict_proba(scaler.transform(X[:5]))[:, 1]
    got = [M.probability(x, m) for x in X[:5]]
    assert np.allclose(want, got, atol=1e-4)


@pytest.fixture
def band(monkeypatch):
    """A fake fitted model whose probability is set per test; the local model judge is recorded."""
    state = {"p": 0.5, "judged": 0}

    async def prob(text, last_reply, since):
        return state["p"]

    async def chat_local(messages, **k):
        state["judged"] += 1
        return {"content": "ARYA"}
    monkeypatch.setattr(addressee, "classifier_probability", prob)
    monkeypatch.setattr(M, "load_model", lambda path=M.MODEL_PATH: {"lo": 0.4, "hi": 0.7})
    import arya.core.llm_local as ll
    monkeypatch.setattr(ll, "chat_local", chat_local)
    return state


@pytest.mark.parametrize("p,expected,judged", [(0.9, True, 0), (0.1, False, 0), (0.55, True, 1)])
def test_decision_band(band, p, expected, judged):
    band["p"] = p
    ok, why = asyncio.run(addressee.is_addressed("so that went well", last_reply="Done."))
    assert ok is expected and band["judged"] == judged
    assert ("classifier" in why) is (judged == 0)


def test_rules_still_come_first(band):
    band["p"] = 0.0
    assert asyncio.run(addressee.is_addressed("pause the music"))[0] is True
    assert band["judged"] == 0


def test_no_model_falls_back_to_the_judge(monkeypatch):
    calls = []

    async def chat_local(messages, **k):
        calls.append(1)
        return {"content": "OTHER"}
    import arya.core.llm_local as ll
    monkeypatch.setattr(ll, "chat_local", chat_local)
    monkeypatch.setattr(M, "load_model", lambda path=M.MODEL_PATH: None)
    assert asyncio.run(addressee.is_addressed("so that went well"))[0] is False and calls == [1]


@pytest.mark.parametrize("text", ["That wasn't for you.", "I wasn't talking to you", "No, not you, NJ.",
                                  "That's not for you, stop listening.", "I was talking to someone else"])
def test_not_for_you(text):
    assert addressee.not_for_you(text)
    assert addressee.quick_verdict(text) is True


@pytest.mark.parametrize("text", ["Is this for you?", "not now", "I'm talking to you about the timer"])
def test_not_not_for_you(text):
    assert not addressee.not_for_you(text)


def test_feedback_becomes_a_label(tmp_path, monkeypatch):
    monkeypatch.setenv("ARYA_DATA_DIR", str(tmp_path / "ARYA"))
    addressee.record_feedback("It's already too crowded out.", "other")
    monkeypatch.setattr(D, "OUT_DIR", tmp_path / "ARYA" / "research")
    log = tmp_path / "arya.log"
    log.write_text("[2026-11-19 16:45:12] WakeWord FOLLOW-UP: \"It's already too crowded out.\"\n"
                   "[2026-11-19 16:45:12] Processing: It's already too crowded out.\n", encoding="utf-8")
    monkeypatch.setattr(D, "DB", tmp_path / "none.db")
    ex = D.build(log)
    assert ex[0].label == "other"


def test_not_for_you_reply_records_the_last_sentence(tmp_path, monkeypatch):
    import arya.core.llm as llm
    monkeypatch.setenv("ARYA_DATA_DIR", str(tmp_path / "ARYA"))
    monkeypatch.setattr(llm, "get_history", lambda limit=10: [
        {"role": "user", "content": "Now we'll talk."}, {"role": "assistant", "content": "Sure."}])
    monkeypatch.setattr(llm, "save_message", lambda *a, **k: None)

    async def run():
        return "".join([s async for s in llm.stream_with_tools("That wasn't for you.")])
    assert "stay out" in asyncio.run(run())
    saved = (tmp_path / "ARYA" / "research" / "feedback.jsonl").read_text(encoding="utf-8")
    assert "Now we'll talk." in saved and '"other"' in saved
