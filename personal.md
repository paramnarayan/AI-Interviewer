# AI Voice Interviewer — Complete Codebase Reference

Personal notes for understanding every part of this project.

---

## Project Layout

```
ai voice interviewer/
├── .env                        # API keys (gitignored)
├── .gitignore
├── requirements.txt            # Root-level (thin); backend/ has the real one
├── backend/
│   ├── requirements.txt        # All Python deps
│   ├── config.py               # Central config — reads .env + sets defaults
│   ├── server.py               # FastAPI app, WebSocket handler, REST endpoints
│   ├── session.py              # Per-interview in-memory state + Redis persistence
│   ├── database.py             # SQLAlchemy async ORM — permanent interview history
│   ├── llm.py                  # Groq LLM: system prompt builder, streaming reply, summary
│   ├── stt.py                  # Speech-to-text via faster-whisper (runs on CPU)
│   ├── tts.py                  # Text-to-speech via Kokoro (runs on CPU)
│   ├── vad.py                  # Voice activity detection via Silero VAD
│   └── routers/
│       ├── __init__.py
│       └── resume.py           # POST /upload-resume endpoint
└── frontend/
    ├── index.html              # Single-page app shell + WebGL shader + wave engine
    ├── main.js                 # All application logic (WS, audio, state machine)
    ├── style.css               # Obsidian design system + all component styles
    └── pcm-worklet.js          # AudioWorklet that forwards mic PCM to the WebSocket
```

---

## How a Full Interview Session Works (End to End)

```
Browser                              FastAPI (server.py)
  │                                        │
  │  1. User fills setup form              │
  │  2. [optional] Selects PDF resume      │
  │     → POST /upload-resume             ──► resume.py: pdfplumber extracts text
  │     ← {"resume_text": "..."}          │
  │                                        │
  │  3. Click "Begin Interview"            │
  │     → WebSocket connect ws://…/ws/interview
  │     → send JSON setup msg  ───────────► set_profile(setup_msg)
  │                                        ├─ create_interview_record() → SQLite
  │                                        └─ session.save() → Redis
  │                                        │
  │  4. Mic audio streams as binary PCM   │
  │     → send bytes continuously ────────► session.audio_buffer.extend(data)
  │                                        │  TurnDetector.process(data)
  │                                        │    Silero VAD: speech prob per 512-sample window
  │                                        │    When speech_started + silence ≥ 1000ms → True
  │                                        │
  │                                        ├─ transcribe(audio_bytes) → faster-whisper
  │     ← {"type":"transcript","text":"…"} │    RMS + duration gate first (skip noise)
  │                                        │    Hallucination filter (removes "thanks" etc.)
  │                                        │
  │                                        ├─ session.add_user_turn(text)
  │                                        ├─ session.save() → Redis
  │                                        ├─ save_message(id,"user",text) → SQLite
  │                                        │
  │                                        ├─ stream_reply(history, profile) → Groq API
  │                                        │    Streams tokens; buffers until sentence-ending
  │                                        │    punctuation (.?!) then synthesizes each sentence
  │     ← binary PCM (sentence 1)         │
  │     ← binary PCM (sentence 2) …       │  synthesize(sentence) → Kokoro TTS
  │     ← {"type":"reply_complete","…"}   │
  │                                        ├─ session.add_assistant_turn(full_reply)
  │                                        ├─ session.save() → Redis
  │                                        └─ save_message(id,"assistant",reply) → SQLite
  │                                        │
  │  5. "End Interview & Get Summary"      │
  │     → send {"type":"end_interview"}   ──► generate_summary(session)
  │                                        │    Formats transcript → Groq JSON mode
  │     ← {"type":"summary","data":{…}}   │    openai/gpt-oss-20b returns structured JSON
  │                                        ├─ save_summary(id, json_str) → SQLite
  │                                        └─ session.clear() → deletes Redis keys
```

---

## File-by-File Deep Dive

### `config.py`

Single source of truth for all configuration. Everything reads from `.env` via `python-dotenv`.

