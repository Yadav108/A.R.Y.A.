"""
Build the "was this said to ARYA?" dataset from the real log.

    python -m arya.research.addressee_data            # -> %LOCALAPPDATA%\\ARYA\\research\\addressee.jsonl

One example per follow-up (speech heard in a conversation without the wake word), with:
  text            what Whisper heard
  decision        what ARYA did: "answered" / "ignored" / "rescued" (ignored, then you pressed "It was for you")
  why             the reason ARYA logged for ignoring it (rule / model (0.3s) / music playing …)
  prev_reply      ARYA's last reply before it (from the history database)
  reply_age_s     seconds since that reply
  prev_user       your last request before it
  label           "arya" / "other" / null — the gold label, filled by annotation (labels.jsonl, kept separately so
                  rebuilding the dataset never loses them)

"rescued" is a free, certain label: you said it was for ARYA. Everything else needs annotation.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

DATA_DIR = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA")
LOG = DATA_DIR / "logs" / "arya.log"
OUT_DIR = DATA_DIR / "research"
DB = Path(os.getenv("ARYA_DB_PATH") or Path(__file__).resolve().parents[1] / "memory" / "arya.db")

# The laptop's clock was India Standard Time until 2026-11-16 21:20 (then Berlin); the database is UTC.
_IST_UNTIL = datetime(2026, 11, 16, 21, 20)
RESCUE_WINDOW_S = 60

_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")


@dataclass
class Example:
    ts: str
    text: str
    decision: str
    why: str = ""
    prev_reply: str = ""
    reply_age_s: float | None = None
    prev_user: str = ""
    label: str | None = None
    extra: dict = field(default_factory=dict)


def _quoted(s: str) -> str:
    try:
        return str(ast.literal_eval(s.strip()))
    except Exception:
        return s.strip().strip("'\"")


def _to_utc(local: datetime) -> datetime:
    if local < _IST_UNTIL:
        return local - timedelta(hours=5, minutes=30)
    # Berlin: CET (UTC+1) in November; CEST (UTC+2) from the last Sunday of March to the last Sunday of October
    return local - timedelta(hours=1 if local.month in (11, 12, 1, 2) or (local.month == 10 and local.day > 25)
                             or (local.month == 3 and local.day < 25) else 2)


def parse_log(path: Path = LOG) -> list[Example]:
    lines = []                    # (time or None, message): the oldest part of the log has no timestamps
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TS.match(raw)
        lines.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"), m.group(2)) if m else (None, raw.strip()))
    out: list[Example] = []
    for i, (when, msg) in enumerate(lines):
        if not msg.startswith("WakeWord FOLLOW-UP: "):
            continue
        text = _quoted(msg[len("WakeWord FOLLOW-UP: "):])
        decision, why = "answered", ""
        for when2, msg2 in lines[i + 1:i + 8]:
            if msg2.startswith(("WakeWord FOLLOW-UP", "WakeWord command", "WakeWord BARGE-IN")):
                break
            if msg2.startswith("Not for ARYA ("):
                end = msg2.find(") — ")                  # "Not for ARYA (model (0.1s)) — staying quiet: …"
                decision, why = "ignored", msg2[len("Not for ARYA ("):end if end > 0 else None]
                break
            if msg2.startswith("WakeWord: just the name again"):
                decision, why = "ignored", "name only"
                break
            if msg2.startswith("Processing: "):
                break
        else:
            decision = "unknown"
        if decision == "ignored":
            # "It was for you": the same text processed shortly after, with no new speech in between
            for when2, msg2 in lines[i + 1:i + 40]:
                late = when and when2 and (when2 - when).total_seconds() > RESCUE_WINDOW_S
                if late or msg2.startswith("WakeWord FOLLOW-UP"):
                    break
                if msg2.startswith("Processing: ") and text[:40].strip().startswith(msg2[12:52].strip()[:30]):
                    decision = "rescued"
                    break
        out.append(Example(ts=when.isoformat(sep=" ") if when else f"line {i}", text=text, decision=decision,
                           why=why))
    return out


def attach_context(examples: list[Example], db: Path = DB) -> None:
    """ARYA's last reply (and your last request) before each sentence, from the history database."""
    if not db.exists():
        return
    con = sqlite3.connect(db)
    rows = con.execute("SELECT role, content, timestamp FROM messages ORDER BY timestamp, id").fetchall()
    con.close()
    # "Model answer." / "Tool answer." are the routing benchmark's fakes, written into the real history once
    # (16 Nov, 21:35-21:38, before it was isolated): not real context.
    msgs = [(r, c, datetime.strptime(t, "%Y-%m-%d %H:%M:%S")) for r, c, t in rows
            if c not in ("Model answer.", "Tool answer.")]
    j = 0
    last = {"assistant": None, "user": None}
    untimed_pos = 0                               # where in the history the untimestamped part of the log is
    for ex in examples:
        if ex.ts.startswith("line "):
            # No clock: find the sentence itself among your saved messages (answered ones were saved), and take
            # what came right before it; an ignored one sits after the previous match.
            hit = next((k for k in range(untimed_pos, len(msgs)) if msgs[k][0] == "user"
                        and msgs[k][1].strip().lower()[:40] in ex.text.strip().lower()), None)
            pos = hit if hit is not None else untimed_pos
            untimed_pos = hit + 1 if hit is not None else untimed_pos
            before = msgs[:pos]
            a = next((m for m in reversed(before) if m[0] == "assistant"), None)
            u = next((m for m in reversed(before) if m[0] == "user"), None)
            ex.prev_reply = a[1][:400] if a else ""
            ex.prev_user = u[1][:200] if u else ""
            continue
        utc = _to_utc(datetime.fromisoformat(ex.ts))
        # strictly before: the sentence itself (if answered) is saved at the same second
        while j < len(msgs) and msgs[j][2] < utc:
            last[msgs[j][0]] = msgs[j]
            j += 1
        if last["assistant"]:
            ex.prev_reply = last["assistant"][1][:400]
            age = (utc - last["assistant"][2]).total_seconds()
            # Around the time-zone change (16 Nov) the clock jumps; an impossible age is unknown, not negative.
            ex.reply_age_s = round(age, 1) if 0 <= age < 6 * 3600 else None
        if last["user"]:
            ex.prev_user = last["user"][1][:200]


