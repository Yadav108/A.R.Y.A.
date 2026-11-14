"""
Maths that reads well on screen and sounds right when spoken.

Small models write formulas in LaTeX ("\\(\\zeta\\)", "\\frac{k}{m}") even when
asked not to. Read aloud, that becomes "backslash open paren zeta…".

  latex_to_text()  LaTeX -> readable Unicode for the screen and the history:
                   \\(\\zeta\\) -> ζ,  \\frac{k}{m} -> k/m,  \\omega_n^2 -> ωₙ²,  \\pm -> ±
  speakable()      that text -> words for the voice:
                   "ωₙ = √(k/m)" -> "omega n equals the square root of k over m"
"""
from __future__ import annotations

import re

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν",
    "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
_SYMBOLS = {
    "pm": "±", "mp": "∓", "cdot": "·", "times": "×", "div": "÷", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "sim": "~", "infty": "∞", "to": "→", "rightarrow": "→",
    "Rightarrow": "⇒", "leftarrow": "←", "implies": "⇒", "propto": "∝", "partial": "∂", "nabla": "∇",
    "degree": "°", "circ": "°", "int": "∫", "sum": "Σ", "prod": "Π", "ldots": "…", "cdots": "…", "dots": "…",
}
_SUP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")
_SUP_OK = set("0123456789+-=()n")
_SUB_OK = set("0123456789+-=()aehijklmnoprstuvx")


