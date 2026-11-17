"""
KiCad's own symbol and footprint libraries, as ARYA's parts catalogue (roadmap K2) — so a design can only use
parts that really exist, with their real pin numbers.

  search("3.3 V LDO regulator")       -> the best-matching symbols (name, library, description, default footprint)
  load("Regulator_Linear", "AMS1117-3.3") -> pins (number, name, electrical type, unit, position) and the
                                          flattened S-expression a schematic embeds (derived symbols such as
                                          AMS1117-3.3, which "extends" AP1117-15, get their parent's pins and
                                          graphics under their own name, exactly as KiCad stores them)
  footprint_exists("Package_TO_SOT_SMD:SOT-223-3_TabPin2")

The search index (~20k symbols: name, description, keywords, default footprint) is built once by a light text
scan of the 224 library files and cached in ARYA's data folder; a symbol's pins are parsed only when needed.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from arya.eda import kicad


def symbol_dir() -> Path | None:
    b = kicad.kicad_bin()
    return b.parent / "share" / "kicad" / "symbols" if b else None


def footprint_dir() -> Path | None:
    b = kicad.kicad_bin()
    return b.parent / "share" / "kicad" / "footprints" if b else None


# ── S-expressions ─────────────────────────────────────────────────────────────

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()"]+')


def parse(text: str) -> list:
    stack, cur = [], []
    for t in _TOKEN.findall(text):
        if t == "(":
            stack.append(cur)
            cur = []
        elif t == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        else:
            cur.append(t)
    return cur[0] if cur else []


def dump(x) -> str:
    if not isinstance(x, list):
        return x
    return "(" + " ".join(dump(i) for i in x) + ")"


def unq(s: str) -> str:
    return s[1:-1].replace('\\"', '"') if isinstance(s, str) and s.startswith('"') else s


def q(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _child(node: list, head: str) -> list | None:
    return next((i for i in node if isinstance(i, list) and i and i[0] == head), None)


def _props(node: list) -> dict[str, str]:
    return {unq(i[1]): unq(i[2]) for i in node if isinstance(i, list) and i and i[0] == "property" and len(i) > 2}


# ── The search index ──────────────────────────────────────────────────────────

_TOP = re.compile(r'^\t\(symbol "((?:\\.|[^"\\])*)"', re.MULTILINE)
_PROP = re.compile(r'^\t\t\(property "(Description|ki_keywords|Footprint|Value|ki_fp_filters)" "((?:\\.|[^"\\])*)"',
                   re.MULTILINE)
_EXTENDS = re.compile(r'^\t\t\(extends "((?:\\.|[^"\\])*)"\)', re.MULTILINE)


def _index_path() -> Path:
    from arya.knowledge.library import DATA_DIR
    return DATA_DIR / "eda" / "symbols-index.json"


def build_index() -> list[dict]:
    """One entry per symbol: lib, name, description, keywords, footprint, extends."""
    out = []
    root = symbol_dir()
    if root is None:
        return out
    for f in sorted(root.glob("*.kicad_sym")):
        text = f.read_text(encoding="utf-8", errors="replace")
        starts = [m.start() for m in _TOP.finditer(text)] + [len(text)]
        for a, b in zip(starts, starts[1:]):
            chunk = text[a:b]
            head = chunk.split("\n\t\t(symbol ", 1)[0]          # properties come before the unit sub-symbols
            props = {k: v for k, v in _PROP.findall(head)}
            ext = _EXTENDS.search(head)
            out.append({"lib": f.stem, "name": _TOP.match(chunk).group(1),
                        "desc": props.get("Description", ""), "kw": props.get("ki_keywords", ""),
                        "fp": props.get("Footprint", ""), "ext": ext.group(1) if ext else ""})
    return out


@lru_cache(maxsize=1)
def index() -> tuple:
    path = _index_path()
    root = symbol_dir()
    stamp = str(root.stat().st_mtime) if root else ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("stamp") == stamp and data.get("symbols"):
            return tuple(data["symbols"])
    except (OSError, ValueError):
        pass
    symbols = build_index()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"stamp": stamp, "symbols": symbols}), encoding="utf-8")
    except OSError:
        pass
    return tuple(symbols)


_WORD = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)?")
_SKIP = {"a", "an", "the", "for", "with", "and", "or", "of", "to", "in", "on", "symbol", "part", "chip", "ic"}


def _words(text: str) -> list[str]:
    return [w.replace(",", ".") for w in _WORD.findall(text.lower()) if w not in _SKIP]


@lru_cache(maxsize=1)
def _doc_freq() -> dict[str, int]:
    df: dict[str, int] = {}
    for s in index():
        for w in set(_words(f"{s['name']} {s['desc']} {s['kw']} {s['lib']}")):
            df[w] = df.get(w, 0) + 1
    return df


def search(query: str, k: int = 8, lib: str | None = None) -> list[dict]:
    """Best-matching symbols: words in the name count most, then keywords, then description (idf-weighted)."""
    words = _words(query)
    if not words:
        return []
    df, n = _doc_freq(), max(1, len(index()))
    ql = query.lower().replace(" ", "")
    scored = []
    for s in index():
        if lib and s["lib"] != lib:
            continue
        name, kw, desc = s["name"].lower(), s["kw"].lower(), s["desc"].lower()
        score = 0.0
        for w in words:
            idf = math.log(n / (1 + df.get(w, 0)))
            if w in _words(name) or (len(w) >= 4 and w in name):
                score += 3 * idf
            elif w in kw:
                score += 2 * idf
            elif w in desc or w in s["lib"].lower():
                score += idf
        if ql and ql == name:
            score += 20
        if score > 0:
            scored.append((score, -len(name), s))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [dict(s) for _, _, s in scored[:k]]


# ── One symbol, in full ───────────────────────────────────────────────────────

@dataclass
class Pin:
    number: str
    name: str
    etype: str                    # power_in, power_out, input, output, bidirectional, passive, no_connect, …
    unit: int                     # 0 = shared by all units
    x: float
    y: float
    angle: int


@dataclass
class Symbol:
    lib: str
    name: str
    props: dict[str, str]
    pins: list[Pin]
    units: int
    sexpr: list = field(repr=False)        # flattened, named "lib:name" — what lib_symbols embeds
    power: bool = False                     # a power symbol (GND, +3V3)

    @property
    def lib_id(self) -> str:
        return f"{self.lib}:{self.name}"

    def pins_for(self, key: str) -> list[Pin]:
        """By number ("A9") -> that pin; by name ("VBUS", "GND", case-insensitive) -> every pin with that name
        (a USB-C receptacle has VBUS on A9 and B9; an MCU has several GND pins) — once per pin number."""
        key = str(key).strip()
        by_number = [p for p in self.pins if p.number == key]
        if by_number:
            return by_number[:1]
        seen, out = set(), []
        for p in self.pins:
            if p.name.lower() == key.lower() and p.name not in ("~", "") and p.number not in seen:
                seen.add(p.number)
                out.append(p)
        return out

    def pin(self, key: str) -> Pin | None:
        found = self.pins_for(key)
        return found[0] if found else None


def _raw_block(lib: str, name: str) -> list:
    root = symbol_dir()
    if root is None:
        raise RuntimeError("KiCad isn't installed")
    f = root / f"{lib}.kicad_sym"
    if not f.exists():
        raise KeyError(f"no symbol library {lib}")
    # Memory-mapped: some libraries are tens of MB, and this laptop often has little memory to spare with ARYA's
    # models loaded (a MemoryError, 2026-11-17). Search the file in place; decode only this symbol's block.
    import mmap
    with open(f, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        m = re.search(rb'^\t\(symbol ' + re.escape(q(name).encode("utf-8")) + rb'\s*$', mm, re.MULTILINE)
        if not m:
            raise KeyError(f"no symbol {lib}:{name}")
        i, depth = m.start() + 1, 0
        pos = i
        while pos < len(mm):                           # the balanced block (strings never contain bare parens here)
            chunk = mm[pos:pos + 65536]
            for k, c in enumerate(chunk):
                if c == 40:                            # "("
                    depth += 1
                elif c == 41:                          # ")"
                    depth -= 1
                    if depth == 0:
                        return parse(mm[i:pos + k + 1].decode("utf-8", errors="replace"))
            pos += len(chunk)
    raise KeyError(f"broken symbol {lib}:{name}")


@lru_cache(maxsize=256)
def load(lib: str, name: str) -> Symbol:
    node = _raw_block(lib, name)
    ext = _child(node, "extends")
    if ext:                                           # derived: the parent's units under this name
        parent = _raw_block(lib, unq(ext[1]))
        mine = {unq(i[1]): i for i in node if isinstance(i, list) and i and i[0] == "property"}
        flat = [node[0], q(f"{lib}:{name}")]
        for item in parent[2:]:
            if isinstance(item, list) and item and item[0] == "property" and unq(item[1]) in mine:
                flat.append(mine.pop(unq(item[1])))
            elif isinstance(item, list) and item and item[0] == "symbol":
                sub = list(item)
                sub[1] = q(unq(sub[1]).replace(unq(ext[1]), name, 1))
                flat.append(sub)
            else:
                flat.append(item)
        flat[2:2] = list(mine.values())               # properties only the child has
        node = flat
    else:
        node = [node[0], q(f"{lib}:{name}")] + node[2:]
    pins: list[Pin] = []
    units = 1
    for sub in (i for i in node if isinstance(i, list) and i and i[0] == "symbol"):
        m = re.search(r"_(\d+)_(\d+)$", unq(sub[1]))
        unit = int(m.group(1)) if m else 0
        units = max(units, unit)
        for p in (i for i in sub if isinstance(i, list) and i and i[0] == "pin"):
            at = _child(p, "at")
            pins.append(Pin(number=unq(_child(p, "number")[1]), name=unq(_child(p, "name")[1]), etype=p[1],
                            unit=unit, x=float(at[1]), y=float(at[2]), angle=int(float(at[3])) if len(at) > 3 else 0))
    power = _child(node, "power") is not None
    return Symbol(lib, name, _props(node), pins, units, node, power)


def footprint_exists(fp: str) -> bool:
    if ":" not in fp:
        return False
    lib, name = fp.split(":", 1)
    root = footprint_dir()
    return bool(root) and (root / f"{lib}.pretty" / f"{name}.kicad_mod").exists()


@lru_cache(maxsize=1)
def all_footprints() -> tuple[str, ...]:
    """Every footprint as 'Library:Name' (~14k; a directory listing, cached)."""
    root = footprint_dir()
    if root is None:
        return ()
    return tuple(f"{d.stem}:{f.stem}" for d in sorted(root.glob("*.pretty")) for f in sorted(d.glob("*.kicad_mod")))


def suggest_footprints(sym: Symbol | None = None, bad: str = "", k: int = 3) -> list[str]:
    """Real footprints for a symbol: its default, then ones matching its footprint filters (KiCad's own hint, e.g.
    'USB*C*Receptacle*'), then names close to a wrong guess ('PinHeader_2x3_…' -> 'PinHeader_2x03_…')."""
    import difflib
    import fnmatch
    out: list[str] = []
    if sym is not None and sym.props.get("Footprint") and footprint_exists(sym.props["Footprint"]):
        out.append(sym.props["Footprint"])
    filters = (sym.props.get("ki_fp_filters", "") if sym else "").split()
    if sym is not None and not filters and not out:             # no hint at all: footprints named like the symbol
        filters = [f"*{sym.name}*"]
    if filters:
        matched = [fp for fp in all_footprints()
                   if any(fnmatch.fnmatch(fp.split(":", 1)[1].lower(), f.lower()) for f in filters)]
        out += sorted((fp for fp in matched if fp not in out), key=_common_first)
    if bad:
        name = re.sub(r"(\d)x(\d)(?=\D)", r"\g<1>x0\2", bad.split(":", 1)[-1])     # 2x3 -> 2x03, as KiCad names it
        names = {f.split(":", 1)[1]: f for f in all_footprints()}
        if name in names:
            out.insert(0, names[name])
        else:
            for close in reversed(difflib.get_close_matches(name, list(names), n=k, cutoff=0.8)):
                if names[close] not in out:
                    out.insert(0, names[close])
    return list(dict.fromkeys(out))[:k]


_COMMON = ("_0603_", "_0805_", "SOIC-8_3.9x4.9mm", "SOT-23", "SOT-223", "_1206_", "PinHeader_1x", "SW_Push_1P1T",
           "SW_PUSH_6mm")


def _common_first(fp: str) -> tuple:
    """Everyday parts first: 0603 / 0805 passives, SOIC / SOT packages; odd variants (hand-solder pads, LDRs,
    01005) last."""
    rank = next((i for i, c in enumerate(_COMMON) if c.lower() in fp.lower()), len(_COMMON))
    odd = any(w in fp for w in ("HandSolder", "Pad", "LDR", "01005", "Castellated", "Kelvin", "Array"))
    return (odd, rank, len(fp), fp)


def footprints(lib: str) -> list[str]:
    root = footprint_dir()
    folder = root / f"{lib}.pretty" if root else None
    return sorted(p.stem for p in folder.glob("*.kicad_mod")) if folder and folder.exists() else []
