"""
Visuals ARYA can show in the app window: plots and diagrams.

  * plot_function — y = f(x) curves from a safe maths expression (no eval:
    only numbers, x/t, + - * / ** and functions like sin, exp, sqrt).
  * plot_system   — step / impulse / Bode plots of a transfer function, or a
    standard first-/second-order system, with the key numbers computed
    exactly (overshoot, rise/settling time, bandwidth) so ARYA can state
    them accurately instead of guessing.
  * diagram       — block diagrams and flowcharts from "A -> B" connections;
    the app draws them (Mermaid, bundled locally).

Images are kept in memory (last 40) — nothing is written to disk.
"""
from __future__ import annotations

import ast
import io
import math
import re
import threading
import uuid
from collections import OrderedDict

import numpy as np

_images: "OrderedDict[str, bytes]" = OrderedDict()

BG, PANEL, FG, DIM, GRID = "#0b0d14", "#11131c", "#e8eaf2", "#9aa2b6", "#262a38"
COLORS = ["#818cf8", "#2dd4bf", "#f59e0b", "#f472b6", "#60a5fa", "#a3e635"]


def image(image_id: str) -> bytes | None:
    return _images.get(image_id)


def _store(png: bytes) -> str:
    image_id = uuid.uuid4().hex[:12]
    _images[image_id] = png
    if len(_images) > 40:
        _images.popitem(last=False)
    return image_id


_mpl_lock = threading.Lock()


def _pyplot():
    """Import matplotlib once, under a lock: two answers drawing at the same moment in different threads
    could otherwise both run its first import and fail ("partially initialized module 'matplotlib'")."""
    with _mpl_lock:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    return plt


def _figure(title: str, xlabel: str, ylabel: str):
    plt = _pyplot()

    fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=130)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PANEL)
    for side in ax.spines.values():
        side.set_color(GRID)
    ax.tick_params(colors=DIM, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.7)
    ax.set_title(title, color=FG, fontsize=12, pad=10)
    ax.set_xlabel(xlabel, color=DIM, fontsize=10)
    ax.set_ylabel(ylabel, color=DIM, fontsize=10)
    return fig, ax, plt


def _png(fig, plt) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


# ── Safe expressions ──────────────────────────────────────────────────────────

_FUNCS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
    "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan, "sinh": np.sinh, "cosh": np.cosh,
    "tanh": np.tanh, "exp": np.exp, "log": np.log, "ln": np.log, "log10": np.log10, "sqrt": np.sqrt,
    "abs": np.abs, "sign": np.sign, "floor": np.floor, "ceil": np.ceil,
    "step": lambda v: np.heaviside(v, 1.0), "heaviside": lambda v: np.heaviside(v, 1.0),
    "sigmoid": lambda v: 1 / (1 + np.exp(-v)), "relu": lambda v: np.maximum(0, v),
}
_CONSTS = {"pi": math.pi, "e": math.e, "g": 9.81}
_BINOPS = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide, ast.Pow: np.power}


def _compile(expr: str):
    """Expression -> (function of x, variable name). Raises ValueError if unsafe."""
    text = expr.strip().lower().replace("^", "**").replace("×", "*").replace("·", "*")
    text = re.sub(r"^\s*[a-z]\s*\(\s*[a-z]\s*\)\s*=\s*|^\s*y\s*=\s*", "", text)   # "f(x) = …", "y = …"
    text = re.sub(r"(\d)\s*([a-z(])", r"\1*\2", text)                              # 2x -> 2*x, 3(…) -> 3*(…)
    tree = ast.parse(text, mode="eval")
    var = "t" if re.search(r"\bt\b", text) and not re.search(r"\bx\b", text) else "x"

    def ev(node, x):
        if isinstance(node, ast.Expression):
            return ev(node.body, x)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == var:
                return x
            if node.id in _CONSTS:
                return _CONSTS[node.id]
            raise ValueError(f"unknown name '{node.id}'")
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left, x), ev(node.right, x))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand, x)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and len(node.args) == 1:
            return _FUNCS[node.func.id](ev(node.args[0], x))
        raise ValueError("only numbers, x/t, + - * / ** and maths functions are allowed")

    ev(tree, np.array([0.5]))           # validate now, with a sample value
    return (lambda x: ev(tree, x)), var


