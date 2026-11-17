"""
Design from scratch (roadmap K2): the project's requirements -> a complete circuit, drawn as a KiCad schematic.

  "design the circuit"  /  "draft the schematic for a USB-C powered ATtiny85 board with an LED"

  0. One yes per project to use the cloud model (asked once, remembered in the project folder). What goes out: the
     project's requirements, decisions and open questions from design.md, plus public KiCad library data, with
     personal details redacted (arya/core/guard.py). Never your memories, files or other conversations.
  1. PLAN (one cloud call): the model lists the parts the board needs — role, a search query, a block.
  2. CATALOGUE (local): ARYA searches YOUR KiCad libraries for each and builds a compact catalogue of candidates
     with their real pins and real footprints, plus the everyday parts (R, C, LED, button, headers).
  3. WIRE (one cloud call): the model writes the complete circuit, choosing only from the catalogue.
  4. CHECK (local): mechanical slips are fixed (arya/eda/circuit.py repair), everything is validated against the
     libraries; problems go back for a fix call (at most three).
  5. The schematic is generated (one sheet per block), KiCad's ERC runs, design.md gets the decisions (why each
     part), the open questions and ARYA's fixes. ARYA says when the draft is ready.

Why plan-then-execute and not a chatty tool loop: Groq's free tier allows 8000 tokens per minute. Measured
2026-11-17: a tool loop needed ~20 calls of 5-9k tokens (~20 minutes) and lost track of its own searches; this
needs 2-5 calls (~1-4 minutes) and each request stays well under the limit.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from arya.eda import circuit as C
from arya.eda import kicad
from arya.eda import schematic as G
from arya.eda import symbols as S

MAX_FIXES = 3
MAX_WAIT_S = 300                       # total waiting for Groq's per-minute token limit, per call
CATALOGUE_CHARS = 16000                # ~4.5k tokens of catalogue: with the rest, under 8k per request
GENERIC = ["Device:R", "Device:C", "Device:C_Polarized", "Device:LED", "Switch:SW_Push", "Connector:TestPoint",
           "Connector_Generic:Conn_01x02", "Connector_Generic:Conn_01x04"]

ENGINEER = """You are a senior hardware engineer designing a PCB schematic in KiCad 9 from the user's \
requirements. Pick robust, commonly available parts and follow each chip's typical application circuit: \
decoupling (100 nF per IC supply pin group plus bulk capacitance), pull-ups where a bus or reset needs them, \
USB-C CC resistors (5.1k to GND) for a sink, an indicator LED where useful, connectors and test points so the \
board can be programmed and measured."""

PLAN_RULES = """List every part the board needs EXCEPT plain resistors, capacitors, LEDs, push buttons and pin \
headers (those are always available). For each: its role, the block it belongs to (Power, MCU, USB, Sensors …) and \
a search query for KiCad's symbol libraries — a part number when you know a good one (AMS1117-3.3, ATtiny85, \
ESP32-C3-MINI-1, TPS62162), otherwise the function (\"USB C receptacle power only\", \"N-channel MOSFET SOT-23\"). \
Then call plan_parts."""

WIRE_RULES = """Write the complete circuit and call submit_circuit. Rules:
- Use ONLY symbols from the catalogue below, with their exact Library:Name.
- Connect pins by number or exact pin name; a name shared by several pins (GND, VBUS) connects all of them.
- Every IC pin is connected or "NC". Unused GPIOs: "NC".
- Power nets use KiCad power names: GND, +3V3, +5V, VBUS, +12V, VCC. Other nets: short UPPER_CASE names.
- Footprints: one listed for the symbol in the catalogue (passives: 0603, bulk caps 0805).
  Connector_Generic:Conn_01xNN / Conn_02xNN_Odd_Even exist for any NN (footprint e.g.
  Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical).
- References unique (R1, C1, U1, J1, D1, SW1 …). One-line "why" per part. List decisions and open questions."""

PLAN_TOOL = {"type": "function", "function": {
    "name": "plan_parts",
    "description": "The parts the board needs (not plain R/C/LED/buttons/headers).",
    "parameters": {"type": "object", "properties": {
        "parts": {"type": "array", "items": {"type": "object", "properties": {
            "role": {"type": "string"}, "block": {"type": "string"}, "query": {"type": "string"}},
            "required": ["role", "block", "query"]}},
        "approach": {"type": "string", "description": "two sentences: how the board works"}},
        "required": ["parts"]}}}

