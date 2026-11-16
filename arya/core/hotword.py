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
STALE_SPEECH_S = 8.0               # speech waiting longer than this for Whisper is dropped, not answered late
_STT_GATE = threading.Semaphore(1)

# Second check: transcribe the audio that triggered the detector and require the name.
VERIFY = os.getenv("WAKE_VERIFY", "1") != "0"
VERIFY_WINDOW_CHUNKS = 23          # ~1.8 s before the trigger — holds "Hey ARYA"
VERIFY_TIMEOUT_S = 8.0
# Spellings Whisper uses for the name. "area"/"are ya" are accepted here because
# this only runs after the acoustic detector already fired on the audio.
_VERIFY_PATTERN = re.compile(
    r"(?<!\w)(?:"
    r"a+r+i?y+a+h?|ari+a+h?|ar+ia|aar?iya|ariya|arja|area|are\s?ya|ayra|harya"   # not "Aryan"
    r"|n\.?\s?j\.?|en\s?jay|enjay|enjae|n\s?jay"                                   # "Hey NJ" (second wake word)
    r")(?!\w)",
    re.IGNORECASE,
)

# Bare "stop" while ARYA is talking (no wake word). ARYA's own voice reaches the
# mic too, so only a short burst clearly louder than its recent playback level
# counts, and only if the burst transcribes to nothing but a stop phrase.
STOP_BARGE_IN = os.getenv("ARYA_BARGE_IN_STOP", "1") != "0"
STOP_LOUDER_THAN_ECHO = float(os.getenv("BARGE_IN_STOP_RATIO", "1.8"))  # x ARYA's loud (90th pct) level
STOP_MAX_VOICED_CHUNKS = 25        # > 2 s of loud audio isn't a "stop" — it's ARYA (or a conversation)
COMMAND_MAX_VOICED_CHUNKS = 60     # ≤ ~5 s: may be a command said over ARYA (checked against its own words)
STOP_MIN_VOICED_CHUNKS = 2         # ≥ 160 ms
STOP_END_SILENCE_CHUNKS = 5        # 400 ms below the gate ends the burst
ECHO_HISTORY_CHUNKS = 75           # ~6 s of ARYA's playback level, kept across replies

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


def extra_models() -> list[Path]:
    """Other trained wake words next to hey_arya.npz (e.g. hey_nj.npz); backups (*.prev.npz) don't count."""
    if os.getenv("WAKE_EXTRA_WORDS", "1") == "0":
        return []
    return sorted(p for p in MODEL_DIR.glob("hey_*.npz") if p.name != MODEL_FILE.name and ".prev" not in p.name)


class _Head:
    """One wake word's classifier on the shared openWakeWord speech features."""

    def __init__(self, path: Path, main: bool = False) -> None:
        params = np.load(path)
        self.word = path.stem.removeprefix("hey_")
        self.W1 = params["W1"].astype(np.float32)
        self.b1 = params["b1"].astype(np.float32)
        self.W2 = params["W2"].astype(np.float32)
        self.b2 = params["b2"].astype(np.float32)
        self.n_frames = int(params["n_frames"])
        thr = float(params["threshold"])
        self.threshold = float(os.getenv("WAKE_THRESHOLD", thr)) if main else thr
        # A real "Hey ARYA" scores high for several 80 ms frames in a row; one-frame blips are ignored.
        frames = int(params["trigger_frames"]) if "trigger_frames" in params.files else 1
        self.trigger_frames = int(os.getenv("WAKE_TRIGGER_FRAMES", frames)) if main else frames
        t = min(max(self.threshold, 1e-4), 1 - 1e-4)
        self._logit_threshold = float(np.log(t / (1 - t)))

    def logit(self, features) -> float:
        x = features.get_features(self.n_frames).reshape(1, -1)
        h = np.maximum(0.0, x @ self.W1 + self.b1)
        return float((h @ self.W2 + self.b2).ravel()[0])

    def probability(self, features) -> float:
        return 1.0 / (1.0 + np.exp(-self.logit(features)))

    def relative(self, features) -> float:
        """On a scale where this word's own threshold is 0.5 (so different words can be compared)."""
        return 1.0 / (1.0 + np.exp(-(self.logit(features) - self._logit_threshold)))


