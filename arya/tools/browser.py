"""
Browser control (Phase B): Chrome tabs, web searches and YouTube (Music) by voice.

  "new tab" / "close this tab" / "reopen the closed tab"
  "next tab" / "previous tab" / "go to tab 3" / "last tab"
  "search Google for Bode plot examples", "google Coriolis force", "search YouTube for PID tuning"
  "play Coldplay on YouTube Music" / "play lofi beats on YouTube"

Tabs: ARYA brings Chrome to the front and presses Chrome's own shortcuts (Ctrl+T, Ctrl+W, …). Keys are
only sent after checking that Chrome really is the front window — never into another app.
Searches go to the site you name; a plain "search for …" still means your course library.
"Play X on YouTube": ARYA asks YouTube for the first matching video and opens it (it starts playing);
if that lookup fails it opens the search results instead.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from urllib.parse import quote_plus

VK_CONTROL, VK_SHIFT, VK_MENU, VK_TAB = 0x11, 0x10, 0x12, 0x09
_KEYUP = 0x0002

SEARCH_URLS = {
    "google": "https://www.google.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "youtube music": "https://music.youtube.com/search?q={q}",
}
WATCH_URLS = {"youtube": "https://www.youtube.com/watch?v={id}",
              "youtube music": "https://music.youtube.com/watch?v={id}"}


# ── Chrome window + keys (Windows) ────────────────────────────────────────────

def _chrome_window() -> int | None:
    """The Chrome browser window nearest the top of the screen (EnumWindows goes top to bottom)."""
    import psutil
    import win32gui
    import win32process

    found: list[int] = []

    def visit(hwnd, _):
        if found or not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != "Chrome_WidgetWin_1":
            return True
        if not win32gui.GetWindowText(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() == "chrome.exe":
                found.append(hwnd)
        except Exception:
            pass
        return True

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def _focus(hwnd: int) -> bool:
    """Bring the window to the front; True only if it really is in front afterwards."""
    import win32api
    import win32con
    import win32gui

    if win32gui.GetForegroundWindow() == hwnd:
        return True
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32api.keybd_event(VK_MENU, 0, 0, 0)               # Windows only lets the active app change focus;
        win32api.keybd_event(VK_MENU, 0, _KEYUP, 0)          # a tap of Alt counts as user input
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        print(f"Browser: couldn't bring Chrome to the front ({type(exc).__name__})")
    for _ in range(10):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.03)
    return False


def _press(*keys: int) -> None:
    import win32api

    for k in keys:
        win32api.keybd_event(k, 0, 0, 0)
    for k in reversed(keys):
        win32api.keybd_event(k, 0, _KEYUP, 0)


def _in_chrome(*keys: int) -> str | None:
    """Press a shortcut in Chrome. Returns an error sentence, or None when done."""
    hwnd = _chrome_window()
    if hwnd is None:
        return "Chrome isn't open."
    if not _focus(hwnd):
        return "I couldn't bring Chrome to the front, so I didn't press anything."
    _press(*keys)
    return None


# ── Search / play ─────────────────────────────────────────────────────────────

async def _open(url: str) -> bool:
    from arya.tools.os_tools import open_url
    return not (await open_url(url)).startswith("Failed")


async def first_video(query: str) -> str | None:
    """The id of YouTube's first search result for `query`, or None."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True,
                                     headers={"Accept-Language": "en", "User-Agent": "Mozilla/5.0"}) as client:
            r = await client.get(SEARCH_URLS["youtube"].format(q=quote_plus(query)))
        m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', r.text) or \
            re.search(r'"videoId":"([\w-]{11})"', r.text)
        return m.group(1) if m else None
    except Exception as exc:
        print(f"Browser: YouTube lookup failed ({type(exc).__name__})")
        return None


# ── Voice ─────────────────────────────────────────────────────────────────────

_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_TAB = r"(?:browser\s+|chrome\s+)?tab"
_NEW = re.compile(rf"\b(?:open|new|add)\s+(?:a\s+)?(?:new\s+)?(?:empty\s+|blank\s+)?{_TAB}\b", re.IGNORECASE)
_CLOSE = re.compile(rf"\bclose\s+(?:this|the|that|my|current|the current)?\s*{_TAB}\b", re.IGNORECASE)
_REOPEN = re.compile(rf"\b(?:reopen|re-open|restore|bring back|undo closing)\s+(?:the\s+|that\s+|my\s+)?"
                     rf"(?:last\s+|closed\s+|last closed\s+)?{_TAB}\b|\bopen (?:the )?(?:last )?closed {_TAB}\b",
                     re.IGNORECASE)
_NEXT = re.compile(rf"\b(?:next|following)\s+{_TAB}\b|\bswitch\s+(?:to\s+the\s+)?(?:next\s+)?{_TAB}\b",
                   re.IGNORECASE)
_PREV = re.compile(rf"\b(?:previous|prev|left)\s+{_TAB}\b|\bswitch\s+(?:to\s+the\s+)?previous\s+{_TAB}\b",
                   re.IGNORECASE)
