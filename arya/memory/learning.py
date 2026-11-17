"""
Learning from every conversation (roadmap PR 3) — on this laptop only, never sent to the cloud.

After a conversation ends (15 minutes without a new message) and while ARYA is idle, the local model reads
what *you* said and proposes memories: preferences, people, projects, routines, facts about you that will
still be true next month. Precision matters more than recall — a wrong memory is worse than a missing one —
so every proposal has to pass checks in code before it is kept:

  * evidence: it must quote your own words, and the quote must really be in one of your messages
    (the model can't learn something ARYA said, or something nobody said)
  * not about a passing state ("I'm tired", "today"), not sensitive (health, money, religion, politics,
    sexuality, passwords) — those are only kept when you say "remember that …" yourself
  * confidence: confident ones are kept at once; the rest stay "candidates" until a later conversation
    says the same thing again (two independent mentions)
  * duplicates merge by meaning (MiniLM embedding similarity ≥ 0.86); the newer wording wins, so
    "my lab partner is now Ben" replaces "… is Anna"

Each conversation also gets a one-sentence summary (an "episode"), so ARYA can pick threads up again:
"what did we talk about yesterday?".

Recall: before each answer the memories most related to your request (embedding similarity, plus how
sure and how recent) go into the local model's instructions — a few lines, never recited back.

Voice: "what have you learned about me?", "forget that I …", "don't remember this conversation",
"pause learning" / "resume learning", "what did we talk about yesterday?".
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

CONVERSATION_GAP_S = 15 * 60          # a conversation ends after 15 quiet minutes
CHECK_EVERY_S = 5 * 60
KEEP_AT = 0.75                        # model confidence to keep a memory at once
CANDIDATE_AT = 0.5                    # below this: dropped
SAME_AT = 0.86                        # embedding similarity: the same memory
RECALL_K = 5
RECALL_MIN = 0.25                     # similarity to the request for a fact to be recalled
STANDING_PREFS = 3                    # preferences always recalled (MiniLM misses "dinner" ~ "vegetarian": 0.16)
MAX_PROMPT_CHARS = 600
MAX_ACTIVE = 200
MAX_PER_CONVERSATION = 3

KINDS = ("preference", "person", "project", "routine", "fact")

# Never learned by inference (only kept when the user explicitly says "remember that …").
_SENSITIVE = re.compile(
    r"\b(?:health|ill(?:ness)?|sick|disease|diagnos\w*|medic\w*|therap\w*|depress\w*|anxiety|pregnan\w*|"
    r"tooth|teeth|toothache|dentist|doctor|hospital|pain|hurt\w*|injur\w*|surgery|allerg\w*|symptom\w*|"
    r"religio\w*|church|mosque|temple|pray\w*|politic\w*|vote[sd]?|party member|sexual\w*|gay|lesbian|"
    r"bisexual|salary|income|debt|loan|bank|credit card|money|password|pin\b|passport|social security|"
    r"address is|phone number)\b", re.IGNORECASE)
# Passing states and one-off requests — not things to remember about someone.
_TRANSIENT = re.compile(
    r"\b(?:today|tonight|right now|at the moment|this (?:morning|evening|afternoon|week|weekend)|currently feeling|"
    r"tired|bored|hungry|sleepy|stressed today|in a hurry|just now|asked (?:about|for|to)|wants? to know|"
    r"set a timer|play(?:ed|ing)? (?:a |the )?song|"
    # one-off requests and plans with a date (from the learning benchmark: "is looking for vegetarian recipes
    # using chickpeas", "plans to visit Würzburg next month" — the sister was visiting)
    r"is looking for|looking for|searching for|wants? (?:a|some|ideas)|next (?:week|month|year)|tomorrow|"
    r"upcoming|plans? to|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday) (?:morning|evening)?$|"
    # change stories: the new state is what to remember, not that something changed
    r"no longer|switched|used to|anymore)\b", re.IGNORECASE)

SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "text": {"type": "string"},
                    "evidence": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "text", "evidence", "confidence"],
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["memories", "summary"],
}

PROMPT = """You read a conversation between a user and their assistant ARYA and pick out long-term memories \
about the USER: their preferences (also how they want ARYA to talk), the people in their life, their projects, \
routines, and facts about them that will still be true in a month.

