"""
Phone access (roadmap C5): talk to ARYA from your phone over home Wi-Fi, and see its nudges there.

  "connect my phone"      turns phone access on and opens a QR code on the laptop; scan it with the phone camera
  "turn off phone access" stops listening on the network
  "new phone link"        makes a new phone key: every phone paired before is locked out
  "is phone access on?"   says whether the phone can reach ARYA, and if not, why

How it stays safe — the laptop's main server (port 8000) is untouched and still answers only this machine:

  * A second, small server (port 8001, ARYA_PHONE_PORT) with only what the phone needs: send a message, get
    the answer as text, read the nudges (timers, reminders, pomodoro, breaks, calendar heads-ups).
  * It listens only on the laptop's address on the current network, and only when that network is one
    Windows calls *Private*, or a Wi-Fi that needs a password or login to join (WPA2 / WPA3, personal or
    enterprise like eduroam — ARYA_PHONE_ANY_WIFI=0 for Private only). On open Wi-Fi or a Public wired
    network it does not listen at all; checked every two minutes, so it switches itself off when you leave.
    (Tailscale, when it's up, works on any network and is encrypted end to end.)
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
import io

from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from arya.server import security

PORT = int(os.getenv("ARYA_PHONE_PORT", "8001"))
ANY_SECURED_WIFI = os.getenv("ARYA_PHONE_ANY_WIFI", "1") != "0"   # 0: home (Private) Wi-Fi only, as before
SETTINGS_FILE = security.DATA_DIR / "phone.json"
TOKEN_FILE = security.DATA_DIR / "phone_token"
COOKIE = "arya_phone"
CHECK_EVERY_S = 120
MAX_TEXT = 1000
TURN_TIMEOUT_S = 150
FEED_SIZE = 200                   # events kept (memory and disk): what an app offline for a day catches up on
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


def interface_alias(ip: str) -> str:
    """The Windows interface name ('Wi-Fi', 'Ethernet') that has this address; '' if unknown."""
    ip = str(ipaddress.ip_address(ip))                   # validated: it goes into a command line
    try:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               f"(Get-NetIPAddress -IPAddress '{ip}' -ErrorAction Stop).InterfaceAlias"],
                              capture_output=True, text=True, timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# Wi-Fi that only people with the password (or a university / work login) can join. Not open Wi-Fi, not OWE
# ("Enhanced Open": encrypted, but anyone can join), not WEP (broken).
_SECURED = re.compile(r"^(?:WPA2|WPA3|WPA)(?:-?\w+)*$", re.IGNORECASE)


def wifi_security(netsh_output: str, alias: str) -> tuple[str, str] | None:
    """(authentication, SSID) of the connected Wi-Fi interface named `alias` in `netsh wlan show interfaces`;
    None when that interface isn't Wi-Fi, isn't connected, or the output can't be read."""
    for block in re.split(r"\n\s*\n(?=\s*Name\s*:)", netsh_output):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep and key.strip() and key.strip() not in fields:
                fields[key.strip()] = value.strip()
        if fields.get("Name") == alias and fields.get("State", "").lower() == "connected":
            return fields.get("Authentication", ""), fields.get("SSID", "")
    return None


def secured(authentication: str) -> bool:
    return bool(_SECURED.match(authentication.strip())) and "OWE" not in authentication.upper()


def _wifi_security(ip: str) -> tuple[str, str] | None:
    alias = interface_alias(ip)
    if not alias:
        return None
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return wifi_security(out, alias)


@dataclass
class Where:
    ip: str | None
    why_not: str = ""            # '' = fine; 'no-network' / 'public' / 'open' / 'unknown'
    network: str = ""
    how: str = ""                # why it's fine: 'Private' / 'WPA2-Personal' …


