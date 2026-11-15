"""M5: echo cancellation — ARYA's own voice removed from the microphone, yours kept."""
import numpy as np
import pytest

from arya.core.echo import FRAME, SR, EchoCanceller

pytest.importorskip("livekit.rtc")

PLAY_RATE = 24_000
MIC_CHUNK = 1280                  # 80 ms, like the hotword engine


def speechlike(seconds: float, seed: int) -> np.ndarray:
    """Band-limited noise with a syllable rhythm — close enough to speech for an echo canceller."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    noise = np.convolve(rng.normal(0, 1, n), np.hanning(9), mode="same")
    t = np.arange(n) / SR
    envelope = np.clip(np.sin(2 * np.pi * 3.3 * t + rng.uniform(0, 6)), 0, None) ** 1.5
    x = noise * envelope
    return (0.3 * x / np.max(np.abs(x))).astype(np.float32)


def simulate(aec: EchoCanceller, far16: np.ndarray, near: np.ndarray) -> np.ndarray:
    """Speaker plays far16 (fed at 24 kHz in its own chunk sizes); the mic hears its echo + near."""
    ir = np.zeros(int(0.12 * SR) + 300, np.float32)
    ir[int(0.12 * SR)] = 0.5
    ir[int(0.12 * SR) + 90] = 0.15
    mic = np.convolve(far16, ir)[: far16.size] + near
    far24 = np.interp(np.linspace(0, far16.size - 1, int(far16.size * PLAY_RATE / SR)), np.arange(far16.size), far16)
    out = np.zeros_like(mic)
    per_mic_chunk = int(MIC_CHUNK * PLAY_RATE / SR)                 # 1920 speaker samples per 80 ms
    for k, i in enumerate(range(0, mic.size - MIC_CHUNK + 1, MIC_CHUNK)):
        played = far24[k * per_mic_chunk:(k + 1) * per_mic_chunk]
        for j in range(0, played.size, 512):                         # the speaker callback's own block size
            aec.feed_reference(played[j:j + 512], PLAY_RATE)
        ref = aec.take_reference(MIC_CHUNK)                          # in the mic callback
        out[i:i + MIC_CHUNK] = aec.process(mic[i:i + MIC_CHUNK].astype(np.float32), ref)
    return mic, out


def db(a, b):
    return 10 * np.log10((np.mean(a ** 2) + 1e-12) / (np.mean(b ** 2) + 1e-12))


def test_arya_voice_is_removed_from_the_mic():
    aec = EchoCanceller()
    far = speechlike(10, seed=1)
    mic, out = simulate(aec, far, np.zeros_like(far))
    settled = slice(SR * 4, SR * 10)                                 # after the canceller has adapted
    assert db(mic[settled], out[settled]) > 25                       # echo at least 25 dB weaker


def test_your_voice_survives_double_talk():
    aec = EchoCanceller()
    far = speechlike(10, seed=2)
    near = np.zeros_like(far)
    you = speechlike(2, seed=3) * 0.8
    near[SR * 7:SR * 9] = you
    mic, out = simulate(aec, far, near)
    talk = slice(SR * 7, SR * 9)
    assert np.mean(np.abs(out[talk])) > 0.4 * np.mean(np.abs(near[talk]))      # you are still clearly there


def test_reference_bookkeeping():
    aec = EchoCanceller()
    aec.feed_reference(np.ones(240, np.float32), PLAY_RATE)              # 10 ms at 24 kHz -> 160 at 16 kHz
    ref = aec.take_reference(FRAME * 2)
    assert ref[:FRAME].min() > 0.99 and np.all(ref[FRAME:] == 0)       # padded with silence
    aec.feed_reference(np.ones(SR * 3, np.float32), SR)                 # mic stalled: keep at most 1 s
    assert aec._ref_len <= SR


def test_disabled_passes_audio_through(monkeypatch):
    monkeypatch.setenv("ARYA_AEC", "0")
    aec = EchoCanceller()
    mic = speechlike(0.08, seed=4)
    assert not aec.available() and np.array_equal(aec.process(mic, np.zeros_like(mic)), mic)
