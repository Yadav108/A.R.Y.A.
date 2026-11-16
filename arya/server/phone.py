"""
Phone access (roadmap C5): talk to ARYA from your phone over home Wi-Fi, and see its nudges there.

  "connect my phone"      turns phone access on and opens a QR code on the laptop; scan it with the phone camera
  "turn off phone access" stops listening on the network
  "new phone link"        makes a new phone key: every phone paired before is locked out
  "is phone access on?"   says whether the phone can reach ARYA, and if not, why

How it stays safe — the laptop's main server (port 8000) is untouched and still answers only this machine:

  * A second, small server (port 8001, ARYA_PHONE_PORT) with only what the phone needs: send a message, get
    the answer as text, read the nudges (timers, reminders, pomodoro, breaks, calendar heads-ups).
  * It listens only on the laptop's home-network address, and only while Windows calls that network
    *Private*. On a Public network (café, campus, a hotspot) it does not listen at all; checked every two
    minutes, so it switches itself off when you leave.
  * Only devices on a private network (192.168.x / 10.x / 172.16-31.x) are answered, and only when the
    Host is the laptop's address (no DNS rebinding).
  * Its own key, separate from the laptop's. Pairing puts it in the QR code after '#', which the browser
    never sends in a URL or to a log; the phone trades it once for an HttpOnly cookie. "new phone link"
    replaces it.
  * Plain HTTP (a home network has no certificate), so the phone can never touch files, the clipboard or
    commands, nor delete / move / close anything (policy.REMOTE_BLOCKED) — even if someone on your Wi-Fi
    read the cookie, that is all they could do.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from arya.server import security

PORT = int(os.getenv("ARYA_PHONE_PORT", "8001"))
SETTINGS_FILE = security.DATA_DIR / "phone.json"
TOKEN_FILE = security.DATA_DIR / "phone_token"
COOKIE = "arya_phone"
CHECK_EVERY_S = 120
MAX_TEXT = 1000
TURN_TIMEOUT_S = 150
FEED_SIZE = 50
PAIR_FAILS_ALLOWED = 10           # wrong keys per device per 10 minutes before it has to wait
STATIC = Path(__file__).resolve().parents[1] / "static" / "phone.html"


# ── Settings and the phone key ────────────────────────────────────────────────

def enabled() -> bool:
    try:
        return bool(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("enabled"))
    except (OSError, ValueError):
        return False


def set_enabled(on: bool) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({"enabled": bool(on)}), encoding="utf-8")
    recheck()


def token() -> str:
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    return new_token()


def new_token() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


def token_ok(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(str(candidate).encode(), token().encode())


# ── Which network ─────────────────────────────────────────────────────────────

def lan_ip() -> str | None:
    """The laptop's address on the network with the default route — if it is a private one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))                  # no packet is sent; just picks the outgoing interface
            ip = s.getsockname()[0]
    except OSError:
        return None
    a = ipaddress.ip_address(ip)
    return ip if a.is_private and not (a.is_loopback or a.is_link_local) else None


def network_profile(ip: str) -> tuple[str, str]:
    """(category, network name) Windows gives the interface with this address: 'Private' / 'Public' /
    'DomainAuthenticated'; ('', '') if it can't tell."""
    ip = str(ipaddress.ip_address(ip))                   # validated: it goes into a command line
    script = (f"$i = (Get-NetIPAddress -IPAddress '{ip}' -ErrorAction Stop).InterfaceIndex; "
              f"$p = Get-NetConnectionProfile -InterfaceIndex $i -ErrorAction Stop | Select-Object -First 1; "
              f"\"$($p.NetworkCategory)|$($p.Name)\"")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "", ""
    category, _, name = out.partition("|")
    return category.strip(), name.strip()


@dataclass
class Where:
    ip: str | None
    why_not: str = ""            # '' = fine; 'no-network' / 'public' / 'unknown'
    network: str = ""


def where_to_listen() -> Where:
    ip = lan_ip()
    if ip is None:
        return Where(None, "no-network")
    category, name = network_profile(ip)
    if category in ("Private", "DomainAuthenticated"):
        return Where(ip, "", name)
    return Where(None, "public" if category == "Public" else "unknown", name)


# ── State shared by the server and the voice commands ─────────────────────────

@dataclass
class _State:
    listening_on: str | None = None
    last: Where | None = None
    hosts: set = field(default_factory=set)               # Host headers the phone server answers to
    feed: deque = field(default_factory=lambda: deque(maxlen=FEED_SIZE))
    next_id: int = 1
    fails: dict = field(default_factory=dict)             # client ip -> deque of failure times
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _State()
_turn: Callable[[str], Awaitable[dict]] | None = None
_loop: asyncio.AbstractEventLoop | None = None
_wake: asyncio.Event | None = None