def _group(s: str, i: int) -> tuple[str, int]:
    """The {…} group (or single char) starting at s[i]; returns (content, index after)."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return "", i
    if s[i] != "{":
        if s[i] == "\\":                                    # \alpha as an argument
            m = re.match(r"\\[A-Za-z]+", s[i:])
            if m:
                return m.group(0), i + m.end()
        return s[i], i + 1
    depth, j = 0, i
    while j < len(s):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    return s[i + 1:], len(s)


def _wrap(x: str) -> str:
    """a/b needs brackets around anything longer than one term."""
    x = x.strip()
    return x if re.fullmatch(r"[\w.ⁿ²³⁰¹⁴-⁹₀-₉ₐ-ₜ]+|\(.*\)|√\(.*\)", x) else f"({x})"


def _script(content: str, table, ok: set, mark: str) -> str:
    content = content.strip()
    if content and all(ch in ok for ch in content):
        return content.translate(table)
    return f"{mark}({content})" if len(content) > 1 else f"{mark}{content}"


def _convert(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            m = re.match(r"\\([A-Za-z]+)", s[i:])
            if not m:                                       # \, \; \! \{ \} \\
                nxt = s[i + 1:i + 2]
                out.append(nxt if nxt in "{}%$#&_" else " ")
                i += 2
                continue
            name, i = m.group(1), i + m.end()
            if name in ("frac", "dfrac", "tfrac"):
                a, i = _group(s, i)
                b, i = _group(s, i)
                out.append(f"{_wrap(_convert(a))}/{_wrap(_convert(b))}")
            elif name == "sqrt":
                if i < len(s) and s[i] == "[":                 # \sqrt[3]{x}
                    j = s.find("]", i)
                    root, i = s[i + 1:j], j + 1
                    a, i = _group(s, i)
                    out.append(f"{root}√({_convert(a)})")
                else:
                    a, i = _group(s, i)
                    body = _convert(a).strip()
                    out.append(f"√{body}" if re.fullmatch(r"\w", body) else f"√({body})")
            elif name in ("text", "mathrm", "mathbf", "mathit", "operatorname", "textbf", "textit", "mbox", "boldsymbol"):
                a, i = _group(s, i)
                out.append(_convert(a))
            elif name in ("dot", "ddot", "hat", "bar", "vec", "tilde"):
                a, i = _group(s, i)
                mark = {"dot": "̇", "ddot": "̈", "hat": "̂", "bar": "̄", "vec": "⃗",
                        "tilde": "̃"}[name]
                out.append(_convert(a) + mark)
            elif name in ("left", "right", "big", "Big", "bigg", "Bigg", "displaystyle", "quad", "qquad", "limits"):
                out.append(" " if name in ("quad", "qquad") else "")
            elif name in GREEK:
                out.append(GREEK[name])
            elif name in _SYMBOLS:
                out.append(_SYMBOLS[name])
            elif name in ("sin", "cos", "tan", "exp", "ln", "log", "lim", "max", "min", "det", "arctan", "sinh",
                          "cosh", "tanh", "Re", "Im"):
                out.append(name)
            else:
                out.append(name)                             # unknown command: keep its name, drop the slash
            continue
        if ch in "^_":
            a, i = _group(s, i + 1)
            inner = _convert(a)
            out.append(_script(inner, _SUP, _SUP_OK, "^") if ch == "^" else _script(inner, _SUB, _SUB_OK, "_"))
            continue
        if ch in "{}":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_DELIMS = re.compile(r"\\\(|\\\)|\\\[|\\\]|\$\$")


def latex_to_text(text: str) -> str:
    """Readable Unicode maths for the screen. Text without LaTeX comes back unchanged."""
    if "\\" not in text and "$$" not in text and not re.search(r"\w\^\{|_\{", text):
        return text
    # inline $…$ only when it clearly holds maths (a backslash command or ^/_), never "$5"
    text = re.sub(r"\$([^$\n]*?(?:\\[A-Za-z]+|[\^_])[^$\n]*?)\$", r"\1", text)
    text = _DELIMS.sub("", text)
    text = _convert(text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r" +([,.;:!?])", r"\1", text)


# ── Spoken form ───────────────────────────────────────────────────────────────

_GREEK_NAMES = {v: k for k, v in GREEK.items() if k.islower() and not k.startswith("var")}
_GREEK_NAMES.update({"Δ": "delta", "Σ": "sigma", "Ω": "omega", "Φ": "phi", "Ψ": "psi", "Γ": "gamma",
                     "Θ": "theta", "Λ": "lambda", "Π": "pi", "Ξ": "xi"})
_SUB_WORDS = dict(zip("₀₁₂₃₄₅₆₇₈₉ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
                      ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"] + list("aehijklmnoprstuvx")))
_SUP_DIGITS = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789"))
_UNITS = r"(?:s|ms|min|h|hr|m|cm|mm|km|kg|g|N|Nm|J|W|kW|V|A|Hz|rad|deg|°|m²|s²|m2|s2|sec)"
_WORD_SYMBOLS = [
    ("±", " plus or minus "), ("∓", " minus or plus "), ("≈", " approximately "), ("≤", " less than or equal to "),
    ("≥", " greater than or equal to "), ("≠", " is not equal to "), ("→", " goes to "), ("⇒", " which means "),
    ("∞", " infinity "), ("∝", " is proportional to "), ("∂", " partial "), ("·", " times "), ("×", " times "),
    ("÷", " divided by "), ("∫", " the integral of "), ("…", " "), ("−", "-"),
    ("∠", " the angle of "), ("°", " degrees "),
]


def _power_words(p: str) -> str:
    return {"2": " squared", "3": " cubed", "-1": " inverse", "n": " to the n"}.get(p, f" to the power {p}")


def _balanced_powers(t: str) -> str:
    """'e^(-ζπ/√(1-ζ²))' -> 'e to the power of (-ζπ/√(1-ζ²))' — brackets may nest."""
    out, i = [], 0
    while i < len(t):
        if t[i] == "^" and t[i + 1:i + 2] == "(":
            depth, j = 0, i + 1
            while j < len(t):
                depth += t[j] == "("
                depth -= t[j] == ")"
                if depth == 0:
                    break
                j += 1
            out.append(f" to the power of ({t[i + 2:j]}) ")
            i = j + 1
            continue
        out.append(t[i])
        i += 1
    return "".join(out)


_UNIT_WORDS = {"s": "second", "sec": "second", "ms": "millisecond", "min": "minute", "h": "hour", "hr": "hour",
               "m": "metre", "cm": "centimetre", "mm": "millimetre", "km": "kilometre", "kg": "kilogram",
               "g": "gram", "N": "newton", "Nm": "newton metre", "J": "joule", "W": "watt", "kW": "kilowatt",
               "V": "volt", "A": "amp", "Hz": "hertz", "rad": "radian", "deg": "degree"}


def _unit_rate(m: re.Match) -> str:
    """'9.4 km/h' -> '9.4 kilometres per hour', '2.55 m/s2' -> '2.55 metres per second squared'."""
    num, top, bottom, power = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    plural = "" if num.strip() in ("1", "1.0") else "s"
    top_w = _UNIT_WORDS[top] + plural
    sq = " squared" if power in ("2", "²") else " cubed" if power in ("3", "³") else ""
    return f"{num} {top_w} per {_UNIT_WORDS[bottom]}{sq}"


def speakable(text: str) -> str:
    """Words for the voice: symbols, Greek letters, powers, fractions and roots spelled out."""
    t = latex_to_text(text)
    units = "|".join(sorted(map(re.escape, _UNIT_WORDS), key=len, reverse=True))
    t = re.sub(rf"(\d+(?:\.\d+)?)\s*({units})\s*/\s*({units})(²|³|2|3)?\b", _unit_rate, t)
    if not re.search(r"[=<>^/+√±≈≤≥≠→·×∞°∠Ͱ-Ͽ⁰-₟]|\b\w_\w|(?<=\w)\*(?=\w)|\bsqrt\b", t):
        return t
    t = re.sub(r"\bsqrt\b\s*", "√", t)
    t = re.sub(r"\b([A-Za-z])\(([A-Za-z])\)", r"\1 of \2", t)         # x(t) -> x of t, G(s) -> G of s
    t = _balanced_powers(t)
    # Greek letters (keep words apart: "ζωₙ" -> "zeta omega n")
    t = "".join(f" {_GREEK_NAMES[c]} " if c in _GREEK_NAMES else c for c in t)
    # superscripts: "x²" -> "x squared", "e⁻¹" -> "e inverse"
    t = re.sub(r"[⁺⁻]?[⁰¹²³⁴⁵⁶⁷⁸⁹ⁿ]+",
               lambda m: _power_words(m.group(0).replace("⁻", "-").replace("⁺", "").replace("ⁿ", "n")
                                      .translate(str.maketrans(_SUP_DIGITS))), t)
    t = re.sub(r"\^\(([^()]*)\)", lambda m: f" to the power {m.group(1)} ", t)
    t = re.sub(r"\^\s*(-?\w+)", lambda m: _power_words(m.group(1)), t)
    # subscripts: "ωₙ" -> "omega n", "z_c" -> "z c", "v_max" -> "v max"
    t = "".join(f" {_SUB_WORDS[c]}" if c in _SUB_WORDS else c for c in t)
    t = re.sub(r"_\(([^()]*)\)|_(\w+)", lambda m: " " + (m.group(1) or m.group(2)), t)
    # roots
    t = re.sub(r"(\d)√", r"\1 root of ", t)
    t = t.replace("√", " the square root of ")
    # fractions: "k/m" -> "k over m", "(a+b)/(c)" -> "a+b over c"
    t = re.sub(r"(?<=[\w)\s])\s*/\s*(?=[\w(√ ])", " over ", t)
    # operators
    t = re.sub(r"(?<=[\w)\s])\s*\*\s*(?=[\w(])", " times ", t)
    for sym, words in _WORD_SYMBOLS:
        t = t.replace(sym, words)
    t = re.sub(r"\s*=\s*", " equals ", t)
    t = re.sub(r"\s*<\s*", " is less than ", t)
    t = re.sub(r"\s*>\s*", " is greater than ", t)
    t = re.sub(r"(?<=[\w)\s])\s*\+\s*", " plus ", t)
    t = re.sub(r"(?:(?<=equals )|(?<=^)|(?<=\()|(?<=plus or minus ))\s*-\s*(?=[\w(])", "minus ", t)
    t = re.sub(r"(?<=[\w)])\s+-\s+(?=[\w(])", " minus ", t)            # "a - b"
    t = re.sub(r"(?<=[a-zA-Z)])-(?=\d)", " minus ", t)                   # "s-2"
    t = re.sub(r"(?<=\d)\s*-\s*(?=[\w(])", " minus ", t)                # "1-ζ²", "4 - x"
    t = re.sub(r"[()\[\]{}]", " ", t)                                    # brackets aren't spoken
    return re.sub(r"\s+", " ", t).strip()
