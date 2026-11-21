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


def _type(text: str) -> None:
    """Type text as keyboard input (Unicode, so any language works)."""
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_UNICODE, INPUT_KEYBOARD = 0x0004, 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class INPUT(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    for ch in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | _KEYUP):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.ki = KEYBDINPUT(0, ord(ch), flags, 0, None)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


TAB_SEARCH_WAIT_S = 2.0
# Lead-ins Whisper keeps: "First, open Dynamics X-Solution PDF tab." (2026-11-21: went to the model -> open_app)
_LEAD = r"^\W*(?:(?:arya|first|now|then|ok|okay|so|and|also)\W+)*"
TAB_ANSWER_S = 90.0                     # after "Which tab?", the next short phrase is the tab's name
_awaiting_tab = {"until": 0.0}
_FILLER_WORDS = {"tab", "tabs", "the", "with", "that", "this", "from", "page", "window", "chrome", "open", "my",
                 "and", "for", "about", "showing"}


def tab_queries(query: str) -> list[str]:
    """What to type into Chrome's tab search: the whole name, then each telling word on its own."""
    words = [w for w in re.findall(r"[\w.]+", query) if len(w) >= 4 and w.lower() not in _FILLER_WORDS]
    out: list[str] = []
    for q in [query.strip()] + (words if len(words) > 1 else []):
        if q and q.lower() not in (o.lower() for o in out):
            out.append(q)
    return out[:5]


def awaiting_tab() -> bool:
    return time.monotonic() < _awaiting_tab["until"]


# The answer to "Which tab?": "Dynamics Ex Solutions.pdf", "the dynamics one", "PDF". Not a new request.
_TAB_ANSWER = re.compile(r"^\W*(?:(?:it'?s|the|my)\s+)*(?P<q>[^?]{2,60}?)(?:\s+(?:one|tab|please))*\W*$",
                         re.IGNORECASE)


_NOT_AN_ANSWER = re.compile(r"^\W*(?:thanks?|thank you|ok|okay|no|nope|never ?mind|cancel|stop|forget it|yes|yeah|"
                            r"yep|fine|cool|great|nothing|leave it|doesn'?t matter|switch|go|jump|move|take me)\b",
                            re.IGNORECASE)
_TO_IT = re.compile(_LEAD + r"(?:please\s+)?(?:switch|go|jump|move|take me)\s+(?:back\s+)?(?:to|over to|there)\s*"
                    r"(?:it|that|that one|this one|there)?\W*$", re.IGNORECASE)


def _tab_answer(message: str) -> str | None:
    if not awaiting_tab() or _NEW_REQUEST.match(message) or _NOT_AN_ANSWER.match(message) or len(message.split()) > 8:
        return None
    m = _TAB_ANSWER.match(message)
    q = m.group("q").strip(" .\"“”'‘’") if m else ""
    return q if q and not _NOT_A_NAME.match(q) else None


def _is_chrome(hwnd: int) -> bool:
    try:
        import psutil
        import win32process
        return psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name().lower() == "chrome.exe"
    except Exception:
        return False


def _foreground() -> int:
    import win32gui
    return win32gui.GetForegroundWindow()


def _wait_for(test, seconds: float) -> int | None:
    """Poll the foreground window until test(hwnd) holds; that window, or None after `seconds`."""
    end = time.monotonic() + seconds
    while True:
        f = _foreground()
        if f and test(f):
            return f
        if time.monotonic() >= end:
            return None
        time.sleep(0.03)


