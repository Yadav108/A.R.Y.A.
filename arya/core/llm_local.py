"""
Local LLM backend — thin async wrapper around Ollama's /api/chat endpoint.

ARYA's default brain (ARYA_MODE=offline), and the fallback when Groq is
rate-limited or unreachable in cloud mode.
Accepts and returns OpenAI-style messages so llm.py can treat both backends alike.
"""
import json
import os
import re
from collections.abc import AsyncIterator

import httpx
from dotenv import load_dotenv

load_dotenv()

# 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first and costs ~0.3-2 s per new connection.
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/").replace("//localhost", "//127.0.0.1")
# qwen2.5:3b: reliable tool calling and fits a 4 GB GPU next to Whisper medium.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
# Models that "think" before answering (qwen3, deepseek-r1, …): thinking is switched off for spoken answers.
THINKING_MODEL = re.compile(r"^(?:qwen3|deepseek-r1|magistral|gpt-oss)", re.IGNORECASE)

# Ollama's default 2048-token context truncates ARYA's ~3k-token tool schemas.
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
# Keep the model in VRAM between requests — reloading it costs ~40 s cold.
KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "60m")
# Memory-map the model file instead of copying it into RAM. Ollama turns mmap off by default when the model
# fits on the GPU; on this laptop that left llama-server with 7.0 GB of committed memory for a 2.3 GB model and
# the system with 27 MB of free commit — the cause of the out-of-memory failures (pytest dying, KiCad's Python
# not starting, "paging file too small"). With mmap: 2.9 GB, and 1.7 GB of commit free (measured 2026-11-17).
# Every request must send the same value, or Ollama reloads the model.
USE_MMAP = os.getenv("OLLAMA_USE_MMAP", "1") != "0"
# Spoken answers: low randomness keeps a small model close to the facts and course
# excerpts it was given (Ollama's default ~0.8 lets it drift, e.g. mixing up damping cases).
ANSWER_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.3"))

# Generous read timeout: the first request after idle loads the model into VRAM.
_TIMEOUT = httpx.Timeout(120.0, connect=3.0)


_shared: tuple | None = None                  # (event loop, client)


def _client() -> httpx.AsyncClient:
    """One client per event loop, kept open: creating one costs ~0.3-0.4 s on Windows (SSL setup),
    which every spoken answer used to pay before its first word."""
    global _shared
    import asyncio

    loop = asyncio.get_running_loop()
    if _shared is None or _shared[0] is not loop or _shared[1].is_closed:
        _shared = (loop, httpx.AsyncClient(timeout=_TIMEOUT))   # a client can't move between loops
    return _shared[1]


class LocalLLMUnavailable(RuntimeError):
    """Ollama is not running, or the configured model has not been pulled."""


# The GPU is full (from the log, 2026-11-16: "llama-server reported out-of-memory during startup: cudaMalloc",
# "process has terminated: exit status 0xc0000409"): ARYA went quiet for an hour. Instead, load the model
# partly (then fully) on the CPU — slower answers, but answers — and try the whole GPU again after a while.
import time as _time

_GPU_FULL = re.compile(r"cudaMalloc|out.of.memory|ggml_backe|CUDA error|0xc0000409|0xc0000005", re.IGNORECASE)
GPU_STEPS = (18, 0)                       # layers on the GPU (qwen2.5:3b has 36): half, then none
GPU_RETRY_S = 10 * 60
_gpu: dict = {"layers": None, "until": 0.0}


def _gpu_layers() -> int | None:
    if _gpu["layers"] is not None and _time.monotonic() >= _gpu["until"]:
        _gpu["layers"] = None             # try the whole GPU again
    return _gpu["layers"]


