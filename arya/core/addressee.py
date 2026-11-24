"""
Is this sentence meant for ARYA?

After "Hey ARYA", ARYA keeps listening for follow-ups without the wake word.
But a room isn't quiet: you take a phone call, talk to someone, a video plays.
Before answering a follow-up, ARYA decides whether it was addressed:

  1. Rules settle the clear cases instantly (its name, a yes/no it's waiting
     for, stop/thanks, media commands).
  2. Otherwise the LOCAL model judges it from context — ARYA's last reply, how
     long ago that was, the recent conversation and the new sentence. This
     always runs on the laptop, even in cloud mode: other people's speech
     never leaves the machine.

Wake-word commands ("Hey ARYA, …") and typed messages are never checked —
they're addressed by definition.
"""
from __future__ import annotations

import os
import re
import time

SMART_LISTENING_DEFAULT = os.getenv("ARYA_SMART_LISTENING", "1") != "0"
IGNORED_LIMIT = 3                 # this many ignored in a row ends the conversation

_NAME = re.compile(r"\b(?:arya|aria|ariya|aarya|n\.?\s?j\.?|en\s?jay|enjay)(?!\w)", re.IGNORECASE)   # + "NJ"
# Also with a lead-in or a tag: "Okay, go ahead.", "Yes, sir.", "Thank you very much.", "Yeah, thank you
# sweetheart." (from the log: these went to the model, which judged them side talk).
_ALWAYS_FOR_ARYA = re.compile(
    r"^\W*(?:(?:ok|okay|yeah|yes|alright|oh|well|so|right)\W+)?"
    # A bare "yes" / "yeah" / "no" is only for ARYA while it waits for one (pending_confirmation); otherwise
    # the model judges it (from the log: "Oh yeah." / "Uh, yeah." from side talk were answered).
    r"(?:stop|cancel|never ?mind|be quiet|shut up|thanks?|thank you|cheers|bye|goodbye|"
    r"that'?s (?:all|it)|repeat that|say (?:that|it) again|what\??|how are you(?: doing)?(?: today)?|"
    r"go ahead|go on|continue|carry on|do it|tell me more|more please)"
    r"(?:\W+(?:sir|ma'?am|dude|bro|buddy|sweetheart|dear|man|very much|so much|a lot|again|please|then|"
    r"arya|aria|lol|haha|hahaha))*\W*$",      # "How are you?", "Thanks. LOL." (2026-11-19: rescued by hand)
    re.IGNORECASE,
)

# "Yes, sir." / "No, ARYA." — a yes/no with a tag said to it is still for ARYA; a bare "Oh yeah." isn't settled.
_YES_TO_ARYA = re.compile(r"^\W*(?:(?:ok|okay|oh|well)\W+)?(?:yes|yeah|yep|no|nope)\W+(?:sir|ma'?am|arya|aria)\W*$",
                          re.IGNORECASE)

