"""
Maths the model shouldn't do in its head: functions, roots, solving, calculus, units.

    "100*exp(-0.3*pi/sqrt(1-0.3^2))"  -> 37.23
    "solve x^2 - 5x + 6 = 0"          -> x = 2, x = 3
    "derivative of x^3*sin(x)"        -> 3x²·sin(x) + x³·cos(x)
    "integral of x^2 from 0 to 3"     -> 9
    "90 km/h to m/s"                  -> 25 m/s

Input is checked against an allow-list (digits, operators, known function names,
single-letter variables) BEFORE SymPy parses it — SymPy's parser evaluates code,
so nothing else may reach it.
"""
from __future__ import annotations

import re

FUNCTIONS = {"sqrt", "exp", "log", "ln", "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
             "abs", "pi", "e", "E", "oo", "factorial", "floor", "ceiling"}
VARIABLES = set("abcdfghjklmnpqrstuvwxyzαβγδθλμωτζ")      # single letters (e, i reserved)
_ALLOWED_CHARS = re.compile(r"^[0-9A-Za-zαβγδθλμωτζπ_+\-*/^()., =]*$")
_IDENT = re.compile(r"[A-Za-zαβγδθλμωτζπ_]+")


class MathError(ValueError):
    """Speakable reason why an expression can't be handled."""


def _clean(expr: str) -> str:
    s = expr.strip().replace("×", "*").replace("·", "*").replace("÷", "/").replace("−", "-").replace("π", "pi")
    s = s.replace("√", "sqrt").replace("²", "^2").replace("³", "^3")
    s = re.sub(r"(\d),(\d{3})\b", r"\1\2", s)                       # 1,000 -> 1000
    s = re.sub(r"\bln\(", "log(", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\b", r"(\1/100)*", s, flags=re.IGNORECASE)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*(?:percent|%)", r"(\1/100)", s, flags=re.IGNORECASE)
    s = re.sub(r"\bsquared\b", "^2", s)
    s = re.sub(r"\bcubed\b", "^3", s)
    s = re.sub(r"\btimes\b", "*", s)
    s = re.sub(r"\b(?:divided by|over)\b", "/", s)
    s = re.sub(r"\bplus\b", "+", s)
    s = re.sub(r"\bminus\b", "-", s)
    return s


def _check(s: str) -> None:
    if not _ALLOWED_CHARS.match(s) or "__" in s or re.search(r"\.[A-Za-z_]", s):
        raise MathError("That expression has characters I can't calculate with.")
    for name in _IDENT.findall(s):
        if name not in FUNCTIONS and not (len(name) == 1 and name in VARIABLES):
            raise MathError(f"I don't know '{name}' in a calculation.")


def _sympify(s: str):
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application, parse_expr,
                                            standard_transformations)
    import sympy

    _check(s)
    local = {name: getattr(sympy, name) for name in ("sqrt", "exp", "log", "sin", "cos", "tan", "asin", "acos",
                                                     "atan", "sinh", "cosh", "tanh", "pi", "E", "oo",
                                                     "factorial", "floor", "ceiling")}
    local.update({"e": sympy.E, "abs": sympy.Abs})
    transformations = standard_transformations + (implicit_multiplication_application, convert_xor)
    return parse_expr(s, local_dict=local, global_dict={"__builtins__": {}, **_sympy_globals()},
                      transformations=transformations, evaluate=True)


def _sympy_globals() -> dict:
    import sympy
    return {"Symbol": sympy.Symbol, "Integer": sympy.Integer, "Float": sympy.Float, "Rational": sympy.Rational}


