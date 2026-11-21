"""
Reinforcement learning from how you react: contextual Thompson-sampling bandits for ARYA's own choices.

ARYA makes small decisions on every free answer the local model writes, and learns from your next move which
choice works for you, in which situation:

  length     brief (1-2 sentences, 2 spoken) / normal (as before) / full (up to 6, 8 spoken)
  follow_up  ask (end with one specific follow-up question) / no_ask (don't end with a question)

Context = mode (everyday / study / work) + what kind of message it was (question / explanation request / chat).
For each context and option, a Beta(alpha, beta) belief of "this works"; each turn ARYA samples from the beliefs
and takes the best sample (Thompson sampling), with a 10 % floor of random choices so it keeps checking the others.

Reward — read from what you do next (reward_for):
  "stop" / interrupting it              -> too long, or unwanted
  "tell me more", "explain more", "why?" -> too short (but the question engaged you)
  "no, I meant …", "that's wrong"         -> poor answer
  thanks / "ok" / a new request          -> fine
  answering ARYA's question               -> the question was worth asking
  nothing within 10 minutes               -> mildly fine

Every decision is logged with the probability it had (propensity), so other policies can be evaluated offline
(python -m arya.research.bandit_report). Everything stays in the local data folder (bandits.db). Commands, tools and
handlers are never affected — only how a free answer is shaped. ARYA_BANDITS=0 turns it off.
"""
from __future__ import annotations

import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

ENABLED = os.getenv("ARYA_BANDITS", "1") != "0"
EXPLORE = 0.10                 # share of turns with a uniformly random option
SETTLE_AFTER_S = 10 * 60       # no reaction for this long: settled as "mildly fine"
PROPENSITY_DRAWS = 400


@dataclass(frozen=True)
class Policy:
    name: str
    arms: tuple[str, ...]
    prior: dict                   # arm -> (alpha, beta): where beliefs start


POLICIES = {
    # "normal" is how ARYA answered before; it starts a little ahead so learning begins from today's behaviour.
    "length": Policy("length", ("brief", "normal", "full"),
                     {"brief": (2.0, 2.0), "normal": (3.0, 2.0), "full": (2.0, 2.0)}),
    "follow_up": Policy("follow_up", ("ask", "no_ask"), {"ask": (2.0, 2.0), "no_ask": (3.0, 2.0)}),
}

PROMPT = {
    ("length", "brief"): "Answer this one in one or two short sentences.",
    ("length", "normal"): "",
    # Measured (arya/research/bandit_actuators.py): "up to six sentences" gave 2.6 on average — barely more than
    # normal (2.1). It has to override the one-to-three rule explicitly.
    ("length", "full"): "This one deserves a fuller answer than usual: four to six sentences, with the key reason "
                        "or an example (this overrides the one-to-three sentence rule).",
    ("follow_up", "ask"): "End with one short, specific follow-up question about what they said (never a generic "
                          "offer of help).",
    ("follow_up", "no_ask"): "Do not end with a question.",
}
SPOKEN = {"brief": 2, "normal": None, "full": 8}            # sentences said aloud (None: the usual limit)


def _db_path() -> Path:
    base = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA")
    return base / "bandits.db"


DB_OVERRIDE: Path | None = None


def _db() -> sqlite3.Connection:
    p = DB_OVERRIDE or _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS beliefs (policy TEXT, context TEXT, arm TEXT, alpha REAL, beta REAL, "
              "n INTEGER DEFAULT 0, PRIMARY KEY (policy, context, arm))")
    c.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at REAL, policy TEXT, context TEXT, "
              "arm TEXT, propensity REAL, explored INTEGER, reward REAL, reason TEXT)")
    return c


# ── Context ──────────────────────────────────────────────────────────────────

_EXPLAIN = re.compile(r"\b(?:explain|why|how does|how do|how is|what happens|walk me through|teach|derive|"
                      r"difference between|compare|describe)\b", re.IGNORECASE)
_QUESTION = re.compile(r"\?\s*$|^\W*(?:what|who|when|where|which|how much|how many|is|are|can|could|do|does|did|"
                       r"should|will|would)\b", re.IGNORECASE)


