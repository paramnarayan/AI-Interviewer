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

AI Voice Interviewer enables low-latency, bidirectional conversational interviews. Users speak naturally through their browser; incoming audio is captured at 16 kHz, converted into raw Linear PCM bytes via an AudioWorklet, and streamed over a full-duplex WebSocket connection.

The backend uses a Voice Activity Detector (webrtcvad) with an internal frame buffer to detect turn completion. Transcriptions from faster-whisper are processed by a Groq-hosted language model with hidden reasoning format. The response tokens are incrementally synthesized into 24 kHz audio chunks using Kokoro TTS on sentence boundaries, providing rapid Time-to-First-Audio (TTFA) and smooth conversational turn-taking.

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
|                                              | (Stream Tokens|  |                 |
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
+-----------------------------------------------------------------------------------+
```

---

## Technical Stack & Components

### Client Side (`frontend/`)

- `index.html`: Semantic layout featuring an interactive orb, soundwave visualization container, processing spinner, dynamic status indicators, and live transcript panel.
- `style.css`: Dark-themed design system with ambient background animations, glowing orb gradients, state-specific visual transitions (`idle`, `listening`, `processing`, `speaking`), and responsive layouts.
- `pcm-worklet.js`: Runs on a dedicated Web Audio rendering thread. Quantizes native `Float32` audio samples to signed 16-bit integers (`Int16Array`) with zero-copy buffer transfer.
- `main.js`: Manages the state machine, WebSocket lifecycle, `AnalyserNode` frequency spectrum rendering across 48 radial bars, audio transmission gating, and gapless 24 kHz PCM playback queuing.

### Server Side (`backend/`)

- `server.py`: FastAPI application hosting the WebSocket endpoint `/ws/interview` and static asset serving. Offloads synchronous compute tasks to worker threads via `asyncio.to_thread`.
- `vad.py`: `TurnDetector` wrapping `webrtcvad`. Buffers streaming byte chunks into 30 ms frames (960 bytes at 16 kHz) and triggers an end-of-turn event after a 700 ms silence duration.
- `stt.py`: Automated speech recognition wrapping `faster-whisper` (`small.en`, `int8` on CPU). Includes audio duration checks, RMS energy threshold validation, and hallucination suppression.
- `llm.py`: Asynchronous streaming interface to Groq Cloud (`openai/gpt-oss-20b`), configured with concise interview coaching prompts and `reasoning_format="hidden"`.
- `tts.py`: Speech synthesis using `Kokoro-82M` (American English `af_heart` voice), generating 24 kHz PCM16 audio buffers.
- `session.py`: In-memory state container managing dialogue history and turn-level audio accumulation.
- `config.py`: Environment loader and system parameter definitions.

---

## Pipeline Mechanics

1. **Audio Ingestion**:
   - The browser captures microphone audio at 16,000 Hz.
   - `PCMProcessor` quantizes `Float32` chunks to `Int16` buffers and streams binary frames over the WebSocket during the `listening` state.

2. **Voice Activity Detection**:
   - `TurnDetector` buffers incoming bytes and evaluates 30 ms slices (480 samples / 960 bytes).
   - Once speech has commenced, the detector monitors for 700 ms of consecutive silence (23 frames).
   - Upon silence confirmation, the turn is finalized, and accumulated bytes are passed to STT.

3. **Validation & Speech-to-Text**:
   - Audio is verified against a minimum duration (0.5s) and minimum RMS energy threshold (0.01) to eliminate silent background noise.
   - `faster-whisper` executes fast greedy transcription (`beam_size=1`).
   - Known subtitle hallucinations (e.g., "Thank you", "Thanks for watching") are rejected.

4. **Streaming LLM & Chunked Synthesis**:
   - Transcribed text is added to the conversation history and sent to the Groq API.
   - As tokens stream back, they accumulate in a sentence buffer.
   - When sentence terminators (`.`, `?`, `!`) are encountered, the segment is immediately synthesized into 24 kHz PCM audio and dispatched to the client.

5. **Scheduled Gapless Playback**:
   - The browser receives binary audio chunks, normalizes them to `Float32Array`, and schedules them sequentially using Web Audio timeline timestamps (`playbackQueueTime`).

---

## Prerequisites & Dependencies

### System Requirements
- Python 3.10+ (tested on Python 3.12)
- Modern Web Browser with Web Audio API and AudioWorklet support
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
GROQ_MODEL=openai/gpt-oss-20b
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

Click the central orb to start your mock interview session.

---

## WebSocket Protocol Specification

Endpoint: `ws://<host>:<port>/ws/interview`

### Client -> Server (Upstream)
- **Binary Format**: Raw 16-bit Linear PCM (16 kHz, single channel, Little-Endian). Sent exclusively during the `listening` state.

### Server -> Client (Downstream)
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

---

## Configuration Reference

Key settings located in `backend/config.py`:

| Parameter | Default Value | Description |
|---|---|---|
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Large language model endpoint hosted on Groq |
| `WHISPER_MODEL_SIZE` | `small.en` | faster-whisper model variant |
| `WHISPER_DEVICE` | `cpu` | Inference target device (`cpu` or `cuda`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | Model quantization precision |
| `KOKORO_LANG_CODE` | `a` | Synthesis language (`a` = American English) |
| `KOKORO_VOICE` | `af_heart` | Synthesizer voice profile |
| `SAMPLE_RATE_IN` | `16000` | Input capture rate (Hz) |
| `SAMPLE_RATE_OUT` | `24000` | Output playback synthesis rate (Hz) |
| `VAD_SILENCE_MS` | `700` | Silence duration required to conclude user turn (ms) |

---

## Engineering Highlights

- **Feedback Loop Mitigation**: Client-side mic streaming is muted during `processing` and `speaking` states to prevent speaker output from re-entering the microphone.
- **Non-Blocking Architecture**: Synchronous C-extension inferences (`faster-whisper`, `kokoro`) run in dedicated worker threads using `asyncio.to_thread`, keeping the event loop responsive.
- **Low Latency Sentence Streaming**: Combining token streaming with sentence-level TTS synthesis delivers responses with minimal Time-to-First-Audio (TTFA).
- **VAD Chunk Normalization**: Internal byte accumulation handles variable browser chunk sizes (128 samples / 256 bytes) and aligns them into strict 30 ms WebRTC VAD frames (960 bytes).
