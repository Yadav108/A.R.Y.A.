"""
Two guards, both local and rule-based (ideas from Stanford's OpenJarvis, security/injection_scanner.py and
security/boundary.py, adapted to ARYA):

1. Injected instructions in text that isn't yours — files you upload, emails, web pages, tool results, text
   read off the screen or the webcam. A line that talks to an AI ("ignore previous instructions", fake
   chat-template tokens, "don't tell the user", a download piped into a shell) is cut out before the model sees
   it, and ARYA's learning never learns from it. Your own requests are never scanned.

2. Personal data leaving the laptop. Before anything goes to the cloud model (only ever after you said yes),
   emails, phone numbers, IBANs, card numbers, API keys / passwords and your Windows user name are replaced by
   placeholders like [email 1]; the cloud's answer gets the real values back on the laptop.
"""
from __future__ import annotations

import getpass
import re
from dataclasses import dataclass

# ── 1. Injected instructions ──────────────────────────────────────────────────

_AI = r"(?:assistant|ai|arya|chatbot|bot|language\s+model|llm|model|agent|gpt|claude|qwen)"
_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("critical", "chat template", re.compile(
        r"<\|(?:im_start|im_end|system|user|assistant|begin_of_text|start_header_id|end_header_id|eot_id)\|>|"
        r"\[/?INST\]|<</?SYS>>", re.IGNORECASE)),
    ("high", "override", re.compile(
        r"\b(?:ignore|disregard|forget|override|bypass|skip)\s+(?:all\s+|any\s+|every\s+)?(?:of\s+)?(?:the\s+|your\s+|my\s+|these\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|original|initial|safety)\s+"
        r"(?:instructions?|prompts?|rules|directions|guidelines|messages|context)\b", re.IGNORECASE)),
    ("high", "new instructions", re.compile(
        r"\b(?:new|updated|real|actual)\s+(?:system\s+)?instructions?\s*:|^\s*#{2,}\s*(?:system|instructions?)\s*:?\s*$|"
        r"^\s*system\s*(?:prompt|message)?\s*:\s*you\b", re.IGNORECASE | re.MULTILINE)),
    ("high", "jailbreak", re.compile(
        r"\b(?:DAN\s+mode|do\s+anything\s+now|developer\s+mode\s+(?:enabled|on)|jailbreak(?:ed)?\b|"
        r"you\s+are\s+now\s+(?:an?\s+)?(?:unrestricted|unfiltered|evil|jailbroken))", re.IGNORECASE)),
    ("high", "secrecy", re.compile(
        r"\b(?:do\s+not|don'?t|never)\s+(?:tell|inform|alert|mention\s+(?:this\s+)?to|let)\s+the\s+user\b",
        re.IGNORECASE)),
    ("high", "prompt leak", re.compile(
        r"\b(?:reveal|print|show|repeat|output|leak)\s+(?:me\s+)?(?:your|the)\s+(?:full\s+)?"
        r"(?:system\s+prompt|hidden\s+(?:prompt|instructions)|initial\s+instructions)\b", re.IGNORECASE)),
    # Install guides legitimately say "curl … | sh"; ARYA has no shell tool, so this is only noted, never cut.
    ("medium", "shell", re.compile(
        r"\b(?:curl|wget|iwr|invoke-webrequest)\b[^\n|]{0,200}\|\s*(?:ba|z)?sh\b|\biex\s*\(|"
        r"\binvoke-expression\b|powershell(?:\.exe)?\s+-(?:e|enc|encodedcommand)\s+\S{16,}", re.IGNORECASE)),
    ("high", "addressed to the AI", re.compile(
        rf"\b(?:dear\s+|hey\s+|attention\s*:?\s*)?{_AI}\s*[,:]?\s+(?:you\s+(?:must|should|will)|please|now)\s+"
        r"(?:ignore|delete|send|forward|open|run|execute|email|transfer|buy|download|install|reveal)\b",
        re.IGNORECASE)),
    ("medium", "role play", re.compile(
        rf"\b(?:pretend\s+(?:to\s+be|you\s+are)|from\s+now\s+on\s+you\s+are|act\s+as\s+(?:an?\s+)?{_AI})\b",
        re.IGNORECASE)),
]
_SEVERE = {"critical", "high"}


@dataclass
class Hit:
    severity: str
    kind: str
    snippet: str


def scan(text: str) -> list[Hit]:
    hits = []
    for severity, kind, pattern in _PATTERNS:
        for m in pattern.finditer(text or ""):
            hits.append(Hit(severity, kind, m.group(0)[:80]))
    return hits


def severe(text: str) -> bool:
    return any(h.severity in _SEVERE for h in scan(text))


REMOVED = "[removed by ARYA: a line that gave instructions to an AI]"


def clean(text: str, source: str) -> str:
    """Text from outside (a file, an email, a web page, a tool result, the screen) with every line that tries to
    instruct an AI cut out. Logged (what kind, from where — never the text)."""
    if not text or not any(p.search(text) for sev, _k, p in _PATTERNS if sev in _SEVERE):
        return text
    out, kinds = [], []
    for line in text.split("\n"):
        hits = [h for h in scan(line) if h.severity in _SEVERE]
        if hits:
            kinds += [h.kind for h in hits]
            out.append(REMOVED)
        else:
            out.append(line)
    cleaned = "\n".join(out)
    for _sev, kind, pattern in _PATTERNS:              # a template token mid-line, or across lines
        if _sev == "critical" and pattern.search(cleaned):
            cleaned = pattern.sub("", cleaned)
            kinds.append(kind)
    if kinds:
        print(f"Guard: removed {len(kinds)} injected instruction(s) from {source} ({', '.join(sorted(set(kinds)))})")
        try:
            from arya.core.audit import log_event
            log_event("injection_removed", source=source, kinds=",".join(sorted(set(kinds))))
        except Exception:
            pass
    return cleaned


