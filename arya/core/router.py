"""
Hard-question router: when the small local model is likely to get it wrong, ARYA
offers the cloud — and asks first.

    "Derive the step response of a second-order system"   -> derivation
    "A 90 kg cyclist with 150 W on a 5 % slope, η = 0.95,
     c_w·A = 0.4 m² … find the speed"                     -> multi-step problem
    "Solve exercise 1-13 step by step"                     -> worked solution

ARYA then says: "This one needs a careful derivation. Want me to ask the cloud? Only
your question and the course page go out — say yes or no." On "yes", only the question
and the course excerpt are sent (never voice, history or files); on "no", the local
model answers as usual. Rules only — no model call to decide.
"""
from __future__ import annotations

import os
import re

ENABLED = os.getenv("ARYA_CLOUD_OFFER", "1") != "0"

_DERIVE = re.compile(r"\b(?:derive|derivation|prove|proof|show that|show why|why exactly|from first principles|"
                     r"mathematically why)\b", re.IGNORECASE)
_SOLVE = re.compile(r"\b(?:solve|calculate|compute|determine|find|work out)\b", re.IGNORECASE)
_WORKED = re.compile(r"\b(?:step by step|full solution|complete solution|solve (?:the |this )?(?:whole )?"
                     r"(?:exercise|problem|task|question)|work through|walk me through)\b", re.IGNORECASE)
_GIVEN = re.compile(r"-?\d+(?:\.\d+)?\s*(?:%|°|[a-zA-Zµ²³/·]+)?")

REASONS = {
    "derivation": "This one needs a careful derivation.",
    "problem": "That's a multi-step problem with several given values.",
    "worked": "A full worked solution needs careful step-by-step maths.",
}


def hard_reason(message: str) -> str | None:
    """'derivation' / 'problem' / 'worked', or None for anything the local model handles well."""
    if not ENABLED:
        return None
    words = message.split()
    if len(words) < 4:
        return None
    if _DERIVE.search(message):
        return "derivation"
    givens = [g for g in _GIVEN.findall(message) if re.search(r"\d", g)]
    if _SOLVE.search(message) and len(givens) >= 3:
        return "problem"
    if _WORKED.search(message) and re.search(r"\b(?:exercise|problem|task|aufgabe)\b|\d+\s*[-–.]\s*\d+", message,
                                             re.IGNORECASE):
        return "worked"
    return None


def cloud_available() -> bool:
    return bool(os.getenv("GROQ_API_KEY"))


def offer_question(reason: str) -> str:
    return (f"{REASONS.get(reason, 'This one is tricky for me.')} Want me to ask the cloud? Only your question "
            f"and the course page go out. Say yes or no.")
