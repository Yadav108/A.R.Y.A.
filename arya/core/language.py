"""
Hindi and Hinglish (2026-11-17): ARYA understands both and answers in Hinglish — Hindi in the Latin alphabet,
mixed with English, the way people talk. Measured on this laptop: the local 3B model writes readable, correct
Hinglish but not fluent Devanagari Hindi (grammatical-looking nonsense, also when translating), so Hinglish it is.

Understanding speech: Whisper transcribes as English (fast, and best for your accented English — automatic
language detection called your "Hey Aria" Tamil, Telugu and even Hindi). When the result is romanized Hindi
("Kal ek bajhe mujhe yaad dilana"), the same audio is run once more in translate mode ("Remind me tomorrow at 1
o'clock"), so timers, music, tabs and the rest work as in English; the turn is marked Hinglish.

When ARYA answers in Hinglish: a Hinglish or Devanagari request, the rest of that conversation (15 minutes), or
always after "Hindi mein baat karo" / "talk to me in Hindi" until "English please". Instant replies (timers set,
music paused…) stay short English.
"""
from __future__ import annotations

import re
import threading
import time

STICKY_S = 15 * 60
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Distinctly Hindi words (romanized). No English look-alikes ("to", "the", "do", "hi", "me", "ka"…).
_HINDI_WORDS = set("""
hai hain tha thi kya kyu kyun kyon kaise kaisa kaisi kaun kab kahan kidhar idhar udhar mujhe mujhko mera meri
mere tera teri tere tum tumhe tumhara tumhari aap aapka aapki aapko hum humko hamara nahi nahin haan haa accha
acha achha theek thik yaar yar bhai bhaiya kuch kuchh abhi aaj kal parso batao bata bataiye samjhao samjha karo
karna karde kardo kar raha rahi rahe hoon hun gaya gayi gaye wala wali wale bahut bohot bahot sab sabko chalo
dena dedo dijiye lagao laga lagado chahiye matlab phir fir dobara jaldi zara thoda thodi mein mai ek baar
bajhe baje bajay yaad dilana dilao sunao suno bolo likho dikhao chalao khol kholo ruko rukna wapas
waapas pehle baad liye wajah kyunki lekin aur bhi sirf zyada raat subah shaam dopahar ghar khana
pani paani padhai padhna kaam aana jaana dekho dekhna achhe bure bura acchi sahi galat
""".split())          # not "main", "band", "ya", "bas", "din", "kam", "bol": they are English words too
_ALWAYS_HINGLISH = re.compile(r"\b(?:hindi|hinglish)\s+(?:mein|me|main)\s+(?:baat\s+)?(?:karo|kar|bolo|bol|batao)\b|"
                              r"\b(?:talk|speak|reply|answer)\s+(?:to\s+me\s+)?in\s+(?:hindi|hinglish)\b|"
                              r"\bswitch\s+to\s+(?:hindi|hinglish)\b", re.IGNORECASE)
_ENGLISH_AGAIN = re.compile(r"^\W*(?:arya\W+)?(?:please\s+)?(?:(?:talk|speak|reply|answer)\s+(?:to\s+me\s+)?in\s+english|"
                            r"english\s+(?:please|only)|switch\s+(?:back\s+)?to\s+english|back\s+to\s+english)\W*$",
                            re.IGNORECASE)

_lock = threading.Lock()
_spoken: dict[str, str] = {}             # English text ARYA works with -> what you actually said (romanized)
_state = {"sticky_until": 0.0, "always": False}


def hindi_words(text: str) -> tuple[int, int]:
    words = re.findall(r"[a-z']+", (text or "").lower())
    return sum(w in _HINDI_WORDS for w in words), len(words)


def is_hinglish(text: str) -> bool:
    """Romanized Hindi (Hinglish): 'Kal ek bajhe mujhe yaad dilana', 'yaar aaj kya plan hai'."""
    hits, total = hindi_words(text)
    return hits >= 3 or (hits >= 2 and hits / max(1, total) >= 0.3)


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def note_spoken(original: str, english: str) -> None:
    """Speech: you said `original` (romanized Hindi); ARYA works with its English translation."""
    with _lock:
        _spoken[english.strip()] = original.strip()
        while len(_spoken) > 20:
            _spoken.pop(next(iter(_spoken)))


def said(message: str) -> str | None:
    """What you actually said, if `message` is ARYA's English translation of Hindi speech."""
    with _lock:
        return _spoken.get(message.strip())


def turn_language(message: str, now: float | None = None) -> str:
    """'hinglish' or 'en' for the reply to this request (and it keeps the conversation sticky)."""
    now = now or time.time()
    if _ENGLISH_AGAIN.match(message):
        _state.update(sticky_until=0.0, always=False)
        return "en"
    if _ALWAYS_HINGLISH.search(message):
        _state["always"] = True
    hindi = said(message) is not None or has_devanagari(message) or is_hinglish(message)
    if hindi:
        _state["sticky_until"] = now + STICKY_S
        return "hinglish"
    if _state["always"] or now < _state["sticky_until"]:
        return "hinglish"
    return "en"


def matches(message: str) -> bool:
    return bool(_ALWAYS_HINGLISH.search(message) or _ENGLISH_AGAIN.match(message))


def handle(message: str) -> str | None:
    """'Hindi mein baat karo' / 'English please' — the switch itself (the next answers follow it)."""
    if _ENGLISH_AGAIN.match(message):
        turn_language(message)
        return "Sure, English from now on."
    if _ALWAYS_HINGLISH.search(message) and len(message.split()) <= 8:
        turn_language(message)
        return "Bilkul! Ab se main aapse Hinglish mein baat karungi. English chahiye toh bas \"English please\" bolna."
    return None


# With a worked example: measured, the bare rule was broken ("kal mera exam hai…" got a Devanagari answer).
RULE = ("\nLanguage: the user is talking Hinglish (Hindi mixed with English). Reply in natural, casual Hinglish written "
        "ONLY in the Latin alphabet, the way friends in India text each other; keep technical terms, names and numbers "
        "in English. Never use Devanagari script. Example — user: \"kal mera exam hai, dar lag raha hai\" — you: "
        "\"Arre, tension mat lo! Aaj raat achhi neend lo, aur kal pehle woh questions karo jo tumhe aate hain. "
        "All the best!\"\n")


# ── Safety net: Devanagari in a Hinglish answer is written in Latin letters (the English voice can't read it) ──

_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e", "ऐ": "ai",
           "ओ": "o", "औ": "au", "ऑ": "o"}
_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
           "ॉ": "o", "ं": "n", "ँ": "n", "ः": "h"}
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
         "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
         "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
         "ष": "sh", "स": "s", "ह": "h", "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "rh", "फ़": "f"}
_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def romanize(text: str) -> str:
    """'कल का परीक्षा' -> 'kal ka pareeksha' (readable Hinglish; the inherent 'a' is dropped at word ends)."""
    out, i, chars = [], 0, list(text.translate(_DIGITS).replace("।", ".").replace("़", ""))
    while i < len(chars):
        ch = chars[i]
        if ch in _CONS:
            out.append(_CONS[ch])
            nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if nxt == "्":                                  # virama: no vowel
                i += 2
                continue
            if nxt in _MATRAS:
                out.append(_MATRAS[nxt])
                i += 2
                continue
            word_end = not nxt or not ("ऀ" <= nxt <= "ॿ")
            if not word_end:
                out.append("a")
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _MATRAS:
            out.append(_MATRAS[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def prompt_rule(lang: str) -> str:
    return RULE if lang == "hinglish" else ""
