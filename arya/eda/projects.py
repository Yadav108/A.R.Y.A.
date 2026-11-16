"""
KiCad projects by voice, and checks on every save (roadmap K1). All local.

  "new board project called motor driver"     -> a KiCad project in Documents\\KiCad\\ARYA\\motor-driver
  "open the motor driver in KiCad"             -> KiCad opens it
  "continue the motor driver design"          -> becomes the active project; where you left off (design.md)
  "check the schematic" / "check the board" / "run DRC"   -> KiCad's ERC / DRC, summarised
  "export the board for manufacturing"        -> Gerbers + drill + BOM + PDF, zipped
  "design decision: 12 V input, buck to 3.3 V" / "requirement: fits in 50 by 50 mm" / "open question: …"
  "add to the design log: ordered the parts"  -> design.md, the project's memory
  "list my board projects"
  While you work in KiCad: each time you save the schematic or board, ARYA runs the check quietly and speaks
  only when there's a NEW error (warnings and old errors stay quiet).
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from arya.eda import kicad

POLL_S = 4.0
_STATE_FILE = ".arya-state.json"
_state: dict = {"active": None, "loaded": False}
_seen: dict[str, dict] = {}            # project -> {"sch": (mtime, error keys), "pcb": (…)}


def _state_path():
    return kicad.projects_dir() / _STATE_FILE


def _load() -> None:
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        _state["active"] = json.loads(_state_path().read_text(encoding="utf-8")).get("active")
    except (OSError, ValueError):
        pass


def set_active(p: kicad.Project) -> None:
    _load()
    _state["active"] = p.name
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps({"active": p.name}), encoding="utf-8")
    except OSError:
        pass


def active() -> kicad.Project | None:
    _load()
    name = _state["active"]
    if name:
        p = kicad.Project(name, kicad.projects_dir() / name)
        if p.pro.exists():
            return p
    all_ = kicad.projects()
    return all_[0] if all_ else None


# ── What you say ──────────────────────────────────────────────────────────────

_KIND = r"(?:kicad|pcb|board|circuit[\s-]?board|electronics|hardware)"
_NEW = re.compile(rf"^\W*(?:(?:please|can\s+you|could\s+you)\s+)?(?:make|create|start|set\s+up|begin|new)\s+(?:me\s+)?"
                  rf"(?:a\s+)?(?:new\s+)?{_KIND}\s+project\s*(?:called|named|for)?\s*[:,-]?\s*(?P<name>.+?)\W*$",
                  re.IGNORECASE)
_OPEN = re.compile(r"^\W*(?:please\s+)?(?:open|launch|show)\s+(?:the\s+|my\s+)?(?P<name>.+?)\s+(?:in\s+kicad|kicad\s+project|"
                   r"board\s+project|pcb\s+project|project\s+in\s+kicad)\W*$|^\W*(?:open|launch|start)\s+kicad\W*$",
                   re.IGNORECASE)
_CONTINUE = re.compile(r"^\W*(?:let'?s\s+)?(?:work\s+on|continue(?:\s+with)?|resume|pick\s+up)\s+(?:the\s+|my\s+)?"
                       r"(?P<name>.+?)\s+(?:design|board|pcb|kicad\s+project|board\s+project)\W*$|"
                       r"\bwhere\s+(?:was\s+i|did\s+i\s+(?:leave\s+off|stop))\s+(?:on|with)\s+(?:the\s+|my\s+)?"
                       r"(?P<name2>.+?)\s+(?:design|board|pcb|project)\W*$", re.IGNORECASE)
_CHECK = re.compile(r"^\W*(?:please\s+)?(?:(?:run\s+)?(?:an?\s+)?(?P<tool>erc|drc)(?:\s+check)?|"
                    r"(?:check|run\s+the\s+checks?\s+on|any\s+(?:errors|problems)\s+(?:in|on)|what'?s\s+wrong\s+with)\s+"
                    r"(?:the\s+|my\s+|this\s+)?(?:(?P<name>[\w-]+(?:\s+[\w-]+)?)\s+)?(?P<what>schematic|board|pcb|layout|"
                    r"kicad\s+project))(?:\s+(?:for\s+(?:errors|problems|mistakes)|again|now|please))?\W*$",
                    re.IGNORECASE)
_EXPORT = re.compile(r"\b(?:export|make|generate|create)\s+(?:the\s+)?(?:gerbers?|manufacturing\s+files|"
                     r"fab(?:rication)?\s+files|production\s+files)\b|\bexport\s+(?:the\s+|my\s+)?(?:board|pcb|design)"
                     r"\s+for\s+(?:manufacturing|production|fab(?:rication)?|jlcpcb|pcbway)\b", re.IGNORECASE)
_LOG = re.compile(r"^\W*(?:add\s+to|note\s+in|put\s+in|write\s+in|log\s+in)\s+(?:the\s+)?design\s+log\s*[:,-]?\s*(?P<log>.+?)\s*$|"
                  r"^\W*design\s+(?:decision|choice)\s*[:,-]\s*(?P<dec>.+?)\s*$|"
                  r"^\W*(?:new\s+)?(?:board\s+)?requirement\s*[:,-]\s*(?P<req>.+?)\s*$|"
                  r"^\W*open\s+question\s*[:,-]\s*(?P<q>.+?)\s*$", re.IGNORECASE)
_LIST = re.compile(r"\b(?:list|what\s+are|show\s+me|which\s+are)\s+(?:all\s+)?(?:of\s+)?my\s+(?:kicad|board|pcb|electronics|"
                   r"hardware)\s+projects\b", re.IGNORECASE)
_THIS = {"", "the", "my", "this", "our", "current", "same"}


def matches(message: str) -> bool:
    return any(r.search(message) for r in (_NEW, _OPEN, _CONTINUE, _CHECK, _EXPORT, _LOG, _LIST))


def _pick(spoken: str | None) -> kicad.Project | None:
    spoken = (spoken or "").strip()
    if spoken.lower() in _THIS or spoken.lower() in {"kicad", "kicad project"}:
        return active()
    return kicad.find(spoken)


async def handle(message: str, remote: bool = False) -> str | None:
    if not matches(message):
        return None
    if not kicad.available():
        return "KiCad isn't installed on this laptop. Install KiCad 9 from kicad.org and ask again."
    m = _NEW.match(message)
    if m:
        name = m.group("name")
        try:
            p = await asyncio.to_thread(kicad.create, name)
        except FileExistsError:
            p = kicad.find(name)
            if p:
                set_active(p)
            return f"There's already a project called {kicad.safe_name(name)}. I've made it the active one."
        except ValueError:
            return "What should the project be called?"
        set_active(p)
        return (f"Created the {p.title} project in Documents, KiCad, ARYA. Tell me what the board must do, "
                "like \"requirement: runs from USB\", and say \"open it in KiCad\" when you want to draw.")
    m = _LOG.match(message)
    if m:
        p = active()
        if p is None:
            return "There's no board project yet. Say \"new board project called\" and a name."
        section, text = next((s, m.group(g)) for g, s in (("log", "Log"), ("dec", "Decisions"),
                                                          ("req", "Requirements"), ("q", "Open questions"))
                             if m.group(g))
        await asyncio.to_thread(kicad.add_to_design, p, section, text.rstrip("."))
        if section == "Decisions":                      # decisions are also dated in the log
            await asyncio.to_thread(kicad.add_to_design, p, "Log", f"decided: {text.rstrip('.')}")
        where = {"Log": "the design log", "Decisions": "the decisions", "Requirements": "the requirements",
                 "Open questions": "the open questions"}[section]
        return f"Added to {where} of {p.title}."
    if _LIST.search(message):
        all_ = kicad.projects()
        if not all_:
            return "No board projects yet. Say \"new board project called\" and a name."
        names = ", ".join(p.title for p in all_[:6])
        act = active()
        return f"Your board projects: {names}." + (f" Active: {act.title}." if act else "")
    m = _CONTINUE.search(message)
    if m:
        p = _pick(m.group("name") or m.group("name2"))
        if p is None:                                   # "let's work on the slide design": not ours
            if not re.search(r"\b(?:board|pcb|kicad)\b", message, re.IGNORECASE):
                return None
            return "I don't know that project. Say \"list my board projects\" to hear them."
        set_active(p)
        return await asyncio.to_thread(kicad.where_we_are, p)
    m = _OPEN.match(message)
    if m:
        p = _pick(m.group("name"))
        if p is None:
            return "I don't know that project. Say \"list my board projects\" to hear them."
        set_active(p)
        import os
        os.startfile(str(p.pro))
        return f"Opening {p.title} in KiCad."
    if _EXPORT.search(message):
        p = active()
        if p is None:
            return "There's no board project yet."
        if kicad.board_is_empty(p):
            return f"The {p.title} board has no parts yet, so there's nothing to manufacture."
        report = await asyncio.to_thread(kicad.drc, p)
        archive = await asyncio.to_thread(kicad.export_manufacturing, p)
        warn = (f" Careful: the board still has {len(report.errors)} DRC error{'s' * (len(report.errors) != 1)}."
                if report.errors else "")
        if report.unconnected:
            warn += f" And {report.unconnected} connections aren't routed."
        await asyncio.to_thread(kicad.add_to_design, p, "Log", f"exported manufacturing files ({archive.name})")
        return f"Exported Gerbers, drill files, BOM and schematic PDF to {archive.name} in the project's manufacturing folder.{warn}"
    m = _CHECK.match(message)
    if m:
        p = _pick(m.group("name"))
        if p is None:
            return "There's no board project yet. Say \"new board project called\" and a name."
        tool = (m.group("tool") or "").lower()
        what = (m.group("what") or "").lower()
        board = tool == "drc" or what in {"board", "pcb", "layout"}
        if what == "kicad project" or (not tool and not what):
            board = None
        replies = []
        if board is not True:
            if kicad.schematic_is_empty(p):
                replies.append(f"The {p.title} schematic is still empty.")
            else:
                r = await asyncio.to_thread(kicad.erc, p)
                _remember(p, "sch", r)
                replies.append(kicad.summary(r))
        if board is not False:
            if kicad.board_is_empty(p):
                replies.append("The board has no parts yet." if board is None else
                               f"The {p.title} board has no parts yet. In KiCad's PCB editor, press F8 to bring "
                               "them in from the schematic.")
            else:
                r = await asyncio.to_thread(kicad.drc, p)
                _remember(p, "pcb", r)
                replies.append(kicad.summary(r))
        set_active(p)
        return " ".join(replies)
    return None


# ── Checks on every save ──────────────────────────────────────────────────────

def _remember(p: kicad.Project, which: str, report: kicad.Report) -> None:
    f = p.sch if which == "sch" else p.pcb
    try:
        mtime = f.stat().st_mtime
    except OSError:
        mtime = 0.0
    _seen.setdefault(p.name, {})[which] = (mtime, {v.key for v in report.errors})


def check_saved(p: kicad.Project, now: float | None = None) -> str | None:
    """A file saved since the last look (and not still being written): check it; describe NEW errors only.
    The first look at a project only records the baseline."""
    now = now or time.time()
    out = []
    for which, f, run, empty in (("sch", p.sch, kicad.erc, kicad.schematic_is_empty),
                                 ("pcb", p.pcb, kicad.drc, kicad.board_is_empty)):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        before = _seen.get(p.name, {}).get(which)
        if before is not None and before[0] == mtime:
            continue
        if now - mtime < 2.0:                          # KiCad may still be writing
            continue
        if empty(p):
            _seen.setdefault(p.name, {})[which] = (mtime, set())
            continue
        report = run(p)
        _remember(p, which, report)
        if before is None:
            continue                                   # baseline: say nothing
        new = [v for v in report.errors if v.key not in before[1]]
        if new:
            what = "schematic" if which == "sch" else "board"
            out.append(f"{p.title.capitalize()} {what}: {len(new)} new error{'s' * (len(new) != 1)} since you saved. "
                       f"{kicad._short(new[0])}.")
    return " ".join(out) or None


async def run(announce, busy=lambda: False) -> None:
    """Watch the active project while ARYA runs."""
    if not kicad.available():
        return
    while True:
        await asyncio.sleep(POLL_S)
        if busy():
            continue
        try:
            p = active()
            if p is None:
                continue
            said = await asyncio.to_thread(check_saved, p)
            if said:
                print(f"KiCad: {said}")
                await announce(said)
        except Exception as exc:
            print(f"KiCad watcher: {type(exc).__name__}: {exc}")
            await asyncio.sleep(60)