def _num(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return _pretty(x)
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
        return str(int(round(v)))
    return f"{v:.6g}"


def _pretty(x) -> str:
    import sympy
    s = sympy.sstr(x).replace("**", "^").replace("*", "·")
    return s


# ── Units ─────────────────────────────────────────────────────────────────────
# factor to the SI base unit of its kind
_UNITS = {
    "speed": {"m/s": 1, "metres per second": 1, "meters per second": 1, "km/h": 1 / 3.6, "kmh": 1 / 3.6,
              "kph": 1 / 3.6, "kilometres per hour": 1 / 3.6, "kilometers per hour": 1 / 3.6, "mph": 0.44704,
              "miles per hour": 0.44704},
    "length": {"m": 1, "metres": 1, "meters": 1, "metre": 1, "meter": 1, "cm": 0.01, "mm": 0.001, "km": 1000,
               "in": 0.0254, "inch": 0.0254, "inches": 0.0254, "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
               "mile": 1609.344, "miles": 1609.344, "µm": 1e-6, "um": 1e-6},
    "mass": {"kg": 1, "g": 0.001, "gram": 0.001, "grams": 0.001, "lb": 0.45359237, "lbs": 0.45359237,
             "pound": 0.45359237, "pounds": 0.45359237, "t": 1000, "tonne": 1000, "tonnes": 1000},
    "time": {"s": 1, "sec": 1, "second": 1, "seconds": 1, "ms": 0.001, "min": 60, "minute": 60, "minutes": 60,
             "h": 3600, "hour": 3600, "hours": 3600, "day": 86400, "days": 86400},
    "angle": {"rad": 1, "radian": 1, "radians": 1, "deg": 0.017453292519943295, "degree": 0.017453292519943295,
              "degrees": 0.017453292519943295, "°": 0.017453292519943295},
    "angular speed": {"rad/s": 1, "radians per second": 1, "rpm": 0.10471975511965977,
                      "revolutions per minute": 0.10471975511965977, "1/min": 0.10471975511965977,
                      "hz": 6.283185307179586},
    "force": {"n": 1, "newton": 1, "newtons": 1, "kn": 1000, "kilonewton": 1000, "kilonewtons": 1000},
    "power": {"w": 1, "watt": 1, "watts": 1, "kw": 1000, "kilowatt": 1000, "kilowatts": 1000,
              "hp": 745.6998715822702, "horsepower": 745.6998715822702},
    "energy": {"j": 1, "joule": 1, "joules": 1, "kj": 1000, "kwh": 3.6e6, "wh": 3600, "cal": 4.184, "kcal": 4184},
    "pressure": {"pa": 1, "kpa": 1000, "bar": 1e5, "mbar": 100, "psi": 6894.757293168, "atm": 101325},
}
_TEMPS = {"c": "C", "°c": "C", "celsius": "C", "f": "F", "°f": "F", "fahrenheit": "F", "k": "K", "kelvin": "K"}


def _unit_kind(u: str) -> tuple[str, float] | None:
    u = u.strip().lower().rstrip(".")
    for kind, table in _UNITS.items():
        if u in table:
            return kind, table[u]
    return None


def convert_units(text: str) -> str | None:
    """'90 km/h to m/s' / 'convert 3 feet into cm' -> answer, or None if it isn't a conversion."""
    m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*([a-zA-Zµ°/ ]+?)\s+(?:to|in|into)\s+([a-zA-Zµ°/ ]+?)\s*\??$",
                  text.strip().rstrip("."), re.IGNORECASE)
    if not m:
        return None
    value, src, dst = float(m.group(1).replace(",", ".")), m.group(2).strip(), m.group(3).strip()
    if src.lower() in _TEMPS and dst.lower() in _TEMPS:
        a, b = _TEMPS[src.lower()], _TEMPS[dst.lower()]
        kelvin = {"C": value + 273.15, "F": (value - 32) * 5 / 9 + 273.15, "K": value}[a]
        out = {"C": kelvin - 273.15, "F": (kelvin - 273.15) * 9 / 5 + 32, "K": kelvin}[b]
        return f"{_num(value)} {a} is {out:.4g} {b}."
    s, d = _unit_kind(src), _unit_kind(dst)
    if not s or not d:
        return None
    if s[0] != d[0]:
        raise MathError(f"I can't convert {src} ({s[0]}) into {dst} ({d[0]}).")
    out = value * s[1] / d[1]
    return f"{_num(value)} {src} is {out:.6g} {dst}."


# ── Main entry ────────────────────────────────────────────────────────────────

def evaluate(text: str) -> str:
    """One answer line for a maths request. Raises MathError with a speakable reason."""
    raw = text.strip().rstrip("?").strip()
    raw = re.sub(r"^(?:please\s+)?(?:calculate|compute|evaluate|what is|what's|convert)\s+", "", raw, flags=re.I)
    conv = convert_units(raw)
    if conv:
        return conv
    import sympy

    m = re.match(r"^(?:solve)\s+(.+?)(?:\s+for\s+([a-z]))?$", raw, re.IGNORECASE)
    if m:
        eq = _clean(m.group(1))
        sides = eq.split("=")
        if len(sides) > 2:
            raise MathError("Give one equation, like x^2 - 4 = 0.")
        expr = _sympify(sides[0]) - (_sympify(sides[1]) if len(sides) == 2 else 0)
        var = sympy.Symbol(m.group(2)) if m.group(2) else (sorted(expr.free_symbols, key=str) or [None])[0]
        if var is None:
            raise MathError("There's no unknown to solve for.")
        sols = sympy.solve(expr, var)
        if not sols:
            return f"No solution for {var}."
        return "; ".join(f"{var} = {_num(sympy.nsimplify(s) if s.is_number else s)}"
                         + (f" ≈ {float(s):.6g}" if s.is_number and not s.is_Integer and s.is_real else "")
                         for s in sols)
    m = re.match(r"^(?:the\s+)?(?:derivative|differentiate)\s+(?:of\s+)?(.+?)(?:\s+with respect to\s+([a-z]))?$",
                 raw, re.IGNORECASE)
    if m:
        f = _sympify(_clean(m.group(1)))
        var = sympy.Symbol(m.group(2)) if m.group(2) else (sorted(f.free_symbols, key=str) or [sympy.Symbol("x")])[0]
        return f"d/d{var} = {_pretty(sympy.simplify(sympy.diff(f, var)))}"
    m = re.match(r"^(?:the\s+)?(?:integral|integrate)\s+(?:of\s+)?(.+?)(?:\s+d([a-z]))?"
                 r"(?:\s+from\s+(\S+)\s+to\s+(\S+))?$", raw, re.IGNORECASE)
    if m:
        f = _sympify(_clean(m.group(1)))
        var = sympy.Symbol(m.group(2)) if m.group(2) else (sorted(f.free_symbols, key=str) or [sympy.Symbol("x")])[0]
        if m.group(3):
            a, b = _sympify(_clean(m.group(3))), _sympify(_clean(m.group(4)))
            val = sympy.integrate(f, (var, a, b))
            return f"Integral from {_num(a)} to {_num(b)} = {_num(val)}" + (
                f" ≈ {float(val):.6g}" if val.is_number and not val.is_Integer else "")
        return f"Integral = {_pretty(sympy.integrate(f, var))} + C"
    expr = _sympify(_clean(raw))
    if expr.free_symbols:
        return f"Simplified: {_pretty(sympy.simplify(expr))}"
    val = sympy.N(expr, 12)
    if not val.is_real:
        return f"Result: {_pretty(sympy.nsimplify(val, tolerance=1e-10)) if val.is_number else val}"
    return f"Result: {_num(val)}"
