import asyncio
import os
import site
import tempfile
from pathlib import Path
from typing import Iterable

from faster_whisper import WhisperModel


_model: WhisperModel | None = None

MIN_AVG_LOGPROB = -1.0
MAX_NO_SPEECH_PROB = 0.6
WHISPER_BEAM_SIZE = 5
WHISPER_COMPRESSION_RATIO_THRESHOLD = 2.4

# Whisper's well-known canned hallucinations on quiet/ambiguous audio — trained
# into it from YouTube caption data. If the *entire* accepted transcript is one
# of these verbatim, it's noise, not speech.
_HALLUCINATION_PHRASES = {
    "thank you for watching",
    "thanks for watching",
    "thank you for watching!",
    "thanks for watching!",
    "please subscribe",
    "subscribe to my channel",
    "like and subscribe",
    "don't forget to subscribe",
    "see you in the next video",
    "see you next time",
    "thanks for watching this video",
    "thank you so much for watching",
    "i'll see you in the next one",
}


def _accept_segments(segments: Iterable) -> str:
    parts: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        if getattr(segment, "no_speech_prob", 0.0) > MAX_NO_SPEECH_PROB:
            continue
        if getattr(segment, "avg_logprob", 0.0) < MIN_AVG_LOGPROB:
            continue
        parts.append(text)
    result = " ".join(parts).strip()
    if result.lower().strip(" .!?") in _HALLUCINATION_PHRASES:
        return ""
    return result


def _register_nvidia_dll_dirs() -> None:
    """Add NVIDIA CUDA-12 wheel DLL directories to Windows DLL search path.

    nvidia-cublas-cu12 (and friends) install their .dll files under
    site-packages/nvidia/<package>/bin/.  ctranslate2 looks for
    cublas64_12.dll via the normal Windows DLL search order, which does
    NOT include arbitrary site-packages sub-folders unless we tell it to.
    """
    packages = ["cublas", "cuda_runtime", "cuda_nvrtc", "cufft", "curand"]
    for sp in site.getsitepackages():
        for pkg in packages:
            dll_dir = Path(sp) / "nvidia" / pkg / "bin"
            if dll_dir.is_dir():
                try:
                    os.add_dll_directory(str(dll_dir))
                except (OSError, AttributeError):
                    pass
                # also patch PATH for any loader that doesn't use add_dll_directory
                os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")


def load_model() -> WhisperModel:
    """Load Whisper medium. Called once at server startup."""
    global _model
    if _model is not None:
        return _model

    _register_nvidia_dll_dirs()

    try:
        _model = WhisperModel("medium", device="cuda", compute_type="float16")
        print("STT: Whisper medium loaded on CUDA (float16)")
    except Exception as exc:
        print(f"STT: CUDA load failed ({exc}), falling back to CPU int8")
        _model = WhisperModel("medium", device="cpu", compute_type="int8")
        print("STT: Whisper medium loaded on CPU (int8)")

    return _model


def _run_model(m: WhisperModel, source) -> str:
    segments, _ = m.transcribe(
        source,
        language="en",
        beam_size=WHISPER_BEAM_SIZE,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 120},
        no_speech_threshold=MAX_NO_SPEECH_PROB,
        log_prob_threshold=MIN_AVG_LOGPROB,
        compression_ratio_threshold=WHISPER_COMPRESSION_RATIO_THRESHOLD,
    )
    return _accept_segments(segments)


def _run_with_cpu_fallback(source) -> str:
    global _model
    model = load_model()
    try:
        return _run_model(model, source)
    except RuntimeError as exc:
        if any(k in str(exc).lower() for k in ("cublas", "cuda", "cufft", "curand")):
            print(f"STT: CUDA runtime error — switching to CPU permanently. ({exc})")
            _model = WhisperModel("medium", device="cpu", compute_type="int8")
            print("STT: Whisper reloaded on CPU (int8)")
            return _run_model(_model, source)
        raise


def _transcribe_sync(audio_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        return _run_with_cpu_fallback(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def transcribe_array(audio: "np.ndarray") -> str:
    """Transcribe 16 kHz mono float32 PCM with Whisper medium. Blocking —
    called from the wake-word worker thread to get an accurate command
    transcript after the small model has spotted the wake word."""
    if audio is None or len(audio) == 0:
        return ""
    return _run_with_cpu_fallback(audio.astype("float32", copy=False))


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe raw audio bytes → English text. Non-blocking."""
    if not audio_bytes:
        return ""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, audio_bytes)
