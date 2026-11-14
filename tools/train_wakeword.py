"""
Train ARYA's custom "Hey ARYA" wake-word detector.

Pipeline (a scaled-down version of openWakeWord's training recipe that fits
on a laptop — the official one needs ~20 GB of negative data):

  1. Synthesize "Hey ARYA" / "ARYA" / "Okay ARYA" with ~49 edge-tts voices
     (14 English accents + Hindi) at several speeds and pitches.
  2. Synthesize hard negatives ("area", "I am", "aria", "are you", "Aryan",
     everyday commands *without* the wake word, ...).
  3. Optionally add YOUR recordings from tools/record_wakeword.py
     (arya/models/wakeword/user_samples/) — these matter most.
  4. Augment: gain, synthetic room reverb, white/pink/brown noise, babble.
  5. Convert to openWakeWord speech embeddings (16 x 96 per 1.28 s window).
  6. Train a small MLP, pick the threshold on held-out voices/sentences,
     and save arya/models/wakeword/hey_arya.npz.

Usage:
    .venv\\Scripts\\python.exe tools\\train_wakeword.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sys
import time
from io import BytesIO
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL_DIR = ROOT / "arya" / "models" / "wakeword"
USER_DIR = MODEL_DIR / "user_samples"
CACHE = Path(os.getenv("WAKE_TRAIN_CACHE", ROOT / ".wake_cache"))
TTS_DIR = CACHE / "tts"

SR = 16_000
CLIP = 2 * SR            # 2 s clips → exactly 16 embedding frames
N_FRAMES = 16
SEED = 1337

POS_TEXTS = ["Hey Arya", "Arya", "Okay Arya", "Hi Arya", "Hey Aarya", "Aarya"]
PROSODY = [("+0%", "+0Hz"), ("-15%", "-8Hz"), ("+15%", "+8Hz"), ("-5%", "+15Hz"), ("+8%", "-15Hz")]

# Sounds-like-ARYA phrases and everyday speech that must NOT trigger.
CONFUSABLES = [
    "area", "this area", "I am", "I'm", "I am what", "I do not know", "are you", "are you there",
    "aria", "an aria", "Maria", "Ariana", "Aryan", "hey Aryan", "really", "Korea", "Syria",
    "carrier", "array", "Ariel", "hey Ria", "hey Rhea", "hey you", "hey there", "okay",
    "hey Siri", "Alexa", "okay Google", "hey Google", "hey Jarvis", "hi there", "hey",
    "ah yeah", "oh yeah", "are ya", "Arnie", "Atlanta", "hurry up", "sorry", "story",
    "hey Sarah", "hey Laura", "hey Maya", "hey Priya", "hey Riya", "hey Diya", "idea",
]
SENTENCES = [
    "What time is it now?", "Set a timer for ten minutes.", "What's the weather like today?",
    "Can you open Spotify?", "Pause the music.", "Skip this song.", "Turn the volume down a bit.",
    "Remind me to call mom at six.", "I'm going to head back to the lab.", "Exactly, that's what I meant.",
    "Yes, if you're the same person.", "I don't think that's right.", "Let's grab lunch after the lecture.",
    "The control system needs a better PID tuning.", "Did you finish the thermodynamics assignment?",
    "We should meet at the library tomorrow.", "Can you send me the slides?", "That was a great match last night.",
    "My phone battery is almost dead.", "Where did I put my keys?", "The train is running late again.",
    "I'll call you back in five minutes.", "How was your weekend?", "This coffee is way too strong.",
    "Hand me that screwdriver please.", "The robot arm keeps overshooting the target.",
    "Okay, let's start the meeting.", "Can you hear me now?", "Hello, how are you doing?",
    "Thanks a lot, see you later.", "I need to finish this report by Friday.", "Open the window, it's hot in here.",
    "Are you coming to the party?", "Really? I didn't know that.", "The area under the curve is the integral.",
    "I am almost done with the simulation.", "Is the oscilloscope still in the lab?", "Turn off the lights.",
    "What's for dinner tonight?", "Play some music.", "Next song please.", "Good morning everyone.",
    "Let me think about it.", "Where's the nearest supermarket?", "Can you help me with this equation?",
    "The motor driver overheated again.", "I'm watching a movie right now.", "Call me when you get home.",
    "Maria said she would be late.", "The aria in the second act was beautiful.", "Aryan, are you there?",
]
NEG_VOICES_PER_TEXT = 6

# Broad everyday speech so the detector learns what "not the wake word" sounds
# like in general (every 5th one is held out for evaluation).
EVERYDAY = [
    "I'll be there in about twenty minutes.", "Did anyone see my charger?", "We ran out of milk again.",
    "The meeting got moved to Thursday afternoon.", "Can you pass me the salt?", "It's raining pretty hard outside.",
    "I think the printer is jammed.", "Let's order pizza tonight.", "My sister is visiting next weekend.",
    "The bus was completely full this morning.", "Do you want some tea?", "I forgot my password again.",
    "That movie was way better than I expected.", "We need to buy a new vacuum cleaner.", "The professor extended the deadline.",
    "Could you turn on the heater?", "I'm so tired today.", "What did you have for breakfast?",
    "The internet is really slow right now.", "Let's go for a walk after dinner.", "I can't find the remote anywhere.",
    "He said he'd call back later.", "This laptop keeps overheating.", "My flight lands at nine thirty.",
    "Have you tried restarting it?", "The lab results came back yesterday.", "Can we reschedule to Monday?",
    "I need to renew my visa soon.", "That's a really good point.", "I'm not sure that's going to work.",
    "Wait, say that again.", "Honestly, I have no idea.", "Okay, sounds good to me.",
    "No, the other one on the left.", "We should leave in five minutes.", "How much does that cost?",
    "Put it on the table please.", "I'll do it tomorrow morning.", "Is anyone sitting here?",
    "The soup needs more salt.", "Close the door behind you.", "My back hurts from sitting all day.",
    "Let's split the bill.", "Which station do we get off at?", "The code compiles but the test fails.",
    "We need more data for the regression.", "Plot the error against the step size.", "The sensor readings look noisy.",
    "Check the voltage on that pin.", "I pushed the changes to the branch.", "The server crashed overnight.",
    "Can you review my pull request?", "The battery voltage is dropping fast.", "Let's measure it one more time.",
    "Hmm, that's strange.", "Oh no, I spilled my coffee.", "Yeah, I know what you mean.",
    "Right, right, okay.", "Uh, let me check my calendar.", "Well, it depends on the weather.",
    "So anyway, what was I saying?", "Alright, let's get started.", "Hey, how's it going?",
    "Hi, nice to meet you.", "Hey man, long time no see.", "Hey, can you help me for a second?",
    "Okay, I'll see you then.", "Hey, are you free tonight?", "Hi, is this the right room?",
    "Are you hungry?", "Are you serious right now?", "Are we there yet?",
    "I really appreciate it.", "Are you sure about that?", "Our area manager is visiting.",
    "Arrange the chairs in a circle.", "The harbour area is beautiful at night.", "Aaron is running late.",
    "Are your parents coming too?", "Our cat sleeps all day.", "Iron the shirt before you leave.",
    "Our yard needs mowing.", "The arena was packed last night.", "Arya Stark is my favourite character.",
    "I read the article you sent.", "Arthur will join us later.", "The array index is out of bounds.",
    "The air conditioner is broken.", "Can I borrow your pen?", "Turn left at the next traffic light.",
    "I've been learning German for a year.", "The weather in Jülich is always grey.", "Let's take the train to Cologne.",
    "Did you water the plants?", "My phone is on silent.", "Grab an umbrella just in case.",
    "That restaurant closes at ten.", "I'll send you the link.", "The game starts at eight.",
    "What's the score?", "He scored in the last minute.", "We lost on penalties again.",
    "I'm going to the gym.", "Do you need anything from the store?", "The washing machine is done.",
    "Dinner is ready.", "Can you keep it down a bit?", "I'm on a call right now.",
    "Mute yourself please.", "Can everyone see my screen?", "Let's take a five minute break.",
    "Any questions so far?", "Next slide please.", "Thanks everyone for joining.",
]


# ── TTS generation ────────────────────────────────────────────────────────────

async def _list_voices() -> list[str]:
    import edge_tts
    voices = await edge_tts.list_voices()
    return [v["ShortName"] for v in voices if v["Locale"].startswith(("en-", "hi-IN"))]


def _tts_path(text: str, voice: str, rate: str, pitch: str) -> Path:
    key = hashlib.sha1(f"{text}|{voice}|{rate}|{pitch}".encode()).hexdigest()[:16]
    return TTS_DIR / f"{key}.mp3"


async def _synth(job, sem, stats) -> None:
    import edge_tts
    text, voice, rate, pitch = job
    path = _tts_path(*job)
    if path.exists():
        return
    async with sem:
        for attempt in range(3):
            try:
                buf = BytesIO()
                async for ch in edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).stream():
                    if ch["type"] == "audio":
                        buf.write(ch["data"])
                if buf.tell():
                    path.write_bytes(buf.getvalue())
                    stats["ok"] += 1
                return
            except Exception:
                await asyncio.sleep(1.5 * (attempt + 1))
        stats["fail"] += 1


async def generate(pos_jobs, neg_jobs) -> None:
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [j for j in pos_jobs + neg_jobs if not _tts_path(*j).exists()]
    print(f"TTS: {len(pos_jobs)} positive + {len(neg_jobs)} negative clips, {len(jobs)} to synthesize")
    sem = asyncio.Semaphore(8)
    stats = {"ok": 0, "fail": 0}
    t = time.monotonic()
    for i in range(0, len(jobs), 200):
        await asyncio.gather(*(_synth(j, sem, stats) for j in jobs[i:i + 200]))
        print(f"  {min(i + 200, len(jobs))}/{len(jobs)}  ({time.monotonic() - t:.0f}s, {stats['fail']} failed)")


def load_audio(path: Path) -> np.ndarray | None:
    from faster_whisper.audio import decode_audio
    try:
        pcm = decode_audio(str(path), sampling_rate=SR)
    except Exception:
        return None
    if pcm.size == 0:
        return None
    # trim leading/trailing near-silence that TTS adds
    idx = np.where(np.abs(pcm) > 0.01)[0]
    return pcm[idx[0]: idx[-1] + 1].astype(np.float16) if idx.size else None  # float16 halves RAM


# ── Augmentation ──────────────────────────────────────────────────────────────

def _colored_noise(n: int, rng: np.random.Generator, kind: str) -> np.ndarray:
    white = rng.standard_normal(n)
    if kind == "white":
        x = white
    else:
        spec = np.fft.rfft(white)
        f = np.arange(spec.size) + 1.0
        spec /= np.sqrt(f) if kind == "pink" else f
        x = np.fft.irfft(spec, n)
    return (x / (np.std(x) + 1e-9)).astype(np.float32)


def _room(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    rt60 = rng.uniform(0.15, 0.7)
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    rir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    rir[0] = 1.0
    rir /= np.sqrt(np.sum(rir ** 2))
    from scipy.signal import fftconvolve
    return fftconvolve(x, rir)[: x.size].astype(np.float32)


def augment(x: np.ndarray, rng: np.random.Generator, babble: list[np.ndarray]) -> np.ndarray:
    x = x.astype(np.float32)
    if rng.random() < 0.5:
        x = _room(x, rng)
    sig = np.sqrt(np.mean(x ** 2)) + 1e-9
    if rng.random() < 0.85:
        snr = rng.uniform(3, 30)
        noise = _colored_noise(x.size, rng, rng.choice(["white", "pink", "brown"]))
        x = x + noise * sig / (10 ** (snr / 20))
    if babble and rng.random() < 0.3:
        b = babble[rng.integers(len(babble))]
        start = rng.integers(0, max(1, b.size - x.size))
        seg = np.resize(b[start:start + x.size], x.size).astype(np.float32)
        x = x + seg * sig * rng.uniform(0.05, 0.3) / (np.sqrt(np.mean(seg ** 2)) + 1e-9)
    peak = np.max(np.abs(x)) + 1e-9
    return x / peak * rng.uniform(0.05, 0.95)


def place_at_end(phrase: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """2 s clip with the phrase ending 0–250 ms before the clip end."""
    out = np.zeros(CLIP, dtype=np.float32)
    tail = int(rng.uniform(0, 0.25) * SR)
    phrase = phrase[-(CLIP - tail):]
    end = CLIP - tail
    out[end - phrase.size:end] = phrase
    return out


# ── Features ──────────────────────────────────────────────────────────────────

def feature_extractor():
    from openwakeword.utils import AudioFeatures
    return AudioFeatures(
        melspec_model_path=str(MODEL_DIR / "melspectrogram.onnx"),
        embedding_model_path=str(MODEL_DIR / "embedding_model.onnx"),
        inference_framework="onnx",
    )


def to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1, 1) * 32767).astype(np.int16)


def embed_fixed(fe, clips, batch_size: int = 16) -> np.ndarray:
    """Iterable of 2 s clips → (N, 16, 96) embeddings. Clips are consumed lazily
    in small batches so memory stays flat however many there are."""
    out, batch = [], []

    def flush():
        if batch:
            arr = np.stack(batch)
            out.append(fe.embed_clips(arr, batch_size=batch_size)[:, -N_FRAMES:, :].astype(np.float16))
            batch.clear()

    for c in clips:
        batch.append(to_int16(c))
        if len(batch) == batch_size:
            flush()
    flush()
    return np.concatenate(out) if out else np.zeros((0, N_FRAMES, 96), np.float16)


def embed_windows(fe, audio: np.ndarray, stride: int = 2) -> np.ndarray:
    """Long audio → all 16-frame windows (stride in 80 ms frames)."""
    if audio.size < CLIP:
        audio = np.concatenate([np.zeros(CLIP - audio.size, np.float32), audio])
    emb = fe._get_embeddings(to_int16(audio))
    if emb.ndim == 1 or emb.shape[0] < N_FRAMES:
        return np.zeros((0, N_FRAMES, 96), np.float16)
    idx = range(N_FRAMES, emb.shape[0] + 1, stride)
    return np.stack([emb[i - N_FRAMES:i] for i in idx]).astype(np.float16)


# ── Model ─────────────────────────────────────────────────────────────────────

def mlp_scores(params: dict, x: np.ndarray) -> np.ndarray:
    h = np.maximum(0, x @ params["W1"] + params["b1"])
    z = (h @ params["W2"] + params["b2"]).ravel()
    return 1 / (1 + np.exp(-z))


def count_triggers(scores: np.ndarray, threshold: float, frames: int, refractory: int = 19) -> int:
    """Detections under the live rule: `frames` consecutive scores >= threshold,
    then a ~1.5 s cooldown (matches arya/core/hotword.py)."""
    hits = run = cool = 0
    for s in scores:
        if cool:
            cool -= 1
            run = 0
            continue
        run = run + 1 if s >= threshold else 0
        if run >= frames:
            hits += 1
            cool = refractory
            run = 0
    return hits


def main() -> None:
    rng = np.random.default_rng(SEED)
    random.seed(SEED)
    voices = asyncio.run(_list_voices())
    print(f"{len(voices)} voices")

    # Hold out whole voices so test recall measures generalisation to new speakers.
    held_voices = set(random.sample(voices, 8))
    pos_jobs = [(t, v, r, p) for v in voices for t in POS_TEXTS
                for (r, p) in random.sample(PROSODY, 3)]
    neg_texts = CONFUSABLES + SENTENCES
    held_texts = set(random.sample(SENTENCES, 10)) | set(random.sample(CONFUSABLES, 8))
    neg_jobs = [(t, v, *random.choice(PROSODY)) for t in neg_texts
                for v in random.sample(voices, NEG_VOICES_PER_TEXT)]
    held_texts |= set(EVERYDAY[::5])
    neg_jobs += [(t, v, *random.choice(PROSODY)) for t in EVERYDAY for v in random.sample(voices, 4)]
    asyncio.run(generate(pos_jobs, neg_jobs))

    print("Loading audio...")
    pos = {"train": [], "test": []}
    for job in pos_jobs:
        p = _tts_path(*job)
        a = load_audio(p) if p.exists() else None
        if a is not None:
            pos["test" if job[1] in held_voices else "train"].append(a)
    neg = {"train": [], "test": []}
    for job in neg_jobs:
        p = _tts_path(*job)
        a = load_audio(p) if p.exists() else None
        if a is not None:
            neg["test" if job[0] in held_texts else "train"].append(a)
    babble = neg["train"]

    # Your own recordings (tools/record_wakeword.py) — weighted heavily.
    user_pos, user_neg = [], []
    if USER_DIR.exists():
        from faster_whisper.audio import decode_audio
        for f in sorted((USER_DIR / "positive").glob("*.wav")):
            a = load_audio(f)
            if a is not None:
                user_pos.append(a)
        for f in sorted((USER_DIR / "negative").glob("*.wav")):
            user_neg.append(decode_audio(str(f), sampling_rate=SR))
    print(f"positives: {len(pos['train'])} train / {len(pos['test'])} test (held-out voices), "
          f"negatives: {len(neg['train'])} train / {len(neg['test'])} test, "
          f"user: {len(user_pos)} positive / {len(user_neg)} negative recordings")

    fe = feature_extractor()
    t = time.monotonic()

    HISTORY = np.zeros(2 * SR, np.float32)   # live detection always has >= 2 s of history

    def with_context(c):
        """Sometimes put other speech before the phrase, so 'silence before a
        short phrase' can't become the model's cue for the wake word."""
        if babble and rng.random() < 0.4:
            other = babble[rng.integers(len(babble))]
            other = other[-int(rng.uniform(0.4, 1.5) * SR):] * rng.uniform(0.3, 1.0)
            gap = np.zeros(int(rng.uniform(0.1, 0.5) * SR), np.float32)
            return np.concatenate([other, gap, c])
        return c

    def fixed_features(clips, copies):
        """Phrase ending 0-250 ms before the window edge - identical treatment
        for wake-word clips and non-wake clips."""
        return embed_fixed(fe, (augment(place_at_end(with_context(c), rng), rng, babble)
                                for c in clips for _ in range(copies)))

    def sliding_features(clips, stride):
        """Every window over (history + clip + tail): what the live detector sees."""
        out = [embed_windows(fe, augment(np.concatenate([HISTORY, with_context(c), np.zeros(int(0.5 * SR), np.float32)]),
                                         rng, babble), stride=stride) for c in clips]
        return np.concatenate(out) if out else np.zeros((0, N_FRAMES, 96), np.float16)

    def eval_sequences(clips, copies):
        """Per-clip score sequences for held-out evaluation (stride 1, like live)."""
        seqs, seconds = [], 0.0
        for c in clips:
            for _ in range(copies):
                a = augment(np.concatenate([HISTORY, c, np.zeros(int(0.6 * SR), np.float32)]), rng, babble)
                seqs.append(embed_windows(fe, a, stride=1))
                seconds += (c.size + 0.6 * SR) / SR
        return seqs, seconds

    # Synthetic features are built in stages, each saved to disk as soon as it is
    # done and freed from RAM. A crash/kill resumes from the last finished stage;
    # re-running after recording your voice only embeds the new recordings.
    import gc
    rebuild = os.getenv("WAKE_REBUILD_FEATURES") == "1"

    def stage(name, build):
        f = CACHE / f"features_v3_{name}.npz"
        if f.exists() and not rebuild:
            print(f"  {name}: cached")
            return dict(np.load(f))
        t0 = time.monotonic()
        data = build()
        np.savez(f, **data)
        print(f"  {name}: done ({time.monotonic() - t0:.0f}s)", flush=True)
        gc.collect()
        return data

    def seqs_pack(seqs, seconds=0.0):
        return {"x": np.concatenate(seqs), "idx": np.cumsum([len(q) for q in seqs])[:-1],
                "seconds": np.float64(seconds)}

    print("building features (staged, cached in .wake_cache)...", flush=True)
    Xp = stage("pos", lambda: {"x": fixed_features(pos["train"], 3)})["x"]
    Xn_parts = [
        stage("neg_fixed", lambda: {"x": fixed_features(neg["train"], 2)})["x"],      # mirror of the positives
        stage("neg_slide", lambda: {"x": sliding_features(neg["train"], stride=2)})["x"],  # what the detector sees live
        stage("noise", lambda: {"x": np.concatenate([
            embed_windows(fe, augment(_colored_noise(CLIP * 3, rng, k) * 0.05, rng, babble), stride=3)
            for k in ["white", "pink", "brown"] * 15])})["x"],
    ]
    pe = stage("pos_eval", lambda: seqs_pack(eval_sequences(pos["test"], 2)[0]))
    ne = stage("neg_eval", lambda: seqs_pack(*eval_sequences(neg["test"], 3)))
    pos_eval = np.split(pe["x"], pe["idx"])
    neg_eval = np.split(ne["x"], ne["idx"])
    neg_seconds = float(ne["seconds"])
    del pe, ne, pos, neg
    gc.collect()

    if user_pos:
        Xp = np.concatenate([Xp, fixed_features(user_pos, 25)])
    if user_neg:
        Xn_parts += [embed_windows(fe, augment(a, rng, babble), stride=1) for a in user_neg for _ in range(3)]
    Xn = np.concatenate(Xn_parts)
    del Xn_parts
    gc.collect()
    print(f"features: {len(Xp)} positive / {len(Xn)} negative windows ({time.monotonic() - t:.0f}s)")

    # -- Train --
    # Features stay float16 in RAM; the MLP sees float32 chunks of a few thousand
    # rows at a time (partial_fit), so peak memory is ~the feature store itself.
    from sklearn.neural_network import MLPClassifier
    D = N_FRAMES * 96
    Xp = Xp.reshape(-1, D)
    Xn = Xn.reshape(-1, D)
    val_p = rng.random(len(Xp)) < 0.1
    val_n = rng.random(len(Xn)) < 0.1
    tr_p, tr_n = np.flatnonzero(~val_p), np.flatnonzero(~val_n)
    va_X = np.concatenate([Xp[val_p], Xn[val_n]]).astype(np.float32)
    va_y = np.concatenate([np.ones(val_p.sum()), np.zeros(val_n.sum())])
    n_pos_draw = max(len(tr_p), len(tr_n) // 2)          # positives oversampled to ~1:2
    print(f"training MLP: {len(tr_p)} positive / {len(tr_n)} negative windows "
          f"(positives drawn {n_pos_draw}x per epoch), {len(va_y)} validation", flush=True)

    clf = MLPClassifier(hidden_layer_sizes=(64,), alpha=1e-3, batch_size=256,
                        learning_rate_init=1e-3, random_state=SEED)
    CHUNK_ROWS = 4096
    best_loss, best_params, stale = np.inf, None, 0
    for epoch in range(40):
        rows = np.concatenate([rng.choice(tr_p, n_pos_draw), tr_n + len(Xp)])
        rng.shuffle(rows)
        for i in range(0, len(rows), CHUNK_ROWS):
            r = rows[i:i + CHUNK_ROWS]
            is_pos = r < len(Xp)
            xb = np.empty((len(r), D), np.float32)
            xb[is_pos] = Xp[r[is_pos]]
            xb[~is_pos] = Xn[r[~is_pos] - len(Xp)]
            clf.partial_fit(xb, is_pos.astype(np.float64), classes=np.array([0.0, 1.0]))
        prob = np.clip(clf.predict_proba(va_X)[:, 1], 1e-7, 1 - 1e-7)
        loss = float(-np.mean(va_y * np.log(prob) + (1 - va_y) * np.log(1 - prob)))
        print(f"  epoch {epoch + 1:2d}  validation loss {loss:.4f}", flush=True)
        if loss < best_loss - 1e-4:
            best_loss, stale = loss, 0
            best_params = {"W1": clf.coefs_[0].copy(), "b1": clf.intercepts_[0].copy(),
                           "W2": clf.coefs_[1].copy(), "b2": clf.intercepts_[1].copy()}
        else:
            stale += 1
            if stale >= 5:
                break
    params = {k: v.astype(np.float32) for k, v in best_params.items()}
    del va_X
    gc.collect()

    def score(seq):
        return mlp_scores(params, seq.reshape(-1, D).astype(np.float32))

    pos_scores = [score(s) for s in pos_eval]
    neg_scores = [score(s) for s in neg_eval]
    hours = neg_seconds / 3600

    print(f"\nheld-out (scored every 80 ms, like live): {len(pos_scores)} wake-word clips from "
          f"{len(held_voices)} unseen voices, {neg_seconds / 60:.1f} min of unseen non-wake speech")
    print(" threshold  frames | recall | false triggers")
    results = []
    for k in (1, 2, 3):
        for th in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98):
            recall = float(np.mean([count_triggers(s, th, k) > 0 for s in pos_scores]))
            fts = sum(count_triggers(s, th, k) for s in neg_scores)
            results.append((th, k, recall, fts / hours))
            print(f"   {th:.2f}       {k}   | {recall:6.1%} | {fts:3d} ({fts / hours:5.1f}/h)")
    # Best recall with <= 1 false trigger/hour. If that's out of reach, keep recall
    # usable (>= 85%) and take the fewest false triggers among those settings.
    ok = [r for r in results if r[3] <= 1.0]
    usable = [r for r in results if r[2] >= 0.85]
    if ok:
        best = max(ok, key=lambda r: (r[2], -r[3]))
    elif usable:
        best = min(usable, key=lambda r: (r[3], -r[2]))
    else:
        best = max(results, key=lambda r: (r[2], -r[3]))

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out = MODEL_DIR / "hey_arya.npz"
    np.savez(out, **params, threshold=np.float32(best[0]), trigger_frames=np.int32(best[1]),
             n_frames=np.int32(N_FRAMES))
    meta = {
        "threshold": best[0], "trigger_frames": best[1], "heldout_recall": best[2],
        "heldout_false_triggers_per_hour": best[3],
        "train_positive_windows": int(len(Xp)), "train_negative_windows": int(len(Xn)),
        "user_positive_recordings": len(user_pos), "user_negative_recordings": len(user_neg),
        "voices": len(voices), "trained": time.strftime("%Y-%m-%d %H:%M"),
    }
    (MODEL_DIR / "hey_arya.json").write_text(json.dumps(meta, indent=2))
    print(f"\nsaved {out.relative_to(ROOT)}  threshold={best[0]}  frames={best[1]}  "
          f"recall={best[2]:.1%}  false triggers={best[3]:.1f}/h")

if __name__ == "__main__":
    main()