| Variable | Default | What it controls |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API authentication |
| `GROQ_MODEL` | set in `.env` → `qwen/qwen3.6-27b` | Main interview model (reasoning) |
| `GROQ_SUMMARY_MODEL` | `openai/gpt-oss-20b` | Summary-only model (non-reasoning, JSON mode) |
| `WHISPER_MODEL_SIZE` | `small.en` | Whisper model variant (speed vs accuracy) |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | Quantization for CPU inference |
| `KOKORO_LANG_CODE` | `a` | American English |
| `KOKORO_VOICE` | `af_heart` | Voice persona |
| `SAMPLE_RATE_IN` | `16000` | Mic capture rate (Hz) — must match Whisper |
| `SAMPLE_RATE_OUT` | `24000` | TTS output rate (Hz) — must match Kokoro |
| `VAD_SILENCE_MS` | `1000` | How long silence must persist to end a turn |
| `VAD_THRESHOLD` | `0.5` | Silero speech probability cutoff (0–1) |

---

### `server.py`

FastAPI application. Three responsibilities:

**1. App setup**
- `lifespan` async context manager: calls `init_db()` before the server accepts requests, creating SQLite tables if they don't exist.
- `CORSMiddleware` with `allow_origins=["*"]` — open for local dev, lock down for prod.
- Mounts the `frontend/` directory as static files at `/` so the browser can load the UI.
- Includes the `resume_router` (`POST /upload-resume`).

**2. REST endpoint: `GET /history/{candidate_name}`**
- Calls `get_user_interview_history(name)` and returns JSON.
- Used to retrieve a candidate's past interview records.

**3. WebSocket endpoint: `/ws/interview`**
- Each connection gets a fresh `uuid.uuid4()` as `interview_id` — this is both the Redis session key and the SQL primary key.
- Receives the setup JSON first (name, role, company, tech_stack, resume_text).
- Then enters an event loop that handles two message types:
  - **Text** (`msg["text"]`): currently only `{"type": "end_interview"}` — triggers summary generation, DB save, Redis clear.
  - **Binary** (`msg["bytes"]`): raw 16-bit PCM audio chunks from the browser mic.
- All DB calls (`create_interview_record`, `save_message`, `save_summary`) are wrapped in `try/except` so a DB failure never aborts an active session.

---

### `session.py`

`InterviewSession` holds the live state of one interview:

| Attribute | Type | Purpose |
|---|---|---|
| `session_id` | `str` | UUID from server.py — used as Redis key prefix |
| `profile` | `dict` | Candidate info (name, role, company, tech_stack, resume_text) |
| `history` | `list[dict]` | Conversation turns: `[{"role": "user"/"assistant", "content": "..."}]` |
| `audio_buffer` | `bytearray` | Accumulates raw mic PCM between VAD turns |

**Redis methods:**
- `save()` — pipeline-writes `history` + `profile` as JSON strings with a 24h TTL. Called after every completed turn (both user and assistant sides).
- `load()` — restores state from Redis on reconnect (returns `True` if data found).
- `clear()` — deletes both Redis keys after a completed interview (SQL has the permanent copy).

Redis keys: `interview:{session_id}:history` and `interview:{session_id}:profile`

Env vars: `REDIS_URL` (default `redis://localhost:6379`), `REDIS_SESSION_TTL_S` (default 86400).

---

### `database.py`

Async SQLAlchemy ORM for permanent interview history.

**Engine**: reads `DATABASE_URL` env var (default: `sqlite+aiosqlite:///./interviews.db`). To switch to Postgres: `DATABASE_URL=postgresql+asyncpg://user:pass@host/db`.

**Tables:**

`interview_records`
| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID string from server.py |
| `candidate_name` | TEXT | Indexed for history lookup |
| `target_role` | TEXT | |
| `company` | TEXT | Nullable |
| `created_at` | DATETIME TZ | UTC |
| `summary_json` | TEXT | NULL until interview ends; stores JSON string |

`message_records`
| Column | Type | Notes |
|---|---|---|
| `id` | INT PK | Autoincrement |
| `interview_id` | TEXT FK | References `interview_records.id`, CASCADE delete |
| `role` | TEXT | `"user"` or `"assistant"` |
| `content` | TEXT | Full turn text |
| `created_at` | DATETIME TZ | UTC |

**CRUD functions:**
- `init_db()` — creates all tables if missing. Called on startup.
- `create_interview_record(id, profile)` — inserts one row into `interview_records`.
- `save_message(id, role, content)` — inserts one row into `message_records`.
- `save_summary(id, json_str)` — updates `summary_json` on the record.
- `get_user_interview_history(name)` — SELECT WHERE candidate_name = name ORDER BY created_at DESC. Returns list of dicts with parsed summary and message_count.

---

### `llm.py`

Three functions, two Groq models.

**`SystemPrompt(profile)`**
Builds the system prompt string. If `profile["resume_text"]` is present, it appends a fenced resume block instructing the model to reference actual projects. The prompt tells the LLM to: ask one question at a time, build follow-ups from previous answers, give brief feedback, keep responses under 3 sentences.

