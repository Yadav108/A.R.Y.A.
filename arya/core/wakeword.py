"""
Wake-word engine — Phase 2 of the ARYA roadmap.

Flow
----
sounddevice mic  →  energy VAD  →  speech segment collected
                                         │
                                   whisper-tiny (CPU int8)
                                         │
                              "arya" in transcript?
                                   YES  │
                              extract command text
                                         │
                              async callback → WebSocket pipeline
"""

import asyncio
import os
import tempfile
import threading
import wave
from collections import deque
from typing import Callable, Awaitable

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


# ── Audio constants ───────────────────────────────────────────────────────────
SAMPLE_RATE        = 16_000        # Hz  (Whisper native rate)
FRAME_MS           = 100           # ms per VAD frame
FRAME_SAMPLES      = int(SAMPLE_RATE * FRAME_MS / 1000)

ENERGY_THRESHOLD   = 0.025         # RMS threshold — above = speech (raised to ignore background audio)
MIN_SPEECH_FRAMES  = 5             # ≥ 0.5 s of speech before we bother
MAX_SPEECH_FRAMES  = 120           # ≤ 12 s recording cap
SILENCE_FRAMES     = 8             # 0.8 s of quiet ends the utterance

# All spellings Whisper-tiny uses for "ARYA"
WAKE_VARIANTS = {"arya", "aria", "ria", "rya", "r.a.", "ara", "arja"}


class WakeWordEngine:
    """
    Listens to the microphone in a background daemon thread.
    When the user says anything containing "arya", fires an async callback
    with the extracted command (text after "arya").
    """

    def __init__(self) -> None:
        self._model:    WhisperModel | None = None
        self._callback: Callable[[str], Awaitable[None]] | None = None
        self._loop:     asyncio.AbstractEventLoop | None = None
        self._running   = False
        self._thread:   threading.Thread | None = None
        self._muted     = False          # muted while ARYA is speaking

    # ── Public API ────────────────────────────────────────────────────────────

    def set_callback(
        self,
        callback: Callable[[str], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._callback = callback
        self._loop = loop

    def mute(self, state: bool) -> None:
        """Mute during TTS playback so ARYA doesn't hear herself."""
        self._muted = state

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="wakeword")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load_model(self) -> WhisperModel:
        from arya.core.stt import _register_nvidia_dll_dirs
        _register_nvidia_dll_dirs()
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        print("WakeWord: Whisper tiny loaded on CPU (int8)")
        return model

    def _run(self) -> None:
        self._model = self._load_model()

        audio_buf: list[np.ndarray] = []
        speech_frames  = 0
        silence_frames = 0
        recording      = False

        def _cb(indata: np.ndarray, frames: int, time, status) -> None:
            nonlocal audio_buf, speech_frames, silence_frames, recording

            if self._muted:
                return

            chunk = indata[:, 0].copy()
            rms   = float(np.sqrt(np.mean(chunk ** 2)))
            is_speech = rms > ENERGY_THRESHOLD

            if is_speech:
                speech_frames  += 1
                silence_frames  = 0
                audio_buf.append(chunk)
                if not recording and speech_frames >= 2:
                    recording = True
            else:
                if recording:
                    silence_frames += 1
                    audio_buf.append(chunk)

                    end_of_utt = silence_frames >= SILENCE_FRAMES and speech_frames >= MIN_SPEECH_FRAMES
                    too_long   = speech_frames  >= MAX_SPEECH_FRAMES

                    if end_of_utt or too_long:
                        data   = np.concatenate(audio_buf).copy()
                        speech_frames  = 0
                        silence_frames = 0
                        recording      = False
                        audio_buf      = []
                        threading.Thread(
                            target=self._process,
                            args=(data,),
                            daemon=True,
                        ).start()
                elif speech_frames > 0 and not recording:
                    # very short noise burst — reset
                    speech_frames  = 0
                    silence_frames = 0
                    audio_buf      = []

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            callback=_cb,
        ):
            print("WakeWord: listening — say 'Hey ARYA ...' to activate")
            while self._running:
                sd.sleep(200)

    def _process(self, audio: np.ndarray) -> None:
        """Transcribe segment; fire callback if 'arya' detected."""
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name
            _write_wav(tmp_path, audio)

            segments, _ = self._model.transcribe(
                tmp_path,
                language="en",
                beam_size=3,
                vad_filter=False,
            )
            transcript = " ".join(s.text.strip() for s in segments).strip()
            if not transcript:
                return

            lower = transcript.lower()

            # Find which variant (if any) triggered
            matched_variant = None
            matched_idx     = -1
            for variant in WAKE_VARIANTS:
                idx = lower.find(variant)
                if idx != -1:
                    if matched_idx == -1 or idx < matched_idx:
                        matched_variant = variant
                        matched_idx     = idx

            if matched_variant:
                print(f"WakeWord TRIGGERED ({matched_variant!r}): {transcript!r}")
            else:
                print(f"WakeWord heard (no match): {transcript!r}")
                return

            # Extract the command that came after the wake word
            command = transcript[matched_idx + len(matched_variant):].lstrip(" ,.:!?").strip()
            print(f"WakeWord command: {command!r}")

            if self._callback and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._callback(command),
                    self._loop,
                )

        except Exception as exc:
            print(f"WakeWord error: {exc}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_wav(path: str, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


# ── Singleton ─────────────────────────────────────────────────────────────────
engine = WakeWordEngine()
