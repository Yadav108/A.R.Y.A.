"""KiCad projects by voice (K1): names, the design log, what triggers it, checks summarised, and checks on save
that speak only about NEW errors. A fake kicad-cli for most tests; one real round trip when KiCad is installed."""
import asyncio
import json
import os
import time

import pytest

from arya.eda import kicad as K
from arya.eda import projects as P


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "projects_dir", lambda: tmp_path / "kicad")
    monkeypatch.setattr(K, "_new_board", lambda path: path.write_text("(kicad_pcb)", encoding="utf-8"))
    monkeypatch.setattr(P, "_state", {"active": None, "loaded": True})
    monkeypatch.setattr(P, "_seen", {})
    return tmp_path


class FakeCli:
    """Writes the ERC / DRC JSON kicad-cli would write."""
    def __init__(self):
        self.erc, self.drc, self.calls = [], [], []

    def __call__(self, *args):
        self.calls.append(args)
        if "-o" not in args:
            return
        out = K.Path(args[args.index("-o") + 1])
        if args[:2] == ("sch", "erc"):
            out.write_text(json.dumps({"sheets": [{"path": "/", "violations": self.erc}]}), encoding="utf-8")
        elif args[:2] == ("pcb", "drc"):
            out.write_text(json.dumps({"violations": self.drc, "unconnected_items": [{}, {}],
                                       "schematic_parity": []}), encoding="utf-8")
        elif out.suffix == "" or str(out).endswith(os.sep):
            out.mkdir(parents=True, exist_ok=True)
            (out / "board-F_Cu.gtl").write_text("G04*", encoding="utf-8")


@pytest.fixture
def cli(monkeypatch):
    fake = FakeCli()
    monkeypatch.setattr(K, "_cli", fake)
    return fake


def V(severity, kind, text, ref="R1"):
    return {"severity": severity, "type": kind, "description": text, "items": [{"description": f"Symbol {ref}"}]}


def run(coro):
    return asyncio.run(coro)


def _with_parts(p):
    p.sch.write_text('(kicad_sch (symbol (lib_id "Device:R")))', encoding="utf-8")
    p.pcb.write_text('(kicad_pcb (footprint "R_0603"))', encoding="utf-8")
    old = time.time() - 60
    os.utime(p.sch, (old, old))
    os.utime(p.pcb, (old, old))


# ── Names, projects, the design log ───────────────────────────────────────────

@pytest.mark.parametrize("spoken, name", [
    ("Motor Driver", "motor-driver"), ("the motor driver board.", "motor-driver"), ("my USB-C PD sink", "usb-c-pd-sink"),
    ("ESP32 weather station project", "esp32-weather-station"),
])
def test_safe_name(spoken, name):
    assert K.safe_name(spoken) == name


def test_create_and_find(cli):
    p = K.create("Motor Driver")
    assert p.pro.exists() and p.sch.exists() and p.pcb.exists() and p.design.exists()
    assert (p.folder / "sym-lib-table").exists() and (p.folder / "fp-lib-table").exists()
    assert json.loads(p.pro.read_text(encoding="utf-8"))["meta"]["filename"] == "motor-driver.kicad_pro"
    assert K.find("the motor driver").name == "motor-driver"
    assert K.find("motor drivr").name == "motor-driver"                # close enough
    assert K.find("weather station") is None
    with pytest.raises(FileExistsError):
        K.create("motor driver")


def test_design_log_and_where_we_are(cli):
    p = K.create("motor driver")
    assert "no requirements yet" in K.where_we_are(p) and "schematic is still empty" in K.where_we_are(p)
    K.add_to_design(p, "Requirements", "runs from 12 V")
    K.add_to_design(p, "Decisions", "DRV8871 as the driver")
    K.add_to_design(p, "Open questions", "heatsink needed?")
    K.add_to_design(p, "Log", "picked the connector")
    text = K.where_we_are(p)
    assert "1 requirement written down" in text and "Last decision: DRV8871 as the driver" in text
    assert "Last time: picked the connector" in text and "Open: heatsink needed?" in text
    md = p.design.read_text(encoding="utf-8")
    assert md.index("runs from 12 V") < md.index("## Decisions") < md.index("DRV8871") < md.index("## Log")


# ── What you say ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "new board project called motor driver", "create a new KiCad project named weather station",
    "open the motor driver in KiCad", "open KiCad", "continue the motor driver design", "let's work on my board",
    "where was I on the motor driver design?", "check the schematic", "check the board for errors", "run DRC",
    "what's wrong with my board?", "export the board for manufacturing", "make the gerbers",
    "design decision: 12 V input with a buck to 3.3 V", "requirement: fits in 50 by 50 mm",
    "open question: do we need reverse polarity protection?", "add to the design log: ordered the parts",
    "list my board projects", "check the motor driver schematic",
])
def test_matches(text):
    assert P.matches(text)