def set_turn_handler(handler: Callable[[str], Awaitable[dict]]) -> None:
    """websocket.py: runs one typed request through ARYA and returns {'reply': …, 'confirm': bool}."""
    global _turn
    _turn = handler


def note_event(event: str) -> None:
    """Status listener: every spoken nudge ('NOTIFY:…') is kept for the phone. Any thread."""
    if not isinstance(event, str) or not event.startswith("NOTIFY:"):
        return
    text = event[len("NOTIFY:"):].strip()
    if not text:
        return
    with _state.lock:
        _state.feed.append({"id": _state.next_id, "at": time.strftime("%H:%M"), "text": text})
        _state.next_id += 1


def feed_after(after: int) -> list[dict]:
    with _state.lock:
        return [item for item in _state.feed if item["id"] > after]


def recheck() -> None:
    """Look at the network again now instead of in up to two minutes. Any thread."""
    if _loop is not None and _wake is not None:
        with contextlib.suppress(RuntimeError):
            _loop.call_soon_threadsafe(_wake.set)


def pairing_url(ip: str) -> str:
    return f"http://{ip}:{PORT}/#k={token()}"


# ── The phone's own little server ─────────────────────────────────────────────

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                               "connect-src 'self'; img-src 'self' data:; manifest-src 'self'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def client_ok(host: str | None) -> bool:
    try:
        a = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return a.is_private or a.is_loopback


def host_ok(host_header: str | None) -> bool:
    return bool(host_header) and security._hostname(host_header) in _state.hosts


@app.middleware("http")
async def _guard(request: Request, call_next):
    if not client_ok(request.client.host if request.client else None) or not host_ok(request.headers.get("host")):
        return Response("Not here.", status_code=403, media_type="text/plain")
    if request.method == "POST":
        # Only ARYA's own phone page: a page elsewhere can't add this header without a CORS preflight,
        # which this server never answers, and browsers always send Origin on a POST.
        origin = request.headers.get("origin")
        if request.headers.get("x-arya") != "1" or (origin is not None and not host_ok(origin)):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    response = await call_next(request)
    response.headers.update(_HEADERS)
    return response


def _paired(request: Request) -> bool:
    return token_ok(request.cookies.get(COOKIE))


def _too_many_fails(client: str) -> bool:
    now = time.monotonic()
    fails = _state.fails.setdefault(client, deque())
    while fails and now - fails[0] > 600:
        fails.popleft()
    return len(fails) >= PAIR_FAILS_ALLOWED


@app.get("/")
async def page() -> HTMLResponse:
    return HTMLResponse(STATIC.read_text(encoding="utf-8"))


@app.get("/manifest.json")
async def manifest() -> JSONResponse:
    return JSONResponse({"name": "ARYA", "short_name": "ARYA", "start_url": "/", "display": "standalone",
                         "background_color": "#0f1115", "theme_color": "#0f1115"})


@app.post("/api/pair")
async def pair(request: Request) -> JSONResponse:
    from arya.core.audit import log_event

    client = request.client.host if request.client else "?"
    if _too_many_fails(client):
        return JSONResponse({"error": "too many tries — wait 10 minutes"}, status_code=429)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    if not token_ok(body.get("key") if isinstance(body, dict) else None):
        _state.fails[client].append(time.monotonic())
        log_event("phone_pair_rejected", client=client)
        print(f"Phone: wrong key from {client}")
        return JSONResponse({"error": "this link is old — say \"connect my phone\" at the laptop"}, status_code=401)
    log_event("phone_paired", client=client)
    print(f"Phone: paired {client}")
    response = JSONResponse({"ok": True})
    response.set_cookie(COOKIE, token(), max_age=365 * 24 * 3600, httponly=True, samesite="strict", path="/")
    return response


@app.get("/api/hello")
async def hello(request: Request) -> JSONResponse:
    if not _paired(request):
        return JSONResponse({"paired": False}, status_code=401)
    with _state.lock:
        last = _state.next_id - 1
    from arya.core import modes
    return JSONResponse({"paired": True, "last": last, "recent": feed_after(max(0, last - 5)),
                         "mode": modes.current_name()})


@app.get("/api/feed")
async def feed(request: Request, after: int = 0) -> JSONResponse:
    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    from arya.core import modes
    return JSONResponse({"items": feed_after(after), "mode": modes.current_name()})