def _step_down(exc: Exception) -> bool:
    """After a GPU-memory failure: use fewer GPU layers next. False when there's nothing left to try."""
    if not _GPU_FULL.search(str(exc)):
        return False
    current = _gpu_layers()
    smaller = [n for n in GPU_STEPS if current is None or n < current]
    if not smaller:
        return False
    _gpu.update(layers=smaller[0], until=_time.monotonic() + GPU_RETRY_S)
    where = "partly on the CPU" if smaller[0] else "on the CPU"
    print(f"LLM: GPU memory full — loading {OLLAMA_MODEL} {where} for now (slower; GPU again in "
          f"{GPU_RETRY_S // 60} min)")
    return True


def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """Convert OpenAI-style messages to Ollama's format.

    Differences: tool-call arguments are objects (not JSON strings), and
    tool results carry no tool_call_id.
    """
    converted: list[dict] = []
    for msg in messages:
        out = {"role": msg["role"], "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            calls = []
            for call in msg["tool_calls"]:
                fn = call["function"]
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"function": {"name": fn["name"], "arguments": args}})
            out["tool_calls"] = calls
        converted.append(out)
    return converted


def _payload(messages: list[dict], stream: bool, tools: list | None,
             temperature: float | None, max_tokens: int | None, fmt: dict | str | None = None) -> dict:
    import time
    _last_used["t"] = time.monotonic()                 # for trim(): only reload while nobody's using it
    options: dict = {"num_ctx": NUM_CTX, "use_mmap": USE_MMAP}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    if _gpu_layers() is not None:
        options["num_gpu"] = _gpu_layers()
    payload: dict = {
        "model": OLLAMA_MODEL,
        "messages": _to_ollama_messages(messages),
        "stream": stream,
        "keep_alive": KEEP_ALIVE,
    }
    if tools:
        payload["tools"] = tools
    if fmt is not None:
        payload["format"] = fmt             # structured output: "json" or a JSON schema the reply must follow
    if THINKING_MODEL.search(OLLAMA_MODEL):
        payload["think"] = False            # hidden reasoning costs seconds before the first spoken word
    payload["options"] = options
    return payload


def _raise_for_status(response: httpx.Response, body: str) -> None:
    if response.status_code < 400:
        return
    try:
        detail = json.loads(body).get("error", body)
    except (json.JSONDecodeError, AttributeError):
        detail = body
    if response.status_code == 404:
        raise LocalLLMUnavailable(
            f"Ollama model '{OLLAMA_MODEL}' not found — run: ollama pull {OLLAMA_MODEL}"
        )
    raise LocalLLMUnavailable(f"Ollama error {response.status_code}: {detail}")


async def chat_local(
    messages: list[dict],
    tools: list | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    fmt: dict | str | None = None,
) -> dict:
    """Non-streaming chat. Returns Ollama's assistant message:
    {"role": "assistant", "content": str, "tool_calls": [{"function": {"name", "arguments": dict}}]?}
    `fmt`: a JSON schema (or "json") the reply must follow — Ollama's structured outputs.
    """
    while True:
        payload = _payload(messages, False, tools, temperature, max_tokens, fmt)
        try:
            response = await _client().post(f"{OLLAMA_HOST}/api/chat", json=payload)
            _raise_for_status(response, response.text)
            return response.json().get("message", {})
        except httpx.HTTPError as exc:
            raise LocalLLMUnavailable(f"Cannot reach Ollama at {OLLAMA_HOST}: {exc!r}") from exc
        except LocalLLMUnavailable as exc:
            if not _step_down(exc):
                raise


