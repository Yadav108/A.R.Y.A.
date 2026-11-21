# Conversation understanding: research plan and results

ARYA hears everything said in the room during a conversation, not only what is meant for it. The
first question every follow-up has to answer is *"was that said to me?"*. The second is *"what does
the user mean?"*. This document keeps the plan, the method and the measured results. Everything is
measured on real use, and the data stays on the laptop.

## Problem 1: addressee detection (done, v1)

**Data.** `python -m arya.research.addressee_data` turns `arya.log` into one example per follow-up
(speech heard without the wake word). Each example carries ARYA's previous reply, the seconds since
that reply, and the decision ARYA made.

Labels come from three sources:
- **"It was for you" clicks** (rescues): certain positives, taken from the log.
- **"That wasn't for you"** (new voice command): certain negatives, stored in `research/feedback.jsonl`.
- **Annotation** of the rest: 473 follow-ups, 379 labelled, and 94 left out as ambiguous.

The dataset and labels live in `%LOCALAPPDATA%\ARYA\research`, never in this repository.

**Metric.** Two errors with different costs:
- **False accept (FA):** side talk answered. This is the worse error, because it also pollutes
  history and learned memory.
- **False reject (FR):** a request ignored.

Model selection minimises 2·FA + FR. Evaluation is **leave-one-day-out**, so the same day's phrases
never sit in both training and test.

**Model.** Stage 1, the rules, stay as before: they are high precision (98.5 % when they decide)
and cover 38 % of follow-ups. Stage 2 is new: L2 logistic regression on

- the MiniLM sentence embedding (384 dimensions, already loaded for the knowledge index), plus
- 15 conversation features: similarity to ARYA's last reply, reply age, whether ARYA asked a real
  question, question shape, starts with a verb, you / we / I, repetition, English share.

Stage 3 means ARYA asks the local 3B model only when p is between 0.4 and 0.7.

Training uses scikit-learn. ARYA applies the model with numpy, which takes 40–80 ms on the CPU.

**Results** (379 labelled follow-ups, leave-one-day-out, 21 Nov 2026):

| pipeline | accuracy | FA (side talk answered) | FR (request ignored) | 3B-model calls |
|---|---|---|---|---|
| rules only | 75.2 % | 1.3 % | 41.6 % | 0 |
| rules + 3B model (before) | 78.9 % | 20.3 % | 21.7 % | 233 |
| rules + classifier | 83.4 % | 6.3 % | 24.0 % | 0 |
| **rules + classifier + 3B model in the band** | **86.5 %** | **11.4 %** | **14.9 %** | **71** |

**Continual learning.** Once a day, after the backup, ARYA rebuilds the dataset from the log
(including each "It was for you" and "that wasn't for you" since the last refit) and refits. It
replaces the model only if the leave-one-day-out cost isn't worse.

**Known limits.**
- The labels come from a single annotator.
- The decision band was chosen on the same cross-validation, so the numbers are slightly optimistic.
- The music-playing state isn't in the old logs, so that rule (lyrics are ignored) isn't measured here.
- 84 examples from the oldest part of the log have no timestamps, so their reply age is unknown.

## Next problems (planned)

2. **Reference resolution / dialogue state.** "Switch to it", "do that again", "the second one",
   "it didn't work" currently each need their own rule. Plan: one dialogue-state record holding the
   last action, its arguments, its outcome, and the entities mentioned (tab, song, file, time).
   Follow-ups would resolve against that record. Measured on the follow-ups that refer back.
3. **Learning from side talk.** Exclude exchanges marked "that wasn't for you" from
   `memory/learning.py`. In the 21 Nov log, song lyrics were learned as a preference.
4. **Intent routing accuracy.** Extend the routing benchmark from the log's misroutes, and mine
   `python -m arya.report --candidates` weekly.
5. **ASR robustness.** Measure the name variants Whisper produces ("Enjoy", "Andrew", "GMT") and
   initial-prompt biasing for "NJ" / "ARYA", against the 22 recorded wake samples.
6. **Efficiency.** Per-turn latency budget: the addressee check now usually costs under 100 ms
   instead of a 0.1–0.8 s model call. Next: cache prompt prefixes and skip the knowledge lookup for
   chit-chat.