@app.post("/api/say")
async def say(request: Request) -> JSONResponse:
    from arya.core.audit import log_event

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str(body.get("text", "") if isinstance(body, dict) else "").strip()
    if not text:
        return JSONResponse({"error": "empty"}, status_code=400)
    if len(text) > MAX_TEXT:
        return JSONResponse({"error": f"keep it under {MAX_TEXT} characters"}, status_code=400)
    if _turn is None:
        return JSONResponse({"error": "ARYA is still starting"}, status_code=503)
    log_event("phone_request", chars=len(text))
    try:
        result = await asyncio.wait_for(_turn(text), timeout=TURN_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse({"reply": "That took too long — try again, or ask at the laptop.", "confirm": False})
    return JSONResponse(result)


@app.post("/api/mode")
async def mode(request: Request) -> JSONResponse:
    """The mode switch on the phone page: default / study / work (arya/core/modes.py)."""
    from arya.core import modes

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    name = str(body.get("mode", "") if isinstance(body, dict) else "").lower()
    if name not in modes.MODES:
        return JSONResponse({"error": "mode must be default, study or work"}, status_code=400)
    modes.set_mode(name, reason="phone")
    return JSONResponse({"ok": True, "mode": name})


@app.post("/api/unpair")
async def unpair() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


# ── Starting and stopping it ──────────────────────────────────────────────────

def _make_server(ip: str):
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):              # Ctrl+C and friends belong to the main server
            yield

    config = uvicorn.Config(app, host=ip, port=PORT, log_level="warning", access_log=False, lifespan="off")
    return _Server(config)


async def _serve(server, sock: socket.socket) -> None:
    try:
        await server.serve(sockets=[sock])
    except (Exception, SystemExit) as exc:      # uvicorn exits the process on errors — never let that out
        print(f"Phone: server stopped ({type(exc).__name__}: {exc})")
    finally:
        with contextlib.suppress(OSError):
            sock.close()


async def run() -> None:
    """Started by the main server: keeps the phone server running exactly while it should."""
    global _loop, _wake
    _loop, _wake = asyncio.get_running_loop(), asyncio.Event()
    server, task = None, None
    while True:
        try:
            where = await asyncio.to_thread(where_to_listen) if enabled() else None
            _state.last = where
            want = where.ip if where else None
            if task is not None and (want != _state.listening_on or task.done()):
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, timeout=10)
                print(f"Phone: stopped listening on {_state.listening_on}")
                server, task, _state.listening_on, _state.hosts = None, None, None, set()
            if want and task is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind((want, PORT))
                except OSError as exc:
                    sock.close()
                    print(f"Phone: can't listen on {want}:{PORT} ({exc})")
                else:
                    _state.hosts = {want}
                    server = _make_server(want)
                    task = asyncio.create_task(_serve(server, sock))
                    _state.listening_on = want
                    print(f"Phone: listening on http://{want}:{PORT} (network {where.network!r}, Private)")
            elif enabled() and where and where.why_not and task is None:
                print(f"Phone: not listening — {where.why_not} network {where.network!r}")
        except Exception as exc:
            print(f"Phone: check failed: {type(exc).__name__}: {exc}")
        _wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=CHECK_EVERY_S)


# ── Pairing page on the laptop (main server, this machine only) ───────────────

def pairing_page() -> str:
    where = where_to_listen()
    _state.last = where
    if where.ip:
        import segno

        url = pairing_url(where.ip)
        qr = segno.make(url, error="m").svg_inline(scale=7, dark="#111", light="#fff")
        body = (f"<div class='qr'>{qr}</div><p>Scan this with your phone camera. The phone must be on the same "
                f"Wi-Fi ({html.escape(where.network or 'this network')}).</p>"
                f"<p class='small'>Or type <b>http://{where.ip}:{PORT}</b> — but only the QR code pairs the phone. "
                f"Anyone who sees this code can use ARYA from the phone page until you say \"new phone link\".</p>")
    elif where.why_not == "public":
        body = (f"<p class='warn'>Windows treats <b>{html.escape(where.network or 'this Wi-Fi')}</b> as a "
                f"<b>Public</b> network, so ARYA won't listen on it.</p><p>If this is your home Wi-Fi: Settings → "
                f"Network &amp; internet → Wi-Fi → {html.escape(where.network or 'your network')} → "
                f"Network profile type → <b>Private</b>. Then reload this page.</p>")
    elif where.why_not == "no-network":
        body = "<p class='warn'>The laptop isn't on a home network right now.</p>"
    else:
        body = "<p class='warn'>I couldn't tell what kind of network this is, so I'm not listening on it.</p>"
    status = "on" if enabled() else "off (say \"connect my phone\")"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Connect your phone — ARYA</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:Segoe UI,system-ui,sans-serif;background:#0f1115;color:#e8e8ea;max-width:560px;margin:40px auto;padding:0 16px;line-height:1.5}}