def plot_function(expressions: list[str], x_min: float = -10, x_max: float = 10, title: str = "") -> dict:
    exprs = [e for e in (expressions if isinstance(expressions, list) else [expressions]) if str(e).strip()][:6]
    if not exprs:
        raise ValueError("Give at least one expression, like sin(x) or exp(-0.5*t)*cos(3*t).")
    x_min, x_max = float(x_min), float(x_max)
    if not x_max > x_min:
        x_min, x_max = -10.0, 10.0
    for e in exprs:
        if re.search(r"(?<![a-z])s(?![a-z])", str(e).lower()):
            raise ValueError("That's a transfer function in s — use plot_system with numerator and denominator "
                             "coefficients (or natural_frequency and damping_ratio) instead.")
    compiled = [(_compile(str(e)), str(e)) for e in exprs]
    var = compiled[0][0][1]
    x = np.linspace(x_min, x_max, 800)
    fig, ax, plt = _figure(title or ", ".join(exprs), var, "value")
    facts = []
    with np.errstate(all="ignore"):
        for i, ((f, _), label) in enumerate(compiled):
            y = np.asarray(f(x), dtype=float) * np.ones_like(x)
            y[~np.isfinite(y)] = np.nan
            ax.plot(x, y, color=COLORS[i % len(COLORS)], linewidth=2, label=label)
            if np.isfinite(y).any():
                facts.append(f"{label}: ranges from {np.nanmin(y):.3g} to {np.nanmax(y):.3g} on [{x_min:g}, {x_max:g}]")
    ax.axhline(0, color=GRID, linewidth=1)
    if len(compiled) > 1:
        ax.legend(facecolor=PANEL, edgecolor=GRID, labelcolor=FG, fontsize=9)
    image_id = _store(_png(fig, plt))
    return {"type": "image", "id": image_id, "caption": title or "Plot of " + ", ".join(exprs), "facts": facts}


# ── Systems ───────────────────────────────────────────────────────────────────

_TERM = re.compile(r"([+-]?)\s*(\d*\.?\d*)\s*\*?\s*(s(?:\s*(?:\^|\*\*)\s*(\d+))?)?")


def parse_poly(text: str) -> list[float]:
    """'s^2 + 2s + 10' / 's**2+0.6*s+4' / '0.5s+1' / '10' -> coefficients, highest power first."""
    t = text.replace(" ", "").replace("²", "^2").replace("³", "^3").lower()
    if not t or re.search(r"[^0-9s.+\-*^]", t):
        raise ValueError(f"can't read the polynomial '{text}'")
    powers: dict[int, float] = {}
    for sign, num, s_part, power in _TERM.findall(t):
        if not num and not s_part:
            continue
        coeff = float(num) if num not in ("", ".") else 1.0
        coeff = -coeff if sign == "-" else coeff
        p = (int(power) if power else 1) if s_part else 0
        powers[p] = powers.get(p, 0.0) + coeff
    if not powers:
        raise ValueError(f"can't read the polynomial '{text}'")
    return [powers.get(p, 0.0) for p in range(max(powers), -1, -1)]


def parse_transfer_function(text: str) -> tuple[list[float], list[float]] | None:
    """'10/(s^2+2s+10)', '(s+1)/(s^2+3s+2)', '1/(0.5s+1)' -> (numerator, denominator)."""
    m = re.search(r"(\([^()]*\)|[\d.]+\s*s?(?:\^\d)?)\s*/\s*\(([^()]*s[^()]*)\)", text.replace("**", "^"))
    if not m:
        return None
    try:
        return parse_poly(m.group(1).strip("()")), parse_poly(m.group(2))
    except ValueError:
        return None


