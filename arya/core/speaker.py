"""
Local speaker output — plays ARYA's voice through the PC's own speakers.

Why not the browser? Browser playback needs an open tab, can be blocked by
autoplay rules, and the server never knows when the audio actually *finished*
(it only knows when it finished sending). Playing locally means ARYA works
fully hands-free with no tab open, and the wake-word engine can duck/unduck
the mic at exactly the right moments.
"""
import asyncio
import threading
import time
from io import BytesIO

import numpy as np
import sounddevice as sd
from faster_whisper.audio import decode_audio

PLAYBACK_RATE = 24_000  # edge-tts native rate


def decode_mp3(mp3: bytes) -> np.ndarray:
    """MP3 bytes → mono float32 PCM at PLAYBACK_RATE (uses PyAV, already a
    faster-whisper dependency)."""
    if not mp3:
        return np.zeros(0, dtype=np.float32)
    return decode_audio(BytesIO(mp3), sampling_rate=PLAYBACK_RATE)


class LocalSpeaker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()   # one clip at a time

    def stop(self) -> None:
        """Cut off whatever is playing right now (barge-in / 'stop')."""
        self._stop.set()

    async def play(self, mp3: bytes) -> bool:
        """Play one clip. Returns False if it was interrupted."""
        if not mp3:
            return True
        try:
            return await asyncio.to_thread(self._play_sync, mp3)
        except asyncio.CancelledError:
            self.stop()
            raise

    def _play_sync(self, mp3: bytes) -> bool:
        pcm = decode_mp3(mp3)
        if pcm.size == 0:
            return True
        with self._lock:
            self._stop.clear()
            sd.play(pcm, PLAYBACK_RATE)
            deadline = time.monotonic() + pcm.size / PLAYBACK_RATE + 1.0
            try:
                while time.monotonic() < deadline:
                    if self._stop.is_set():
                        sd.stop()
                        return False
                    stream = sd.get_stream()
                    if not stream.active:
                        break
                    time.sleep(0.02)
            except RuntimeError:
                # No active stream (already finished) — nothing to wait for.
                pass
            return True


speaker = LocalSpeaker()