h1{{font-size:22px}}.qr{{background:#fff;display:inline-block;padding:12px;border-radius:12px}}.warn{{color:#ffcf6e}}
.small{{color:#9aa0aa;font-size:14px}}</style></head><body><h1>Connect your phone</h1>
<p class="small">Phone access is {status}.</p>{body}</body></html>"""


def open_pairing_page() -> None:
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{security.PORT}/phone")


# ── Voice ─────────────────────────────────────────────────────────────────────

# "connect my phone", "connect phone" (from the log) — but not "connect my phone to the TV / Bluetooth",
# "link my phone number"
_PHONE = (r"(?:(?:my|the|a)\s+)?(?:phone|mobile)(?!\s+(?:number|bill|case|charger|call|screen)\b)"
          r"(?!\s+(?:to|with)\b(?!\s+(?:arya|aria|you|the laptop|this laptop)\b))")
_NEW = re.compile(r"\b(?:new|reset|change|renew)\s+(?:the\s+)?phone\s+(?:link|code|key|access)\b|"
                  r"\bforget\s+my\s+phone\b", re.IGNORECASE)
_OFF = re.compile(r"\b(?:turn|switch)\s+off\s+(?:the\s+)?phone\s+access\b|"
                  r"\b(?:disable|stop)\s+(?:the\s+)?phone\s+access\b|\bphone\s+access\s+off\b|"
                  rf"\b(?:disconnect|unpair)\s+{_PHONE}\b", re.IGNORECASE)
_ON = re.compile(r"\b(?:turn|switch)\s+on\s+(?:the\s+)?phone\s+access\b|"
                 r"\b(?:enable|start|set\s+up)\s+(?:the\s+)?phone\s+access\b|\bphone\s+access\s+on\b|"
                 rf"\b(?:connect|pair|link)\s+{_PHONE}\b|"
                 r"\b(?:show|open|give)\s+(?:me\s+)?(?:the\s+)?phone\s+(?:link|code|qr(?:\s+code)?)\b",
                 re.IGNORECASE)
_STATUS = re.compile(r"\bis\s+(?:the\s+)?phone\s+access\s+(?:on|off|working|enabled)\b|"
                     r"\bphone\s+access\s+status\b|\bcan\s+my\s+phone\s+(?:reach|connect\s+to|talk\s+to)\b",
                     re.IGNORECASE)


def matches(message: str) -> bool:
    """A phone-access command (no side effects) — so smart listening never drops "connect phone"."""
    return any(p.search(message) for p in (_NEW, _STATUS, _OFF, _ON))


def _why(where: Where) -> str:
    if where.why_not == "public":
        net = f"“{where.network}”" if where.network else "this Wi-Fi"
        return (f"Windows treats {net} as a public network, so I'm not listening on it. If it's your home Wi-Fi, "
                f"set it to Private in Settings, Network and internet, Wi-Fi, then the network")
    if where.why_not == "no-network":
        return "the laptop isn't on a home network right now"
    return "I couldn't tell what kind of network this is, so I'm staying off it"


def handle(message: str, remote: bool = False) -> str | None:
    if _NEW.search(message):
        if remote:
            return "Make a new phone link at the laptop — from here it would lock this phone out."
        new_token()
        if enabled():
            open_pairing_page()
            return "Done: phones paired before can't reach me any more. The new QR code is on your screen; scan it to pair again."
        return "Done: phones paired before can't reach me any more. Say \"connect my phone\" when you want one back."
    if _STATUS.search(message):                     # before on/off: "is phone access on?" must not turn it on
        if not enabled():
            return "Phone access is off. Say \"connect my phone\" to turn it on."
        where = _state.last or where_to_listen()
        if _state.listening_on:
            return f"Yes, your phone can reach me at {_state.listening_on}, port {PORT}, on this Wi-Fi."
        if where.ip:
            recheck()
            return "Phone access is on; I'll be reachable on this Wi-Fi in a moment."
        return f"Phone access is on, but {_why(where)}."
    if _OFF.search(message):
        set_enabled(False)
        return ("Phone access is off now; turn it back on at the laptop." if remote
                else "Phone access is off. I only listen on this laptop again.")
    if _ON.search(message):
        if remote:
            return "You're already connected from your phone."
        set_enabled(True)
        where = where_to_listen()
        open_pairing_page()
        if where.ip:
            return ("Phone access is on. The QR code is on your screen — scan it with your phone camera; the phone needs "
                    "to be on the same Wi-Fi. From the phone you can ask anything and see my reminders, but not "
                    "touch files or commands.")
        return f"Phone access is on, but {_why(where)}. The details are on your screen."
    return None
