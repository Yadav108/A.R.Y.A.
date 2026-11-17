"""
KiCad, driven from ARYA (roadmap K1) — everything here is local: no model, no cloud.

  Projects live in  Documents\\KiCad\\ARYA\\<name>\\  (ARYA_EDA_DIR to change):
      <name>.kicad_pro / .kicad_sch / .kicad_pcb   from KiCad's own template and Python (so KiCad opens them as
                                                    its own), plus project-local symbol and footprint tables
                                                    (this laptop's KiCad has no global symbol table)
      design.md                                    requirements, decisions log, open questions — the project's
                                                    memory between sessions ("continue the motor driver design")
      checks/                                      the last ERC / DRC reports (JSON, from kicad-cli)
      manufacturing/                               Gerbers + drill + BOM + schematic PDF, zipped per export
  Checks and exports use kicad-cli (KiCad 9); a new board is written by KiCad's Python (pcbnew).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CLI_TIMEOUT_S = 180
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── Where KiCad and the projects are ──────────────────────────────────────────

def kicad_bin() -> Path | None:
    """The newest KiCad's bin folder (KICAD_BIN to override)."""
    if os.getenv("KICAD_BIN"):
        p = Path(os.environ["KICAD_BIN"])
        return p if (p / "kicad-cli.exe").exists() else None
    base = Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "KiCad"
    versions = sorted((p for p in base.glob("*") if (p / "bin" / "kicad-cli.exe").exists()),
                      key=lambda p: [int(x) if x.isdigit() else 0 for x in p.name.split(".")], reverse=True)
    return versions[0] / "bin" if versions else None


def available() -> bool:
    return kicad_bin() is not None


def projects_dir() -> Path:
    if os.getenv("ARYA_EDA_DIR"):
        return Path(os.environ["ARYA_EDA_DIR"])
    from arya.core.policy import _known_folder
    docs = _known_folder("Documents") or Path.home() / "Documents"
    return docs / "KiCad" / "ARYA"


def safe_name(spoken: str) -> str:
    """'the Motor Driver.' -> 'motor-driver'"""
    name = re.sub(r"^(?:the|a|an|my)\s+", "", spoken.strip().strip(".!?\"'“”"), flags=re.IGNORECASE)
    name = re.sub(r"\s+(?:project|board|pcb|design)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return name[:48]


@dataclass
class Project:
    name: str
    folder: Path

    @property
    def pro(self) -> Path:
        return self.folder / f"{self.name}.kicad_pro"

    @property
    def sch(self) -> Path:
        return self.folder / f"{self.name}.kicad_sch"

    @property
    def pcb(self) -> Path:
        return self.folder / f"{self.name}.kicad_pcb"

    @property
    def design(self) -> Path:
        return self.folder / "design.md"

    @property
    def title(self) -> str:
        return self.name.replace("-", " ")


def projects() -> list[Project]:
    root = projects_dir()
    if not root.exists():
        return []
    found = [Project(p.stem, p.parent) for p in root.glob("*/*.kicad_pro")]
    return sorted(found, key=lambda p: _mtime(p), reverse=True)


def _mtime(p: Project) -> float:
    return max((f.stat().st_mtime for f in (p.sch, p.pcb, p.design) if f.exists()), default=0.0)


def find(spoken: str) -> Project | None:
    """The project whose name best matches what was said ('motor driver' -> motor-driver)."""
    import difflib
    want = safe_name(spoken)
    if not want:
        return None
    all_ = projects()
    for p in all_:
        if p.name == want:
            return p
    for p in all_:
        if want in p.name or p.name in want:
            return p
    close = difflib.get_close_matches(want, [p.name for p in all_], n=1, cutoff=0.6)
    return next((p for p in all_ if close and p.name == close[0]), None)


# ── A new project ─────────────────────────────────────────────────────────────

def _empty_schematic(title: str) -> str:
    import uuid
    return (f'(kicad_sch (version 20250114) (generator "arya") (generator_version "9.0") (uuid "{uuid.uuid4()}")\n'
            f'  (paper "A4")\n  (title_block (title "{title}") (date "{datetime.now():%Y-%m-%d}"))\n'
            '  (lib_symbols)\n  (sheet_instances (path "/" (page "1")))\n)\n')


_DESIGN_MD = """# {title}

Started {date}. Kept by ARYA: what this board must do, what was decided and why, what's still open.

## Requirements

- (what the board must do, input power, size, connectors, budget …)

## Decisions

## Open questions

## Log

- {date}: project created.
"""


def create(spoken: str) -> Project:
    """A KiCad project folder ARYA and KiCad can both work in. Raises FileExistsError / RuntimeError."""
    bin_ = kicad_bin()
    if bin_ is None:
        raise RuntimeError("KiCad isn't installed")
    name = safe_name(spoken)
    if not name:
        raise ValueError("no name")
    folder = projects_dir() / name
    if folder.exists() and any(folder.iterdir()):
        raise FileExistsError(name)
    folder.mkdir(parents=True, exist_ok=True)
    p = Project(name, folder)
    template = bin_.parent / "share" / "kicad" / "template"
    pro = json.loads((template / "kicad.kicad_pro").read_text(encoding="utf-8"))
    pro["meta"]["filename"] = p.pro.name
    p.pro.write_text(json.dumps(pro, indent=2), encoding="utf-8")
    for table in ("sym-lib-table", "fp-lib-table"):          # this laptop's KiCad has no global symbol table
        if (template / table).exists():
            shutil.copy(template / table, folder / table)
    p.sch.write_text(_empty_schematic(p.title), encoding="utf-8")
    _new_board(p.pcb)
    p.design.write_text(_DESIGN_MD.format(title=p.title.title(), date=f"{datetime.now():%Y-%m-%d}"),
                        encoding="utf-8")
    return p


def _new_board(path: Path) -> None:
    """An empty board written by KiCad's own Python, so it's exactly what KiCad expects."""
    import time
    py = kicad_bin() / "python.exe"
    script = "import pcbnew, sys\nb = pcbnew.BOARD()\npcbnew.SaveBoard(sys.argv[1], b)\n"
    for attempt in (1, 2):                         # KiCad's Python can fail to start when memory is tight
        r = subprocess.run([str(py), "-I", "-c", script, str(path)], capture_output=True, text=True,
                           timeout=CLI_TIMEOUT_S, creationflags=_NO_WINDOW)
        if r.returncode == 0 and path.exists():
            return
        if attempt == 1:
            time.sleep(3)
    raise RuntimeError(f"KiCad couldn't create the board (exit {r.returncode & 0xFFFFFFFF:#x}: "
                       f"{(r.stderr or r.stdout).strip()[-160:]})")


# ── Checks ────────────────────────────────────────────────────────────────────

@dataclass
class Violation:
    severity: str                 # error / warning / exclusion
    kind: str                     # KiCad's type, e.g. pin_not_connected
    text: str                     # description + the items it's about
    where: str = ""               # sheet or board

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.text}"