SUBMIT_TOOL = {"type": "function", "function": {
    "name": "submit_circuit",
    "description": "The complete circuit.",
    "parameters": {"type": "object", "properties": {
        "blocks": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "parts": {"type": "array", "items": {"type": "object", "properties": {
                "ref": {"type": "string"}, "symbol": {"type": "string"}, "value": {"type": "string"},
                "footprint": {"type": "string"},
                "pins": {"type": "object", "description": "pin number or name -> net name, or 'NC'",
                         "additionalProperties": {"type": "string"}},
                "why": {"type": "string"}},
                "required": ["ref", "symbol", "value", "footprint", "pins"]}}},
            "required": ["name", "parts"]}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}}},
        "required": ["blocks"]}}}


class DesignFailed(RuntimeError):
    pass


# ── Local: the brief and the catalogue ────────────────────────────────────────

def _brief(p: kicad.Project, request: str) -> str:
    s = kicad._sections(p)
    parts = [f"Project: {p.title}"]
    for name in ("Requirements", "Decisions", "Open questions"):
        if s.get(name):
            parts.append(f"{name}:\n" + "\n".join(f"- {i}" for i in s[name] if not i.startswith("ARYA's fix")))
    if request:
        parts.append(f"The user's request now: {request}")
    return "\n\n".join(parts)


def _pins(lib_id: str, max_pins: int = 90) -> str:
    """'Lib:Name — description; footprints: …' then one line per pin: 'number name type'."""
    sym = S.load(*lib_id.split(":", 1))
    seen, lines = set(), []
    for p in sym.pins:
        if p.number in seen:
            continue
        seen.add(p.number)
        lines.append(f"  {p.number} {p.name if p.name not in ('~', '') else '-'} {p.etype}"
                     + (f" u{p.unit}" if sym.units > 1 and p.unit else ""))
    desc = sym.props.get("Description", "")[:90]
    fps = S.suggest_footprints(sym, k=3)
    head = f"{sym.lib_id}" + (f" — {desc}" if desc else "") + (f"; footprints: {', '.join(fps)}" if fps else "")
    more = f"\n  … {len(lines) - max_pins} more pins" if len(lines) > max_pins else ""
    return head + "\n" + "\n".join(lines[:max_pins]) + more


def catalogue(plan: list[dict]) -> tuple[str, list[str]]:
    """Candidates for each planned part (top 2 by search) and the everyday parts: their pins and footprints.
    Returns (text, the symbols in it)."""
    chosen: list[str] = []
    notes: list[str] = []
    for item in plan[:14]:
        hits = S.search(str(item.get("query", "")), 2)
        if not hits:
            notes.append(f"(nothing found for {item.get('role')!r}: {item.get('query')!r} — pick something else from the catalogue)")
        for h in hits:
            lib_id = f"{h['lib']}:{h['name']}"
            if lib_id not in chosen:
                chosen.append(lib_id)
    sections, size = [], 0
    for i, lib_id in enumerate(chosen + [g for g in GENERIC if g not in chosen]):
        try:
            text = _pins(lib_id)
        except (KeyError, RuntimeError):
            continue
        if size + len(text) > CATALOGUE_CHARS and i % 2 == 1 and lib_id not in GENERIC:
            continue                                   # over budget: drop second-choice candidates first
        sections.append(text)
        size += len(text)
    return "\n\n".join(sections + notes), chosen


# ── The cloud ─────────────────────────────────────────────────────────────────

def _retry_after(exc: Exception) -> float:
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", str(exc))
    if not m:
        return 15.0
    secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1) + 60 * int(m.group(1) or 0)
    return min(60.0, secs + 1.0)