_NUM = r"\s*(?:of|=|is|:|at)?\s*(-?\d*\.?\d+)"


def quick_system_request(message: str) -> dict | None:
    """Plot requests that already contain the system's numbers are handled without
    the model: 'step response with damping ratio 0.3 and natural frequency 2',
    'bode plot of 10/(s^2+2s+10)', 'first order system with time constant 0.5 s'."""
    text = message.lower().replace("ζ", "zeta ").replace("ω", "omega ").replace("τ", "tau ")
    if not re.search(r"\b(plot|show|draw|graph|sketch|visuali[sz]e|response|bode)\b", text):
        return None
    if not re.search(r"\b(step|impulse|bode|response)\b", text):
        return None
    kind = "bode" if "bode" in text else "impulse" if "impulse" in text else "step"
    args: dict = {"kind": kind}
    tf = parse_transfer_function(message)
    if tf:
        args["numerator"], args["denominator"] = tf
        return args
    z = re.search(r"\b(?:damping ratio|damping|zeta)" + _NUM, text)
    wn = re.search(r"\b(?:natural frequency|omega\s*n|omega_n|wn|ωn)" + _NUM, text)
    tau = re.search(r"\b(?:time constant|tau)" + _NUM, text)
    k = re.search(r"\b(?:gain|k)" + _NUM, text)
    if z and wn:
        args["damping_ratio"], args["natural_frequency"] = float(z.group(1)), float(wn.group(1))
    elif tau:
        args["time_constant"] = float(tau.group(1))
    else:
        return None
    if k:
        args["gain"] = float(k.group(1))
    return args


_WORDS = [
    (r"\bsquared\b", "^2"), (r"\bcubed\b", "^3"), (r"\bto the power of\b", "^"), (r"\bminus\b", "-"),
    (r"\bplus\b", "+"), (r"\btimes\b", "*"), (r"\bdivided by\b", "/"), (r"\bover\b", "/"),
    (r"\be to the\b", "exp"), (r"\bsquare root of\b", "sqrt"), (r"\bnatural log of\b", "log"),
]
_FUNC_NAMES = "sin|cos|tan|asin|acos|atan|arcsin|arccos|arctan|sinh|cosh|tanh|exp|log|ln|log10|sqrt|abs|sign|sigmoid|relu|step"


def _normalise_expr(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"^(?:the\s+)?(?:functions?\s+|graph of\s+|curve of\s+|plot of\s+)?", "", t)
    t = re.sub(r"^(?:y|f\s*\(\s*[xt]\s*\))\s*=\s*", "", t)
    for pat, rep in _WORDS:
        t = re.sub(pat, rep, t)
    # "sin x" / "sin 2x" / "exp -t" -> sin(x), sin(2x), exp(-t)
    t = re.sub(rf"\b({_FUNC_NAMES})\s+(-?[\d.]*\s*[xt])\b", r"\1(\2)", t)
    return re.sub(r"\s+", "", t)


def _constant(text: str) -> float:
    t = text.lower().replace("π", "pi").strip()
    t = re.sub(r"(\d)\s*pi", r"\1*pi", t).replace(" ", "")
    f, _ = _compile(t if t else "0")
    return float(np.asarray(f(np.array([0.0]))).ravel()[0])


