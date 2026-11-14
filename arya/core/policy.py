"""
What ARYA may do on this laptop, and when it must ask first.

Three layers, enforced in code (not by asking the model nicely):

1. Risk tiers — every tool is either run freely or needs a spoken "yes":
     * CONFIRM: delete, move/rename, close apps — always.
     * Side effects (open a URL, run a command, copy to the clipboard, open an
       app, save a note) need a "yes" once the current request has read
       untrusted text (clipboard, files, command output, web, notes). That text
       could contain injected instructions ("now open evil.com/?d=…") — this
       stops it from turning into an action without you hearing about it.
     * In ARYA_MODE=cloud, reading private data (clipboard, files, folders,
       notes, command output) also needs a "yes", because tool results are
       sent to Groq.
2. Folder sandbox — file tools only work inside your user folder (plus
   ARYA_ALLOWED_DIRS). AppData, hidden folders (.ssh, .aws…) and secret files
   (.env, keys, certificates, password databases) are always off limits.
3. Audit — every request for confirmation, approval, refusal and block is
   written to the local action log.
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

PENDING_TTL_S = 60.0


class PolicyError(PermissionError):
    """A tool tried to touch something ARYA is never allowed to touch."""


class ConfirmationRequired(Exception):
    """The action needs a spoken 'yes' first — `question` is what ARYA asks."""

    def __init__(self, question: str):
        super().__init__(question)
        self.question = question


# ── Folder sandbox ────────────────────────────────────────────────────────────

HOME = Path.home()


def _known_folder(name: str) -> Path | None:
    """Real location of Desktop/Documents/… (OneDrive often redirects them)."""
    try:
        from win32com.shell import shell

        fid = getattr(shell, f"FOLDERID_{name}", None)
        if fid is not None:
            return Path(shell.SHGetKnownFolderPath(fid))
    except Exception:
        pass
    p = HOME / name
    return p if p.exists() else None


_KNOWN = {}
for _spoken, _fid in [("desktop", "Desktop"), ("documents", "Documents"), ("my documents", "Documents"),
                      ("downloads", "Downloads"), ("pictures", "Pictures"), ("photos", "Pictures"),
                      ("music", "Music"), ("videos", "Videos")]:
    _p = _known_folder(_fid)
    if _p is not None:
        _KNOWN[_spoken] = _p

ALLOWED_ROOTS: list[Path] = [HOME.resolve()] + [
    Path(p).expanduser().resolve() for p in os.getenv("ARYA_ALLOWED_DIRS", "").split(os.pathsep) if p.strip()
]
_DENIED_DIRS = {"appdata", "application data", "local settings"}
_DENIED_FILES = [
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.crt", "*.cer", "id_rsa*", "id_dsa*",
    "id_ecdsa*", "id_ed25519*", "*.kdbx", "*.kdb", "*.ovpn", "credentials*", "*.keychain*",
    "ntuser.dat*", "*.wallet", "secrets.*", "*.gpg", "*.asc",
]


def resolve_user_path(raw: str) -> Path:
    """'Downloads/report.pdf', '~/Desktop/x', 'C:\\Users\\me\\a.txt', 'notes.txt' → absolute Path."""
    text = os.path.expandvars(os.path.expanduser(str(raw).strip().strip('"').strip("'")))
    path = Path(text)
    if not path.is_absolute():
        parts = re.split(r"[\\/]+", text, maxsplit=1)
        base = _KNOWN.get(parts[0].strip().lower())
        if base is not None:
            path = base / (parts[1] if len(parts) > 1 else "")
        else:
            path = HOME / text
    return path.resolve()


def check_path(raw: str | Path) -> Path:
    """Resolve and enforce the sandbox. Raises PolicyError with a speakable reason."""
    path = raw if isinstance(raw, Path) else resolve_user_path(raw)
    path = path.resolve()
    if not any(path == root or root in path.parents for root in ALLOWED_ROOTS):
        raise PolicyError(f"{path} is outside your user folder, so I'm not allowed to touch it.")
    root = next(r for r in ALLOWED_ROOTS if path == r or r in path.parents)
    for part in path.relative_to(root).parts:
        if part.lower() in _DENIED_DIRS or part.startswith("."):
            raise PolicyError(f"{part} is a protected system or hidden folder, so I'm not allowed to touch it.")
    if any(fnmatch.fnmatch(path.name.lower(), pat) for pat in _DENIED_FILES):
        raise PolicyError(f"{path.name} looks like a key or secrets file, so I'm not allowed to touch it.")
    return path


def describe_path(path: Path) -> str:
    """'report.pdf in Downloads' — how ARYA says a path out loud."""
    parent = path.parent
    for spoken, base in _KNOWN.items():
        if parent == base:
            return f"{path.name} in {base.name}"
    try:
        rel = parent.relative_to(HOME)
        return f"{path.name} in {rel}" if str(rel) != "." else f"{path.name} in your user folder"
    except ValueError:
        return str(path)


# ── Risk tiers ────────────────────────────────────────────────────────────────

ALWAYS_CONFIRM = {"delete_file", "move_file", "close_app"}
# Results that may contain text someone else wrote (= possible injected instructions).
UNTRUSTED_OUTPUT = {"get_clipboard", "search_wikipedia", "run_command", "file_search", "list_folder",
                    "read_text_file", "get_notes", "search_notes"}
# Actions that could carry data out or change things — confirmed after untrusted input.
SIDE_EFFECTS = {"open_url", "run_command", "set_clipboard", "open_app", "save_note"}
# Private data that would be sent to Groq in cloud mode.
PRIVATE_READS = {"get_clipboard", "read_text_file", "list_folder", "file_search", "run_command",
                 "get_notes", "search_notes"}


@dataclass
class Pending:
    tool: str
    args: dict
    question: str
    request: str
    expires: float = field(default_factory=lambda: time.monotonic() + PENDING_TTL_S)


class _State:
    tainted = False
    tainted_by = ""
    request = ""
    pending: Pending | None = None
    executed: set = set()          # tools that actually ran during this request
    last_asked = 0.0               # when ARYA last asked for a yes/no


_state = _State()


def begin_turn(request: str) -> None:
    """Every new user request starts untainted."""
    _state.tainted = False
    _state.tainted_by = ""
    _state.request = request
    _state.executed = set()


def note_result(tool: str) -> None:
    _state.executed.add(tool)
    if tool in UNTRUSTED_OUTPUT:
        _state.tainted = True
        _state.tainted_by = _state.tainted_by or tool


def needs_confirmation(tool: str, args: dict, cloud_mode: bool) -> bool:
    if tool in ALWAYS_CONFIRM:
        return True
    if tool in SIDE_EFFECTS and _state.tainted:
        return True
    if cloud_mode and tool in PRIVATE_READS:
        return True
    return False


_TAINT_SOURCE = {
    "get_clipboard": "your clipboard", "search_wikipedia": "Wikipedia", "run_command": "a command's output",
    "file_search": "file names", "list_folder": "a folder listing", "read_text_file": "a file",
    "get_notes": "your notes", "search_notes": "your notes",
}


def question_for(tool: str, args: dict, cloud_mode: bool) -> str:
    """What ARYA asks before acting — specific enough to catch a wrong target."""
    def path_desc(key: str) -> str:
        try:
            return describe_path(resolve_user_path(args.get(key, "")))
        except Exception:
            return str(args.get(key, "that file"))

    if tool == "delete_file":
        q = f"Move {path_desc('path')} to the Recycle Bin?"
    elif tool == "move_file":
        dest = args.get("destination", "")
        try:
            d = resolve_user_path(dest)
            dest_desc = d.name if d.parent == resolve_user_path(args.get("source", "")).parent else describe_path(d)
        except Exception:
            dest_desc = dest
        q = f"Move {path_desc('source')} to {dest_desc}?"
    elif tool == "close_app":
        q = f"Close {args.get('app_name', 'that app')}? Unsaved work there could be lost."
    elif tool == "open_url":
        q = f"Open {args.get('url', 'that link')}?"
    elif tool == "run_command":
        q = f"Run the command {args.get('command', '')}?"
    elif tool == "set_clipboard":
        q = "Copy that to your clipboard?"
    elif tool == "open_app":
        q = f"Open {args.get('app_name', 'that app')}?"
    elif tool == "save_note":
        q = "Save that as a note?"
    elif cloud_mode and tool in PRIVATE_READS:
        q = f"That needs {_TAINT_SOURCE.get(tool, 'private data')}, which would be sent to the cloud. Go ahead?"
    else:
        q = f"Run {tool.replace('_', ' ')}?"
    if tool in SIDE_EFFECTS and _state.tainted and tool not in ALWAYS_CONFIRM:
        q = f"This came from {_TAINT_SOURCE.get(_state.tainted_by, 'something I read')}. " + q
    return q + " Say yes or no."


def request_confirmation(tool: str, args: dict, cloud_mode: bool) -> str:
    question = question_for(tool, args, cloud_mode)
    _state.pending = Pending(tool=tool, args=dict(args), question=question, request=_state.request)
    _state.last_asked = time.monotonic()
    return question


def asked_recently(window_s: float = 600.0) -> bool:
    """A yes/no question was asked in the last few minutes (but has since expired)."""
    return time.monotonic() - _state.last_asked < window_s


def acted_this_turn() -> bool:
    """Did anything that changes the laptop actually run during this request?"""
    return bool(_state.executed & (ALWAYS_CONFIRM | SIDE_EFFECTS | {"media_control", "set_timer",
                                                                    "set_reminder", "cancel_timer", "pomodoro"}))


def has_pending() -> bool:
    p = _state.pending
    if p is not None and time.monotonic() > p.expires:
        _state.pending = None
    return _state.pending is not None


def take_pending() -> Pending | None:
    p = _state.pending if has_pending() else None
    _state.pending = None
    return p


def cancel_pending() -> None:
    _state.pending = None


_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|ya|sure|definitely|absolutely|correct|confirm(?:ed)?|affirmative|"
    r"do it|go ahead|go for it|please do|proceed|ok(?:ay)? (?:do it|go ahead|yes)|yes please|that's right)\b"
)
_NO = re.compile(
    r"^(?:no|nope|nah|don't|do not|dont|cancel|stop|never ?mind|negative|abort|leave it|not now|wait)\b"
)


def parse_yes_no(text: str) -> bool | None:
    """True for yes, False for no, None if it's something else (a new request)."""
    t = re.sub(r"[^\w\s']", " ", text.lower())
    t = re.sub(r"^\s*(?:(?:hey|ok|okay)\s+)?(?:arya|aria)\s+", "", re.sub(r"\s+", " ", t)).strip()
    if _NO.match(t):
        return False
    if _YES.match(t) and len(t.split()) <= 6 and not re.search(r"\b(?:but|instead|except|only)\b", t):
        return True
    return None