Rules:
- Only what the USER said about themselves. Never use what ARYA said.
- Asking about a topic is NOT a fact about the user ("what is the Coriolis effect" teaches you nothing about them).
- No one-off requests (timers, songs, searches), moods or passing states (tired, bored, busy today).
- Nothing about health, religion, politics, sexuality, money, passwords or addresses.
- Diet, units, language and how they want answers are "preference".
- Write each memory as one short, simple sentence that starts with "The user". For people use exactly
  "The user's <relation> is <name>." When something changed, write only how it is now.
- At most three memories, the most important first. Never two memories about the same thing.
- "evidence" must be the user's own words, copied exactly from one of their lines.
- "confidence": 0.9 when they stated it plainly about themselves, 0.6 when it is implied.
- Most conversations contain nothing worth remembering: then return an empty list.
- "summary": one short sentence saying what the conversation was about.

Example:
USER: I'm working on a line-following robot for my project course, can you remind me how PID works?
ARYA: A PID controller combines proportional, integral and derivative terms...
USER: thanks, and keep it shorter next time, I prefer quick answers
Answer: {"memories": [{"kind": "project", "text": "The user is building a line-following robot for a project \
course.", "evidence": "I'm working on a line-following robot for my project course", "confidence": 0.9}, \
{"kind": "preference", "text": "The user prefers short, quick answers.", "evidence": "I prefer quick answers", \
"confidence": 0.9}], "summary": "PID control for the user's line-following robot."}"""


# ── Storage ───────────────────────────────────────────────────────────────────

DB_OVERRIDE = None                    # tests and the learning eval: a scratch database


def _db() -> sqlite3.Connection:
    from arya.memory.database import DB_PATH
    c = sqlite3.connect(DB_OVERRIDE or DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, evidence TEXT,
            confidence REAL NOT NULL, status TEXT NOT NULL,           -- active / candidate
            seen INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, last_seen REAL NOT NULL,
            conversation TEXT, embedding BLOB);
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY, first_msg INTEGER, last_msg INTEGER, started REAL, ended REAL,
            mode TEXT, summary TEXT NOT NULL, embedding BLOB);
        CREATE TABLE IF NOT EXISTS learning_state (key TEXT PRIMARY KEY, value TEXT);
    """)
    return c


