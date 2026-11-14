"""
ARYA's voice.

TTS_ENGINE=piper (default) — Piper neural TTS running locally on the CPU. Fully
  offline: nothing ARYA says ever leaves the machine.
TTS_ENGINE=edge — Microsoft Edge online voices (sends every reply to Microsoft).

Piper voices live in arya/models/tts/; get more with
  .venv/Scripts/python.exe -m piper.download_voices --download-dir arya/models/tts <voice>
and pick one with PIPER_VOICE=<voice> (default en_GB-jenny_dioco-medium).
"""
import asyncio
import os
import re
import threading
import wave
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

TTS_ENGINE = os.getenv("TTS_ENGINE", "piper").strip().lower()
VOICE_NAME = os.getenv("EDGE_VOICE", "en-GB-SoniaNeural")          # edge engine only
PIPER_DIR = Path(__file__).resolve().parents[1] / "models" / "tts"
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-jenny_dioco-medium")
PIPER_SPEED = float(os.getenv("PIPER_SPEED", "1.0"))                # >1 = faster speech

_piper = None
_piper_lock = threading.Lock()


def _load_piper():
    global _piper
    if _piper is None:
        from piper import PiperVoice

        model = PIPER_DIR / f"{PIPER_VOICE}.onnx"
        if not model.exists():
            raise RuntimeError(
                f"Piper voice {PIPER_VOICE!r} not found in {PIPER_DIR}. Download it with: "
                f"python -m piper.download_voices --download-dir arya/models/tts {PIPER_VOICE}"
            )
        _piper = PiperVoice.load(str(model))
        print(f"TTS: Piper voice {PIPER_VOICE} loaded (offline)")
    return _piper


def _piper_wav(sentence: str) -> bytes:
    from piper import SynthesisConfig

    voice = _load_piper()
    buffer = BytesIO()
    with _piper_lock, wave.open(buffer, "wb") as wav:
        voice.synthesize_wav(sentence, wav, syn_config=SynthesisConfig(length_scale=1.0 / PIPER_SPEED))
    return buffer.getvalue()


async def _edge_mp3(sentence: str) -> bytes:
    import edge_tts

    buffer = BytesIO()
    communicate = edge_tts.Communicate(text=sentence, voice=VOICE_NAME)
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            data = chunk.get("data")
            if isinstance(data, (bytes, bytearray)):
                buffer.write(data)
    return buffer.getvalue()


def warm_up() -> None:
    """Load the local voice at startup so the first reply isn't delayed."""
    if TTS_ENGINE != "edge":
        _piper_wav("Ready.")

# Short phrases ARYA says often ("Yes?", "Done.", timer alerts) are cached so
# they play instantly.
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

    try:
        if TTS_ENGINE == "edge":
            audio = await _edge_mp3(sentence)
        else:
            audio = await asyncio.to_thread(_piper_wav, sentence)
    except Exception as exc:
        raise RuntimeError(f"Failed to synthesize speech: {exc}") from exc

    if audio and len(sentence) <= _CACHE_MAX_CHARS:
        _cache[sentence] = audio
        if len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return audio