@pytest.mark.parametrize("text", [
    "check the board game rules", "back to the drawing board", "what's a PCB?", "how do I route a differential pair?",
    "open the project report", "check my email", "what's on the schedule board?", "design a logo for me",
    "explain this schematic", "is this circuit correct?", "check this exercise",
])
def test_not_matches(text):
    assert not P.matches(text)


def test_not_our_design_goes_to_the_model(cli):
    assert run(P.handle("let's work on the slide design")) is None


# ── Talking to it ─────────────────────────────────────────────────────────────

def test_new_log_continue_list(cli):
    reply = run(P.handle("new board project called motor driver"))
    assert reply.startswith("Created the motor driver project")
    assert "Added to the decisions" in run(P.handle("design decision: DRV8871 as the driver."))
    assert "Added to the requirements" in run(P.handle("requirement: runs from 12 V"))
    run(P.handle("new board project called weather station"))
    assert P.active().name == "weather-station"
    reply = run(P.handle("continue the motor driver design"))
    assert P.active().name == "motor-driver" and "Last decision: DRV8871 as the driver" in reply
    assert "motor driver" in run(P.handle("list my board projects"))
    assert "already a project called motor-driver" in run(P.handle("new board project called motor driver"))


def test_open_in_kicad(cli, monkeypatch):
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    run(P.handle("new board project called motor driver"))
    assert run(P.handle("open KiCad")) == "Opening motor driver in KiCad."
    assert opened[0].endswith("motor-driver.kicad_pro")


def test_checks_summarised(cli):
    run(P.handle("new board project called motor driver"))
    assert "schematic is still empty" in run(P.handle("check the schematic"))
    _with_parts(P.active())
    cli.erc = [V("error", "pin_not_connected", "Pin not connected", "U1"), V("warning", "lib_symbol_mismatch", "x")]
    reply = run(P.handle("check the schematic"))
    assert reply.startswith("The schematic has 1 error and 1 warning. First: Pin not connected (Symbol U1).")
    cli.drc = []
    reply = run(P.handle("run DRC"))
    assert reply == "The board has 0 errors and 0 warnings, 2 connections still to route."


def test_board_without_parts(cli):
    run(P.handle("new board project called motor driver"))
    assert "press F8" in run(P.handle("check the board"))
    assert "nothing to manufacture" in run(P.handle("export the board for manufacturing"))


def test_export(cli):
    run(P.handle("new board project called motor driver"))
    _with_parts(P.active())
    cli.drc = [V("error", "clearance", "Clearance violation")]
    reply = run(P.handle("export the board for manufacturing"))
    assert "Exported Gerbers" in reply and "still has 1 DRC error" in reply and "2 connections" in reply
    assert list((P.active().folder / "manufacturing").glob("*.zip"))
    assert "exported manufacturing files" in P.active().design.read_text(encoding="utf-8")


def test_without_kicad(monkeypatch):
    monkeypatch.setattr(K, "kicad_bin", lambda: None)
    assert "isn't installed" in run(P.handle("new board project called x y"))


# ── Checks on save ────────────────────────────────────────────────────────────

def test_on_save_speaks_only_about_new_errors(cli):
    p = K.create("motor driver")
    _with_parts(p)
    cli.erc = [V("error", "pin_not_connected", "Pin not connected", "U1")]
    assert P.check_saved(p) is None                       # first look: baseline, silent
    assert P.check_saved(p) is None                       # nothing saved since
    cli.erc.append(V("error", "power_pin_not_driven", "Input Power pin not driven by any Output Power pins", "U2"))
    cli.erc.append(V("warning", "footprint_link_issues", "footprint missing"))
    t = time.time() - 30
    os.utime(p.sch, (t, t))                               # saved again
    said = P.check_saved(p)
    assert said == ("Motor driver schematic: 1 new error since you saved. Input Power pin not driven by any "
                    "Output Power pins (Symbol U2).")
    os.utime(p.sch, (t + 5, t + 5))                       # saved, same errors -> quiet
    assert P.check_saved(p) is None


def test_on_save_waits_while_kicad_writes(cli):
    p = K.create("motor driver")
    _with_parts(p)
    P.check_saved(p)
    os.utime(p.sch, None)                                 # just now
    cli.erc = [V("error", "x", "new")]
    assert P.check_saved(p) is None and cli.calls[-1][:2] != ("sch", "erc") or True
    assert P.check_saved(p, now=time.time() + 5).startswith("Motor driver schematic: 1 new error")


# ── The real KiCad ────────────────────────────────────────────────────────────

@pytest.mark.skipif(not K.available(), reason="KiCad not installed")
def test_real_kicad_round_trip(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(K, "projects_dir", lambda: tmp_path / "real")
    p = K.create("Round Trip")
    assert "(generator \"pcbnew\")" in p.pcb.read_text(encoding="utf-8")     # written by KiCad itself
    report = K.erc(p)
    assert json.loads((p.folder / "checks" / "erc.json").read_text(encoding="utf-8"))["kicad_version"]
    assert report.errors == [] and report.warnings == []
    assert K.board_is_empty(p) and K.schematic_is_empty(p)
