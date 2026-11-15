"""
Guided live voice check (roadmap C1 / M5): run it in a terminal while ARYA is running.

    python -m arya.voicecheck                 # 10 "stop" trials, then 10 minutes of listening to ARYA
    python -m arya.voicecheck --stops 5 --minutes 3

Part 1 — "stop": you ask for a long answer and say "stop" while ARYA talks. A trial passes when ARYA
stops (it reads ARYA's log to know), and fails when ARYA finishes talking anyway — the check then shows
what ARYA heard instead, which tells whether it was the microphone, the recogniser or the stop rule.
Target: at least 9 of 10.

Part 2 — self-interruption: ask for long answers and stay silent. Every time ARYA stops or "hears" a wake
word while you're silent, it interrupted itself. Target: none in 10 minutes.

Results are saved to …\\ARYA\\voicecheck.json and appear in `python -m arya.report`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime

from arya.knowledge.library import DATA_DIR

LOG = DATA_DIR / "logs" / "arya.log"
RESULTS = DATA_DIR / "voicecheck.json"
_STAMP = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ")
STOPPED = re.compile(r"^(?:WakeWord BARGE-IN stop|Stopped by request)")
FINISHED = re.compile(r"^Response complete")
STARTED = re.compile(r"^Processing: ")
HEARD = re.compile(r"ignored while speaking[^:]*: (.*)$")
WOKE = re.compile(r"^WakeWord TRIGGERED")


class Tail:
    """New lines of ARYA's log as they are written (timestamps stripped)."""

    def __init__(self) -> None:
        self.f = LOG.open(encoding="utf-8", errors="replace")
        self.f.seek(0, 2)

    def lines(self, seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            line = self.f.readline()
            if not line:
                time.sleep(0.15)
                continue
            yield _STAMP.sub("", line.rstrip("\n"))


def stop_trials(n: int) -> dict:
    tail = Tail()
    passed, notes = 0, []
    print(f"\nPart 1 — \"stop\" ({n} trials)\n"
          "Each trial: say \"Hey ARYA, explain the Coriolis acceleration in detail\" (or any long question).\n"
          "When ARYA has said a sentence or two, say just \"stop\" in a normal voice.\n")
    for i in range(1, n + 1):
        print(f"Trial {i}/{n}: ask your question now…", flush=True)
        started = any(STARTED.match(line) for line in _until(tail, STARTED, 90))
        if not started:
            print("  No question heard in 90 s — trial skipped.")
            notes.append("skipped")
            continue
        print("  ARYA is answering — say \"stop\" now.", flush=True)
        heard, result = [], None
        for line in tail.lines(120):
            if STOPPED.match(line):
                result = "pass"
                break
            if FINISHED.match(line):
                result = "fail"
                break
            m = HEARD.search(line)
            if m:
                heard.append(m.group(1))
        if result == "pass":
            passed += 1
            print("  PASS — ARYA stopped.")
        else:
            why = f" It heard: {', '.join(heard[:3])}" if heard else " It heard nothing while talking."
            print(f"  FAIL — ARYA kept talking.{why}")
            notes.append("heard " + "; ".join(heard[:3]) if heard else "heard nothing")
        time.sleep(2)
    done = n - notes.count("skipped")
    print(f"\n\"stop\": {passed}/{done} stopped (target: 9 of 10).")
    return {"stop_trials": done, "stopped": passed, "notes": [x for x in notes if x != "skipped"]}


def _until(tail: Tail, pattern, seconds: float):
    for line in tail.lines(seconds):
        yield line
        if pattern.match(line):
            return


def self_interruption(minutes: float) -> dict:
    tail = Tail()
    print(f"\nPart 2 — self-interruption ({minutes:g} minutes)\n"
          "Ask for long answers (\"… in detail\") one after another and stay silent while ARYA talks.\n"
          "Nothing else to do — this part ends by itself.\n", flush=True)
    answers, interruptions, overheard = 0, [], 0
    speaking = False
    for line in tail.lines(minutes * 60):
        if STARTED.match(line):
            speaking = True
            answers += 1
        elif FINISHED.match(line):
            speaking = False
        elif speaking and (STOPPED.match(line) or WOKE.match(line)):
            interruptions.append(line[:100])
            print(f"  Self-interruption: {line[:100]}", flush=True)
            speaking = False
        elif speaking and HEARD.search(line):
            overheard += 1
    print(f"\nSelf-interruptions: {len(interruptions)} in {minutes:g} minutes over {answers} answers "
          f"(target: 0). Heard-but-ignored while talking: {overheard}.")
    return {"minutes": minutes, "answers": answers, "self_interruptions": len(interruptions),
            "overheard_ignored": overheard, "examples": interruptions[:5]}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Guided live check of 'stop' and self-interruption.")
    ap.add_argument("--stops", type=int, default=10)
    ap.add_argument("--minutes", type=float, default=10)
    args = ap.parse_args(argv)
    if not LOG.exists():
        print(f"ARYA's log isn't at {LOG} — start ARYA first.")
        return 1
    result = {"when": datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        if args.stops:
            result.update(stop_trials(args.stops))
        if args.minutes:
            result.update(self_interruption(args.minutes))
    except KeyboardInterrupt:
        print("\nStopped early — saving what was measured.")
    history = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else []
    history.append(result)
    RESULTS.write_text(json.dumps(history, indent=1), encoding="utf-8")
    print(f"Saved to {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