_GOTO = re.compile(rf"\b(?:go|switch|jump)\s+to\s+(?:the\s+)?(?:{_TAB}\s+(?P<n>\d|one|two|three|four|five|six|"
                   rf"seven|eight)|(?P<o>first|second|third|fourth|fifth|sixth|seventh|eighth)\s+{_TAB}|"
                   rf"(?P<last>last)\s+{_TAB})\b", re.IGNORECASE)
_ENGINE = r"(?P<eng>google|youtube music|youtube|the web|the internet|online)"
_SEARCH = re.compile(rf"^\W*(?:arya\W+)?(?:(?:can you|could you|please)\s+)?(?:search|look up|look for|find)\s+"
                     rf"(?:on\s+|in\s+)?{_ENGINE}\s+(?:for\s+)?(?P<q>.+?)\W*$|"
                     rf"^\W*(?:arya\W+)?(?:(?:can you|could you|please)\s+)?(?:search(?:\s+for)?|look up|find)\s+"
                     rf"(?P<q2>.+?)\s+(?:on|in)\s+(?P<eng2>google|youtube music|youtube|the web|the internet)\W*$|"
                     rf"^\W*(?:arya\W+)?(?:please\s+)?google\s+(?P<q3>.+?)\W*$", re.IGNORECASE)
_PLAY = re.compile(r"\bplay\s+(?P<q>.+?)\s+(?:on|in|from)\s+(?P<eng>youtube music|youtube)\b", re.IGNORECASE)
# "Play Kon Tujhe by Kishore Kumar", "play the song Kesariya", "play lofi study beats" — no site named
_PLAY_SONG = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?play\s+(?:me\s+)?(?:the\s+|a\s+)?(?:"
                        r"(?:song|track|music video)\s+(?P<q1>.+?)|(?P<q2>.+?\s+by\s+.+?)|"
                        r"(?P<q3>.+?\s+(?:songs|beats|playlist|mix|music))"
                        r")\W*$", re.IGNORECASE)
_ANY_MUSIC = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:(?:play|put on)\s+(?:some|any|me some|a little)\s+music|"
                        r"(?:some|any)\s+music)(?:\s+please)?\W*$", re.IGNORECASE)
SITES = {"youtube music": "https://music.youtube.com", "youtube": "https://www.youtube.com",
         "google maps": "https://maps.google.com", "google": "https://www.google.com",
         "gmail": "https://mail.google.com", "github": "https://github.com", "chatgpt": "https://chatgpt.com",
         "wikipedia": "https://en.wikipedia.org", "whatsapp": "https://web.whatsapp.com"}
_OPEN_SITE = re.compile(
    r"^\W*(?:arya\W+)?(?:(?:can you|could you|please)\s+)?(?:open|go to|launch|take me to)\s+(?:the\s+)?"
    r"(?P<site>youtube music|youtube|google maps|google|gmail|github|chatgpt|wikipedia|whatsapp)"
    r"(?:\s+(?:in chrome|website|site|page|for me))?"
    r"(?:\s+and\s+search(?:\s+for)?(?:\s+(?P<q>.+?))?)?\W*$", re.IGNORECASE)
MUSIC_SITE = os.getenv("ARYA_MUSIC_SITE", "youtube").strip().lower()          # where "play X by Y" goes

# After "Tell me a song or an artist", the next sentence ("classic music.", "Kishore Kumar") is the answer.
SONG_ANSWER_S = 120.0
_awaiting_song_until = 0.0
# Sentences that are clearly a new request, not a song name.
_NEW_REQUEST = re.compile(r"^\W*(?:arya\W+)?(?:what|why|how|when|where|who|which|is|are|can|could|would|do|does|"
                          r"explain|tell|set|start|stop|open|close|search|google|show|minimi|maximi|cancel|no\b|"
                          r"never ?mind|pause|next|previous|volume|turn|increase|decrease|mute|quiz|remind)",
                          re.IGNORECASE)


def awaiting_song() -> bool:
    return time.monotonic() < _awaiting_song_until


def _song_answer(message: str) -> str | None:
    """'classic music.' after ARYA asked for a song -> 'classic music'; None if it's a new request."""
    global _awaiting_song_until
    if not awaiting_song():
        return None
    _awaiting_song_until = 0.0                       # one answer only
    if _NEW_REQUEST.match(message) or _ANY_MUSIC.match(message):
        return None
    query = re.sub(r"^\W*(?:arya\W+)?(?:please\s+)?(?:play\s+|put on\s+)?(?:some\s+|the\s+|me\s+)?", "", message,
                   flags=re.IGNORECASE).strip(" ,.!?")
    return query if re.search(r"[A-Za-z]{2,}", query) else None


def _engine(word: str | None) -> str:
    w = (word or "google").lower()
    return w if w in SEARCH_URLS else "google"


def _site(eng: str) -> str:
    return {"google": "Google", "youtube": "YouTube", "youtube music": "YouTube Music"}[eng]


