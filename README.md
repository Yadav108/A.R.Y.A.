# ARYA — Voice Assistant

A local voice assistant built for a Mechatronics engineering student.  
Speak into your browser, get a spoken response back — with tool support for weather, system info, notes, study aids, timers, and more.

---

## How It Works

```
Browser mic  →  WebSocket  →  Intent classifier (llama-3.1-8b-instant)
                                    │
                          ┌─────────┴──────────┐
                       casual chat           needs a tool
                          │                     │
                    stream tokens       llama-3.3-70b-versatile
                          │               (tool call + result)
                          └─────────┬──────────┘
                               edge-tts TTS
                                    │
                             MP3 audio → browser
```

1. Your speech is transcribed by the browser's Web Speech API.
2. A fast 8B model decides whether the message needs a tool or is just conversation.
3. **Casual chat** streams token-by-token back to the browser.
4. **Tool requests** go to the 70B model, which picks and calls the right tool, then the result is summarised and spoken.
5. Responses are synthesised sentence-by-sentence with Microsoft Edge TTS and played as a sequential audio queue.

All conversation history and notes are stored locally in a SQLite database.

---

## Features

### Conversation
- Streaming responses with live audio playback
- 20-message conversation history (persisted in SQLite)
- Push-to-talk interface in the browser

### Tools

| Category | Tools |
|---|---|
| **Weather** | Current conditions via Open-Meteo (no API key needed) |
| **System** | CPU usage, RAM usage, battery level |
| **OS** | Open apps, open URLs, search for files, run terminal commands |
| **Productivity** | Set timers, start Pomodoro sessions (25 min focus / 5 min break), read/write clipboard |
| **Knowledge** | Calculator, Wikipedia summaries |
| **Notes** | Save, retrieve, search, and tag notes (stored in SQLite) |
| **Study** | Explain concepts (simple / technical / exam mode), formula lookup, flashcard quizzes |
| **Daily briefing** | Time, weather, recent notes, and system status in one summary |

---

## Stack

