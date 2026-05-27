import asyncio
import os
import site
import tempfile
from pathlib import Path

from faster_whisper import WhisperModel


_model: WhisperModel | None = None


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


def _transcribe_sync(audio_bytes: bytes) -> str:
    global _model
    model = load_model()

    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    def _run(m: WhisperModel) -> str:
        segments, _ = m.transcribe(
            tmp_path,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    try:
        return _run(model)
    except RuntimeError as exc:
        if any(k in str(exc).lower() for k in ("cublas", "cuda", "cufft", "curand")):
            print(f"STT: CUDA runtime error — switching to CPU permanently. ({exc})")
            _model = WhisperModel("medium", device="cpu", compute_type="int8")
            print("STT: Whisper reloaded on CPU (int8)")
            return _run(_model)
        raise
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def transcribe(audio_bytes: bytes) -> str:
    """Transcribe raw audio bytes → English text. Non-blocking."""
    if not audio_bytes:
        return ""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, audio_bytes)
