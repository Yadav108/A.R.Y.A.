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
import re
import tempfile
import threading
import time
import wave
from collections import deque
from typing import Awaitable, Callable

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel


# ── Audio constants ───────────────────────────────────────────────────────────
SAMPLE_RATE        = 16_000        # Hz  (Whisper native rate)
FRAME_MS           = 30            # ms per VAD frame (shorter = faster response)
FRAME_SAMPLES      = int(SAMPLE_RATE * FRAME_MS / 1000)

ENERGY_THRESHOLD   = 0.008         # RMS threshold — tuned lower for sensitivity
MIN_SPEECH_FRAMES  = 3             # ≥ 90 ms of speech before we bother
MAX_SPEECH_FRAMES  = 200           # ≤ 6 s recording cap
SILENCE_FRAMES     = 15            # ~450 ms of quiet ends the utterance
PREROLL_FRAMES     = 8             # ~240 ms pre-roll so the start of "ARYA" isn't clipped

# Only keep unambiguous spellings — short variants like "ria"/"ara" cause false negatives
WAKE_VARIANTS = {"arya", "aria", "r.a.", "arja", "ariya", "ahria", "ar ya"}
FOLLOWUP_TIMEOUT_SECONDS = 12.0
MIN_AVG_LOGPROB = -1.0
MAX_NO_SPEECH_PROB = 0.6

# While ARYA is speaking, a bare stop phrase (no wake word needed) still counts
# as barge-in — it doesn't need the wake-word's ambiguity protection because
# the loud-input gate (BARGE_IN_DUCK_MULTIPLIER) below already filters out
# normal speaker bleed before this text match is even reached.
BARGE_IN_STOP_PHRASES = {
    "stop", "stop talking", "shut up", "be quiet", "quiet", "quiet please",
    "cancel", "cancel that", "never mind", "nevermind", "stop it", "silence",
    "that's enough", "thats enough", "enough",
}

# While ARYA is talking, the mic stays live (instead of fully muted) so you can
# interrupt her — but speaker bleed picked up by the same mic must not
# self-trigger. Require speech this many times louder than normal before it
# even counts as a candidate utterance while she's speaking, and require an
# explicit wake-word match (never lenient follow-up mode) to actually
# interrupt. Tune this up if ARYA interrupts herself; down if real interrupts
# don't register.
BARGE_IN_DUCK_MULTIPLIER = 4.0

# Regex built from variants — require word boundary on the right side
_WAKE_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(v) for v in sorted(WAKE_VARIANTS, key=len, reverse=True)) + r")(?!\w)",
    re.IGNORECASE,
)


def _is_low_confidence_segment(segment) -> bool:
    return (
        getattr(segment, "no_speech_prob", 0.0) > MAX_NO_SPEECH_PROB
        or getattr(segment, "avg_logprob", 0.0) < MIN_AVG_LOGPROB
    )


