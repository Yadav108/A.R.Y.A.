"""
Phone tasks, done on the phone — so ARYA can be the phone's assistant instead of Google / Gemini.

When you talk to ARYA in the phone app, requests that belong to the phone are not answered by the laptop. They
become an *action* that the app carries out with Android's own apps (android/…/Actions.kt):

  "set an alarm for 6:45" / "wake me up at 7"       -> the phone's clock app sets the alarm
  "timer for 10 minutes" / "pasta timer 8 minutes"   -> the phone's timer
  "call Mom" / "call 0176 1234567"                   -> the phone looks Mom up in its contacts and calls
  "text Anna I'm running late" / "WhatsApp Ben …"    -> a message ready to send (you tap send)
  "navigate to the main station" / "directions to …" -> Maps starts navigation
  "open WhatsApp" / "launch Spotify"                 -> the app opens
  "turn on the flashlight" / "torch off"             -> the torch
  "take a selfie" / "open the camera" / "record a video"
  "turn on Wi-Fi / Bluetooth / airplane mode"        -> the setting's panel (apps may not switch these alone)
  "volume up" / "mute the phone"                     -> the phone's volume
  "play Kesariya" / "play lofi on Spotify"           -> plays on the phone (YouTube Music unless you name an app)

Names, numbers and message text never need the laptop's address book: the phone resolves "Mom" itself, so your
contacts stay on the phone. Anything said "on the laptop" stays a laptop request. When unsure, parse() returns
None and ARYA answers as usual.
"""
from __future__ import annotations

import re
from datetime import datetime

_ON_LAPTOP = re.compile(r"\b(?:on|from)\s+(?:the|my)\s+(?:laptop|computer|pc|desktop)\b", re.IGNORECASE)
_LEAD = r"^\W*(?:(?:hey|ok|okay|please|arya|nj|now|so|and|can you|could you|would you)\W+)*(?:please\s+)?"

_ALARM = re.compile(_LEAD + r"(?:(?:set|create|make|put)\s+(?:me\s+)?(?:an?\s+|the\s+|my\s+)?alarm\b|"
                    r"wake\s+me(?:\s+up)?\b|alarm\s+(?:for|at)\b)", re.IGNORECASE)
_TIMER = re.compile(_LEAD + r"(?:(?:set|start)\s+(?:a\s+|an\s+|the\s+|my\s+)?(?:\w+\s+){0,2}timer\b|"
                    r"timer\s+(?:for\s+)?\d|(?:\w+\s+){0,2}timer\s+(?:for\s+)?(?:\d|an?\s|one|two|three|five|ten|"
                    r"fifteen|twenty|thirty)|\d+\s*(?:-|\s)?(?:min(?:ute)?s?|sec(?:ond)?s?|hours?)\s+timer\b)",
                    re.IGNORECASE)
_CALL = re.compile(_LEAD + r"(?:call|phone|ring|dial)\s+(?!me\b|it\b|that\b|this\b|you\b|back\b|him\b|her\b)"
                   r"(?P<who>[\w'.+ ()-]{2,40}?)(?:\s+(?:on|using)\s+(?P<app>whatsapp|speaker))?\W*$", re.IGNORECASE)
_SMS = re.compile(_LEAD + r"(?:(?:send\s+(?:a\s+)?(?:text|message|sms|whatsapp)(?:\s+message)?\s+to)|text|message|"
                  # a name is one word, or two when the second is capitalised ("Anna Schmidt", not "Anna I am …")
                  r"(?P<wa>whatsapp))\s+(?P<who>[A-Za-z][\w'.-]*(?:\s+(?-i:[A-Z][a-z][\w'.-]*))?|\+?\d[\d ]{5,})"
                  r"(?:\s+on\s+(?P<wa2>whatsapp))?\s*[,:]?\s+(?:(?:saying|that|to say|and say)\s+)?(?P<text>.+?)\W*$",
                  re.IGNORECASE)
_NAVIGATE = re.compile(_LEAD + r"(?:navigate|(?:give me\s+|get\s+)?directions|take me|drive me|route me|show me the way)"
                       r"\s+(?:to\s+|home\b)(?P<to>.*?)\W*$|" + _LEAD +
                       r"how\s+do\s+i\s+get\s+to\s+(?P<to2>.+?)\W*$", re.IGNORECASE)
