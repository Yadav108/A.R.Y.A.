"""
Field report (roadmap C1): how ARYA did in real use, read from its own log.

    python -m arya.report               # last 7 days
    python -m arya.report --days 1      # today and yesterday's evening
    python -m arya.report --candidates  # also write benchmark candidates from real failures

What it counts: turns and which part of ARYA answered them (instant handlers vs. the model), real
latency (median and slowest 10 %), wake-word hits and rejections, voice stops, speech ARYA judged
"not for me" that you overrode with "It was for you", blocked false claims, and errors.
Turns that went wrong become *candidates* for the benchmark (arya/eval), so it grows from real use.

The report is saved as a Markdown file next to the log (…\\ARYA\\reports\\). Nothing leaves the laptop.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from arya.knowledge.library import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
REPORT_DIR = DATA_DIR / "reports"

_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] ")
_START = re.compile(r"^===== ARYA desktop started (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) =====")
_ROUTES = [
    (re.compile(r"^Instant answer: (.+)$"), lambda m: m.group(1)),
    (re.compile(r"^Practice: "), lambda m: "practice"),
    (re.compile(r"^Tutor: "), lambda m: "tutor"),
    (re.compile(r"^Instant visual: "), lambda m: "exercise figure"),
    (re.compile(r"^Instant (?:diagram|plot): "), lambda m: "plot / diagram"),
    (re.compile(r"^Instant command: media_control"), lambda m: "media keys"),
    (re.compile(r"^Instant command: "), lambda m: "system status"),
    (re.compile(r"^Offering cloud vision"), lambda m: "handwriting (cloud offer)"),
    (re.compile(r"^Offering the cloud"), lambda m: "cloud offer"),
    (re.compile(r"^Confirmed: "), lambda m: "confirmed action"),
    (re.compile(r"^Needs tool: True"), lambda m: "model + tools"),
    (re.compile(r"^Needs tool: False"), lambda m: "model"),
]
_ERRORS = re.compile(r"^(?:Pipeline error|Traceback|TTS error|TTS timeout|Announcement failed|Nudge check failed|"
                     r"[A-Z][\w ]+ skipped: (?!.*\bnot\b)|LLM: .*failed|.*Ollama error)")
MODEL_ROUTES = {"model", "model + tools"}


@dataclass
class Turn:
    when: datetime
    text: str
    route: str = ""
    first_words: float | None = None
    blocked: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    overridden: bool = False              # ARYA ignored it, you clicked "It was for you"


@dataclass
class Report:
    since: datetime
    turns: list[Turn] = field(default_factory=list)
    recognition: list[float] = field(default_factory=list)
    wakes: int = 0
    wake_rejected: int = 0
    voice_stops: int = 0
    heard_while_speaking: int = 0
    ignored: int = 0
    errors: list[str] = field(default_factory=list)
    sessions: int = 0


def _lines(paths: list[Path]):
    """(timestamp, text) for every log line; unstamped lines inherit the last known time."""
    now = None
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                m = _STAMP.match(line)
                if m:
                    now = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    line = line[m.end():]
                else:
                    s = _START.match(line)
                    if s:
                        now = datetime.strptime(s.group(1), "%Y-%m-%d %H:%M:%S")
                        yield now, "__SESSION__"
                        continue
                if now is not None:
                    yield now, line


def parse(days: float = 7, paths: list[Path] | None = None, now: datetime | None = None) -> Report:
    now = now or datetime.now()
    rep = Report(since=now - timedelta(days=days))
    paths = paths or [LOG_DIR / "arya.log.1", LOG_DIR / "arya.log"]
    turn: Turn | None = None
    ignored_recent: list[tuple[datetime, str]] = []
    for when, line in _lines(paths):
        if when < rep.since:
            continue
        if line == "__SESSION__":
            rep.sessions += 1
            continue
        if line.startswith("Processing: "):
            text = line[len("Processing: "):]
            turn = Turn(when, text)
            # "It was for you" comes right after: only within 2 minutes ("Yes" 33 minutes later is new speech)
            if any((text.startswith(i[:60]) or i.startswith(text)) and when - t <= timedelta(minutes=2)
                   for t, i in ignored_recent[-3:]):
                turn.overridden = True
            rep.turns.append(turn)
            continue
        if turn is not None and not turn.route:
            for pattern, name in _ROUTES:
                m = pattern.match(line)
                if m:
                    turn.route = name(m)
                    break
        m = re.match(r"^Latency: first words after ([\d.]+)s", line)
        if m and turn is not None and turn.first_words is None:
            turn.first_words = float(m.group(1))
        m = re.match(r"^Latency: speech recognised in ([\d.]+)s", line)
        if m:
            rep.recognition.append(float(m.group(1)))
        if line.startswith("WakeWord TRIGGERED"):
            rep.wakes += 1
        elif re.match(r"^WakeWord(?::)? (?:rejected|dropped|heard \(no match\)|: verification failed)", line):
            rep.wake_rejected += 1
        elif line.startswith("WakeWord BARGE-IN stop") or line.startswith("Stopped by request"):
            rep.voice_stops += 1
        elif "ignored while speaking" in line:
            rep.heard_while_speaking += 1
        if line.startswith("Not for ARYA"):
            rep.ignored += 1
            m = re.search(r"staying quiet: '(.*)'$", line) or re.search(r'staying quiet: "(.*)"$', line)
            if m:
                ignored_recent.append((when, m.group(1)))
        if line.startswith("Blocked a false action claim") and turn is not None:
            turn.blocked.append(line.split(": ", 1)[-1])
        if _ERRORS.match(line):
            rep.errors.append(line[:160])
            if turn is not None:
                turn.errors.append(line[:160])
    _complete_texts(rep.turns)
    return rep


def _complete_texts(turns: list[Turn]) -> None:
    """The log keeps the first 60 characters of a request; the history has the whole sentence."""
    try:
        from arya.memory.database import DB_PATH
        with sqlite3.connect(DB_PATH) as c:
            rows = [r[0] for r in c.execute("SELECT content FROM messages WHERE role = 'user' "
                                            "ORDER BY id DESC LIMIT 3000")]
    except Exception:
        return
    for t in turns:
        if len(t.text) >= 60:
            full = next((r for r in rows if r.startswith(t.text)), None)
            if full:
                t.text = full


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def candidates(rep: Report) -> list[dict]:
    """Turns worth turning into benchmark questions."""
    from arya.core.addressee import _ASSISTANT_COMMAND

    out, seen = [], set()
    for t in rep.turns:
        why = []
        if t.blocked:
            why.append("false action claim blocked: " + t.blocked[0][:80])
        if t.overridden:
            why.append("ignored as side talk, you said it was for ARYA")
        if t.errors:
            why.append("error: " + t.errors[0][:80])
        if t.route in MODEL_ROUTES and _ASSISTANT_COMMAND.match(t.text):
            why.append("a command that no handler caught (went to the model)")
        key = t.text.lower().strip(" .?!")
        if why and key not in seen:
            seen.add(key)
            out.append({"q": t.text, "route": t.route or "?", "why": "; ".join(why),
                        "when": t.when.strftime("%Y-%m-%d %H:%M")})
    return out


def render(rep: Report, days: float) -> str:
    lines = [f"# ARYA field report — last {days:g} day{'s' if days != 1 else ''}",
             f"Since {rep.since:%Y-%m-%d %H:%M}; {rep.sessions} app start{'s' if rep.sessions != 1 else ''}.", ""]
    n = len(rep.turns)
    lines.append(f"## Turns: {n}")
    if n:
        routes = Counter(t.route or "(no answer)" for t in rep.turns)
        model = sum(v for k, v in routes.items() if k in MODEL_ROUTES)
        lines.append(f"Instant (no model): {n - model} ({round(100 * (n - model) / n)} %), "
                     f"model: {model} ({round(100 * model / n)} %).")
        lines += [f"- {k}: {v}" for k, v in routes.most_common()]
    lines.append("")
    fw = [t.first_words for t in rep.turns if t.first_words is not None]
    lines.append("## Latency")
    if fw:
        lines.append(f"First words after the request: median {statistics.median(fw):.2f} s, "
                     f"slowest 10 % {_pct(fw, 0.9):.2f} s, worst {max(fw):.2f} s ({len(fw)} answers).")
    if rep.recognition:
        lines.append(f"Speech recognition: median {statistics.median(rep.recognition):.2f} s, "
                     f"slowest 10 % {_pct(rep.recognition, 0.9):.2f} s.")
    if fw and rep.recognition:
        total = statistics.median(fw) + statistics.median(rep.recognition) + 0.72
        lines.append(f"End of your speech to ARYA's first word: about {total:.1f} s (target 2.5 s).")
    lines += ["", "## Listening",
              f"- Wake word: {rep.wakes} accepted, {rep.wake_rejected} rejected after checking",
              f"- Stopped by voice: {rep.voice_stops}",
              f"- Heard while ARYA spoke but not taken as a command: {rep.heard_while_speaking}",
              f"- Judged \"not for ARYA\": {rep.ignored}, of which you overrode "
              f"{sum(t.overridden for t in rep.turns)}"]
    checks = DATA_DIR / "voicecheck.json"
    if checks.exists():
        try:
            import json
            last = json.loads(checks.read_text(encoding="utf-8"))[-1]
            bits = []
            if last.get("stop_trials"):
                bits.append(f"\"stop\" worked {last['stopped']}/{last['stop_trials']} (target 9/10)")
            if "self_interruptions" in last:
                bits.append(f"{last['self_interruptions']} self-interruptions in {last['minutes']:g} min (target 0)")
            if bits:
                lines.append(f"- Last voice check ({last['when']}): " + ", ".join(bits))
        except Exception:
            pass
    else:
        lines.append("- No voice check yet: run `python -m arya.voicecheck` while ARYA is running")
    blocked = [(t.text, b) for t in rep.turns for b in t.blocked]
    lines += ["", f"## False action claims blocked: {len(blocked)}"]
    lines += [f"- \"{q[:70]}\" → \"{b[:80]}\"" for q, b in blocked[:10]]
    lines += ["", f"## Errors: {len(rep.errors)}"]
    for e, k in Counter(re.sub(r"\d+", "#", e)[:100] for e in rep.errors).most_common(8):
        lines.append(f"- {k}× {e}")
    cands = candidates(rep)
    lines += ["", f"## Benchmark candidates: {len(cands)}"]
    lines += [f"- \"{c['q'][:90]}\" ({c['route']}): {c['why']}" for c in cands[:15]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="How ARYA did in real use, from its log.")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--candidates", action="store_true", help="write benchmark candidates to a YAML file")
    args = ap.parse_args(argv)
    rep = parse(args.days)
    text = render(rep, args.days)
    print(text)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"report-{datetime.now():%Y-%m-%d}.md"
    out.write_text(text, encoding="utf-8")
    print(f"Saved {out}")
    if args.candidates:
        import yaml
        cands = candidates(rep)
        path = REPORT_DIR / "benchmark-candidates.yaml"
        path.write_text(yaml.safe_dump(cands, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"Wrote {len(cands)} candidates to {path} — add the good ones to arya/eval/routes.yaml or golden.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
