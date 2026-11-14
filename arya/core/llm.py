import asyncio
import os
import json
import re
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

import groq
import httpx
from dotenv import load_dotenv
from groq import AsyncGroq

from arya.core.llm_local import OLLAMA_MODEL, LocalLLMUnavailable, chat_local, stream_local
from arya.memory.database import get_history, save_message, save_memory_vector, semantic_search


load_dotenv()

# Groq retired the Llama 3.x models ARYA originally used (model_not_found as of
# Nov 2026). Override in .env if Groq's lineup changes again — list what your
# key can use with:  python -c "import groq; print([m.id for m in groq.Groq().models.list().data])"
MODEL_NAME = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")             # tool selection
INTENT_MODEL_NAME = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")  # intent, chat, follow-ups


# ── Privacy mode ──────────────────────────────────────────────────────────────
# offline (default): everything runs on the local Ollama model. Groq is used only
#   for a request that explicitly asks for it ("ask the cloud, …"), and then only
#   that one question is sent — no history, memories, name or location.
# cloud: the old behaviour — Groq first, local model as fallback.
ARYA_MODE = os.getenv("ARYA_MODE", "offline").strip().lower()
if ARYA_MODE not in ("offline", "cloud"):
    ARYA_MODE = "offline"

# True while serving a request the user explicitly sent to the cloud.
_cloud_turn: ContextVar[bool] = ContextVar("_cloud_turn", default=False)

# "Ask the cloud, explain …" / "Use the cloud to …" / "… — ask the cloud."
_CLOUD_PREFIX = re.compile(
    r"^\W*(?:(?:hey|ok|okay)\W+)?(?:(?:arya|aria)\W+)?(?:please\W+)?"
    r"(?:ask|use|check with|try)\s+(?:the\s+)?(?:cloud|groq|online model|big model)\b"
    r"[\s,:;.\-]*(?:(?:and|to|about|for|whether|if)\s+)?",
    re.IGNORECASE,
)
_CLOUD_SUFFIX = re.compile(
    r"[\s,;.\-]*(?:please\s+)?(?:ask|use|check with)\s+(?:the\s+)?(?:cloud|groq|online model|big model)\W*$",
    re.IGNORECASE,
)


def split_cloud_request(message: str) -> tuple[bool, str]:
    """('Ask the cloud, explain entropy') -> (True, 'Explain entropy')."""
    for pattern in (_CLOUD_PREFIX, _CLOUD_SUFFIX):
        stripped = pattern.sub("", message, count=1).strip()
        if stripped != message.strip() and stripped:
            return True, stripped[0].upper() + stripped[1:]
    return False, message


def _groq_allowed() -> bool:
    return (ARYA_MODE == "cloud" or _cloud_turn.get()) and _breaker.allow()


def _groq_extra(model: str) -> dict:
    """Per-model request options. gpt-oss models are reasoning models: keep
    reasoning short for voice latency and hide it so only the answer comes back."""
    if "gpt-oss" in model:
        return {"reasoning_effort": "low", "include_reasoning": False}
    return {}

# Max tool calls ARYA may chain for one request ("check the weather and set a
# timer", "find my thesis notes and copy the first one").
MAX_TOOL_STEPS = 4

# Requests that probably need more than one tool ("weather and a timer",
# "find my notes then copy the first one").
_MULTI_PART = re.compile(r"\b(and|then|also|plus|after that|as well)\b|,", re.IGNORECASE)


def _system_prompt(with_tools: bool = False) -> str:
    """Built per request so ARYA always knows the current date and time.
    Tool guidance is only included when tools are actually offered — gpt-oss
    models try to call tools whenever the prompt mentions them."""
    now = datetime.now().strftime("%A, %d %B %Y, %H:%M")
    city = os.getenv("DEFAULT_CITY", "Berlin")
    user = os.getenv("USER_NAME", "").strip()
    who = f"You are talking with {user}, a Mechatronics engineering student. " if user else (
        "You are talking with a Mechatronics engineering student. "
    )
    return (
        "You are ARYA, a sharp, warm, hands-free voice assistant running on the user's Windows PC. "
        + who
        + f"Current local date and time: {now}. Home city: {city}.\n"
        "Your replies are spoken aloud by a text-to-speech voice, so:\n"
        "- Answer in one to three short sentences unless the user asks for detail.\n"
        "- Never use markdown, bullet points, headings, tables, emojis or code blocks.\n"
        "- Say numbers, units and times the way a person would say them.\n"
        "- Lead with the answer; skip filler like 'Sure!' or 'Great question'.\n"
        + (
            "Only call tools when the user asks for an action or live data. "
            "You may chain several tool calls to finish a multi-part request. "
            "Never call tools for greetings, casual conversation, or general knowledge."
            if with_tools else ""
        )
    )


class _ModelWantsTool(Exception):
    """The streaming chat model tried to call a tool — escalate to the tool pipeline."""


_client: AsyncGroq | None = None


class GroqNotConfigured(RuntimeError):
    """GROQ_API_KEY is missing — ARYA runs on the local model only."""