_TORCH = re.compile(r"\b(?:flash\s?light|torch)\b", re.IGNORECASE)
_TORCH_ON = re.compile(r"\b(?:on|turn on|switch on|light)\b(?!.*\boff\b)", re.IGNORECASE)
_TORCH_OFF = re.compile(r"\boff\b", re.IGNORECASE)
_CAMERA = re.compile(_LEAD + r"(?:take\s+(?:a\s+|me\s+a\s+)?(?P<kind>photo|picture|pic|selfie|video)|"
                     r"(?:open|start)\s+(?:the\s+)?(?P<cam>camera|selfie camera|front camera)|"
                     r"record\s+(?:a\s+)?video)\b", re.IGNORECASE)
_SETTING = re.compile(_LEAD + r"(?:turn|switch|put)\s+(?P<state>on|off)\s+(?:the\s+|my\s+)?(?P<what>wi-?fi|wlan|"
                      r"bluetooth|airplane mode|flight mode|mobile data|data|hotspot|location|gps|nfc)\b|" + _LEAD +
                      r"(?:turn|switch)\s+(?:the\s+|my\s+)?(?P<what2>wi-?fi|wlan|bluetooth|airplane mode|flight mode|"
                      r"mobile data|hotspot|location|gps|nfc)\s+(?P<state2>on|off)\b|" + _LEAD +
                      r"(?:open\s+)?(?P<what3>wi-?fi|bluetooth|battery|display|sound|notification)\s+settings\b",
                      re.IGNORECASE)
_VOLUME = re.compile(_LEAD + r"(?:(?:turn\s+)?(?:the\s+)?(?:volume|it|sound)\s+(?P<dir>up|down)|"
                     r"(?P<dir2>louder|quieter|softer)|(?P<mute>mute|silence)\s+(?:the\s+|my\s+)?phone|"
                     r"(?:put\s+)?(?:the\s+|my\s+)?phone\s+(?:on\s+)?(?P<mode>silent|vibrate))\W*$", re.IGNORECASE)
_PLAY = re.compile(_LEAD + r"play\s+(?P<q>.+?)(?:\s+on\s+(?P<app>spotify|youtube music|youtube))?\W*$", re.IGNORECASE)
_OPEN = re.compile(_LEAD + r"(?:open|launch|start)\s+(?:the\s+|my\s+)?(?P<app>[\w .'&+-]{2,30}?)(?:\s+app)?\W*$",
                   re.IGNORECASE)
_NOT_AN_APP = re.compile(r"\b(?:tab|window|file|folder|document|pdf|link|website|page|door|mode|camera|"
                         r"flash\s?light|torch|settings?|timer|alarm)\b|^(?:it|that|this|up|a new|the door)$",
                         re.IGNORECASE)
_NOT_A_SONG = re.compile(r"^(?:it|that|this|again|music|something|anything|the music|some music|the song|"
                         r"the next song|next|previous|the video)$", re.IGNORECASE)


