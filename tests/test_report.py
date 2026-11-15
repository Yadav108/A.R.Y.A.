"""C1: field report from ARYA's log, and the guided voice check — on synthetic logs."""
import threading
import time
from datetime import datetime

import pytest

from arya import report as R
from arya import voicecheck as V

LOG = """
===== ARYA desktop started 2026-11-15 20:00:00 =====
Processing: what's on tomorrow
Instant answer: calendar
Latency: first words after 0.80s (answer 0.70s + voice 0.10s)
[2026-11-15 21:00:00] WakeWord TRIGGERED (verified: 'Hey Arya.')
[2026-11-15 21:00:01] Latency: speech recognised in 1.10s
[2026-11-15 21:00:02] Processing: explain the Coriolis acceleration
[2026-11-15 21:00:02] Needs tool: True
[2026-11-15 21:00:04] Latency: first words after 2.40s (answer 1.90s + voice 0.50s)
[2026-11-15 21:01:00] Not for ARYA (model (0.1s)) — staying quiet: 'minimize the Chrome window.'
[2026-11-15 21:01:03] Processing: minimize the Chrome window.
[2026-11-15 21:01:03] Needs tool: False
[2026-11-15 21:01:04] Blocked a false action claim: 'Window minimized to the taskbar.'
[2026-11-15 21:02:00] WakeWord: rejected — heard 'hey are you'
[2026-11-15 21:03:00] Processing: set a timer for 10 minutes
[2026-11-15 21:03:00] Needs tool: True
[2026-11-15 21:03:01] Pipeline error: database or disk is full
[2026-11-15 21:04:00] WakeWord BARGE-IN stop (no wake word): 'stop'
[2026-11-15 21:05:00] WakeWord: ignored while speaking (not a stop): 'the force'
""".lstrip()


@pytest.fixture
def log(tmp_path):
    p = tmp_path / "arya.log"
    p.write_text(LOG, encoding="utf-8")
    return p


def test_parse(log):
    rep = R.parse(days=1, paths=[log], now=datetime(2026, 11, 16, 9, 0))
    assert [(t.text, t.route) for t in rep.turns] == [
        ("what's on tomorrow", "calendar"), ("explain the Coriolis acceleration", "model + tools"),
        ("minimize the Chrome window.", "model"), ("set a timer for 10 minutes", "model + tools")]
    assert rep.turns[0].when == datetime(2026, 11, 15, 20, 0)          # unstamped lines: the session start
    assert [t.first_words for t in rep.turns[:2]] == [0.8, 2.4]
    assert rep.turns[2].overridden and rep.turns[2].blocked == ["'Window minimized to the taskbar.'"]
    assert (rep.wakes, rep.wake_rejected, rep.voice_stops, rep.heard_while_speaking, rep.ignored) == (1, 1, 1, 1, 1)
    assert rep.errors == ["Pipeline error: database or disk is full"] and rep.recognition == [1.1]


def test_window_and_render(log):
    assert R.parse(days=0.25, paths=[log], now=datetime(2026, 11, 16, 9, 0)).turns == []    # older than 6 h
    rep = R.parse(days=1, paths=[log], now=datetime(2026, 11, 16, 9, 0))
    text = R.render(rep, 1)
    assert "## Turns: 4" in text and "Instant (no model): 1 (25 %)" in text
    assert "median 1.60 s" in text and "Stopped by voice: 1" in text
    assert "you overrode 1" in text and "Window minimized to the taskbar." in text


def test_candidates(log):
    cands = R.candidates(R.parse(days=1, paths=[log], now=datetime(2026, 11, 16, 9, 0)))
    by_q = {c["q"]: c["why"] for c in cands}
    assert "false action claim blocked" in by_q["minimize the Chrome window."]
    assert "ignored as side talk" in by_q["minimize the Chrome window."]
    assert "error: Pipeline error" in by_q["set a timer for 10 minutes"]
    assert "what's on tomorrow" not in by_q                              # went fine


def _write_later(path, lines, delay=0.4):
    def go():
        time.sleep(delay)
        with path.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"[2026-11-15 21:00:00] {line}\n")
                f.flush()
                time.sleep(0.05)
    threading.Thread(target=go, daemon=True).start()


def test_voicecheck_stop_trials(tmp_path, monkeypatch):
    log = tmp_path / "arya.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(V, "LOG", log)
    real_sleep = time.sleep
    monkeypatch.setattr(V.time, "sleep", lambda s: None if s >= 1 else real_sleep(s))   # skip the 2 s pauses
    _write_later(log, ["Processing: explain the Coriolis acceleration in detail", "WakeWord BARGE-IN stop (no wake word): 'stop'",
                       "Processing: explain torque in detail", "WakeWord: ignored while speaking (not a stop): 'top'",
                       "Response complete"])
    result = V.stop_trials(2)
    assert result == {"stop_trials": 2, "stopped": 1, "notes": ["heard 'top'"]}


def test_voicecheck_self_interruption(tmp_path, monkeypatch):
    log = tmp_path / "arya.log"
    log.write_text("", encoding="utf-8")
    monkeypatch.setattr(V, "LOG", log)
    _write_later(log, ["Processing: explain torque in detail", "WakeWord: ignored while speaking (not a stop): 'torque'",
                       "Response complete", "Processing: explain friction in detail",
                       "WakeWord TRIGGERED (verified: 'Hey Arya')", "Processing: next"], delay=0.2)
    result = V.self_interruption(minutes=0.03)
    assert result["answers"] == 3 and result["self_interruptions"] == 1 and result["overheard_ignored"] == 1


def test_log_lines_get_timestamps():
    import io
    import re as _re

    from arya.desktop.app import StampedStream
    buf = io.StringIO()
    s = StampedStream(buf)
    s.write("Processing: hi\nInstant answer: ")
    s.write("calendar\n")
    s.write("")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2 and all(_re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] ", x) for x in lines)
    assert lines[1].endswith("] Instant answer: calendar")