class LLMUnavailableError(RuntimeError):
    """Neither Groq nor the local Ollama model could serve the request."""


def _get_client() -> AsyncGroq:
    global _client
    if _client is not None:
        return _client

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise GroqNotConfigured(
            "Missing GROQ_API_KEY. Add it to your .env file to use Groq."
        )

    # No SDK retries: on a rate limit or outage we fail over to Ollama
    # immediately instead of sleeping through Groq's backoff.
    _client = AsyncGroq(api_key=api_key, max_retries=0, timeout=15.0)
    return _client


# ── Groq → Ollama failover (Phase 4) ──────────────────────────────────────────

# Errors that mean "Groq can't serve this right now" (as opposed to a bad request).
_FALLBACK_ERRORS = (
    groq.RateLimitError,
    groq.APIConnectionError,   # includes APITimeoutError
    groq.InternalServerError,
    httpx.TransportError,      # connection dropped mid-stream
    GroqNotConfigured,
)


class _CircuitBreaker:
    """After `threshold` consecutive Groq failures within `window` seconds,
    skip Groq entirely for `cooldown` seconds."""

    def __init__(self, threshold: int = 3, window: float = 60.0, cooldown: float = 300.0):
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self._failures: list[float] = []
        self._open_until = 0.0
        self.last_error = ""

    def allow(self) -> bool:
        return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        self._failures.clear()

    def record_failure(self, exc: BaseException) -> None:
        now = time.monotonic()
        self.last_error = f"{type(exc).__name__}: {exc}"
        self._failures = [t for t in self._failures if now - t < self.window]
        self._failures.append(now)
        print(f"LLM: Groq unavailable ({self.last_error[:120]}) — falling back to {OLLAMA_MODEL}")
        if len(self._failures) >= self.threshold:
            self._open_until = now + self.cooldown
            self._failures.clear()
            print(f"LLM: circuit open — using {OLLAMA_MODEL} for the next {self.cooldown:.0f}s")


_breaker = _CircuitBreaker()


def _unavailable(local_exc: LocalLLMUnavailable) -> LLMUnavailableError:
    groq_reason = _breaker.last_error or "circuit open"
    return LLMUnavailableError(
        f"Groq unavailable ({groq_reason}); local model unavailable ({local_exc})"
    )


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: str | dict


@dataclass
class _Completion:
    content: str
    tool_calls: list[_ToolCall] = field(default_factory=list)


