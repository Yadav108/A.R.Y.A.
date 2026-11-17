"""K3 at the bench: the assembly order and steps, the bring-up checklist and readings, the camera helpers.
A fake kicad-cli position file for most tests; one real export from KiCad's own demo board."""
import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest

from arya.eda import bench as B
from arya.eda import kicad as K
from arya.eda import projects as P

POS = """Ref,Val,Package,PosX,PosY,Rot,Side
"U1","ATtiny85","SOIC-8_5.3x5.3mm_P1.27mm",50.0,-30.0,0,top
"R1","10k","R_0603_1608Metric",42.0,-28.0,0,top
"R2","10k","R_0603_1608Metric",44.0,-28.0,0,top
"R3","1k","R_0805_2012Metric",80.0,-60.0,0,top
"C1","100n","C_0603_1608Metric",55.0,-32.0,90,top
"D1","green","LED_0603_1608Metric",85.0,-62.0,0,top
"J1","USB-C","USB_C_Receptacle_GCT_USB4085",10.0,-45.0,0,top
"J2","ISP","PinHeader_2x03_P2.54mm_Vertical",90.0,-20.0,0,top
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "projects_dir", lambda: tmp_path / "kicad")
    monkeypatch.setattr(K, "_new_board", lambda path: path.write_text("(kicad_pcb)", encoding="utf-8"))
    monkeypatch.setattr(P, "_state", {"active": None, "loaded": True})

    def fake_cli(*args):
        if args[:3] == ("pcb", "export", "pos"):
            Path(args[args.index("-o") + 1]).write_text(POS, encoding="utf-8")
    monkeypatch.setattr(K, "_cli", fake_cli)
    p = K.create("tiny blinky")
    p.pcb.write_text('(kicad_pcb (footprint "R_0603"))', encoding="utf-8")
    P.set_active(p)
    return p


def say(text):
    return asyncio.run(B.handle(text))


# ── The assembly plan ─────────────────────────────────────────────────────────

def test_order_groups_and_notes(project):
    steps = B.plan_assembly(B.positions(project))
    order = [s.refs for s in steps]
    assert order[0] == ["C1"] and order[1] == ["R1", "R2"]               # 0603 passives first, same value together
    assert order.index(["D1"]) < order.index(["U1"]) < order.index(["J1"])
    assert order[-2:] in ([["J1"], ["J2"]], [["J2"], ["J1"]])            # connectors last
    d1 = next(s for s in steps if s.refs == ["D1"])
    assert "polarity" in d1.note and "Pin 1" in next(s for s in steps if s.refs == ["U1"]).note
    assert next(s for s in steps if s.refs == ["C1"]).where == "top centre, near U1"


def test_assembly_walkthrough_and_resume(project):
    first = say("guide me through assembly")
    assert first.startswith("8 parts in 7 steps, smallest first") and "Step 1 of 7: C1, 100n (0603)" in first
    assert B.matches("next") and say("next").startswith("Step 2 of 7: R1, R2: 10k (0603)")
    assert say("back").startswith("Step 1 of 7")
    assert say("repeat that").startswith("Step 1 of 7")
    say("done")
    assert "U1" in say("what's left?")
    assert say("where does D1 go?").startswith("D1 (green) goes bottom right")
    assert say("pause assembly").startswith("Paused")
    assert not B.matches("next")                                          # no session: "next" is not ours
    assert say("continue assembly").startswith("Step 2 of 7")             # picked up where it was
    for _ in range(5):
        say("next")
    assert say("next").startswith("That's every part")
    assert "assembled the board (8 parts)" in project.design.read_text(encoding="utf-8")


def test_session_expires(project, monkeypatch):
    say("guide me through assembly")
    state = B.load_state(project)
    state["touched"] = time.time() - 31 * 60
    B._state_path(project).write_text(json.dumps(state), encoding="utf-8")
    assert not B.matches("next")


def test_board_without_parts(project):
    project.pcb.write_text("(kicad_pcb)", encoding="utf-8")
    assert "press F8" in say("guide me through assembly")


# ── Bring-up ──────────────────────────────────────────────────────────────────

def _circuit(project):
    from tests.test_kicad_design import USB_LED                            # VBUS, +3V3, GND
    (project.folder / "circuit.json").write_text(json.dumps(USB_LED), encoding="utf-8")


@pytest.mark.parametrize("text, value, unit", [
    ("3.28 volts", 3.28, "V"), ("it reads 4.9", 4.9, ""), ("I measured 3,31 V", 3.31, "V"), ("zero", 0.0, ""),
    ("short", 0.0, "short"), ("it beeps", 0.0, "short"), ("open", float("inf"), "ohm"), ("OL", float("inf"), "ohm"),
    ("4.7k", 4700.0, "ohm"), ("120 ohms", 120.0, "ohm"), ("850 mV", 0.85, "V"),
])
def test_parse_reading(text, value, unit):
    assert B.parse_reading(text) == (value, unit)


def test_expected_volts():
    assert B.expected_volts("+3V3") == 3.3 and B.expected_volts("+5V") == 5.0 and B.expected_volts("VBUS") == 5.0
    assert B.expected_volts("+1V8") == 1.8 and B.expected_volts("GND") is None


def test_bringup_flow(project):
    _circuit(project)
    first = say("start bring-up")
    assert "Check 1 of 4: resistance from VBUS to GND" in first and "Power off" in first
    assert say("open").startswith("Open, no short on VBUS. Check 2 of 4: resistance from +3V3")
    third = say("4.7k")
    assert third.startswith("4700 ohms, no short on +3V3. Check 3 of 4: VBUS to GND, expect 5 volts.")
    assert "Now plug in the power" in third
    state = B.load_state(project)
    assert state["bringup"]["step"] == 2
    assert say("5.02 volts").startswith("5.02 volts on VBUS, in range. Check 4 of 4: +3V3")
    low = say("2.1 volts")
    assert low.startswith("2.1 volts on +3V3, low (expect 3.3)") and "continue bring-up" in low
    assert "FAIL" in project.design.read_text(encoding="utf-8")
    say("continue bring-up")
    assert say("3.29 V").startswith("3.29 volts on +3V3, in range. All supplies check out")
    assert not B.matches("3.3 volts")                                     # finished: numbers are no longer ours


def test_short_and_dead_rail(project):
    _circuit(project)
    say("start bring-up")
    assert say("short").startswith("VBUS looks shorted to GND. Don't power it.")
    assert B.judge(B.Check("volts", "+3V3", 3.3), 0.0, "V")[1].startswith("Nothing on +3V3")
    assert "too high" in B.judge(B.Check("volts", "+3V3", 3.3), 5.0, "V")[1]


def test_skip(project):
    _circuit(project)
    say("start bring-up")
    assert say("skip").startswith("Skipped. Check 2 of 4")


def test_bringup_without_circuit(project):
    assert "don't know this board's supplies" in say("start bring-up")


# ── The camera ────────────────────────────────────────────────────────────────

def test_read_meter(project, monkeypatch):
    from PIL import Image

    from arya.tools import screen, webcam
    _circuit(project)
    monkeypatch.setattr(webcam, "grab", lambda: Image.new("RGB", (8, 8)))
    monkeypatch.setattr(screen, "ocr_image", lambda img: "")
    assert "OCR struggles with LCD digits" in say("read the meter")
    say("start bring-up")
    monkeypatch.setattr(screen, "ocr_image", lambda img: "DC 5.01 V")
    say("open")
    say("open")
    assert say("read the meter").startswith("5.01 volts on VBUS, in range")


def test_check_soldering_needs_the_project_yes(project, monkeypatch):
    from arya.core import router
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    assert "needs the cloud vision model" in say("check my soldering")
    assert "📷" in asyncio.run(B.handle("check my soldering", remote=True))


@pytest.mark.parametrize("text", ["next", "done", "3.3 volts", "skip", "back"])
def test_bare_words_are_not_ours_without_a_session(project, text):
    assert not B.matches(text)


@pytest.mark.parametrize("text", ["guide me through assembly", "start bring-up", "where does R3 go?",
                                  "check my soldering", "read the meter", "let's solder the board"])
def test_matches(text):
    assert B.matches(text)


# ── The real KiCad: its own demo board ────────────────────────────────────────

@pytest.mark.skipif(not K.available(), reason="KiCad not installed")
def test_real_positions_from_a_kicad_demo(tmp_path, monkeypatch):
    demo = K.kicad_bin().parent / "share" / "kicad" / "demos" / "pic_programmer"
    folder = tmp_path / "pic"
    shutil.copytree(demo, folder)
    p = K.Project("pic_programmer", folder)
    board = B.positions(p)
    assert len(board) > 40 and all(b.side in ("top", "bottom") for b in board)
    steps = B.plan_assembly(board)
    assert sum(len(s.refs) for s in steps) == len(board)
    assert all(s.where for s in steps)