def _say_time(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


ASK_S = 60.0
_awaiting = {"kind": "", "until": 0.0}         # "set a timer" -> "For how long?" -> "ten minutes"


def parse(text: str, now: datetime | None = None) -> dict | None:
    """The phone action for a request said to the phone app, or None (then ARYA answers as usual)."""
    import time
    t = (text or "").strip()
    if not t or len(t) > 220 or _ON_LAPTOP.search(t):
        return None
    now = now or datetime.now()
    from arya.tools import alarms

    waiting = _awaiting["kind"] if time.monotonic() < _awaiting["until"] else ""
    _awaiting["kind"] = ""
    if waiting == "timer" and alarms.duration_seconds(t):
        t = "set a timer for " + t
    elif waiting == "alarm" and alarms.when_in("at " + t, now)[0] is not None:
        t = "set an alarm at " + re.sub(r"^\W*(?:at|for)\s+", "", t, flags=re.IGNORECASE)

    if _ALARM.search(t) and not re.search(r"\bremind\b", t, re.IGNORECASE):
        when, _tomorrow = alarms.when_in(t, now)
        if when is None:
            _awaiting.update(kind="alarm", until=time.monotonic() + ASK_S)
            return {"action": "ask", "say": "For what time?"}
        return {"action": "alarm", "hour": when.hour, "minute": when.minute,
                "say": f"Alarm set for {_say_time(when.hour, when.minute)}."}
    if (_TIMER.search(t) or re.match(_LEAD + r"(?:set|start)\s+(?:a\s+|the\s+)?timer\W*$", t, re.IGNORECASE)) \
            and not re.search(r"\bremind\b", t, re.IGNORECASE):
        seconds = alarms.duration_seconds(t)
        if not seconds:
            _awaiting.update(kind="timer", until=time.monotonic() + ASK_S)
            return {"action": "ask", "say": "For how long?"}
        label = alarms._timer_label(t)
        from arya.tools.productivity import _speak_duration
        return {"action": "timer", "seconds": int(seconds), "label": "" if label == "Timer" else label,
                "say": f"Timer set for {_speak_duration(seconds)}."}
    m = _SMS.match(t)
    if m and m.group("who").lower() not in ("me", "you", "him", "her", "them"):
        wa = bool(m.group("wa") or m.group("wa2"))
        who = m.group("who").strip()
        return {"action": "sms", "who": who, "text": m.group("text").strip(), "app": "whatsapp" if wa else "sms",
                "say": f"Here's the message to {who}. Tap send when it looks right."}
    m = _CALL.match(t)
    if m:
        who = m.group("who").strip()
        if not re.search(r"\b(?:an?\s+|the\s+)?(?:uber|taxi|cab|ambulance|function|method|api|meeting)\b", who,
                         re.IGNORECASE):
            return {"action": "call", "who": who, "app": (m.group("app") or "phone").lower(), "say": f"Calling {who}."}
    m = _NAVIGATE.match(t)
    if m:
        to = (m.group("to") or m.group("to2") or "").strip() or "home"
        return {"action": "navigate", "to": to, "say": f"Starting directions to {to}."}
    if _TORCH.search(t) and len(t.split()) <= 8:
        on = not _TORCH_OFF.search(t)
        return {"action": "torch", "on": on, "say": "Flashlight on." if on else "Flashlight off."}
    m = _CAMERA.match(t)
    if m:
        kind = (m.group("kind") or "").lower()
        video = kind == "video" or bool(re.search(r"\bvideo\b", t, re.IGNORECASE))
        selfie = kind == "selfie" or "front" in t.lower() or "selfie" in t.lower()
        return {"action": "camera", "video": video, "selfie": selfie,
                "say": "Opening the camera." if not video else "Opening the video camera."}
    m = _SETTING.match(t)
    if m:
        what = (m.group("what") or m.group("what2") or m.group("what3") or "").lower().replace("wlan", "wifi")
        what = {"wi-fi": "wifi", "flight mode": "airplane mode", "gps": "location", "data": "mobile data"}.get(what, what)
        state = (m.group("state") or m.group("state2") or "").lower()
        return {"action": "settings", "what": what, "state": state,
                "say": f"Here's the {what} setting." if not state else
                f"Here's {what}: tap to turn it {state}. Android doesn't let apps switch it by themselves."}
    m = _VOLUME.match(t)
    if m:
        d = (m.group("dir") or "").lower() or {"louder": "up", "quieter": "down", "softer": "down"}.get(
            (m.group("dir2") or "").lower(), "")
        if m.group("mute") or m.group("mode"):
            mode = (m.group("mode") or "silent").lower()
            return {"action": "volume", "dir": mode, "say": "Phone on vibrate." if mode == "vibrate" else "Phone muted."}
        return {"action": "volume", "dir": d, "say": "Louder." if d == "up" else "Quieter."}
    m = _PLAY.match(t)
    if m and not _NOT_A_SONG.match(m.group("q").strip()):
        app = (m.group("app") or "youtube music").lower()
        q = re.sub(r"^(?:the\s+song\s+|the\s+|some\s+)", "", m.group("q").strip(), flags=re.IGNORECASE)
        return {"action": "play", "query": q, "app": app, "say": f"Playing {q}."}
    m = _OPEN.match(t)
    if m and not _NOT_AN_APP.search(m.group("app")):
        app = m.group("app").strip()
        return {"action": "open_app", "app": app, "say": f"Opening {app}."}
    return None