class HotwordDetector:
    """Streaming scorer: feed 80 ms chunks, get P(wake word just ended)."""

    def __init__(self) -> None:
        from openwakeword.utils import AudioFeatures

        self.features = AudioFeatures(
            melspec_model_path=str(MELSPEC_FILE),
            embedding_model_path=str(EMBEDDING_FILE),
            inference_framework="onnx",
        )
        # "Hey ARYA" plus any other trained wake word ("Hey NJ": hey_nj.npz) — one small head each on the
        # same speech features, so a second word costs almost nothing.
        self.heads = [_Head(MODEL_FILE, main=True)] + [_Head(p) for p in extra_models()]
        main = self.heads[0]
        self.W1, self.b1, self.W2, self.b2, self.n_frames = main.W1, main.b1, main.W2, main.b2, main.n_frames
        if len(self.heads) == 1:
            self.threshold = main.threshold
            self.trigger_frames = main.trigger_frames
        else:
            # Several words: each score is put on a common scale where its own threshold is 0.5.
            self.threshold = 0.5
            self.trigger_frames = min(h.trigger_frames for h in self.heads)
        self.last_word = main.word

    def score(self, chunk: np.ndarray) -> float:
        pcm = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16)
        self.features(pcm)
        if len(self.heads) == 1:
            return self.heads[0].probability(self.features)
        best, word = 0.0, self.heads[0].word
        for h in self.heads:
            s = h.relative(self.features)
            if s > best:
                best, word = s, h.word
        self.last_word = word
        return best

    def reset(self) -> None:
        self.features.reset()


def _strip_wake_words(text: str) -> str:
    """Whisper sometimes catches the tail of the wake word ("Arya, what time…")."""
    leading = re.compile(r"^\W*(?:(?:hey|hi|ok|okay)\W+)?" + _ww()._WAKE_PATTERN.pattern + r"\W*", re.IGNORECASE)
    text = leading.sub("", text).strip()
    from arya.core.addressee import misheard_nj
    m = misheard_nj(text)                          # "Enjoy! How are you?" was "NJ, how are you?"
    return text[m.end():].strip() if m else text


# Words people wrap around "stop" when they interrupt ("Uh, stop.", "Okay, let's stop audio.").
_STOP_FILLER = set("""uh um umm erm er ah oh hmm okay ok hey yo bro dude please now just so well yeah right
lets let's let us can could would will you it its it's that talking speaking audio there arya aria ariya aarya
area harya hi i said""".split())
_STOP_WORDS = {"stop", "quiet", "silence", "enough", "cancel", "shush", "hush", "shut", "up", "be", "that's",
               "thats", "never", "mind", "nevermind", "pause", "wait"}
_NEGATION = re.compile(r"\b(?:don'?t|do not|not|can'?t|won'?t|doesn'?t|didn'?t|never stop)\b")
_STOP_CORE = {"stop", "quiet", "silence", "enough", "cancel", "shush", "hush", "shut", "nevermind", "pause"}


def stop_words_in(text: str) -> set[str]:
    words = set(re.findall(r"[a-z']+", (text or "").lower()))
    return (words & _STOP_CORE) | ({"never mind"} if "never mind" in (text or "").lower() else set())


