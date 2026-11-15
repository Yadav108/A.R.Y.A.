"""
Exercise tutor (roadmap M2).

  "Walk me through exercise 1-13"   -> page on screen, what's given / asked, "where would you start?"
  (your steps)                      -> feedback on each step, one hint at a time; the model sees the
                                       official results but must not give them away
  "I got v = 2.83 m/s"              -> checked against the answers page by code, units converted:
                                       within 1 % correct, within 5 % "close (rounding?)", else "not quite"
  "hint" / "show me the solution"   -> a hint / the results read from the answers page (never generated)
  "stop"                            -> session ends

Answer checks also work outside a session: "For exercise 1-13 I got a = 4.26 m/s²".
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

SESSION_IDLE_S = 20 * 60

# ── Reading results off the answers page ──────────────────────────────────────

_GREEK_WORDS = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "eta": "η", "theta": "θ", "lambda": "λ",
                "mu": "µ", "rho": "ρ", "sigma": "σ", "tau": "τ", "phi": "φ", "omega": "ω", "psi": "ψ"}
_QUANTITY_WORDS = {"speed": "v", "velocity": "v", "acceleration": "a", "angular acceleration": "α",
                   "angular velocity": "ω", "work": "w", "power": "p", "energy": "e", "force": "f", "time": "t"}
_UNIT = r"(?:[a-zA-Zµ°Ω%][a-zA-Z0-9µ°Ω%²³]*(?:/[a-zA-Z0-9²³]+)?|/[a-zA-Z][a-zA-Z0-9²³]*)"
_RESULT = re.compile(r"=\s*(-?\d+(?:[.,]\d+)?)\s*(" + _UNIT + r")?(?=\s*(?:$|[;,=→(\n]|\.(?:\s|$)))")
_NAME = re.compile(r"([A-Za-zα-ωΑ-Ωµ][A-Za-z0-9α-ωΑ-Ωµ]*(?:\([A-Za-z0-9]+\))?)\s*=")


def _norm_name(name: str) -> str:
    s = name.strip()
    for word, letter in _GREEK_WORDS.items():
        s = re.sub(rf"\b{word}\b\s*", letter, s, flags=re.IGNORECASE)
    s = s.replace("ϕ", "φ")
    return re.sub(r"[^A-Za-z0-9α-ωΑ-Ωµφ]", "", s).lower()


def _norm_unit(u: str | None) -> str:
    if not u:
        return ""
    s = u.strip().replace(" ", "").replace("²", "2").replace("³", "3").replace("^", "")
    s = re.sub(r"^(?:rad|1)/", "/", s)
    s = {"deg": "°", "degree": "°", "degrees": "°", "kmh": "km/h", "kph": "km/h", "km/hr": "km/h",
         "sec": "s", "seconds": "s", "second": "s"}.get(s.lower(), s)
    return s


# (dimension, factor to SI)
_UNITS = {"m/s": ("speed", 1), "km/h": ("speed", 1 / 3.6), "m/s2": ("acceleration", 1), "/s": ("rate", 1),
          "/min": ("rate", 2 * math.pi / 60), "/s2": ("angular acceleration", 1), "s": ("time", 1),
          "ms": ("time", 1e-3), "min": ("time", 60), "h": ("time", 3600), "m": ("length", 1), "cm": ("length", 0.01),
          "mm": ("length", 0.001), "km": ("length", 1000), "J": ("energy", 1), "kJ": ("energy", 1000),
          "W": ("power", 1), "kW": ("power", 1000), "N": ("force", 1), "kN": ("force", 1000),
          "°": ("angle", 1), "kg": ("mass", 1), "g": ("mass", 1e-3), "Nm": ("torque", 1), "%": ("percent", 1)}
_UNIT_WORDS = {"m/s": "metres per second", "km/h": "kilometres per hour", "m/s2": "metres per second squared",
               "/s": "per second", "/s2": "per second squared", "s": "seconds", "m": "metres", "J": "joules",
               "W": "watts", "N": "newtons", "°": "degrees"}


@dataclass
class Result:
    name: str                     # as written: "α2", "v", "tA"
    value: float
    unit: str                     # normalised: "m/s", "/s2", ""
    alternates: list = field(default_factory=list)      # same result in other units: [(9.4, "km/h")]

    @property
    def key(self) -> str:
        return _norm_name(self.name)

    def spoken(self) -> str:
        v = f"{self.value:g}"
        return f"{self.name} = {v}" + (f" {self.unit}" if self.unit and not self.unit.startswith("/") else self.unit)


def parse_results(answers: str) -> list[Result]:
    """'tA = π/ω1 = 1.57 s; α2 = 2 π/tA2 = 2.55/s2; …' -> [tA 1.57 s, α2 2.55 /s2, …].
    Only final values count ('W = 2 F r = 255 J' -> 255 J)."""
    text = re.sub(r"[ሬതඥට⃗]+", " ", answers)                        # PDF font noise in formulas
    text = re.sub(r"^\s*\d+\s*[-–]\s*\d+\)\s*", "", text)          # the "1-13)" label
    out: list[Result] = []
    for item in re.split(r";|→|\n(?=\s*[A-Za-zα-ωΑ-Ω][\w()]*\s*=)", text):     # "→" starts the next step
        name_m = _NAME.search(item)
        values = [(float(v.replace(",", ".")), _norm_unit(u)) for v, u in _RESULT.findall(item)]
        if not name_m or not values:
            continue
        main, *alts = values[::-1]                                   # the last "= value unit" is the result
        out.append(Result(name_m.group(1), main[0], main[1], alternates=alts))
    return out


# ── Reading the student's claim ───────────────────────────────────────────────

_CLAIM_NUM = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(" + _UNIT + r")?")


def parse_claim(text: str) -> tuple[str | None, float, str] | None:
    """'I got v = 2.83 m/s' -> ('v', 2.83, 'm/s'); 'alpha 2 is 2.55' -> ('α2', 2.55, ''); None if no number."""
    t = text.replace("²", "2").replace("³", "3")
    t = re.sub(r"\b(?:for |of )?(?:exercise|question|problem|task)\s*\d+(?:\s*[-–.]\s*\d+)?\b", " ", t, flags=re.I)
    m = re.search(r"([A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω]*(?:\s+\d)?)\s*(?:=|is|was|equals|comes out (?:as|to))\s*"
                  r"(-?\d+(?:[.,]\d+)?)\s*(" + _UNIT + r")?", t, re.IGNORECASE)
    name = None
    if m and m.group(1).lower() not in ("it", "answer", "result", "this", "that", "mine", "i", "got", "value"):
        name, value, unit = m.group(1), float(m.group(2).replace(",", ".")), _norm_unit(m.group(3))
    else:
        nums = list(_CLAIM_NUM.finditer(t))
        if not nums:
            return None
        n = nums[-1]
        value, unit = float(n.group(1).replace(",", ".")), _norm_unit(n.group(2))
    if unit and unit not in _UNITS and not unit.startswith("/"):
        unit = ""                                                  # "2.83 for" -> no unit
    lowered = t.lower()
    if name is None:                                               # "90 degrees for beta a"
        after = re.search(r"\bfor (?:the )?([A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω]*(?: [A-Za-z0-9])?)\W*$", t.strip(), re.I)
        if after and after.group(1).lower() not in _QUANTITY_WORDS:
            name = after.group(1)
    if name and name.lower() in _QUANTITY_WORDS:                   # "the speed is 9 km/h" -> v
        name = _QUANTITY_WORDS[name.lower()]
    if name is None:
        for word, sym in sorted(_QUANTITY_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"\b{word}\b", lowered):
                name = sym
                break
    return name, value, unit


def _si(value: float, unit: str) -> tuple[str, float] | None:
    if not unit:
        return None
    dim = _UNITS.get(unit)
    return (dim[0], value * dim[1]) if dim else None


def check(claim: tuple[str | None, float, str], results: list[Result]) -> str:
    """A spoken verdict. Reveals the official value only when the student already has it."""
    name, value, unit = claim
    if not results:
        return ("I don't have official results for that exercise, so I can't check the number. "
                "Tell me how you got it and I'll look at your steps.")
    key = _norm_name(name) if name else ""
    candidates = [r for r in results if key and r.key == key]
    if not candidates and key:                                     # "a2" vs "α2", "v" vs "vK"
        candidates = [r for r in results if r.key.startswith(key) or key.startswith(r.key)]
    claim_si = _si(value, unit)
    if not candidates:
        if claim_si:
            candidates = [r for r in results if _si(r.value, r.unit) and _si(r.value, r.unit)[0] == claim_si[0]]
        if not candidates:
            candidates = results

    best, best_rel, unit_problem, best_opt = None, math.inf, None, None
    for r in candidates:
        options = [(r.value, r.unit)] + list(r.alternates)
        for v, u in options:
            if unit and u and claim_si and _si(v, u) and _si(v, u)[0] != claim_si[0]:
                unit_problem = r
                continue
            ref = _si(v, u)[1] if (claim_si and u and _si(v, u)) else v
            mine = claim_si[1] if (claim_si and u and _si(v, u)) else value
            rel = abs(mine - ref) / max(abs(ref), 1e-12)
            if rel < best_rel or (rel == best_rel and u == unit):
                best, best_rel, best_opt = r, rel, (v, u)
    if best is None and unit_problem is not None:
        words = _UNIT_WORDS.get(unit_problem.unit, unit_problem.unit or "a different unit")
        return f"Check your unit: the answer sheet gives {unit_problem.name} in {words}."
    if best is None:
        return "I couldn't match that to a result on the answer sheet. Which quantity is it?"
    if best_rel <= 0.01:
        v, u = best_opt
        shown = Result(best.name, v, u).spoken()                      # in the unit the student used
        return f"Correct! {shown} matches the answer sheet."
    if best_rel <= 0.05:
        return (f"Close: that's within {max(1, round(best_rel * 100))} % of the answer sheet for {best.name}, "
                f"so probably rounding. Check how many digits you kept.")
    if not key and len(candidates) > 1:            # no name given: don't point at (or give away) one quantity
        dim = claim_si[0] if claim_si else "result"
        return f"Not quite: that doesn't match any {dim} on the answer sheet. Which quantity is it, or want a hint?"
    return f"Not quite: that doesn't match the answer sheet for {best.name}. Want a hint?"


# ── Sessions ──────────────────────────────────────────────────────────────────

_START = re.compile(r"\b(?:walk me through|step by step|tutor me|let'?s (?:do|solve|work on|go through|practi[cs]e)|"
                    r"help me (?:with|solve|through)|guide me through|teach me|go through|practi[cs]e)\b", re.I)
_END = re.compile(r"^\W*(?:ok(?:ay)?\W+)?(?:stop|quit|end|exit|done|finish(?:ed)?|that'?s enough|i'?m done|"
                  r"let'?s stop)(?:\s+(?:tutoring|the exercise|this|it|here|now))?\W*$", re.I)
_SOLUTION = re.compile(r"\b(?:show|tell|give|read)\b.{0,20}\b(?:solution|answers?|results?)\b|\bi give up\b", re.I)
_HINT = re.compile(r"\bhint\b|\bi'?m stuck\b|\bwhat(?:'s| is)? next\b|\bnext step\b|\bhelp me\b|"
                   r"\bi don'?t know\b|\bno idea\b", re.I)
_CLAIMS = re.compile(r"\b(?:i got|i get|i have|my (?:answer|result)|i found|i calculated|comes out|"
                     r"is it|i think it'?s)\b|=|\b(?:is|was|equals|comes to)\s+-?\d", re.I)
# Mid-session requests about something else go to ARYA as usual (the session stays open).
_UNRELATED = re.compile(r"\b(?:time|date|day|weather|temperature outside|timer|alarm|remind(?:er)?|open|close|"
                        r"launch|play|pause|music|song|volume|louder|quieter|mute|battery|cpu|note|notes|email|"
                        r"calendar|joke|news|wikipedia|search the web)\b", re.I)


_ASKS_VALUE = re.compile(r"\b(?:what(?:'s| is| are)|which value|value of|how (?:big|large|much|fast)|"
                         r"answer (?:for|to)|result (?:for|of))\b", re.I)
_EXPLAIN = re.compile(r"\b(?:explain|why|how (?:do|did|can|would)|derive|walk|step)\b", re.I)


def lookup(message: str, results: list) -> Result | None:
    """The result whose symbol the question names: 'alpha 2' -> α2, 'v(tA)' -> v, 'the velocity' -> v."""
    text = re.sub(r"\b(?:exercise|question|problem|task)\s*\d+(?:\s*[-–.]\s*\d+)?\b", " ", message, flags=re.I)
    text = re.sub(r"\([^()]*\)", " ", text)                       # "v(tA)": the bracket is the time point
    tokens = re.findall(r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω]*|\d+", text)
    # Longest names first: "beta a" -> βa and "alpha 2" -> α2 before "a" or "alpha" alone.
    keys = [_norm_name(f"{a} {b}") for a, b in zip(tokens, tokens[1:]) if a.lower() in _GREEK_WORDS]
    keys += [_norm_name(t) for t in tokens if not t.isdigit()]
    for word, sym in sorted(_QUANTITY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\b", text, re.I):
            keys.append(sym)
            break
    by_key = {r.key: r for r in reversed(results)}                # first occurrence wins
    for k in keys:
        if k in by_key:
            return by_key[k]
    latin = {r.key.translate(_GREEK_TO_LATIN): r for r in reversed(results) if r.key not in ("a", "v")}
    for k in keys:                                                # notes that write α2 as "a2", ω as "w"
        if k.translate(_GREEK_TO_LATIN) in latin:
            return latin[k.translate(_GREEK_TO_LATIN)]
    return None


_GREEK_TO_LATIN = str.maketrans({"α": "a", "β": "b", "γ": "g", "δ": "d", "ω": "w", "θ": "t", "φ": "f", "µ": "u",
                                 "η": "n", "τ": "t", "ρ": "p"})


@dataclass
class Session:
    ref: str
    statement: str
    answers: str
    results: list
    sources: list
    worked: str = ""                                     # the student's handwritten solution, if read before
    transcript: list = field(default_factory=list)       # [(role, text)]
    last: float = field(default_factory=time.monotonic)


_session: Session | None = None


def active() -> Session | None:
    global _session
    if _session and time.monotonic() - _session.last > SESSION_IDLE_S:
        _session = None
    return _session


def end() -> None:
    global _session
    _session = None


def _is_answers(text: str) -> bool:
    eq = text.count("=")
    long_words = len(re.findall(r"[A-Za-z]{4,}", text))
    return eq >= 2 and long_words <= 4 * eq


def load_exercise(ref_question: str) -> Session | None:
    """Find statement + answers for the exercise named in the question."""
    from arya.knowledge import library

    refs = library.exercise_refs(ref_question)
    if not refs:
        return None
    passages = library.find_exercise(refs, ref_question, k=6)
    if not passages:
        return None
    worked = [p for p in passages if p.title == "Handwritten solutions"]       # the student's own solution
    passages_ = [p for p in passages if p.title != "Handwritten solutions"]
    answers = [p for p in passages_ if _is_answers(p.text)]
    statements = [p for p in passages_ if not _is_answers(p.text)]
    statement = max(statements, key=lambda p: len(p.text)) if statements else None
    ref = passages[0].note.replace("exercise ", "")
    answers_text = "\n".join(p.text for p in answers)
    return Session(ref=ref, statement=statement.text if statement else "", answers=answers_text,
                   results=parse_results(answers_text), sources=[p.as_dict() for p in passages],
                   worked=worked[0].text if worked else "")


def _asked_first(s: Session) -> list:
    """Results the task asks for ('Determine … α2 … v(tA) and a(tA)') before intermediate ones."""
    asked_sentences = " ".join(re.findall(r"[^.!?]*\b(?:determine|find|calculate|compute|what|which)\b[^.!?]*",
                                          s.statement, re.IGNORECASE))
    tokens = {_norm_name(t) for t in re.findall(r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω]*", asked_sentences)}
    return sorted(s.results, key=lambda r: 0 if r.key in tokens else 1)


def solution_text(s: Session) -> str:
    numeric = _asked_first(s)[:6]
    if numeric:
        listed = "; ".join(r.spoken() for r in numeric)
        more = f" There are {len(s.results) - 6} more on the answers page." if len(s.results) > 6 else ""
        return f"The answer sheet for exercise {s.ref} says: {listed}.{more} Want me to explain how one of them is found?"
    if s.answers:
        clean = re.sub(r"\s+", " ", re.sub(r"[ሬതඥට⃗]+", " ", s.answers)).strip()[:300]
        return f"The answer sheet for exercise {s.ref} says: {clean}"
    return f"I don't have the answers page for exercise {s.ref}, only the task."


def _tutor_prompt(s: Session, mode: str) -> str:
    base = (
        "You are ARYA, a patient Socratic tutor for a mechatronics student, speaking aloud. "
        f"You are working through exercise {s.ref} from their course together.\n\n"
        f"Task (from their exercise sheet):\n{s.statement[:1500]}\n\n"
        f"Official results (HIDDEN — never state these numbers or the final formulas unless the student "
        f"already said them):\n{s.answers[:900] or 'not available'}\n\n"
        + (f"The student's own handwritten worked solution (HIDDEN, use it to see which method they know):\n"
           f"{s.worked[:1500]}\n\n" if s.worked else "") +
        "Rules: at most three short spoken sentences; no lists, headings, markdown or LaTeX; use the task's "
        "notation; never do the whole solution for them; never invent given values. Let the student write the "
        "formulas and do the calculations: point at the idea or law, don't state the formula unless they are "
        "stuck after a hint."
    )
    if mode == "start":
        return base + (" Now: say in one sentence what the exercise is about, then what is given and what is "
                       "asked, then ask which law or first step they would use.")
    if mode == "hint":
        return base + " The student is stuck: give exactly one concrete hint for the next step, then ask them to try."
    return base + (" Respond to the student's last message: if their step or idea is right, say so briefly and ask "
                   "for the next step; if it is wrong, point gently at the mistake with one hint.")


async def _ask_model(s: Session, mode: str, message: str = "") -> str:
    from arya.core.llm import INTENT_MODEL_NAME, _complete, plain_text, trim_unfinished
    from arya.core.speech_math import latex_to_text

    messages = [{"role": "system", "content": _tutor_prompt(s, mode)}]
    for role, text in s.transcript[-8:]:
        messages.append({"role": role, "content": text})
    if message:
        messages.append({"role": "user", "content": message})
    try:
        completion = await _complete(INTENT_MODEL_NAME, messages, temperature=0.3, max_tokens=170)
        reply = plain_text(latex_to_text(trim_unfinished(completion.content or ""))).strip()
    except Exception as exc:
        print(f"Tutor model failed: {exc}")
        reply = ""
    return reply


async def handle(message: str) -> tuple[str, list | None] | None:
    """(reply, sources to show) when this is tutoring, else None (ARYA answers normally)."""
    global _session
    s = active()

    if _START.search(message):
        from arya.knowledge import library
        if library.exercise_refs(message):
            new = load_exercise(message)
            if new is None:
                return "I couldn't find that exercise in your material.", None
            _session = new
            reply = await _ask_model(new, "start")
            if not reply:
                reply = (f"Exercise {new.ref} is on screen. Read the task, then tell me which law or first step "
                         f"you'd use.")
            new.transcript.append(("assistant", reply))
            return reply, new.sources

    claim = parse_claim(message) if _CLAIMS.search(message) else None
    if s is None:
        from arya.knowledge import library
        if not library.exercise_refs(message):
            return None
        if claim:                                    # "for exercise 1-13 I got v = 2.83 m/s"
            ex = load_exercise(message)
            if ex:
                return check(claim, ex.results), ex.sources
        if _ASKS_VALUE.search(message) and not _EXPLAIN.search(message):
            ex = load_exercise(message)              # "what is α2 in exercise 1-13?" — read off the answers page
            found = lookup(message, ex.results) if ex else None
            if found:
                return f"In exercise {ex.ref}, the answer sheet gives {found.spoken()}.", ex.sources
        return None

    from arya.core.llm import quick_media_command, quick_status_command
    from arya.knowledge import library
    if quick_status_command(message) or quick_media_command(message) or (_UNRELATED.search(message) and not claim):
        return None                                                  # "what time is it" mid-session
    other = library.exercise_refs(message)
    if other and s.ref not in other and not claim:
        end()                                                        # moved on to another exercise
        return None

    s.last = time.monotonic()
    if _END.match(message):
        end()
        return f"Okay, we'll stop exercise {s.ref} here. Good work!", None
    if _SOLUTION.search(message):
        reply = solution_text(s)
    elif claim:
        reply = check(claim, s.results)
        if reply.startswith("Correct"):
            reply += " What's the next step?"
    elif _HINT.search(message):
        reply = await _ask_model(s, "hint", message) or "Look at what the task gives you and which law connects it to what's asked."
    else:
        reply = await _ask_model(s, "turn", message) or "Tell me your next step and I'll check it."
    s.transcript += [("user", message), ("assistant", reply)]
    return reply, None
