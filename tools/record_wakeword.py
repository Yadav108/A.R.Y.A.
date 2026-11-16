"""
Record your own voice to tune ARYA's wake-word detector.

Records (about 2 minutes total):
  * 12 x "Hey ARYA"  (and a few plain "ARYA")  → positive examples
  * ~40 s of you talking normally WITHOUT saying ARYA → negative examples
  * ~15 s of your room being quiet               → negative examples

Then retrain:  .venv\\Scripts\\python.exe tools\\train_wakeword.py

    --plain   only record plain "ARYA" (about 1 minute), the way you call it across the room —
              use this when ARYA reacts to "Hey ARYA" but misses a plain "ARYA"

Recordings stay on this PC (arya/models/wakeword/user_samples/, git-ignored).
"""
from __future__ import annotations

import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "arya" / "models" / "wakeword" / "user_samples"
SR = 16_000

POSITIVE_PROMPTS = ["Hey ARYA"] * 8 + ["ARYA"] * 2 + ["Okay ARYA"] * 2
# --plain: your plain "ARYA", the way you'd call it across the room — short, a bit louder, sometimes a question.
PLAIN_PROMPTS = (["ARYA"] * 4 + ["ARYA?  (as a question)"] * 2 + ["ARYA  (from a step further away)"] * 2
                 + ["ARYA  (quickly, as if busy)"] * 2 + ["ARYA, open Chrome"] * 2 + ["ARYA, what time is it"] * 2)
READ_ALOUD = (
    "Read this aloud at a normal pace, then keep chatting about your day until the timer ends:\n\n"
    "  \"I'm going to head back to the lab after lunch. The area under the curve is the integral.\n"
    "   Are you coming to the lecture tomorrow? I am almost done with the control systems report.\n"
    "   What time is it now? Set a timer and play some music. Aryan, are you there?\"\n"
)


def save(path: Path, audio: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())


def record(seconds: float) -> np.ndarray:
    audio = sd.rec(int(seconds * SR), samplerate=SR, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def trim_to_speech(audio: np.ndarray, margin_s: float = 0.12) -> np.ndarray | None:
    """Keep only the spoken part (energy well above the clip's noise floor)."""
    frame = int(0.02 * SR)
    n = audio.size // frame
    if n == 0:
        return None
    rms = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    floor = np.percentile(rms, 20)
    voiced = np.where(rms > max(0.01, floor * 4))[0]
    if voiced.size < 5:   # < 100 ms of speech — nothing usable
        return None
    m = int(margin_s * SR)
    return audio[max(0, voiced[0] * frame - m): min(audio.size, (voiced[-1] + 1) * frame + m)]


def countdown(msg: str) -> None:
    print(msg, end="", flush=True)
    for i in (3, 2, 1):
        print(f" {i}", end="", flush=True)
        time.sleep(0.6)
    print("  ● REC", flush=True)


# "--word nj": your "Hey NJ" for the second wake word (tools/train_wakeword.py --word nj)
NJ_PROMPTS = (["Hey NJ"] * 10 + ["NJ"] * 4 + ["Okay NJ"] * 2 + ["NJ?  (as a question)"] * 2
              + ["Hey NJ  (from a step further away)"] * 2 + ["NJ, open Chrome"] * 2 + ["Hey NJ, what time is it"] * 2)
NJ_READ_ALOUD = (
    "Read this aloud at a normal pace, then keep chatting about your day until the timer ends:\n\n"
    "  \"Enjoy the weekend! The engine of the robot needs a new driver. Angie and Jay are coming at six.\n"
    "   In July the DJ played until midnight. I'm engaged in a long project and my energy is low.\n"
    "   Orange juice or tea? And then, and then the injury report came in.\"\n"
)


def main() -> None:
    global OUT, READ_ALOUD
    plain = "--plain" in sys.argv[1:]
    word = sys.argv[sys.argv.index("--word") + 1].lower() if "--word" in sys.argv else "arya"
    if word == "nj":
        OUT = ROOT / "arya" / "models" / "wakeword" / "user_samples_nj"
        READ_ALOUD = NJ_READ_ALOUD
    prompts = NJ_PROMPTS if word == "nj" else PLAIN_PROMPTS if plain else POSITIVE_PROMPTS
    retrain = ".venv\\Scripts\\python.exe tools\\train_wakeword.py" + (" --word nj" if word == "nj" else "")
    print(f"Microphone: {sd.query_devices(kind='input')['name']}")
    print("Speak at your normal distance from the mic, in your normal voice.\n")
    stamp = time.strftime("%Y%m%d-%H%M%S")

    saved = 0
    for i, phrase in enumerate(prompts, 1):
        countdown(f"[{i}/{len(prompts)}] Say \"{phrase}\" after the countdown:")
        clip = trim_to_speech(record(3.0 if "," in phrase else 2.5))
        if clip is None:
            print("    didn't catch that — skipping (no speech detected)")
            continue
        save(OUT / "positive" / f"{stamp}{'_plain' if plain else ''}_{i:02d}.wav", clip)
        saved += 1
        print(f"    saved ({clip.size / SR:.2f}s)")
    if saved < 6:
        print("\nToo few usable recordings — check the mic and run again.")
        sys.exit(1)
    if plain:                                  # the negatives from the first session are still there
        print(f"\nDone — {saved} plain \"ARYA\" samples saved to {OUT.relative_to(ROOT)}")
        print(f"Now retrain:  {retrain}")
        return

    print("\n" + READ_ALOUD)
    countdown("Start reading after the countdown (40 s):")
    save(OUT / "negative" / f"{stamp}_talking.wav", record(40))

    countdown("\nNow stay quiet for 15 s (normal room noise is fine):")
    save(OUT / "negative" / f"{stamp}_room.wav", record(15))

    print(f"\nDone — {saved} wake-word samples + 55 s of negatives saved to {OUT.relative_to(ROOT)}")
    print(f"Now train:  {retrain}")


if __name__ == "__main__":
    main()