def is_stop_phrase(text: str) -> bool:
    """'Stop.', 'Uh, stop.', 'Okay, let's stop audio.', 'no no stop', 'ARYA, be quiet' -> True;
    'stop the motor', "don't stop", 'stop talking about it', 'No.' -> False."""
    t = re.sub(r"[^\w\s']", " ", _strip_wake_words(text or "")).lower()
    t = re.sub(r"\s+", " ", t).strip()
    if not t or _NEGATION.search(t):
        return False
    words = [w for w in t.split() if w not in _STOP_FILLER]
    if not words:
        return False
    if all(w == "no" for w in words):
        return len(words) >= 2                          # "no, no, no" — a lone "No." could be anything
    words = [w for w in words if w != "no"]
    if not words or not all(w in _STOP_WORDS for w in words):
        return False                                    # "stop the motor": a real sentence, not an interrupt
    present = set(words)
    return bool(present & _STOP_CORE) or {"never", "mind"} <= present


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
        self._echo_levels: deque = deque(maxlen=ECHO_HISTORY_CHUNKS)   # mic level while ARYA talks
        self._recent_spoken: deque = deque(maxlen=4)                  # (time, sentence) ARYA just said

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

    def note_spoken(self, sentence: str) -> None:
        """The server tells us each sentence as ARYA starts saying it, so a 'stop' heard
        while ARYA itself is saying 'stop' isn't mistaken for the user."""
        self._recent_spoken.append((time.monotonic(), sentence or ""))

    def _arya_said_stop_recently(self, seconds: float = 6.0) -> set[str]:
        now = time.monotonic()
        said: set[str] = set()
        for t, s in self._recent_spoken:
            if now - t <= seconds:
                said |= stop_words_in(s)
        return said

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
        # Also by the wall clock: on Windows time.monotonic() (QueryPerformanceCounter) can stand still while
        # the laptop sleeps — from the log, a conversation opened at 04:41 was still open at 17:29.
        self._followup_wall_deadline = time.time() + conversation_seconds()

    def _followup_mode_enabled(self) -> bool:
        if self._followup_active and (time.monotonic() >= self._followup_deadline
                                      or time.time() >= getattr(self, "_followup_wall_deadline", float("inf"))):
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
        chunks: queue.Queue = queue.Queue(maxsize=256)
        from arya.core.echo import canceller
        aec = canceller.available()

        def on_audio(indata, frames, time_info, status) -> None:
            mic = indata[:, 0].copy()
            # The reference is taken *now*, in step with this chunk, even if the worker lags behind.
            ref = canceller.take_reference(mic.size) if aec else None
            try:
                chunks.put_nowait((mic, ref))
            except queue.Full:
                pass  # worker fell behind — drop audio rather than block the driver

        with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=CHUNK, callback=on_audio):
            words = " / ".join(f"'Hey {h.word.upper()}'" for h in self._detector.heads)
            print(f"WakeWord: listening for {words} (dedicated detector, threshold "
                  f"{self._detector.threshold:.2f} x{self._detector.trigger_frames}, "
                  f"verify {'on' if VERIFY else 'off'}, conversation {conversation_seconds() / 60:.0f} min)")
            while self._running:
                try:
                    chunk, ref = chunks.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    if ref is not None:
                        chunk = canceller.process(chunk, ref)      # ARYA's own voice removed
                    self._on_chunk(chunk)
                except Exception as exc:
                    try:                                    # the listening loop must outlive its own error report
                        print(f"WakeWord error: {type(exc).__name__}: {exc}")
                    except Exception:
                        pass

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

        if self._capture is not None and self._capture["kind"] != "stop":
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
            word = getattr(self._detector, "last_word", "arya")
            print(f"WakeWord: detector fired (score {score:.2f}{', while speaking' if ducked else ''}"
                  f"{'' if word == 'arya' else f', {word}'})")
            # (a "Hey ARYA" takes over from a stop burst being captured)
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

        if self._capture is not None:                     # a possible "stop" burst
            self._stop_step(chunk, rms)
            return

        if ducked and STOP_BARGE_IN and self._interrupt_or_callback():
            gate = self._stop_gate()
            if rms > gate:
                self._capture = {"kind": "stop", "audio": list(self._preroll)[-2:] + [chunk], "voiced": 1,
                                 "silence": 0, "gate": gate, "levels": [rms]}
                self._preroll.clear()
                return
            self._echo_levels.append(rms)                  # learn how loud ARYA itself is in the mic

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

    # ── Bare "stop" while ARYA is talking ───────────────────────────────────

    def _interrupt_or_callback(self) -> bool:
        return self._callback is not None

    def _stop_gate(self) -> float:
        """How loud a burst must be to be you, not ARYA's own voice in the mic."""
        floor = self._speech_threshold() * 2.0
        if len(self._echo_levels) < 10:
            return max(floor, self._speech_threshold() * _ww().BARGE_IN_DUCK_MULTIPLIER)   # not learned yet
        loud = float(np.percentile(np.fromiter(self._echo_levels, float), 90))
        return max(floor, loud * STOP_LOUDER_THAN_ECHO)

    def _stop_step(self, chunk: np.ndarray, rms: float) -> None:
        cap = self._capture
        cap["audio"].append(chunk)
        if rms > cap["gate"] * 0.6:                        # a little hysteresis inside a word
            cap["voiced"] += 1
            cap["silence"] = 0
            cap["levels"].append(rms)
        else:
            cap["silence"] += 1
        if cap["voiced"] > COMMAND_MAX_VOICED_CHUNKS:
            # Long and loud: ARYA's own voice (louder than learned) or someone talking.
            # Learn from it so ARYA's playback doesn't keep opening bursts.
            self._echo_levels.extend(cap["levels"])
            self._capture = None
            return
        if cap["silence"] >= STOP_END_SILENCE_CHUNKS:
            self._capture = None
            if cap["voiced"] >= STOP_MIN_VOICED_CHUNKS:
                threading.Thread(target=self._finish_stop, args=(cap,), daemon=True).start()

    def _is_barge_in_command(self, heard: str) -> bool:
        """A clear request for ARYA heard while it talks — and not ARYA's own words coming back."""
        if not heard or len(re.findall(r"[A-Za-z]{2,}", heard)) < 2:
            return False
        try:
            from arya.core import addressee
            if addressee._ALWAYS_FOR_ARYA.match(heard.strip()):
                return False                                        # "thank you", "yes", "okay": not a new request
            rest, named = addressee.split_name(heard)
            if not (named or addressee.quick_verdict(heard) is True):
                return False
        except Exception:
            return False
        words = set(re.findall(r"[a-z']{3,}", heard.lower()))
        now = time.monotonic()
        for t, said in self._recent_spoken:                         # its own sentence through the mic
            if now - t < 15 and words:
                overlap = len(words & set(re.findall(r"[a-z']{3,}", said.lower()))) / len(words)
                if overlap >= 0.5:
                    return False
        return True

    def _finish_stop(self, cap: dict) -> None:
        try:
            heard = _transcribe(np.concatenate(cap["audio"]))
        except Exception as exc:
            print(f"WakeWord: stop check failed ({exc})")
            return
        if is_stop_phrase(heard) and stop_words_in(heard) & self._arya_said_stop_recently():
            print(f"WakeWord: ignored {heard!r} — ARYA itself just said that word")
            return
        if is_stop_phrase(heard) and cap.get("voiced", 0) <= STOP_MAX_VOICED_CHUNKS:
            print(f"WakeWord BARGE-IN stop (no wake word): {heard!r}")
            if self._callback is not None:
                self._post(self._callback("stop", False))   # the server stops the reply at once
        elif self._is_barge_in_command(heard):
            # "Decrease the volume by 50 percent" said over ARYA's "Yes?" — a request, not noise.
            print(f"WakeWord BARGE-IN command: {heard!r}")
            if self._callback is not None:
                self._post(self._callback(_strip_wake_words(heard) or heard, False))
        else:
            if heard.strip():
                print(f"WakeWord: ignored while speaking (not a stop): {heard!r}")
            self._echo_levels.extend(cap["levels"][:5])      # most likely ARYA's voice — raise the gate a bit

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
            # One recognition at a time; speech that waited too long is dropped instead of answered minutes late
            # (from the log: with Whisper on the CPU, captures piled up and were answered 12 minutes later).
            if not _STT_GATE.acquire(timeout=STALE_SPEECH_S):
                print(f"WakeWord: dropped speech that waited over {STALE_SPEECH_S:.0f}s for recognition")
                return
            try:
                t0 = time.monotonic()
                command = _transcribe(np.concatenate(cap["audio"]))
                print(f"Latency: speech recognised in {time.monotonic() - t0:.2f}s")
            except Exception as exc:
                print(f"WakeWord: transcription failed ({exc})")
            finally:
                _STT_GATE.release()
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
