# ARYA — Development Roadmap

> **Goal:** A fully local, zero-cost, voice-first AI assistant that rivals commercial products —
> no cloud STT, no cloud TTS, no API dependency for reasoning.
> Runs on an RTX 3050 4 GB laptop with 16 GB RAM.

---

## Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Local STT — faster-whisper medium CUDA | ✅ Done |
| 2 | Wake word engine — "Hey ARYA" (Whisper tiny CPU) | ✅ Done |
| 3 | Semantic memory — sentence-transformers vector search | ✅ Done |
| 4 | Offline LLM fallback — Ollama llama3.2:3b | 🔲 Next |
| 5 | Local TTS — Kokoro neural voice | 🔲 Planned |
| 6 | Screen awareness — OCR + context injection | 🔲 Planned |
| 7 | Proactive intelligence — pattern detection & alerts | 🔲 Planned |

---

## Phase 3 — Semantic Memory
**Model:** `all-MiniLM-L6-v2` (~90 MB disk, CPU, sentence-transformers)

### Problem
ARYA currently stores conversation history as plain rows in SQLite and retrieves the last N messages
by recency. If you asked about a concept 3 days ago, that context is gone. Notes are keyword-searched
only — no understanding of meaning.

### Solution
Every message, note, and tool result gets embedded as a 384-dim float32 vector and stored alongside
the text. Retrieval is cosine similarity — semantically related context surfaces even when the wording
differs.

### Architecture
```
User speaks → embed query (MiniLM, CPU, ~5 ms)
                   ↓
         dot-product over stored vectors
                   ↓
         top-k relevant chunks injected into LLM context
                   ↓
         Groq sees both recent history AND relevant past
```

### Files to create / modify
- `arya/core/embedder.py` — load MiniLM once, `embed(text) → np.ndarray`
- `arya/memory/database.py` — add `embeddings` column (BLOB, stored as `np.float32.tobytes()`)
- `arya/memory/database.py` — `semantic_search(query_vec, top_k=5) → list[str]`
- `arya/core/llm.py` — inject semantic hits into `stream_with_tools` context window

### Key details
- No extra database (Chroma, Faiss, etc.) — pure SQLite + numpy dot product
- Embed on write, not on read — retrieval is always fast
- Cap stored embeddings at 10 000 rows; prune oldest on overflow
- Memory kinds: `conversation`, `note`, `tool_result`
- "Forget" command: `arya/tools/notes.py` → `delete_note()` removes row + its embedding

### Dependencies to add to requirements.txt
```
sentence-transformers
```

---

## Phase 4 — Offline LLM Fallback
**Model:** `llama3.2:3b` via Ollama (~2 GB disk, 4 GB VRAM)

### Problem
ARYA depends entirely on Groq. If the API is down, rate-limited, or there's no internet, ARYA is deaf
and mute at the reasoning layer. The RTX 3050 can run a 3B model comfortably.

### Solution
Wrap the Groq client in a circuit-breaker. On `rate_limit` / `connection` error → transparently
fall back to a local Ollama endpoint. User hears no interruption.

### Architecture
```
stream_with_tools(message)
       ↓
  try Groq (primary)          ← fast, 70B quality
       │ RateLimitError / ConnectionError
       ↓
  try Ollama llama3.2:3b      ← local, ~800 ms first token
       │
  yield sentences as normal
```

### Files to create / modify
- `arya/core/llm_local.py` — thin async wrapper around `http://localhost:11434/api/chat`
  with streaming support
- `arya/core/llm.py` — catch `groq.RateLimitError` and `httpx.ConnectError` in
  `stream_with_tools` and `_tool_pipeline`, delegate to `llm_local`
- `arya/server/websocket.py` — on error message check for "rate_limit" to keep existing UX
- `.env` — add `OLLAMA_MODEL=llama3.2:3b` (overridable)

### Key details
- Tools still work in fallback mode — `_tool_pipeline` calls tool functions directly, only the
  final summarisation step uses the LLM
- Circuit-breaker: after 3 consecutive Groq failures in 60 s, switch to local for 5 minutes
  without retrying Groq — reduces latency and error log spam
- Intent classifier (`needs_tool`) also falls back to a simpler keyword-match heuristic when
  both APIs are down (avoids a second 3B inference for binary YES/NO)

### Setup (one-time, user runs manually)
```bash
# Install Ollama
winget install Ollama.Ollama
# Pull the model
ollama pull llama3.2:3b
```

### Dependencies to add to requirements.txt
```
# none — Ollama exposes an HTTP API; httpx already present
```

---

## Phase 5 — Local TTS (Kokoro)
**Model:** `kokoro-82m` (~500 MB disk, CPU/CUDA, ~50 ms latency)

### Problem
`edge-tts` (current) requires an internet connection and adds ~300–800 ms network round-trip
per sentence. On a slow connection, TTS becomes the bottleneck.

### Solution
Replace `edge-tts` with Kokoro, a 82M-parameter neural TTS model that runs fully locally.
Same sentence-streaming interface — no other code needs to change.

### Architecture
```
stream_with_tools → sentence
       ↓
  speak_sentence(sentence)        (arya/core/tts.py)
       ↓
  kokoro.generate(sentence)       ← runs on CPU, ~50 ms
       ↓
  bytes (WAV/PCM)  → WebSocket → browser plays
```

