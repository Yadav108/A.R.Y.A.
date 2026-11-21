"""
Do the bandit's options really change the answers? (If the model ignored them, there would be nothing to learn.)

    python -m arya.research.bandit_actuators

For a fixed set of questions and chat lines, the local model answers once per option; measured:
  length     sentences per answer for brief / normal / full
  follow_up  share of answers that end with a question for ask / no_ask
"""
from __future__ import annotations

import asyncio
import re

PROMPTS = [
    "why is the sky blue?",
    "what is a PID controller?",
    "explain the Coriolis acceleration",
    "how does a transformer work in electrical engineering?",
    "what should I cook tonight, something quick",
    "I had a long day at the lab today",
    "what's a good way to start learning Rust?",
    "tell me about the history of the bicycle",
]


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if re.search(r"[A-Za-z]{2,}", s)])


async def _answer(prompt: str, arms: dict[str, str]) -> str:
    import arya.core.llm as llm
    from arya.core import bandit
    from arya.core.llm_local import chat_local
    bandit._current.clear()
    bandit._current.update(arms)
    try:
        reply = await chat_local([{"role": "system", "content": llm._system_prompt(with_tools=False)},
                                  {"role": "user", "content": prompt}], temperature=0.3, max_tokens=260)
    finally:
        bandit._current.clear()
    return (reply.get("content") or "").strip()


async def run() -> None:
    lengths = {arm: [] for arm in ("brief", "normal", "full")}
    asks = {arm: [] for arm in ("ask", "no_ask")}
    for p in PROMPTS:
        for arm in lengths:
            lengths[arm].append(_sentences(await _answer(p, {"length": arm, "follow_up": "no_ask"})))
        for arm in asks:
            asks[arm].append((await _answer(p, {"length": "normal", "follow_up": arm})).rstrip().endswith("?"))
    for arm, xs in lengths.items():
        print(f"length    {arm:7} {sum(xs) / len(xs):4.1f} sentences  {xs}")
    for arm, xs in asks.items():
        print(f"follow_up {arm:7} {sum(xs) / len(xs):5.0%} end with a question")


if __name__ == "__main__":
    asyncio.run(run())
