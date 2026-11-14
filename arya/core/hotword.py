"""
Dedicated "Hey ARYA" wake-word engine.

Why not Whisper? The old engine transcribed *everything* the mic heard with a
small Whisper model and searched the text for "arya". On a real mic that model
hears the name as "I am" / "I do not know", clips the start of utterances, and
burns CPU + RAM transcribing background chatter all day.

This engine runs a tiny keyword-spotting model instead:
  mic (80 ms chunks) → openWakeWord speech embeddings → ARYA's trained MLP head
  → score. Only *after* the wake word fires is anything transcribed, with the
  accurate Whisper medium model, in two steps:

  1. Verify — the ~1.8 s of audio that triggered the detector is transcribed
     and must actually contain "ARYA" (or a close spelling). This rejects the
     detector's sound-alike hits ("I am", "Iron the shirt…") silently.
  2. Command — the speech after the wake word is transcribed and sent on.

Once a conversation has started, follow-ups need no wake word for
CONVERSATION_MINUTES (default 8) after the last exchange; "that's all",
"thanks" or "bye" end it sooner.

Model: arya/models/wakeword/hey_arya.npz, trained by tools/train_wakeword.py
(improve it for your voice with tools/record_wakeword.py, then retrain).
"""
from __future__ import annotations

import asyncio
import os
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np
import sounddevice as sd

# Shared settings live in arya.core.wakeword, which itself imports this module
# to pick the engine — so import it lazily (at call time) to avoid a cycle.
def _ww():
    from arya.core import wakeword
    return wakeword

MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / "wakeword"
MODEL_FILE = MODEL_DIR / "hey_arya.npz"
MELSPEC_FILE = MODEL_DIR / "melspectrogram.onnx"
EMBEDDING_FILE = MODEL_DIR / "embedding_model.onnx"

SR = 16_000
CHUNK = 1280                       # 80 ms — openWakeWord's native step

ENERGY_FLOOR = float(os.getenv("WAKE_ENERGY_FLOOR", "0.012"))  # min RMS that counts as speech
WAIT_FOR_COMMAND_S = 1.2           # silence after "Hey ARYA" → treat as bare wake ("Yes?")
END_SILENCE_S = 0.72               # pause that ends a command
MAX_CAPTURE_S = 15.0
TRIGGER_COOLDOWN_S = 1.5
PREROLL_CHUNKS = 5                 # 400 ms kept before a follow-up starts
MIN_FOLLOWUP_SPEECH_CHUNKS = 3     # ≥240 ms of voiced audio before a follow-up is transcribed

# Second check: transcribe the audio that triggered the detector and require the name.
VERIFY = os.getenv("WAKE_VERIFY", "1") != "0"
VERIFY_WINDOW_CHUNKS = 23          # ~1.8 s before the trigger — holds "Hey ARYA"
VERIFY_TIMEOUT_S = 8.0
# Spellings Whisper uses for the name. "area"/"are ya" are accepted here because
# this only runs after the acoustic detector already fired on the audio.
_VERIFY_PATTERN = re.compile(
    r"(?<!\w)(?:"
    r"a+r+i?y+a+h?|ari+a+h?|ar+ia|aar?iya|ariya|arja|area|are\s?ya|ayra|harya"   # not "Aryan"
    r")(?!\w)",
    re.IGNORECASE,
)

# Follow-ups that are just noise/filler — never sent to the LLM.
_FILLER = {
    "", "uh", "um", "umm", "hmm", "hm", "mm", "mhm", "mm-hmm", "uh-huh", "ah", "oh", "huh", "er", "erm",
    "you", "thanks for watching", "thank you for watching", "subtitles by the amara.org community",
}


def model_available() -> bool:
    return MODEL_FILE.exists() and MELSPEC_FILE.exists() and EMBEDDING_FILE.exists()


def conversation_seconds() -> float:
    """How long the hands-free conversation stays open after the last exchange."""
    try:
        return max(0.2, float(os.getenv("CONVERSATION_MINUTES", "8"))) * 60.0
    except ValueError:
        return 480.0


def is_wake_word(text: str) -> bool:
    return bool(_VERIFY_PATTERN.search(text or ""))


def _is_filler(text: str) -> bool:
    return re.sub(r"[^\w\s'-]", "", text).strip().lower() in _FILLER


class HotwordDetector:
    """Streaming scorer: feed 80 ms chunks, get P(wake word just ended)."""

    def __init__(self) -> None:
        from openwakeword.utils import AudioFeatures

        self.features = AudioFeatures(
            melspec_model_path=str(MELSPEC_FILE),
            embedding_model_path=str(EMBEDDING_FILE),
            inference_framework="onnx",
        )
        params = np.load(MODEL_FILE)
        self.W1 = params["W1"].astype(np.float32)
        self.b1 = params["b1"].astype(np.float32)
        self.W2 = params["W2"].astype(np.float32)
        self.b2 = params["b2"].astype(np.float32)
        self.n_frames = int(params["n_frames"])
        self.threshold = float(os.getenv("WAKE_THRESHOLD", float(params["threshold"])))
        # A real "Hey ARYA" scores high for several 80 ms frames in a row;
        # one-frame blips on other speech are ignored.
        default_frames = int(params["trigger_frames"]) if "trigger_frames" in params.files else 1
        self.trigger_frames = int(os.getenv("WAKE_TRIGGER_FRAMES", default_frames))

    def score(self, chunk: np.ndarray) -> float:
        pcm = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16)
        self.features(pcm)
        x = self.features.get_features(self.n_frames).reshape(1, -1)
        h = np.maximum(0.0, x @ self.W1 + self.b1)
        z = float((h @ self.W2 + self.b2).ravel()[0])
        return 1.0 / (1.0 + np.exp(-z))

    def reset(self) -> None:
        self.features.reset()