async def _complete(
    model: str,
    messages: list[dict],
    *,
    tools: list | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> _Completion:
    """One chat completion: Groq if allowed and healthy, otherwise the local model."""
    if _groq_allowed():
        kwargs: dict = {"model": model, "messages": messages, **_groq_extra(model)}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto", parallel_tool_calls=False)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        try:
            try:
                response = await _get_client().chat.completions.create(**kwargs)
            except groq.APIConnectionError:
                # One quick retry for a transient blip (timeout / dropped
                # connection) before declaring Groq down. Rate limits and
                # server errors fall through to the local model immediately.
                response = await _get_client().chat.completions.create(**kwargs)
        except _FALLBACK_ERRORS as exc:
            _breaker.record_failure(exc)
        else:
            _breaker.record_success()
            msg = response.choices[0].message
            calls = [
                _ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                for tc in (msg.tool_calls or [])
            ]
            return _Completion(content=msg.content or "", tool_calls=calls)

    try:
        msg = await chat_local(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
    except LocalLLMUnavailable as exc:
        raise _unavailable(exc) from exc
    calls = [
        _ToolCall(
            id=f"call_{i}",
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments") or {},
        )
        for i, tc in enumerate(msg.get("tool_calls") or [])
    ]
    return _Completion(content=msg.get("content") or "", tool_calls=calls)


async def _stream_tokens(model: str, messages: list[dict]) -> AsyncIterator[str]:
    """Stream content tokens: Groq if healthy, otherwise the local Ollama model.
    Falls back only if Groq fails before producing any output."""
    if _groq_allowed():
        yielded = False
        try:
            request = dict(model=model, messages=messages, stream=True, **_groq_extra(model))
            try:
                stream = await _get_client().chat.completions.create(**request)
            except groq.APIConnectionError:
                stream = await _get_client().chat.completions.create(**request)  # one retry
            async for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                token = getattr(delta, "content", None) if delta else None
                if token:
                    yielded = True
                    yield token
            _breaker.record_success()
            return
        except _FALLBACK_ERRORS as exc:
            if yielded:
                raise
            _breaker.record_failure(exc)
        except groq.APIError as exc:
            if not yielded and "called a tool" in str(exc).lower():
                raise _ModelWantsTool() from exc
            raise

    try:
        async for token in stream_local(messages):
            yield token
    except LocalLLMUnavailable as exc:
        raise _unavailable(exc) from exc


# Offline intent heuristic — mirrors the categories in the needs_tool() prompt.
# Used when Groq is down so we don't spend a second local inference on YES/NO.
_TOOL_KEYWORDS = re.compile(
    r"\b("
    r"weather|temperature in|forecast|"
    r"open|launch|website|"
    r"search for (?:a |the )?file|find (?:a |the |my )?file|"
    r"run (?:the )?command|directory|"
    r"cpu|ram|memory usage|battery|"
    r"timers?|pomodoro|focus session|remind me|reminders?|time left|"
    r"pause|resume|play|skip|next (?:song|track)|previous (?:song|track)|volume|mute|unmute|louder|quieter|"
    r"clipboard|"
    r"calculate|percent|convert|"
    r"wikipedia|look up|"
    r"take a note|save (?:a |this )?note|my notes|search notes|note this|remember this|"
    r"explain|describe|simply|technically|for (?:the |an )?exam|"
    r"formula|equation|"
    r"quiz me|test me|flashcards?|questions on|"
    r"briefing|brief me"
    r")\b",
    re.IGNORECASE,
)


def _keyword_needs_tool(message: str) -> bool:
    return bool(_TOOL_KEYWORDS.search(message))


# ── Speech helpers ────────────────────────────────────────────────────────────

# Sentence end = punctuation followed by whitespace (so "3.14" never splits).
# Also splits "hour.Timer" — models sometimes drop the space between sentences.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$|[A-Z][a-z])|\n+")


def split_sentences(text: str) -> list[str]:
    """Split text into speakable sentences (keeps decimals like 3.14 intact)."""
    parts: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        chunk = text[start:match.end()]
        if chunk.strip():
            parts.append(chunk)
        start = match.end()
    if text[start:].strip():
        parts.append(text[start:])
    return parts


# ── Semantic memory helpers ───────────────────────────────────────────────────

_MAX_STORED_LEN = 500   # chars to embed (quality) — content stored up to 300 in DB


async def _store_embedding(kind: str, source_id: int, labelled_content: str) -> None:
    """Background task: embed labelled_content and persist vector. Never raises."""
    try:
        from arya.core.embedder import embed
        vec = await embed(labelled_content[:_MAX_STORED_LEN])
        save_memory_vector(kind, source_id, labelled_content, vec)
    except Exception as exc:
        print(f"Embedder: store failed (kind={kind}, id={source_id}): {exc}")


async def _get_semantic_context(message: str, exclude: set[str]) -> str:
    """
    Embed message, search memory, return a formatted memory block to append to
    SYSTEM_PROMPT — or an empty string when nothing relevant is found.
    """
    try:
        from arya.core.embedder import embed
        vec = await embed(message)
        hits = semantic_search(vec, top_k=3, exclude_content=exclude)
        if not hits:
            return ""
        lines = "\n".join(f"  - {h[:150]}" for h in hits)
        return f"\n\n### Relevant context from memory:\n{lines}"
    except Exception as exc:
        print(f"Semantic search skipped: {exc}")
        return ""


async def chat(message: str) -> str:
    response = await _complete(
        INTENT_MODEL_NAME,
        [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": message},
        ],
    )
    return response.content


_INTENT_PROMPT = (
    "You are a binary classifier. Reply with only YES or NO. No explanation.\n\n"
    "Reply YES for ANY of these:\n"
    "- Weather requests: 'weather in X', 'temperature in X'\n"
    "- App/URL opening: 'open X', 'launch X', 'go to X website'\n"
    "- File operations: 'search for file X', 'find file X'\n"
    "- Terminal commands: 'run command X', 'what is my directory'\n"
    "- System status: 'CPU usage', 'RAM usage', 'battery level'\n"
    "- Timers and reminders: 'set timer', 'remind me at 5 to X', 'remind me in 10 minutes', "
    "'how long is left on my timer', 'cancel the timer', 'pomodoro', 'focus session'\n"
    "- Media and volume: 'pause', 'play music', 'next song', 'skip', 'volume up', 'mute', 'turn it down'\n"
    "- Clipboard: 'what is in clipboard', 'copy X to clipboard'\n"
    "- Math/Calculator: 'calculate X', 'what is X percent of Y', 'convert X to Y'\n"
    "- Wikipedia: 'search wikipedia for X', 'look up X on wikipedia'\n"
    "- Notes: 'take a note', 'save note', 'read my notes', 'search notes'\n"
    "- Study explain: 'explain X', 'what is X simply', 'describe X technically', 'explain X for exam'\n"
    "- Study formula: 'formula for X', 'equation for X', 'give me formula'\n"
    "- Study quiz: 'quiz me', 'test me', 'flashcard', 'give me questions on X'\n"
    "- Daily briefing: 'morning briefing', 'daily briefing', 'brief me'\n\n"
    "Reply NO for:\n"
    "- Greetings: 'hello', 'how are you', 'good morning'\n"
    "- Date or time questions: 'what time is it', 'what day is today'\n"
    "- Casual chat: 'tell me a joke', 'what do you think about X'\n"
    "- Follow-up questions in conversation: 'can you elaborate', 'tell me more'\n"
    "- Opinion questions: 'which is better X or Y'"
)


async def needs_tool(message: str) -> bool:
    # Offline: the keyword check is instant and as accurate as asking the
    # local model (15/15 on the benchmark set) — no extra inference.
    if not _groq_allowed():
        return _keyword_needs_tool(message)
    try:
        response = await _get_client().chat.completions.create(
            model=INTENT_MODEL_NAME,
            messages=[
                {"role": "system", "content": _INTENT_PROMPT},
                {"role": "user", "content": message},
            ],
            temperature=0,
            # Room for a reasoning model's short hidden reasoning before "YES".
            max_tokens=64,
            **_groq_extra(INTENT_MODEL_NAME),
        )
    except _FALLBACK_ERRORS as exc:
        _breaker.record_failure(exc)
        return _keyword_needs_tool(message)
    _breaker.record_success()
    decision = (response.choices[0].message.content or "").strip().upper()
    return decision.startswith("YES")


async def get_daily_briefing() -> str:
    now_text = datetime.now().strftime("%A, %B %d %Y, %I:%M %p")

    default_city = os.getenv("DEFAULT_CITY", "Berlin")
    try:
        default_lat = float(os.getenv("DEFAULT_LAT", "52.52"))
        default_lon = float(os.getenv("DEFAULT_LON", "13.405"))
    except ValueError as exc:
        raise RuntimeError("Invalid DEFAULT_LAT or DEFAULT_LON in environment.") from exc

    from arya.tools.notes import get_notes
    from arya.tools.productivity import list_timers
    from arya.tools.system_info import get_system_info
    from arya.tools.weather import get_weather

    weather, notes, system_status, timers = await asyncio.gather(
        get_weather(default_city, default_lat, default_lon),
        get_notes(limit=5),
        get_system_info(),
        list_timers(),
    )

    prompt = (
        "Time and date:\n"
        f"{now_text}\n\n"
        "Weather:\n"
        f"{weather}\n\n"
        "Recent notes:\n"
        f"{notes}\n\n"
        "Active timers and reminders:\n"
        f"{timers}\n\n"
        "System status:\n"
        f"{system_status}\n"
    )

    response = await _complete(
        INTENT_MODEL_NAME,
        [
            {
                "role": "system",
                "content": (
                    "You are ARYA. Generate a concise spoken morning briefing "
                    "in a friendly professional tone. Cover: time, weather, "
                    "recent notes, pending reminders, and system status. "
                    "Keep it under 5 sentences. No markdown or lists."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    return response.content


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city name to fetch weather for",
                    },
                    "latitude": {
                        "type": "number",
                        "description": "Latitude of the city (optional — looked up from the name if omitted)",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Longitude of the city (optional — looked up from the name if omitted)",
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an application on Windows by name",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "The name of the application to open",
                    },
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL in the browser",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open in the default browser",
                    }, \
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "Search for a file by name on Windows",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The filename or partial filename to search for",
                    },
                    "search_path": {
                        "type": "string",
                        "description": "Root directory where search should start",
                        "default": "C:\\Users",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a read-only Windows diagnostic command and return its output. "
                "Only these commands are allowed: dir, echo, whoami, systeminfo, "
                "ipconfig, ping, tasklist, where, hostname, ver, date, time, type, "
                "tree, vol, netstat, driverquery. No shell operators (&, |, ;, <, >)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The Windows terminal command to run",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Get current CPU usage and RAM usage of the computer",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_battery",
            "description": "Get current battery percentage and charging status",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Start a countdown timer. Give the duration as minutes and/or seconds, "
                           "exactly as the user said it (10 minutes -> minutes=10).",
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {
                        "type": "number",
                        "description": "Minutes part of the duration",
                    },
                    "seconds": {
                        "type": "integer",
                        "description": "Seconds part of the duration",
                    },
                    "label": {
                        "type": "string",
                        "description": "Label or name for the timer",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pomodoro",
            "description": "Start a pomodoro study session with focus and break intervals",
            "parameters": {
                "type": "object",
                "properties": {
                    "sessions": {
                        "type": "integer",
                        "description": "Number of pomodoro sessions, default 1",
                    },
                    "label": {
                        "type": "string",
                        "description": "Topic or subject being studied",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Read the current content of the clipboard",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": "Write or copy text to the clipboard",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to copy to clipboard",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Calculate a math expression or unit conversion",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The math expression to evaluate",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wikipedia",
            "description": "Search Wikipedia and return a short summary of a topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The topic to search on Wikipedia",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_concept",
            "description": (
                "Explain any concept or topic for a Mechatronics engineering student. "
                "Use for: 'explain X', 'what is X', 'describe X', 'explain X simply', "
                "'explain X technically', 'explain X for exam'. "
                "Extract the explanation level from the user message and include it "
                "in the topic parameter like: 'PID controller simply' or "
                "'state space technically' or 'transfer function for exam'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The concept or topic to explain",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "formula_lookup",
            "description": (
                "Look up engineering or math formulas with variables, "
                "units and examples. Use for questions like 'what is the formula for X', "
                "'give me the equation for X'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The formula topic to look up",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flashcard_quiz",
            "description": (
                "Generate flashcard quiz questions on any study topic. "
                "Use for: 'quiz me on X', 'test me on X', 'give me N questions on X'. "
                "Include the number in the topic parameter like: '3 questions on control theory'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic to generate questions on",
                    },
                },
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Save a research note or study note. Extract the actual note content "
                "from phrases like 'take a note:', 'note this:', 'remember this:', "
                "'save this:'. The content parameter should contain only the note text, "
                "not the trigger phrase."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The note content to save",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Category tag like 'thesis', 'exam', 'idea', 'general'",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_notes",
            "description": "Retrieve saved research or study notes, optionally filtered by tag",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Filter notes by tag",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of notes to return, default 10",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes",
            "description": "Search through saved notes by keyword",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to search in notes",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_briefing",
            "description": "Get a morning briefing with current time, weather, recent notes and system status",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": (
                "Control music/video playback and system volume on the PC. "
                "Use for: 'pause', 'resume', 'play music', 'next song', 'skip', "
                "'previous track', 'volume up', 'turn it down', 'mute', 'unmute'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "play_pause", "next", "previous", "stop",
                            "volume_up", "volume_down", "mute",
                        ],
                        "description": "The media action. 'mute' toggles mute on/off.",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "For volume_up/volume_down: how many 2% steps, default 5 (10%).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Remind the user about something at a clock time or after a delay. "
                "Use for: 'remind me at 17:30 to call mom', 'remind me in 20 minutes to check the oven'. "
                "Give exactly one of at_time or in_minutes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "What to remind the user about, e.g. 'call mom'",
                    },
                    "at_time": {
                        "type": "string",
                        "description": "24-hour clock time HH:MM, e.g. '17:30'. Tomorrow if already past today.",
                    },
                    "in_minutes": {
                        "type": "number",
                        "description": "Minutes from now",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_timers",
            "description": "List active timers, reminders and pomodoros with the time remaining on each",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Cancel an active timer, reminder or pomodoro by its label, or all of them if no label is given",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Label (or part of it) of the timer to cancel; omit to cancel all",
                    },
                },
                "required": [],
            },
        },
    },
]


async def _dispatch_tool(tool_name: str, args: dict) -> str:
    """Route a tool call to its implementation. Raises on bad arguments."""
    # Small local models often send the *string* "null"/"none" for optional
    # arguments instead of omitting them — treat those as missing.
    args = {
        k: v for k, v in args.items()
        if not (isinstance(v, str) and v.strip().lower() in {"", "null", "none", "undefined"})
    }
    if tool_name == "get_weather":
        from arya.tools.weather import geocode, get_weather

        try:
            city = str(args["city"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid weather arguments.") from exc
        try:
            latitude = float(args["latitude"])
            longitude = float(args["longitude"])
        except (KeyError, TypeError, ValueError):
            # Small local models don't know coordinates — look the city up.
            latitude, longitude, city = await geocode(city)

        return await get_weather(city=city, latitude=latitude, longitude=longitude)
    elif tool_name == "open_app":
        try:
            app_name = str(args["app_name"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid app arguments.") from exc

        from arya.tools.os_tools import open_app

        return await open_app(app_name=app_name)
    elif tool_name == "open_url":
        try:
            url = str(args["url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid URL arguments.") from exc

        from arya.tools.os_tools import open_url

        return await open_url(url=url)
    elif tool_name == "file_search":
        try:
            filename = str(args["filename"])
            search_path = str(args.get("search_path", "C:\\Users"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid file search arguments.") from exc

        from arya.tools.os_tools import file_search

        return await file_search(filename=filename, search_path=search_path)
    elif tool_name == "run_command":
        try:
            command = str(args["command"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid command arguments.") from exc

        from arya.tools.os_tools import run_command

        return await run_command(command=command)
    elif tool_name == "get_system_info":
        from arya.tools.system_info import get_system_info

        return await get_system_info()
    elif tool_name == "get_battery":
        from arya.tools.system_info import get_battery

        return await get_battery()
    elif tool_name == "set_timer":
        try:
            duration_seconds = round(float(args.get("minutes") or 0) * 60 + float(args.get("seconds") or 0)
                                     + float(args.get("duration_seconds") or 0))
            label = str(args.get("label", "Timer"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Model returned invalid timer arguments.") from exc
        if duration_seconds <= 0:
            raise RuntimeError("Timer needs a duration — ask the user how long.")

        from arya.tools.productivity import set_timer

        return await set_timer(duration_seconds=duration_seconds, label=label)
    elif tool_name == "pomodoro":
        try:
            sessions = int(args.get("sessions", 1))
            label = str(args.get("label", "Study"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid pomodoro arguments.") from exc

        from arya.tools.productivity import pomodoro

        return await pomodoro(sessions=sessions, label=label)
    elif tool_name == "get_clipboard":
        from arya.tools.productivity import get_clipboard

        return await get_clipboard()
    elif tool_name == "set_clipboard":
        try:
            text = str(args["text"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid clipboard arguments.") from exc

        from arya.tools.productivity import set_clipboard

        return await set_clipboard(text=text)
    elif tool_name == "calculate":
        try:
            expression = str(args["expression"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid calculation arguments.") from exc

        from arya.tools.knowledge import calculate

        return await calculate(expression=expression)
    elif tool_name == "search_wikipedia":
        try:
            query = str(args["query"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid Wikipedia arguments.") from exc

        from arya.tools.knowledge import search_wikipedia

        return await search_wikipedia(query=query)
    elif tool_name == "explain_concept":
        try:
            topic = str(args["topic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid explain_concept arguments.") from exc

        lowered_topic = topic.lower()
        if "simply" in lowered_topic or "simple" in lowered_topic:
            level = "simple"
        elif "technically" in lowered_topic or "technical" in lowered_topic:
            level = "technical"
        elif "exam" in lowered_topic:
            level = "exam"
        else:
            level = "simple"

        cleaned_topic = re.sub(r"\b(simply|simple|technically|technical|exam)\b", "", topic, flags=re.IGNORECASE)
        cleaned_topic = re.sub(r"\s+", " ", cleaned_topic).strip()
        if not cleaned_topic:
            cleaned_topic = topic

        from arya.tools.study import explain_concept

        return await explain_concept(topic=cleaned_topic, level=level)
    elif tool_name == "formula_lookup":
        try:
            topic = str(args["topic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid formula_lookup arguments.") from exc

        from arya.tools.study import formula_lookup

        return await formula_lookup(topic=topic)
    elif tool_name == "flashcard_quiz":
        try:
            topic = str(args["topic"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid flashcard_quiz arguments.") from exc

        match = re.search(r"\d+", topic)
        num_questions = int(match.group()) if match else 3
        cleaned_topic = re.sub(r"\d+", "", topic)
        cleaned_topic = re.sub(r"\s+", " ", cleaned_topic).strip()
        if not cleaned_topic:
            cleaned_topic = topic

        from arya.tools.study import flashcard_quiz

        return await flashcard_quiz(topic=cleaned_topic, num_questions=num_questions)
    elif tool_name == "save_note":
        try:
            content = str(args["content"])
            tag = str(args.get("tag", "general"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid note arguments.") from exc

        from arya.tools.notes import save_note

        return await save_note(content=content, tag=tag)
    elif tool_name == "get_notes":
        from arya.tools.notes import get_notes

        return await get_notes(tag=args.get("tag"), limit=args.get("limit", 10))
    elif tool_name == "search_notes":
        try:
            keyword = str(args["keyword"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete or invalid search_notes arguments.") from exc

        from arya.tools.notes import search_notes

        return await search_notes(keyword=keyword)
    elif tool_name == "get_daily_briefing":
        return await get_daily_briefing()
    elif tool_name == "media_control":
        from arya.tools.media import media_control

        steps = args.get("steps")
        return await media_control(
            action=str(args.get("action", "")),
            steps=int(steps) if steps is not None else 5,
        )
    elif tool_name == "set_reminder":
        try:
            reminder_message = str(args["message"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Model returned incomplete reminder arguments.") from exc

        from arya.tools.productivity import set_reminder

        in_minutes = args.get("in_minutes")
        return await set_reminder(
            message=reminder_message,
            at_time=args.get("at_time"),
            in_minutes=float(in_minutes) if in_minutes not in (None, "") else None,
        )
    elif tool_name == "list_timers":
        from arya.tools.productivity import list_timers

        return await list_timers()
    elif tool_name == "cancel_timer":
        from arya.tools.productivity import cancel_timer

        return await cancel_timer(label=args.get("label"))
    else:
        raise RuntimeError(f"Unsupported tool requested by model: {tool_name}")


async def _execute_tool(tool_name: str, args: dict) -> str:
    """Run one tool. Failures come back as text so the model can recover
    (retry with fixed arguments, or explain the problem) instead of the whole
    turn crashing with 'ARYA encountered an error'."""
    from arya.core.audit import log_tool

    started = time.monotonic()
    try:
        result = await _dispatch_tool(tool_name, args)
    except Exception as exc:
        print(f"Tool error ({tool_name}): {exc}")
        log_tool(tool_name, args, ok=False, ms=(time.monotonic() - started) * 1000, error=str(exc))
        return f"Tool '{tool_name}' failed: {exc}"
    log_tool(tool_name, args, ok=True, ms=(time.monotonic() - started) * 1000)
    return str(result)


def _parse_args(tool_call: _ToolCall) -> dict:
    args = tool_call.arguments
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return {}
    return args if isinstance(args, dict) else {}


async def _tool_pipeline(
    message: str,
    history: list[dict] | None = None,
    mem_context: str | None = None,
    save_user: bool = True,
) -> str:
    """Agent loop: let the model call tools (chaining up to MAX_TOOL_STEPS)
    until it can answer. Only call when a tool is confirmed needed."""
    if history is None:
        history = get_history(limit=10)
    if mem_context is None:
        recent_contents = {item["content"] for item in history}
        mem_context = await _get_semantic_context(message, recent_contents)

    if save_user:
        msg_id = save_message("user", message)
        asyncio.create_task(_store_embedding("message", msg_id, f"[user]: {message}"))

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(with_tools=True) + mem_context},
        *({"role": item["role"], "content": item["content"]} for item in history),
        {"role": "user", "content": message},
    ]

    multi_part = bool(_MULTI_PART.search(message))
    seen_calls: set[tuple[str, str]] = set()
    assistant_response = ""
    for step in range(MAX_TOOL_STEPS + 1):
        # The big model picks the first tool; the fast 8B model handles the
        # follow-up steps (reading results, chaining another call, answering),
        # which keeps latency and Groq token use down.
        model = MODEL_NAME if step == 0 else INTENT_MODEL_NAME
        # Re-sending ~3k tokens of tool schemas on every step burns Groq's
        # free-tier per-minute budget, so after the first call only offer
        # tools again when the request sounds multi-part.
        allow_tools = step == 0 or (step < MAX_TOOL_STEPS and multi_part)
        completion = await _complete(
            model, messages, tools=TOOLS if allow_tools else None, temperature=0
        )

        if not completion.tool_calls:
            assistant_response = completion.content
            break

        tool_call = completion.tool_calls[0]
        tool_name = re.split(r"[\s{=,]", tool_call.name)[0].strip()
        args = _parse_args(tool_call)
        signature = (tool_name, json.dumps(args, sort_keys=True))
        if signature in seen_calls:
            # Model is looping on the same call — force a final answer.
            completion = await _complete(INTENT_MODEL_NAME, messages, temperature=0)
            assistant_response = completion.content
            break
        seen_calls.add(signature)

        print(f"Tool called (step {step + 1}): {tool_name}, Args: {args}")
        tool_result = await _execute_tool(tool_name, args)

        messages.append({
            "role": "assistant",
            "content": completion.content,
            "tool_calls": [{
                "id": tool_call.id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(args)},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": (
                f"Tool result: {tool_result}\n\n"
                "If the request needs another tool, call it. Otherwise report the "
                "result to the user accurately, in plain spoken sentences. Do not "
                "recalculate, rephrase numbers, or add information not present in the result."
            ),
        })

    if not assistant_response.strip():
        assistant_response = "Done."

    resp_id = save_message("assistant", assistant_response)
    asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {assistant_response}"))
    return assistant_response


async def chat_with_tools(message: str) -> str:
    """Public entry point: checks intent then dispatches tools if needed."""
    if not await needs_tool(message):
        return await chat(message)
    return await _tool_pipeline(message)


# ── Instant commands (no LLM) ─────────────────────────────────────────────────
# Short, unambiguous media commands are matched directly: instant, and immune to
# small local models fumbling the tool call ("volume up" → empty reply).
_QUICK_LEADING = re.compile(
    r"^(?:(?:hey|ok|okay)\s+)?(?:arya\s+|aria\s+)?(?:please\s+|can you\s+|could you\s+|would you\s+)*")
_QUICK_MEDIA = [
    (re.compile(r"^(?:turn|crank|bump|put|bring)\s+(?:the\s+)?(?:volume|sound|music|it)\s+up$|^(?:volume|sound)\s+up$"
                r"|^(?:make\s+it\s+)?louder$|^increase\s+(?:the\s+)?volume$"
                r"|^turn\s+up\s+(?:the\s+)?(?:volume|sound|music)$"), "volume_up"),
    (re.compile(r"^(?:turn|bring|put)\s+(?:the\s+)?(?:volume|sound|music|it)\s+down$|^(?:volume|sound)\s+down$"
                r"|^(?:make\s+it\s+)?(?:quieter|softer)$|^(?:lower|decrease|reduce)\s+(?:the\s+)?volume$"
                r"|^turn\s+down\s+(?:the\s+)?(?:volume|sound|music)$"), "volume_down"),
    (re.compile(r"^(?:un)?mute(?:\s+(?:the\s+)?(?:sound|volume|audio|it|music|pc|computer))?$"), "mute"),
    (re.compile(r"^(?:pause|resume|unpause|continue)(?:\s+(?:the\s+)?(?:music|song|video|playback|it|track))?$"
                r"|^play(?:\s+(?:the\s+)?(?:music|song|video|it))?$"), "play_pause"),
    (re.compile(r"^(?:next|skip)(?:\s+(?:this\s+|the\s+)?(?:song|track|one|video))?$"
                r"|^(?:play\s+)?(?:the\s+)?next\s+(?:song|track)$"), "next"),
    (re.compile(r"^(?:previous|go\s+back)(?:\s+(?:song|track))?$"
                r"|^(?:play\s+)?(?:the\s+)?(?:previous|last)\s+(?:song|track)$"), "previous"),
    (re.compile(r"^stop\s+(?:the\s+)?(?:music|song|video|playback)$"), "stop"),
]
_LITTLE = re.compile(r"\s*\b(?:a (?:little )?bit|a little|slightly)\b")
_LOT = re.compile(r"\s*\b(?:a lot|way|much)\b")


def quick_media_command(message: str) -> dict | None:
    """'Turn the volume up a bit' -> {'action': 'volume_up', 'steps': 3}; None if not a media command."""
    text = re.sub(r"[^\w\s']", " ", message.lower())
    text = _QUICK_LEADING.sub("", re.sub(r"\s+", " ", text).strip())
    text = re.sub(r"\s+(?:please|now|thanks|thank you)$", "", text).strip()
    steps = 5
    if _LITTLE.search(text):
        steps, text = 3, _LITTLE.sub("", text).strip()
    elif _LOT.search(text):
        steps, text = 12, _LOT.sub("", text).strip()
    for pattern, action in _QUICK_MEDIA:
        if pattern.search(text):
            args: dict = {"action": action}
            if action in ("volume_up", "volume_down"):
                args["steps"] = steps
            return args
    return None


def _cloud_system_prompt() -> str:
    """For explicit cloud requests: speaking rules and the date only — no name,
    location or memories leave the machine."""
    now = datetime.now().strftime("%A, %d %B %Y")
    return (
        "You are a voice assistant. Today is " + now + ". Your reply is spoken aloud: answer in one to "
        "four short sentences, no markdown, lists or emojis, numbers said the way a person would."
    )


async def stream_with_tools(message: str):
    """Yield the reply sentence by sentence (so TTS can start on sentence one).
    Routes: instant media command → explicit cloud question → LLM (local by default)."""
    quick = quick_media_command(message)
    if quick is not None:
        print(f"Instant command: media_control {quick}")
        result = await _execute_tool("media_control", quick)
        save_message("user", message)
        save_message("assistant", result)
        yield result
        return

    is_cloud, question = split_cloud_request(message)
    inner = (_stream_cloud_question(question) if is_cloud and ARYA_MODE == "offline"
             else _stream_with_tools(message))
    try:
        async for sentence in inner:
            yield sentence
    finally:
        # Close the inner generator *now* when the user interrupts, so its
        # cleanup (saving the partial reply to history) runs immediately.
        await inner.aclose()


async def _stream_cloud_question(question: str):
    """Send one question — and nothing else — to Groq. Falls back to the local
    model if Groq isn't configured or reachable."""
    from arya.core.audit import log_event

    print(f"Cloud request (only this question is sent): {question[:80]}")
    log_event("cloud_request", chars=len(question))
    save_message("user", question)
    messages = [{"role": "system", "content": _cloud_system_prompt()}, {"role": "user", "content": question}]
    token = _cloud_turn.set(True)
    parts: list[str] = []
    buffer = ""
    try:
        if not os.getenv("GROQ_API_KEY"):
            yield "The cloud isn't set up, so I'll answer locally."
        async for tok in _stream_tokens(INTENT_MODEL_NAME, messages):
            buffer += tok
            while (match := _SENTENCE_END.search(buffer)):
                sentence, buffer = buffer[: match.end()], buffer[match.end():]
                if sentence.strip():
                    parts.append(sentence)
                    yield sentence
        if buffer.strip():
            parts.append(buffer)
            yield buffer
    finally:
        _cloud_turn.reset(token)
        if parts:
            save_message("assistant", "".join(parts).strip())


async def _stream_with_tools(message: str):
    """Yield the reply sentence by sentence (so TTS can start on sentence one)."""
    # Intent check and memory lookup are independent — run them together.
    history = get_history(limit=10)
    recent_contents = {item["content"] for item in history}
    needs_tool_result, mem_context = await asyncio.gather(
        needs_tool(message),
        _get_semantic_context(message, recent_contents),
    )
    print(f"Needs tool: {needs_tool_result}")

    if needs_tool_result:
        response = await _tool_pipeline(message, history, mem_context)
        for sentence in split_sentences(response):
            yield sentence
        return

    msg_id = save_message("user", message)
    asyncio.create_task(_store_embedding("message", msg_id, f"[user]: {message}"))

    messages = [
        {"role": "system", "content": _system_prompt() + mem_context},
        *({"role": item["role"], "content": item["content"]} for item in history),
        {"role": "user", "content": message},
    ]

    buffer = ""
    full_response_parts: list[str] = []

    try:
        try:
            async for token in _stream_tokens(INTENT_MODEL_NAME, messages):
                buffer += token

                while True:
                    match = _SENTENCE_END.search(buffer)
                    if not match:
                        break

                    sentence = buffer[: match.end()]
                    buffer = buffer[match.end():]
                    if sentence.strip():
                        full_response_parts.append(sentence)
                        yield sentence
        except _ModelWantsTool:
            # The intent check said "no tool" but the model disagrees — trust
            # the model and run the tool pipeline (it saves its own reply).
            print("Chat model wants a tool — escalating to tool pipeline")
            response = await _tool_pipeline(message, history, mem_context, save_user=False)
            for sentence in split_sentences(response):
                yield sentence
            return

        if buffer.strip():
            full_response_parts.append(buffer)
            yield buffer
    finally:
        # Runs on normal completion *and* when the user interrupts ARYA, so
        # history records what was actually said instead of a dangling question.
        full_response = "".join(full_response_parts).strip()
        if full_response:
            resp_id = save_message("assistant", full_response)
            asyncio.create_task(_store_embedding("message", resp_id, f"[assistant]: {full_response}"))