**`stream_reply(history, profile)`** — async generator
- Sends `[system prompt] + history` to `GROQ_MODEL` (`qwen/qwen3.6-27b`).
- `reasoning_format="hidden"` suppresses Qwen3's internal chain-of-thought from appearing in the output.
- Yields tokens as they stream in.
- In `server.py`, tokens are buffered until punctuation (`.?!`), then each sentence is TTS-synthesized and sent as PCM.

**`generate_summary(session)`**
- Formats the full history into `Candidate: …\nInterviewer: …` lines.
- Sends a structured JSON request to `SUMMARY_MODEL` (`openai/gpt-oss-20b`) with `response_format={"type": "json_object"}`.
- Parses and returns the JSON dict with keys: `overall_impression`, `strengths`, `areas_to_improve`, `communication_notes`, `suggested_next_steps`.
- Falls back to stripping markdown code fences if the model wraps JSON in backticks.

**Why two models?** `qwen/qwen3.6-27b` is a reasoning model — great for natural conversation but unreliable with `json_object` response format. `openai/gpt-oss-20b` is a standard instruction-following model that reliably returns clean JSON.

---

### `stt.py` — Speech-to-Text

Uses `faster-whisper` (CTranslate2 port of OpenAI Whisper) running locally on CPU.

`transcribe(pcm_bytes, context_terms)`:
1. Converts raw int16 PCM → float32 normalized audio.
2. **Duration gate**: skips if audio < 0.5s (avoids wasting inference on tiny fragments).
3. **RMS gate**: skips if audio is too quiet (RMS < 0.01), which catches silence/noise.
4. Builds an optional `initial_prompt` from `context_terms` (candidate name, company, tech stack) to bias Whisper toward recognizing domain-specific words correctly.
5. Runs `whispermodel.transcribe()` with English language and beam_size=5.
6. **Hallucination filter**: Whisper sometimes outputs "thank you" or "thanks for watching" for silence — these are explicitly filtered out.

---

### `tts.py` — Text-to-Speech

Uses `Kokoro` (a fast local TTS model) running on CPU.

`synthesize(text)`:
1. Runs the Kokoro pipeline on the input text, iterating over audio chunks.
2. Concatenates all chunks into a single numpy float32 array.
3. Converts to int16 PCM and returns raw bytes.

Output is sent over the WebSocket as binary frames. The browser's `AudioContext` queues them for gapless playback using `playbackQueueTime` to schedule each buffer exactly where the previous one ends.

---

### `vad.py` — Voice Activity Detection

`TurnDetector` wraps the Silero VAD model to detect when a speaker has finished their turn.

**How it works:**
1. Accumulates incoming PCM in a `bytearray` buffer.
2. Processes in 512-sample windows (32ms at 16kHz).
3. For each window, runs Silero VAD inference to get a speech probability (0–1).
4. State machine:
   - If `prob >= threshold` (0.5): marks `speech_started = True`, resets silence counter.
   - If `speech_started` and `prob < threshold`: increments `silence_sample_count`.
   - When `silence_sample_count >= silence_samples_needed` (16000 samples = 1 second): returns `True` (end of turn) and calls `reset()`.
5. `reset()` clears the buffer and calls `model.reset_states()` to reset Silero's internal GRU state.

---

### `routers/resume.py`

`POST /upload-resume` — single endpoint, no auth required.

1. `_validate_pdf()`: checks filename ends in `.pdf` AND `content_type` is `application/pdf` or `application/x-pdf`. Returns 400 otherwise.
2. Reads the full file bytes.
3. Opens with `pdfplumber`, iterates pages, extracts text per page.
4. Stops early once 4000 chars are accumulated (no need to process a 200-page PDF).
5. Joins page texts with `\n`, strips, checks it's non-empty → 422 if empty (scanned/image PDF).
6. Truncates to 4000 chars and returns `{"resume_text": "..."}`.

---

## Frontend Architecture

### `pcm-worklet.js`

AudioWorklet processor that runs in a dedicated audio thread. Receives 128-sample float32 frames from the mic → converts to int16 → posts to the main thread via `port.postMessage`. The main thread (main.js) forwards these over the WebSocket as binary frames.

### `main.js` — Application State Machine

States: `idle` → `listening` → `processing` → `speaking` → `listening` …

