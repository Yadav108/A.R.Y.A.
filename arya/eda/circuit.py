"""
A circuit as data (roadmap K2): what the design step writes, what ARYA checks, what the schematic is generated
from. Stored as circuit.json in the project folder, readable and editable by hand:

  {"blocks": [{"name": "Power", "parts": [
      {"ref": "U1", "symbol": "Regulator_Linear:AMS1117-3.3", "value": "AMS1117-3.3",
       "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
       "pins": {"VI": "+5V", "GND": "GND", "VO": "+3V3"},          # pin name or number -> net ("NC": unused)
       "why": "cheap 1 A LDO, 5 V in from USB"}]}]}

validate() checks it against KiCad's real libraries before anything is drawn: the symbol, every pin and the
footprint exist; every pin of an IC is connected or marked NC; every net joins at least two pins; power inputs
are connected; each IC supply has a decoupling capacitor. Errors stop generation; warnings are reported.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from arya.eda import symbols as S

NC = "NC"


@dataclass
class Part:
    ref: str
    symbol: str
    value: str
    footprint: str
    pins: dict[str, str]
    why: str = ""
    block: str = ""

    @property
    def lib(self) -> str:
        return self.symbol.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.symbol.split(":", 1)[1] if ":" in self.symbol else self.symbol


@dataclass
class Circuit:
    blocks: dict[str, list[Part]] = field(default_factory=dict)

    @property
    def parts(self) -> list[Part]:
        return [p for ps in self.blocks.values() for p in ps]

    @classmethod
    def from_dict(cls, data: dict) -> "Circuit":
        c = cls()
        for b in data.get("blocks", []):
            name = str(b.get("name") or "Main").strip() or "Main"
            c.blocks[name] = [Part(ref=str(p.get("ref", "")).strip(), symbol=str(p.get("symbol", "")).strip(),
                                   value=str(p.get("value", "")).strip(), footprint=str(p.get("footprint", "")).strip(),
                                   pins={str(k): str(v).strip() for k, v in (p.get("pins") or {}).items()},
                                   why=str(p.get("why", "")), block=name)
                              for p in b.get("parts", []) if isinstance(p, dict)]
        return c

    def to_dict(self) -> dict:
        return {"blocks": [{"name": name, "parts": [
            {"ref": p.ref, "symbol": p.symbol, "value": p.value, "footprint": p.footprint, "pins": p.pins,
             **({"why": p.why} if p.why else {})} for p in parts]} for name, parts in self.blocks.items()]}

    @classmethod
    def load(cls, path: Path) -> "Circuit":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass
class Problem:
    level: str                    # error / warning
    ref: str
    text: str

    def __str__(self) -> str:
        return f"{self.ref}: {self.text}" if self.ref else self.text


def power_names() -> set[str]:
    """Net names KiCad has a power symbol for (GND, +3V3, +5V, VCC …)."""
    return {s["name"] for s in S.index() if s["lib"] == "power" and not s["name"].startswith("PWR_FLAG")}


def is_power_net(net: str) -> bool:
    return net in power_names()


def resolved(c: Circuit) -> dict[str, dict[str, str]]:
    """ref -> {pin number: net}, for the parts whose symbol loads (names turned into numbers)."""
    out = {}
    for p in c.parts:
        try:
            sym = S.load(p.lib, p.name)
        except (KeyError, RuntimeError):
            continue
        nets = {}
        for key, net in p.pins.items():
            for pin in sym.pins_for(key):
                nets[pin.number] = net
        out[p.ref] = nets
    return out


def validate(c: Circuit) -> list[Problem]:
    probs: list[Problem] = []
    seen_refs: set[str] = set()
    net_pins: dict[str, list[str]] = {}
    supplies: dict[str, set[str]] = {}            # IC ref -> its supply nets
    caps: list[set[str]] = []                     # nets each capacitor joins
    for p in c.parts:
        if not re.match(r"^[A-Z]+[0-9]+$", p.ref):
            probs.append(Problem("error", p.ref or "?", "reference must look like R1, U2, C10"))
        if p.ref in seen_refs:
            probs.append(Problem("error", p.ref, "used twice"))
        seen_refs.add(p.ref)
        if ":" not in p.symbol:
            probs.append(Problem("error", p.ref, f"symbol {p.symbol!r} needs the form Library:Name"))
            continue
        try:
            sym = S.load(p.lib, p.name)
        except (KeyError, RuntimeError):
            near = [s["lib"] + ":" + s["name"] for s in S.search(p.name.replace("_", " "), 3)]
            probs.append(Problem("error", p.ref, f"no symbol {p.symbol} in KiCad's libraries"
                                 + (f" (close: {', '.join(near)})" if near else "")))
            continue
        if not p.footprint:
            probs.append(Problem("error", p.ref, "no footprint"
                                 + (f" (the symbol suggests {sym.props.get('Footprint')})" if sym.props.get("Footprint") else "")))
        elif not S.footprint_exists(p.footprint):
            near = S.suggest_footprints(sym, p.footprint)
            probs.append(Problem("error", p.ref, f"no footprint {p.footprint} in KiCad's libraries"
                                 + (f" (real ones: {', '.join(near)})" if near else "")))
        if not p.value:
            probs.append(Problem("warning", p.ref, "no value"))
        used: set[str] = set()
        for key, net in p.pins.items():
            found = sym.pins_for(key)
            if not found:
                names = ", ".join(f"{x.number} {x.name}" if x.name not in ("~", "") else x.number for x in sym.pins[:24])
                probs.append(Problem("error", p.ref, f"{sym.name} has no pin {key!r} (pins: {names})"))
                continue
            if not net:
                probs.append(Problem("error", p.ref, f"pin {key} has an empty net name"))
                continue
            for pin in found:
                if pin.number in used:
                    probs.append(Problem("error", p.ref, f"pin {pin.number} assigned twice"))
                used.add(pin.number)
                if net != NC:
                    net_pins.setdefault(net, []).append(f"{p.ref}.{pin.number}")
                    if pin.etype == "power_in" and not re.search(r"gnd|vss|ground", net, re.IGNORECASE):
                        supplies.setdefault(p.ref, set()).add(net)
        numbers = {x.number for x in sym.pins}
        for num in sorted(numbers - used, key=lambda n: (len(n), n)):
            pin = next(x for x in sym.pins if x.number == num)
            if pin.etype == "no_connect":
                continue
            what = f"pin {num}" + (f" ({pin.name})" if pin.name not in ("~", "") else "")
            if pin.etype == "power_in":
                probs.append(Problem("error", p.ref, f"{what} is a power input and isn't connected"))
            else:
                probs.append(Problem("warning", p.ref, f"{what} isn't connected (give it a net, or \"NC\")"))
        if p.symbol.startswith("Device:C"):
            caps.append({n for n in p.pins.values() if n != NC})
    for net, pins in net_pins.items():
        if len(pins) < 2 and not is_power_net(net):
            probs.append(Problem("error", "", f"net {net} has only one pin ({pins[0]})"))
    for ref, nets in supplies.items():
        if not ref.startswith("U"):
            continue
        for net in sorted(nets):
            if not any(net in cs and any(re.search(r"gnd|vss", n, re.IGNORECASE) for n in cs) for cs in caps):
                probs.append(Problem("warning", ref, f"no decoupling capacitor from {net} to GND"))
    return probs


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def repair(c: Circuit) -> list[str]:
    """Fix the mechanical slips a model makes, before validating (each fix is reported, for design.md):
    a symbol named in the wrong case or library (Device:SW_PUSH -> Switch:SW_Push), and a footprint that doesn't
    exist (PinHeader_2x3 -> PinHeader_2x03; a symbol name given as a footprint -> the best real match)."""
    fixes = []
    for p in c.parts:
        try:
            S.load(p.lib, p.name)
        except (KeyError, RuntimeError):
            same = [s for s in S.index() if _norm(s["name"]) == _norm(p.name)]
            same.sort(key=lambda s: s["lib"] != p.lib)
            if same:
                new = f"{same[0]['lib']}:{same[0]['name']}"
                fixes.append(f"{p.ref}: symbol {p.symbol} -> {new}")
                p.symbol = new
        try:
            sym = S.load(p.lib, p.name)
        except (KeyError, RuntimeError):
            continue
        if not p.footprint or not S.footprint_exists(p.footprint):
            options = S.suggest_footprints(sym, p.footprint)
            if options:
                fixes.append(f"{p.ref}: footprint {p.footprint or '(none)'} -> {options[0]} (please check)")
                p.footprint = options[0]
    return fixes


def errors(probs: list[Problem]) -> list[Problem]:
    return [p for p in probs if p.level == "error"]