def _switch_to_named_tab(query: str) -> str:
    """Chrome's own tab search (Ctrl+Shift+A): type the name, Enter — then read which tab is showing."""
    import win32gui

    hwnd = _chrome_window()
    if hwnd is None:
        return "Chrome isn't open."
    if not _focus(hwnd):
        return "I couldn't bring Chrome to the front, so I didn't press anything."
    _press(VK_CONTROL, VK_SHIFT, ord("A"))
    # Type only into the tab-search popup itself. (2026-11-21: with a fixed 0.45 s wait the popup sometimes
    # wasn't open yet, and the tab name + Enter went into the page in front — a chat.)
    popup = _wait_for(lambda f: f != hwnd and _is_chrome(f), TAB_SEARCH_WAIT_S)
    if popup is None:
        return "Chrome's tab search didn't open, so I didn't type anything. Try again, or say the tab's position."
    found = False
    for i, attempt in enumerate(tab_queries(query)):
        if i:
            _press(VK_CONTROL, ord("A"))                # replace the last attempt in the search box
        _type(attempt)
        time.sleep(0.5)                                 # results filter as you type
        if _foreground() != popup:
            return "The tab search closed before I could pick a tab, so I didn't press Enter."
        _press(0x0D)                                    # Enter: the best match
        if _wait_for(lambda f: f == hwnd, 1.0) is not None or _foreground() != popup:
            found = True
            break
        # No tab matched: Enter does nothing and the popup stays open. Try fewer words ("dynamics exercise
        # solution" finds nothing; "dynamics" finds "Dynamics Ex Solutions.pdf" — 2026-11-21 log).
    if not found:
        # Close it: otherwise the next Ctrl+Shift+A closes it instead of opening it (2026-11-21, live).
        _press(0x1B)
        _awaiting_tab["until"] = time.monotonic() + TAB_ANSWER_S
        return f"I couldn't find a tab matching {query}. Which tab? Say a word from its title."
    _awaiting_tab["until"] = 0.0
    time.sleep(0.15)                                    # the title follows the switch
    title = re.sub(r"\s+-\s+Google Chrome$", "", win32gui.GetWindowText(_chrome_window() or hwnd))
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if words and not any(w in title.lower() for w in words):
        return f"I searched your tabs for {query}; Chrome is showing “{title}” now. If that's not it, say the tab's title."
    return f"Switched to “{title}”."


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
# 2026-11-21 check: "switch tabs", "change the tab", "tab to the right", "switch to the other tab",
# "go back a tab", "second tab", "last tab" went to the model, which can't press keys.
_NEXT = re.compile(rf"\b(?:next|following)\s+{_TAB}\b|\bswitch\s+(?:to\s+the\s+)?(?:next\s+)?{_TAB}\b|"
                   rf"\b(?:switch|change)\s+(?:the\s+)?{_TAB}s?\b|\b{_TAB}\s+(?:to\s+the\s+)?right\b|"
                   rf"\b(?:switch|go|move)\s+(?:back\s+)?to\s+the\s+other\s+{_TAB}\b|"
                   rf"\b(?:go|move)\s+(?:forward|ahead)\s+(?:a|one)\s+{_TAB}\b", re.IGNORECASE)
_PREV = re.compile(rf"\b(?:previous|prev|left)\s+{_TAB}\b|\bswitch\s+(?:to\s+the\s+)?previous\s+{_TAB}\b|"
                   rf"\b{_TAB}\s+(?:to\s+the\s+)?left\b|\b(?:go|move)\s+back\s+(?:a|one)\s+{_TAB}\b|"
                   rf"\bback\s+one\s+{_TAB}\b", re.IGNORECASE)
_GOTO = re.compile(rf"\b(?:go|switch|jump)\s+to\s+(?:the\s+)?(?:{_TAB}\s+(?P<n>\d|one|two|three|four|five|six|"
                   rf"seven|eight)|(?P<o>first|second|third|fourth|fifth|sixth|seventh|eighth)\s+{_TAB}|"
                   rf"(?P<last>last)\s+{_TAB})\b|"
                   # just the position: "second tab", "last tab", "tab 2"
                   rf"^\W*(?:the\s+)?(?:{_TAB}\s+(?P<n2>\d|one|two|three|four|five|six|seven|eight)|"
                   rf"(?P<o2>first|second|third|fourth|fifth|sixth|seventh|eighth)\s+{_TAB}|"
                   rf"(?P<last2>last)\s+{_TAB})(?:\s+please)?\W*$", re.IGNORECASE)
# "go back to the last tab I was on": Chrome has no key for the tab used before (tab search's order isn't
# reliable either — tried live), so ARYA says what works instead of guessing.
_RECENT = re.compile(rf"\b(?:tab\s+i\s+(?:was\s+(?:on|in|using)|had\s+open|used)(?:\s+before)?|"
                     rf"(?:last|previously)\s+used\s+{_TAB})\b", re.IGNORECASE)
