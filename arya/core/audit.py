"""
Local audit log: every action ARYA takes on your laptop, and every connection
attempt, one JSON line each in %LOCALAPPDATA%\\ARYA\\logs\\actions.log.

Conversation text is NOT logged — only what was done (tool name, arguments
with long/free-text values redacted, outcome, duration). Nothing here ever
leaves the machine.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

LOG_DIR = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA") / "logs"
LOG_FILE = LOG_DIR / "actions.log"
MAX_BYTES = 5 * 1024 * 1024        # rotate to actions.log.1 beyond 5 MB

# Free-text arguments (note bodies, clipboard contents…) are private — log only their size.
_REDACT_KEYS = {"text", "content", "note", "body", "message", "clipboard"}
_MAX_VALUE = 120

_lock = threading.Lock()


def _redact(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if k.lower() in _REDACT_KEYS and isinstance(v, str):
            out[k] = f"<{len(v)} chars>"
        elif isinstance(v, str) and len(v) > _MAX_VALUE:
            out[k] = v[:_MAX_VALUE] + "…"
        else:
            out[k] = v
    return out


def log_event(kind: str, **fields) -> None:
    """Append one event. Never raises — auditing must not break ARYA."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **fields}
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        print(f"Audit log write failed: {exc}")


def log_tool(tool: str, args: dict, ok: bool, ms: float, error: str | None = None) -> None:
    fields = {"tool": tool, "args": _redact(args), "ok": ok, "ms": round(ms)}
    if error:
        fields["error"] = error[:200]
    log_event("tool", **fields)