def context_of(message: str) -> str:
    try:
        from arya.core import modes
        mode = modes.current_name()
    except Exception:
        mode = "default"
    kind = "explain" if _EXPLAIN.search(message) else "question" if _QUESTION.search(message) else "chat"
    return f"{mode}:{kind}"


# ── Choosing ─────────────────────────────────────────────────────────────────

def beliefs(policy: str, context: str) -> dict[str, tuple[float, float, int]]:
    pol = POLICIES[policy]
    out = {arm: (*pol.prior[arm], 0) for arm in pol.arms}
    with _db() as c:
        for r in c.execute("SELECT arm, alpha, beta, n FROM beliefs WHERE policy = ? AND context = ?",
                           (policy, context)):
            if r["arm"] in out:
                out[r["arm"]] = (r["alpha"], r["beta"], r["n"])
    return out


def _thompson(b: dict[str, tuple[float, float, int]], rng: random.Random) -> str:
    return max(b, key=lambda arm: rng.betavariate(b[arm][0], b[arm][1]))


def choose(policy: str, context: str, rng: random.Random | None = None) -> tuple[str, float, bool]:
    """(arm, propensity, explored)."""
    rng = rng or random.Random()
    b = beliefs(policy, context)
    arms = list(b)
    explored = rng.random() < EXPLORE
    arm = rng.choice(arms) if explored else _thompson(b, rng)
    # P(arm) under this policy: the exploration share plus Thompson's own probability (estimated by sampling)
    wins = sum(_thompson(b, rng) == arm for _ in range(PROPENSITY_DRAWS)) / PROPENSITY_DRAWS
    return arm, EXPLORE / len(arms) + (1 - EXPLORE) * wins, explored


# ── This turn's choices ──────────────────────────────────────────────────────

@dataclass
class Decision:
    policy: str
    context: str
    arm: str
    propensity: float
    explored: bool
    at: float


_pending: list[Decision] = []          # waiting for your reaction
_current: dict[str, str] = {}          # this turn's arms (read by the prompt and the speaker)
_flags = {"interrupted": False, "stopped": False}


def start_turn(message: str, rng: random.Random | None = None) -> None:
    """Settle the last turn's choices from this message, then choose for this one."""
    _current.clear()
    if not ENABLED:
        return
    settle(message)
    ctx = context_of(message)
    for name in POLICIES:
        arm, prop, explored = choose(name, ctx, rng)
        _current[name] = arm
        _pending.append(Decision(name, ctx, arm, prop, explored, time.monotonic()))


def end_turn(route: str | None) -> None:
    """Only free answers by the model are shaped and learned from; for anything else the choices are dropped."""
    if route not in ("model", "model + tools"):
        del _pending[:]
    _current.clear()
    _flags.update(interrupted=False, stopped=False)


def prompt_rule() -> str:
    rules = [PROMPT.get((p, a), "") for p, a in _current.items()]
    rules = [r for r in rules if r]
    return ("For this reply: " + " ".join(rules) + "\n") if rules else ""


def spoken_limit(default: int) -> int:
    n = SPOKEN.get(_current.get("length", "normal"))
    return n if n is not None else default


def note_interrupted() -> None:
    """You talked over ARYA's answer (a barge-in)."""
    _flags["interrupted"] = True
    if _pending:
        settle("", reason_override="interrupted")


def note_stopped() -> None:
    """You said "stop" during the answer."""
    _flags["stopped"] = True
    if _pending:
        settle("", reason_override="stopped")


# ── Reward ───────────────────────────────────────────────────────────────────

_MORE = re.compile(r"^\W*(?:(?:and|so|ok|okay)\W+)?(?:tell me more|more detail|in more detail|explain (?:it )?(?:more|"
                   r"again|further|better)|elaborate|go on|continue|keep going|what else|why\??|how so\??|"
                   r"(?:can you )?explain that|and then\??|details?\??)\b", re.IGNORECASE)
