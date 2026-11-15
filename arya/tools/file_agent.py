"""
File agent (Phase B): the everyday file chores, by voice.

  "What did I download today?"            -> the newest files in Downloads (and Desktop/Documents)
  "Open the last PDF I downloaded"         -> opens it
  "Find the slides I downloaded yesterday" -> lists matches
  "What's taking up space in Downloads?"   -> biggest files + free disk space
  "Tidy up my Downloads"                   -> proposes sorting loose files into folders by type and
                                              ASKS first; nothing is ever deleted
  "Undo the tidy"                          -> moves everything back

Everything stays inside the sandbox of arya/core/policy.py (your user folder, no hidden/system folders).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path

KINDS = {
    "pdf": {".pdf"},
    "slides": {".pptx", ".ppt", ".key", ".odp"},
    "document": {".pdf", ".docx", ".doc", ".odt", ".txt", ".md", ".rtf", ".tex", ".pptx", ".ppt", ".xlsx",
                 ".xls", ".csv", ".ods", ".odp", ".epub"},
    "spreadsheet": {".xlsx", ".xls", ".csv", ".ods"},
    "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic", ".svg", ".tif", ".tiff"},
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"},
    "audio": {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "installer": {".exe", ".msi", ".msix", ".appx"},
    "code": {".py", ".m", ".c", ".cpp", ".h", ".java", ".js", ".ts", ".ipynb", ".json", ".xml", ".yaml", ".yml",
             ".slx", ".mat"},
}
# Tidy targets: first matching group wins.
TIDY_GROUPS = [("Documents", KINDS["document"]), ("Images", KINDS["image"]), ("Videos", KINDS["video"]),
               ("Music", KINDS["audio"]), ("Archives", KINDS["archive"]), ("Installers", KINDS["installer"]),
               ("Code", KINDS["code"])]
_UNFINISHED = {".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload"}
TIDY_MIN_AGE_S = 24 * 3600              # leave today's downloads where they are
TIDY_MIN_FILES = 3
SCAN_LIMIT = 20_000


def _folders() -> dict[str, Path]:
    from arya.core import policy
    return {k: v for k, v in policy._KNOWN.items() if k in ("downloads", "desktop", "documents")}


def _undo_file() -> Path:
    from arya.knowledge.library import DATA_DIR
    return DATA_DIR / "tidy-undo.json"


def _spoken_kind(kind: str | None, n: int = 2) -> str:
    names = {"pdf": "PDF", "slides": "slide deck", "document": "document", "spreadsheet": "spreadsheet",
             "image": "image", "video": "video", "audio": "audio file", "archive": "zip file",
             "installer": "installer", None: "file"}
    word = names.get(kind, "file")
    return word if n == 1 else word + "s"


def _ago(ts: float, now: float) -> str:
    d, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now).date()
    if d.date() == today:
        return f"today at {d:%H:%M}"
    if d.date() == today - timedelta(days=1):
        return f"yesterday at {d:%H:%M}"
    if (today - d.date()).days < 7:
        return f"on {d:%A}"
    return f"on {d.day} {d:%B}"


# ── Scanning ──────────────────────────────────────────────────────────────────

def recent_files(kind: str | None = None, since: float | None = None, where: list[str] | None = None,
                 limit: int = 5) -> list[tuple[float, Path]]:
    """Newest files (not folders) — Downloads and Desktop top level, Documents two levels deep."""
    folders = _folders()
    names = where or ["downloads", "desktop", "documents"]
    exts = KINDS.get(kind) if kind else None
    found: list[tuple[float, Path]] = []
    for name in names:
        base = folders.get(name)
        if not base or not base.is_dir():
            continue
        depth = 2 if name == "documents" else 0
        for root, dirs, files in os.walk(base):
            rel_depth = len(Path(root).relative_to(base).parts)
            dirs[:] = [] if rel_depth >= depth else [d for d in dirs if not d.startswith(".")]
            for f in files:
                p = Path(root) / f
                if f.startswith(".") or f.lower() == "desktop.ini" or p.suffix.lower() in _UNFINISHED:
                    continue
                if exts and p.suffix.lower() not in exts:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if since is None or mtime >= since:
                    found.append((mtime, p))
    found.sort(reverse=True)
    return found[:limit]


def biggest_files(folder: Path, limit: int = 5) -> list[tuple[int, Path]]:
    sizes, seen = [], 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            seen += 1
            if seen > SCAN_LIMIT:
                break
            try:
                sizes.append(((Path(root) / f).stat().st_size, Path(root) / f))
            except OSError:
                continue
    sizes.sort(reverse=True)
    return sizes[:limit]


# ── Tidy ──────────────────────────────────────────────────────────────────────

def tidy_plan(folder: Path, now: float | None = None) -> list[tuple[str, str]]:
    """(source, destination) for loose files older than a day, by type. Never overwrites."""
    now = now or time.time()
    moves = []
    for entry in sorted(os.scandir(folder), key=lambda e: e.name.lower()):
        if not entry.is_file() or entry.name.startswith(".") or entry.name.lower() == "desktop.ini":
            continue
        p = Path(entry.path)
        if p.suffix.lower() in _UNFINISHED or p.suffix.lower() == ".lnk":
            continue                                       # unfinished downloads, desktop shortcuts
        try:
            if now - entry.stat().st_mtime < TIDY_MIN_AGE_S:
                continue
        except OSError:
            continue
        group = next((g for g, exts in TIDY_GROUPS if p.suffix.lower() in exts), "Other")
        moves.append((str(p), str(folder / group / p.name)))
    return moves


def _free_name(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(2, 1000):
        cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"no free name for {dest.name}")


def apply_moves(moves: list[tuple[str, str]], record: bool = True) -> tuple[int, list[tuple[str, str]]]:
    """Move files, never overwriting. Returns (moved, the (to, from) pairs needed to undo)."""
    from arya.core.policy import check_path

    done, undo = 0, []
    for src, dst in moves:
        s, d = check_path(src), check_path(dst)
        if not s.is_file():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        d = _free_name(d)
        shutil.move(str(s), str(d))
        undo.append((str(d), str(s)))
        done += 1
    if record and undo:
        _undo_file().parent.mkdir(parents=True, exist_ok=True)
        _undo_file().write_text(json.dumps({"ts": time.time(), "moves": undo}), encoding="utf-8")
    return done, undo


def undo_tidy() -> str:
    path = _undo_file()
    if not path.exists():
        return "There's no tidy-up to undo."
    data = json.loads(path.read_text(encoding="utf-8"))
    moved, _ = apply_moves([tuple(m) for m in data["moves"]], record=False)
    path.unlink()
    for _, src in data["moves"]:                           # remove the type folders we made, if now empty
        folder = Path(src).parent
        for g, _ in TIDY_GROUPS + [("Other", set())]:
            try:
                (folder / g).rmdir()
            except OSError:
                pass
    return f"Done, I moved {moved} file{'s' if moved != 1 else ''} back where they were."


def describe_plan(folder_name: str, moves: list[tuple[str, str]]) -> str:
    counts: dict[str, int] = {}
    for _, dst in moves:
        g = Path(dst).parent.name
        counts[g] = counts.get(g, 0) + 1
    parts = [f"{n} into {g}" for g, n in sorted(counts.items(), key=lambda x: -x[1])]
    listing = ", ".join(parts[:-1]) + f" and {parts[-1]}" if len(parts) > 1 else parts[0]
    return (f"Your {folder_name} has {len(moves)} loose files older than a day. Shall I sort them by type: "
            f"{listing}? Nothing gets deleted, and you can say \"undo the tidy\" afterwards.")


# ── Voice ─────────────────────────────────────────────────────────────────────

_KIND_WORDS = [("pdf", r"pdfs?"), ("slides", r"slides?|slide decks?|presentations?|powerpoints?"),
               ("spreadsheet", r"spreadsheets?|excel files?"), ("image", r"images?|pictures?|photos?|screenshots?"),
               ("video", r"videos?"), ("audio", r"audio files?|songs?|recordings?"),
               ("archive", r"zips?|zip files?|archives?"), ("installer", r"installers?|setup files?"),
               ("document", r"documents?|docs?|word files?")]
_RECENT = re.compile(r"\bwhat (?:did i|have i) (?:just )?download(?:ed)?\b|\b(?:my )?(?:recent|latest|newest|last) "
                     r"downloads?\b|\bwhat(?:'s| is) (?:new )?in (?:my )?downloads\b", re.IGNORECASE)
_OPEN_LAST = re.compile(r"\bopen (?:the |my )?(?:last|latest|newest|most recent)\b(?P<what>[^?.!]*)", re.IGNORECASE)
_FIND = re.compile(r"\b(?:find|where(?:'s| is| are)|show me|look for)\b(?P<what>.*?)\b(?:i|that i) "
                   r"(?:downloaded|saved|got)\b(?P<when>.*)$", re.IGNORECASE)
_SPACE = re.compile(r"\b(?:what(?:'s| is) )?taking (?:up )?(?:the most |so much |all the )?(?:disk |storage )?space\b|"
                    r"\b(?:biggest|largest) files\b|\bhow much (?:disk |storage )?space\b|\bdisk space\b|"
                    r"\bfree (?:disk |drive )?space (?:left|on (?:my|the) (?:disk|drive|laptop|c drive))\b",
                    re.IGNORECASE)                       # bare "free space" is physics (vacuum), not disks
_TIDY = re.compile(r"\b(?:tidy|clean|sort|organi[sz]e)\s+(?:up\s+)?(?:my\s+|the\s+)?(?P<where>downloads|desktop)"
                   r"(?:\s+folder)?\b", re.IGNORECASE)
_UNDO = re.compile(r"\bundo (?:the |that |my )?(?:tidy|tidying|clean ?up|sorting)\b", re.IGNORECASE)


def _kind_in(text: str) -> str | None:
    for kind, pattern in _KIND_WORDS:
        if re.search(rf"\b(?:{pattern})\b", text, re.IGNORECASE):
            return kind
    return None


def _since_in(text: str, now: float) -> float | None:
    today = datetime.combine(datetime.fromtimestamp(now).date(), datetime.min.time())
    t = text.lower()
    if "today" in t or "just" in t:
        return today.timestamp()
    if "yesterday" in t:
        return (today - timedelta(days=1)).timestamp()
    if "this week" in t or "recently" in t or "recent" in t:
        return (today - timedelta(days=7)).timestamp()
    return None


def _where(p: Path) -> str:
    """'report.pdf in Downloads', 'sheet3.pdf in Documents/Uni/Dynamics'."""
    for name, base in _folders().items():
        if p.parent == base:
            return f"{p.name} in {base.name}"
        if base in p.parents:
            return f"{p.name} in {base.name}/" + "/".join(p.parent.relative_to(base).parts)
    from arya.core.policy import describe_path
    return describe_path(p)


def _list(found: list[tuple[float, Path]], now: float) -> str:
    items = [f"{_where(p)}, {_ago(ts, now)}" for ts, p in found]
    return "; ".join(items)


def handle(message: str, now: float | None = None) -> str | None:
    """ARYA's reply for file chores, else None. Tidying only *offers* (policy.offer); the yes runs it."""
    from arya.core import policy
    from arya.tools.files import _human_size

    now = now or time.time()
    if _UNDO.search(message):
        reply = undo_tidy()
        if reply.startswith("Done"):
            policy.note_result("move_file")
        return reply

    m = _TIDY.search(message)
    if m:
        name = m.group("where").lower()
        folder = _folders().get(name)
        if not folder:
            return f"I can't find your {name} folder."
        moves = tidy_plan(folder, now)
        if len(moves) < TIDY_MIN_FILES:
            return f"Your {name.capitalize()} is already tidy: only {len(moves)} loose file{'s' if len(moves) != 1 else ''} older than a day."
        return policy.offer("tidy_folder", {"folder": str(folder), "moves": moves},
                            describe_plan(name.capitalize(), moves))

    if _SPACE.search(message):
        folder_word = next((w for w in ("downloads", "desktop", "documents") if w in message.lower()), "downloads")
        folder = _folders().get(folder_word)
        disk = shutil.disk_usage(folder.anchor if folder else Path.home().anchor)
        reply = f"Your drive has {_human_size(disk.free)} free of {_human_size(disk.total)}."
        if folder and folder.is_dir():
            big = biggest_files(folder, 3)
            if big:
                reply += f" The biggest files in {folder_word.capitalize()}: " + ", ".join(
                    f"{p.name} ({_human_size(n)})" for n, p in big) + "."
        return reply

    m = _OPEN_LAST.search(message)
    if m and not re.search(r"\b(?:tab|window|song|track|app|email|message|chat)\b", m.group("what"), re.IGNORECASE):
        what = m.group("what")
        kind = _kind_in(what)
        if not kind and not re.search(r"\b(?:download|file|thing)", what, re.IGNORECASE):
            return None
        where = ["downloads"] if re.search(r"download", what, re.IGNORECASE) else None
        found = recent_files(kind, where=where, limit=1)
        if not found:
            return f"I couldn't find any {_spoken_kind(kind)} in your Downloads, Desktop or Documents."
        p = found[0][1]
        from arya.core.policy import check_path
        os.startfile(str(check_path(p)))
        policy.note_result("open_app")
        return f"Opening {_where(p)}, from {_ago(found[0][0], now)}."

    m = _FIND.search(message)
    if m or _RECENT.search(message):
        text = message
        kind = _kind_in(m.group("what") if m else text)
        since = _since_in(m.group("when") if m else text, now)
        if since is None and _RECENT.search(message):
            since = _since_in("recently", now)
        if m and not kind and not since:
            return None
        found = recent_files(kind, since=since, where=["downloads"] if "download" in text.lower() else None)
        what = _spoken_kind(kind)
        if not found:
            return f"I don't see any {what} from then in your Downloads, Desktop or Documents."
        if len(found) == 1:
            return f"One {_spoken_kind(kind, 1)}: {_list(found, now)}."
        return f"Your newest {what}: {_list(found, now)}."
    return None


def confirm_tidy(args: dict) -> str:
    moved, _ = apply_moves([tuple(m) for m in args["moves"]])
    folder = Path(args["folder"]).name
    return f"Done, I sorted {moved} file{'s' if moved != 1 else ''} in {folder} into folders by type."