### Files to modify
- `arya/core/tts.py` — replace `edge_tts.Communicate` with `kokoro` pipeline;
  keep `speak_sentence(text) → bytes` signature identical
- Load model once at startup (singleton), same pattern as Whisper

### Key details
- Voice: `af_heart` (warm British female, matches current en-GB-SoniaNeural feel)
- Output: 24 kHz PCM → encode to MP3 with `pydub` for browser compatibility
- Graceful fallback: if Kokoro import fails, fall back to `edge-tts` silently
- GPU acceleration optional: `device="cuda"` drops latency to ~15 ms

### Dependencies to add to requirements.txt
```
kokoro
pydub
```

---

## Phase 6 — Screen Awareness (OCR + Context Injection)
**Model:** `tesseract-ocr` (free, system install) or `easyocr` (~200 MB, CUDA)

### Problem
ARYA has no idea what is on the screen. "What does this error mean?" or "Summarise this page"
are unanswerable without vision.

### Solution
A new tool `read_screen()` captures a screenshot, OCRs it, and injects the text as context into
the LLM prompt. ARYA can then reason about visible content.

### Files to create / modify
- `arya/tools/screen.py` — `read_screen() → str` using `Pillow` screenshot + `pytesseract`
- `arya/core/llm.py` — register `read_screen` tool in `TOOLS` list and dispatch in `_tool_pipeline`
- Trigger phrases: "what does this say", "read my screen", "what error is this",
  "summarise this page"

### Dependencies to add
```
pillow
pytesseract   # or easyocr for CUDA-accelerated path
```

---

## Phase 7 — Proactive Intelligence
*No new models — uses existing LLM + embeddings from Phase 3.*

### Problem
ARYA only responds when spoken to. A truly intelligent assistant anticipates needs:
- Pomodoro ends → automatically briefs on what's next
- Same question asked 3 times → surfaces the answer proactively
- Battery below 15% while working → alerts without being asked
- Midnight study session → gently suggests a break

### Solution
A lightweight background loop (`arya/core/proactive.py`) that runs every 60 s and checks a
set of rules. If a rule fires, it calls the existing `notify_callback` (already wired through
WebSocket to the browser and TTS pipeline).

### Rules planned (v1)
| Rule | Trigger | Action |
|------|---------|--------|
| Battery guard | battery < 15 % and not charging | Speak alert once per 30 min |
| Pomodoro debrief | timer fires | Auto-ask "what did you accomplish?" |
| Repeat query | same semantic cluster asked 3× | Suggest saving as a note |
| Late-night | time > 00:30 and usage active | Suggest break |
| Idle wake | no interaction for 2 h during day | Offer daily briefing |

### Files to create / modify
- `arya/core/proactive.py` — `start_proactive_loop(notify_cb)` async task
- `arya/server/websocket.py` — call `start_proactive_loop` in `preload_models` startup event

---

## Ongoing / Cross-cutting improvements

These are not phase-gated — pick them up whenever they become the sharpest pain point.

### LLM context improvements
- [ ] Sliding window with semantic pruning: keep last 5 messages + top-3 semantic hits
      instead of last 20 by recency (reduces token cost, improves relevance)
- [ ] System prompt personalisation: inject user's degree, study topics, city from `.env`
      so ARYA's tone and examples match context

### Tool improvements
- [ ] `python_repl` tool — run a sandboxed Python snippet and return stdout
      (useful for one-off calculations the math tool can't handle)
- [ ] `spotify_control` — play/pause/skip/volume via Spotify Web API (free tier)
- [ ] `read_clipboard_as_task` — paste a todo list and have ARYA break it into steps
- [ ] Multi-result Wikipedia: current tool returns one page; add `search_multiple(query, n=3)`
      for ambiguous queries

### Wake word improvements
- [ ] Configurable wake phrase via `.env` → `WAKE_WORD=arya`
- [ ] Whisper confidence threshold: discard low-probability transcripts (reduces false positives)
- [ ] Post-wake silence detection: if no speech follows wake within 4 s, play a "I'm listening"
      chime and remain in active listening mode

### UI / frontend improvements
- [ ] Conversation history panel — show past exchanges, not just current turn
- [ ] Settings panel — adjustable TTS speed, wake word on/off toggle, theme
- [ ] Mobile-friendly layout — currently 640 px wide card works, but a proper mobile view
      with larger tap targets and swipe-to-dismiss would help
- [ ] Visual waveform while recording (Web Audio API AnalyserNode)

### Reliability
- [ ] `healthz` HTTP endpoint — returns model load status; lets a startup script wait for ready
- [ ] Graceful restart: save in-flight conversation to SQLite before shutdown
- [ ] Log rotation — current print() calls; wire to Python `logging` with file handler

---

## Hardware context (RTX 3050 4 GB, 16 GB RAM)

| Component | VRAM / RAM used | Headroom |
|-----------|----------------|---------|
| Whisper medium (CUDA fp16) | ~1.5 GB VRAM | ✅ |
| Whisper tiny (CPU int8) | ~150 MB RAM | ✅ |
| MiniLM embedder (CPU, Phase 3) | ~90 MB RAM | ✅ |
| Ollama llama3.2:3b (Phase 4) | ~2.0 GB VRAM | ✅ fits alongside Whisper |
| Kokoro TTS (CPU, Phase 5) | ~200 MB RAM | ✅ |
| **Total peak (all phases)** | **~3.5 GB VRAM, ~1.5 GB RAM** | ✅ safe margin |