# "Mom, …", "Sarah, did you…", "hey guys, …" — speech that opens by addressing someone else.
_OTHER_PERSON = re.compile(
    r"^\W*(?:hey|hi|oi|yo|ok|okay)?\W*(?:mom|mum|mommy|dad|daddy|papa|mama|honey|babe|baby|dear|darling|guys|"
    r"bro|dude|man|sis|buddy|everyone|(?!(?i:arya|aria|ok|okay|yes|yeah|yep|yup|no|nope|so|well|now|hey|hi|"
    r"and|but|then|wait|hmm|hm|um|uh|oh|ah|right|alright|sure|thanks|sorry|actually|also|cool|great|nice|fine|"
    r"perfect|good|look|listen|please|maybe|anyway|honestly|really|true|exactly|what|why|how|wow|"
    # Whisper capitalises every sentence's first word: "Plus, …", "Basically, …", "Tomorrow, remind me…"
    # are not names (from the log: all three were dropped as speech to someone else).
    r"plus|basically|one|two|three|first|second|third|next|finally|besides|otherwise|however|meanwhile|"
    r"tomorrow|today|tonight|later|yesterday|obviously|seriously|literally|instead|also|again|still|"
    r"okay|listen|see|like|well|yo|hello|here|there|this|that|now|honestly|sometimes|usually|"
    r"morning|evening|afternoon|so|and|or|if|when|because|after|before)\b)[A-Z][a-z]+)"
    r"\s*,",
)
# Commands of the kind people give an assistant: answered without asking the model.
_ASSISTANT_COMMAND = re.compile(
    r"^\W*(?:please\s+)?(?:set|start|open|close|play|pause|skip|turn|remind|calculate|convert|search|"
    r"look up|show me|list|delete|move|rename|define|plot|graph|draw|sketch|mute|unmute|"
    r"minimi[sz]e|maximi[sz]e|restore|reopen|switch to|go to tab|new tab|next tab|previous tab|google|"
    r"tidy|summari[sz]e|undo|quiz me|check my|stop studying|go to|minimi[sz]ed|maximi[sz]ed|"
    r"(?:some|any) music|put on)\b",
    re.IGNORECASE,
)
# Question openers. People ask each other these too ("Where is it?", "Can you explain that?"),
# so they only skip the model when the question has real content of its own.
_QUESTION_OPENER = re.compile(
    r"^\W*(?:(?:can|could|would|will) you|please|what(?:'s| is| are| was| does| do)|how (?:much|many|long|far|"
    r"do|does|is|to)|when (?:is|does|was)|where (?:is|are)|who (?:is|was)|why (?:is|does|do)|tell me|read|find|"
    r"explain|describe)\b",
    re.IGNORECASE,
)
_VAGUE = set("""it its that this these those there here then them they him her his she he we us our me my you your
something anything everything thing stuff one ones what why how where when who which
do does did done doing is are was were be been have has had get got go going gone come
tell told say said know knew mean meant think explain describe please again now just really
can could would will should shall might must not don't didn't doesn't isn't wasn't aren't won't
the and but for with about from into out off over very also only still even
okay well yeah yes right sure thanks""".split())


def _has_content(text: str) -> bool:
    """'Can you explain sliding friction' -> True; 'Can you explain that?' / 'Where is it?' -> False."""
    rest = _QUESTION_OPENER.sub("", text, count=1)
    words = re.findall(r"[a-zA-Z][a-zA-Z']+|\d+", rest.lower())
    return any((w.isdigit() or len(w) >= 3) and w not in _VAGUE for w in words)

_PROMPT = (
    "A voice assistant called ARYA is in a hands-free conversation with its user, who does not need "
    "to say ARYA's name between turns. Its microphone just picked up a new sentence. Decide whether "
    "that sentence was said TO ARYA.\n\n"
    "Say ARYA if it is: a request or question for the assistant, an answer to something ARYA just asked, "
    "a follow-up on what ARYA said (\"and tomorrow?\", \"why?\", \"make it shorter\"), a correction "
    "(\"no, I meant Berlin\"), or a reaction to ARYA. A direct command or question of the kind people ask "
    "an assistant (maths, facts, weather, timers, apps, files) is for ARYA even when the topic is new.\n"
    "Say OTHER if it is: the user talking to another person (often using their name, \"mom\", \"honey\", "
    "\"guys\"), one side of a phone call, thinking out loud, reading something aloud, TV or video audio, "
    "or a remark ABOUT the assistant in the third person (\"she's slow today\").\n"
    "A sentence that continues the topic ARYA was just discussing is usually for ARYA. A sentence about "
    "something unrelated, said long after ARYA last spoke, is usually not.\n\n"
    "Reply with exactly one word: ARYA or OTHER."
)


# How Whisper writes "ARYA" in follow-ups ("Area.", "Uh, Aria?", "Hey, Ario.") — only trusted when the name
# is the whole utterance or opens it, followed by a pause ("Area, play …").
_LOOSE_NAME = (r"(?:a+r+i?y+a+h?|aria+h?|ariya|aarya|ario|aryo|area|harya|arja|ayra|ariel|arial"   # "Hey, Ariel." (log)
               r"|n\.?\s?j\.?|en\s?jay|enjay)")                                                     # "NJ"
