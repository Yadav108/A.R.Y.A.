# ARYA — Roadmap

> **Goal:** the best private study companion — a voice-first tutor that knows *your* courses,
> is never confidently wrong, walks you through exercises, checks your answers and plans your
> exam prep — and then the best PC & life assistant on the same foundation.
>
> **Principles:** local first; the cloud only for hard questions and only after asking (sending just
> the question and the relevant course excerpt, never voice, history or files); every change is
> measured (`pytest`, `python -m arya.eval`).

---

## Where ARYA is today

| Area | What works | Where |
|------|------------|-------|
| Hearing | "Hey ARYA" detector + Whisper verification; follow-ups without the wake word; smart listening (ignores talk not meant for ARYA); bare "stop" barge-in | `arya/core/hotword.py`, `addressee.py`, `stt.py` |
| Speaking | Piper TTS (offline); maths spoken as words; ~5 spoken sentences, the rest on screen | `arya/core/tts.py`, `speech_math.py` |
| Brain | Local qwen2.5:3b via Ollama (offline mode); Groq for "ask the cloud, …"; tools (files, apps, timers, notes, weather, maths, plots) | `arya/core/llm.py`, `llm_local.py`, `arya/tools/` |
| Course library | PDFs/Word/PowerPoint/notes indexed in place, hybrid search, page previews, OCR of scans (Windows OCR), exercises by number, duplicates, checked core notes | `arya/knowledge/library.py`, `core_notes.md` |
| Visuals | Function, step/impulse/Bode plots with computed facts; closed-loop block diagrams | `arya/knowledge/visuals.py` |
| Memory | Conversation history + semantic recall | `arya/memory/database.py` |
| App | Desktop window + tray, sources and visuals panel, privacy mode, action log, token-protected local server | `arya/desktop/`, `arya/static/app.html`, `arya/server/` |
| Quality | 132 pytest tests; 59-question golden benchmark | `tests/`, `arya/eval/` |

## Hardware (measured)

Lenovo Legion 5 (82JW): Ryzen 7 5800H, RTX 3050 4 GB, **8 GB RAM in one module, of which the Radeon
iGPU reserves 2 GB → 5.9 GB usable**. Whisper medium (GPU, ~1 GB VRAM + system reservation),
qwen2.5:3b (~2.3 GB) and the app window fit only just. Levers: BIOS "UMA Frame Buffer" 2 GB → 512 MB
(~7.4 GB usable), `WHISPER_MODEL=small`, keep the disk above a few GB free so the page file can grow.

---

## Milestones

### M0 — Foundations
- [ ] BIOS: lower the iGPU frame buffer (user)
- [x] Tests in the repo (`pytest`) and the golden benchmark (`python -m arya.eval`)
- [x] This roadmap brought up to date
- [ ] Memory profile documented (`WHISPER_MODEL=small`, context size)

### M1 — Trustworthy answers  (benchmark 78 % → 93–100 %)
- [x] Hard-question router: offers the cloud for derivations / exam problems, asks first, sends only question + excerpt (`arya/core/router.py`)
- [x] `sympy` maths tool — numbers come from a tool, never from the model's head (`arya/tools/mathsolve.py`)
- [x] Exact course formulas and formula cards answered without the model (`arya/study/formulas.py`)
- [x] Corrections memory: "that's wrong — …" is saved and outranks other sources next time (`arya/knowledge/corrections.py`)
- [ ] Checked one-page fundamentals per course in the core notes (control and electrical fixes done; Dynamics, PID depth next)

### M2 — Exercise tutor
- [x] Walkthrough mode (given / wanted / idea, Socratic next steps, hints) — `arya/study/tutor.py`
- [x] "Check my answer" against the answers page (tolerance + units), and "what is α2 in exercise 1-13?" read off it
- [x] Handwritten worked solutions explained via cloud vision (ask first), transcriptions kept and searchable — `arya/study/handwriting.py`

### M3 — Practice & planning
- [x] Quizzes from your own material (exercise results from the answers pages, checked formula cards); spaced repetition (SM-2), weak-topic tracking — `arya/study/practice.py`
- [x] Timers and reminders that survive restarts — `arya/tools/productivity.py`
- [x] Exam planner: exam dates by voice, "what should I study today?", a study line in the daily briefing — `arya/study/planner.py`
- [x] Progress view in the app: the Study card (exams, cards due, accuracy and exercises per course, 7-day chart, streak, "Quiz me") — `/api/progress`

### M4 — See what you see
- [x] "Explain what's on my screen": the window you're looking at (never ARYA's own, even when covered) read with Windows OCR, answered locally; pictures offered to the cloud, asking first — `arya/tools/screen.py`
- [x] Photos of paper notes: drop or paste into the app, read with OCR, explained, "add it to my library" (Photo notes)
- [x] Formulas readable in the app: done differently — LaTeX is turned into Unicode maths (ζ, ωₙ², √(k/m)) since M1, so KaTeX isn't needed

### M5 — Voice excellence
- [x] Echo cancellation with the playback as reference (WebRTC AEC3 via `livekit`, `arya/core/echo.py`):
      47 dB echo removed in simulation, your voice kept while ARYA talks; `ARYA_AEC=0` turns it off
- [x] Latency, measured per answer in the log (`Latency: …`). Fixes: one shared Ollama client
      (a new one cost ~0.4 s per answer), `127.0.0.1` instead of `localhost`, clock moved after the
      fixed prompt rules, long first sentence voiced clause-first. End of your speech → first word:
      pause detection 0.72 s + recognition ~0.55 s + first sentence 0.6–1.6 s + voice 0.25–0.4 s
- [ ] Live check: 10 spoken "stop"s during replies → ≥ 9 stop; no self-interruptions in 10 minutes

### Then — PC & life assistant
Study-mode routine, calendar, proactive reminders (battery, breaks, due cards), email summaries
(ask first), file agent, music.

---

## How to check a change
```
pytest                      # fast, no models needed except the small embedder
python -m arya.eval         # golden questions through the real pipeline; compares with the last run
python -m arya.eval --tag control -v
```