# "switch to the Claude tab", "go to my Gmail tab", "switch to 'claude' tab opened in chrome under JCNS group"
# (from the log, 2026-11-16 19:17: only positions — next / previous / tab 3 — were understood)
_NAMED_TAB = re.compile(_LEAD + r"(?:(?:can you|could you|please)\s+)?(?:switch|go|jump|take me|move)\s+"
                        # "go to the tab with the PDF" (2026-11-21: searched the tabs for "the")
                        r"(?:back\s+)?(?:to|over to)\s+the\s+tab\s+(?:with|that\s+has|showing|for|about|of)\s+"
                        r"(?:the\s+|my\s+|a\s+)?(?P<q3>.+?)\W*$|"
                        # "open the Gmail tab" / "open Dynamics PDF tab": switch to it ("open a new tab" opens one)
                        + _LEAD + r"(?:(?:can you|could you|please)\s+)?open\s+(?:the\s+|my\s+)?[\"“'‘]?(?P<q4>.+?)"
                        r"[\"”'’]?\s+tab\W*$|"
                        + _LEAD + r"(?:(?:can you|could you|please)\s+)?(?:switch|go|jump|take me|move)\s+"
                        r"(?:back\s+)?(?:to|over to)\s+(?:the\s+|my\s+)?[\"“'‘]?(?P<q>.+?)[\"”'’]?\s+tab\b.*$|"
                        + _LEAD + r"(?:please\s+)?(?:show|find)\s+(?:me\s+)?(?:the\s+|my\s+)?[\"“'‘]?(?P<q2>.+?)"
                        r"[\"”'’]?\s+tab\W*$", re.IGNORECASE)
_NOT_A_NAME = re.compile(r"^(?:next|previous|prev|last|first|second|third|fourth|fifth|sixth|seventh|eighth|new|"
                         r"other|closed|last closed|left|right|\d+|the|a|an|this|that|my|it|current|"
                         r"empty|blank|previously used|last used)$", re.IGNORECASE)
# "open a new tab", "open another tab", "open the last closed tab" are not tab names
_NOT_OPEN_NAME = re.compile(r"^(?:a|an|another|one|new|empty|blank|incognito|private)\b|\bclosed\b", re.IGNORECASE)
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
# "Play Sati Gatha", "play romantic song" — any other "play X" is a song, unless X is a media key ("play it",
# "play the next song"), a game, or something on the laptop. (From the log: these went to the model, which
# pressed play/pause and nothing started.)
_PLAY_BARE = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:can you\s+|could you\s+)?play\s+(?:me\s+)?(?P<q>.+?)"
                        r"(?:\s+please)?\W*$", re.IGNORECASE)
_NOT_A_SONG = re.compile(r"^(?:it|that|this|them|again|something|anything|back|along|around|"
                         r"(?:the\s+|my\s+|some\s+|any\s+)?(?:music|video|playback|audio|sound)|"
                         r"(?:the\s+)?(?:next|previous|last|same)\b.*|.*\b(?:again|game|games|chess|quiz|"
                         r"card|cards|flashcards?|file|recording|lecture|voice ?note|podcast|"
                         # study: "play the step response", "play the simulation"
                         r"response|plot|graph|animation|simulation|diagram|example|exercise|solution|slides?|"
                         r"step|bode|signal)\b.*)$", re.IGNORECASE)
# "Open YouTube Music and play romantic song" → one tab that plays it, not an empty site plus a search
_OPEN_AND_PLAY = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:open|go to|launch)\s+(?P<eng>youtube music|youtube)"
                            r"\s*,?\s*(?:and\s+)?(?:then\s+)?play\s+(?P<q>.+?)\W*$", re.IGNORECASE)
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


def _song_query(raw: str) -> str:
    """'the song "Kaun Tujhe" by Kishore Kumar' -> 'Kaun Tujhe by Kishore Kumar' (what to search for)."""
    q = re.sub(r"[\"“”‘’']", "", raw).strip(" ,.")
    q = re.sub(r"^(?:me\s+)?(?:the\s+|a\s+)?(?:song|track|music video|music)\s+(?:called\s+|named\s+)?", "", q,
               flags=re.IGNORECASE)
    q = re.sub(r"\s+(?:song|track)$", "", q, flags=re.IGNORECASE)
    return q.strip(" ,.") or raw.strip(" ,.")


