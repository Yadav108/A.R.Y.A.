"""
Intercom between the phone and the laptop, and finding either one.

From the phone:
  "say 'dinner is ready' on the laptop" / "announce dinner is ready" / "tell everyone I'm on my way"
      -> a short chime, then ARYA says it aloud through the laptop speakers (and shows it in the app)
  "where's my laptop?" / "ring the laptop"  -> the laptop beeps and says "I'm here!"
From the laptop:
  "send this to my phone" / "send the answer to my phone"   -> ARYA's last answer appears on the phone
  "send this link to my phone"                              -> the link on your clipboard (or in the last answer)
  "send 'buy resistors' to my phone"                        -> that text
  "ring my phone" / "where's my phone?"   -> the phone page plays a loud tone and vibrates — only while the page
                                             is open (a web page can't wake a phone that isn't showing it)

From the phone log (2026-11-17 00:06–00:08): "Say 'bharat don't cry' on laptop", tried three times.
"""
from __future__ import annotations

import asyncio
import io
import re
import wave

import numpy as np

SR = 22050
MAX_MESSAGE = 300


def tone_wav(parts: list[tuple[float, int]], volume: float = 0.45) -> bytes:
    """[(frequency Hz or 0 for a pause, milliseconds)] -> WAV bytes (soft edges so it doesn't click)."""
    chunks = []
    for freq, ms in parts:
        n = int(SR * ms / 1000)
        t = np.arange(n) / SR
        wave_ = np.sin(2 * np.pi * freq * t) if freq else np.zeros(n)
        edge = min(n // 4, int(SR * 0.012))
        if freq and edge:
            env = np.ones(n)
            env[:edge] = np.linspace(0, 1, edge)
            env[-edge:] = np.linspace(1, 0, edge)
            wave_ = wave_ * env
        chunks.append(wave_)
    pcm = (np.concatenate(chunks) * volume * 32767).astype(np.int16) if chunks else np.zeros(0, np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


CHIME = tone_wav([(880, 130), (0, 40), (1320, 230)])
BEEPS = tone_wav([(1000, 180), (0, 120)] * 3 + [(1400, 320)], volume=0.7)

_speak = None                    # websocket.py: async speak(text, sound, shown) on the laptop, after the current turn


def set_speaker(speak) -> None:
    global _speak
    _speak = speak


def _later(text: str, sound: bytes, shown: str) -> None:
    """Speak after the current turn (which holds the voice lock while it answers)."""
    if _speak is not None:
        asyncio.get_running_loop().create_task(_speak(text, sound, shown))


# ── Phone → laptop ────────────────────────────────────────────────────────────

_Q = r"[\"“”'‘’]?"
_SAY_ON_LAPTOP = re.compile(rf"^\W*(?:please\s+)?(?:say|tell|announce|speak|read\s+out)\s+(?:out\s+)?{_Q}(?P<m>.+?){_Q}\s+"
                            r"(?:on|through|from|via|at|over|using)\s+(?:the\s+|my\s+)?(?:laptop|computer|pc|"
                            r"speakers?)\W*$", re.IGNORECASE)
_ANNOUNCE = re.compile(rf"^\W*(?:please\s+)?announce\s*[:,-]?\s*(?:that\s+)?{_Q}(?P<m>.+?){_Q}\W*$", re.IGNORECASE)
_TELL_HOME = re.compile(rf"^\W*(?:please\s+)?tell\s+(?:everyone|everybody|them|the\s+room|people)\s+(?:at\s+home\s+|here\s+)?"
                        rf"(?:that\s+)?{_Q}(?P<m>.+?){_Q}\W*$", re.IGNORECASE)
_FIND_LAPTOP = re.compile(r"\bwhere(?:'s|\s+is)\s+(?:my\s+|the\s+)?(?:laptop|computer|pc)\W*$|"     # not "…laptop charger"
                          r"\b(?:ring|beep|find)\s+(?:my\s+|the\s+)?(?:laptop|computer|pc)\b|"
                          r"\bmake\s+(?:the\s+|my\s+)?(?:laptop|computer|pc)\s+(?:beep|ring|make\s+a\s+(?:sound|noise))\b",
                          re.IGNORECASE)

# ── Laptop → phone ────────────────────────────────────────────────────────────

_SEND = re.compile(r"^\W*(?:please\s+)?(?:send|push|share|text|forward)\s+(?P<what>.+?)\s+to\s+my\s+(?:phone|mobile)\W*$",
                   re.IGNORECASE)
_RING_PHONE = re.compile(r"\b(?:ring|find|beep)\s+my\s+(?:phone|mobile)\W*$|\bwhere(?:'s|\s+is)\s+my\s+(?:phone|mobile)\W*$|"
                         r"\bmake\s+my\s+(?:phone|mobile)\s+(?:ring|beep)\b", re.IGNORECASE)
_THIS = re.compile(r"^(?:this|that|it|the\s+answer|your\s+(?:last\s+)?answer|the\s+last\s+answer|what\s+you\s+(?:just\s+)?"
                   r"said|the\s+summary)$", re.IGNORECASE)
_LINK = re.compile(r"^(?:this|that|the)\s+(?:link|url|website|page)$", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']{3,500}", re.IGNORECASE)


def matches(message: str) -> bool:
    return any(p.search(message) for p in (_SAY_ON_LAPTOP, _ANNOUNCE, _TELL_HOME, _FIND_LAPTOP, _SEND, _RING_PHONE))


def _message(m: re.Match) -> str:
    return m.group("m").strip(" \"“”'‘’")[:MAX_MESSAGE]


def _last_answer() -> str:
    from arya.memory.database import get_history
    return next((m["content"] for m in reversed(get_history(limit=8)) if m["role"] == "assistant"), "")


def _clipboard_url() -> str | None:
    try:
        import pyperclip
        m = _URL.search(pyperclip.paste() or "")
        return m.group(0) if m else None
    except Exception:
        return None


def handle(message: str, remote: bool = False) -> str | None:
    """The reply, or None if this isn't an intercom request. Sound on the laptop happens after the reply."""
    from arya.server import phone

    if remote:
        m = _SAY_ON_LAPTOP.match(message) or _ANNOUNCE.match(message) or _TELL_HOME.match(message)
        if m:
            text = _message(m)
            if not re.search(r"\w", text):
                return "What should I say on the laptop?"
            _later(text, CHIME, f"📣 From your phone: {text}")
            return f"Saying it on the laptop: “{text}”."
        if _FIND_LAPTOP.search(message):
            _later("I'm here! Over here!", BEEPS, "🔔 Your phone is looking for the laptop")
            return "The laptop is beeping and saying “I'm here” now."
        if _RING_PHONE.search(message) or _SEND.match(message):
            return "You're on your phone right now. Ask at the laptop to send something here."
        return None

    if _RING_PHONE.search(message):
        if not phone.enabled():
            return "Phone access is off, so I can't reach your phone. Say \"connect my phone\" first."
        phone.push("🔔 ARYA is ringing your phone", kind="ring")
        return ("Ringing your phone. If its ARYA page is open, it beeps and vibrates; a closed page can't ring, "
                "so try calling it instead.")
    m = _SEND.match(message)
    if m:
        if not phone.enabled():
            return "Phone access is off, so I can't reach your phone. Say \"connect my phone\" first."
        what = m.group("what").strip(" \"“”'‘’.")
        url = None
        if _LINK.match(what):
            url = _clipboard_url()
            if url is None:                                  # no link copied: the one in my last answer
                u = _URL.search(_last_answer())
                url = u.group(0) if u else None
            if url is None:
                return "I don't see a link on your clipboard or in my last answer. Copy it, then ask again."
            text = url
        elif _THIS.match(what):
            text = _last_answer()
            if not text:
                return "There's no answer of mine to send yet."
            u = _URL.search(text)
            url = u.group(0) if u else None
        else:
            text = what[:MAX_MESSAGE]
        phone.push(text[:1500], kind="message", url=url)
        return "Sent to your phone."
    m = _SAY_ON_LAPTOP.match(message) or _ANNOUNCE.match(message)
    if m:
        text = _message(m)
        _later(text, CHIME, f"📣 {text}")
        return "Okay."
    return None
