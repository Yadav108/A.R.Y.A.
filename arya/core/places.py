"""
Continuity: ARYA knows *where* you are — from Wi-Fi, not GPS — and reminds you at the right place.

  "remind me to take the probe when I leave home"          -> fires on the phone as you walk out of Wi-Fi range
  "when I get to the lab, remind me to ask Max about the detector"
  "remind me to water the plants when I get home"
  "this is the lab" / "call this place the gym"            -> said to the PHONE: its current Wi-Fi becomes "lab"
                                                               (at the laptop: the laptop's Wi-Fi)
  "what are my place reminders?" / "which places do you know?" / "forget the gym" / "cancel the lab reminders"
  "where am I?"                                             -> the place the phone last reported

A place is just a Wi-Fi network name (SSID) with a label you chose. The phone checks for arrival and departure
itself, so "when I leave home" still fires after the laptop is out of reach; it tells the laptop when it's back.
Nothing here uses GPS, maps or the cloud; network names stay on your laptop and phone.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from datetime import datetime

HOME = "home"
MAX_LABEL = 30

_state = {"place": None, "since": 0.0, "near_laptop": False, "seen": 0.0}


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS places (label TEXT PRIMARY KEY, ssid TEXT NOT NULL, created TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS place_reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, "
                 "place TEXT, event TEXT, created TEXT, done TEXT)")
    return conn


# ── Places ────────────────────────────────────────────────────────────────────

def clean_label(label: str) -> str:
    label = re.sub(r"^(?:the|my|our)\s+", "", (label or "").strip().lower())
    label = re.sub(r"[^\w' -]", "", label).strip()
    return {"house": HOME, "flat": HOME, "apartment": HOME, "place": HOME}.get(label, label)[:MAX_LABEL]


def name_place(label: str, ssid: str) -> str:
    label, ssid = clean_label(label), (ssid or "").strip().strip('"')
    if not label or not ssid or ssid.lower() in ("<unknown ssid>", "unknown"):
        raise ValueError("no Wi-Fi name")
    with _db() as c:
        c.execute("DELETE FROM places WHERE ssid = ? AND label != ?", (ssid, label))   # one name per network
        c.execute("INSERT OR REPLACE INTO places (label, ssid, created) VALUES (?, ?, ?)",
                  (label, ssid, datetime.now().isoformat(timespec="seconds")))
    _changed()
    return label


def places() -> dict[str, str]:
    with _db() as c:
        return {r["label"]: r["ssid"] for r in c.execute("SELECT label, ssid FROM places ORDER BY label")}


def forget_place(label: str) -> bool:
    with _db() as c:
        n = c.execute("DELETE FROM places WHERE label = ?", (clean_label(label),)).rowcount
    _changed()
    return n > 0


def laptop_ssid() -> str:
    """The Wi-Fi the laptop is on ('' when on cable / unknown)."""
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return ""
    m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", out, re.MULTILINE)
    return m.group(1) if m else ""


# ── Reminders ─────────────────────────────────────────────────────────────────

def add_reminder(text: str, place: str, event: str) -> int:
    with _db() as c:
        cur = c.execute("INSERT INTO place_reminders (text, place, event, created) VALUES (?, ?, ?, ?)",
                        (text, clean_label(place), event, datetime.now().isoformat(timespec="seconds")))
        rid = int(cur.lastrowid)
    _changed()
    return rid


def pending() -> list[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT id, text, place, event FROM place_reminders WHERE done IS NULL "
                                           "ORDER BY id")]


def mark_done(rid: int) -> bool:
    with _db() as c:
        n = c.execute("UPDATE place_reminders SET done = ? WHERE id = ? AND done IS NULL",
                      (datetime.now().isoformat(timespec="seconds"), int(rid))).rowcount
    return n > 0


def cancel_for(place: str) -> int:
    with _db() as c:
        n = c.execute("UPDATE place_reminders SET done = 'cancelled' WHERE place = ? AND done IS NULL",
                      (clean_label(place),)).rowcount
    _changed()
    return n


def due(prev: str | None, new: str | None, items: list[dict]) -> list[dict]:
    """Reminders that fire when the phone moves from place `prev` to place `new` (None = an unknown place or no
    Wi-Fi). The same rule runs on the phone (Places.kt); tests/test_places.py keeps both in step."""
    if prev == new:
        return []
    return [r for r in items if (r["event"] == "leave" and prev is not None and r["place"] == prev)
            or (r["event"] == "arrive" and new is not None and r["place"] == new)]


def snapshot() -> dict:
    """What the phone needs: known places and pending reminders."""
    return {"places": places(), "reminders": pending()}


def _changed() -> None:
    """Tell the phone to fetch the new list (its long poll returns at once with a new version)."""
    try:
        from arya.server import phone
        phone.places_changed()
    except Exception:
        pass


# ── Presence (reported by the phone) ──────────────────────────────────────────

def report(place: str | None, near_laptop: bool, fired: list[int] | None = None) -> None:
    for rid in fired or []:
        mark_done(rid)
    if place != _state["place"]:
        _state["since"] = time.time()
        print(f"Places: phone now at {place or 'an unknown place'}")
    _state.update(place=place, near_laptop=bool(near_laptop), seen=time.time())


def where() -> str:
    if not _state["seen"]:
        return "I haven't heard from your phone yet."
    ago = int((time.time() - _state["seen"]) // 60)
    when = "just now" if ago < 2 else f"{ago} minutes ago"
    if _state["near_laptop"]:
        return f"Your phone is on the same Wi-Fi as the laptop ({when})."
    if _state["place"]:
        return f"Your phone was at {_state['place']} {when}."
    return f"Your phone was somewhere I don't have a name for {when}."


# ── Voice ─────────────────────────────────────────────────────────────────────

_PLACE = r"(?P<place>home|(?:the\s+|my\s+)?[a-z][\w' -]{1,30}?)"
_ARRIVE = r"(?:get|arrive|am|i'm|reach|come)\s+(?:back\s+)?(?:to|at|in|home)?"
_REMIND = re.compile(
    r"^\W*(?:arya\W+)?(?:please\s+)?remind\s+me\s+(?:to\s+|about\s+|that\s+)?(?P<text>.+?)\s+"
    r"(?:when(?:ever)?|once|as\s+soon\s+as)\s+i\s+(?:(?P<leave>leave|(?:am\s+)?leaving|get\s+out\s+of|go\s+out\s+of)"
    r"(?:\s+(?:the\s+)?)?|(?P<arrive>get|arrive|reach|come|am|i'm)\s+(?:back\s+)?(?:(?:to|at|in)\s+)?)"
    + _PLACE + r"\W*$", re.IGNORECASE)
_REMIND_FIRST = re.compile(
    r"^\W*(?:arya\W+)?(?:when(?:ever)?|once|as\s+soon\s+as)\s+i\s+(?:(?P<leave>leave|(?:am\s+)?leaving|get\s+out\s+of)"
    r"\s+|(?P<arrive>get|arrive|reach|come|am)\s+(?:back\s+)?(?:(?:to|at|in)\s+)?)" + _PLACE +
    r"\s*,?\s*(?:please\s+)?remind\s+me\s+(?:to\s+|about\s+|that\s+)?(?P<text>.+?)\W*$", re.IGNORECASE)
_PLACE_WORDS = (r"home|(?:the\s+|my\s+|our\s+)(?:lab|office|gym|work(?:place)?|uni|university|library|campus|cafe|"
                r"café|institute|studio|desk|parents'?\s+(?:place|house)|[a-z]+'s\s+(?:place|house|flat))|"
                r"(?:my\s+)?[a-z]+'s\s+(?:place|house|flat)|work|uni|campus")
# "this is …" only with a place word (so "this is Anna" stays a sentence); "call this place …" takes any name.
_NAME = re.compile(r"^\W*(?:arya\W+)?(?:(?:ok(?:ay)?\W+)?this\s+is\s+(?P<label>" + _PLACE_WORDS + r")|"
                   r"(?:call|name|save|remember|mark)\s+this\s+(?:place|wi-?fi|network|spot|location)\s+(?:as\s+)?"
                   r"(?P<label2>home|(?:the\s+|my\s+)?[a-z][\w' -]{1,25}))\W*$", re.IGNORECASE)
_LIST = re.compile(r"\b(?:what|which|list|show)\b.{0,20}\b(?:place\s+reminders|places?\s+(?:do\s+)?you\s+know|"
                   r"(?:my\s+)?places)\b|\bmy\s+place\s+reminders\b", re.IGNORECASE)
_FORGET = re.compile(r"^\W*(?:arya\W+)?(?:forget|delete|remove)\s+(?:the\s+place\s+)?" + _PLACE + r"(?:\s+place)?\W*$",
                     re.IGNORECASE)
_CANCEL = re.compile(r"^\W*(?:arya\W+)?(?:cancel|delete|clear|remove)\s+(?:the\s+|my\s+|all\s+)?(?:reminders?\s+for\s+"
                     + _PLACE + r"|" + _PLACE.replace("place>", "place2>") + r"\s+reminders?)\W*$", re.IGNORECASE)
_WHERE = re.compile(r"^\W*(?:arya\W+)?(?:where\s+am\s+i(?:\s+(?:now|right\s+now))?)\W*$", re.IGNORECASE)
_NOT_PLACES = re.compile(r"^(?:it|that|this|done|finished|ready|free|back|up|awake|there|asleep|bored|tired|"
                         r"online|out|in|here|laptop|computer|pc|desk(?:top)?)$", re.IGNORECASE)
_BARE_ARRIVE_OK = {HOME, "work", "uni", "campus"}      # "when I get home" — other places need "to/at": "get paid"


def parse_reminder(message: str) -> tuple[str, str, str] | None:
    """(text, place, 'arrive'|'leave') for a place reminder, else None."""
    m = _REMIND.match(message) or _REMIND_FIRST.match(message)
    if not m:
        return None
    place = clean_label(m.group("place"))
    if not place or _NOT_PLACES.match(place) or re.search(r"\d", place):
        return None
    if m.group("arrive"):
        between = message[m.end("arrive"):m.start("place")]
        if not re.search(r"\b(?:to|at|in)\b", between, re.IGNORECASE) and place not in _BARE_ARRIVE_OK:
            return None
    text = m.group("text").strip().rstrip(",")
    if not text or len(text) > 200:
        return None
    return text, place, "leave" if m.group("leave") else "arrive"


def parse_name(message: str) -> str | None:
    m = _NAME.match(message)
    if not m:
        return None
    label = clean_label(m.group("label") or m.group("label2"))
    return None if not label or _NOT_PLACES.match(label) else label


def matches(message: str) -> bool:
    return bool(parse_reminder(message) or parse_name(message) or _LIST.search(message) or _WHERE.match(message)
                or _CANCEL.match(message) or (_FORGET.match(message) and clean_label(_FORGET.match(message)["place"])
                                              in places()))


def _said(event: str, place: str) -> str:
    if place == HOME:
        return "when you leave home" if event == "leave" else "when you get home"
    return f"when you leave the {place}" if event == "leave" else f"when you get to the {place}"


def handle(message: str, remote: bool = False) -> str | None:
    """`remote` = said in the phone app (then 'this is …' is handled on the phone as an action)."""
    r = parse_reminder(message)
    if r:
        text, place, event = r
        add_reminder(text, place, event)
        reply = f"Okay — I'll remind you to {text} {_said(event, place)}. It pops up on your phone."
        if place not in places():
            reply += (f" I don't know the {place}'s Wi-Fi yet: next time you're there, tell your phone "
                      f"\"this is the {place}\"." if place != HOME else
                      " Say \"this is home\" once at home, so I know your Wi-Fi.")
        return reply
    label = parse_name(message)
    if label:
        if remote:                                     # typed on the phone page: the page can't see the Wi-Fi
            return (f"Say \"this is {'home' if label == HOME else 'the ' + label}\" on the app's Talk screen — "
                    "the app reads the phone's Wi-Fi name.")
        ssid = laptop_ssid()
        if not ssid:
            return "I can't see a Wi-Fi network on the laptop. Say it on your phone, where you are."
        name_place(label, ssid)
        return f"Got it — this Wi-Fi is {'home' if label == HOME else 'the ' + label}."
    if _WHERE.match(message):
        return where()
    m = _CANCEL.match(message)
    if m:
        place = m.group("place") or m.group("place2")
        n = cancel_for(place)
        return f"Cancelled {n} reminder{'s' if n != 1 else ''} for {clean_label(place)}." if n else \
            f"There were no reminders for {clean_label(place)}."
    m = _FORGET.match(message)
    if m and forget_place(m.group("place")):
        return f"Forgotten: {clean_label(m.group('place'))}."
    if _LIST.search(message):
        known, items = places(), pending()
        if not known and not items:
            return "No places yet. Tell your phone \"this is the lab\" (or home, the gym…) while you're there."
        parts = []
        if known:
            parts.append("I know " + ", ".join(sorted(known)) + ".")
        for item in items[:5]:
            parts.append(f"To {item['text']} {_said(item['event'], item['place'])}.")
        return " ".join(parts)
    return None
