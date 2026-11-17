"""K2: KiCad's libraries as a parts catalogue, the circuit format and its checks, the generated schematic (checked
by KiCad's own ERC and netlist), and the design agent driven by a scripted fake model — no cloud in tests."""
import asyncio
import json
import re
import types

import pytest

from arya.eda import kicad as K

pytestmark = pytest.mark.skipif(not K.available(), reason="KiCad not installed")

from arya.eda import circuit as C  # noqa: E402
from arya.eda import designer as D  # noqa: E402
from arya.eda import projects as P  # noqa: E402
from arya.eda import schematic as G  # noqa: E402
from arya.eda import symbols as S  # noqa: E402

USB_LED = {"blocks": [
    {"name": "Power", "parts": [
        {"ref": "J1", "symbol": "Connector:USB_C_Receptacle_PowerOnly_6P", "value": "USB-C",
         "footprint": "Connector_USB:USB_C_Receptacle_GCT_USB4085",
         "pins": {"VBUS": "VBUS", "GND": "GND", "CC1": "CC1", "CC2": "CC2", "SHIELD": "GND"}},
        {"ref": "R1", "symbol": "Device:R", "value": "5.1k", "footprint": "Resistor_SMD:R_0603_1608Metric",
         "pins": {"1": "CC1", "2": "GND"}},
        {"ref": "R2", "symbol": "Device:R", "value": "5.1k", "footprint": "Resistor_SMD:R_0603_1608Metric",
         "pins": {"1": "CC2", "2": "GND"}},
        {"ref": "U1", "symbol": "Regulator_Linear:AMS1117-3.3", "value": "AMS1117-3.3",
         "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2", "pins": {"VI": "VBUS", "GND": "GND", "VO": "+3V3"},
         "why": "cheap 1 A LDO"},
        {"ref": "C1", "symbol": "Device:C", "value": "10u", "footprint": "Capacitor_SMD:C_0805_2012Metric",
         "pins": {"1": "VBUS", "2": "GND"}},
        {"ref": "C2", "symbol": "Device:C", "value": "10u", "footprint": "Capacitor_SMD:C_0805_2012Metric",
         "pins": {"1": "+3V3", "2": "GND"}}]},
    {"name": "Indicator", "parts": [
        {"ref": "R3", "symbol": "Device:R", "value": "1k", "footprint": "Resistor_SMD:R_0603_1608Metric",
         "pins": {"1": "+3V3", "2": "LED_A"}},
        {"ref": "D1", "symbol": "Device:LED", "value": "green", "footprint": "LED_SMD:LED_0603_1608Metric",
         "pins": {"A": "LED_A", "K": "GND"}}]}]}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "projects_dir", lambda: tmp_path / "kicad")
    monkeypatch.setattr(P, "_state", {"active": None, "loaded": True})
    return K.create("usb led")


def run(coro):
    return asyncio.run(coro)


# ── The catalogue ─────────────────────────────────────────────────────────────

def test_index_and_search():
    assert len(S.index()) > 15000
    assert S.search("STM32F103C8", 1)[0]["name"] == "STM32F103C8Tx"
    assert "Connector:USB_C_Receptacle" in [s["lib"] + ":" + s["name"] for s in S.search("USB C receptacle", 4)]


def test_derived_symbol_is_flattened():
    s = S.load("Regulator_Linear", "AMS1117-3.3")                # extends AP1117-15
    assert {(p.number, p.name, p.etype) for p in s.pins} == {("3", "VI", "power_in"), ("1", "GND", "power_in"),
                                                              ("2", "VO", "power_out")}
    assert s.sexpr[1] == '"Regulator_Linear:AMS1117-3.3"' and "extends" not in S.dump(s.sexpr)
    assert all("AP1117-15" not in S.unq(sub[1]) for sub in s.sexpr if isinstance(sub, list) and sub[0] == "symbol")


