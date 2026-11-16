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

_NAME = re.compile(r"\b(?:arya|aria|ariya|aarya)\b", re.IGNORECASE)
_ALWAYS_FOR_ARYA = re.compile(
    r"^\W*(?:stop|cancel|never ?mind|be quiet|shut up|thanks?|thank you|cheers|bye|goodbye|"
    r"that'?s (?:all|it)|yes|yeah|yep|no|nope|repeat that|say (?:that|it) again|what\??|"
    r"go ahead|go on|continue|carry on|do it|tell me more|more please)\W*$",
    re.IGNORECASE,
)

# "Mom, …", "Sarah, did you…", "hey guys, …" — speech that opens by addressing someone else.
_OTHER_PERSON = re.compile(
    r"^\W*(?:hey|hi|oi|yo|ok|okay)?\W*(?:mom|mum|mommy|dad|daddy|papa|mama|honey|babe|baby|dear|darling|guys|"
    r"bro|dude|man|sis|buddy|everyone|(?!(?i:arya|aria|ok|okay|yes|yeah|yep|yup|no|nope|so|well|now|hey|hi|"
    r"and|but|then|wait|hmm|hm|um|uh|oh|ah|right|alright|sure|thanks|sorry|actually|also|cool|great|nice|fine|"
    r"perfect|good|look|listen|please|maybe|anyway|honestly|really|true|exactly|what|why|how|wow)\b)[A-Z][a-z]+)"
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
_LOOSE_NAME = r"(?:a+r+i?y+a+h?|aria+h?|ariya|aarya|ario|aryo|area|harya|arja|ayra)"
_NAME_ALONE = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|uh|um|so|yo)\W+)?{_LOOSE_NAME}\W*$", re.IGNORECASE)
_NAME_FIRST = re.compile(rf"^\W*(?:(?:hey|hi|ok|okay|uh|um|so|yo)\W+)?{_LOOSE_NAME}\s*[,.!?:;]\s*(?=\S)",
                         re.IGNORECASE)
# "ARYA, play …" often comes out as "I'll play …".
_MISHEARD_PLAY = re.compile(r"^\W*(?:i'll|i will|ill)\s+(?=play\b)", re.IGNORECASE)


def split_name(text: str) -> tuple[str, bool]:
    """('Area, play Kon Tujhe', …) -> ('play Kon Tujhe', True); ('Area.') -> ('', True); else (text, False).
    Used on follow-ups: a sentence that starts with ARYA's name is for ARYA, and the name isn't the request."""
    if _NAME_ALONE.match(text):
        return "", True
    m = _NAME_FIRST.match(text) or _MISHEARD_PLAY.match(text)
    if m:
        return text[m.end():].strip(), True
    return text, False


def quick_verdict(text: str, pending_confirmation: bool = False) -> bool | None:
    """True/False when a rule settles it, None when the model should decide."""
    if pending_confirmation:
        return True                       # ARYA asked a yes/no question — the answer is for it
    if _NAME.search(text):
        return True
    if _ALWAYS_FOR_ARYA.match(text.strip()):
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


async def is_addressed(text: str, *, last_reply: str = "", seconds_since_reply: float | None = None,
                       recent: list[dict] | None = None, pending_confirmation: bool = False) -> tuple[bool, str]:
    """(addressed?, reason). If the model can't decide: answers in cloud mode, stays quiet offline."""
    verdict = quick_verdict(text, pending_confirmation)
    if verdict is not None:
        return verdict, "rule"

    from arya.core.llm_local import LocalLLMUnavailable, chat_local

    lines = []
    for item in (recent or [])[-4:]:
        who = "User" if item.get("role") == "user" else "ARYA"
        lines.append(f"{who}: {str(item.get('content', ''))[:220]}")
    when = (f"{seconds_since_reply:.0f} seconds ago" if seconds_since_reply is not None else "a while ago")
    context = (
        ("Recent conversation:\n" + "\n".join(lines) + "\n\n" if lines else "")
        + f"ARYA last spoke {when}" + (f": \"{last_reply[:300]}\"" if last_reply else "") + "\n\n"
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