class WakeWordEngine:
    """
    Listens to the microphone in a background daemon thread.
    When the user says anything containing "arya", fires an async callback
    with the extracted command (text after "arya").
    After activation, accepts follow-up utterances for a short window even if
    they do not repeat the wake word.
    While ARYA is speaking, the mic stays live at a raised threshold so a
    loud, deliberate "ARYA" can interrupt her (see BARGE_IN_DUCK_MULTIPLIER).
    """

    def __init__(self) -> None:
        self._model:    WhisperModel | None = None
        self._callback: Callable[[str, bool], Awaitable[None]] | None = None
        self._loop:     asyncio.AbstractEventLoop | None = None
        self._running   = False
        self._thread:   threading.Thread | None = None
        self._speaking  = False          # ducked (not muted) while ARYA is speaking
        self._noise_floor = ENERGY_THRESHOLD  # adaptive noise floor
        self._followup_active = False
        self._followup_deadline = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def set_callback(
        self,
        callback: Callable[[str, bool], Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._callback = callback
        self._loop = loop

    def set_speaking(self, state: bool) -> None:
        """Duck (not mute) the mic while ARYA is speaking so barge-in still works."""
        self._speaking = state

    def _refresh_followup_mode(self) -> None:
        self._followup_active = True
        self._followup_deadline = time.monotonic() + FOLLOWUP_TIMEOUT_SECONDS

    def _followup_mode_enabled(self) -> bool:
        return self._followup_active and time.monotonic() < self._followup_deadline

    def _clear_followup_mode(self) -> None:
        self._followup_active = False
        self._followup_deadline = 0.0

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
        model = WhisperModel("base", device="cpu", compute_type="int8")
        print("WakeWord: Whisper base loaded on CPU (int8)")
        return model

    def _run(self) -> None:
        self._model = self._load_model()

        # Pre-roll ring buffer — always keeps the last N frames so we never
        # clip the beginning of the wake word before energy VAD fires.
        preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)

        audio_buf: list[np.ndarray] = []
        speech_frames  = 0
        silence_frames = 0
        recording      = False
        captured_while_speaking = False
        # Adaptive noise floor: track RMS during silence
        noise_samples: deque[float] = deque(maxlen=50)

        def _cb(indata: np.ndarray, frames: int, time, status) -> None:
            nonlocal audio_buf, speech_frames, silence_frames, recording, captured_while_speaking

            chunk = indata[:, 0].copy()
            rms   = float(np.sqrt(np.mean(chunk ** 2)))

            # Adaptive threshold: 3× the recent noise floor, floored at ENERGY_THRESHOLD.
            # While ARYA is speaking, require much louder input so speaker bleed
            # picked up by this same mic doesn't self-trigger.
            threshold = max(ENERGY_THRESHOLD, self._noise_floor * 3.0)
            if self._speaking:
                threshold *= BARGE_IN_DUCK_MULTIPLIER
            is_speech = rms > threshold

            if not is_speech and not recording:
                if not self._speaking:
                    noise_samples.append(rms)
                    if len(noise_samples) == noise_samples.maxlen:
                        self._noise_floor = float(np.mean(list(noise_samples)))
                preroll.append(chunk)
                return

            if is_speech:
                if not recording:
                    # Flush the pre-roll buffer so we capture "Ar-" before VAD fired
                    audio_buf = list(preroll) + [chunk]
                    recording = True
                    captured_while_speaking = self._speaking
                else:
                    audio_buf.append(chunk)
                speech_frames  += 1
                silence_frames  = 0
            else:
                # Silence while recording
                silence_frames += 1
                audio_buf.append(chunk)

                end_of_utt = silence_frames >= SILENCE_FRAMES and speech_frames >= MIN_SPEECH_FRAMES
                too_long   = speech_frames  >= MAX_SPEECH_FRAMES

                if end_of_utt or too_long:
                    data = np.concatenate(audio_buf).copy()
                    was_speaking   = captured_while_speaking
                    speech_frames  = 0
                    silence_frames = 0
                    recording      = False
                    audio_buf      = []
                    preroll.clear()
                    threading.Thread(
                        target=self._process,
                        args=(data, was_speaking),
                        daemon=True,
                    ).start()
                elif silence_frames > SILENCE_FRAMES * 2:
                    # Gave up — too much silence with too little speech
                    speech_frames  = 0
                    silence_frames = 0
                    recording      = False
                    audio_buf      = []

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=FRAME_SAMPLES,
            callback=_cb,
        ):
            print("WakeWord: listening — say 'ARYA ...' to activate")
            while self._running:
                sd.sleep(200)

    def _process(self, audio: np.ndarray, captured_while_speaking: bool = False) -> None:
        """Transcribe segment; fire callback if 'arya' detected."""
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp_path = f.name

            # Normalize audio so quiet speakers aren't missed
            peak = np.max(np.abs(audio))
            if peak > 0:
                audio = audio / peak * 0.9

            _write_wav(tmp_path, audio)

            segments, _ = self._model.transcribe(
                tmp_path,
                language="en",
                beam_size=1,
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=False,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 100},
                no_speech_threshold=MAX_NO_SPEECH_PROB,
                log_prob_threshold=MIN_AVG_LOGPROB,
                compression_ratio_threshold=2.4,
                initial_prompt="Arya,",  # biases Whisper toward the wake word spelling
            )
            accepted_segments = [s for s in segments if not _is_low_confidence_segment(s)]
            transcript = " ".join(s.text.strip() for s in accepted_segments).strip()
            if not transcript:
                return

            if self._followup_active and time.monotonic() >= self._followup_deadline:
                self._clear_followup_mode()

            match = _WAKE_PATTERN.search(transcript)

            if match:
                matched_variant = match.group(1)
                self._refresh_followup_mode()
                label = "BARGE-IN" if captured_while_speaking else "TRIGGERED"
                print(f"WakeWord {label} ({matched_variant!r}): {transcript!r}")
                command = transcript[match.end():].lstrip(" ,.:!?").strip()
            elif captured_while_speaking:
                # ARYA is talking — a wake-word match or a bare stop phrase are
                # the only things that count as an interrupt. Lenient
                # follow-up matching otherwise stays off to avoid her own
                # voice (picked up as mic bleed) triggering herself.
                cleaned = transcript.strip().lower().strip(" .!?,")
                if cleaned in BARGE_IN_STOP_PHRASES:
                    print(f"WakeWord BARGE-IN stop (no wake word): {transcript!r}")
                    command = cleaned
                else:
                    print(f"WakeWord ignored while speaking (no clear wake word): {transcript!r}")
                    return
            elif self._followup_mode_enabled():
                self._refresh_followup_mode()
                print(f"WakeWord FOLLOW-UP: {transcript!r}")
                command = transcript
            else:
                print(f"WakeWord heard (no match): {transcript!r}")
                return

            print(f"WakeWord command: {command!r}")

            if self._callback and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._callback(command, bool(match)),
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