def test_shared_pin_names_connect_every_pin():
    s = S.load("Connector", "USB_C_Receptacle_PowerOnly_6P")
    assert sorted(p.number for p in s.pins_for("VBUS")) == ["A9", "B9"]
    assert [p.number for p in s.pins_for("A9")] == ["A9"]


def test_footprint_suggestions():
    assert S.suggest_footprints(S.load("Device", "R"))[0] == "Resistor_SMD:R_0603_1608Metric"
    assert S.suggest_footprints(S.load("Device", "LED"))[0] == "LED_SMD:LED_0603_1608Metric"
    assert S.suggest_footprints(None, "Connector_PinHeader_2.54mm:PinHeader_2x3_P2.54mm_Vertical") == \
        ["Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical"]
    assert S.suggest_footprints(S.load("Switch", "SW_Push"))[0].startswith("Button_Switch_SMD:SW_Push")


# ── The circuit and its checks ────────────────────────────────────────────────

def test_good_circuit_validates_and_round_trips(tmp_path):
    c = C.Circuit.from_dict(USB_LED)
    assert C.validate(c) == []
    c.save(tmp_path / "c.json")
    assert C.Circuit.load(tmp_path / "c.json").to_dict() == C.Circuit.from_dict(USB_LED).to_dict()


def _broken(**changes):
    data = json.loads(json.dumps(USB_LED))
    part = data["blocks"][0]["parts"][3]                          # U1
    part.update(changes)
    return C.Circuit.from_dict(data)


@pytest.mark.parametrize("change, text", [
    ({"symbol": "Regulator_Linear:AMS9999"}, "no symbol Regulator_Linear:AMS9999"),
    ({"footprint": "Package_TO_SOT_SMD:SOT-999"}, "no footprint Package_TO_SOT_SMD:SOT-999"),
    ({"pins": {"VI": "VBUS", "GND": "GND", "OUT": "+3V3"}}, "has no pin 'OUT'"),
    ({"pins": {"VI": "VBUS", "VO": "+3V3"}}, "power input and isn't connected"),
    ({"ref": "R1"}, "used twice"),
])
def test_validation_errors(change, text):
    errs = [str(e) for e in C.errors(C.validate(_broken(**change)))]
    assert any(text in e for e in errs), errs


def test_lonely_net_and_missing_decoupling():
    data = json.loads(json.dumps(USB_LED))
    data["blocks"][0]["parts"] = [p for p in data["blocks"][0]["parts"] if p["ref"] != "C2"]
    data["blocks"][1]["parts"][1]["pins"]["A"] = "LED_X"          # LED_A and LED_X now have one pin each
    probs = [str(p) for p in C.validate(C.Circuit.from_dict(data))]
    assert any("net LED_A has only one pin" in p for p in probs)
    assert any("U1: no decoupling capacitor from +3V3" not in p for p in probs)


def test_repair_fixes_mechanical_slips():
    c = C.Circuit.from_dict({"blocks": [{"name": "X", "parts": [
        {"ref": "SW1", "symbol": "Device:SW_PUSH", "value": "", "footprint": "Switch:SW_Push", "pins": {"1": "A", "2": "GND"}},
        {"ref": "J1", "symbol": "Connector:AVR-ISP-6", "value": "ISP",
         "footprint": "Connector_PinHeader_2.54mm:PinHeader_2x3_P2.54mm_Vertical", "pins": {}}]}]})
    fixes = C.repair(c)
    assert c.parts[0].symbol == "Switch:SW_Push" and S.footprint_exists(c.parts[0].footprint)
    assert c.parts[1].footprint == "Connector_PinHeader_2.54mm:PinHeader_2x03_P2.54mm_Vertical"
    assert len(fixes) == 3 and "please check" in fixes[1]


# ── The schematic, checked by KiCad itself ────────────────────────────────────