async def stream_local(
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    """Streaming chat. Yields content tokens as they arrive."""
    while True:
        yielded = False
        try:
            async for token in _stream_once(messages, temperature, max_tokens):
                yielded = True
                yield token
            return
        except LocalLLMUnavailable as exc:
            if yielded or not _step_down(exc):
                raise


async def _stream_once(messages: list[dict], temperature: float | None, max_tokens: int | None) -> AsyncIterator[str]:
    payload = _payload(messages, True, None, temperature, max_tokens)
    try:
        async with _client().stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")
                _raise_for_status(response, body)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise LocalLLMUnavailable(f"Ollama error: {chunk['error']}")
                token = chunk.get("message", {}).get("content")
                if token:
                    yield token
                if chunk.get("done"):
                    break
    except httpx.HTTPError as exc:
        raise LocalLLMUnavailable(f"Cannot reach Ollama at {OLLAMA_HOST}: {exc!r}") from exc


async def warm_up(attempts: int = 2) -> bool:
    """Load the model into VRAM now (at startup) so the first request isn't slow.
    Retries once — a load can fail transiently while Whisper is still settling."""
    import asyncio

    attempt = 0
    while attempt < attempts:
        attempt += 1
        try:
            response = await _client().post(f"{OLLAMA_HOST}/api/chat", json={   # also opens the shared client
                **_payload([{"role": "user", "content": "hi"}], False, None, None, 1),
            })
            _raise_for_status(response, response.text)
            print(f"LLM: local model {OLLAMA_MODEL} ready"
                  + ("" if _gpu_layers() is None else f" ({_gpu_layers()} layers on the GPU)"))
            return True
        except (httpx.HTTPError, LocalLLMUnavailable) as exc:
            print(f"LLM: local model warm-up failed (attempt {attempt}): {str(exc)[:160]}")
            if _step_down(exc):                    # GPU full: try again at once with fewer GPU layers
                attempt -= 1
                continue
            if attempt < attempts:
                await asyncio.sleep(5)
    return False


# llama-server (Ollama's runner) keeps a RAM cache of past prompts that may grow to 8 GB (its default
# --cache-ram; Ollama doesn't expose it). Measured 2026-11-17: 2.9 GB after loading, 6.2 GB after 87 prompts,
# 7.0 GB after an evening — the out-of-memory failures. When ARYA has been idle a while and it's past
# TRIM_AT_MB, the model is unloaded and loaded again (a few seconds, while nobody is talking), which empties it.
# The lasting fix is outside ARYA: the user environment variable LLAMA_ARG_CACHE_RAM=512, then restart Ollama.
TRIM_AT_MB = int(os.getenv("ARYA_OLLAMA_TRIM_MB", "4500"))
TRIM_IDLE_S = 10 * 60
_last_used = {"t": 0.0}


def runner_memory_mb() -> int:
    """Committed memory of Ollama's model runner(s), MB (0 if none or unknown)."""
    try:
        import psutil
    except ImportError:
        return 0
    total = 0
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower().startswith(("llama-server", "ollama_llama_server")):
                total += p.memory_info().private
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total // 2**20


async def unload() -> None:
    await _client().post(f"{OLLAMA_HOST}/api/generate", json={"model": OLLAMA_MODEL, "keep_alive": 0})


async def trim(busy=lambda: False, every_s: float = 300.0, now=None) -> None:
    """Started by the server: keep llama-server's prompt cache from eating the laptop's memory."""
    import asyncio
    import time
    now = now or time.monotonic
    while True:
        await asyncio.sleep(every_s)
        try:
            if busy() or now() - _last_used["t"] < TRIM_IDLE_S:
                continue
            mb = runner_memory_mb()
            if mb < TRIM_AT_MB:
                continue
            await unload()
            await asyncio.sleep(3)
            ok = await warm_up(attempts=2)
            print(f"LLM: Ollama's prompt cache had grown to {mb / 1024:.1f} GB — reloaded the model "
                  f"({runner_memory_mb() / 1024:.1f} GB now{'' if ok else ', reload failed'})")
        except Exception as exc:
            print(f"LLM: memory trim skipped ({type(exc).__name__}: {exc})")


async def keep_trying(busy=lambda: False, every_s: float = 120.0, on_ready=None) -> None:
    """After a failed start (out of memory, 2026-11-16 23:47 and 00:00), load the model again every two
    minutes until it works — closing an app is enough, no restart needed. Skipped while a turn runs."""
    import asyncio

    while True:
        await asyncio.sleep(every_s)
        if busy():
            continue
        if await warm_up(attempts=1):
            if on_ready is not None:
                on_ready()
            return
