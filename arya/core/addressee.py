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
    r"that'?s (?:all|it)|yes|yeah|yep|no|nope|repeat that|say (?:that|it) again|what\??)\W*$",
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
_ASSISTANT_REQUEST = re.compile(
    r"^\W*(?:(?:can|could|would|will) you|please|what(?:'s| is| are)|how (?:much|many|long|far|do|does|is)|"
    r"when (?:is|does)|where is|who (?:is|was)|set|start|stop|open|close|play|pause|skip|turn|remind|"
    r"calculate|convert|search|find|look up|tell me|show me|read|list|delete|move|rename|explain|define)\b",
    re.IGNORECASE,
)

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


def quick_verdict(text: str, pending_confirmation: bool = False) -> bool | None:
    """True/False when a rule settles it, None when the model should decide."""
    if pending_confirmation:
        return True                       # ARYA asked a yes/no question — the answer is for it
    if _NAME.search(text):
        return True
    if _ALWAYS_FOR_ARYA.match(text.strip()):
        return True
    try:
        from arya.core.llm import quick_media_command
        if quick_media_command(text) is not None:
            return True
    except Exception:
        pass
    if _OTHER_PERSON.match(text):
        return False                      # opens by addressing someone else
    if _ASSISTANT_REQUEST.match(text):
        return True                       # a command/question of the kind people ask an assistant
    return None


async def is_addressed(text: str, *, last_reply: str = "", seconds_since_reply: float | None = None,
                       recent: list[dict] | None = None, pending_confirmation: bool = False) -> tuple[bool, str]:
    """(addressed?, reason). Fails open: if the model can't decide, ARYA answers."""
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
        return True, f"model unavailable ({exc})"
    answer = (reply.get("content") or "").strip().upper()
    took = time.monotonic() - started
    if answer.startswith("OTHER"):
        return False, f"model ({took:.1f}s)"
    return True, f"model ({took:.1f}s)"
