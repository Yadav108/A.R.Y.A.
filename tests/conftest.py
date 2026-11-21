"""
Shared test setup.

Everything runs against throwaway data: ARYA's databases point at a temp folder
before any arya module is imported, and the small course library used by the
knowledge tests lives in tests/_tmp_kb/ (ARYA's safety policy refuses to index
anything under AppData, which is where pytest's own temp folders are).
Nothing touches your real history or knowledge.db.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_DATA = Path(tempfile.mkdtemp(prefix="arya_test_"))
os.environ["ARYA_DATA_DIR"] = str(_DATA)
os.environ["ARYA_KNOWLEDGE_DB"] = str(_DATA / "knowledge.db")
os.environ["ARYA_DB_PATH"] = str(_DATA / "arya.db")
os.environ.setdefault("ARYA_MODE", "offline")
os.environ["ARYA_BANDITS"] = "0"            # answers don't vary by learned style in tests (tests/test_bandit.py turns it on)

KB_TMP = ROOT / "tests" / "_tmp_kb"


@pytest.fixture(autouse=True)
def _study_mode():
    """Every test starts in study mode, in memory: the behaviour the older tests were written for, and no
    test inherits a mode another test switched to (tests/test_modes.py picks its own)."""
    from arya.core import modes
    modes.use_memory("study")
    yield


@pytest.fixture(autouse=True)
def _phone_feed(tmp_path, monkeypatch):
    """Every test gets an empty phone feed and a scratch events file: a reminder firing in a test must never
    land in the user's real phone_events.jsonl, nor in another test's feed."""
    from arya.server import phone
    monkeypatch.setattr(phone, "EVENTS_FILE", tmp_path / "phone_events.jsonl")
    monkeypatch.setattr(phone, "_state", phone._State())
    # No Tailscale unless a test says so: on a laptop where it's installed, the real address would change
    # what the phone tests see (it did, once the user installed Tailscale).
    monkeypatch.setattr(phone, "tailscale_ip", lambda: None)
    yield


@pytest.fixture(scope="session")
def bg_loop():
    """An event loop running in a background thread (for code that posts coroutines from threads)."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)


EXERCISES = """# Exercises chapter 1

1-12) A handling robot consists of two rotationally connected rods with a clamshell.
Rod 1 is moved by a servomotor. Determine velocity and acceleration of the clamshell.

1-13) An industrial robot has to transport a workpiece from the position depicted for t = 0
into the dotted position at t = tA. The arm (length L1 = 1 m) rotates with constant angular
velocity w1 = 2/s. Determine the constant angular acceleration a2 relative to L1.

1-14) A disc rotates with constant angular velocity and a slider moves in a radial slot.
Determine the Coriolis acceleration of the slider.

# Answers

1-13) tA = pi/w1 = 1.57 s; a2 = 2 pi/tA2 = 2.55/s2; v = 2.83 m/s; a = 4.26 m/s2.
"""

CONTROL = """# Second order systems

A second order system has natural frequency omega_n and damping ratio zeta. The percent overshoot
of the step response is 100 times exp(-zeta pi / sqrt(1 - zeta^2)). Underdamped systems with zeta
below one oscillate before settling. """ * 2 + """

# Bode plots

A Bode plot shows magnitude in decibels and phase in degrees against frequency on a logarithmic
axis. The bandwidth is where the magnitude falls 3 dB below its low-frequency value. """ * 2

COOKING = ("To bake bread, mix flour, water, yeast and salt, knead it, let it rise, "
           "then bake it at 230 degrees for thirty minutes. ") * 6


@pytest.fixture(scope="session")
def kb():
    """A small indexed course library: Dynamics exercises, Control notes (+ an exact copy), cooking."""
    from arya.knowledge import library

    root = KB_TMP / uuid.uuid4().hex[:8]
    (root / "Dynamics").mkdir(parents=True)
    (root / "Control").mkdir(parents=True)
    (root / "Dynamics" / "Exercises chapter 1.md").write_text(EXERCISES, encoding="utf-8")
    (root / "Control" / "lecture2.md").write_text(CONTROL, encoding="utf-8")
    (root / "Control" / "copy_of_lecture2.md").write_text(CONTROL, encoding="utf-8")
    (root / "Control" / "cooking.txt").write_text(COOKING, encoding="utf-8")
    summary = library.index([root])
    yield {"root": root, "library": library, "summary": summary}
    shutil.rmtree(root, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(KB_TMP, ignore_errors=True)
    shutil.rmtree(_DATA, ignore_errors=True)