@dataclass
class Report:
    what: str                     # "schematic" / "board"
    violations: list[Violation] = field(default_factory=list)
    unconnected: int = 0
    parity: int = 0               # board vs schematic differences
    when: float = 0.0

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "warning"]


def _cli(*args: str) -> subprocess.CompletedProcess:
    bin_ = kicad_bin()
    if bin_ is None:
        raise RuntimeError("KiCad isn't installed")
    return subprocess.run([str(bin_ / "kicad-cli.exe"), *args], capture_output=True, text=True,
                          timeout=CLI_TIMEOUT_S, creationflags=_NO_WINDOW)


def _violations(items: list[dict], where: str = "") -> list[Violation]:
    out = []
    for v in items:
        about = "; ".join(i.get("description", "") for i in v.get("items", []) if i.get("description"))
        out.append(Violation(v.get("severity", "error"), v.get("type", ""),
                             v.get("description", "") + (f" ({about})" if about else ""), where))
    return out


def erc(p: Project) -> Report:
    out = p.folder / "checks" / "erc.json"
    out.parent.mkdir(exist_ok=True)
    _cli("sch", "erc", "--severity-all", "--format", "json", "-o", str(out), str(p.sch))
    data = json.loads(out.read_text(encoding="utf-8"))
    vs = [v for s in data.get("sheets", []) for v in _violations(s.get("violations", []), s.get("path", "/"))]
    return Report("schematic", vs, when=out.stat().st_mtime)


def drc(p: Project) -> Report:
    out = p.folder / "checks" / "drc.json"
    out.parent.mkdir(exist_ok=True)
    _cli("pcb", "drc", "--severity-all", "--schematic-parity", "--format", "json", "-o", str(out), str(p.pcb))
    data = json.loads(out.read_text(encoding="utf-8"))
    return Report("board", _violations(data.get("violations", []), "board"),
                  unconnected=len(data.get("unconnected_items", [])),
                  parity=len(data.get("schematic_parity", [])), when=out.stat().st_mtime)


def board_is_empty(p: Project) -> bool:
    try:
        return "(footprint " not in p.pcb.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True