**Key variables:**
- `ws` — the active WebSocket connection
- `audioCtx` — Web Audio API context (16kHz for capture, plays at 24kHz via buffer source)
- `playbackQueueTime` — running timestamp for gapless TTS audio scheduling
- `resumeText` — extracted resume text from `/upload-resume`; included in the WS setup JSON

**Startup flow:**
1. User fills form (name, role, company, tech_stack).
2. Optionally selects a PDF → `change` event → `fetch('/upload-resume', FormData)` → stores `resumeText`.
3. Click "Begin Interview" → `connectWebSocket(profile)` where `profile` includes `resume_text`.
4. On WS `onopen`: sends profile JSON, hides form panel, calls `startMicrophone()`.

**Audio capture:**
- `startMicrophone()` creates an `AudioContext` at 16kHz, gets mic stream, creates `AnalyserNode` (for potential visualizations), and loads the PCM worklet.
- The worklet's `onmessage` handler forwards int16 frames to the WS — but only when `appState === 'listening'` (prevents TTS audio feedback loop).

**Audio playback:**
- `handleAudioMessage(arrayBuffer)`: converts int16 → float32 → creates an `AudioBuffer` → schedules it starting at `playbackQueueTime` for seamless, gap-free playback of multiple sentences.

**Summary modal:**
- Opens immediately when "End Interview" is clicked (shows spinner).
- Populates from `{"type": "summary", "data": {...}}` WS message.
- Has a 35-second timeout fallback to show an error if summary never arrives.

### `index.html`

Static single-page shell. Notable sections:
- **Setup form**: name, role (required), company (optional), tech stack, resume upload (optional).
- **Resume upload zone**: custom `<label>` wrapping a hidden `<input type="file">`. Shows upload state via `data-upload-state` attribute driving CSS classes.
- **Orb wrapper**: contains two `<canvas>` elements — `shader-canvas` (WebGL noise sphere) and `wave-canvas` (circular pulse rings).
- **Transcript panel**: hidden initially, shown once session starts. Entries added by `addTranscriptEntry()`.
- **Summary modal**: contains loading spinner, structured content sections, and an error fallback.
- **WebGL shader** (inline `<script>`): full GLSL fragment shader with simplex noise for the animated obsidian orb. Intensity controlled by `window.setShaderIntensity(val)` — driven by app state.
- **Wave engine** (inline `<script>`): spawns expanding ring animations from the orb, controlled by `window.setWaveActive(bool)`.

---

## Data Flow Summary

```
Mic → PCM worklet → WS (binary) → server.py buffer
                                        ↓
                                   VAD detects turn end
                                        ↓
                                   stt.py (Whisper)
                                        ↓
                                session.add_user_turn()
                                session.save() → Redis
                                save_message() → SQLite
                                        ↓
                                   llm.py stream_reply()
                                   (Groq: qwen3.6-27b)
                                        ↓ tokens
                              sentence buffer → tts.py (Kokoro)
                                        ↓ PCM bytes
                              WS (binary) → browser playback queue
                                        ↓
                                session.add_assistant_turn()
                                session.save() → Redis
                                save_message() → SQLite
```

---

## Environment Variables Reference

| Variable | File read in | Default |
|---|---|---|
| `GROQ_API_KEY` | config.py | required |
| `GROQ_MODEL` | config.py | required (set in .env) |
| `GROQ_SUMMARY_MODEL` | config.py | `openai/gpt-oss-20b` |
| `DATABASE_URL` | database.py | `sqlite+aiosqlite:///./interviews.db` |
| `DB_ECHO` | database.py | `""` (off) |
| `REDIS_URL` | session.py | `redis://localhost:6379` |
| `REDIS_SESSION_TTL_S` | session.py | `86400` (24h) |

---

## Key Design Decisions

- **Two persistence layers**: Redis for live reconnect state (fast, ephemeral), SQLite for permanent history (queryable, durable). Redis is cleared after a successful summary; SQL never is.
- **Two LLM models**: reasoning model (Qwen3) for natural conversation, non-reasoning model (gpt-oss-20b) for reliable structured JSON output.
- **Sentence-level TTS streaming**: the LLM streams tokens and audio is synthesized per-sentence instead of waiting for the full reply — keeps perceived latency low.
- **DB failures don't kill sessions**: all `create_interview_record`, `save_message`, `save_summary` calls are inside `try/except` — a DB outage degrades gracefully without crashing the WebSocket handler.
- **VAD state machine over raw silence detection**: Silero VAD is much more robust than RMS thresholding for distinguishing breathing/background noise from actual pauses between words.
