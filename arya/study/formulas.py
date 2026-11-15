"""
Course formulas evaluated exactly — the numbers never come from the language model.

"What is the percent overshoot for a damping ratio of 0.3?"  ->  37.2 %, with the formula.
A small local model gets this arithmetic wrong (or invents the formula); these are
computed here and answered instantly, like the instant plots in visuals.py.
"""
from __future__ import annotations

import math
import re

_NUM = r"(-?\d+(?:\.\d+)?)"
_ZETA = re.compile(r"\b(?:zeta|ζ|damping ratio|damping)\s*(?:of|=|is|:)?\s*" + _NUM, re.IGNORECASE)
_WN = re.compile(r"\b(?:omega\s*_?n|ω\s*_?n|ωₙ|wn|natural frequency)\s*(?:of|=|is|:)?\s*" + _NUM
                 + r"\s*(?:rad/s|radians per second|1/s)?", re.IGNORECASE)
_K = re.compile(r"\b(?:k|stiffness|spring constant)\s*(?:of|=|is|:)?\s*" + _NUM, re.IGNORECASE)
_M = re.compile(r"\b(?:m|mass)\s*(?:of|=|is|:)?\s*" + _NUM, re.IGNORECASE)
_R = re.compile(r"\b(?:r|resistance|resistor)\s*(?:of|=|is|:)?\s*" + _NUM + r"\s*(k|m|meg)?\s*(?:ohms?|Ω)?",
                re.IGNORECASE)
_C = re.compile(r"\b(?:c|capacitance|capacitor)\s*(?:of|=|is|:)?\s*" + _NUM + r"\s*(m|µ|u|micro|n|nano|p|pico)?\s*f?\b",
                re.IGNORECASE)
_L = re.compile(r"\b(?:l|inductance|inductor)\s*(?:of|=|is|:)?\s*" + _NUM + r"\s*(m|milli|µ|u|micro)?\s*h?\b",
                re.IGNORECASE)
_PREFIX = {"k": 1e3, "m": 1e-3, "meg": 1e6, "µ": 1e-6, "u": 1e-6, "micro": 1e-6, "milli": 1e-3,
           "n": 1e-9, "nano": 1e-9, "p": 1e-12, "pico": 1e-12}


_SPELLED = [  # spoken / written-out units -> the short forms the regexes above read
    (r"kilo\s*-?\s*o?hms?\b", "k ohm"), (r"mega\s*-?\s*o?hms?\b|megohms?\b", "meg ohm"),
    (r"(?:micro\s*-?\s*farads?|microfarads?)\b", "u F"), (r"nano\s*-?\s*farads?\b", "n F"),
    (r"pico\s*-?\s*farads?\b", "p F"), (r"milli\s*-?\s*farads?\b", "m F"),
    (r"micro\s*-?\s*henr(?:y|ies|ys)\b", "u H"), (r"milli\s*-?\s*henr(?:y|ies|ys)\b", "m H"),
    (r"\bfarads?\b", "F"), (r"\bhenr(?:y|ies|ys)\b", "H"), (r"\bohms?\b", "ohm"),
]


def _spelled_units(text: str) -> str:
    """'10 kilohm and 100 microfarad' -> '10 k ohm and 100 u F' (previously read as 100 farad)."""
    for pattern, short in _SPELLED:
        text = re.sub(pattern, short, text, flags=re.IGNORECASE)
    return text


def _f(x: float) -> str:
    """3 significant figures, never scientific notation: 1000, 0.00125, 37.2."""
    x = float(f"{x:.3g}")
    return f"{x:.10f}".rstrip("0").rstrip(".") if abs(x) < 1e15 else f"{x:.3g}"


def _val(m: re.Match | None, *, ohms: bool = False) -> float | None:
    if not m:
        return None
    v = float(m.group(1))
    pre = (m.group(2) or "").lower() if m.lastindex and m.lastindex >= 2 else ""
    if ohms and pre == "m":                     # 1 M ohm, not milli
        pre = "meg"
    return v * _PREFIX.get(pre, 1.0)


def _time(seconds: float) -> str:
    seconds = float(f"{seconds:.6g}")                  # 0.9999999 (float noise) -> 1
    value, unit = ((seconds * 1e6, "microsecond") if seconds < 1e-3 else
                   (seconds * 1e3, "millisecond") if seconds < 1 else (seconds, "second"))
    text = _f(value)
    return f"{text} {unit}" + ("" if text == "1" else "s")


