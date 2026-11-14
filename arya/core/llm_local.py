"""
Local LLM backend — thin async wrapper around Ollama's /api/chat endpoint.

ARYA's default brain (ARYA_MODE=offline), and the fallback when Groq is
rate-limited or unreachable in cloud mode.
Accepts and returns OpenAI-style messages so llm.py can treat both backends alike.
"""
import json
import os
from collections.abc import AsyncIterator

import httpx
from dotenv import load_dotenv

load_dotenv()

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
# qwen2.5:3b: reliable tool calling and fits a 4 GB GPU next to Whisper medium.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

# Ollama's default 2048-token context truncates ARYA's ~3k-token tool schemas.
NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
# Keep the model in VRAM between requests — reloading it costs ~40 s cold.
KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "60m")

# Generous read timeout: the first request after idle loads the model into VRAM.
_TIMEOUT = httpx.Timeout(120.0, connect=3.0)


class LocalLLMUnavailable(RuntimeError):
    """Ollama is not running, or the configured model has not been pulled."""


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
             temperature: float | None, max_tokens: int | None) -> dict:
    options: dict = {"num_ctx": NUM_CTX}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    payload: dict = {
        "model": OLLAMA_MODEL,
        "messages": _to_ollama_messages(messages),
        "stream": stream,
        "keep_alive": KEEP_ALIVE,
    }
    if tools:
        payload["tools"] = tools
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
) -> dict:
    """Non-streaming chat. Returns Ollama's assistant message:
    {"role": "assistant", "content": str, "tool_calls": [{"function": {"name", "arguments": dict}}]?}
    """
    payload = _payload(messages, False, tools, temperature, max_tokens)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
            _raise_for_status(response, response.text)
            return response.json().get("message", {})
    except httpx.HTTPError as exc:
        raise LocalLLMUnavailable(f"Cannot reach Ollama at {OLLAMA_HOST}: {exc!r}") from exc


async def stream_local(
    messages: list[dict],
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """Streaming chat. Yields content tokens as they arrive."""
    payload = _payload(messages, True, None, temperature, None)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as response:
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

    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(f"{OLLAMA_HOST}/api/chat", json={
                    **_payload([{"role": "user", "content": "hi"}], False, None, None, 1),
                })
                _raise_for_status(response, response.text)
            print(f"LLM: local model {OLLAMA_MODEL} ready")
            return True
        except (httpx.HTTPError, LocalLLMUnavailable) as exc:
            print(f"LLM: local model warm-up failed (attempt {attempt}): {str(exc)[:160]}")
            if attempt < attempts:
                await asyncio.sleep(5)
    return False