async def _cloud_complete(model: str, messages: list[dict], *, tools=None, tool_choice="auto", temperature=None):
    """The cloud only — never the small local model (it can't design circuits). Waits out rate limits."""
    import groq

    import arya.core.llm as llm
    from arya.core.guard import Redaction
    redaction = Redaction()
    kwargs = dict(model=model, messages=redaction.messages(messages), tools=tools, tool_choice=tool_choice,
                  parallel_tool_calls=False, **llm._groq_extra(model))
    if temperature is not None:
        kwargs["temperature"] = temperature
    waited = 0.0
    while True:
        try:
            response = await llm._get_client().chat.completions.create(**kwargs)
            break
        except groq.RateLimitError as exc:
            if "per day" in str(exc):                 # the free tier's 200k tokens a day: waiting won't help
                m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?", str(exc))
                later = (f" in about {int(m.group(1) or 0) * 60 + int(m.group(2) or 0) or 1} minutes"
                         if m and (m.group(1) or m.group(2)) else " later")
                raise DesignFailed("today's cloud allowance is used up (Groq's free tier: 200,000 tokens a day, "
                                   f"about six designs); try again{later}") from exc
            wait = _retry_after(exc)
            if waited + wait > MAX_WAIT_S:
                raise DesignFailed("the cloud model's rate limit didn't clear in five minutes") from exc
            print(f"KiCad design: cloud rate limit, waiting {wait:.0f} s")
            await asyncio.sleep(wait)
            waited += wait
        except (groq.APIConnectionError, groq.InternalServerError) as exc:
            if waited > MAX_WAIT_S:
                raise DesignFailed("couldn't reach the cloud model") from exc
            await asyncio.sleep(5)
            waited += 5
        except groq.BadRequestError as exc:
            # gpt-oss sometimes writes a thought where the tool call should be ("output_parse_failed"): a text
            # turn, which the caller retries.
            body = getattr(exc, "body", None) or {}
            err = body.get("error", body) if isinstance(body, dict) else {}
            if isinstance(err, dict) and err.get("code") in ("output_parse_failed", "tool_use_failed"):
                return llm._Completion(content=redaction.restore(str(err.get("failed_generation", ""))[:500]))
            raise
    msg = response.choices[0].message
    calls = [llm._ToolCall(id=tc.id, name=tc.function.name, arguments=redaction.restore(tc.function.arguments or ""))
             for tc in (msg.tool_calls or [])]
    return llm._Completion(content=redaction.restore(msg.content or ""), tool_calls=calls)


async def _call_tool(complete, messages: list[dict], tool: dict, tries: int = 3) -> dict:
    """One forced tool call; its arguments. A text turn instead (a Groq parsing glitch) is retried."""
    import arya.core.llm as llm
    name = tool["function"]["name"]
    for _ in range(tries):
        out = await complete(llm.MODEL_NAME, messages, tools=[tool],
                             tool_choice={"type": "function", "function": {"name": name}}, temperature=0.2)
        for call in out.tool_calls or []:
            if call.name == name:
                return call.arguments if isinstance(call.arguments, dict) else json.loads(call.arguments or "{}")
    raise DesignFailed(f"the cloud model didn't answer with {name}")


# ── The whole design ──────────────────────────────────────────────────────────

async def design(p: kicad.Project, request: str = "", complete=None) -> dict:
    """Plan -> catalogue -> wire -> check/fix. Returns {"circuit", "problems", "fixes", "decisions",
    "open_questions", "steps", "plan"}. `complete` defaults to the cloud (injectable for tests)."""
    from arya.core.guard import clean
    complete = complete or _cloud_complete
    brief = clean(_brief(p, request), "design.md")
    calls = 0

    plan_args = await _call_tool(complete, [{"role": "system", "content": ENGINEER},
                                            {"role": "user", "content": brief + "\n\n" + PLAN_RULES}], PLAN_TOOL)
    calls += 1
    plan = [i for i in plan_args.get("parts", []) if isinstance(i, dict)]
    text, _chosen = await asyncio.to_thread(catalogue, plan)
    plan_lines = "\n".join(f"- {i.get('block')}: {i.get('role')} (searched {i.get('query')!r})" for i in plan)
    wire_prompt = (brief + "\n\nYour plan:\n" + plan_lines + (f"\nApproach: {plan_args['approach']}"
                                                             if plan_args.get("approach") else "")
                   + "\n\n" + WIRE_RULES + "\n\nCatalogue (symbols, footprints, pins):\n" + text)
    messages = [{"role": "system", "content": ENGINEER}, {"role": "user", "content": wire_prompt}]
    fixes: list[str] = []
    for attempt in range(1, MAX_FIXES + 2):
        args = await _call_tool(complete, messages, SUBMIT_TOOL)
        calls += 1
        circuit = C.Circuit.from_dict(args)
        fixes += C.repair(circuit)                    # wrong-case symbols, made-up footprints: fixed here
        problems = C.validate(circuit) if circuit.parts else [C.Problem("error", "", "the circuit has no parts")]
        errs = C.errors(problems)
        if not errs:
            return {"circuit": circuit, "problems": problems, "fixes": fixes, "steps": calls, "plan": plan,
                    "decisions": [str(d) for d in args.get("decisions", []) or []],
                    "open_questions": [str(q) for q in args.get("open_questions", []) or []]}
        print(f"KiCad design: draft {attempt} not accepted, {len(errs)} problem(s), first: {errs[0]}")
        if attempt > MAX_FIXES:
            raise DesignFailed(f"{len(errs)} problems left after {attempt} drafts: " + "; ".join(map(str, errs[:3])))
        messages = [{"role": "system", "content": ENGINEER}, {"role": "user", "content": (
            wire_prompt + "\n\nYour last draft (ARYA already fixed what it could) was NOT accepted. Problems:\n"
            + "\n".join(f"- {e}" for e in errs[:20]) + "\n\nThe draft:\n"
            + json.dumps(circuit.to_dict(), separators=(",", ":"))[:9000]
            + "\n\nFix the problems and call submit_circuit with the full circuit.")}]