_NAME_ALONE = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|uh|um|so|yo)\W+)?{_LOOSE_NAME}\W*$", re.IGNORECASE)
_NAME_FIRST = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|uh|um|so|yo)\W+)?{_LOOSE_NAME}\s*[,.!?:;]\s*(?=\S)",
                         re.IGNORECASE)
# "ARYA, play …" often comes out as "I'll play …".
_MISHEARD_PLAY = re.compile(r"^\W*(?:i'll|i will|ill)\s+(?=play\b)", re.IGNORECASE)


# "NJ" as Whisper writes it without a hint (measured on the user's 22 recordings: "Enjoy! Brief me.",
# "Hey, enjoy.", "Okay, engine, minimize the window."). Only at the very start, followed by punctuation AND
# more words: "Enjoy your meal" / "the engine is hot" stay as they are, and a lone "Enjoy!" is not the name
# (4 of 69 sound-alike clips came out as exactly that). Recognises 11 of the 22; only when "Hey NJ" is set up.
# 2026-11-19 log: "Andrew, you're kidding me", "Uh, Yonjai, brief me.", "ENG, remember …", "Hey, GMT, do you …"
# (ARYA then called the user Andrew). A comma after the name and more words are still required.
_NJ_FORMS = r"(?:enjoy|enjie|anjay|anjie|eun-?jae|ng|engine|eng|andrew|yonjai|yonjay|enjai|injay|gmt)"
_MISHEARD_NJ = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|uh|um|so)\W+)?{_NJ_FORMS}\s*[!,.:;]\s+"
                          r"(?=\w)", re.IGNORECASE)


# From the log (2026-11-16/17): "Hey, engine." / "Hi, NG." (the name alone, after a greeting) and
# "Enjoy open window explorer." (no pause after it, but a clear command follows).
_MISHEARD_NJ_ALONE = re.compile(rf"^\W*(?:hey|hi|ok|okay)\W+{_NJ_FORMS}\W*$", re.IGNORECASE)
_MISHEARD_NJ_TIGHT = re.compile(r"^\W*(?:(?:hey|hi|ok|okay)\W+)?(?:enjoy|anjay|engine)\s+(?=\w)", re.IGNORECASE)


_LONE_NJ = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|uh|um)\W+)?(?:{_NJ_FORMS}|{_LOOSE_NAME})\W*$", re.IGNORECASE)


def lone_nj(text: str) -> bool:
    """'Enjoy.' / 'Engine?' / 'NJ.' — only the name. Used right after a bare wake word, where a lone 'Enjoy!'
    can't be the start of a sentence for someone else."""
    return bool(_LONE_NJ.match(text or ""))


def misheard_nj(text: str) -> re.Match | None:
    from arya.core import hotword
    if not hotword.extra_models():
        return None
    text = text or ""
    m = _MISHEARD_NJ.match(text) or _MISHEARD_NJ_ALONE.match(text)
    if m:
        return m
    m = _MISHEARD_NJ_TIGHT.match(text)                 # "Enjoy open …": only if the rest is a command for ARYA
    if m and _ASSISTANT_COMMAND.match(text[m.end():]) and not _ENJOY_OBJECT.match(text[m.end():]):
        return m
    return None


_ENJOY_OBJECT = re.compile(r"(?:your|the|it|this|that|yourself|life|my|our|his|her|their)\b", re.IGNORECASE)


def split_name(text: str) -> tuple[str, bool]:
    """('Area, play Kon Tujhe', …) -> ('play Kon Tujhe', True); ('Area.') -> ('', True); else (text, False).
    Used on follow-ups: a sentence that starts with ARYA's name is for ARYA, and the name isn't the request."""
    if _NAME_ALONE.match(text):
        return "", True
    m = _NAME_FIRST.match(text) or _MISHEARD_PLAY.match(text) or misheard_nj(text)
    if m:
        return text[m.end():].strip(), True
    return text, False


# "First, resume the music." (2026-11-21: ignored, then answered in words without pressing play)
_FILLER = re.compile(r"^\W*(?:hurry\s+up|come\s+on|quick(?:ly)?|okay\s+so|ok\s+so|alright|all\s+right|now|first|"
                     r"first\s+of\s+all|then|and\s+then|after\s+that|next|also|okay|ok|"
                     r"listen|wait|oh|hmm|yeah|yes|no|sorry|please|and|so|um|uh)\s*[,.!:;-]+\s*(?=\w)", re.IGNORECASE)


