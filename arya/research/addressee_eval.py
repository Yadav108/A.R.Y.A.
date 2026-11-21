"""
How well does ARYA tell speech meant for it from everything else? Measured on the labelled real follow-ups.

    python -m arya.research.addressee_eval              # current pipeline (rules + local model judge)
    python -m arya.research.addressee_eval --no-model   # rules only (unclear -> what the classifier says)
    python -m arya.research.addressee_eval --errors     # list the mistakes

Positive class = "for ARYA". The two mistakes cost differently:
  false accept (side talk answered)  — annoying, and it pollutes history and learning
  false reject (request ignored)     — you repeat yourself or press "It was for you"
"""
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

from arya.research.addressee_data import Example, load


@dataclass
class Scores:
    n: int
    accuracy: float
    precision: float
    recall: float
    false_accept: float      # of the side talk, how much was answered
    false_reject: float      # of the requests, how many were ignored
    model_calls: int

    def line(self, name: str) -> str:
        return (f"{name:34} n={self.n:3}  acc {self.accuracy:5.1%}  prec {self.precision:5.1%}  rec {self.recall:5.1%}  "
                f"FA {self.false_accept:5.1%}  FR {self.false_reject:5.1%}  model calls {self.model_calls}")


def score(gold: list[bool], pred: list[bool], model_calls: int = 0) -> Scores:
    tp = sum(g and p for g, p in zip(gold, pred))
    tn = sum((not g) and (not p) for g, p in zip(gold, pred))
    fp = sum((not g) and p for g, p in zip(gold, pred))
    fn = sum(g and (not p) for g, p in zip(gold, pred))
    pos, neg = tp + fn, tn + fp
    return Scores(n=len(gold), accuracy=(tp + tn) / max(1, len(gold)), precision=tp / max(1, tp + fp),
                  recall=tp / max(1, pos), false_accept=fp / max(1, neg), false_reject=fn / max(1, pos),
                  model_calls=model_calls)


def context(ex: Example) -> dict:
    """What ARYA knew when it heard the sentence (as far as the log tells)."""
    from arya.core import addressee
    since = ex.reply_age_s
    asked = bool(addressee.asks_a_real_question(ex.prev_reply) and since is not None and since < 15)
    recent = ([{"role": "user", "content": ex.prev_user}] if ex.prev_user else []) + \
             ([{"role": "assistant", "content": ex.prev_reply}] if ex.prev_reply else [])
    return {"last_reply": ex.prev_reply, "seconds_since_reply": since, "recent": recent, "asked_question": asked}


def stage_one(text: str) -> tuple[str, bool | None]:
    """The part of the live pipeline before the "is it for me?" check: the name at the start settles it."""
    from arya.core import addressee
    rest, named = addressee.split_name(text)
    if named:
        return rest, True
    return text, None


async def current_pipeline(ex: Example, use_model: bool = True) -> tuple[bool, bool]:
    """(addressed?, asked the model?) — the live decision, replayed."""
    from arya.core import addressee
    text, settled = stage_one(ex.text)
    if settled is not None:
        return settled, False
    ctx = context(ex)
    verdict = addressee.quick_verdict(text)
    if verdict is not None:
        return verdict, False
    if ctx["asked_question"] and addressee._BARE_ANSWER.match(text):
        return True, False
    if not use_model:
        return False, False
    ok, _ = await addressee.is_addressed(text, last_reply=ctx["last_reply"],
                                        seconds_since_reply=ctx["seconds_since_reply"], recent=ctx["recent"],
                                        asked_question=ctx["asked_question"])
    return ok, True


def labelled() -> list[Example]:
    return [e for e in load() if e.label in ("arya", "other")]


async def run(use_model: bool, show_errors: bool) -> Scores:
    from arya.core import hotword
    hotword.extra_models = lambda: ["hey_nj.npz"]           # "Hey NJ" is set up on this laptop
    data = labelled()
    gold, pred, calls, errors = [], [], 0, []
    started = time.monotonic()
    for ex in data:
        p, asked = await current_pipeline(ex, use_model)
        gold.append(ex.label == "arya")
        pred.append(p)
        calls += asked
        if p != (ex.label == "arya"):
            errors.append((ex, p))
    s = score(gold, pred, calls)
    print(s.line("rules + model" if use_model else "rules only (unclear = ignore)"),
          f"  ({time.monotonic() - started:.0f} s)")
    if show_errors:
        for ex, p in errors:
            kind = "FALSE ACCEPT" if p else "FALSE REJECT"
            print(f"  {kind:12} | {ex.text[:90]!r}  <- {ex.prev_reply[:50]!r}")
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-model", action="store_true")
    ap.add_argument("--errors", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(not a.no_model, a.errors))


if __name__ == "__main__":
    main()