def _get(key: str, default: str = "") -> str:
    with _db() as c:
        r = c.execute("SELECT value FROM learning_state WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _set(key: str, value: str) -> None:
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO learning_state (key, value) VALUES (?, ?)", (key, value))


def paused() -> bool:
    return _get("paused") == "1"


def _vec(text: str) -> np.ndarray:
    from arya.core.embedder import embed_sync
    return np.asarray(embed_sync(text), dtype=np.float32)


def memories(status: str | None = "active") -> list[sqlite3.Row]:
    with _db() as c:
        if status is None:
            return c.execute("SELECT * FROM memories ORDER BY last_seen DESC").fetchall()
        return c.execute("SELECT * FROM memories WHERE status = ? ORDER BY last_seen DESC", (status,)).fetchall()


# ── Conversations ─────────────────────────────────────────────────────────────

@dataclass
class Conversation:
    first: int
    last: int
    ended: float                       # unix time of the last message
    lines: list[tuple[str, str]]       # (role, content)

    @property
    def key(self) -> str:
        return f"{self.first}-{self.last}"


def _parse_ts(ts: str) -> float:
    """messages.timestamp is SQLite CURRENT_TIMESTAMP (UTC, 'YYYY-MM-DD HH:MM:SS')."""
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def finished_conversations(after_id: int, now: float | None = None) -> list[Conversation]:
    """Conversations made of messages after `after_id` that have ended (15 quiet minutes since)."""
    from arya.memory.database import DB_PATH
    now = now or time.time()
    with sqlite3.connect(DB_OVERRIDE or DB_PATH) as c:
        rows = c.execute("SELECT id, role, content, timestamp FROM messages WHERE id > ? ORDER BY id",
                         (after_id,)).fetchall()
    convs: list[Conversation] = []
    for mid, role, content, ts in rows:
        t = _parse_ts(ts)
        if convs and t - convs[-1].ended <= CONVERSATION_GAP_S:
            convs[-1].last, convs[-1].ended = mid, t
            convs[-1].lines.append((role, content))
        else:
            convs.append(Conversation(mid, mid, t, [(role, content)]))
    return [cv for cv in convs if now - cv.ended > CONVERSATION_GAP_S]


def _worth_reading(cv: Conversation) -> bool:
    said = " ".join(t for r, t in cv.lines if r == "user")
    return len(re.findall(r"[A-Za-z]{2,}", said)) >= 8


def transcript(cv: Conversation) -> str:
    out = []
    for role, text in cv.lines[-40:]:
        if role == "user":
            out.append("USER: " + text.strip()[:400])
        else:
            out.append("ARYA: " + text.strip()[:160])
    return "\n".join(out)


# ── Checks (the precision part) ───────────────────────────────────────────────

def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower().replace("’", "'"))


def evidence_ok(evidence: str, user_lines: list[str]) -> bool:
    """The quote really is the user's: ≥ 80 % of its words, in order-insensitive overlap, inside one of their lines."""
    ev = [w for w in _words(evidence) if len(w) > 1]
    if len(ev) < 2:
        return False
    for line in user_lines:
        have = set(_words(line))
        if sum(w in have for w in ev) / len(ev) >= 0.8:
            return True
    return False