_SHORTER = re.compile(r"\b(?:shorter|too long|keep it short|briefly|in short|just the answer|stop talking)\b",
                      re.IGNORECASE)
_WRONG = re.compile(r"^\W*(?:no|nope|wrong|that'?s (?:wrong|not (?:it|right|what i))|not what i|i meant|you didn'?t|"
                    r"that doesn'?t|incorrect)\b", re.IGNORECASE)
_THANKS = re.compile(r"^\W*(?:(?:ok|okay|alright|great|nice|perfect|cool|good|awesome)\W+)*(?:thanks?|thank you|"
                     r"cheers|got it|makes sense|perfect|great|nice|cool|awesome|ok|okay)\b", re.IGNORECASE)


def reward_for(policy: str, arm: str, message: str, gap_s: float, reason: str = "") -> tuple[float | None, str]:
    """(reward in 0..1, why) for one earlier choice, from your next move. None: no lesson (side talk)."""
    from arya.core import addressee
    if reason == "stopped":
        return (0.0, "stopped") if policy == "length" else ((0.1, "stopped") if arm == "ask" else (0.5, "stopped"))
    if reason == "interrupted":
        return (0.2, "interrupted") if policy == "length" else ((0.3, "interrupted") if arm == "ask"
                                                                else (0.5, "interrupted"))
    if gap_s > SETTLE_AFTER_S or not message:
        return (0.7, "no reaction") if policy == "length" else ((0.4, "no reaction") if arm == "ask"
                                                               else (0.7, "no reaction"))
    if addressee.not_for_you(message):
        return None, "side talk"
    if _SHORTER.search(message):
        return (0.0 if arm != "brief" else 0.3, "asked shorter") if policy == "length" else (0.5, "asked shorter")
    if _MORE.match(message):
        if policy == "length":
            return (0.1 if arm == "brief" else 0.3 if arm == "normal" else 0.5), "asked for more"
        return 0.8, "asked for more"
    if _WRONG.match(message):
        return 0.3, "corrected"
    if _THANKS.match(message):
        if policy == "length":
            return 1.0, "thanks"
        return (0.6, "thanks, question left") if arm == "ask" else (1.0, "thanks")
    # Anything else: a new request, or an answer to ARYA's question
    command = addressee.quick_verdict(message) is True
    if policy == "follow_up":
        if arm == "ask":
            return (0.4, "question ignored") if command else (1.0, "answered the question")
        return 0.8, "carried on"
    return 0.8, "carried on"


def settle(message: str, reason_override: str = "") -> None:
    """Turn your reaction into rewards for the pending choices, and update the beliefs."""
    if not _pending:
        return
    now = time.monotonic()
    done = list(_pending)
    del _pending[:]
    with _db() as c:
        for d in done:
            r, why = reward_for(d.policy, d.arm, message, now - d.at, reason_override)
            c.execute("INSERT INTO events (at, policy, context, arm, propensity, explored, reward, reason) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (time.time(), d.policy, d.context, d.arm, d.propensity, int(d.explored), r, why))
            if r is not None:
                update(d.policy, d.context, d.arm, r, c)


def update(policy: str, context: str, arm: str, reward: float, c: sqlite3.Connection | None = None) -> None:
    """Bayesian update of one belief with a reward in 0..1 (a fractional Bernoulli observation)."""
    a0, b0 = POLICIES[policy].prior[arm]
    sql = ("INSERT INTO beliefs (policy, context, arm, alpha, beta, n) VALUES (?, ?, ?, ?, ?, 1) "
           "ON CONFLICT(policy, context, arm) DO UPDATE SET alpha = alpha + ?, beta = beta + ?, n = n + 1")
    args = (policy, context, arm, a0 + reward, b0 + 1 - reward, reward, 1 - reward)
    if c is not None:
        c.execute(sql, args)
    else:
        with _db() as conn:
            conn.execute(sql, args)


def reset() -> None:
    """Forget what was learned ("reset your style")."""
    del _pending[:]
    _current.clear()
    with _db() as c:
        c.execute("DELETE FROM beliefs")