def _netlist(p) -> dict[str, list]:
    out = p.folder / "t.net"
    K._cli("sch", "export", "netlist", "-o", str(out), str(p.sch))
    nets = {}
    for b in re.split(r'\(net \(code "\d+"\) ', out.read_text(encoding="utf-8"))[1:]:
        name = re.match(r'\(name "([^"]+)"\)', b).group(1).split("/")[-1]
        nets[name] = sorted(re.findall(r'\(node \(ref "([^"]+)"\) \(pin "([^"]+)"\)', b))
    return nets


def test_generated_schematic_is_erc_clean_and_exact(project):
    c = C.Circuit.from_dict(USB_LED)
    result = G.generate(project, c)
    assert result.written == ["Power", "Indicator"] and result.kept == []
    report = K.erc(project)
    assert report.errors == [] and report.warnings == []
    want: dict[str, list] = {}
    for ref, nets in C.resolved(c).items():
        for pin, net in nets.items():
            want.setdefault(net, []).append((ref, pin))
    got = _netlist(project)
    assert {k: sorted(v) for k, v in want.items()} == {k: v for k, v in got.items() if not k.startswith("unconnected")}


def test_your_edited_sheet_is_never_overwritten(project):
    c = C.Circuit.from_dict(USB_LED)
    G.generate(project, c)
    sheet = project.folder / "power.kicad_sch"
    sheet.write_text(sheet.read_text(encoding="utf-8").replace("Generated by ARYA", "Edited by me"), encoding="utf-8")
    result = G.generate(project, c)
    assert result.kept == ["Power"] and "Edited by me" in sheet.read_text(encoding="utf-8")
    assert (project.folder / "power.arya.kicad_sch").exists() and result.written == ["Indicator"]


# ── The design agent, with a scripted model ───────────────────────────────────

def _call(name, args, i=[0]):
    i[0] += 1
    return types.SimpleNamespace(content="", tool_calls=[types.SimpleNamespace(id=f"c{i[0]}", name=name,
                                                                              arguments=json.dumps(args))])


def _scripted(*turns):
    seen = []

    async def complete(model, messages, **kw):
        seen.append(messages)
        turn = turns[len(seen) - 1]
        return turn(messages) if callable(turn) else turn
    return complete, seen


PLAN = {"parts": [{"role": "3.3 V regulator", "block": "Power", "query": "AMS1117-3.3"},
                  {"role": "USB-C power input", "block": "Power", "query": "USB C receptacle power only"}],
        "approach": "USB-C 5 V into an LDO; an LED shows 3.3 V."}


def test_plan_catalogue_wire_fix(project):
    bad = json.loads(json.dumps(USB_LED))
    bad["blocks"][0]["parts"][3]["pins"] = {"VI": "VBUS", "GND": "GND", "OUT": "+3V3"}
    complete, seen = _scripted(
        _call("plan_parts", PLAN),
        types.SimpleNamespace(content="We have parts.", tool_calls=[]),          # a Groq text glitch: retried
        _call("submit_circuit", {**bad, "decisions": ["AMS1117"], "open_questions": []}),
        _call("submit_circuit", {**USB_LED, "decisions": ["AMS1117 for 3.3 V"], "open_questions": ["Heatsink?"]}))
    result = run(D.design(project, "", complete=complete))
    assert len(result["circuit"].parts) == 8 and result["decisions"] == ["AMS1117 for 3.3 V"]
    assert result["steps"] == 3                                               # plan + 2 drafts
    wire = seen[1][1]["content"]
    assert "Catalogue (symbols, footprints, pins)" in wire
    assert "Regulator_Linear:AMS1117-3.3" in wire and "  3 VI power_in" in wire          # real pins, from KiCad
    assert "Device:R" in wire and "Resistor_SMD:R_0603_1608Metric" in wire              # everyday parts
    retry = seen[3][1]["content"]
    assert "NOT accepted" in retry and "has no pin 'OUT'" in retry
    assert all(len(json.dumps(m)) / 3.5 < 7000 for m in seen)              # under Groq's 8000 tokens per request