def strip_filler(text: str) -> str:
    """'Hurry up, pause the music.' -> 'pause the music.' — only when what follows is a clear command."""
    m = _FILLER.match(text or "")
    rest = text[m.end():].strip() if m else ""
    return rest if rest and quick_verdict(rest) is True else text


# "That wasn't for you", "I wasn't talking to you", "not you, NJ" — the last answered sentence was side talk.
# A label for the classifier (the opposite of the "It was for you" button).
_NOT_FOR_YOU = re.compile(
    r"^\W*(?:(?:no|nope|sorry|hey|arya|nj|ok|okay)\W+)*(?:that\s+(?:was\s*n[o']?t|wasn'?t|is\s*n[o']?t|isn'?t|"
    r"was\s+not|is\s+not)\s+(?:for|to|meant\s+for)\s+you|i\s+(?:was\s*n[o']?t|wasn'?t|was\s+not)\s+(?:talking|speaking)"
    r"\s+to\s+you|not\s+(?:talking\s+to\s+)?you|(?:i\s+was\s+)?talking\s+to\s+(?:someone|somebody)\s+else|"
    r"(?:that'?s|it'?s|that\s+is)\s+not\s+for\s+you)(?:\W+(?:arya|nj|stop listening))*\W*$", re.IGNORECASE)


# How the current turn reached ARYA: "wake", "name", "typed", "push-to-talk", "phone", or "follow:<how>"
# (rule / classifier with its p / model / answer / unfiltered). Saved with your line; learning reads it.
origin: dict = {"source": "typed", "p": None}

# Sources that are clearly said to ARYA. A follow-up the classifier judged counts only when it was sure.
TRUSTED_SOURCES = {"wake", "name", "typed", "push-to-talk", "phone", "follow:rule", "follow:answer"}
# Measured (arya/research/learning_trust_eval.py, leave-one-day-out): side talk among learnable follow-ups
# 14.0% (everything answered) -> 2.0% at 0.7 with 65% of real requests kept; 0.85 leaked more (2.3%) and kept less.
TRUST_P = 0.7


def set_origin(source: str, p: float | None = None) -> None:
    origin.update(source=source, p=p)


def trusted(source: str | None, p: float | None) -> bool | None:
    """True/False for a recorded source; None when unknown (older lines: judged again when learning)."""
    if not source:
        return None
    if source in TRUSTED_SOURCES:
        return True
    if source == "follow:classifier":
        return p is not None and p >= TRUST_P
    return False                         # the 3B judge, unfiltered speech, side talk


def not_for_you(text: str) -> bool:
    return bool(_NOT_FOR_YOU.match(text or ""))


def record_feedback(text: str, label: str) -> None:
    """Save a label from use ("other" after "that wasn't for you") for the daily refit. Local only."""
    import json
    from datetime import datetime
    from pathlib import Path
    path = Path(os.getenv("ARYA_DATA_DIR") or Path(os.getenv("LOCALAPPDATA") or Path.home()) / "ARYA") / "research" / "feedback.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text, "label": label, "at": datetime.now().isoformat(timespec="seconds")},
                               ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Listening: feedback not saved ({exc})")


def repetitive(text: str) -> bool:
    """'Cheese. Cheese. Cheese. …', 'I don't know, I don't know, but I don't know …', 'This is a joke.' x4:
    the same phrase over and over is Whisper looping on background noise or a song, not a request."""
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    if len(words) < 6:
        return False
    for n in range(1, 13):        # "I don't know what to do with my life" x5 is a 10-word loop (2026-11-21)
        grams = [" ".join(words[i:i + n]) for i in range(0, len(words) - n + 1)]
        if not grams:
            continue
        top = max(set(grams), key=grams.count)
        if grams.count(top) >= 4 and grams.count(top) * n >= 0.6 * len(words):
            return True
    return False