def load_labels(path: Path | None = None) -> dict[tuple[str, str], str]:
    path = path or OUT_DIR / "labels.jsonl"
    labels = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                d = json.loads(line)
                labels[(d["ts"], d["text"])] = d["label"]
    return labels


def build(path: Path = LOG) -> list[Example]:
    examples = parse_log(path)
    attach_context(examples)
    labels = load_labels()
    for ex in examples:
        ex.label = labels.get((ex.ts, ex.text))
        if ex.label is None and ex.decision == "rescued":
            ex.label = "arya"                           # you said so
    # "That wasn't for you": the latest answered follow-up with that text was side talk (you said so)
    fb = OUT_DIR / "feedback.jsonl"
    if fb.exists():
        for line in fb.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            hit = next((e for e in reversed(examples) if e.decision == "answered"
                        and d["text"].strip().lower() in e.text.strip().lower()), None)
            if hit is not None:
                hit.label = d["label"]
    return examples


def load(path: Path | None = None) -> list[Example]:
    path = path or OUT_DIR / "addressee.jsonl"
    return [Example(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    examples = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "addressee.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for ex in examples:
            f.write(json.dumps(asdict(ex), ensure_ascii=False) + "\n")
    from collections import Counter
    print(f"{len(examples)} follow-ups -> {out}")
    print("decisions:", dict(Counter(e.decision for e in examples)))
    print("labelled:", dict(Counter(e.label for e in examples)))


if __name__ == "__main__":
    main()