def test_requests_stay_under_groqs_limit(project):
    """Groq's free tier: 8000 tokens per minute AND per request. ~3.5 characters per token."""
    text, chosen = D.catalogue(PLAN["parts"] + [{"role": "MCU", "block": "MCU", "query": "STM32F103C8"}])
    assert "MCU_ST_STM32F1:STM32F103C8Tx" in chosen
    brief = D._brief(project, "a USB-C powered STM32 board with an LED")
    prompt = brief + D.WIRE_RULES + text + json.dumps(D.SUBMIT_TOOL) + D.ENGINEER
    assert len(prompt) / 3.5 < 7000


def test_gives_up_honestly(project):
    complete, _ = _scripted(*[types.SimpleNamespace(content="hmm", tool_calls=[])] * 5)
    with pytest.raises(D.DesignFailed, match="didn't answer with plan_parts"):
        run(D.design(project, "", complete=complete))


def test_apply_writes_everything(project):
    result = {"circuit": C.Circuit.from_dict(USB_LED), "problems": [], "steps": 5, "fixes": ["J1: footprint x -> y (please check)"],
              "decisions": ["AMS1117 for 3.3 V"], "open_questions": ["Heatsink needed?"]}
    said = D.apply(project, result)
    assert said.startswith("The usb led draft is ready: 8 parts in 2 blocks (Power, Indicator).")
    assert "no errors, no warnings" in said and "Open question: Heatsink needed?" in said
    md = project.design.read_text(encoding="utf-8")
    assert "AMS1117 for 3.3 V" in md and "ARYA's fix — J1" in md and "ARYA drafted the circuit: 8 parts" in md
    assert (project.folder / "circuit.json").exists()


def test_retry_after():
    assert D._retry_after(Exception("Please try again in 12.5s.")) == 13.5
    assert D._retry_after(Exception("try again in 1m3.2s")) == 60.0
    assert D._retry_after(Exception("try again in 450ms")) == 1.45


# ── Asking once per project ───────────────────────────────────────────────────

def test_design_asks_once_then_remembers(project, monkeypatch):
    from arya.core import policy, router
    monkeypatch.setattr(router, "cloud_available", lambda: True)
    started = []
    monkeypatch.setattr(P, "start_design", lambda p, request: started.append((p.name, request)) or "Designing.")
    P.set_active(project)
    assert "Tell me what the usb led board must do first" in run(P.handle("design the circuit"))
    q = run(P.handle("design the circuit for a USB-C LED board"))
    assert "Use the cloud for the usb led project?" in q
    pending = policy.take_pending()
    assert pending.tool == "eda_cloud" and pending.args == {"project": "usb-led", "request": "a USB-C LED board"}
    assert P.answer_consent(pending.args, False).startswith("Okay, nothing goes to the cloud")
    assert not D.cloud_ok(project)
    assert P.answer_consent(pending.args, True) == "Designing."
    assert D.cloud_ok(project) and started == [("usb-led", "a USB-C LED board")]
    assert run(P.handle("draft the schematic")) == "Designing."                  # no second question
    assert "a USB-C LED board" in project.design.read_text(encoding="utf-8")      # kept as a requirement


def test_generate_and_check_the_circuit_file(project):
    P.set_active(project)
    assert "no circuit file yet" in run(P.handle("generate the schematic"))
    C.Circuit.from_dict(USB_LED).save(project.folder / "circuit.json")
    assert run(P.handle("check the circuit")) == "The usb led circuit file checks out: 8 parts, no problems."
    said = run(P.handle("generate the schematic"))
    assert said.startswith("Redrew the usb led schematic") and "no errors, no warnings" in said


@pytest.mark.parametrize("text", ["design the circuit", "draft the schematic for a USB-C LED board",
                                  "design the board from scratch", "generate the schematic", "check the circuit"])
def test_matches(text):
    assert P.matches(text)


@pytest.mark.parametrize("text", ["draw the circuit for an RC low-pass filter", "design a logo", "what is a schematic?",
                                  "check the circuit breaker in the lab", "explain this circuit"])
def test_not_matches(text):
    assert not P.matches(text)
