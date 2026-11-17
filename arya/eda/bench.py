"""
At the bench (roadmap K3): assembling a board and bringing it up, by voice, for the active KiCad project.

ASSEMBLY  "guide me through assembly" / "start assembling the motor driver"
  The board's part positions (kicad-cli pcb export pos) in a soldering order: smallest SMD passives first, then
  diodes/transistors, ICs, bigger parts, through-hole, connectors last — identical values together ("R1, R4, R7:
  10k"). Each step says what, where on the board ("top left, near U1") and what to watch (polarity, pin 1).
  "next" / "done" / "placed"  "back"  "where does R3 go?"  "what's left?"  "stop assembly"
  Progress is saved in the project (bench.json): "continue assembly" picks up where you were.

BRING-UP  "start bring-up" / "bring up the board"
  A checklist from circuit.json's power nets: first resistance checks with the power off (no short from each
  supply to GND), then each supply's voltage with it on (+3V3 -> 3.3 V ±5 %). Say what the meter shows
  ("3.28 volts", "it reads 4.9", "zero", "short"): ARYA compares it, logs PASS/FAIL in design.md and, on a
  failure, says what to check first. "read the meter" tries the webcam with local OCR (LCD digits are hard for it;
  if it can't read them it says so and you say the number).

CHECK MY SOLDERING  "check my soldering" / "check this part"
  One webcam photo of the board; describing solder joints needs the cloud vision model, so it runs only if this
  project already has your yes for the cloud (asked once). The laptop camera is 720p — for close-ups, take a photo
  with the phone (📷) and ask "check this photo".
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from dataclasses import dataclass

from arya.eda import kicad

SESSION_IDLE_S = 30 * 60               # "next" / numbers count as bench answers this long after the last step


# ── Assembly ──────────────────────────────────────────────────────────────────

@dataclass
class Placement:
    ref: str
    value: str
    package: str
    x: float                            # mm, board coordinates (y grows downwards)
    y: float
    rot: float
    side: str                           # top / bottom


def positions(p: kicad.Project) -> list[Placement]:
    out = p.folder / "checks" / "positions.csv"
    out.parent.mkdir(exist_ok=True)
    kicad._cli("pcb", "export", "pos", "--format", "csv", "--units", "mm", "-o", str(out), str(p.pcb))
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    return [Placement(r["Ref"], r["Val"], r["Package"], float(r["PosX"]), -float(r["PosY"]), float(r["Rot"]),
                      r["Side"]) for r in rows if r.get("Ref")]


_SIZE = re.compile(r"_(0201|0402|0603|0805|1206|1210|1812|2010|2512)_")


def _rank(pl: Placement) -> tuple:
    """Soldering order: small flat SMD first, tall and through-hole last (so the iron still reaches)."""
    pkg = pl.package
    letters = re.match(r"[A-Z]+", pl.ref)
    kind = letters.group(0) if letters else ""
    tht = bool(re.search(r"THT|_P\d+\.\d+mm|Axial|Radial|DIP|TO-220|PinHeader|PinSocket|Disc|Horizontal|Vertical", pkg))
    size = _SIZE.search(pkg)
    if kind in ("J", "P", "CN", "X") or "Connector" in pkg or "USB" in pkg:
        group = 6
    elif tht:
        group = 5
    elif size and kind in ("R", "C", "L", "FB") and "LED" not in pkg:      # plain passives; LEDs need polarity care
        group = 0 + ("0201 0402 0603 0805 1206 1210 1812 2010 2512".split().index(size.group(1)) / 10)
    elif kind in ("D", "Q", "LED") or "SOT-23" in pkg or "SOD" in pkg:
        group = 1
    elif kind == "U" or re.search(r"SOIC|QFN|QFP|TSSOP|SSOP|MSOP|DFN|BGA", pkg):
        group = 2
    else:
        group = 3
    return (pl.side != "top", group, kind, pl.value, int(re.sub(r"\D", "", pl.ref) or 0))


@dataclass
class Step:
    refs: list[str]
    value: str
    package: str
    where: str
    note: str


def _polarity(ref: str, package: str) -> str:
    kind = re.match(r"[A-Z]+", ref).group(0) if re.match(r"[A-Z]+", ref) else ""
    if kind == "LED" or kind == "D" or "LED" in package or "SOD" in package:
        return "Mind the polarity: the cathode mark on the part goes to the line or K side of the footprint."
    if "CP_" in package or "Elec" in package or "Tantalum" in package:
        return "Polarised capacitor: the stripe (−) side goes to the minus pad."
    if kind == "U" or re.search(r"SOIC|QFN|QFP|TSSOP|SSOP|DIP", package):
        return "Pin 1 first: line up the dot on the chip with the pin-1 mark on the board, tack one corner, check, then the rest."
    if kind == "Q" or "SOT-23" in package:
        return "Three pins, only one way round: match the part's outline to the footprint."
    return ""


def _where(pl: Placement, board: list[Placement]) -> str:
    xs, ys = [b.x for b in board], [b.y for b in board]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    def third(v, a, b, words):
        if b - a < 1:
            return words[1]
        return words[min(2, int(3 * (v - a) / (b - a + 1e-9)))]
    vert = third(pl.y, y0, y1, ("top", "middle", "bottom"))
    horiz = third(pl.x, x0, x1, ("left", "centre", "right"))
    spot = "the centre" if (vert, horiz) == ("middle", "centre") else f"{vert} {horiz}".replace("middle ", "")
    big = [b for b in board if b.ref != pl.ref and re.match(r"(U|J|Q|SW)\d", b.ref)]
    near = min(big, key=lambda b: (b.x - pl.x) ** 2 + (b.y - pl.y) ** 2, default=None)
    side = "" if pl.side == "top" else " on the BOTTOM side"
    if near is not None and ((near.x - pl.x) ** 2 + (near.y - pl.y) ** 2) ** 0.5 < 15:
        return f"{spot}{side}, near {near.ref}"
    return f"{spot}{side}"


def plan_assembly(board: list[Placement]) -> list[Step]:
    steps: list[Step] = []
    for pl in sorted(board, key=_rank):
        last = steps[-1] if steps else None
        if last and last.value == pl.value and last.package == pl.package and not _polarity(pl.ref, pl.package) \
                and len(last.refs) < 6:
            last.refs.append(pl.ref)                         # same value, same package: one step
            continue
        steps.append(Step([pl.ref], pl.value, pl.package, _where(pl, board), _polarity(pl.ref, pl.package)))
    return steps


def _say_step(i: int, steps: list[Step]) -> str:
    s = steps[i]
    what = f"{', '.join(s.refs)}: {s.value}" if len(s.refs) > 1 else f"{s.refs[0]}, {s.value}"
    size = _SIZE.search(s.package)
    pkg = f" ({size.group(1)})" if size else (f" ({s.package.split('_')[0]})" if s.package else "")
    text = f"Step {i + 1} of {len(steps)}: {what}{pkg}, {s.where}."
    if len(s.refs) > 1:
        text += f" {len(s.refs)} of the same."
    if s.note:
        text += " " + s.note
    return text


# ── Bring-up ──────────────────────────────────────────────────────────────────

_VOLTS = {"+3V3": 3.3, "+3.3V": 3.3, "+5V": 5.0, "VBUS": 5.0, "+12V": 12.0, "+1V8": 1.8, "+1V2": 1.2,
          "+2V5": 2.5, "+9V": 9.0, "+24V": 24.0, "+15V": 15.0, "-5V": -5.0, "-12V": -12.0, "VCC": 5.0}


def expected_volts(net: str) -> float | None:
    if net in _VOLTS:
        return _VOLTS[net]
    m = re.match(r"^\+?(\d+)V(\d*)$", net)                  # +3V3, +5V0, +12V
    if m:
        return float(f"{m.group(1)}.{m.group(2) or 0}")
    return None


@dataclass
class Check:
    kind: str                      # "short" (power off, ohms) / "volts" (power on)
    net: str
    expect: float | None = None    # volts
    tol: float = 0.05


def plan_bringup(p: kicad.Project) -> list[Check]:
    from arya.eda import circuit as C
    nets: set[str] = set()
    f = p.folder / "circuit.json"
    if f.exists():
        c = C.Circuit.load(f)
        nets = {n for part in c.parts for n in part.pins.values() if C.is_power_net(n)}
    supplies = sorted((n for n in nets if expected_volts(n) is not None), key=lambda n: -abs(expected_volts(n)))
    checks = [Check("short", n) for n in supplies]
    checks += [Check("volts", n, expected_volts(n), 0.05 if n != "VBUS" else 0.10) for n in supplies]
    return checks


def _say_check(i: int, checks: list[Check]) -> str:
    c = checks[i]
    head = f"Check {i + 1} of {len(checks)}: "
    if c.kind == "short":
        first = " Power off and unplugged for these." if i == 0 else ""
        return (head + f"resistance from {c.net} to GND. It should NOT be near zero ohms. What does the meter say?" + first)
    first = " Now plug in the power." if i == 0 or checks[i - 1].kind == "short" else ""
    return head + f"{c.net} to GND, expect {c.expect:g} volts.{first} What do you read?"


_NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(m|k|meg|mega)?\s*(v|volts?|ohms?|Ω|kΩ|mv|millivolts?)?", re.IGNORECASE)
_WORD_NUM = {"zero": 0.0, "nothing": 0.0, "nil": 0.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
             "twelve": 12.0}


def parse_reading(text: str) -> tuple[float | None, str]:
    """('3.28 volts') -> (3.28, 'V'); ('4.7k') -> (4700, 'ohm'); ('short', 'beeps') -> (0, 'short');
    ('open', 'OL') -> (inf, 'ohm')."""
    t = text.lower().strip()
    if re.search(r"\b(?:short(?:ed)?|beeps?|continuity)\b", t):
        return 0.0, "short"
    if re.search(r"\b(?:open|ol|o\.l\.|infinite|overload|no reading)\b", t):
        return float("inf"), "ohm"
    m = _NUM.search(t.replace(",", "."))
    if m:
        v = float(m.group(1))
        mult, unit = (m.group(2) or "").lower(), (m.group(3) or "").lower()
        if unit in ("mv", "millivolt", "millivolts") or (mult == "m" and unit.startswith("v")):
            return v / 1000, "V"
        if mult == "k" or unit == "kω":
            return v * 1000, "ohm"
        if mult in ("meg", "mega"):
            return v * 1e6, "ohm"
        return v, "ohm" if unit.startswith(("ohm", "ω")) else ("V" if unit.startswith("v") else "")
    for w, v in _WORD_NUM.items():
        if re.search(rf"\b{w}\b", t):
            return v, ""
    return None, ""


def judge(check: Check, value: float, unit: str) -> tuple[bool, str]:
    if check.kind == "short":
        ohms = value if unit in ("ohm", "short", "") else value
        if ohms < 10:
            return False, (f"{check.net} looks shorted to GND. Don't power it. Look for solder bridges on the "
                           "chips around it, a capacitor in backwards, or a part on the wrong footprint.")
        return True, f"{'Open' if ohms == float('inf') else f'{ohms:g} ohms'}, no short on {check.net}."
    v, want = value, check.expect or 0.0
    if want and abs(v - want) <= abs(want) * check.tol:
        return True, f"{v:g} volts on {check.net}, in range."
    if abs(v) < 0.2:
        return False, (f"Nothing on {check.net}. Check the input first, then the regulator: is it in the right way "
                       "round, and is its enable pin pulled high?")
    if v < want:
        return False, (f"{v:g} volts on {check.net}, low (expect {want:g}). Something may be loading it: feel for a "
                       "hot part, and check the input is high enough for the regulator's dropout.")
    return False, (f"{v:g} volts on {check.net}, too high (expect {want:g}). Wrong regulator variant, or a feedback "
                   "resistor value? Unplug before anything gets hurt.")


# ── State (per project) ───────────────────────────────────────────────────────

def _state_path(p: kicad.Project):
    return p.folder / "bench.json"


def load_state(p: kicad.Project) -> dict:
    try:
        return json.loads(_state_path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(p: kicad.Project, state: dict) -> None:
    state["touched"] = time.time()
    _state_path(p).write_text(json.dumps(state, indent=2), encoding="utf-8")


def _live(state: dict, now: float | None = None) -> str | None:
    """'assembly' / 'bringup' while a bench session is going, else None."""
    if state.get("mode") and (now or time.time()) - state.get("touched", 0) < SESSION_IDLE_S:
        return state["mode"]
    return None


# ── What you say ──────────────────────────────────────────────────────────────

_ASSEMBLE = re.compile(r"\b(?:guide|walk|take)\s+me\s+through\s+(?:the\s+)?(?:assembly|assembling|soldering)\b|"
                       r"\b(?:start|begin|continue|resume)\s+(?:the\s+)?(?:assembly|assembling|soldering)\b|"
                       r"\b(?:start|begin)\s+assembling\b|\blet'?s\s+(?:assemble|solder)\s+(?:the\s+|my\s+)?board\b",
                       re.IGNORECASE)
_BRINGUP = re.compile(r"\b(?:start|begin|continue|resume|do)\s+(?:the\s+)?bring[\s-]?up\b|\bbring\s+up\s+(?:the|my)\s+board\b|"
                      r"\b(?:power[\s-]?on|first\s+power)\s+(?:checks?|test)\b", re.IGNORECASE)
_WHERE = re.compile(r"\bwhere\s+(?:does|do|is|should)\s+(?P<ref>[A-Z]{1,3}\d{1,3})\b(?:\s+(?:go|sit|be))?", re.IGNORECASE)
_NEXT = re.compile(r"^\W*(?:ok(?:ay)?\W+)?(?:next(?:\s+(?:part|step|one))?|done|placed|soldered|finished|got\s+it|"
                   r"that'?s\s+(?:done|in))\W*$", re.IGNORECASE)
_BACK = re.compile(r"^\W*(?:back|previous(?:\s+step)?|go\s+back|repeat(?:\s+that)?|say\s+(?:that|it)\s+again)\W*$",
                   re.IGNORECASE)
_LEFT = re.compile(r"\bwhat'?s\s+(?:left|remaining)\b|\bhow\s+many\s+(?:parts|steps)\s+(?:are\s+)?left\b", re.IGNORECASE)
_STOP = re.compile(r"\b(?:stop|pause|end|quit)\s+(?:the\s+)?(?:assembly|assembling|soldering|bring[\s-]?up)\b",
                   re.IGNORECASE)
_SKIP = re.compile(r"^\W*skip(?:\s+(?:it|this(?:\s+one)?|that))?\W*$", re.IGNORECASE)
_READ_METER = re.compile(r"\bread\s+(?:the\s+|my\s+)?(?:meter|multimeter|display)\b", re.IGNORECASE)
_CHECK_SOLDER = re.compile(r"\bcheck\s+(?:my\s+|the\s+)?(?:soldering|solder\s+joints?|this\s+(?:part|joint))\b",
                           re.IGNORECASE)
_READING = re.compile(r"^\W*(?:it\s+(?:reads|says|shows)\s+|i\s+(?:read|get|got|measured|see)\s+|reads\s+|about\s+|"
                      r"around\s+)?(?:-?\d+(?:[.,]\d+)?\s*(?:m|k|meg)?\s*(?:v|volts?|ohms?|Ω|mv)?|zero|short(?:ed)?|"
                      r"open|ol|o\.l\.|infinite|nothing|it\s+beeps|beeps)\W*(?:volts?|ohms?)?\W*$", re.IGNORECASE)


def _project_and_state():
    from arya.eda import projects as P
    p = P.active()
    return p, (load_state(p) if p else {})


def matches(message: str) -> bool:
    if any(r.search(message) for r in (_ASSEMBLE, _BRINGUP, _WHERE, _LEFT, _STOP, _READ_METER, _CHECK_SOLDER)):
        return True
    if _NEXT.match(message) or _BACK.match(message) or _READING.match(message) or _SKIP.match(message):
        try:
            _p, state = _project_and_state()
        except Exception:
            return False
        return _live(state) is not None              # only during a bench session
    return False


async def handle(message: str, remote: bool = False) -> str | None:
    if not matches(message):
        return None
    p, state = _project_and_state()
    if p is None:
        return "There's no board project yet. Say \"new board project called\" and a name."
    live = _live(state)

    if _STOP.search(message):
        state["mode"] = None
        save_state(p, state)
        return "Paused. Say \"continue assembly\" or \"continue bring-up\" to pick up where you left off."

    if _ASSEMBLE.search(message):
        if kicad.board_is_empty(p):
            return (f"The {p.title} board has no parts placed yet. In KiCad's PCB editor press F8 to bring them in "
                    "from the schematic, place them, save, and ask again.")
        board = await asyncio.to_thread(positions, p)
        steps = plan_assembly(board)
        resume = re.search(r"\b(?:continue|resume)\b", message, re.IGNORECASE) and state.get("assembly")
        i = min(state.get("assembly", {}).get("step", 0), len(steps) - 1) if resume else 0
        state.update(mode="assembly", assembly={"step": i, "steps": [s.__dict__ for s in steps]})
        save_state(p, state)
        tht = sum(1 for s in steps if _rank(Placement(s.refs[0], s.value, s.package, 0, 0, 0, "top"))[1] >= 5)
        intro = "" if resume else (f"{len(board)} parts in {len(steps)} steps, smallest first"
                                   + (f", {tht} through-hole steps at the end" if tht else "") + ". Say next when each is done. ")
        return intro + _say_step(i, steps)

    if _BRINGUP.search(message):
        checks = plan_bringup(p)
        if not checks:
            return ("I don't know this board's supplies: there's no circuit.json with power nets like +3V3 or +5V. "
                    "Tell me what to measure and I'll log it.")
        resume = re.search(r"\b(?:continue|resume)\b", message, re.IGNORECASE) and state.get("bringup")
        i = state.get("bringup", {}).get("step", 0) if resume else 0
        state.update(mode="bringup", bringup={"step": i, "checks": [c.__dict__ for c in checks], "results": []})
        save_state(p, state)
        return ("Bring-up, one measurement at a time. " if not resume else "") + _say_check(i, checks)

    m = _WHERE.search(message)
    if m:
        ref = m.group("ref").upper()
        if kicad.board_is_empty(p):
            return "The board has no parts placed yet, so I can't say where it goes."
        board = await asyncio.to_thread(positions, p)
        pl = next((b for b in board if b.ref.upper() == ref), None)
        if pl is None:
            return f"There's no {ref} on the {p.title} board."
        note = _polarity(pl.ref, pl.package)
        return f"{pl.ref} ({pl.value}) goes {_where(pl, board)}." + (f" {note}" if note else "")

    if _LEFT.search(message):
        a = state.get("assembly")
        if not a:
            return "No assembly started. Say \"guide me through assembly\"."
        left = a["steps"][a["step"]:]
        refs = [r for s in left for r in s["refs"]]
        return f"{len(left)} steps left, {len(refs)} parts: {', '.join(refs[:12])}" + ("…" if len(refs) > 12 else ".")

    if _READ_METER.search(message):
        return await _read_meter(p, state, remote)

    if _CHECK_SOLDER.search(message):
        return await _check_soldering(p, state, remote)

    if live == "assembly" and (_NEXT.match(message) or _BACK.match(message)):
        a = state["assembly"]
        steps = [Step(**s) for s in a["steps"]]
        if _BACK.match(message):
            a["step"] = max(0, a["step"] - (0 if re.search(r"repeat|again", message, re.IGNORECASE) else 1))
        else:
            a["step"] += 1
        if a["step"] >= len(steps):
            state["mode"] = None
            save_state(p, state)
            kicad.add_to_design(p, "Log", f"assembled the board ({sum(len(s.refs) for s in steps)} parts)")
            return ("That's every part. Before power: inspect for bridges, especially on the ICs, then say \"start "
                    "bring-up\" and we'll check the supplies.")
        save_state(p, state)
        return _say_step(a["step"], steps)

    if live == "bringup" and (_READING.match(message) or _NEXT.match(message) or _SKIP.match(message)):
        b = state["bringup"]
        checks = [Check(**c) for c in b["checks"]]
        c = checks[b["step"]]
        if _SKIP.match(message):
            kicad.add_to_design(p, "Log", f"bring-up {c.kind} {c.net}: skipped")
            b["step"] += 1
            if b["step"] >= len(checks):
                state["mode"] = None
                save_state(p, state)
                return "Skipped. That was the last check."
            save_state(p, state)
            return "Skipped. " + _say_check(b["step"], checks)
        if _NEXT.match(message):
            return "Tell me the number on the meter for this one, or say \"skip\". " + _say_check(b["step"], checks)
        value, unit = parse_reading(message)
        if value is None:
            return "I didn't get a number. Say it like \"3.28 volts\" or \"open\"."
        ok, verdict = judge(c, value, unit)
        b["results"].append({"net": c.net, "kind": c.kind, "value": value if value != float("inf") else "open",
                             "ok": ok})
        kicad.add_to_design(p, "Log", f"bring-up {c.kind} {c.net}: {verdict.split('.')[0]} — {'PASS' if ok else 'FAIL'}")
        if not ok:
            save_state(p, state)
            return verdict + " Fix that first; say \"continue bring-up\" when you're ready to measure again."
        b["step"] += 1
        if b["step"] >= len(checks):
            state["mode"] = None
            save_state(p, state)
            return verdict + " All supplies check out. The board is alive: next, program it or test the parts on it."
        save_state(p, state)
        return verdict + " " + _say_check(b["step"], checks)
    return None


# ── The camera at the bench ───────────────────────────────────────────────────

async def _read_meter(p: kicad.Project, state: dict, remote: bool) -> str:
    if remote:
        return "From the phone: tap 📷, photograph the meter, and tell me the number you see."
    from arya.tools import screen, webcam
    try:
        img = await asyncio.to_thread(webcam.grab)
    except Exception:
        return "I couldn't open the webcam. Tell me the number on the meter instead."
    text = await asyncio.to_thread(screen.ocr_image, img)
    nums = re.findall(r"-?\d+[.,]\d+|-?\d+", text or "")
    if not nums:
        return ("I can't read the display: OCR struggles with LCD digits. Tell me the number, like \"3.28 volts\".")
    reading = nums[0].replace(",", ".")
    if _live(state) == "bringup":
        return await handle(f"{reading} volts") or f"I read {reading}."
    return f"I read {reading} on the display. If that's wrong, tell me the number."


async def _check_soldering(p: kicad.Project, state: dict, remote: bool) -> str:
    from arya.core import router
    from arya.eda import designer
    if remote:
        return "Take a close-up with 📷 on the phone and ask \"check this photo\"."
    if not router.cloud_available() or not designer.cloud_ok(p):
        return ("Looking at solder joints needs the cloud vision model. Say \"design the circuit\" once to give this "
                "project the cloud yes, or check them under a loupe: shiny, concave, no bridges.")
    from arya.knowledge import visuals
    from arya.study import handwriting
    from arya.tools import webcam
    try:
        img = await asyncio.to_thread(webcam.grab)
    except Exception:
        return "I couldn't open the webcam."
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    image_id = visuals._store(png)
    import arya.core.llm as llm
    llm.emit_artifact("VISUAL", {"type": "image", "id": image_id, "caption": "Your board (webcam)"})
    a = state.get("assembly") or {}
    current = a.get("steps", [])[max(0, a.get("step", 0) - 1)] if a.get("steps") else None
    focus = f" Focus on {', '.join(current['refs'])} ({current['value']}, {current['package']})." if current else ""
    prompt = ("This is a webcam photo of a printed circuit board being hand-soldered." + focus +
              " Point out visible problems only: solder bridges, missing or tombstoned parts, parts that look rotated "
              "or reversed, dull or balled joints. Say if the photo is too blurry to judge. At most four short plain "
              "sentences, no lists.")
    from arya.core.audit import log_event
    log_event("cloud_vision_request", bytes=len(png), source="bench")
    try:
        return await handwriting._vision_call(png, prompt=prompt)
    except Exception as exc:
        return f"The cloud check didn't work ({type(exc).__name__}). Check under a loupe: shiny joints, no bridges."
