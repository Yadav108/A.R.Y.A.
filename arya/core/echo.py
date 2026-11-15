"""
Echo cancellation (roadmap M5): ARYA stops hearing itself.

The speaker hands every chunk it plays to feed_reference(); the microphone callback takes the
matching stretch of that reference in real time (take_reference) and the hotword worker runs both
through WebRTC's AEC3 (LiveKit's AudioProcessingModule) in 10 ms frames. ARYA's own voice is
removed from the mic signal, so "stop", "Hey ARYA" and talking over ARYA work while it speaks.

Measured on simulated audio (ARYA's voice echoed with 120 ms delay + reflections, you talking over
it): echo reduced by 47 dB; your voice comes through clearer than without (correlation 0.64 vs 0.48).

Only echo cancellation and a high-pass filter run — no noise suppression or gain control, so the
wake-word detector, trained on plain audio, sees what it expects. ARYA_AEC=0 turns it off.
"""
from __future__ import annotations

import os
import threading
from collections import deque

import numpy as np

SR = 16_000
FRAME = 160                                    # 10 ms at 16 kHz — what the module works in
MAX_REF_S = 1.0                                # keep at most 1 s of unplayed reference


class EchoCanceller:
    def __init__(self) -> None:
        self.enabled = os.getenv("ARYA_AEC", "1") != "0"
        self._apm = None
        self._rtc = None
        self._ref: deque = deque()
        self._ref_len = 0
        self._lock = threading.Lock()
        self._tried = False
        self._carry = np.zeros(0, np.float32)

    def available(self) -> bool:
        if not self.enabled:
            return False
        if not self._tried:
            self._tried = True
            try:
                from livekit import rtc
                self._rtc = rtc
                self._apm = rtc.AudioProcessingModule(echo_cancellation=True, high_pass_filter=True)
                print("Echo cancellation: on (WebRTC AEC3)")
            except Exception as exc:
                print(f"Echo cancellation unavailable ({exc}); ARYA's own voice is filtered by loudness only")
        return self._apm is not None

    # ── Reference (what the speaker plays) ─────────────────────────────────────

    def feed_reference(self, pcm: np.ndarray, rate: int) -> None:
        """Called from the speaker's audio callback with each chunk as it is played."""
        if not self.enabled or pcm.size == 0:
            return
        if rate != SR:
            n = max(1, int(round(pcm.size * SR / rate)))
            pcm = np.interp(np.linspace(0, pcm.size - 1, n), np.arange(pcm.size), pcm)
        pcm = pcm.astype(np.float32)
        with self._lock:
            self._ref.append(pcm)
            self._ref_len += pcm.size
            while self._ref_len > MAX_REF_S * SR and self._ref:          # mic stalled or muted: drop old
                self._ref_len -= self._ref.popleft().size

    def take_reference(self, n: int) -> np.ndarray:
        """Called from the microphone callback: the reference for these n samples (silence if none)."""
        out = np.zeros(n, np.float32)
        if not self.enabled:
            return out
        filled = 0
        with self._lock:
            while filled < n and self._ref:
                head = self._ref[0]
                take = min(n - filled, head.size)
                out[filled:filled + take] = head[:take]
                filled += take
                if take == head.size:
                    self._ref.popleft()
                else:
                    self._ref[0] = head[take:]
                self._ref_len -= take
        return out

    # ── Processing ────────────────────────────────────────────────────────────

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """Mic chunk (float32, 16 kHz) with ARYA's voice removed. Unchanged if AEC is unavailable."""
        if not self.available() or mic.size % FRAME:
            return mic
        rtc = self._rtc
        to16 = lambda a: (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)
        out = np.empty_like(mic)
        for i in range(0, mic.size, FRAME):
            self._apm.process_reverse_stream(rtc.AudioFrame(to16(ref[i:i + FRAME]).tobytes(), SR, 1, FRAME))
            frame = rtc.AudioFrame(to16(mic[i:i + FRAME]).tobytes(), SR, 1, FRAME)
            self._apm.process_stream(frame)
            out[i:i + FRAME] = np.frombuffer(bytes(frame.data), np.int16).astype(np.float32) / 32768
        return out


canceller = EchoCanceller()
