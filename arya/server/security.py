"""
Who may talk to ARYA.

ARYA can run commands, read the clipboard and search files, so the server must
only obey *you*:

  * Loopback only — the server listens on 127.0.0.1, so nothing else on the
    network (café / campus Wi-Fi) can even connect. ARYA_HOST overrides this
    deliberately, never by accident.
  * Host-header allowlist — blocks DNS-rebinding, where a malicious web page
    points its own domain at 127.0.0.1 to reach local servers.
  * Origin check — a web page you visit cannot open a WebSocket to ARYA
    (cross-site WebSocket hijacking); only ARYA's own page can.
  * Secret token — every WebSocket must present it. It is generated once,
    stored in your user profile (outside the repo) and injected into the page
    only for requests from this machine. Other programs: read it from the
    token file and connect to ws://127.0.0.1:8000/ws?token=...
"""
from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

HOST = os.getenv("ARYA_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.getenv("ARYA_PORT", "8000"))

DATA_DIR = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA")
TOKEN_FILE = DATA_DIR / "token"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOWED_HOSTS = _LOOPBACK_HOSTS | {
    h.strip().lower() for h in os.getenv("ARYA_ALLOWED_HOSTS", "").split(",") if h.strip()
}


def is_loopback_bind() -> bool:
    return HOST in _LOOPBACK_HOSTS


def _load_or_create_token() -> str:
    env = os.getenv("ARYA_TOKEN", "").strip()
    if env:
        return env
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    except OSError:
        pass
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(TOKEN_FILE, 0o600)    # no-op on Windows, where %LOCALAPPDATA% is already per-user
    except OSError:
        pass
    return token


TOKEN = _load_or_create_token()


def token_ok(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), TOKEN.encode())


def _hostname(value: str) -> str:
    """'localhost:8000' / '[::1]:8000' / 'http://127.0.0.1:8000' → bare lowercase host."""
    if "//" not in value:
        value = "//" + value
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def host_ok(host_header: str | None) -> bool:
    return bool(host_header) and _hostname(host_header) in ALLOWED_HOSTS


def origin_ok(origin: str | None) -> bool:
    """Browsers always send Origin on WebSockets; it must be ARYA's own page.
    Non-browser clients send none — they still need the token."""
    if origin is None:
        return True
    return _hostname(origin) in ALLOWED_HOSTS


def is_local_client(client_host: str | None) -> bool:
    return client_host in {"127.0.0.1", "::1", "localhost"}