def check(item: dict, user_lines: list[str]) -> tuple[bool, str]:
    """(keep?, why not). Code-side rules a proposal must pass, whatever the model said."""
    kind, text = str(item.get("kind", "")), str(item.get("text", "")).strip()
    evidence = str(item.get("evidence", "")).strip()
    try:
        conf = float(item.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if kind not in KINDS:
        return False, "unknown kind"
    if not re.match(r"^the user\b", text, re.IGNORECASE) or len(text) < 15 or len(text) > 200:
        return False, "not a sentence about the user"
    if conf < CANDIDATE_AT:
        return False, "not confident"
    if _SENSITIVE.search(text) or _SENSITIVE.search(evidence):
        return False, "sensitive"
    if _TRANSIENT.search(text):
        return False, "passing state or one-off request"
    from arya.core import guard
    hits = guard.scan(f"{text}\n{evidence}")
    if any(h.severity in ("critical", "high") for h in hits):
        return False, "injected instructions"
    if hits:                                       # "pretend you are …": kept for the record, never recalled
        item["untrusted"] = True
    if not evidence_ok(evidence, user_lines):
        return False, "evidence not in the user's words"
    # "she loves hiking" (the sister) must not become "The user enjoys hiking" (from the learning benchmark)
    if re.search(r"\b(?:she|he|they|her|his|their)\b", evidence, re.IGNORECASE) and not _names_someone(text):
        return False, "about someone else"
    return True, ""


_RELATION = re.compile(r"\b(?:sister|brother|mother|father|mom|mum|dad|parents?|friend|partner|wife|husband|"
                       r"girlfriend|boyfriend|colleague|supervisor|professor|boss|roommate|flatmate|son|daughter|"
                       r"cousin|aunt|uncle|grand\w+)\b", re.IGNORECASE)


def _names_someone(text: str) -> bool:
    """Does the memory name another person — a relation, or a capitalised name after the first word?"""
    return bool(_RELATION.search(text) or re.search(r"(?<!^)\b[A-Z][a-z]{2,}\b", text.strip()))


# ── Storing ───────────────────────────────────────────────────────────────────

_SLOT = re.compile(r"^the user's (?P<s>[a-z][a-z' -]{2,40}?) (?:is|are|was|were)\b|"
                   r"^the user has an? (?P<s2>[a-z][a-z' -]{2,40}?) (?:named|called)\b", re.IGNORECASE)


def _slot(text: str) -> str | None:
    """'The user's lab partner is Ben.' / 'The user has a lab partner named Ben.' -> "lab partner": the same
    slot with a new value is an update. (Embeddings can't tell: 'partner is Anna' vs 'partner is Ben' = 0.73,
    'likes jazz' vs 'likes rock' = 0.65.)"""
    m = _SLOT.match(text.strip())
    if not m:
        return None
    return (m.group("s") or m.group("s2")).lower().removeprefix("new ").removeprefix("current ")


def _value_name(text: str) -> str | None:
    """'The user's lab partner is Anna.' -> 'Anna' (a capitalised name in the slot's value)."""
    m = re.search(r"\b(?:is|are|was|were|named|called)\s+(?:now\s+)?([A-Z][a-z]{2,})\b", text)
    return m.group(1) if m else None


def store(item: dict, conversation: str, now: float | None = None) -> str:
    """'new' / 'reinforced' / 'updated' / 'duplicate of explicit' — after check() passed."""
    now = now or time.time()
    text = str(item["text"]).strip().rstrip(".") + "."
    conf = float(item["confidence"])
    v = _vec(text)
    if item.get("untrusted"):                      # quarantined: never merged into, never recalled
        with _db() as c:
            c.execute("INSERT INTO memories (kind, text, evidence, confidence, status, seen, created, last_seen, "
                      "conversation, embedding) VALUES (?, ?, ?, ?, 'untrusted', 1, ?, ?, ?, ?)",
                      (item["kind"], text, item.get("evidence"), conf, now, now, conversation, v.tobytes()))
        print(f"Learning: quarantined {text!r} (looks like an instruction to an AI)")
        return "untrusted"
    try:                                          # already something you asked ARYA to remember
        from arya.memory import profile
        for r in profile.items():
            if float(_vec(r["text"]) @ v) >= SAME_AT:
                return "duplicate of explicit"
    except Exception:
        pass
    slot = _slot(text)
    with _db() as c:
        best, best_sim = None, 0.0
        for r in c.execute("SELECT * FROM memories WHERE embedding IS NOT NULL").fetchall():
            s = float(np.frombuffer(r["embedding"], np.float32) @ v)
            if slot and _slot(r["text"]) == slot:
                s = max(s, SAME_AT)                   # same slot ("lab partner"): the newer value replaces it
            if s > best_sim:
                best, best_sim = r, s
        if best is not None and best_sim >= SAME_AT:
            seen = best["seen"] + (1 if best["conversation"] != conversation else 0)
            status = "active" if (best["status"] == "active" or conf >= KEEP_AT or seen >= 2) else "candidate"
            changed = text.lower() != best["text"].lower()
            c.execute("UPDATE memories SET text = ?, evidence = ?, confidence = ?, status = ?, seen = ?, "
                      "last_seen = ?, conversation = ?, embedding = ? WHERE id = ?",
                      (text, item.get("evidence"), max(conf, best["confidence"]), status, seen, now, conversation,
                       v.tobytes(), best["id"]))
            if changed:
                print(f"Learning: updated {best['text']!r} -> {text!r}")
                old_name = _value_name(best["text"]) if slot and _slot(best["text"]) == slot else None
                if old_name and old_name not in text:
                    # "lab partner is Anna" -> "… is Ben": memories built on Anna are out of date too
                    stale = c.execute("SELECT id, text FROM memories WHERE id != ? AND text LIKE ?",
                                      (best["id"], f"%{old_name}%")).fetchall()
                    for r in stale:
                        c.execute("DELETE FROM memories WHERE id = ?", (r["id"],))
                        print(f"Learning: dropped {r['text']!r} (about {old_name}, who was replaced)")
            return "updated" if changed else "reinforced"
        status = "active" if conf >= KEEP_AT else "candidate"
        c.execute("INSERT INTO memories (kind, text, evidence, confidence, status, seen, created, last_seen, "
                  "conversation, embedding) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                  (item["kind"], text, item.get("evidence"), conf, status, now, now, conversation, v.tobytes()))
        n = c.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]
        if n > MAX_ACTIVE:                         # least recently confirmed go first
            c.execute("DELETE FROM memories WHERE id IN (SELECT id FROM memories WHERE status = 'active' "
                      "ORDER BY last_seen LIMIT ?)", (n - MAX_ACTIVE,))
    print(f"Learning: {'learned' if status == 'active' else 'noted (needs a second mention)'} {text!r}")
    return "new"


def save_episode(cv: Conversation, summary: str, mode: str) -> None:
    summary = summary.strip()
    if not summary or len(summary) > 300:
        return
    with _db() as c:
        c.execute("INSERT INTO episodes (first_msg, last_msg, started, ended, mode, summary, embedding) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (cv.first, cv.last, cv.ended, cv.ended, mode, summary, _vec(summary).tobytes()))


# ── The model step ────────────────────────────────────────────────────────────

async def propose(transcript_text: str) -> dict:
    """The local model's proposals for one conversation (never the cloud)."""
    from arya.core.llm_local import chat_local
    reply = await chat_local(
        [{"role": "system", "content": PROMPT},
         {"role": "user", "content": "Conversation:\n" + transcript_text + "\n\nAnswer with JSON only."}],
        temperature=0, max_tokens=400, fmt=SCHEMA)
    try:
        data = json.loads(reply.get("content") or "{}")
    except json.JSONDecodeError:
        return {"memories": [], "summary": ""}
    return data if isinstance(data, dict) else {"memories": [], "summary": ""}


async def learn_from(cv: Conversation, mode: str = "default") -> dict:
    """Read one finished conversation: returns counts, for the log and the eval."""
    stats = {"proposed": 0, "kept": 0, "rejected": {}}
    if not _worth_reading(cv):
        return stats
    from arya.core import guard
    if guard.severe("\n".join(t for _r, t in cv.lines)):    # pasted text giving an AI orders: learn nothing here
        stats["rejected"]["injected instructions"] = 1
        print("Learning: skipped a conversation that contains instructions aimed at an AI")
        await asyncio.to_thread(save_episode, cv, "", mode)
        return stats
    data = await propose(transcript(cv))
    user_lines = [t for r, t in cv.lines if r == "user"]
    def confidence(i: dict) -> float:
        try:
            return float(i.get("confidence", 0))
        except (TypeError, ValueError):
            return 0.0
    items = sorted((i for i in (data.get("memories", []) or []) if isinstance(i, dict)), key=confidence,
                   reverse=True)
    for n, item in enumerate(items):
        if n >= MAX_PER_CONVERSATION:
            stats["rejected"]["over the limit"] = stats["rejected"].get("over the limit", 0) + 1
            continue
        stats["proposed"] += 1
        ok, why = check(item, user_lines)
        if not ok:
            stats["rejected"][why] = stats["rejected"].get(why, 0) + 1
            print(f"Learning: rejected {str(item.get('text'))[:80]!r} ({why})")
            continue
        await asyncio.to_thread(store, item, cv.key)
        stats["kept"] += 1
    await asyncio.to_thread(save_episode, cv, str(data.get("summary", "")), mode)
    return stats


async def run(busy) -> None:
    """Started by the server: read finished conversations while ARYA is idle."""
    await asyncio.sleep(180)
    while True:
        try:
            if not paused() and not busy():
                after = int(_get("last_msg", "0") or 0)
                if after == 0:                                         # first run: start from now, not history
                    from arya.memory.database import DB_PATH
                    with sqlite3.connect(DB_OVERRIDE or DB_PATH) as c:
                        after = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
                    _set("last_msg", str(after))
                skipped = set(json.loads(_get("off_record", "[]") or "[]"))
                for cv in (await asyncio.to_thread(finished_conversations, after))[:1]:   # one per round
                    if busy():
                        break
                    from arya.core import modes
                    if not any(cv.first <= mid <= cv.last for mid in skipped):     # not "off the record"
                        stats = await learn_from(cv, modes.current_name())
                        if stats["proposed"]:
                            print(f"Learning: conversation {cv.key}: {stats['kept']} of {stats['proposed']} kept")
                    _set("last_msg", str(cv.last))
        except Exception as exc:
            print(f"Learning skipped: {type(exc).__name__}: {exc}")
        await asyncio.sleep(CHECK_EVERY_S)


# ── Recall ────────────────────────────────────────────────────────────────────

def recall(message: str, now: float | None = None) -> list[str]:
    """The learned memories that matter for this request, best first."""
    rows = memories("active")
    if not rows:
        return []
    now = now or time.time()
    v = _vec(message)
    standing, related = [], []
    for r in rows:
        if r["embedding"] is None:
            continue
        sim = float(np.frombuffer(r["embedding"], np.float32) @ v)
        age_days = (now - r["last_seen"]) / 86400
        score = 0.65 * sim + 0.2 * r["confidence"] + 0.15 * float(np.exp(-age_days / 60))
        if r["kind"] == "preference":
            standing.append((score, r["text"]))   # how they want ARYA to answer applies to every answer
        elif sim >= RECALL_MIN:
            related.append((score, r["text"]))    # other facts only when related (no "Priya" in a PID answer)
    standing.sort(reverse=True)
    related.sort(reverse=True)
    picked = [t for _, t in standing[:STANDING_PREFS]]
    picked += [t for _, t in related if t not in picked][:max(0, RECALL_K - len(picked))]
    return picked


def prompt_block(message: str) -> str:
    lines, size = [], 0
    for text in recall(message):
        if size + len(text) > MAX_PROMPT_CHARS:
            break
        lines.append(f"  - {text}")
        size += len(text)
    if not lines:
        return ""
    return ("\n\n### What you've learned about the user from earlier conversations (use it naturally when it "
            "helps; don't recite it, and the user's words now override it):\n" + "\n".join(lines))


# ── Voice ─────────────────────────────────────────────────────────────────────

_LEARNED = re.compile(r"\bwhat (?:have you|did you) learn(?:ed|t)? about me\b|\bwhat do you know about me\b|"
                      r"\bwhat do you remember about me\b", re.IGNORECASE)
_OFF_RECORD = re.compile(r"\b(?:don'?t|do not) (?:remember|learn from|save) (?:this|that|our) conversation\b|"
                         r"\bforget (?:this|that|our) conversation\b|\boff the record\b", re.IGNORECASE)
_PAUSE = re.compile(r"\b(?:pause|stop|turn off) (?:the )?learning\b|\bstop learning about me\b", re.IGNORECASE)
_RESUME = re.compile(r"\b(?:resume|restart|turn on|start) (?:the )?learning\b|\bstart learning about me again\b",
                     re.IGNORECASE)
_EPISODES = re.compile(r"\bwhat did we (?:talk|speak|chat) about(?P<when>\s+(?:yesterday|today|last time|"
                       r"this morning|last week|earlier))?\b", re.IGNORECASE)


def _to_you(text: str) -> str:
    from arya.memory.profile import _to_you as tu
    return tu(text.rstrip("."))


def forget(what: str) -> int:
    """'forget that I play guitar' — learned memories sharing most of those words go."""
    words = [w for w in re.findall(r"[a-zA-Z0-9äöüß]{3,}", what.lower())
             if w not in {"that", "the", "about", "you", "remember", "user", "this", "with", "learned"}]
    if re.fullmatch(r"(?:everything|all)(?:\s+(?:about me|you know about me|you learned|of it))?", what.strip(),
                    re.IGNORECASE):
        with _db() as c:
            return c.execute("DELETE FROM memories").rowcount
    gone = 0
    with _db() as c:
        for r in c.execute("SELECT id, text FROM memories").fetchall():
            t = r["text"].lower()
            if words and sum(w in t for w in words) >= max(1, (len(words) + 1) // 2):
                c.execute("DELETE FROM memories WHERE id = ?", (r["id"],))
                gone += 1
    return gone


def _current_conversation_start() -> int:
    from arya.memory.database import DB_PATH
    with sqlite3.connect(DB_OVERRIDE or DB_PATH) as c:
        rows = c.execute("SELECT id, timestamp FROM messages ORDER BY id DESC LIMIT 400").fetchall()
    start, prev = None, None
    for mid, ts in rows:
        t = _parse_ts(ts)
        if prev is not None and prev - t > CONVERSATION_GAP_S:
            break
        start, prev = mid, t
    return start or 0


def handle(message: str) -> str | None:
    if _OFF_RECORD.search(message):
        # The conversation is marked by its latest message; whatever it grows into, the conversation that
        # contains that message is skipped (conversations are split at 15 quiet minutes).
        from arya.memory.database import DB_PATH
        with sqlite3.connect(DB_OVERRIDE or DB_PATH) as c:
            last = c.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        skipped = json.loads(_get("off_record", "[]") or "[]")[-50:] + [last]
        _set("off_record", json.dumps(skipped))
        start = _current_conversation_start()
        with _db() as c:                           # anything already learned from it goes too
            c.execute("DELETE FROM memories WHERE conversation LIKE ?", (f"{start}-%",))
        return "Okay, I won't learn anything from this conversation."
    if _PAUSE.search(message):
        _set("paused", "1")
        return "Learning paused. I'll still remember what you explicitly ask me to; say \"resume learning\" to turn it back on."
    if _RESUME.search(message):
        _set("paused", "0")
        return "Learning is back on."
    m = _EPISODES.search(message)
    if m:
        when = (m.group("when") or "").strip().lower()
        now = datetime.now()
        start = {"yesterday": (now - timedelta(days=1)).replace(hour=0, minute=0),
                 "today": now.replace(hour=0, minute=0), "this morning": now.replace(hour=0, minute=0),
                 "last week": now - timedelta(days=7)}.get(when)
        end = now.replace(hour=0, minute=0) if when == "yesterday" else now
        with _db() as c:
            if start is not None:
                rows = c.execute("SELECT summary FROM episodes WHERE ended BETWEEN ? AND ? ORDER BY ended",
                                 (start.timestamp(), end.timestamp())).fetchall()
            else:
                rows = c.execute("SELECT summary FROM episodes ORDER BY ended DESC LIMIT 3").fetchall()[::-1]
        if not rows:
            return "I don't have a summary of that yet; I note what we talked about a while after each conversation."
        return "We talked about: " + " Then: ".join(r["summary"].rstrip(".") + "." for r in rows[-4:])
    if _LEARNED.search(message):
        from arya.memory import profile
        told = [profile._to_you(r["text"]) for r in profile.items()]
        learned = [_to_you(r["text"]) for r in memories("active")[:8]]
        if not told and not learned:
            return ("Nothing yet. I learn a little from each conversation, and you can tell me things directly, like "
                    "\"remember that I prefer short answers\".")
        parts = []
        if told:
            parts.append("You told me: " + "; ".join(told) + ".")
        if learned:
            parts.append("I've picked up: " + "; ".join(learned) + ". Say \"forget that …\" to remove any of it.")
        if paused():
            parts.append("Learning is paused right now.")
        return " ".join(parts)
    return None
