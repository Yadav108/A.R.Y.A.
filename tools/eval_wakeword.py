"""
Measure a wake word end to end, the way ARYA uses it: the detector head fires (score >= threshold for
`trigger_frames` frames in a row, scored every 80 ms), then Whisper must actually hear the name in the
~1.8 s that triggered it. The raw "false triggers per hour" from training counts only the first stage.

    .venv\\Scripts\\python.exe tools\\eval_wakeword.py --word nj [--prompt "Hey Arya. Hey NJ."]

Reports, for the sound-alike clips synthesized during training (enjoy, Angie, DJ, in July, …), everyday
sentences, and your own recordings:
  detector hits          how often stage 1 fires
  false wakes            hits that also pass the Whisper name check (what you would actually notice)
  your "Hey NJ" caught   recall on your recordings, both stages
Needs ARYA stopped (it loads Whisper on the GPU).
"""
from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import train_wakeword as T                      # noqa: E402  (same word lists, cache and audio loading)

from arya.core import hotword, stt              # noqa: E402

PROMPT = sys.argv[sys.argv.index("--prompt") + 1] if "--prompt" in sys.argv else "Hey Arya."
PER_TEXT = 3                                    # clips per sound-alike / sentence


def run_clip(head: hotword._Head, fe, audio: np.ndarray) -> list[np.ndarray]:
    """Stream like live (2 s of silence first, then the clip, then 0.6 s): the 1.8 s windows where it fires."""
    fe.reset()
    stream = np.concatenate([np.zeros(2 * T.SR, np.float32), audio, np.zeros(int(0.6 * T.SR), np.float32)])
    run, cool, fires, hist = 0, 0, [], []
    for i in range(0, stream.size - hotword.CHUNK + 1, hotword.CHUNK):
        chunk = stream[i:i + hotword.CHUNK]
        fe((np.clip(chunk, -1, 1) * 32767).astype(np.int16))
        hist.append(chunk)
        p = head.probability(fe)
        if cool:
            cool -= 1
            continue
        run = run + 1 if p >= head.threshold else 0
        if run >= head.trigger_frames:
            fires.append(np.concatenate(hist[-hotword.VERIFY_WINDOW_CHUNKS:]))
            run, cool = 0, int(hotword.TRIGGER_COOLDOWN_S / 0.08)
    return fires


def verified(window: np.ndarray) -> tuple[bool, str]:
    heard = stt._run_with_cpu_fallback(window.astype("float32"), prompt=PROMPT)
    return hotword.is_wake_word(heard), heard


def main() -> None:
    word = T.WORD
    head = hotword._Head(hotword.MODEL_DIR / f"hey_{word}.npz")
    fe = T.feature_extractor()
    stt.load_model()
    print(f"hey_{word}: threshold {head.threshold} x{head.trigger_frames}; Whisper prompt {PROMPT!r}\n")

    voices = asyncio.run(T._list_voices())
    random.seed(7)

    def clips(texts):
        out = []
        for t in texts:
            found = [T._tts_path(t, v, r, p) for v in voices for (r, p) in T.PROSODY]
            found = [f for f in found if f.exists()]
            for f in random.sample(found, min(PER_TEXT, len(found))):
                a = T.load_audio(f)
                if a is not None:
                    out.append((t, a))
        return out

    groups = {"sound-alikes": clips(T.CONFUSABLES), "everyday sentences": clips(T.SENTENCES[-12:] + T.EVERYDAY[::4])}
    t0 = time.monotonic()
    for name, items in groups.items():
        hits = wakes = 0
        seconds = sum(a.size for _, a in items) / T.SR
        examples = []
        for text, audio in items:
            for w in run_clip(head, fe, audio):
                hits += 1
                ok, heard = verified(w)
                if ok:
                    wakes += 1
                    examples.append(f"{text!r} -> heard {heard!r}")
        print(f"{name}: {len(items)} clips ({seconds / 60:.1f} min): detector hits {hits}, "
              f"false wakes after the Whisper check {wakes}")
        for e in examples[:8]:
            print("    false wake:", e)

    user = sorted((T.USER_DIR / "positive").glob("*.wav"))
    caught = 0
    for f in user:
        a = T.load_audio(f)
        if a is None:
            continue
        fires = run_clip(head, fe, a)
        if fires and any(verified(w)[0] for w in fires):
            caught += 1
    print(f"\nyour recordings: {caught}/{len(user)} caught (both stages; these were also used in training)")
    talk = sorted((T.USER_DIR / "negative").glob("*talking*.wav"))
    for f in talk:
        from faster_whisper.audio import decode_audio
        a = decode_audio(str(f), sampling_rate=T.SR)
        fires = run_clip(head, fe, a)
        wakes = sum(verified(w)[0] for w in fires)
        print(f"your {a.size / T.SR:.0f} s of talking: detector hits {len(fires)}, false wakes {wakes}")
    print(f"\n({time.monotonic() - t0:.0f} s)")


if __name__ == "__main__":
    main()
