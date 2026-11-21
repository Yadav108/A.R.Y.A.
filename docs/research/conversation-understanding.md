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

## Problem 2: follow-ups that refer back (done, v1)

**Data.** I mined the history for short follow-ups that refer to an earlier turn, and found 38. The ones
ARYA got wrong fall into four patterns:
- **Complaints after an action:** "No it haven't" after "Resume playing the song".
- **Pronouns for the last thing named:** "switch to it" (a tab), "Delete it." (a photo), "Find it in the
  Windows Explorer." (a document).
- **A detail spread over turns:** "Nico's MCP phase 1 plan" … "It is in downloads."
- **Repeats:** "again", "one more time".

**Benchmark.** `python -m arya.eval --dialogues` runs `arya/eval/dialogues.yaml`: 13 dialogues (26
turns) built from these patterns, in the routing sandbox, without a reset between turns. Two of the
dialogues check that chat and questions are *not* resolved ("explain it again more simply",
"it didn't rain today").

**Method.** `arya/core/dialogue.py` keeps one record:
- **The last action:** which handler answered and the exact request. Only actions safe to repeat count
  (music, tabs, windows, opening apps, sites and files), never a delete, a move, a message or a timer.
- **The entities named:** song, tab, app, window, document and its path.

Before a turn, `resolve()` rewrites a referring sentence into the explicit request, which then goes
through the usual handlers. After the turn, `record()` notes what happened. A chat or question turn in
between clears the last action, and references expire after 10 minutes. File names are now matched
fuzzily as word sets, so "MCP phase one plan" finds `NICOS_MCP_Phase1_Plan.docx`.

**Results** (21 Nov 2026):

| | turns passed | referring turns resolved |
|---|---|---|
| before | 13/26 (50 %) | 1/11 |
| after | **26/26 (100 %)** | **11/11** |

The routing benchmark stays at 159/159, and the golden questions are untouched (none was rewritten).

**Limits.** The benchmark is hand-built from log patterns. It counts which handler answered, not whether
that was the best result, and "the second one" (picking from a list) isn't covered yet.

## Problem 3: learning only from what was said to ARYA (done, v1)

**Failure.** Learning read every saved user line. Side talk that ARYA wrongly *answered* became
evidence for memories. Between 19 and 21 Nov this produced five wrong memories, all deleted:
"spontaneous, doesn't plan ahead" (from song lyrics), "wants to explore the city", "working on a
psychology project", "prefers own work", and an out-of-date reminder.

**Method.**
- Every user line is now saved with **how it reached ARYA**, in the `message_source` table:
  `wake`, `name`, `typed`, `push-to-talk`, `phone`, `follow:rule`, `follow:answer`,
  `follow:classifier` with its p, `follow:model`, `follow:unfiltered`, or `side-talk` (marked by
  "that wasn't for you").
- `learning.said_to_arya()` drops lines that weren't clearly said to ARYA, together with ARYA's
  answer to them, before the model reads the conversation.
- **Trusted** means: wake word, the name, typed, push-to-talk, phone, a rule, a bare answer to
  ARYA's question, or the classifier at p ≥ 0.7.
- Older lines without a recorded source are judged again: the name or a rule, otherwise the
  classifier.

**Measured** (`python -m arya.research.learning_trust_eval`, 379 labelled follow-ups, classifier
probabilities leave-one-day-out):

| | learnable follow-ups | side talk among them | real requests kept |
|---|---|---|---|
| before: everything answered | 222 | 14.0 % | 86.4 % |
| after, p ≥ 0.85 | 128 | 2.3 % | 56.6 % |
| **after, p ≥ 0.7 (chosen)** | **147** | **2.0 %** | **65.2 %** |

Lines said with the wake word, typed, or sent from the phone are always learnable, so most of what
ARYA learns from is unaffected.

The learning benchmark (`--learning`) has three new cases: lyrics answered, a chat with a friend
next to a real fact, and a sure follow-up that must still be learned. Result: 17/17 cases,
0 forbidden memories, recall 100 %.

## Next problems (planned)

4. **Intent routing accuracy.** Extend the routing benchmark from the log's misroutes, and mine
   `python -m arya.report --candidates` weekly.
5. **ASR robustness.** Measure the name variants Whisper produces ("Enjoy", "Andrew", "GMT") and
   initial-prompt biasing for "NJ" / "ARYA", against the 22 recorded wake samples.
6. **Efficiency.** Per-turn latency budget: the addressee check now usually costs under 100 ms
   instead of a 0.1–0.8 s model call. Next: cache prompt prefixes and skip the knowledge lookup for
   chit-chat.