def where_to_listen() -> Where:
    """Home Wi-Fi Windows calls Private (as before), or any Wi-Fi that needs a password or login to join (WPA2 /
    WPA3, personal or enterprise like eduroam) even when Windows calls it Public — a café's FRITZ!Box, a
    friend's flat, the university. Never open Wi-Fi, and never a Public wired network (no way to tell who's on
    it). The phone still needs the pairing key for everything."""
    ip = lan_ip()
    if ip is None:
        return Where(None, "no-network")
    category, name = network_profile(ip)
    if category in ("Private", "DomainAuthenticated"):
        return Where(ip, "", name, category)
    if category == "Public" and ANY_SECURED_WIFI:
        wifi = _wifi_security(ip)
        if wifi and secured(wifi[0]):
            return Where(ip, "", wifi[1] or name, wifi[0])
        if wifi:
            return Where(None, "open", wifi[1] or name)
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
    loaded: bool = False                                  # events read back from disk
    servers: dict = field(default_factory=dict)           # ip -> (uvicorn server, task): home Wi-Fi and Tailscale


_state = _State()
_turn: Callable[[str], Awaitable[dict]] | None = None
_loop: asyncio.AbstractEventLoop | None = None
_wake: asyncio.Event | None = None


def set_turn_handler(handler: Callable[[str], Awaitable[dict]]) -> None:
    """websocket.py: runs one typed request through ARYA and returns {'reply': …, 'confirm': bool}."""
    global _turn
    _turn = handler


_upload: Callable | None = None


def set_upload_handler(handler: Callable) -> None:
    """websocket.py: receive_upload(request, name) — the same checks and processing as the laptop app."""
    global _upload
    _upload = handler


_voice: Callable | None = None
VOICE_MAX_S = 30                  # one spoken request: at most this long
VOICE_RATE = 16000


def set_voice_handler(handler: Callable) -> None:
    """websocket.py: phone_voice_turn(audio) — an async iterator of {"type": "heard" | "say" | "done", …}."""
    global _voice
    _voice = handler


def wav_to_array(data: bytes):
    """The app's recording (WAV, 16 kHz, mono, 16-bit) -> float32 samples. ValueError if it's anything else."""
    import wave

    import numpy as np
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != VOICE_RATE:
                raise ValueError("expected 16 kHz mono 16-bit WAV")
            frames = w.readframes(min(w.getnframes(), VOICE_MAX_S * VOICE_RATE))
    except wave.Error as exc:
        raise ValueError(f"not a WAV file ({exc})") from exc
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def note_event(event: str) -> None:
    """Status listener: every spoken nudge ('NOTIFY:…') is kept for the phone, and a recording you
    uploaded becoming ready (or failing) too. Any thread."""
    if not isinstance(event, str):
        return
    if event.startswith("ARYA_UPLOAD:"):
        try:
            u = json.loads(event[len("ARYA_UPLOAD:"):])
        except ValueError:
            return
        if u.get("status") == "ready":
            text = f"📎 {u.get('title', 'Your file')} is ready — ask me anything about it."
        elif u.get("status") == "failed":
            text = f"📎 I couldn't read {u.get('title', 'your file')}."
        else:
            return                                     # progress: not worth a nudge
    elif event.startswith("NOTIFY:"):
        text = event[len("NOTIFY:"):].strip()
    else:
        return
    if not text:
        return
    push(text)


# What the ARYA phone app turns into Android notifications (everything else is only shown in the feed).
NOTIFY_KINDS = {"reminder", "timer", "alarm", "calendar", "meeting", "briefing", "wrapup", "ring", "message"}
EVENTS_FILE = security.DATA_DIR / "phone_events.jsonl"
DEDUPE_S = 15                     # a reminder pushed typed is not pushed again as its generic "NOTIFY" copy


def push(text: str, kind: str = "nudge", url: str | None = None, title: str | None = None,
         ref: dict | None = None) -> dict | None:
    """Put something on the phone's feed: a nudge, a message sent from the laptop ('message', with an optional
    link), 'ring' (find my phone), or a typed event the app notifies about (NOTIFY_KINDS: reminder, timer, alarm,
    calendar, meeting, briefing, wrapup). Events are numbered (ids keep growing across restarts) and kept on disk,
    so the app catches up on what it missed while it was offline. Any thread."""
    _load_feed()
    now = time.time()
    with _state.lock:
        if kind == "nudge" and any(i["text"] == text and i["kind"] != "nudge" and now - i.get("ts", 0) < DEDUPE_S
                                   for i in list(_state.feed)[-10:]):
            return None                                # already sent as a typed event
        item = {"id": max(_state.next_id, int(now * 1000)), "at": time.strftime("%H:%M"), "ts": round(now, 3),
                "text": text, "kind": kind}
        if title:
            item["title"] = title
        if ref:
            item["ref"] = ref
        if url and re.match(r"^https?://", url, re.IGNORECASE):       # only web links become tappable
            item["url"] = url
        _state.feed.append(item)
        _state.next_id = item["id"] + 1
        try:
            EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with EVENTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            pass
    _wake_waiters_threadsafe()
    return item