def _strip_wake_words(text: str) -> str:
    """Whisper sometimes catches the tail of the wake word ("Arya, what time…")."""
    leading = re.compile(r"^\W*(?:(?:hey|hi|ok|okay)\W+)?" + _ww()._WAKE_PATTERN.pattern + r"\W*", re.IGNORECASE)
    return leading.sub("", text).strip()


def _transcribe(audio: np.ndarray) -> str:
    from arya.core.stt import transcribe_array
    return transcribe_array(audio)


def _transcribe_wake(audio: np.ndarray) -> str:
    from arya.core.stt import transcribe_wake
    return transcribe_wake(audio)


class HotwordEngine:
    """Drop-in replacement for WakeWordEngine (same public API)."""

    def __init__(self) -> None:
        self._callback: Callable[[str, bool], Awaitable[None]] | None = None
        self._interrupt: Callable[[], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._speaking = False
        self._speaking_tail_until = 0.0
        self._followup_active = False
        self._followup_deadline = 0.0
        self._noise_floor = ENERGY_FLOOR / 3
        self._detector: HotwordDetector | None = None
        # State for the chunk loop (only touched by the worker thread)
        self._capture: dict | None = None
        self._cooldown_until = 0.0
        self._hot_run = 0                                   # consecutive frames at/above threshold
        self._preroll: deque = deque(maxlen=PREROLL_CHUNKS)
        self._history: deque = deque(maxlen=VERIFY_WINDOW_CHUNKS)
        self._muted = False
        self._event: Callable[[str], None] | None = None

    # ── Public API (matches WakeWordEngine) ──────────────────────────────────

    def set_callback(self, callback: Callable[[str, bool], Awaitable[None]], loop: asyncio.AbstractEventLoop) -> None:
        self._callback = callback
        self._loop = loop

    def set_interrupt_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Called as soon as a "Hey ARYA" heard while ARYA is talking is
        verified, so she goes quiet while the user says the rest."""
        self._interrupt = callback

    def set_speaking(self, state: bool) -> None:
        if self._speaking and not state:
            self._speaking_tail_until = time.monotonic() + _ww().SPEAKING_TAIL_SECONDS
            if self._followup_active:
                self._refresh_followup_mode()
        self._speaking = state

    def followup_active(self) -> bool:
        return self._followup_mode_enabled()

    def set_muted(self, muted: bool) -> None:
        """Microphone off: nothing is detected, captured or transcribed."""
        self._muted = muted
        if muted:
            self._capture = None
            self._hot_run = 0
            self.end_conversation()
        print(f"WakeWord: microphone {'muted' if muted else 'live'}")

    def is_muted(self) -> bool:
        return self._muted

    def set_event_callback(self, callback: Callable[[str], None]) -> None:
        """Called (from the audio thread) with 'ARYA_LISTENING' the moment a
        wake word is verified, so a status UI can react before transcription."""
        self._event = callback

    def followup_seconds_left(self) -> float:
        return max(0.0, self._followup_deadline - time.monotonic()) if self._followup_mode_enabled() else 0.0

    def end_conversation(self) -> None:
        if self._followup_active:
            print("WakeWord: conversation ended — say 'Hey ARYA' to start again")
        self._followup_active = False
        self._followup_deadline = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotword")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    # ── Internals ────────────────────────────────────────────────────────────

    def _refresh_followup_mode(self) -> None:
        self._followup_active = True
        self._followup_deadline = time.monotonic() + conversation_seconds()

    def _followup_mode_enabled(self) -> bool:
        if self._followup_active and time.monotonic() >= self._followup_deadline:
            self._followup_active = False
            print("WakeWord: conversation timed out — say 'Hey ARYA' to start again")
        return self._followup_active

    def _is_ducked(self) -> bool:
        return self._speaking or time.monotonic() < self._speaking_tail_until

    def _post(self, coro) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _run(self) -> None:
        self._detector = HotwordDetector()
        chunks: queue.Queue[np.ndarray] = queue.Queue(maxsize=256)

        def on_audio(indata, frames, time_info, status) -> None:
            try:
                chunks.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass  # worker fell behind — drop audio rather than block the driver

        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=CHUNK, callback=on_audio):
            print(f"WakeWord: listening for 'Hey ARYA' (dedicated detector, threshold "
                  f"{self._detector.threshold:.2f} x{self._detector.trigger_frames}, "
                  f"verify {'on' if VERIFY else 'off'}, conversation {conversation_seconds() / 60:.0f} min)")
            while self._running:
                try:
                    chunk = chunks.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    self._on_chunk(chunk)
                except Exception as exc:
                    print(f"WakeWord error: {exc}")

    def _speech_threshold(self) -> float:
        return max(ENERGY_FLOOR, self._noise_floor * 3.0)

    def _on_chunk(self, chunk: np.ndarray) -> None:
        if self._muted:
            return
        now = time.monotonic()
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        ducked = self._is_ducked()
        score = self._detector.score(chunk)
        self._history.append(chunk)

        if self._capture is not None:
            self._capture_step(chunk, rms, now)
            self._preroll.append(chunk)
            return

        threshold = self._detector.threshold
        if ducked:
            # ARYA's own voice is in the mic — demand a much more confident hit.
            threshold = threshold + (1.0 - threshold) * 0.5

        self._hot_run = self._hot_run + 1 if score >= threshold else 0
        needed = getattr(self._detector, "trigger_frames", 1)
        if self._hot_run >= needed and now >= self._cooldown_until:
            self._hot_run = 0
            self._cooldown_until = now + TRIGGER_COOLDOWN_S
            print(f"WakeWord: detector fired (score {score:.2f}{', while speaking' if ducked else ''})")
            cap = {"kind": "wake", "audio": [], "start": now, "speech": False, "silence": 0, "voiced": 0,
                   "ducked": ducked, "verified": threading.Event(), "ok": not VERIFY, "aborted": False}
            self._capture = cap
            self._preroll.clear()
            if VERIFY:
                wake_audio = np.concatenate(list(self._history))
                threading.Thread(target=self._verify, args=(cap, wake_audio), daemon=True).start()
            else:
                cap["verified"].set()
                self._accept_wake(cap)
            return

        speechy = rms > self._speech_threshold()
        if not ducked and not speechy:
            # Track the room's noise floor while nobody is talking.
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * rms

        if not ducked and speechy and self._followup_mode_enabled():
            self._capture = {"kind": "followup", "audio": list(self._preroll) + [chunk], "start": now,
                             "speech": True, "silence": 0, "voiced": 1, "aborted": False}
            self._preroll.clear()
            return

        self._preroll.append(chunk)

    def _accept_wake(self, cap: dict) -> None:
        if self._event is not None:
            try:
                self._event("ARYA_LISTENING")
            except Exception:
                pass
        self._refresh_followup_mode()
        if cap["ducked"] and self._interrupt is not None:
            self._post(self._interrupt())

    def _verify(self, cap: dict, wake_audio: np.ndarray) -> None:
        heard = ""
        try:
            heard = _transcribe_wake(wake_audio)
        except Exception as exc:
            print(f"WakeWord: verification failed ({exc}) — accepting the detector")
            heard = "arya"
        cap["ok"] = is_wake_word(heard)
        if cap["ok"]:
            print(f"WakeWord TRIGGERED (verified: {heard!r})")
            self._accept_wake(cap)
        else:
            cap["aborted"] = True
            print(f"WakeWord: rejected — heard {heard!r}, not 'ARYA'")
        cap["verified"].set()

    def _capture_step(self, chunk: np.ndarray, rms: float, now: float) -> None:
        cap = self._capture
        if cap.get("aborted"):
            self._capture = None        # verification said it wasn't the wake word
            return
        cap["audio"].append(chunk)
        if rms > self._speech_threshold():
            cap["speech"] = True
            cap["silence"] = 0
            cap["voiced"] += 1
        elif cap["speech"]:
            cap["silence"] += 1

        elapsed = now - cap["start"]
        end_silence = int(END_SILENCE_S * SR / CHUNK)
        done = (
            (cap["kind"] == "wake" and not cap["speech"] and elapsed >= WAIT_FOR_COMMAND_S)
            or (cap["speech"] and cap["silence"] >= end_silence)
            or elapsed >= MAX_CAPTURE_S
        )
        if done:
            self._capture = None
            threading.Thread(target=self._finish, args=(cap,), daemon=True).start()

    def _finish(self, cap: dict) -> None:
        is_wake = cap["kind"] == "wake"
        if is_wake:
            cap["verified"].wait(VERIFY_TIMEOUT_S)
            if not cap["ok"]:
                return
        elif cap["voiced"] < MIN_FOLLOWUP_SPEECH_CHUNKS:
            return                          # a click or a cough

        command = ""
        if cap["speech"]:
            try:
                command = _transcribe(np.concatenate(cap["audio"]))
            except Exception as exc:
                print(f"WakeWord: transcription failed ({exc})")
            command = _strip_wake_words(command)

        if not is_wake:
            if _is_filler(command):
                if command:
                    print(f"WakeWord: ignored filler {command!r}")
                return
            # No refresh here: the server decides whether this was meant for
            # ARYA. A real exchange keeps the conversation open (set_speaking
            # refreshes it after the reply); ignored chatter doesn't.
            print(f"WakeWord FOLLOW-UP: {command!r}")
        else:
            print(f"WakeWord command: {command!r}")

        if self._callback is not None:
            self._post(self._callback(command, is_wake))
