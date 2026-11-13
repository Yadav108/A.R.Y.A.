import re
from collections import OrderedDict
from io import BytesIO

import edge_tts


VOICE_NAME = "en-GB-SoniaNeural"

# Short phrases ARYA says often ("Yes?", "Done.", timer alerts) are cached so
# they play instantly instead of waiting on a network round-trip.
_CACHE_MAX = 64
_CACHE_MAX_CHARS = 80
_cache: "OrderedDict[str, bytes]" = OrderedDict()


def clean_for_speech(text: str) -> str:
    """Strip markdown/formatting that a TTS voice would read out literally."""
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)        # code blocks
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)          # [label](url) → label
    text = re.sub(r"https?://\S+", "the link", text)              # bare URLs
    text = re.sub(r"(?<=\d)\s*\*\s*(?=\d)", " times ", text)      # keep 2*3 as maths
    text = re.sub(r"[*_`#>|~]+", "", text)                         # emphasis, headings, quotes
    text = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", text, flags=re.MULTILINE)  # list markers
    return re.sub(r"\s+", " ", text).strip()


async def speak_sentence(text: str) -> bytes:
    sentence = clean_for_speech(text)
    if not sentence or not re.search(r"\w", sentence):
        return b""

    cached = _cache.get(sentence)
    if cached is not None:
        _cache.move_to_end(sentence)
        return cached

    buffer = BytesIO()

    try:
        communicate = edge_tts.Communicate(text=sentence, voice=VOICE_NAME)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, (bytes, bytearray)):
                    buffer.write(data)
    except Exception as exc:
        raise RuntimeError(f"Failed to synthesize speech: {exc}") from exc

    audio = buffer.getvalue()
    if audio and len(sentence) <= _CACHE_MAX_CHARS:
        _cache[sentence] = audio
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return audio