# A short, plain answer right after ARYA asked something real: "Yes, please.", "No.", "The second one."
_BARE_ANSWER = re.compile(r"^\W*(?:(?:yes|yeah|yep|yup|no|nope|sure|okay|ok)\b(?:\W+(?:please|thanks|thank you|"
                          r"go ahead|do it|not now|later))?|the\s+(?:first|second|third|last|other)\s+one)\W*$",
                          re.IGNORECASE)


# Offers that end almost every chatty reply: not a question that waits for an answer.
_GENERIC_QUESTION = re.compile(
    r"(?:how (?:can|may|could) i (?:help|assist)|what(?:'s| is) on your mind|anything else|what (?:else )?(?:can|"
    r"shall|should) i do|what brings you|what would you like (?:to|me to) (?:do|explore|discuss|talk)|"
    r"need (?:help|anything)|just chatting|is there (?:anything|something))[^?]*\?\s*$", re.IGNORECASE)


def asks_a_real_question(reply: str) -> bool:
    """'Shall I play it on YouTube Music?' -> True; 'How can I assist you today?' -> False."""
    reply = (reply or "").rstrip()
    return reply.endswith("?") and not _GENERIC_QUESTION.search(reply[-160:])


def quick_verdict(text: str, pending_confirmation: bool = False) -> bool | None:
    """True/False when a rule settles it, None when the model should decide."""
    if repetitive(text):
        return False
    if not_for_you(text):
        return True                       # said to ARYA, about the last thing it answered
    if pending_confirmation:
        return True                       # ARYA asked a yes/no question — the answer is for it
    if _NAME.search(text):
        return True
    filler = _FILLER.match(text)          # "Hurry up, pause the music." (from the log: ignored)
    if filler and text[filler.end():].strip() and quick_verdict(text[filler.end():]) is True:
        return True
    if _ALWAYS_FOR_ARYA.match(text.strip()) or _YES_TO_ARYA.match(text.strip()):
        return True
    try:
        from arya.core.llm import contextual_media_command, quick_media_command
        if quick_media_command(text) is not None or contextual_media_command(text) is not None:
            return True                   # incl. "louder again" right after a volume change
    except Exception:
        pass
    try:
        from arya.core.llm import quick_status_command
        if quick_status_command(text) is not None:
            return True                   # "battery?", "what time is it"
    except Exception:
        pass
    try:
        from arya.core.llm import _APP_REQUEST
        from arya.tools.os_tools import _APP_ALIASES
        app = _APP_REQUEST.match(text.strip())
        if app and app.group("app").lower().strip(" .") in _APP_ALIASES:
            return True                   # "Windows Explorer" — a bare app name (from the log: overridden)
    except Exception:
        pass
    try:
        from arya.core import activity, capture, intercom, modes, quiet
        from arya.tools import remote, tasks, webcam, worldclock
        if (modes.matches(text) or tasks.matches(text) or intercom.matches(text) or worldclock.matches(text)
                or quiet.matches(text) or remote.matches(text) or activity.matches(text)
                or capture.matches(text) or webcam.matches(text)):
            return True
        from arya.core import habits
        if habits.addressed(text):        # "track a habit: …", "how are my habits?" (not "I went to the gym")
            return True
        from arya.tools import browser
        if browser.tab_command(text):     # "switch tab", "second tab", "the Gmail tab" (2026-11-21: went to the model)
            return True
        from arya.eda import bench
        from arya.eda import projects as eda
        if eda.matches(text) or bench.matches(text):     # incl. "next" / "3.28 volts" during a bench session
            return True                   # "work mode", "what's on my list?"
    except Exception:
        pass
    try:
        from arya.server import phone
        if phone.matches(text):
            return True                   # "connect phone." (from the log: dropped as not for ARYA)
    except Exception:
        pass
    if _OTHER_PERSON.match(text):
        return False                      # opens by addressing someone else
    if _ASSISTANT_COMMAND.match(text):
        return True                       # a command of the kind people give an assistant
    if _QUESTION_OPENER.match(text) and _has_content(text):
        return True                       # "What is the Coriolis acceleration?"
    return None                           # the local model decides from context


USE_CLASSIFIER = os.getenv("ARYA_ADDRESSEE_CLASSIFIER", "1") != "0"


