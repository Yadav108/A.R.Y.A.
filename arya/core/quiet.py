"""
Meeting / do-not-disturb (roadmap C).

  "I'm in a meeting (for an hour / until 3:30)", "do not disturb for 30 minutes", "meeting mode"
      -> ARYA says nothing aloud on the laptop: answers appear as text, and timers, reminders and nudges go to
         the phone (with a vibration) and the app instead. It still listens, so you can type or say something.
  "I'm back", "meeting's over", "meeting mode off"   -> back to normal (also when the time is up)
Automatically during a call: while Teams, Zoom, WhatsApp, Webex, Skype, Discord, Slack or a browser meeting
(Google Meet, Teams or Zoom in Chrome/Edge/Firefox) is using the microphone — Windows records which app is
using it — and back to normal when the call ends. "Meeting mode off" during a call keeps ARYA talking until it ends.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime

DEFAULT_S = 60 * 60
CHECK_EVERY_S = 15

_state = {"until": 0.0, "reason": "", "auto": False, "declined_call": ""}

CALL_APPS = {"mstteams": "Teams", "msteams": "Teams", "teams": "Teams", "ms-teams": "Teams", "zoom": "Zoom",
             "whatsapp": "WhatsApp", "5319275a.whatsappdesktop": "WhatsApp", "ciscowebex": "Webex",
             "webexmta": "Webex", "skype": "Skype", "discord": "Discord", "slack": "Slack"}
BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera"}
_MEETING_TITLE = re.compile(r"\b(?:meet|zoom|teams|webex|jitsi|whereby|discord|skype)\b", re.IGNORECASE)


def active(now: float | None = None) -> bool:
    return (now or time.time()) < _state["until"]


def reason() -> str:
    return _state["reason"] if active() else ""


def start(seconds: float, why: str, auto: bool = False, now: float | None = None) -> None:
    _state.update(until=(now or time.time()) + seconds, reason=why, auto=auto)
    print(f"Quiet: {why} until {datetime.fromtimestamp(_state['until']):%H:%M}")


def end() -> None:
    _state.update(until=0.0, reason="", auto=False)


# ── In a call? (Windows' record of which app is using the microphone) ─────────

def mic_users() -> list[str]:
    """Apps using the microphone right now (registry key names, lower case), e.g. ['zoom.exe', 'mstteams_8wek…']."""
    import winreg
    base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    found = []

    def scan(path: str) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, name) as sub:
                            stop, _ = winreg.QueryValueEx(sub, "LastUsedTimeStop")
                            if stop == 0:
                                found.append(name.lower())
                    except OSError:
                        continue
        except OSError:
            pass
    scan(base)
    scan(base + r"\NonPackaged")
    return found


def call_app(users: list[str] | None = None, titles: list[str] | None = None) -> str | None:
    """'Zoom' / 'Teams' / 'Google Meet in Chrome' if a call app is using the mic, else None."""
    users = mic_users() if users is None else users
    for u in users:
        exe = u.rsplit("#", 1)[-1].removesuffix(".exe")
        package = exe.split("_")[0]
        for key in (exe, package):
            if key in CALL_APPS:
                return CALL_APPS[key]
        if exe in BROWSERS:
            if titles is None:
                from arya.tools.windows import _visible_windows
                titles = [t for _h, _p, _c, t in _visible_windows()]
            for t in titles:
                m = _MEETING_TITLE.search(t)
                if m:
                    return f"a {m.group(0).title()} call in your browser"
    return None


async def run(notify) -> None:
    """Started by the server: quiet during calls, back afterwards. `notify(text)` goes to the phone and the app."""
    while True:
        try:
            app = await asyncio.to_thread(call_app)
            if app and not active():
                if _state["declined_call"] != app:
                    start(4 * 3600, f"call: {app}", auto=True)
                    notify(f"🔕 Looks like you're in a call ({app}). I'll stay quiet until it ends.")
            elif not app:
                _state["declined_call"] = ""
                if _state["auto"] and active():
                    end()
                    notify("🔔 Call over: I'm talking again.")
        except Exception as exc:
            print(f"Quiet: call check failed ({type(exc).__name__})")
        await asyncio.sleep(CHECK_EVERY_S)


# ── Voice ─────────────────────────────────────────────────────────────────────

_SPAN = r"(?:\s+(?:for|till|until|til)\s+(?P<span>.+?))?"
_START = re.compile(r"^\W*(?:arya\W+)?(?:(?:i'?m|i\s+am|we'?re|we\s+are)\s+(?:in|going\s+into|joining|on|about\s+to\s+"
                    r"join)\s+(?:a\s+|the\s+)?(?:meeting|call|class|lecture|exam|seminar|interview)" + _SPAN +
                    r"|(?:(?:turn\s+on|start|enable|go\s+(?:in)?to)\s+)?(?:do\s+not\s+disturb(?:\s+mode)?|don'?t\s+disturb(?:\s+me)?|dnd|meeting\s+mode|"
                    r"silent\s+mode|quiet\s+mode)(?:\s+on)?" + _SPAN.replace("span", "span2") +
                    r"|(?:stay|be|keep)\s+(?:quiet|silent)\s+(?:for|till|until)\s+(?P<span3>.+?))\W*$", re.IGNORECASE)
_END = re.compile(r"^\W*(?:arya\W+)?(?:(?:the\s+)?(?:meeting|call|class|lecture|exam|seminar|interview)(?:'s|\s+is)?\s+"
                  r"(?:over|done|finished|ended)|i'?m\s+back|i\s+am\s+back|(?:end|stop|turn\s+off|exit|leave)\s+(?:the\s+)?"
                  r"(?:meeting\s+mode|do\s+not\s+disturb(?:\s+mode)?|silent\s+mode|quiet\s+mode)|(?:meeting|silent|quiet)\s+mode\s+off|"
                  r"do\s+not\s+disturb(?:\s+mode)?\s+off|you\s+can\s+(?:talk|speak)\s+again)\W*$", re.IGNORECASE)


def matches(message: str) -> bool:
    return bool(_START.match(message) or _END.match(message))


def _seconds(span: str | None, now: datetime) -> float | None:
    if not span:
        return DEFAULT_S
    from arya.tools.alarms import duration_seconds, when_in
    s = duration_seconds(span)
    if s:
        return s
    when, _ = when_in("at " + span, now)
    if when is not None and when > now:
        return (when - now).total_seconds()
    return None


def handle(message: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now()
    if _END.match(message):
        if not active(now.timestamp()):
            return None              # not in meeting mode: "you can talk again" may be for the nudge settings
        if _state["auto"]:
            _state["declined_call"] = _state["reason"].removeprefix("call: ")
        end()
        return "Okay, I'm talking again."
    m = _START.match(message)
    if m:
        span = m.group("span") or m.group("span2") or m.group("span3")
        seconds = _seconds(span, now)
        if seconds is None:
            return "Until when? Say \"for an hour\" or \"until 3:30\"."
        start(seconds, "meeting", now=now.timestamp())
        until = datetime.fromtimestamp(now.timestamp() + seconds)
        return (f"Meeting mode until {until:%H:%M}. I won't say anything aloud; reminders go to your phone. "
                "Say \"I'm back\" when you're done.")
    return None