| Layer | Technology |
|---|---|
| LLM | Local [Ollama](https://ollama.com) `qwen2.5:3b` by default (offline); [Groq](https://groq.com) `gpt-oss` only on request |
| Backend | FastAPI + Uvicorn, WebSocket |
| TTS | [Piper](https://github.com/OHF-Voice/piper1-gpl) — `en_GB-jenny_dioco-medium`, local (optional: `edge-tts`) |
| Database | SQLite (via `sqlite3` stdlib) |
| Weather | [Open-Meteo](https://open-meteo.com) — free, no key |
| Frontend | Vanilla HTML/JS — Web Speech API, AudioContext |
| OS integration | `pywin32`, `psutil`, `pyperclip` |

---

## Requirements

- Python 3.11+
- A [Groq API key](https://console.groq.com) (free tier available)
- Windows (for `pywin32`, `os.startfile`, clipboard tools)
- Chrome or Edge (Web Speech API for voice input)

---

## Setup

**1. Clone and create a virtual environment**
```bash
git clone <repo-url>
cd ARYA
python -m venv .venv
.venv\Scripts\activate
```

**2. Install dependencies**
```bash
pip install -r requirements.txt
```

**3. Configure environment**

Create a `.env` file in the project root:
```env
GROQ_API_KEY=your_groq_api_key_here   # optional — only for "ask the cloud, …"

# Used for the daily briefing weather pull
DEFAULT_CITY=Berlin
DEFAULT_LAT=52.52
DEFAULT_LON=13.405

# Optional
USER_NAME=Aryan                    # ARYA addresses you by name
GROQ_MODEL=openai/gpt-oss-120b     # tool-calling model
GROQ_FAST_MODEL=openai/gpt-oss-20b # intent, chat, follow-ups
AUDIO_OUTPUT=local                 # local = PC speakers (hands-free); browser = play in the tab
WAKE_ACCURATE_COMMANDS=1           # re-transcribe commands with Whisper medium (0 to save RAM)
ARYA_MODE=offline                  # offline (default) = local brain; cloud = Groq first
OLLAMA_MODEL=qwen2.5:3b            # local brain
TTS_ENGINE=piper                   # piper (local, default) | edge (online Microsoft voice)
PIPER_VOICE=en_GB-jenny_dioco-medium
```

**Hands-free use:** run `python main.py` and just talk — no browser tab needed.
Say "Hey ARYA, …" (or just "ARYA" and wait for "Yes?"). Once woken, the conversation stays open:
just keep talking, no wake word needed, until 8 minutes after the last exchange
(`CONVERSATION_MINUTES=5` to change it). Say "Hey ARYA, stop" to interrupt; "that's all", "thanks" or "bye" to end.

**Wake word ("Hey ARYA").** ARYA uses a dedicated keyword-spotting model (openWakeWord
features + a small classifier trained for "Hey ARYA") instead of transcribing everything it hears.
Every detection is double-checked: the audio that triggered it is transcribed and must
actually contain "ARYA", so sound-alikes ("I am", "Iron the shirt…") are dropped silently.
A synthetic-voice model ships in `arya/models/wakeword/`. To tune it to *your* voice and mic
(strongly recommended, ~2 minutes):
```bash
.venv\Scripts\python.exe tools\record_wakeword.py   # say "Hey ARYA" 12x, talk normally, stay quiet
.venv\Scripts\python.exe tools\train_wakeword.py    # retrain (re-uses cached TTS clips)
```
Your recordings stay local (`arya/models/wakeword/user_samples/`, git-ignored).
Tuning: `WAKE_THRESHOLD=0.6` (higher = fewer false triggers, lower = more sensitive);
`WAKE_VERIFY=0` skips the double-check (faster, more false wakes);
`WAKE_ENGINE=whisper` switches back to the old Whisper-based detection.

**Offline by default (privacy).** ARYA's brain and voice run on this laptop:
```bash
winget install Ollama.Ollama
ollama pull qwen2.5:3b
.venv\Scripts\python.exe -m piper.download_voices --download-dir arya/models/tts en_GB-jenny_dioco-medium
```
Nothing you say leaves the machine — except when you explicitly ask, e.g. *"Ask the cloud,
explain the Navier–Stokes equations"*: then **only that one question** goes to Groq (no history,
memories, name or location), and the next request is local again. Weather and Wikipedia
lookups send only the city / search term. Volume and media commands ("volume up", "skip this
song") run instantly without any model. `ARYA_MODE=cloud` restores the old Groq-first behaviour.

**4. Run**
```bash
python main.py
```

Then open **http://localhost:8000** in Chrome or Edge.

**Security.** ARYA can run commands and read your clipboard, so it only obeys this computer:
- The server listens on `127.0.0.1` only — nothing else on your network can connect.
- Every WebSocket needs a secret token, generated once into `%LOCALAPPDATA%\ARYA\token`
  (outside the repo). The page gets it automatically when opened on this machine; other
  scripts read the file and connect to `ws://127.0.0.1:8000/ws?token=...` (see `test_ws.py`).
- Requests with a foreign `Host` header (DNS rebinding) or from other websites (`Origin`) are refused.
- Every action ARYA takes and every connection attempt is logged locally to
  `%LOCALAPPDATA%\ARYA\logs\actions.log` (tool + arguments, free text redacted; no conversation text).
- LAN access is opt-in: `ARYA_HOST=0.0.0.0 ARYA_ALLOWED_HOSTS=<your-pc-ip>`, then open
  `http://<your-pc-ip>:8000/#token=<token>` on the other device. Delete the token file to rotate it.

---

## Usage

- **Hold the button** and speak. Release when done.
- ARYA responds in text (shown in the textarea) and audio (played automatically).
- The status line shows: `Ready → Listening → Thinking → Speaking`.

### Example prompts

```
What is the weather in Munich?
Open Spotify
Set a timer for 10 minutes
Start a pomodoro session for control theory
Explain PID controllers simply
What is the formula for transfer functions?
Quiz me on 5 questions on state space
Take a note: review Laplace transforms before Thursday
What's in my clipboard?
Morning briefing
```

---

## Project Structure

```
ARYA/
├── main.py                   # Entry point (uvicorn)
├── requirements.txt
├── .env                      # Not committed — see Setup
│
└── arya/
    ├── core/
    │   ├── llm.py            # LLM logic, tool dispatch, streaming
    │   └── tts.py            # Piper (local) / edge-tts synthesis
    ├── memory/
    │   ├── database.py       # Conversation history (SQLite)
    │   └── arya.db           # Auto-created on first run
    ├── server/
    │   └── websocket.py      # FastAPI app + WebSocket handler
    ├── static/
    │   └── index.html        # Browser UI
    └── tools/
        ├── weather.py        # Open-Meteo
        ├── os_tools.py       # App/URL/file/command
        ├── productivity.py   # Timers, Pomodoro, clipboard
        ├── knowledge.py      # Calculator, Wikipedia
        ├── notes.py          # Notes CRUD (SQLite)
        ├── study.py          # Explain, formula, quiz
        └── system_info.py    # CPU, RAM, battery
```

---

## Known Limitations

- Voice input requires **Chrome or Edge** (Web Speech API is not supported in Firefox).
- The `run_command` tool executes real Windows shell commands — use with caution.
- Only one tool call is made per message (parallel tool calls are disabled).
- Groq free-tier has rate limits; heavy use may hit daily token caps.