def quick_function_request(message: str) -> dict | None:
    """'Plot sin x and cos x from 0 to 2 pi' -> plot_function arguments, or None
    when it isn't a clear function plot (then the model handles it)."""
    m = re.match(r"^\W*(?:please\s+)?(?:can you\s+)?(?:plot|graph|draw|sketch)\s+(?:me\s+)?(.+?)\s*$",
                 message.strip().rstrip("?.!"), re.IGNORECASE)
    if not m:
        return None
    body = m.group(1)
    x_min = x_max = None
    rng = re.search(r"\s+(?:from|between|for\s+[xt]\s+from|for\s+[xt]\s+between)\s+(.+?)\s+(?:to|and)\s+(.+)$", body, re.IGNORECASE)
    if rng:
        try:
            x_min, x_max = _constant(rng.group(1)), _constant(rng.group(2))
        except Exception:
            return None
        body = body[:rng.start()]
    parts = [p for p in re.split(r"\s*,\s*|\s+and\s+", body) if p.strip()]
    if not parts or len(parts) > 6:
        return None
    exprs = []
    for part in parts:
        expr = _normalise_expr(part)
        try:
            _compile(expr)
        except Exception:
            return None
        exprs.append(expr)
    if x_min is None or x_max is None or not x_max > x_min:
        trig = any(re.search(r"\b(sin|cos|tan)\b", e) for e in exprs)
        x_min, x_max = (-2 * math.pi, 2 * math.pi) if trig else (-10.0, 10.0)
    return {"expressions": exprs, "x_min": x_min, "x_max": x_max}


def spoken_summary(result: dict) -> str:
    """'Here's the step response — overshoot about 37 percent, …' from computed facts."""
    if result.get("type") == "diagram":
        kind = "flowchart" if "flowchart TD" in result.get("mermaid", "") else "block diagram"
        name = (result.get("caption") or "").strip()
        if not name or name.lower() == "diagram":
            return f"Here's the {kind}, on screen."
        # "Temperature Control Loop" -> "temperature control loop"; acronyms (PID, DC) stay as they are.
        name = " ".join(w.lower() if w[:1].isupper() and w[1:].islower() else w for w in name.split())
        return f"Here's the {kind} of the {name}, on screen."
    if result.get("caption", "").startswith("Plot of"):
        names = result["caption"][len("Plot of "):].split(", ")
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        return f"Here's the plot of {listed}, on screen."
    facts = [f for f in result.get("facts", []) if not f.startswith("poles")]
    key = [f for f in facts if f.startswith(("overshoot", "no overshoot", "2 % settling", "-3 dB bandwidth",
                                             "the output grows"))]
    facts = (key or facts)[:2]
    head = f"Here's the {result['caption'].split(' — ')[0].lower()}, on screen"
    if not facts:
        return head + "."
    text = "; ".join(facts).replace("≈", "about").replace("%", " percent").replace("10–90", "10 to 90")
    text = re.sub(r"(\d) s\b", r"\1 seconds", text)
    text = re.sub(r"(\d) rad/s\b", r"\1 radians per second", text)
    return re.sub(r"\s{2,}", " ", f"{head}: {text}.")

def _tf(numerator, denominator, natural_frequency, damping_ratio, time_constant, gain):
    k = float(gain) if gain not in (None, "") else 1.0
    if numerator and denominator:
        num = [float(v) for v in numerator]
        den = [float(v) for v in denominator]
        name = f"G(s) = ({_poly(num)}) / ({_poly(den)})"
    elif natural_frequency not in (None, "") and damping_ratio not in (None, ""):
        wn, z = float(natural_frequency), float(damping_ratio)
        num, den = [k * wn * wn], [1.0, 2 * z * wn, wn * wn]
        name = f"2nd order, ωn = {wn:g} rad/s, ζ = {z:g}" + (f", K = {k:g}" if k != 1 else "")
    elif time_constant not in (None, ""):
        tau = float(time_constant)
        num, den = [k], [tau, 1.0]
        name = f"1st order, τ = {tau:g} s" + (f", K = {k:g}" if k != 1 else "")
    else:
        raise ValueError("Give numerator and denominator coefficients, or natural_frequency + damping_ratio, "
                         "or time_constant.")
    if not any(den):
        raise ValueError("The denominator can't be zero.")
    return num, den, name


