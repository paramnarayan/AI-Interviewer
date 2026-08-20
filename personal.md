# Technical Architecture & Codebase Deep-Dive Guide

This document provides a thorough, component-by-component explanation of the AI Voice Interviewer codebase. It is designed to serve as both an architectural reference and an interview preparation guide covering real-time voice agent design, Web Audio pipelines, streaming inference, and concurrency.

---

## Table of Contents

1. [High-Level Architecture & Design Principles](#1-high-level-architecture--design-principles)
2. [Backend Deep-Dive](#2-backend-deep-dive)
   - [backend/server.py](#backendserverpy)
   - [backend/vad.py](#backendvadpy)
   - [backend/stt.py](#backendsttpy)
   - [backend/llm.py](#backendllmpy)
   - [backend/tts.py](#backendttspy)
   - [backend/session.py](#backendsessionpy)
   - [backend/config.py](#backendconfigpy)
3. [Frontend Deep-Dive](#3-frontend-deep-dive)
   - [frontend/index.html](#frontendindexhtml)
   - [frontend/style.css](#frontendstylecss)
   - [frontend/pcm-worklet.js](#frontendpcm-workletjs)
   - [frontend/main.js](#frontendmainjs)
4. [Audio DSP & Pipeline Mechanics](#4-audio-dsp--pipeline-mechanics)
5. [Concurrency & Non-Blocking Design](#5-concurrency--non-blocking-design)
6. [Key Engineering Challenges & Solutions](#6-key-engineering-challenges--solutions)
7. [Technical Interview Questions & Answers](#7-technical-interview-questions--answers)

---

## 1. High-Level Architecture & Design Principles

The application is structured as a full-duplex, low-latency streaming pipeline:

1. **Client Audio Capture**: The browser captures microphone audio at 16 kHz using the Web Audio API. An `AudioWorkletProcessor` converts 32-bit floating point audio into signed 16-bit Linear PCM bytes and streams them over a persistent WebSocket connection.
2. **Turn Detection**: The backend receives raw chunks and buffers them into exact 30 ms frames (960 bytes). `webrtcvad` classifies frames as speech or non-speech. When speech is followed by 700 ms of continuous silence, an end-of-turn is triggered.
3. **Speech-to-Text (STT)**: The accumulated turn audio is validated through duration, energy (RMS), and hallucination filters before passing to `faster-whisper` for transcription.
4. **Streaming LLM**: Transcribed text is added to the conversation history and passed to the Groq API (`openai/gpt-oss-20b`), which streams response tokens back asynchronously.
5. **Sentence-Level TTS Chunking**: Rather than waiting for the entire LLM response to complete, tokens accumulate into sentences (split on `.`, `?`, `!`). Each sentence is synthesized immediately by Kokoro TTS into 24 kHz PCM audio and streamed back to the browser.
6. **Scheduled Audio Playback**: The client receives binary PCM chunks and schedules them gaplessly on the Web Audio timeline using precise time offsets.

---

## 2. Backend Deep-Dive

### `backend/server.py`

#### Purpose
The core ASGI server coordinating the WebSocket lifecycle, event-driven audio ingestion, turn detection, model dispatching, and static file hosting.

#### Detailed Code Explanation

```python
import asyncio
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from vad import TurnDetector
from stt import transcribe
from llm import stream_reply
from tts import synthesize
from session import InterviewSession
from config import SAMPLE_RATE_IN, VAD_SILENCE_MS
```
- Imports FastAPI WebSocket primitives and async libraries.
- Imports custom modules for VAD, STT, LLM, TTS, and session management.

```python
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("interview")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
```
- Sets up standard logging for tracking connection states and turn events.
- Initializes the FastAPI app with CORS middleware to allow cross-origin requests during development.
- Resolves the absolute path to the `frontend` directory for static file serving.

```python
@app.websocket("/ws/interview")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("WebSocket connected")
    session = InterviewSession()
    detector = TurnDetector(sample_rate=SAMPLE_RATE_IN, silence_ms=VAD_SILENCE_MS)
```
- Handles incoming WebSocket upgrade requests at `/ws/interview`.
- Instantiates a fresh `InterviewSession` (isolated history and buffer) and `TurnDetector` per client connection.

```python
    try:
        while True:
            data = await websocket.receive_bytes()
            session.audio_buffer.extend(data)

            if detector.process(data):
                log.info("End-of-turn detected, transcribing %d bytes...", len(session.audio_buffer))

                audio_bytes = bytes(session.audio_buffer)
                session.audio_buffer.clear()
                user_text = await asyncio.to_thread(transcribe, audio_bytes)
```
- `websocket.receive_bytes()` awaits incoming binary PCM16 audio chunks sent from the client.
- Appends incoming raw bytes into the session's temporary audio buffer.
- `detector.process(data)` returns `True` only when a complete utterance followed by the requisite silence period is detected.
- Extracts `audio_bytes` and clears the buffer immediately to prepare for the next turn.
- `asyncio.to_thread(transcribe, audio_bytes)` executes CPU-heavy Whisper transcription on a background worker thread, ensuring the async event loop remains responsive.

```python
                if not user_text.strip():
                    continue

                session.add_user_turn(user_text)
                await websocket.send_json({"type": "transcript", "text": user_text})

                full_reply = ""
                sentence_buffer = ""

                async for token in stream_reply(session.history):
                    full_reply += token
                    sentence_buffer += token

                    if any(p in token for p in ".?!"):
                        pcm = await asyncio.to_thread(synthesize, sentence_buffer)
                        await websocket.send_bytes(pcm)
                        sentence_buffer = ""

                if sentence_buffer.strip():
                    pcm = await asyncio.to_thread(synthesize, sentence_buffer)
                    await websocket.send_bytes(pcm)

                session.add_assistant_turn(full_reply)
                await websocket.send_json({"type": "reply_complete", "text": full_reply})
                log.info("Reply sent: %s", full_reply[:80])

                detector.reset()
                session.audio_buffer.clear()
```
- If transcription yields non-empty text, it registers the user turn in dialogue history and sends a JSON transcript message to the frontend.
- Iterates over tokens yielded by `stream_reply(session.history)` asynchronously.
- Accumulates tokens in `sentence_buffer`. When punctuation (`.`, `?`, `!`) is detected, it runs `synthesize` in a background thread and immediately streams the binary PCM audio bytes downstream over WebSocket.
- After the token stream completes, any trailing sentence fragment is synthesized and dispatched.
- Records the complete assistant turn in session history and dispatches a `reply_complete` JSON payload.
- Resets the `TurnDetector` internal state and clears any remnant audio buffer.

```python
    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    except Exception as e:
        log.exception("Error in websocket handler: %s", e)

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
```
- Gracefully handles client disconnections and unexpected exceptions.
- Mounts static files at the root route `/` so the frontend UI is served directly from the same origin. Note: the mount is declared after the WebSocket route to prevent route interception.

---

### `backend/vad.py`

#### Purpose
Implements voice activity detection using Google's `webrtcvad` engine, converting arbitrary streaming chunk sizes into strict 30 ms frame evaluations.

#### Detailed Code Explanation

```python
import webrtcvad


class TurnDetector:
    def __init__(self, aggressiveness=2, sample_rate=16000, silence_ms=700):
        self.vad = webrtcvad.Vad(aggressiveness)
        self.sample_rate = sample_rate
        self.frame_ms = 30
        self.frame_bytes = int(sample_rate * self.frame_ms / 1000) * 2
        self.silence_frames_needed = silence_ms // self.frame_ms
        self.silence_count = 0
        self.speech_started = False
        self._buffer = bytearray()
```
- `webrtcvad.Vad(aggressiveness)`: Aggressiveness mode ranges from 0 (least aggressive about filtering non-speech) to 3 (most aggressive). Mode 2 provides an optimal balance between sensitivity to voice and rejection of background noise.
- `frame_bytes`: At 16,000 Hz, 30 ms corresponds to `(16000 * 30 / 1000) = 480` samples. Since each sample is a 16-bit signed integer (2 bytes), each frame is exactly `480 * 2 = 960` bytes.
- `silence_frames_needed`: For 700 ms silence, `700 // 30 = 23` consecutive silent frames must occur after speech to confirm turn completion.
- `_buffer`: Internal byte accumulator across multiple small incoming WebSocket packets.

```python
    def process(self, pcm_bytes: bytes) -> bool:
        self._buffer.extend(pcm_bytes)

        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[:self.frame_bytes])
            self._buffer = self._buffer[self.frame_bytes:]

            is_speech = self.vad.is_speech(frame, self.sample_rate)

            if is_speech:
                self.speech_started = True
                self.silence_count = 0
            elif self.speech_started:
                self.silence_count += 1
                if self.silence_count >= self.silence_frames_needed:
                    self.reset()
                    return True
        return False

    def reset(self):
        self.speech_started = False
        self.silence_count = 0
        self._buffer.clear()
```
- `process(pcm_bytes)`: Appends incoming bytes to `_buffer`. Loops while there are at least 960 bytes, slices off exact 30 ms frames, and tests with `vad.is_speech(frame, sample_rate)`.
- If speech is detected, `speech_started` is set to `True` and `silence_count` is reset to 0.
- If speech has previously begun and current frame is non-speech, `silence_count` increments.
- When `silence_count >= silence_frames_needed`, turn end is triggered, internal state is reset, and `True` is returned.

---

### `backend/stt.py`

#### Purpose
Speech-to-text inference wrapper around `faster-whisper` (CTranslate2 implementation) with multi-stage hallucination guards.

#### Detailed Code Explanation

```python
from faster_whisper import WhisperModel
import numpy as np
from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE, SAMPLE_RATE_IN

whispermodel = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)

MIN_RMS_THRESHOLD = 0.01
MIN_DURATION_SEC = 0.5

HALLUCINATIONS = {
    "thank you", "thanks", "thank you.", "thanks.", "bye", "bye.",
    "thanks for watching", "thanks for watching.", "thank you for watching",
    "thank you for watching.", "you", "you.", "subscribe",
}
```
- Initializes `WhisperModel` once on module import using `small.en` on CPU with `int8` quantization for efficient inference.
- Defines validation thresholds: minimum audio duration (0.5s) and minimum Root Mean Square (RMS) signal energy (0.01).
- Sets up a blocklist of common Whisper hallucinations that occur when running inference on low-energy or silent audio.

```python
def transcribe(pcm_bytes: bytes) -> str:
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    duration = len(audio) / SAMPLE_RATE_IN
    if duration < MIN_DURATION_SEC:
        return ""

    rms = np.sqrt(np.mean(audio ** 2))
    if rms < MIN_RMS_THRESHOLD:
        return ""

    segments, info = whispermodel.transcribe(audio, language="en", beam_size=1)
    text = " ".join(seg.text for seg in segments).strip()

    if text.lower().strip() in HALLUCINATIONS:
        return ""

    return text
```
- Deserializes raw PCM16 bytes into a numpy array and normalizes to `[-1.0, 1.0]` float range by dividing by 32768.0.
- **Gate 1 (Duration)**: Rejects very short audio bursts (< 0.5s).
- **Gate 2 (RMS Energy)**: Computes root mean square of signal `sqrt(mean(x^2))`. Rejects near-silent background noise.
- Transcribes using greedy search (`beam_size=1`) for minimum latency.
- **Gate 3 (Hallucination Blocklist)**: Filters out spurious subtitle hallucinations.

---

### `backend/llm.py`

#### Purpose
Asynchronous integration with Groq Cloud LLM API for low-latency streaming completions.

#### Detailed Code Explanation

```python
from groq import AsyncGroq
from config import GROQ_API_KEY, GROQ_MODEL

client = AsyncGroq(api_key=GROQ_API_KEY)

SystemPrompt = "You are an interview coach conducting a technical mock interview. Ask one question at a time, listen to the answer, give brief feedback, then ask a natural follow-up. Keep responses under 3 sentences."


async def stream_reply(history: list[dict]):
    stream = await client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "system", "content": SystemPrompt}] + history,
        stream=True
    )
    async for chunk in stream:
        token = chunk.choices[0].delta.content or ""
        if token:
            yield token
```
- Instantiates `AsyncGroq` client.
- Prepends `SystemPrompt` with instructions to conduct structured, concise technical interviews (under 3 sentences per turn).
- Sends full dialogue `history` (`[{"role": "user", "content": ...}, {"role": "assistant", "content": ...}]`) to maintain conversational context.
- Uses `stream=True` to yield individual tokens asynchronously as they are generated by the model.

---

### `backend/tts.py`

#### Purpose
Text-to-speech synthesis utilizing the lightweight Kokoro-82M pipeline.

#### Detailed Code Explanation

```python
from kokoro import KPipeline
import numpy as np
from config import KOKORO_LANG_CODE, KOKORO_VOICE, SAMPLE_RATE_OUT as SAMPLE_RATE

pipeline = KPipeline(lang_code=KOKORO_LANG_CODE)

def synthesize(text: str) -> bytes:
    audio_chunks = []
    for _, _, audio in pipeline(text, voice=KOKORO_VOICE):
        audio_chunks.append(audio)
    full_audio = np.concatenate(audio_chunks)
    return (full_audio * 32767).astype(np.int16).tobytes()
```
- Initializes `KPipeline` with American English (`lang_code="a"`).
- Iterates over generated audio chunks for the given text segment using voice `af_heart`.
- Concatenates audio segments into a single numpy float array.
- Multiplies float samples `[-1.0, 1.0]` by 32767 to quantize into 16-bit signed integer PCM (`int16`), converts to raw bytes, and returns the payload for WebSocket transmission.

---

### `backend/session.py`

#### Purpose
Lightweight in-memory session container managing turn history and audio accumulation.

```python
class InterviewSession:
    def __init__(self, session_id: str = "default"):
        self.audio_buffer = bytearray()
        self.history: list[dict] = []
        self.session_id = session_id

    def add_user_turn(self, text: str):
        self.history.append({"role": "user", "content": text})

    def add_assistant_turn(self, text: str):
        self.history.append({"role": "assistant", "content": text})
```
- `audio_buffer`: A mutable `bytearray` accumulating raw PCM16 bytes until the VAD signals end-of-turn.
- `history`: List of role-content message dictionaries maintaining turn history for multi-turn LLM reasoning.

---

### `backend/config.py`

#### Purpose
Centralized configuration repository loading environment variables and setting constants.

```python
import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL")

WHISPER_MODEL_SIZE = "small.en"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"

KOKORO_LANG_CODE = "a"
KOKORO_VOICE = "af_heart"

SAMPLE_RATE_IN = 16000
SAMPLE_RATE_OUT = 24000

VAD_SILENCE_MS = 700
```
- Loads environment variables from `.env`.
- Configures sample rates: 16 kHz for input capture (standard for Whisper and WebRTC VAD), 24 kHz for output synthesis (Kokoro native output).
- Sets VAD silence threshold to 700 ms.

---

## 3. Frontend Deep-Dive

### `frontend/index.html`

#### Purpose
Single-page application layout providing semantic structure, accessibility landmarks, the agent orb interface, audio visualization containers, and transcript feed.

#### Key Structural Elements
- `.orb-wrapper`: The interactive interactive button element (`role="button"`, `tabindex="0"`) containing the central animated orb.
- `.ripple-ring`: Three concentric absolute-positioned circular divs that trigger CSS expansion animations when the assistant is speaking.
- `.audio-bars`: Container populated with 48 dynamically created radial equalizer bars.
- `.processing-spinner`: Rotating border ring active during the `processing` state.
- `.orb`: Main visual element with gradient styling, glassmorphism backdrop blur, and SVG microphone icon.
- `.status-area`: Two-tier status display showing primary state ("Listening...", "Thinking...", "Speaking...") and secondary guidance hint.
- `.transcript-panel`: Scrollable glassmorphism container displaying user and coach dialogue history.
- `.connection-status`: Fixed badge displaying WebSocket connection state.

---

### `frontend/style.css`

#### Purpose
CSS design system implementing modern visual aesthetics (dark mode, glassmorphism, glowing gradients, keyframe animations).

#### Core Design Elements
- **Color Palette & Tokens**: Uses CSS custom properties (`--bg-primary`, `--accent-cyan`, `--accent-violet`, `--accent-emerald`, `--accent-rose`).
- **Ambient Canvas**: Fixed pseudo-element `body::before` with rotating radial gradients simulating subtle organic movement.
- **Radial Equalizer**: `.audio-bar` elements with `transform-origin: bottom center` arranged circularly around the orb, dynamically modulated in height via JavaScript.
- **State-Driven Styles**:
  - `.state-listening`: Orb glows with active cyan shadow; audio bars become visible.
  - `.state-processing`: `.processing-spinner` activates with 360-degree rotation animation.
  - `.state-speaking`: `.ripple-ring` elements execute staggered `rippleExpand` keyframe animations radiating outward.
  - `.state-idle`: Gentle scale pulsing (`orbIdle`).

---

### `frontend/pcm-worklet.js`

#### Purpose
An `AudioWorkletProcessor` running on the Web Audio audio-rendering thread to perform high-frequency format conversion without blocking the browser UI thread.

#### Detailed Code Explanation

```javascript
class PCMProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0][0];
    if (input) {
      const int16 = new Int16Array(input.length);
      for (let i = 0; i < input.length; i++) {
        int16[i] = Math.max(-32768, Math.min(32767, input[i] * 32768));
      }
      this.port.postMessage(int16.buffer, [int16.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-processor", PCMProcessor);
```
- `inputs[0][0]`: Accesses channel 0 of the first audio input buffer (128 samples of 32-bit floating point numbers between -1.0 and +1.0).
- Quantization: Clamps values and multiplies by 32768 to convert to signed 16-bit integer values (`-32768` to `+32767`).
- Zero-Copy Transfer: `this.port.postMessage(int16.buffer, [int16.buffer])` transfers ownership of the underlying `ArrayBuffer` to the main thread without memory allocation copying.
- Returns `true` to keep the processor alive for subsequent frames.

---

### `frontend/main.js`

#### Purpose
Coordinates application state, Web Audio capture and playback, frequency domain FFT analysis, WebSocket networking, and DOM rendering.

#### Detailed Code Explanation

```javascript
const appContainer = document.getElementById('appContainer');
const orbWrapper = document.getElementById('orbWrapper');
const audioBarsEl = document.getElementById('audioBars');
const statusText = document.getElementById('statusText');
const statusHint = document.getElementById('statusHint');
const transcriptPanel = document.getElementById('transcriptPanel');
const transcriptEmpty = document.getElementById('transcriptEmpty');
const connectionDot = document.getElementById('connectionDot');
const connectionLabel = document.getElementById('connectionLabel');

let appState = 'idle';
let ws = null;
let audioCtx = null;
let micStream = null;
let analyserNode = null;
let workletNode = null;
let playbackQueueTime = 0;
let animFrameId = null;

const NUM_BARS = 48;
const BAR_RADIUS = 120;
```
- References all required DOM elements and initializes application state variables.
- Configures 48 radial audio bars positioned at a radius of 120 px from the orb center.

```javascript
(function createAudioBars() {
  for (let i = 0; i < NUM_BARS; i++) {
    const bar = document.createElement('div');
    bar.className = 'audio-bar';
    const angle = (i / NUM_BARS) * 360;
    const rad = (angle * Math.PI) / 180;
    const x = Math.cos(rad) * BAR_RADIUS;
    const y = Math.sin(rad) * BAR_RADIUS;
    bar.style.left = `calc(50% + ${x}px - 1.5px)`;
    bar.style.top = `calc(50% + ${y}px)`;
    bar.style.height = '4px';
    bar.style.transform = `rotate(${angle + 90}deg)`;
    audioBarsEl.appendChild(bar);
  }
})();

const audioBars = audioBarsEl.querySelectorAll('.audio-bar');
```
- Trigonometrically computes `(x, y)` coordinates around a circle for each bar and rotates it outwards.

```javascript
function setState(newState) {
  appState = newState;
  appContainer.classList.remove('state-idle', 'state-listening', 'state-processing', 'state-speaking');
  appContainer.classList.add(`state-${newState}`);
  // Updates UI text accordingly
}
```
- Transitions application state and applies CSS classes to activate visual animations.

```javascript
function connectWebSocket() {
  ws = new WebSocket('ws://localhost:8001/ws/interview');
  ws.binaryType = 'arraybuffer';
  // Handles onopen, onclose, onerror, onmessage
}

function handleJsonMessage(msg) {
  if (msg.type === 'transcript') {
    addTranscriptEntry('You', msg.text, 'user');
    setState('processing');
  } else if (msg.type === 'reply_complete') {
    addTranscriptEntry('Coach', msg.text, 'ai');
    const delayMs = Math.max(0, (playbackQueueTime - audioCtx.currentTime) * 1000) + 300;
    setTimeout(() => {
      if (appState === 'speaking') {
        setState('listening');
      }
    }, delayMs);
  }
}

function handleAudioMessage(arrayBuffer) {
  setState('speaking');
  playPCM(arrayBuffer);
}
```
- Handles binary and JSON WebSocket payloads.
- Calculates remaining playback time based on `playbackQueueTime - audioCtx.currentTime` to transition state back to `listening` exactly when audio completes.

```javascript
function playPCM(arrayBuffer) {
  if (!audioCtx) return;

  const int16 = new Int16Array(arrayBuffer);
  const float32 = Float32Array.from(int16, x => x / 32768);

  const buffer = audioCtx.createBuffer(1, float32.length, 24000);
  buffer.copyToChannel(float32, 0);

  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);

  const startAt = Math.max(audioCtx.currentTime, playbackQueueTime);
  src.start(startAt);
  playbackQueueTime = startAt + buffer.duration;
}
```
- Deserializes binary 16-bit PCM back to normalized float32.
- Creates an `AudioBuffer` at 24,000 Hz.
- Schedules playback start time to `Math.max(currentTime, playbackQueueTime)` to ensure seamless, gapless playback of incoming audio chunks.

```javascript
async function startMicrophone() {
  audioCtx = new AudioContext({ sampleRate: 16000 });
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
  });

  const source = audioCtx.createMediaStreamSource(micStream);

  analyserNode = audioCtx.createAnalyser();
  analyserNode.fftSize = 256;
  analyserNode.smoothingTimeConstant = 0.7;
  source.connect(analyserNode);

  await audioCtx.audioWorklet.addModule('pcm-worklet.js');
  workletNode = new AudioWorkletNode(audioCtx, 'pcm-processor');

  workletNode.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN && appState === 'listening') {
      ws.send(e.data);
    }
  };

  source.connect(workletNode);
  startVisualization();
}
```
- Initializes microphone with browser-level DSP (echo cancellation, noise suppression).
- Connects audio graph: `Source -> AnalyserNode` and `Source -> AudioWorkletNode`.
- **Feedback Prevention Gate**: Microphone bytes are sent via WebSocket **only** when `appState === 'listening'`.

---

## 4. Audio DSP & Pipeline Mechanics

### Linear PCM16 Representation
- Uncompressed raw audio representation.
- Each sample is represented as a signed 16-bit integer spanning `[-32768, +32767]`.
- Little-Endian byte ordering is standard across x86 and ARM architectures.

### Sample Rates: 16 kHz vs 24 kHz
- **16,000 Hz (Capture & STT)**: 16 kHz is the standard for human speech recognition (Nyquist frequency 8 kHz covers all intelligible human vocal formants). It reduces bandwidth and computational load on `webrtcvad` and Whisper.
- **24,000 Hz (TTS Playback)**: Kokoro-82M outputs audio at 24 kHz for richer acoustic fidelity and higher voice clarity.

### Fast Fourier Transform (FFT) Visualization
- The `AnalyserNode` computes real-time frequency domain data (`getByteFrequencyData`) with an FFT size of 256 (giving 128 frequency bins).
- The 48 radial bars sample these bins across low, mid, and high vocal frequencies to drive smooth physical visualizations.

---

## 5. Concurrency & Non-Blocking Design

### Python Asyncio Event Loop & CPU-Bound Operations
In Python's `asyncio`, blocking synchronous operations (such as running CTranslate2 matrix computations in Whisper or PyTorch inference in Kokoro) would freeze the event loop. This causes:
- WebSocket ping/pong timeouts.
- Missed network packets.
- Stalled streaming pipelines.

### Thread Pool Offloading
The codebase utilizes `asyncio.to_thread()`:
```python
user_text = await asyncio.to_thread(transcribe, audio_bytes)
pcm = await asyncio.to_thread(synthesize, sentence_buffer)
```
This offloads the synchronous blocking call to a thread pool executor, allowing the main event loop to continue servicing network I/O, WebSockets, and client signals.

---

## 6. Key Engineering Challenges & Solutions

| Problem | Root Cause | Implemented Solution |
|---|---|---|
| **Audio Feedback Loop** | Mic picked up assistant voice from speakers during playback, triggering VAD and echoing back to Whisper. | 1. Client-side gating: Mic only streams when `appState === 'listening'`.<br>2. Explicit VAD reset on turn transition. |
| **Whisper Silence Hallucination** | Whisper generates false transcripts (e.g., "Thank you") on silent audio buffers. | 1. Duration check (> 0.5s).<br>2. RMS energy threshold (> 0.01).<br>3. Common hallucination blocklist filter. |
| **VAD Frame Mismatch** | AudioWorklet produces 128-sample chunks (256 bytes), but `webrtcvad` requires exact 30 ms frames (960 bytes). | Added internal `bytearray` buffer in `TurnDetector` that accumulates chunks and extracts exact 960-byte slices. |
| **High Response Latency** | Waiting for complete LLM generation before starting TTS causes multi-second user delays. | Punctuation-based sentence streaming: TTS synthesizes sentences incrementally as tokens stream from Groq. |
| **Audio Choppiness on Playback** | Receiving audio in disparate network chunks causes audio gaps. | Timeline scheduling in Web Audio API using `playbackQueueTime` offset tracking. |

---

## 7. Technical Interview Questions & Answers

### Q1: How does Voice Activity Detection (VAD) work in this system?
**Answer**: We use Google's `webrtcvad`, which analyzes the spectral energy and acoustic characteristics of 30 ms audio frames (960 bytes at 16 kHz) to classify them as speech or non-speech. Because the browser AudioWorklet sends smaller chunks (256 bytes), we maintain an internal byte buffer in `TurnDetector`. Once speech starts, the system tracks consecutive silence frames. When 700 ms of silence (23 consecutive 30 ms frames) is detected, it signals that the user has completed their turn.

### Q2: Why use WebSockets instead of HTTP POST / REST endpoints for voice streaming?
**Answer**: WebSockets provide a persistent, bidirectional, full-duplex TCP connection with minimal framing overhead. In a voice agent, we require low latency: streaming raw PCM audio chunks upstream continuously, receiving streamed tokens, and streaming synthesized PCM audio chunks downstream simultaneously. HTTP request/response overhead and connection establishment latency would add hundreds of milliseconds of turn-taking delay.

### Q3: How do you achieve low Time-to-First-Audio (TTFA) latency?
**Answer**: We implement a multi-stage streaming pipeline:
1. Fast local VAD detection with a 700 ms silence boundary.
2. Fast STT inference using `faster-whisper` (CTranslate2 `int8` quantization).
3. Streaming LLM inference via Groq API.
4. Sentence-level TTS chunking: rather than waiting for the entire LLM response, the first sentence is dispatched to Kokoro TTS as soon as punctuation (`.`, `?`, `!`) is reached.
5. Immediate binary streaming of audio bytes to the client for scheduled playback.

### Q4: How is audio format conversion handled between client and server?
**Answer**:
- **Capture**: The browser microphone captures `Float32` samples at 16 kHz. An `AudioWorkletProcessor` quantizes these into signed 16-bit integers (`Int16Array`) and transfers the raw `ArrayBuffer` over WebSocket.
- **Server Processing**: STT processes 16 kHz PCM16 bytes.
- **Synthesis**: Kokoro TTS synthesizes audio at 24 kHz float, which is converted to signed 16-bit PCM bytes.
- **Playback**: The client deserializes the 24 kHz PCM16 bytes back into a `Float32Array`, loads it into an `AudioBuffer` at 24,000 Hz, and connects it to the audio destination.

### Q5: How do you prevent blocking the Python asyncio event loop with deep learning models?
**Answer**: Python's `asyncio` is single-threaded. CPU-heavy model inferences (Whisper and Kokoro) are synchronous C-extensions. If called directly inside an async function, they block the entire process, preventing WebSocket frames from being sent or received. We wrap these synchronous calls in `await asyncio.to_thread(...)`, which delegates execution to Python's default `ThreadPoolExecutor` and yields control back to the event loop.
