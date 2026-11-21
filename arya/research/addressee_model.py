"""
A small learned "was this said to ARYA?" classifier for the cases the rules can't settle.

    python -m arya.research.addressee_model            # cross-validate (leave one day out), compare, save

Features (computed in a few ms on the CPU):
  - the sentence's MiniLM embedding (the embedder ARYA already has loaded), 384 numbers
  - how it relates to ARYA's last reply: embedding similarity (same topic?), seconds since it, whether ARYA asked
    a real question
  - shape: words, question mark, starts with a question word / a verb, "you" / "we" / "I", repetition, how much
    of it is English
Model: L2-regularised logistic regression (scikit-learn to fit; ARYA itself only needs numpy to apply it).

The fitted weights come from what was said in your room, so they are saved in the local data folder
(%LOCALAPPDATA%\\ARYA\\addressee_model.json), not in the repository.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import numpy as np

MODEL_PATH = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA") / "addressee_model.json"

_Q_WORD = re.compile(r"^\W*(?:what|why|how|when|where|who|which|whose|can|could|would|will|is|are|do|does|did|"
                     r"should|shall|may)\b", re.IGNORECASE)
_VERB_START = re.compile(r"^\W*(?:please\s+)?(?:open|close|play|pause|stop|resume|set|start|show|tell|explain|give|"
                         r"find|search|remind|switch|go|turn|increase|decrease|mute|unmute|minimi[sz]e|maximi[sz]e|"
                         r"connect|send|take|read|check|list|record|save|delete|move|teach|help|describe|draw|plot|"
                         r"define|calculate|convert|translate|summari[sz]e|look|make|create|add|call|ring|lock)\b",
                         re.IGNORECASE)
_YOU = re.compile(r"\b(?:you|your|you're|yourself)\b", re.IGNORECASE)
_WE = re.compile(r"\b(?:we|us|our|we're|guys|bro|dude|yaar|bhai)\b", re.IGNORECASE)
_I = re.compile(r"\b(?:i|i'm|i've|i'll|me|my)\b", re.IGNORECASE)
_EN = set("""the a an to of and in is it you that for on with this be are was i what can how my me do not have
your we they he she at by from or but so if all will just about would there their when which who why where""".split())

FEATURE_NAMES = ["n_words_log", "question_mark", "q_word_start", "verb_start", "you", "we", "i", "repetitive",
                 "english_share", "reply_sim", "reply_age_log", "reply_age_missing", "asked_question", "generic_reply",
                 "reply_is_question"]


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return float(a @ b / (na * nb)) if na and nb else 0.0


def shape_features(text: str, prev_reply: str, reply_age_s: float | None, text_vec: np.ndarray,
                   reply_vec: np.ndarray | None) -> list[float]:
    from arya.core import addressee
    words = re.findall(r"[a-zA-Z']+", text.lower())
    n = max(1, len(words))
    asked = bool(addressee.asks_a_real_question(prev_reply) and reply_age_s is not None and reply_age_s < 15)
    return [
        math.log1p(len(words)),
        float("?" in text),
        float(bool(_Q_WORD.match(text))),
        float(bool(_VERB_START.match(text))),
        float(bool(_YOU.search(text))),
        float(bool(_WE.search(text))),
        float(bool(_I.search(text))),
        float(addressee.repetitive(text)),
        sum(w in _EN for w in words) / n,
        _cos(text_vec, reply_vec) if reply_vec is not None else 0.0,
        math.log1p(reply_age_s) if reply_age_s is not None else 0.0,
        float(reply_age_s is None),
        float(asked),
        float(bool(prev_reply) and prev_reply.rstrip().endswith("?") and not addressee.asks_a_real_question(prev_reply)),
        float(prev_reply.rstrip().endswith("?")),
    ]


def featurize(text: str, prev_reply: str, reply_age_s: float | None, embed=None) -> np.ndarray:
    if embed is None:
        from arya.core.embedder import embed_sync as embed
    tv = np.asarray(embed(text), dtype=np.float32)
    rv = np.asarray(embed(prev_reply), dtype=np.float32) if prev_reply else None
    return np.concatenate([tv, np.asarray(shape_features(text, prev_reply, reply_age_s, tv, rv), dtype=np.float32)])


# ── Applying a fitted model (numpy only) ─────────────────────────────────────

_loaded: dict = {}


def load_model(path: Path = MODEL_PATH) -> dict | None:
    if "model" not in _loaded:
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
            m["coef"] = np.asarray(m["coef"], dtype=np.float32)
            m["mean"] = np.asarray(m["mean"], dtype=np.float32)
            m["scale"] = np.asarray(m["scale"], dtype=np.float32)
            _loaded["model"] = m
        except (OSError, ValueError, KeyError):
            _loaded["model"] = None
    return _loaded["model"]


def probability(x: np.ndarray, model: dict) -> float:
    z = float(((x - model["mean"]) / model["scale"]) @ model["coef"] + model["intercept"])
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


# ── Fitting and measuring (offline) ──────────────────────────────────────────

def _day(ts: str) -> str:
    return "old-log" if ts.startswith("line ") else ts[:10]


def build_matrix(examples):
    from arya.core.embedder import embed_sync, load_embedder
    load_embedder()
    cache: dict[str, np.ndarray] = {}

    def embed(t: str) -> np.ndarray:
        if t not in cache:
            cache[t] = embed_sync(t)
        return cache[t]
    X = np.stack([featurize(e.text, e.prev_reply, e.reply_age_s, embed) for e in examples])
    y = np.array([e.label == "arya" for e in examples], dtype=int)
    groups = np.array([_day(e.ts) for e in examples])
    return X, y, groups


def fit(X: np.ndarray, y: np.ndarray, C: float):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler().fit(X)
    clf = LogisticRegression(C=C, max_iter=2000, class_weight="balanced").fit(scaler.transform(X), y)
    return scaler, clf


def to_json(scaler, clf, threshold: float, meta: dict) -> dict:
    scale = np.where(scaler.scale_ == 0, 1.0, scaler.scale_)
    return {"coef": clf.coef_[0].tolist(), "intercept": float(clf.intercept_[0]), "mean": scaler.mean_.tolist(),
            "scale": scale.tolist(), "threshold": threshold, "features": "minilm384+" + ",".join(FEATURE_NAMES),
            **meta}


# The decision band: sure -> the classifier decides; in between -> the local model judges.
LO, HI, C_DEFAULT = 0.4, 0.7, 0.01
MIN_EXAMPLES = 150


def cross_validate(data, X, y, groups, rule, llm_decisions=None, C=C_DEFAULT, lo=LO, hi=HI):
    """Leave one day out. Returns (Scores of rules+classifier+model-in-band, Scores of rules+classifier alone)."""
    from arya.research.addressee_eval import score
    P = np.zeros(len(data))
    for day in np.unique(groups):
        tr, te = groups != day, groups == day
        if len(set(y[tr])) < 2:
            P[te] = 0.5
            continue
        scaler, clf = fit(X[tr], y[tr], C)
        P[te] = clf.predict_proba(scaler.transform(X[te]))[:, 1]
    hybrid, alone, calls = [], [], 0
    for i, (r, p) in enumerate(zip(rule, P)):
        if r is not None:
            hybrid.append(bool(r)); alone.append(bool(r)); continue
        alone.append(bool(p >= hi))
        if p >= hi or p < lo or llm_decisions is None:
            hybrid.append(bool(p >= (hi if llm_decisions is not None else 0.5)))
        else:
            hybrid.append(bool(llm_decisions[i])); calls += 1
    gold = list(y.astype(bool))
    return score(gold, hybrid, calls), score(gold, alone)


def _prepare():
    from arya.core import addressee
    from arya.research.addressee_data import build
    from arya.research.addressee_eval import stage_one
    data = [e for e in build() if e.label in ("arya", "other")]
    rule = []
    for e in data:
        text, settled = stage_one(e.text)
        rule.append(settled if settled is not None else addressee.quick_verdict(text))
    X, y, groups = build_matrix(data)
    return data, rule, X, y, groups


def retrain(quiet: bool = False) -> str:
    """Refit on everything labelled so far (incl. every "It was for you" and "that wasn't for you" since), and
    replace the saved model only if leave-one-day-out isn't worse than the saved one's. Runs once a day."""
    data, rule, X, y, groups = _prepare()
    if len(data) < MIN_EXAMPLES or len(set(y)) < 2:
        return f"not enough labelled follow-ups yet ({len(data)})"
    _, alone = cross_validate(data, X, y, groups, rule)
    cost = 2 * alone.false_accept + alone.false_reject
    old = load_model()
    old_cost = (2 * old["cv_alone"]["false_accept"] + old["cv_alone"]["false_reject"]) if old and "cv_alone" in old \
        else None
    if old_cost is not None and cost > old_cost + 0.02:
        return f"kept the old model (new {cost:.3f} vs old {old_cost:.3f} on {len(data)} examples)"
    scaler, clf = fit(X, y, C_DEFAULT)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_PATH.write_text(json.dumps(to_json(scaler, clf, HI, {"lo": LO, "hi": HI, "C": C_DEFAULT, "n": len(data),
                                                               "cv_alone": alone.__dict__})), encoding="utf-8")
    _loaded.clear()
    return f"refitted on {len(data)} labelled follow-ups (leave-one-day-out acc {alone.accuracy:.1%})"


def main() -> None:
    import asyncio

    from arya.core import addressee
    from arya.research.addressee_eval import current_pipeline, score

    data, rule, X, y, groups = _prepare()
    print(f"{len(data)} labelled; {sum(r is None for r in rule)} reach the learned step; per day: "
          + ", ".join(f"{d} {n}" for d, n in zip(*np.unique(groups, return_counts=True))))

    addressee.USE_CLASSIFIER = False                     # the live baseline: rules + local model only

    async def llm_all():
        return [(await current_pipeline(e, True))[0] for e in data]
    llm = asyncio.run(llm_all())
    gold = list(y.astype(bool))
    print(score(gold, llm, sum(r is None for r in rule)).line("rules + local model (before)"))
    for C in (0.01, 0.03, 0.1):
        hybrid, alone = cross_validate(data, X, y, groups, rule, llm, C)
        print(alone.line(f"rules + classifier       C={C}"))
        print(hybrid.line(f"rules + classifier + LLM C={C}"))
    retrain_msg = retrain()
    print("\n" + retrain_msg, "->", MODEL_PATH)


if __name__ == "__main__":
    main()