def _poly(coeffs: list[float]) -> str:
    n = len(coeffs) - 1
    terms = []
    for i, c in enumerate(coeffs):
        p = n - i
        if c == 0:
            continue
        c_txt = "" if (abs(c) == 1 and p > 0) else f"{abs(c):g}"
        s = "s" if p == 1 else (f"s^{p}" if p > 1 else "")
        terms.append(("-" if c < 0 else "+") + c_txt + s)
    out = "".join(terms).lstrip("+")
    return out or "0"


def plot_system(kind: str = "step", numerator=None, denominator=None, natural_frequency=None,
                damping_ratio=None, time_constant=None, gain=None) -> dict:
    from scipy import signal

    kind = (kind or "step").lower()
    num, den, name = _tf(numerator, denominator, natural_frequency, damping_ratio, time_constant, gain)
    system = signal.lti(num, den)
    facts = []
    poles = np.roots(den)
    stable = bool(np.all(np.real(poles) < 0))
    facts.append(f"poles: {', '.join(_cplx(p) for p in poles)} — {'stable' if stable else 'NOT stable'}")

    if kind == "bode":
        w = np.logspace(-2, 3, 600)
        w, mag, phase = signal.bode(system, w)
        plt = _pyplot()

        fig, (a1, a2) = plt.subplots(2, 1, figsize=(7.2, 5.2), dpi=130, sharex=True)
        fig.patch.set_facecolor(BG)
        for ax in (a1, a2):
            ax.set_facecolor(PANEL)
            for side in ax.spines.values():
                side.set_color(GRID)
            ax.tick_params(colors=DIM, labelsize=9)
            ax.grid(True, which="both", color=GRID, linewidth=0.6)
        a1.semilogx(w, mag, color=COLORS[0], linewidth=2)
        a1.set_ylabel("magnitude (dB)", color=DIM)
        a1.set_title(f"Bode plot — {name}", color=FG, fontsize=12)
        a2.semilogx(w, phase, color=COLORS[1], linewidth=2)
        a2.set_ylabel("phase (°)", color=DIM)
        a2.set_xlabel("ω (rad/s)", color=DIM)
        dc = mag[0]
        below = np.where(mag <= dc - 3)[0]
        if below.size:
            facts.append(f"-3 dB bandwidth ≈ {w[below[0]]:.3g} rad/s")
        facts.append(f"low-frequency gain ≈ {dc:.1f} dB")
        image_id = _store(_png(fig, plt))
        return {"type": "image", "id": image_id, "caption": f"Bode plot — {name}", "facts": facts}

    t_end = _settle_guess(poles)
    t = np.linspace(0, t_end, 1200)
    if kind == "impulse":
        t, y = signal.impulse(system, T=t)
        label = "Impulse response"
    else:
        t, y = signal.step(system, T=t)
        label = "Step response"
        dc_gain = num[-1] / den[-1] if den[-1] != 0 else None
        facts += _step_facts(t, y, stable, dc_gain)
    fig, ax, plt = _figure(f"{label} — {name}", "time (s)", "output")
    ax.plot(t, y, color=COLORS[0], linewidth=2)
    if kind != "impulse" and stable:
        final = y[-1]
        ax.axhline(final, color=COLORS[2], linewidth=1, linestyle="--")
    image_id = _store(_png(fig, plt))
    return {"type": "image", "id": image_id, "caption": f"{label} — {name}", "facts": facts}


def _cplx(p: complex) -> str:
    p = complex(p)
    if abs(p.imag) < 1e-9:
        return f"{p.real:.3g}"
    return f"{p.real:.3g} {'+' if p.imag >= 0 else '-'} {abs(p.imag):.3g}j"


def _settle_guess(poles) -> float:
    re_parts = [abs(np.real(p)) for p in poles if np.real(p) < -1e-9]
    if not re_parts:
        return 20.0
    return float(min(80.0, max(1.0, 9.0 / min(re_parts))))


