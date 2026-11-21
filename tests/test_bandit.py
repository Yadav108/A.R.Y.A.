"""Reinforcement learning from reactions (arya/core/bandit.py)."""
import asyncio
import random

import pytest

from arya.core import bandit as B


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "DB_OVERRIDE", tmp_path / "bandits.db")
    monkeypatch.setattr(B, "ENABLED", True)
    B._pending.clear()
    B._current.clear()
    yield
    B._pending.clear()
    B._current.clear()


# ── It learns: a simulated user with hidden preferences ──────────────────────

def test_learns_hidden_preferences(monkeypatch):
    """Chat: this user likes brief answers; explanations: full ones; questions at the end only when explaining.
    After 400 turns the policy picks what the user likes in most of the last 100."""
    monkeypatch.setattr(B, "PROPENSITY_DRAWS", 20)    # not under test here; keeps the run fast
    rng = random.Random(7)
    likes = {("length", "default:chat"): {"brief": 0.9, "normal": 0.5, "full": 0.2},
             ("length", "study:explain"): {"brief": 0.2, "normal": 0.5, "full": 0.9},
             ("follow_up", "default:chat"): {"ask": 0.2, "no_ask": 0.8},
             ("follow_up", "study:explain"): {"ask": 0.85, "no_ask": 0.4}}
    best = {k: max(v, key=v.get) for k, v in likes.items()}
    hits = {k: 0 for k in likes}
    for t in range(400):
        for (policy, ctx), p in likes.items():
            arm, prop, _ = B.choose(policy, ctx, rng)
            assert 0 < prop <= 1
            B.update(policy, ctx, arm, 1.0 if rng.random() < p[arm] else 0.0)
            if t >= 300:
                hits[(policy, ctx)] += arm == best[(policy, ctx)]
    for k, n in hits.items():
        assert n >= 75, (k, n)                      # ≥ 75 of the last 100 (10 % are random on purpose)


def test_starts_from_todays_behaviour():
    rng = random.Random(1)
    picks = [B.choose("length", "default:question", rng)[0] for _ in range(300)]
    assert picks.count("normal") > picks.count("brief") and picks.count("normal") > picks.count("full")


def test_propensity_matches_frequency():
    rng = random.Random(3)
    B.update("follow_up", "default:chat", "no_ask", 1.0)
    picks, props = [], []
    for _ in range(600):
        arm, prop, _ = B.choose("follow_up", "default:chat", rng)
        picks.append(arm)
        props.append(prop if arm == "no_ask" else 1 - prop)
    freq = picks.count("no_ask") / len(picks)
    assert abs(freq - sum(props) / len(props)) < 0.06


# ── Rewards from your next move ──────────────────────────────────────────────

@pytest.mark.parametrize("policy,arm,message,reason,lo,hi", [
    ("length", "full", "", "stopped", 0.0, 0.0),
    ("length", "normal", "", "interrupted", 0.1, 0.3),
    ("length", "brief", "tell me more", "", 0.0, 0.15),
    ("length", "full", "keep it shorter please", "", 0.0, 0.0),
    ("length", "normal", "thanks!", "", 1.0, 1.0),
    ("length", "normal", "No, I meant the Delhi time", "", 0.2, 0.4),
    ("follow_up", "ask", "my sister Mira, she studies medicine", "", 1.0, 1.0),     # answered the question
    ("follow_up", "ask", "pause the music", "", 0.3, 0.5),                        # ignored it, new command
    ("follow_up", "ask", "", "stopped", 0.0, 0.2),
    ("follow_up", "no_ask", "thanks", "", 1.0, 1.0),
])
def test_rewards(policy, arm, message, reason, lo, hi):
    r, _why = B.reward_for(policy, arm, message, 5.0, reason)
    assert lo <= r <= hi


def test_no_reaction_and_side_talk():
    assert B.reward_for("length", "normal", "anything", B.SETTLE_AFTER_S + 1)[0] == pytest.approx(0.7)
    assert B.reward_for("length", "normal", "That wasn't for you.", 3.0)[0] is None


# ── The turn cycle ───────────────────────────────────────────────────────────

def test_turn_cycle_updates_beliefs():
    B.start_turn("why is there a factor of two in the Coriolis term?", random.Random(0))
    assert set(B._current) == {"length", "follow_up"}
    rule = B.prompt_rule()
    assert rule == "" or rule.startswith("For this reply:")
    B.end_turn("model")
    assert len(B._pending) == 2
    B.start_turn("thanks, that makes sense", random.Random(0))       # settles the last turn's choices
    with B._db() as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE reward IS NOT NULL").fetchone()[0] == 2
        assert c.execute("SELECT SUM(n) FROM beliefs").fetchone()[0] == 2


def test_commands_are_not_shaped_or_learned():
    B.start_turn("pause the music", random.Random(0))
    B.end_turn("media keys")
    assert B._pending == [] and B._current == {}


def test_stop_settles_at_once():
    B.start_turn("tell me about the history of control theory", random.Random(0))
    B.end_turn("model")
    B.note_stopped()
    with B._db() as c:
        reasons = [r[0] for r in c.execute("SELECT reason FROM events")]
    assert reasons == ["stopped", "stopped"] and B._pending == []


def test_spoken_limit_follows_the_choice():
    B._current["length"] = "brief"
    assert B.spoken_limit(5) == 2
    B._current["length"] = "normal"
    assert B.spoken_limit(5) == 5


def test_disabled_changes_nothing(monkeypatch):
    monkeypatch.setattr(B, "ENABLED", False)
    B.start_turn("why is the sky blue?")
    assert B._current == {} and B.prompt_rule() == ""


def test_pipeline_runs_the_cycle(monkeypatch):
    import arya.core.llm as llm

    async def pipeline(message):
        llm._route_taken["name"] = "model"
        yield "Because of Rayleigh scattering."
    monkeypatch.setattr(llm, "_turn_pipeline", pipeline)

    async def turn(text):
        return "".join([s async for s in llm.stream_with_tools(text)])
    asyncio.run(turn("why is the sky blue?"))
    assert len(B._pending) == 2
    asyncio.run(turn("thanks"))
    with B._db() as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
