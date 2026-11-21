"""
What ARYA's reinforcement learning has learned so far, and what other policies would have earned.

    python -m arya.research.bandit_report

  beliefs       per context and option: mean of Beta(alpha, beta) and how many reactions it is based on
  rewards       logged average reward per option, and why (stopped / asked for more / thanks …)
  off-policy    inverse-propensity estimates (self-normalised IPS) of fixed policies — "always normal",
                "never ask" … — on the logged decisions, next to what the learning policy actually earned.
                Valid because every decision was logged with the probability it had.
"""
from __future__ import annotations

from collections import Counter, defaultdict


def main() -> None:
    from arya.core import bandit

    with bandit._db() as c:
        beliefs = c.execute("SELECT policy, context, arm, alpha, beta, n FROM beliefs ORDER BY policy, context, arm"
                            ).fetchall()
        events = c.execute("SELECT policy, context, arm, propensity, explored, reward, reason FROM events").fetchall()
    print(f"{len(events)} decisions logged, {sum(e['reward'] is not None for e in events)} with a reward\n")

    print("Beliefs (mean of the Beta posterior, reactions seen):")
    for b in beliefs:
        print(f"  {b['policy']:9} {b['context']:18} {b['arm']:7} {b['alpha'] / (b['alpha'] + b['beta']):.2f}  "
              f"n={b['n']}")

    print("\nRewards by option:")
    by = defaultdict(list)
    why = defaultdict(Counter)
    for e in events:
        if e["reward"] is None:
            continue
        by[(e["policy"], e["arm"])].append(e["reward"])
        why[(e["policy"], e["arm"])][e["reason"]] += 1
    for (p, a), rs in sorted(by.items()):
        print(f"  {p:9} {a:7} mean {sum(rs) / len(rs):.2f} over {len(rs):3}   " +
              ", ".join(f"{k} {v}" for k, v in why[(p, a)].most_common(4)))

    print("\nOff-policy estimates (self-normalised IPS):")
    for p, pol in bandit.POLICIES.items():
        logged = [e for e in events if e["policy"] == p and e["reward"] is not None]
        if not logged:
            continue
        actual = sum(e["reward"] for e in logged) / len(logged)
        print(f"  {p}: the learning policy earned {actual:.2f} on {len(logged)} decisions")
        for arm in pol.arms:
            w = [(1.0 / e["propensity"]) if e["arm"] == arm else 0.0 for e in logged]
            if sum(w) == 0:
                continue
            est = sum(wi * e["reward"] for wi, e in zip(w, logged)) / sum(w)
            print(f"      always {arm:7} would have earned ~{est:.2f}  (from {sum(1 for x in w if x)} matching)")


if __name__ == "__main__":
    main()