def schematic_is_empty(p: Project) -> bool:
    try:
        return "(symbol (lib_id" not in re.sub(r"\s+", " ", p.sch.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return True


def _short(v: Violation) -> str:
    text = re.sub(r"\s+", " ", v.text)
    return text[:140]


def summary(report: Report) -> str:
    """One or two spoken sentences."""
    errs, warns = report.errors, report.warnings
    extra = []
    if report.what == "board":
        if report.parity:
            extra.append(f"{report.parity} difference{'s' * (report.parity != 1)} from the schematic "
                         "(in KiCad: Tools, Update PCB from Schematic, or F8)")
        if report.unconnected:
            extra.append(f"{report.unconnected} connection{'s' * (report.unconnected != 1)} still to route")
    if not errs and not warns and not extra:
        return f"The {report.what} is clean: no errors, no warnings."
    head = (f"The {report.what} has {len(errs)} error{'s' * (len(errs) != 1)} and {len(warns)} "
            f"warning{'s' * (len(warns) != 1)}")
    if extra:
        head += ", " + " and ".join(extra)
    first = errs[0] if errs else (warns[0] if warns else None)
    return head + "." + (f" First: {_short(first)}." if first else "")


# ── Exports ───────────────────────────────────────────────────────────────────

def export_manufacturing(p: Project) -> Path:
    """Gerbers, drill files, BOM and the schematic PDF, zipped in manufacturing/."""
    stamp = f"{datetime.now():%Y-%m-%d_%H%M}"
    out = p.folder / "manufacturing" / stamp
    gerbers = out / "gerbers"
    gerbers.mkdir(parents=True, exist_ok=True)
    _cli("pcb", "export", "gerbers", "-o", str(gerbers) + os.sep, str(p.pcb))
    _cli("pcb", "export", "drill", "-o", str(gerbers) + os.sep, str(p.pcb))
    _cli("sch", "export", "bom", "-o", str(out / f"{p.name}-bom.csv"), str(p.sch))
    _cli("sch", "export", "pdf", "-o", str(out / f"{p.name}-schematic.pdf"), str(p.sch))
    archive = p.folder / "manufacturing" / f"{p.name}-{stamp}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in gerbers.iterdir():
            z.write(f, f"gerbers/{f.name}")
    return archive


# ── The design log (design.md) ────────────────────────────────────────────────

_SECTIONS = ("Requirements", "Decisions", "Open questions", "Log")


def _sections(p: Project) -> dict[str, list[str]]:
    text = p.design.read_text(encoding="utf-8") if p.design.exists() else ""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            current = m.group(1)
            out[current] = []
        elif current and line.strip().startswith("- "):
            item = line.strip()[2:].strip()
            if not item.startswith("("):                 # the template's placeholder
                out[current].append(item)
    return out


def add_to_design(p: Project, section: str, text: str) -> None:
    """Append '- text' under '## section' (log lines get today's date)."""
    if section not in _SECTIONS:
        raise ValueError(section)
    if section == "Log":
        text = f"{datetime.now():%Y-%m-%d %H:%M}: {text}"
    lines = p.design.read_text(encoding="utf-8").splitlines() if p.design.exists() else [f"# {p.title}"]
    try:
        start = next(i for i, l in enumerate(lines) if re.match(rf"^##\s+{re.escape(section)}\s*$", l))
    except StopIteration:
        lines += ["", f"## {section}", ""]
        start = len(lines) - 2
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, f"- {text}")
    p.design.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def where_we_are(p: Project) -> str:
    """'Continue the motor driver design': the project's state, from design.md and the files — no model."""
    s = _sections(p)
    parts = [f"{p.title.capitalize()}:"]
    req = s.get("Requirements", [])
    parts.append(f"{len(req)} requirement{'s' * (len(req) != 1)} written down" if req else "no requirements yet")
    if schematic_is_empty(p):
        parts.append("the schematic is still empty")
    elif board_is_empty(p):
        parts.append("there's a schematic, the board has no parts yet")
    else:
        parts.append("schematic and board both have parts")
    text = parts[0] + " " + ", ".join(parts[1:]) + "."
    decisions = s.get("Decisions", [])
    if decisions:
        text += f" Last decision: {decisions[-1]}."
    log = [l for l in s.get("Log", []) if not l.endswith("project created.")]
    if log:
        text += f" Last time: {re.sub(r'^[0-9-]+ [0-9:]+: ', '', log[-1])}."
    open_ = s.get("Open questions", [])
    if open_:
        text += f" Open: {'; '.join(open_[:2])}."
    return text