async def classifier_probability(text: str, last_reply: str, seconds_since_reply: float | None) -> float | None:
    """P(said to ARYA) from the learned classifier, or None when there is none (not fitted yet / turned off)."""
    if not USE_CLASSIFIER:
        return None
    try:
        import asyncio

        from arya.research.addressee_model import featurize, load_model, probability
        m = load_model()
        if m is None:
            return None
        x = await asyncio.to_thread(featurize, text, last_reply, seconds_since_reply)
        return probability(x, m)
    except Exception as exc:
        print(f"Addressee classifier skipped ({type(exc).__name__}: {exc})")
        return None


async def is_addressed(text: str, *, last_reply: str = "", seconds_since_reply: float | None = None,
                       recent: list[dict] | None = None, pending_confirmation: bool = False,
                       asked_question: bool = False, music_playing: bool = False) -> tuple[bool, str]:
    """(addressed?, reason). If the model can't decide: answers in cloud mode, stays quiet offline.
    pending_confirmation: ARYA waits for a yes/no, a song, a time — any answer is for it.
    asked_question: ARYA's last reply asked something real a moment ago — the model is told, but still
    judges (from the log: every reply ended in a question, and everyone's chatter in the next 15 s was answered)."""
    verdict = quick_verdict(text, pending_confirmation)
    if verdict is not None:
        return verdict, "rule"
    if asked_question and _BARE_ANSWER.match(text):
        return True, "answer to its question"
    if music_playing:
        # Songs have vocals: from the log (2026-11-21), lyrics like "Now we'll talk." were judged ARYA and
        # answered — and even learned from. Commands, real questions and "NJ, …" are settled by the rules above.
        return False, "music playing, unclear sentence ignored"

    # The learned classifier (arya/research/addressee_model.py, fitted on your labelled follow-ups): sure either
    # way -> decided here in a few ms; only the uncertain middle goes to the local model. Measured leave-one-day-
    # out on 379 real follow-ups: side talk answered 20.9% -> 11.4%, requests ignored 23.1% -> 15.8%,
    # model calls 233 -> 70.
    p = await classifier_probability(text, last_reply, seconds_since_reply)
    if p is not None:
        from arya.research.addressee_model import load_model
        m = load_model()
        if p >= m.get("hi", 0.7):
            return True, f"classifier p={p:.2f}"
        if p < m.get("lo", 0.4):
            return False, f"classifier p={p:.2f}"

    from arya.core.llm_local import LocalLLMUnavailable, chat_local

    lines = []
    for item in (recent or [])[-4:]:
        who = "User" if item.get("role") == "user" else "ARYA"
        lines.append(f"{who}: {str(item.get('content', ''))[:220]}")
    when = (f"{seconds_since_reply:.0f} seconds ago" if seconds_since_reply is not None else "a while ago")
    context = (
        ("Recent conversation:\n" + "\n".join(lines) + "\n\n" if lines else "")
        + f"ARYA last spoke {when}" + (f": \"{last_reply[:300]}\"" if last_reply else "") + "\n"
        + ("ARYA just asked the user a question; a sentence that answers it is for ARYA.\n" if asked_question else "")
        + "\n"
        + f"New sentence picked up by the microphone: \"{text}\"\n\nARYA or OTHER?"
    )
    started = time.monotonic()
    try:
        reply = await chat_local(
            [{"role": "system", "content": _PROMPT}, {"role": "user", "content": context}],
            temperature=0, max_tokens=3,
        )
    except LocalLLMUnavailable as exc:
        # Offline, the same model would have to answer, so answering an unclear sentence only
        # produces an error message. In cloud mode Groq can still answer: fail open there.
        try:
            from arya.core.llm import ARYA_MODE
        except Exception:
            ARYA_MODE = "offline"
        if ARYA_MODE == "offline":
            return False, f"model unavailable, unclear sentence ignored ({str(exc)[:80]})"
        return True, f"model unavailable ({exc})"
    answer = (reply.get("content") or "").strip().upper()
    took = time.monotonic() - started
    if answer.startswith("OTHER"):
        return False, f"model ({took:.1f}s)"
    return True, f"model ({took:.1f}s)"