def _step_facts(t, y, stable: bool, dc_gain: float | None = None) -> list[str]:
    if not stable or not np.isfinite(y).all():
        return ["the output grows without bound"]
    final = float(dc_gain) if dc_gain is not None else float(y[-1])   # exact steady state
    facts = [f"final value ≈ {final:.3g}"]
    if abs(final) < 1e-12:
        return facts
    peak = float(np.max(y)) if final > 0 else float(np.min(y))
    overshoot = max(0.0, (peak - final) / abs(final) * 100)
    facts.append(f"overshoot ≈ {overshoot:.1f} %" if overshoot > 0.05 else "no overshoot")
    if overshoot > 0.05:
        facts.append(f"peak time ≈ {t[int(np.argmax(y if final > 0 else -y))]:.3g} s")
    try:
        t10 = t[np.where(y >= 0.1 * final)[0][0]]
        t90 = t[np.where(y >= 0.9 * final)[0][0]]
        facts.append(f"rise time (10–90 %) ≈ {t90 - t10:.3g} s")
    except IndexError:
        pass
    outside = np.where(np.abs(y - final) > 0.02 * abs(final))[0]
    if outside.size:
        facts.append(f"2 % settling time ≈ {t[min(outside[-1] + 1, len(t) - 1)]:.3g} s")
    return facts


# ── Diagrams ──────────────────────────────────────────────────────────────────

_ARROW = re.compile(r"\s*(?:-->|->|→|=>)\s*|\s+to\s+", re.IGNORECASE)


def diagram(connections: list[str], title: str = "", direction: str = "LR") -> dict:
    """["Setpoint -> Controller", "Controller -> Motor : voltage", "Encoder -> Controller : feedback"]
    -> a Mermaid flowchart the app renders. Labels are sanitised; no raw Mermaid from the model."""
    items = connections if isinstance(connections, list) else re.split(r"[;\n]+", str(connections))
    ids: dict[str, str] = {}
    edges = []

    def node(name: str) -> str:
        name = re.sub(r"[\"<>{}\[\]()|#`;]", "", name).strip()[:40] or "?"
        if name not in ids:
            ids[name] = f"n{len(ids)}"
        return ids[name]

    for raw in items[:30]:
        raw = str(raw)
        label = ""
        if ":" in raw:
            raw, label = raw.split(":", 1)
            label = re.sub(r"[\"<>{}\[\]()|#`;]", "", label).strip()[:30]
        parts = [p for p in _ARROW.split(raw) if p.strip()]
        for a, b in zip(parts, parts[1:]):
            redundant = label and any(label.lower() in x.lower() or x.lower() in label.lower() for x in (a, b))
            edges.append((node(a), node(b), "" if redundant else label))
    if not edges:
        raise ValueError("Describe the diagram as connections like 'Controller -> Motor'.")
    direction = direction.upper() if str(direction).upper() in ("LR", "TD", "TB", "RL") else "LR"
    lines = [f"flowchart {direction}"]
    lines += [f'    {nid}["{name}"]' for name, nid in ids.items()]
    lines += [f"    {a} -->|{lbl}| {b}" if lbl else f"    {a} --> {b}" for a, b, lbl in edges]
    return {"type": "diagram", "mermaid": "\n".join(lines), "caption": title or "Diagram",
            "facts": [f"{len(ids)} blocks, {len(edges)} connections"]}


# ── Block diagrams without the model ──────────────────────────────────────────

_LOOP_WORDS = re.compile(r"\b(?:loop|feedback|closed|control(?:ler|led|s)?|regulat\w*|servo)\b", re.IGNORECASE)
_SENSOR_WORDS = re.compile(
    r"sensor|thermocouple|thermometer|thermistor|pt100|rtd|encoder|tacho|resolver|transducer|probe|gauge|camera|"
    r"potentiometer|measur|lidar|gyro|accelerometer|hall|load cell|strain|flowmeter|flow meter|level switch",
    re.IGNORECASE)