def quick_formula_answer(message: str) -> str | None:
    """A spoken answer with the exact number, or None when this isn't such a question."""
    t = message.lower()
    zeta = float(_ZETA.search(t).group(1)) if _ZETA.search(t) else None
    wn = float(_WN.search(t).group(1)) if _WN.search(t) else None

    if re.search(r"overshoot", t) and zeta is not None:
        if not 0 <= zeta < 1:
            return (f"With ζ = {_f(zeta)} there is no overshoot: only an underdamped system "
                    f"(0 < ζ < 1) overshoots.")
        os_ = 100 * math.exp(-zeta * math.pi / math.sqrt(1 - zeta ** 2))
        return (f"The percent overshoot is %OS = 100 · e^(−ζπ/√(1 − ζ²)). "
                f"With ζ = {_f(zeta)} that is about {os_:.1f} %.")
    if re.search(r"settling time", t) and zeta is not None and wn is not None and zeta > 0 and wn > 0:
        ts = 4 / (zeta * wn)
        return (f"The 2 % settling time is about t_s ≈ 4/(ζωₙ). "
                f"With ζ = {_f(zeta)} and ωₙ = {_f(wn)} rad/s that is about {_time(ts)}.")
    if re.search(r"peak time", t) and zeta is not None and wn is not None and 0 < zeta < 1 and wn > 0:
        wd = wn * math.sqrt(1 - zeta ** 2)
        return (f"The peak time is t_p = π/ω_d with ω_d = ωₙ·√(1 − ζ²) = {_f(wd)} rad/s, "
                f"so t_p is about {_time(math.pi / wd)}.")
    if re.search(r"damped (?:natural )?frequency", t) and zeta is not None and wn is not None and 0 <= zeta < 1:
        wd = wn * math.sqrt(1 - zeta ** 2)
        return f"The damped frequency is ω_d = ωₙ·√(1 − ζ²). With ζ = {_f(zeta)} and ωₙ = {_f(wn)} rad/s that is {_f(wd)} rad/s."
    if re.search(r"natural frequency", t) and wn is None:
        k, m = _val(_K.search(t)), _val(_M.search(t))
        if k and m and k > 0 and m > 0:
            w = math.sqrt(k / m)
            return (f"The natural frequency is ωₙ = √(k/m) = {_f(w)} rad/s, "
                    f"which is {_f(w / (2 * math.pi))} Hz.")
    if re.search(r"time constant", t):
        units = _spelled_units(message)
        r = _val(_R.search(units), ohms=True)
        c, l_ = _val(_C.search(units)), _val(_L.search(units))
        if r and c:
            return f"The time constant of the RC circuit is τ = R·C = {_time(r * c)}."
        if r and l_:
            return f"The time constant of the RL circuit is τ = L/R = {_time(l_ / r)}."
    return formula_card(message)


# ── Formula cards (no numbers given) ──────────────────────────────────────────
# The checked statements from core_notes.md, answered without the model — it kept
# inventing an overshoot formula even with the notes in front of it.

CARDS = [
    (r"(?:percent |% ?)?overshoot",
     "The percent overshoot of an underdamped second-order system is %OS = 100 · e^(−ζπ/√(1 − ζ²)), "
     "for 0 < ζ < 1. It depends only on the damping ratio ζ: ζ = 0.3 gives about 37 %, ζ = 0.7 about 4.6 %."),
    (r"settling time",
     "The 2 % settling time of a second-order system is t_s ≈ 4/(ζωₙ), where ζ is the damping ratio and ωₙ "
     "the natural frequency; for a first-order system it is about 4τ."),
    (r"peak time",
     "The peak time is t_p = π/ω_d, with the damped frequency ω_d = ωₙ·√(1 − ζ²)."),
    (r"damped (?:natural )?frequency",
     "The damped frequency is ω_d = ωₙ·√(1 − ζ²): the frequency the underdamped response actually oscillates at."),
    (r"rise time",
     "For a first-order system the 10–90 % rise time is about 2.2τ. For a second-order system there is no simple "
     "exact formula; it shrinks as ωₙ grows."),
    (r"natural frequency of (?:a |the )?(?:mass[- ]spring|spring[- ]mass)",
     "The natural frequency of a mass-spring system is ωₙ = √(k/m), in rad/s, where k is the stiffness and m the "
     "mass; in hertz it is f = ωₙ/(2π)."),
    (r"(?:transfer function|step response) of (?:a |the )?first[- ]order",
     "A first-order system has G(s) = K/(τs + 1), with gain K and time constant τ. Its step response is "
     "y(t) = K(1 − e^(−t/τ)), reaching 63.2 % at t = τ."),
    (r"(?:transfer function|characteristic equation) of (?:a |the )?second[- ]order",
     "A second-order system has G(s) = K·ωₙ²/(s² + 2ζωₙ s + ωₙ²), with damping ratio ζ and natural frequency ωₙ; "
     "its poles are s = −ζωₙ ± jω_d."),
    (r"impedance of (?:a |an |the )?capacitor",
     "The impedance of a capacitor is Z = 1/(jωC): its magnitude 1/(ωC) falls as the frequency rises, "
     "and the current leads the voltage by 90°."),
    (r"impedance of (?:a |an |the )?(?:inductor|coil)",
     "The impedance of an inductor is Z = jωL: its magnitude ωL grows with frequency, "
     "and the current lags the voltage by 90°."),
    (r"impedance of (?:a |an |the )?resistor",
     "The impedance of a resistor is simply Z = R, the same at every frequency."),
    (r"phase margin",
     "The phase margin is PM = 180° + ∠G(jω_gc), measured at the gain-crossover frequency ω_gc where |G| = 0 dB: "
     "how far the phase is from −180° there. A typical design target is 45° to 60°."),
    (r"gain margin",
     "The gain margin is GM = −|G(jω_pc)| in dB, measured at the phase-crossover frequency ω_pc where the phase "
     "is −180°: how much the gain can rise before the loop becomes unstable."),
    (r"time constant of (?:an |the )?rc",
     "The time constant of an RC circuit is τ = R·C (ohms times farads gives seconds)."),
    (r"time constant of (?:an |the )?rl",
     "The time constant of an RL circuit is τ = L/R (henries over ohms gives seconds)."),
]
_ASKS_FORMULA = re.compile(r"\b(?:formula|equation|expression|how (?:do|can|to|would) (?:i |you |we )?"
                           r"(?:calculate|compute|find|get)|what(?:'s| is| are) the)\b", re.IGNORECASE)
_WANTS_EXPLANATION = re.compile(r"\b(?:why|explain|intuition|meaning|means|derive|derivation|difference|compare)\b",
                                re.IGNORECASE)


def formula_card(message: str) -> str | None:
    """'What is the formula for percent overshoot?' -> the checked formula; None for anything else."""
    if not _ASKS_FORMULA.search(message) or _WANTS_EXPLANATION.search(message):
        return None
    t = message.lower()
    for pattern, card in CARDS:
        if re.search(pattern, t):
            return card
    return None