def apply(p: kicad.Project, result: dict) -> str:
    """Save circuit.json, draw the schematic, run ERC, write design.md. Returns what to say."""
    circuit: C.Circuit = result["circuit"]
    circuit.save(p.folder / "circuit.json")
    gen = G.generate(p, circuit)
    report = kicad.erc(p)
    for d in result["decisions"][:12]:
        kicad.add_to_design(p, "Decisions", d)
    for q in result["open_questions"][:8]:
        kicad.add_to_design(p, "Open questions", q)
    for f in result.get("fixes", [])[:12]:                # what ARYA corrected in the model's draft
        kicad.add_to_design(p, "Open questions", f"ARYA's fix — {f}")
    n_parts, blocks = len(circuit.parts), list(circuit.blocks)
    kicad.add_to_design(p, "Log", f"ARYA drafted the circuit: {n_parts} parts in {len(blocks)} blocks "
                                  f"({', '.join(blocks)}); ERC {len(report.errors)} errors, {len(report.warnings)} warnings")
    warns = [w for w in result["problems"] if w.level == "warning"]
    text = (f"The {p.title} draft is ready: {n_parts} parts in {len(blocks)} blocks ({', '.join(blocks)}). "
            + kicad.summary(report))
    if warns:
        text += f" {len(warns)} note{'s' * (len(warns) != 1)} from my checks, first: {warns[0]}."
    if gen.kept:
        text += f" I didn't overwrite your edited {', '.join(gen.kept)}; the new version is next to it."
    if result["open_questions"]:
        text += f" Open question: {result['open_questions'][0]}"
    return text


# ── Consent, once per project ─────────────────────────────────────────────────

def _state_file(p: kicad.Project):
    return p.folder / ".arya-project.json"


def cloud_ok(p: kicad.Project) -> bool:
    try:
        return bool(json.loads(_state_file(p).read_text(encoding="utf-8")).get("cloud_ok"))
    except (OSError, ValueError):
        return False


def set_cloud_ok(p: kicad.Project, ok: bool) -> None:
    try:
        data = json.loads(_state_file(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.update(cloud_ok=ok, cloud_ok_at=time.strftime("%Y-%m-%d %H:%M"))
    _state_file(p).write_text(json.dumps(data, indent=2), encoding="utf-8")


CONSENT_QUESTION = ("Designing the circuit needs the cloud model. Only this project's requirements and decisions go "
                    "out, with personal details removed; never your memories, files or other conversations. Use the "
                    "cloud for the {title} project? I'll remember your answer for this project. Say yes or no.")


async def run(p: kicad.Project, request: str, say) -> None:
    """Background: design, draw, report (spoken through `say`)."""
    started = time.monotonic()
    try:
        result = await design(p, request)
        text = await asyncio.to_thread(apply, p, result)
        print(f"KiCad: designed {p.name} in {time.monotonic() - started:.0f} s ({result['steps']} cloud calls)")
    except DesignFailed as exc:
        text = f"I couldn't finish the {p.title} circuit: {exc}. Add more detail to the requirements and ask again."
        print(f"KiCad: design failed for {p.name}: {exc}")
        try:
            kicad.add_to_design(p, "Log", re.sub(r"\s+", " ", text)[:200])
        except Exception:
            pass
    except Exception as exc:
        text = f"The {p.title} design stopped with an error ({type(exc).__name__}). Nothing was changed."
        print(f"KiCad: design error for {p.name}: {type(exc).__name__}: {exc}")
    await say(text)