def _bare_song(message: str):
    """'play Sati Gatha' → match with the song in group 'q'; None for media keys, games and the like."""
    m = _PLAY_BARE.match(message)
    if not m or _NOT_A_SONG.match(m.group("q").strip(" ,.!?")) or not re.search(r"[A-Za-z]{2,}", m.group("q")):
        return None
    from arya.core.llm import quick_media_command
    return None if quick_media_command(message) else m


def named_tab(message: str) -> str | None:
    """'switch to the Claude tab' -> 'Claude'; None for positions ('the next tab', 'the second tab')."""
    m = _NAMED_TAB.match(message)
    if not m:
        return None
    if m.group("q4") and _NOT_OPEN_NAME.search(m.group("q4")):
        return None
    q = (m.group("q") or m.group("q2") or m.group("q3") or m.group("q4") or "").strip(" \"“”'‘’.")
    if not q or _NOT_A_NAME.match(q) or len(q) > 60:
        return None
    return q


def matches(message: str) -> bool:
    """Would handle() act on this (without doing it)? For splitting multi-part requests."""
    return bool(named_tab(message) or _OPEN_AND_PLAY.match(message) or _PLAY.search(message) or _PLAY_SONG.match(message)
                or _bare_song(message) or _ANY_MUSIC.match(message) or _OPEN_SITE.match(message)
                or _SEARCH.search(message) or _REOPEN.search(message) or _CLOSE.search(message)
                or _NEW.search(message) or tab_command(message))


def tab_command(message: str) -> bool:
    """A Chrome tab command — for ARYA's "is this for me?" rules ("switch tab", "second tab", "the Gmail tab")."""
    return bool(named_tab(message) or _GOTO.search(message) or _NEXT.search(message) or _PREV.search(message)
                or _RECENT.search(message) or _CLOSE.search(message) or _REOPEN.search(message)
                or _tab_answer(message) or (awaiting_tab() and _TO_IT.match(message)))


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
    m = _OPEN_AND_PLAY.match(message) or _PLAY.search(message)
    s = None if m or _ANY_MUSIC.match(message) else (_PLAY_SONG.match(message) or _bare_song(message))
    if m or s:
        if m:
            eng, query = m.group("eng").lower(), _song_query(m.group("q"))
        else:
            eng = MUSIC_SITE if MUSIC_SITE in WATCH_URLS else "youtube"
            query = _song_query(next(g for g in s.groups() if g))
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
            await media_control("play")
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

    if awaiting_tab() and _TO_IT.match(message):        # "switch to it" — ARYA doesn't know which one yet
        _awaiting_tab["until"] = time.monotonic() + TAB_ANSWER_S
        return "Which tab? Say a word from its title, like \"dynamics\"."
    named = named_tab(message) or _tab_answer(message)
    if named:
        reply = await asyncio.to_thread(_switch_to_named_tab, named)
        if not reply.startswith(("Chrome isn't", "I couldn't")):
            policy.note_result("open_url")
        return reply

    shortcut, reply = None, None
    if _REOPEN.search(message):
        shortcut, reply = (VK_CONTROL, VK_SHIFT, ord("T")), "Reopened the last closed tab."
    elif _CLOSE.search(message):
        shortcut, reply = (VK_CONTROL, ord("W")), "Closed the tab."
    elif _NEW.search(message):
        shortcut, reply = (VK_CONTROL, ord("T")), "New tab open."
    elif _RECENT.search(message):
        return ("Chrome has no shortcut for the tab you used before. Tell me its name, like \"switch to the "
                "Gmail tab\", or say \"previous tab\".")
    elif (g := _GOTO.search(message)):
        if g.group("last") or g.group("last2"):
            shortcut, reply = (VK_CONTROL, ord("9")), "Switched to the last tab."
        else:
            raw = (g.group("n") or g.group("o") or g.group("n2") or g.group("o2")).lower()
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
