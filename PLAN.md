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

### Then — PC & life assistant (Phase B)
- [x] Study mode (`arya/study/session.py`): "start studying Dynamics for an hour" opens the course
      folder, starts pomodoros and says what to cover (cards due, new cards, next exercise, exam
      countdown); "how long have I been studying?"; "stop studying" sums up time and cards answered
- [x] Calendar (`arya/tools/agenda.py`): ARYA's own events ("add Control lab every Thursday at 2 pm
      in room B12"), `.ics` files in `%LOCALAPPDATA%\ARYA\calendar\` (weekly lectures expanded,
      time zones converted), optional `ARYA_CALENDAR_URLS` subscriptions. "What's on tomorrow?",
      "what's next?", "am I free at 3?"; in the briefing; heads-up 10 min before an event
- [x] Proactive nudges (`arya/core/proactive.py`): battery at 20 % / 10 % unplugged, a break after
      90 min of continuous use, practice cards due once a day. Quiet during quiet hours (22-8), when
      you're away, while ARYA talks, and when Windows is in full screen / presentation / Do Not
      Disturb. "Do not disturb for an hour", "no nudges", "nudges back on"
- [x] Email (`arya/tools/mail.py`): read-only IMAP with an app password (`ARYA_MAIL_HOST`,
      `ARYA_MAIL_USER`, `ARYA_MAIL_PASSWORD` in `.env`). "Check my email" (instant senders and
      subjects), "summarise my emails", "what did Prof Weber email me?". Local model only; email
      text is fenced as untrusted data; nothing is marked read or changed
- [x] File agent (`arya/tools/file_agent.py`): "what did I download today?", "open the last PDF I
      downloaded", "find the slides I downloaded yesterday", "what's taking up space?", "tidy my
      Downloads" (asks first, sorts by type, never deletes, skips today's/unfinished files),
      "undo the tidy"
- [x] Music (`arya/tools/music.py`, Windows media controls — any player or browser): "what's
      playing?", "play focus music" (`ARYA_FOCUS_MUSIC_URL`, also started by study mode), and music
      pauses when "Hey ARYA" is heard and resumes after the answer (`ARYA_PAUSE_MUSIC=0` to disable;
      never resumed if you paused it yourself; watchdog for false wakes)
- [x] Browser (`arya/tools/browser.py`): Chrome tabs by voice (new, close, reopen, next/previous,
      "go to tab 3"; keys only sent when Chrome is in front), "search Google/YouTube for …",
      "play X on YouTube Music" (opens the first result)
- [x] Window control (`arya/tools/windows.py`): minimize / maximize / restore an app, "show the desktop"

## Phase C — after Phase B (order agreed 2026-11-15: C1 → C2 → C3 → C4 → C5)

### C1 — Prove it in daily use
- [x] Timestamped log; `python -m arya.report`: turns by route, real latency, wake/stop stats, ignored
      speech you overrode, blocked false claims, errors; `--candidates` lists real failures for the benchmark
- [x] Routing benchmark (`arya/eval/routes.yaml`, `python -m arya.eval --routes`): every handler, plus
      questions that must reach the model; side effects recorded, never performed. Found and fixed:
      "set a study session" missed study mode, focus-music help blocked by the claim guard, CPU usage
      went through the slow tool path
- [x] Guided live voice check: `python -m arya.voicecheck` (10 "stop" trials, 10 min self-interruption)
- [ ] Run the voice check and act on it; first field-report numbers (2026-11-15, 2 days): first words
      median 1.6 s but end of speech → first word ≈ 3.4 s; recognition median 1.07 s live; 68 wake
      rejections vs 36 accepted

### C2 — A smarter brain within 8 GB
- [x] Model comparison (golden + hard questions + routes, with ARYA's Whisper on the GPU, 2026-11-16):
      | model | golden (of 77) | hard (of 16) | first sentence median / slowest 10 % | memory |
      | qwen2.5 3B (kept) | 73–74 | 12–14 | ~1.0 s / 2.6–3.2 s | 2.3 GB, 100 % GPU |
      | phi-4-mini 3.8B | 72 | 15 | 3.2 s / 11.3 s | spills to CPU |
      | llama3.2 3B | 58–61 | 9–11 | 2.4 s / 6–8 s | 3.4 GB, 33 % CPU |
      | qwen3 4B | — | — | 10–18 s | 3.5 GB, 34 % CPU; thinking-only, ignores think:false |
      With 4 GB VRAM and Whisper medium (~1.5 GB) only ~2.3 GB is left: every 4B model spills to the CPU.
      Revisit with a GPU with more memory, or Whisper small to free VRAM (costs recognition accuracy).
- [x] Trimmed history: last 6 messages, older turns cut to 280 chars (same score, first sentence ~0.1 s sooner)
- [x] Found on the way: "100 microfarad" read as 100 F (RC time constant 1,000,000 s); PT2 and
      mass-doubling answers wrong → core notes + benchmark questions

### C3 — Exam season
- [x] Lecture recordings → transcripts → library (`arya/knowledge/lectures.py`): 225 unique recordings
      (86.8 GB, 46 copies skipped) transcribed with ARYA's own Whisper while you're away 10 min (or
      "transcribe my lectures now"), 30 s at a time, resumable; sources read "lecture, at 24:00";
      exam courses first; "how far are the lecture transcriptions?", "pause transcribing"
- [x] Timed mock exams (`arya/study/mock_exam.py`): "give me a 60-minute Dynamics mock exam" — one task
      per exercise (~12 min each, old-exam exercises first), task pages from the exercise sheets (never a
      solution sheet: 41 Dynamics exercises usable), clock with a 5-minute warning, answers by voice,
      graded against the answer sheets, every task recorded for spaced repetition / weak topics
- [x] Photo of your handwritten solution checked (`arya/study/solution_check.py`): "check my solution for
      exercise 2-2" asks first (only the photo + task text + official result go out); the cloud names the first
      wrong step, ARYA itself checks the final value against the answer sheet; recorded for spaced repetition

### C4 — Routines and memory
- [ ] Your own voice routines ("start my day" → briefing, calendar, focus music)
- [ ] Morning briefing on first activity after 7:00
- [ ] Long-term preferences ("remember I prefer short answers")

### C5 — ARYA everywhere
- [ ] Phone access over home Wi-Fi (token-protected app), nudges on the phone
- [ ] Start at login, restart after a crash, daily backup of arya.db

---

## How to check a change
```
pytest                      # fast, no models needed except the small embedder
python -m arya.eval         # golden questions through the real pipeline, then the routing benchmark
python -m arya.eval --routes          # only routing: which part of ARYA answers each command
python -m arya.eval --tag control -v
python -m arya.report       # how ARYA did in real use (from its log); --candidates for new benchmark items
python -m arya.voicecheck   # guided live test of "stop" and self-interruption (ARYA must be running)
```
