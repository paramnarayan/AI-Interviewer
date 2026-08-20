# AI Voice Interviewer

An end-to-end, real-time voice interview application built with FastAPI WebSockets, Web Audio API, Voice Activity Detection (VAD), Speech-to-Text (faster-whisper), LLM streaming (Groq API), and Text-to-Speech (Kokoro-82M).

---

## Table of Contents

- [Overview](#overview)
- [Architecture & System Flow](#architecture--system-flow)
- [Technical Stack & Components](#technical-stack--components)
- [Pipeline Mechanics](#pipeline-mechanics)
- [Prerequisites & Dependencies](#prerequisites--dependencies)
- [Installation & Setup](#installation--setup)
- [Running the Application](#running-the-application)
- [WebSocket Protocol Specification](#websocket-protocol-specification)
- [Configuration Reference](#configuration-reference)
- [Engineering Highlights](#engineering-highlights)

---

## Overview

AI Voice Interviewer enables low-latency, bidirectional conversational mock interviews. Before starting, the user fills out a short setup form (name, target role, company, tech stack). These profile details are sent to the backend as the first WebSocket message and are used to personalize the AI interviewer's questions and system prompt throughout the session.

Once the session begins, users speak naturally through their browser; incoming audio is captured at 16 kHz, converted into raw Linear PCM bytes via an AudioWorklet, and streamed over a full-duplex WebSocket connection.

The backend uses a Voice Activity Detector (webrtcvad) with an internal frame buffer to detect turn completion. Transcriptions from faster-whisper are processed by a Groq-hosted Qwen language model with hidden reasoning format. The response tokens are incrementally synthesized into 24 kHz audio chunks using Kokoro TTS on sentence boundaries, providing rapid Time-to-First-Audio (TTFA) and smooth conversational turn-taking.

At the end of a session, the user can click **End Interview & Get Summary** to receive a structured AI-generated performance critique covering strengths, areas to improve, communication notes, and suggested next steps.

---

## Architecture & System Flow

```
+-----------------------------------------------------------------------------------+
|                                  CLIENT (BROWSER)                                 |
|                                                                                   |
|  +-------------------+     +--------------------+     +------------------------+  |
|  | getUserMedia()    | --> | PCMProcessor       | --> | WebSocket Connection   |  |
|  | (16 kHz Mic Input)|     | (AudioWorkletNode) |     | (/ws/interview)        |  |
|  +-------------------+     +--------------------+     +------------------------+  |
|            |                                                       ^     |        |
|            v                                                       |     v        |
|  +-------------------+                                 +---------------+ |        |
|  | AnalyserNode      |                                 | AudioContext  | | (PCM)  |
|  | (FFT Visualizer)  |                                 | (24 kHz Queue)|<+        |
|  +-------------------+                                 +---------------+          |
|                                                                                   |
|  [ Setup Form ] --> Profile JSON --> first WS message                            |
|  [ End Interview Btn ] --> end_interview JSON --> summary modal                  |
+-----------------------------------------------------------------------------------+
                                         |
                                         | Binary PCM / JSON Payloads
                                         v
+-----------------------------------------------------------------------------------+
|                                 SERVER (FASTAPI)                                  |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | WebSocket Route: /ws/interview                                              |  |
|  +-----------------------------------------------------------------------------+  |
|         |                                                       ^                 |
|         v (Raw PCM16 Chunks)                                    |                 |
|  +--------------------+                                         |                 |
|  | TurnDetector (VAD) |                                         |                 |
|  | (Frame Accumulator)| --[Turn Complete]--> +---------------+  |                 |
|  +--------------------+                      | faster-whisper|  |                 |
|                                              | (STT Engine)  |  |                 |
|                                              +---------------+  |                 |
|                                                      |          |                 |
|                                                      v (Text)   |                 |
|                                              +---------------+  |                 |
|                                              | Groq LLM API  |  |                 |
|                                              | (Qwen 3.6-27B)|  |                 |
|                                              | Stream Tokens  |  |                 |
|                                              +---------------+  |                 |
|                                                      |          |                 |
|                                                      v (Tokens) |                 |
|                                              +---------------+  |                 |
|                                              | Sentence Chunk|  |                 |
|                                              | Splitter      |  |                 |
|                                              +---------------+  |                 |
|                                                      |          |                 |
|                                                      v (Text)   |                 |
|                                              +---------------+  |                 |
|                                              | Kokoro TTS    | -+ (24 kHz PCM)    |
|                                              | (Synthesizer) |                    |
|                                              +---------------+                    |
|                                                                                   |
|  [ end_interview JSON ] --> generate_summary() --> summary JSON --> client       |
+-----------------------------------------------------------------------------------+
```

---

## Technical Stack & Components

### Client Side (`frontend/`)

- `index.html`: Two-column layout featuring a **Session Setup form** (left panel) and the interactive orb column (right). Includes: WebGL shader orb, circular wave canvas, processing spinner, dynamic status indicators, live transcript panel, **End Interview** button, and a full **Summary Modal** with structured feedback sections.
- `style.css`: Dark-themed design system with ambient background animations, glowing orb gradients, state-specific visual transitions (`idle`, `listening`, `processing`, `speaking`), setup form styles, and summary modal styles.
- `pcm-worklet.js`: Runs on a dedicated Web Audio rendering thread. Quantizes native `Float32` audio samples to signed 16-bit integers (`Int16Array`) with zero-copy buffer transfer.
- `main.js`: Manages the full application lifecycle — setup form validation, profile collection, WebSocket connection, state machine, `AnalyserNode` frequency spectrum rendering, audio transmission gating, gapless 24 kHz PCM playback queuing, **End Interview** flow, and **Summary Modal** population.

#### WebGL Shader Orb (inline in `index.html`)
An inline GLSL fragment shader renders the central orb using simplex noise on a WebGL canvas. The shader accepts a `u_intensity` uniform (driven by `window.setShaderIntensity`) that smoothly modulates noise speed and color tone across the four app states. A silver rim highlight and pulsing core react to the `speaking` state. Mouse position subtly warps the UV field.

#### Circular Wave Engine (inline in `index.html`)
A canvas-based animation emits concentric ripple rings outward from the orb during `listening` and `speaking` states. Waves spawn every 900 ms, travel from the orb radius to a max radius of 260 px, and fade out smoothly via an eased opacity curve. Controlled via `window.setWaveActive`.

### Server Side (`backend/`)

- `server.py`: FastAPI application hosting the WebSocket endpoint `/ws/interview` and static asset serving. On connection, reads the setup JSON profile as the first message, then enters the main audio loop. Handles both binary PCM frames and JSON control messages (`end_interview`). Offloads synchronous compute to worker threads via `asyncio.to_thread`.
- `vad.py`: `TurnDetector` wrapping `webrtcvad`. Configurable `aggressiveness` level (default: `1`). Buffers streaming byte chunks into 30 ms frames (960 bytes at 16 kHz) and triggers an end-of-turn event after 1000 ms of silence.
- `stt.py`: Automated speech recognition wrapping `faster-whisper` (`small.en`, `int8` on CPU). Includes audio duration checks, RMS energy threshold validation, and hallucination suppression. Accepts an optional `context_terms` list (populated from the session profile — name, company, tech stack) which is injected as `initial_prompt` for improved transcription accuracy of domain-specific terminology. Uses `beam_size=5` for higher transcription quality.
- `llm.py`: Asynchronous streaming interface to Groq Cloud (`qwen/qwen3.6-27b`), configured with `reasoning_format="hidden"`. The system prompt is dynamically built from the session profile via `SystemPrompt()`, tailoring question focus to the candidate's name, role, company, and tech stack. Also provides `generate_summary()` — a non-streaming call that produces a structured JSON performance critique after the session ends.
- `tts.py`: Speech synthesis using `Kokoro-82M` (American English `af_heart` voice), generating 24 kHz PCM16 audio buffers.
- `session.py`: In-memory state container managing dialogue history, turn-level audio accumulation, and the session profile dict.
- `config.py`: Environment loader and system parameter definitions, including `VAD_AGGRESSIVENESS`.

---

## Pipeline Mechanics

1. **Session Setup**:
   - The user fills in their name, target role, optional company, and tech stack in the setup form.
   - On clicking **Begin Interview**, the form is validated, a WebSocket connection is established, and the profile JSON is sent as the very first message to the server.
   - The server calls `session.set_profile()` and extracts context terms (name, company, tech stack tokens) for use in STT prompting.

2. **Audio Ingestion**:
   - The browser captures microphone audio at 16,000 Hz.
   - `PCMProcessor` quantizes `Float32` chunks to `Int16` buffers and streams binary frames over the WebSocket during the `listening` state.

3. **Voice Activity Detection**:
   - `TurnDetector` buffers incoming bytes and evaluates 30 ms slices (480 samples / 960 bytes) using aggressiveness level `1`.
   - Once speech has commenced, the detector monitors for 1000 ms of consecutive silence (33 frames).
   - Upon silence confirmation, the turn is finalized, and accumulated bytes are passed to STT.

4. **Validation & Speech-to-Text**:
   - Audio is verified against a minimum duration (0.5 s) and minimum RMS energy threshold (0.01) to eliminate silent background noise.
   - `faster-whisper` executes greedy transcription with `beam_size=5`.
   - If `context_terms` are available, an `initial_prompt` is prepended to help the model correctly transcribe names, companies, and stack-specific terminology.
   - Known subtitle hallucinations (e.g., "Thank you", "Thanks for watching") are rejected.

5. **Streaming LLM & Chunked Synthesis**:
   - Transcribed text is added to the conversation history and sent to the Groq API (Qwen 3.6-27B) with a profile-personalized system prompt.
   - As tokens stream back, they accumulate in a sentence buffer.
   - When sentence terminators (`.`, `?`, `!`) are encountered, the segment is immediately synthesized into 24 kHz PCM audio and dispatched to the client.

6. **Scheduled Gapless Playback**:
   - The browser receives binary audio chunks, normalizes them to `Float32Array`, and schedules them sequentially using Web Audio timeline timestamps (`playbackQueueTime`).

7. **End Interview & Summary**:
   - The user clicks **End Interview & Get Summary**; the client sends `{"type": "end_interview"}` over the WebSocket.
   - The server calls `generate_summary(session)`, which submits the conversation transcript to the Groq API and receives a structured JSON object.
   - The JSON is sent back as `{"type": "summary", "data": {...}}` and rendered in the Summary Modal with sections for Overall Impression, Strengths, Areas to Improve, Communication Notes, and Next Steps.
   - On error, a `{"type": "summary_error"}` message is sent and the modal shows a fallback error state.

---

## Prerequisites & Dependencies

### System Requirements
- Python 3.10+ (tested on Python 3.12)
- Modern Web Browser with Web Audio API, AudioWorklet, and WebGL support
- Groq API Key

### Core Python Packages
- `fastapi`
- `uvicorn[standard]`
- `python-dotenv`
- `faster-whisper`
- `webrtcvad`
- `groq`
- `kokoro`
- `soundfile`
- `numpy`
- `torch`

---

## Installation & Setup

### 1. Clone Repository
```bash
git clone https://github.com/paramnarayan/AI-Interviewer.git
cd AI-Interviewer
```

### 2. Create Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
pip install -r backend/requirements.txt
```

### 4. Configure Environment Variables
Create a `.env` file in the project root:
```env
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=qwen/qwen3.6-27b
```

---

## Running the Application

### 1. Start Backend Server
Run Uvicorn from the `backend` directory:
```bash
cd backend
uvicorn server:app --host 0.0.0.0 --port 8001
```

### 2. Access the Application
Open your web browser and navigate to:
```
http://localhost:8001
```

Fill in the **Interview Setup** form on the left panel (name, role, optional company, tech stack), then click **Begin Interview** to start your mock session.

---

## WebSocket Protocol Specification

Endpoint: `ws://<host>:<port>/ws/interview`

### Client → Server (Upstream)

- **JSON — Session Setup** *(first message after connection)*:
  ```json
  {
    "type": "setup",
    "name": "Alex Kim",
    "role": "Backend Engineer",
    "company": "Google",
    "tech_stack": ["Python", "FastAPI", "PostgreSQL"]
  }
  ```
- **Binary Format**: Raw 16-bit Linear PCM (16 kHz, single channel, Little-Endian). Sent exclusively during the `listening` state.
- **JSON — End Interview**:
  ```json
  { "type": "end_interview" }
  ```

### Server → Client (Downstream)

- **Binary Format**: Raw 16-bit Linear PCM (24 kHz, single channel, Little-Endian) representing synthesized speech chunks.
- **JSON Messages**:
  - User Transcript:
    ```json
    {
      "type": "transcript",
      "text": "I specialize in backend distributed systems and asynchronous programming."
    }
    ```
  - Assistant Turn Complete:
    ```json
    {
      "type": "reply_complete",
      "text": "Great background. How do you approach data consistency across microservices?"
    }
    ```
  - Interview Summary:
    ```json
    {
      "type": "summary",
      "data": {
        "overall_impression": "...",
        "strengths": ["..."],
        "areas_to_improve": ["..."],
        "communication_notes": "...",
        "suggested_next_steps": ["..."]
      }
    }
    ```
  - Summary Error:
    ```json
    { "type": "summary_error", "message": "..." }
    ```

---

## Configuration Reference

Key settings located in `backend/config.py`:

| Parameter | Default Value | Description |
|---|---|---|
| `GROQ_MODEL` | `qwen/qwen3.6-27b` | Large language model endpoint hosted on Groq |
| `WHISPER_MODEL_SIZE` | `small.en` | faster-whisper model variant |
| `WHISPER_DEVICE` | `cpu` | Inference target device (`cpu` or `cuda`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | Model quantization precision |
| `KOKORO_LANG_CODE` | `a` | Synthesis language (`a` = American English) |
| `KOKORO_VOICE` | `af_heart` | Synthesizer voice profile |
| `SAMPLE_RATE_IN` | `16000` | Input capture rate (Hz) |
| `SAMPLE_RATE_OUT` | `24000` | Output playback synthesis rate (Hz) |
| `VAD_SILENCE_MS` | `1000` | Silence duration required to conclude user turn (ms) |
| `VAD_AGGRESSIVENESS` | `1` | webrtcvad aggressiveness level (0–3; higher = more aggressive noise filtering) |

---

## Engineering Highlights

- **Profile-Personalized Interviews**: The session profile (name, role, company, tech stack) dynamically generates the LLM system prompt, produces role-tailored questions, and seeds the Whisper `initial_prompt` to improve transcription of domain-specific terms.
- **AI Performance Summary**: At session end, the full conversation history is submitted to the LLM for a structured JSON critique — covering strengths, improvement areas, communication quality, and actionable next steps — rendered in a polished summary modal.
- **WebGL Shader Orb**: An inline GLSL simplex-noise shader drives the orb's visual state. The `u_intensity` uniform (smoothly lerped in the render loop) modulates noise speed, color depth, rim glow, and core pulse across all four app states without any CSS or DOM overhead.
- **Circular Wave Engine**: A canvas-based ripple system emits concentric rings during active states. Wave lifecycle (spawn interval, travel distance, opacity easing, line thinning) is fully frame-rate-independent via delta-time scaling.
- **Feedback Loop Mitigation**: Client-side mic streaming is gated to the `listening` state only, preventing speaker output from re-entering the microphone during `processing` and `speaking` states.
- **Non-Blocking Architecture**: Synchronous C-extension inferences (`faster-whisper`, `kokoro`) run in dedicated worker threads using `asyncio.to_thread`, keeping the event loop responsive.
- **Low Latency Sentence Streaming**: Combining token streaming with sentence-level TTS synthesis delivers responses with minimal Time-to-First-Audio (TTFA).
- **VAD Chunk Normalization**: Internal byte accumulation handles variable browser chunk sizes (128 samples / 256 bytes) and aligns them into strict 30 ms WebRTC VAD frames (960 bytes).

