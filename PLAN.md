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

### M1 — Trustworthy answers
- [ ] Hard-question router: offers the cloud for derivations / exam problems, asks first, sends only question + excerpt
- [ ] `sympy` maths tool — numbers come from a tool, never from the model's head
- [ ] Corrections memory: "that's wrong — …" is saved and outranks other sources next time
- [ ] Checked one-page fundamentals per course in the core notes

### M2 — Exercise tutor
- [ ] Walkthrough mode (given / wanted / idea, Socratic next steps, hints)
- [ ] "Check my answer" against the answers page (tolerance + units)
- [ ] Handwritten worked solutions explained via cloud vision (ask first)

### M3 — Practice & planning
- [ ] Quizzes generated from your own material; spaced repetition (SM-2), weak-topic tracking
- [ ] Exam planner folded into the daily briefing; timers and reminders that survive restarts
- [ ] Progress view in the app

### M4 — See what you see
- [ ] "Explain this" on the screen (screenshot + OCR); photos of paper notes
- [ ] Formulas rendered properly in the app (KaTeX)

### M5 — Voice excellence
- [ ] Echo cancellation with the playback as reference → reliable barge-in, full duplex
- [ ] Wake → first word under 2.5 s

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