# ── 2. Personal data before the cloud ─────────────────────────────────────────

def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:sk|pk|rk)-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|\bgsk_[A-Za-z0-9]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{30,}|\bAKIA[0-9A-Z]{16}\b|\bxox[abpr]-[A-Za-z0-9-]{10,}|\bAIza[0-9A-Za-z_-]{35}\b|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
# "my password is hunter22", "PIN: 4821", "api key = Xy9…" — but not code ("token = strTok.nextToken",
# password = "password"): the value has no dots, brackets or quotes and isn't one of the key words itself.
_PASSWORD = re.compile(r"\b(?P<k>password|passwort|passwd|pwd|pin|passcode|api[\s_-]?key|secret|token)"
                       # the value has a digit or symbol: "pin is connected" / "token is valid" are plain English
                       r"(?P<sep>\s*(?:\bis\b|:|=)\s*)(?P<v>(?!(?:password|passwort|secret|token|none|null)\b)"
                       r"(?=[^\s,;]*[\d!@#$%^&*_+\-])"
                       r"[^\s,;.()\[\]{}\"'`]{4,})(?=[\s,;!?]|\.(?:\s|$)|$)", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,4})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_PHONE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s.-]?\(?\d{1,5}\)?(?:[\s.-]?\d{2,5}){2,4}|"
                    r"\(?0\d{2,5}\)?[\s/.-]\d{3,8}(?:[\s.-]\d{2,6})?)(?!\w)")
_PHONE_CONTEXT = re.compile(r"\b(?:phone|mobile|cell|number|call|whatsapp|tel)\b[^\n\d]{0,20}(?P<n>\d{10,12})\b",
                            re.IGNORECASE)


def _phone_like(s: str) -> bool:
    """'+49 2461 61-0' / '0176 1234567' yes; '0001 0100 1000' (binary) and '0956-7135' (too short) no."""
    digits = re.sub(r"\D", "", s)
    if set(digits) <= {"0", "1"}:
        return False
    return len(digits) >= (8 if s.lstrip().startswith("+") else 9)


def _user_names() -> list[str]:
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    return [n for n in names if n and len(n) >= 3]


class Redaction:
    """Placeholders for one cloud request, so the reply can get the real values back."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self._by_value: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        if value in self._by_value:
            return self._by_value[value]
        self.counts[kind] = self.counts.get(kind, 0) + 1
        ph = f"[{kind} {self.counts[kind]}]"
        self.values[ph] = value
        self._by_value[value] = ph
        return ph

    def text(self, text: str) -> str:
        if not text:
            return text
        t = _SECRET.sub(lambda m: self._placeholder("secret", m.group(0)), text)
        t = _PASSWORD.sub(lambda m: m.group("k") + m.group("sep") + self._placeholder("secret", m.group("v")), t)
        t = _EMAIL.sub(lambda m: self._placeholder("email", m.group(0)), t)
        t = _IBAN.sub(lambda m: self._placeholder("bank account", m.group(0)), t)
        t = _CARD.sub(lambda m: self._placeholder("card", m.group(0))
                      if _luhn(re.sub(r"\D", "", m.group(0))) else m.group(0), t)
        t = _PHONE.sub(lambda m: self._placeholder("phone", m.group(0)) if _phone_like(m.group(0)) else m.group(0), t)
        t = _PHONE_CONTEXT.sub(lambda m: m.group(0).replace(m.group("n"), self._placeholder("phone", m.group("n"))), t)
        for name in _user_names():                     # C:\Users\<you>\… — your account name
            t = re.sub(rf"(?i)(\\Users\\|/Users/|/home/){re.escape(name)}\b", r"\1[user]", t)
        return t

    def messages(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            m = dict(m)
            if isinstance(m.get("content"), str):
                m["content"] = self.text(m["content"])
            elif isinstance(m.get("content"), list):            # multimodal: redact the text parts
                m["content"] = [dict(p, text=self.text(p["text"])) if isinstance(p, dict) and p.get("type") == "text"
                                else p for p in m["content"]]
            out.append(m)
        if self.counts:
            what = ", ".join(f"{n} {k}{'s' * (n != 1)}" for k, n in self.counts.items())
            print(f"Guard: redacted {what} before the cloud")
            try:
                from arya.core.audit import log_event
                log_event("cloud_redacted", **{k.replace(" ", "_"): n for k, n in self.counts.items()})
            except Exception:
                pass
        return out

    def restore(self, text: str) -> str:
        for ph, value in self.values.items():
            text = text.replace(ph, value)
        return text

    def restorer(self):
        """For a stream: holds back a possible half-placeholder ("[email") until it's complete."""
        buf = {"t": ""}

        def feed(token: str | None) -> str:
            if token is None:                        # end of stream
                out, buf["t"] = buf["t"], ""
                return self.restore(out)
            buf["t"] += token
            t = buf["t"]
            cut = t.rfind("[")
            if cut != -1 and "]" not in t[cut:] and len(t) - cut < 24:
                out, buf["t"] = t[:cut], t[cut:]
            else:
                out, buf["t"] = t, ""
            return self.restore(out)
        return feed