def push_event(kind: str, title: str, text: str, ref: dict | None = None) -> None:
    """A typed event for the phone app (reminders, timers, calendar, briefing …). Never raises."""
    try:
        push(text, kind=kind, title=title, ref=ref)
    except Exception as exc:
        print(f"Phone: couldn't queue {kind} ({type(exc).__name__})")


def _load_feed() -> None:
    """Once per run: the last events from disk (the file is trimmed to FEED_SIZE lines here)."""
    if _state.loaded:
        return
    with _state.lock:
        if _state.loaded:
            return
        _state.loaded = True
        try:
            lines = EVENTS_FILE.read_text(encoding="utf-8").splitlines()[-FEED_SIZE:]
        except OSError:
            return
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            _state.feed.append(item)
            _state.next_id = max(_state.next_id, int(item.get("id", 0)) + 1)
        try:
            EVENTS_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        except OSError:
            pass


def feed_after(after: int) -> list[dict]:
    _load_feed()
    with _state.lock:
        return [item for item in _state.feed if item["id"] > after]


def last_id() -> int:
    _load_feed()
    with _state.lock:
        return _state.feed[-1]["id"] if _state.feed else 0


# Long-polling (/api/wait): the app's background service waits here; a new event answers at once.
_waiters: set = set()


def _wake_waiters() -> None:
    for fut in list(_waiters):
        if not fut.done():
            fut.set_result(True)


def _wake_waiters_threadsafe() -> None:
    try:
        asyncio.get_running_loop()
        _wake_waiters()                                # we're on the server's loop
    except RuntimeError:
        if _loop is not None:
            with contextlib.suppress(RuntimeError):
                _loop.call_soon_threadsafe(_wake_waiters)


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


TAILNET = ipaddress.ip_network("100.64.0.0/10")         # Tailscale's addresses (Python doesn't call them private)


def tailscale_ip() -> str | None:
    """The laptop's Tailscale address (100.x.y.z) when Tailscale is running and signed in, else None."""
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and ipaddress.ip_address(a.address) in TAILNET:
                    return a.address
    except Exception:
        pass
    return None