_SETPOINT_WORDS = re.compile(r"set ?point|reference|desired|target|command|input", re.IGNORECASE)
_VARIABLES = ("temperature", "speed", "position", "level", "pressure", "flow", "voltage", "current", "angle",
              "humidity", "torque", "force", "velocity", "altitude", "heading", "frequency")


def _edges(connections: list[str]) -> list[tuple[str, str]]:
    out = []
    for raw in connections:
        parts = [p.strip() for p in _ARROW.split(str(raw).split(":", 1)[0]) if p.strip()]
        out += list(zip(parts, parts[1:]))
    return out


def close_loop(connections: list[str], title: str = "") -> list[str]:
    """A 'control loop' drawn without its feedback path gets one: last block -> controller (or Σ)."""
    if not isinstance(connections, list) or not _LOOP_WORDS.search(title or ""):
        return connections
    edges = _edges(connections)
    if not edges:
        return connections
    graph: dict[str, list[str]] = {}
    for a, b in edges:
        graph.setdefault(a, []).append(b)

    def reaches(start: str, goal: str, seen=None) -> bool:
        seen = seen or set()
        for nxt in graph.get(start, []):
            if nxt == goal or (nxt not in seen and (seen.add(nxt) or reaches(nxt, goal, seen))):
                return True
        return False

    if any(reaches(a, a) for a in graph):
        return connections                                   # already has a feedback path
    first_src, first_dst = edges[0]
    target = first_dst if _SETPOINT_WORDS.search(first_src) else first_src
    last = edges[-1][1]
    if last == target:
        return connections
    return connections + [f"{last} -> {target} : feedback"]


def quick_diagram_request(message: str) -> dict | None:
    """'Draw a block diagram of a temperature control loop with a PID controller, heater, oven and
    thermocouple' -> draw_diagram arguments for the proper closed loop; None if it isn't that clear."""
    m = re.search(r"\b(?:draw|show|sketch|make|create|give me|plot)\b.{0,20}?\b(?:block diagram|flow ?chart|diagram)\s+"
                  r"(?:of|for)\s+(?:an?\s+|the\s+|my\s+)?(.+?)\s+(?:with|using|including|consisting of|made of|"
                  r"containing|that has)\s+(.+?)\W*$", message.strip(), re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    comps = [re.sub(r"^(?:an?|the|one|some)\s+", "", c.strip(), flags=re.IGNORECASE)
             for c in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*;\s*|\s+then\s+", m.group(2)) if c.strip()]
    comps = [c for c in comps if c]
    if len(comps) < 2 or len(comps) > 10 or any(len(c.split()) > 4 for c in comps):
        return None
    comps = [c[0].upper() + c[1:] for c in comps]
    title = name[0].upper() + name[1:]
    if not _LOOP_WORDS.search(name):
        return {"connections": [f"{a} -> {b}" for a, b in zip(comps, comps[1:])], "title": title, "direction": "LR"}
    sensors = [c for c in comps if _SENSOR_WORDS.search(c)]
    sensor = sensors[-1] if sensors else None
    forward = [c for c in comps if c != sensor]
    var = next((v for v in _VARIABLES if v in name.lower()), "")
    conns = ["Setpoint -> Σ", f"Σ -> {forward[0]} : error"]
    conns += [f"{a} -> {b}" for a, b in zip(forward, forward[1:])]
    if sensor:
        conns += [f"{forward[-1]} -> {sensor}" + (f" : {var}" if var else ""),
                  f"{sensor} -> Σ : measured" + (f" {var}" if var else " value")]
    else:
        conns += [f"{forward[-1]} -> Σ : " + (var or "output")]        # unity feedback
    return {"connections": conns, "title": title, "direction": "LR"}