async def handle(message: str) -> str | None:
    """ARYA's reply for browser requests, else None."""
    from arya.core import policy
    global _awaiting_song_until

    answer = _song_answer(message)
    if answer:                                        # the song ARYA just asked for
        message = f"play {answer} on {'YouTube Music' if MUSIC_SITE == 'youtube music' else 'YouTube'}"

    from arya.tools.music import _FOCUS
    if _FOCUS.search(message) and not _PLAY.search(message) and (
            os.getenv("ARYA_FOCUS_MUSIC_URL", "").strip()
            or re.search(r"\b(?:focus|study|concentration|background)\s+music\b", message, re.IGNORECASE)):
        return None          # your focus playlist (or how to set it up) — the music handler's; "lofi beats" → YouTube
    m = _PLAY.search(message)
    s = None if m or _ANY_MUSIC.match(message) else _PLAY_SONG.match(message)
    if m or s:
        if m:
            eng, query = m.group("eng").lower(), m.group("q").strip(" ,.")
        else:
            eng = MUSIC_SITE if MUSIC_SITE in WATCH_URLS else "youtube"
            query = (s.group("q1") or s.group("q2") or s.group("q3")).strip(" ,.")
        vid = await first_video(query)
        url = WATCH_URLS[eng].format(id=vid) if vid else SEARCH_URLS[eng].format(q=quote_plus(query))
        if not await _open(url):
            return f"I couldn't open {_site(eng)}."
        policy.note_result("open_url")
        from arya.tools import music
        music.forget()
        return (f"Playing {query} on {_site(eng)}." if vid
                else f"Here are the {_site(eng)} results for {query}; pick one to play.")

    if _ANY_MUSIC.match(message):
        from arya.tools import music
        now = await music.now_playing()
        if now and not now["playing"] and (now["title"] or now["artist"]):
            from arya.tools.media import media_control
            await media_control("play_pause")
            policy.note_result("media_control")
            return f"Resuming {now['title'] or 'your music'}{' by ' + now['artist'] if now['artist'] else ''}."
        if not await _open(SITES["youtube music"]):
            return "I couldn't open YouTube Music."
        policy.note_result("open_url")
        _awaiting_song_until = time.monotonic() + SONG_ANSWER_S
        return "Opening YouTube Music. Tell me a song or an artist and I'll put it on."

    m = _OPEN_SITE.match(message)
    if m:
        site, query = m.group("site").lower(), (m.group("q") or "").strip(" ,.")
        url = SEARCH_URLS[site].format(q=quote_plus(query)) if query and site in SEARCH_URLS else SITES[site]
        if not await _open(url):
            return f"I couldn't open {site.title()}."
        policy.note_result("open_url")
        name = _site(site) if site in SEARCH_URLS else site.title().replace("Github", "GitHub").replace("Chatgpt", "ChatGPT")
        if query:
            return f"Searching {name} for {query}."
        if re.search(r"\band\s+search\b", message, re.IGNORECASE):
            return f"{name} is open. What should I search for?"
        return f"Opening {name}."

    m = _SEARCH.search(message)
    if m:
        query = (m.group("q") or m.group("q2") or m.group("q3") or "").strip(" ,.")
        eng = _engine(m.group("eng") or m.group("eng2") or ("google" if m.group("q3") else None))
        if not query:
            return f"What should I search {_site(eng)} for?"
        if not await _open(SEARCH_URLS[eng].format(q=quote_plus(query))):
            return f"I couldn't open {_site(eng)}."
        policy.note_result("open_url")
        return f"Searching {_site(eng)} for {query}."

    shortcut, reply = None, None
    if _REOPEN.search(message):
        shortcut, reply = (VK_CONTROL, VK_SHIFT, ord("T")), "Reopened the last closed tab."
    elif _CLOSE.search(message):
        shortcut, reply = (VK_CONTROL, ord("W")), "Closed the tab."
    elif _NEW.search(message):
        shortcut, reply = (VK_CONTROL, ord("T")), "New tab open."
    elif (g := _GOTO.search(message)):
        if g.group("last"):
            shortcut, reply = (VK_CONTROL, ord("9")), "Switched to the last tab."
        else:
            raw = (g.group("n") or g.group("o")).lower()
            n = _ORD.get(raw) or int(raw)
            shortcut, reply = (VK_CONTROL, ord(str(n))), f"Switched to tab {n}."
    elif _NEXT.search(message):
        shortcut, reply = (VK_CONTROL, VK_TAB), "Next tab."
    elif _PREV.search(message):
        shortcut, reply = (VK_CONTROL, VK_SHIFT, VK_TAB), "Previous tab."
    if not shortcut:
        return None
    error = await asyncio.to_thread(_in_chrome, *shortcut)
    if error:
        if error == "Chrome isn't open." and shortcut == (VK_CONTROL, ord("T")):
            from arya.tools.os_tools import open_app
            if not (await open_app("chrome")).startswith("Failed"):
                policy.note_result("open_url")
                return "Opened Chrome with a new tab."
        return error
    policy.note_result("open_url")
    return reply