def client_ok(host: str | None) -> bool:
    try:
        a = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return a.is_private or a.is_loopback or (a.version == 4 and a in TAILNET)


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
    """The phone page (cookie) or the ARYA app (Authorization: Bearer <key>, from the same pairing QR code)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return token_ok(auth[7:].strip())
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


_NONCE = re.compile(r"^[0-9a-f]{32,64}$")


def prove(nonce: str, key: str | None = None) -> str:
    """HMAC-SHA256 of the phone's random challenge with the pairing key: shows this is the phone's own laptop
    without the key ever crossing the network (android/…/Finder.kt checks it before sending the key)."""
    import hashlib
    return hmac.new((key or token()).encode(), b"arya-prove:" + nonce.encode(), hashlib.sha256).hexdigest()


@app.get("/api/prove")
async def prove_endpoint(n: str = "") -> JSONResponse:
    """No key needed (that's the point): the app asks before it trusts an address on a Wi-Fi — an old home address
    on someone else's network, or one it found by looking around the Wi-Fi it's on. Phone access must be on."""
    if not _NONCE.match(n) or not enabled():
        return JSONResponse({"error": "bad request"}, status_code=400)
    return JSONResponse({"proof": prove(n)})


@app.get("/api/hello")
async def hello(request: Request) -> JSONResponse:
    if not _paired(request):
        return JSONResponse({"paired": False}, status_code=401)
    from arya.core import health, modes
    with _state.lock:
        recent = list(_state.feed)[-5:]
    servers = sorted(_state.servers)
    return JSONResponse({"paired": True, "last": last_id(), "recent": recent, "mode": modes.current_name(),
                         "addresses": servers, "notify_kinds": sorted(NOTIFY_KINDS),
                         "wake": health.wake_target(servers)})     # the app's "Wake the laptop" (home Wi-Fi)


@app.get("/api/wait")
async def wait(request: Request, after: int = 0, timeout: float = 50.0) -> JSONResponse:
    """The app's background service: everything after `after`, at once if there is any, else as soon as something
    arrives (at most `timeout` s, then an empty answer and the app asks again). Missed events are in the answer
    too, so a phone that was offline catches up."""
    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    items = feed_after(after)
    if not items:
        fut = asyncio.get_running_loop().create_future()
        _waiters.add(fut)
        try:
            await asyncio.wait_for(fut, timeout=max(1.0, min(timeout, 55.0)))
        except asyncio.TimeoutError:
            pass
        finally:
            _waiters.discard(fut)
        items = feed_after(after)
    return JSONResponse({"items": items, "last": last_id()})


SNOOZABLE = {"reminder", "timer", "alarm"}


@app.post("/api/action")
async def action(request: Request) -> JSONResponse:
    """A notification button in the app: {"id": event id, "action": "snooze" | "done", "minutes": 10}."""
    from arya.core.audit import log_event

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    act = str(body.get("action", ""))
    try:
        event_id = int(body.get("id", 0))
    except (TypeError, ValueError):
        event_id = 0
    with _state.lock:
        item = next((i for i in _state.feed if i["id"] == event_id), None)
    if item is None:
        return JSONResponse({"error": "no such event (too old?)"}, status_code=404)
    log_event("phone_action", action=act, event_kind=item.get("kind"))
    if act == "done":
        item["done"] = True
        return JSONResponse({"ok": True, "reply": "Done."})
    if act == "snooze" and item.get("kind") in SNOOZABLE:
        try:
            minutes = max(1.0, min(24 * 60.0, float(body.get("minutes", 10))))
        except (TypeError, ValueError):
            minutes = 10.0
        label = (item.get("ref") or {}).get("label") or item.get("text", "Reminder")
        from arya.tools.productivity import set_reminder
        await set_reminder(label, in_minutes=minutes)
        item["done"] = True
        return JSONResponse({"ok": True, "reply": f"Snoozed for {minutes:g} minutes."})
    return JSONResponse({"error": "action must be snooze (reminders, timers, alarms) or done"}, status_code=400)


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
    # The offline brain lives on the phone (android/…/PhoneBrain.kt): typed "download the offline brain" goes to the
    # app, which does it and shows how it went; in a plain browser there is no phone side to do it.
    from arya.server import phone_actions
    action = phone_actions.parse(text)
    if action and action.get("action") == "phone_brain":
        print(f"Phone request: {text[:60]} -> phone action phone_brain ({action['do']})")
        log_event("phone_action", action="phone_brain", do=action["do"])
        return JSONResponse({"reply": "Open the ARYA app on the phone and type or say this there: the offline brain "
                                      "lives on the phone.", "confirm": False,
                             "action": {"action": "phone_brain", "do": action["do"]}})
    try:
        result = await asyncio.wait_for(_turn(text), timeout=TURN_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse({"reply": "That took too long — try again, or ask at the laptop.", "confirm": False})
    return JSONResponse(result)


@app.post("/api/upload")
async def upload(request: Request, name: str = "") -> JSONResponse:
    """A photo from the camera, or a document / audio / video from the phone, sent TO ARYA as context.
    Send-only: the phone still never reads the laptop's files. Same type, size and disk checks as the
    laptop app; the file's text is untrusted (actions after reading it need a "yes")."""
    from arya.core.audit import log_event

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    if _upload is None:
        return JSONResponse({"error": "ARYA is still starting"}, status_code=503)
    log_event("phone_upload", client=request.client.host if request.client else "?")
    return await _upload(request, name)


@app.post("/api/voice")
async def voice(request: Request):
    """You spoke in the ARYA app: the body is the recording (16 kHz mono WAV, at most 30 s). The answer streams
    back as lines of JSON (application/x-ndjson), so the phone shows what was heard at once and plays the first
    sentence while ARYA is still thinking about the next."""
    from arya.core.audit import log_event

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    if _voice is None:
        return JSONResponse({"error": "ARYA is still starting"}, status_code=503)
    body = bytearray()
    limit = VOICE_MAX_S * VOICE_RATE * 2 + 4096
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return JSONResponse({"error": f"keep it under {VOICE_MAX_S} seconds"}, status_code=413)
    try:
        audio = wav_to_array(bytes(body))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if len(audio) < VOICE_RATE // 4:
        return JSONResponse({"error": "too short"}, status_code=400)
    log_event("phone_voice", seconds=round(len(audio) / VOICE_RATE, 1))     # never the audio or the words

    async def lines():
        try:
            async for event in _voice(audio):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:
            print(f"Phone voice: failed ({type(exc).__name__}: {exc})")
            yield json.dumps({"type": "done", "error": "Something went wrong on the laptop."}) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson")


# ── The offline brain's model file, handed to the phone over Wi-Fi (android/…/PhoneBrainEngine.kt) ─────────────
# The phone's own download from the internet can stall for ever on some phones; from the laptop it's fast. The
# laptop fetches the file once (from the same public source), checks it against the published SHA-256, keeps it.

BRAIN_NAME = "Qwen2.5-1.5B-Instruct_multi-prefill-seq_q8_ekv1280.task"
BRAIN_URL = f"https://huggingface.co/litert-community/Qwen2.5-1.5B-Instruct/resolve/main/{BRAIN_NAME}"
BRAIN_SIZE = 1_597_913_616
BRAIN_SHA256 = "8d867a7c93a6acf2892f08e0174e2f6f351ad256b7e3cfb6d6cd9c89794b42e0"
BRAIN_FILE = security.DATA_DIR / "brain" / BRAIN_NAME
_brain_fetch: dict = {"running": False, "error": ""}


def _brain_ready() -> bool:
    ok = security.DATA_DIR / "brain" / (BRAIN_NAME + ".ok")       # checked once; the size guards against changes
    return BRAIN_FILE.is_file() and BRAIN_FILE.stat().st_size == BRAIN_SIZE and ok.is_file()


def _fetch_brain() -> None:
    """Downloads and checks the model file (a thread; ≈ 1–2 min on a good line). Resumes a partial download."""
    import hashlib
    import urllib.request
    part = BRAIN_FILE.with_name(BRAIN_NAME + ".part")
    try:
        BRAIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if BRAIN_FILE.is_file() and BRAIN_FILE.stat().st_size == BRAIN_SIZE:
            BRAIN_FILE.replace(part)                              # whole but unchecked: check it below
        have = part.stat().st_size if part.exists() else 0
        if have < BRAIN_SIZE:
            req = urllib.request.Request(BRAIN_URL, headers={"Range": f"bytes={have}-", "User-Agent": "ARYA"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab" if r.status == 206 else "wb") as out:
                while chunk := r.read(1 << 20):
                    out.write(chunk)
        digest = hashlib.sha256()
        with open(part, "rb") as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
        if digest.hexdigest() != BRAIN_SHA256:
            part.unlink(missing_ok=True)
            raise ValueError("the file didn't match its published checksum")
        part.replace(BRAIN_FILE)
        BRAIN_FILE.with_name(BRAIN_NAME + ".ok").write_text(BRAIN_SHA256, encoding="utf-8")
        _brain_fetch["error"] = ""
        print("Phone: offline brain file ready on the laptop for the phone")
    except Exception as exc:
        _brain_fetch["error"] = f"{type(exc).__name__}: {exc}"
        print(f"Phone: couldn't get the offline brain file — {_brain_fetch['error']}")
    finally:
        _brain_fetch["running"] = False


@app.api_route("/api/brain-file", methods=["GET", "HEAD"])
async def brain_file(request: Request):
    """The model file for the paired phone: 200 (resumable, Range) when the laptop has it checked; 202 while it
    fetches it (started by this request); 503 when that failed."""
    from fastapi.responses import FileResponse

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    if _brain_ready():
        return FileResponse(BRAIN_FILE, media_type="application/octet-stream", filename=BRAIN_NAME)
    if not _brain_fetch["running"]:
        if _brain_fetch["error"] and request.method == "HEAD":
            return JSONResponse({"error": _brain_fetch["error"]}, status_code=503)
        _brain_fetch.update(running=True, error="")
        import threading
        threading.Thread(target=_fetch_brain, name="arya-brain-fetch", daemon=True).start()
        print("Phone: fetching the offline brain file for the phone (1.6 GB, once)")
    return JSONResponse({"preparing": True}, status_code=202)


PHONE_CLOUD = os.getenv("ARYA_PHONE_CLOUD", "1") != "0"
PHONE_CLOUD_MODEL = os.getenv("ARYA_PHONE_CLOUD_MODEL", "openai/gpt-oss-20b")
_inbox_seen: set[int] = set()
_last_brain = [""]                 # the phone's last reported offline-brain status (logged when it changes)


@app.get("/api/offline-config")
async def offline_config(request: Request) -> JSONResponse:
    """What the phone needs when this laptop is out of reach (android/…/OfflineServices.kt): the home city and its
    coordinates for the weather, and — because the cloud was chosen for questions while the laptop is away (asked
    each time on the phone) — the Groq key and model. Only to a paired phone; ARYA_PHONE_CLOUD=0 leaves the key out."""
    from arya.core.audit import log_event

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    cfg: dict = {"city": os.getenv("DEFAULT_CITY", "Berlin"), "cloud_model": PHONE_CLOUD_MODEL}
    for key, env in (("lat", "DEFAULT_LAT"), ("lon", "DEFAULT_LON")):
        try:
            cfg[key] = float(os.getenv(env, ""))
        except ValueError:
            pass
    groq = os.getenv("GROQ_API_KEY", "").strip()
    if PHONE_CLOUD and groq:
        cfg["groq_key"] = groq
    # The phone says how its offline brain is (PhoneBrainEngine.kt): MISSING / DOWNLOADING 42% / READY / FAILED …
    brain = re.sub(r"[^\w %:.,()?'/—-]", "",request.query_params.get("brain", ""))[:120]
    if brain and brain != _last_brain[0]:
        _last_brain[0] = brain
        print(f"Phone: offline brain {brain}")
    log_event("phone_offline_config", cloud=bool(cfg.get("groq_key")), brain=brain or None)   # never the key itself
    return JSONResponse(cfg)


@app.post("/api/inbox")
async def inbox(request: Request) -> JSONResponse:
    """Notes and to-dos said to the phone while this laptop was out of reach: filed like any capture (your to-dos,
    ideas or notes — arya/core/capture.py). Each item once, even if the phone sends it again."""
    from arya.core import capture
    from arya.core.audit import log_event

    if not _paired(request):
        return JSONResponse({"error": "unpaired"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    items = body.get("items", []) if isinstance(body, dict) else []
    filed = 0
    for item in items[:200]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()[:MAX_TEXT]
        try:
            iid = int(item.get("id", 0))
        except (TypeError, ValueError):
            iid = 0
        if not text or (iid and iid in _inbox_seen):
            continue
        await capture.file(text, source="phone-offline")
        if iid:
            _inbox_seen.add(iid)
        filed += 1
    log_event("phone_inbox", items=filed)
    if filed:
        print(f"Phone: filed {filed} note(s) said while the laptop was away")
    return JSONResponse({"filed": filed})


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
        return JSONResponse({"error": "mode must be default, study, work or brainstorm"}, status_code=400)
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
    told = ""
    while True:
        try:
            where = await asyncio.to_thread(where_to_listen) if enabled() else None
            ts = await asyncio.to_thread(tailscale_ip) if enabled() else None
            _state.last = where
            # Home Wi-Fi only while Windows calls it Private; Tailscale whenever it's up (only your own devices,
            # signed in to your account, can reach that address — and the key is still needed).
            want = {ip for ip in ((where.ip if where else None), ts) if ip}
            for ip in list(_state.servers):
                server, task = _state.servers[ip]
                if ip not in want or task.done():
                    server.should_exit = True
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(task, timeout=10)
                    del _state.servers[ip]
                    print(f"Phone: stopped listening on {ip}")
            for ip in sorted(want - set(_state.servers)):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind((ip, PORT))
                except OSError as exc:
                    sock.close()
                    print(f"Phone: can't listen on {ip}:{PORT} ({exc})")
                    continue
                server = _make_server(ip)
                _state.servers[ip] = (server, asyncio.create_task(_serve(server, sock)))
                how = "Tailscale" if ip == ts else f"network {where.network!r}, {where.how or 'Private'}"
                print(f"Phone: listening on http://{ip}:{PORT} ({how})")
            _state.hosts = set(_state.servers)
            # The address for pairing: Tailscale when there is one (it works at home too, and everywhere else).
            _state.listening_on = ts if ts in _state.servers else (where.ip if where and where.ip in _state.servers
                                                                   else None)
            note = f"{where.why_not} network {where.network!r}" if enabled() and where and where.why_not else ""
            if note and not _state.servers and note != told:
                print(f"Phone: not listening — {note}")
            told = note
        except Exception as exc:
            print(f"Phone: check failed: {type(exc).__name__}: {exc}")
        _wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=CHECK_EVERY_S)


# ── Pairing page on the laptop (main server, this machine only) ───────────────

def pairing_page() -> str:
    where = where_to_listen()
    _state.last = where
    ts = tailscale_ip()
    if where.ip or ts:
        import segno

        url = pairing_url(ts or where.ip)
        qr = segno.make(url, error="m").svg_inline(scale=7, dark="#111", light="#fff")
        reach = ("through <b>Tailscale</b>: it works at home and anywhere else, as long as Tailscale is on on both "
                 "devices" if ts else f"on the same Wi-Fi ({html.escape(where.network or 'this network')}) only — "
                 "install Tailscale on both devices to reach ARYA from anywhere")
        also = (f" At home it also answers on <b>http://{where.ip}:{PORT}</b>." if ts and where.ip else "")
        body = (f"<div class='qr'>{qr}</div><p>Scan this with the <b>ARYA app</b> (Pair → Scan), or with the "
                f"phone camera for the browser page. The phone reaches ARYA {reach}.{also}</p>"
                f"<p class='small'>Only the QR code pairs a phone. Anyone who sees this code can use ARYA from a "
                f"phone until you say \"new phone link\".</p>")
    elif where.why_not == "public":
        body = (f"<p class='warn'>Windows treats <b>{html.escape(where.network or 'this Wi-Fi')}</b> as a "
                f"<b>Public</b> network, so ARYA won't listen on it.</p><p>If this is your home Wi-Fi: Settings → "
                f"Network &amp; internet → Wi-Fi → {html.escape(where.network or 'your network')} → "
                f"Network profile type → <b>Private</b>. Then reload this page.</p>")
    elif where.why_not == "open":
        body = (f"<p class='warn'><b>{html.escape(where.network or 'This Wi-Fi')}</b> is open Wi-Fi (no password), "
                f"so ARYA won't listen on it. Turn on Tailscale on the laptop and phone to reach ARYA from here.</p>")
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
    if where.why_not == "open":
        net = f"“{where.network}”" if where.network else "this Wi-Fi"
        return f"{net} is open Wi-Fi with no password, so I'm not listening on it. Tailscale still works"
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
            ts = next((ip for ip in _state.servers if ipaddress.ip_address(ip) in TAILNET), None)
            if ts:
                return f"Yes, your phone can reach me from anywhere through Tailscale, at {ts}, port {PORT}."
            return (f"Yes, your phone can reach me at {_state.listening_on}, port {PORT}, on this Wi-Fi. To reach me "
                    "from anywhere, install Tailscale on the laptop and the phone.")
        if tailscale_ip():
            recheck()
            return "Phone access is on; I'll be reachable through Tailscale in a moment."
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
        if tailscale_ip():
            return ("Phone access is on, through Tailscale, so it works from anywhere. The QR code is on your screen: "
                    "scan it in the ARYA app. Reminders, timers, calendar heads-ups and the briefing will reach the "
                    "phone; it still can't touch files or commands.")
        if where.ip:
            return ("Phone access is on. The QR code is on your screen — scan it with your phone camera; the phone needs "
                    "to be on the same Wi-Fi. From the phone you can ask anything and see my reminders, but not "
                    "touch files or commands.")
        return f"Phone access is on, but {_why(where)}. The details are on your screen."
    return None
