"""
How much side talk could reach learning? Measured on the labelled follow-ups.

    python -m arya.research.learning_trust_eval

Before: every line ARYA answered was learnable. After: only lines clearly said to ARYA (the name, a rule, or a
classifier probability >= TRUST_P; the classifier's probabilities here are leave-one-day-out, never from a model
that saw the line). Reported:
  leakage    of the learnable lines, how many were really side talk  (lower is better)
  retention  of your real requests, how many stay learnable           (higher is better)
"""
from __future__ import annotations

import numpy as np


def main() -> None:
    from arya.core import addressee
    from arya.research.addressee_eval import stage_one
    from arya.research.addressee_model import C_DEFAULT, _prepare, fit

    data, rule, X, y, groups = _prepare()
    P = np.zeros(len(data))
    for day in np.unique(groups):
        tr, te = groups != day, groups == day
        scaler, clf = fit(X[tr], y[tr], C_DEFAULT)
        P[te] = clf.predict_proba(scaler.transform(X[te]))[:, 1]
    gold = y.astype(bool)

    def report(name: str, learnable: np.ndarray) -> None:
        n = int(learnable.sum())
        leak = float((learnable & ~gold).sum()) / max(1, n)
        keep = float((learnable & gold).sum()) / max(1, int(gold.sum()))
        print(f"{name:44} learnable {n:3}  leakage {leak:5.1%}  retention {keep:5.1%}")

    answered = np.array([e.decision in ("answered", "rescued") for e in data])
    report("before: everything ARYA answered", answered)
    named = np.array([stage_one(e.text)[1] is True for e in data])
    rule_yes = np.array([r is True for r in rule])
    for t in (0.7, 0.85, 0.9):
        report(f"after: name / rule / classifier p >= {t}", answered & (named | rule_yes | (P >= t)))
    report(f"after, live threshold (TRUST_P = {addressee.TRUST_P})",
           answered & (named | rule_yes | (P >= addressee.TRUST_P)))


if __name__ == "__main__":
    main()
