"""
metrics.py — Prometheus instruments for the Voice Interview Coach pipeline.

Every metric maps to one stage in the conversation turn:
    User speaks → VAD → STT → LLM stream → TTS → audio out

Import individual names directly:
    from metrics import STT_LATENCY, LLM_TTFT, ...
"""

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Per-stage latency histograms
# ---------------------------------------------------------------------------

STT_LATENCY = Histogram(
    "stt_latency_seconds",
    "Time from audio-end (VAD trigger) to transcript ready",
    buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0],
)

LLM_TTFT = Histogram(
    "llm_ttft_seconds",
    "Time to first token from Groq (perceived response start)",
    buckets=[0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0],
)

LLM_TOTAL_LATENCY = Histogram(
    "llm_total_response_seconds",
    "Full LLM stream duration (first token to stream exhausted)",
    buckets=[0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0],
)

TTS_LATENCY = Histogram(
    "tts_synthesis_seconds",
    "Time to synthesize one sentence chunk via Kokoro",
    buckets=[0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0],
)

TURN_END_TO_END = Histogram(
    "conversation_turn_seconds",
    "Full turn: VAD speech-end to first audio PCM bytes sent to client",
    buckets=[0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 10.0],
)

# ---------------------------------------------------------------------------
# Session / activity gauges
# ---------------------------------------------------------------------------

ACTIVE_SESSIONS = Gauge(
    "active_websocket_sessions",
    "Number of concurrent WebSocket interview sessions",
)

# ---------------------------------------------------------------------------
# Event counters
# ---------------------------------------------------------------------------

VAD_SEGMENTS = Counter(
    "vad_speech_segments_total",
    "Total speech segments detected by VAD (i.e., turns initiated)",
)

STT_ERRORS = Counter(
    "stt_errors_total",
    "Transcription failures in faster-whisper",
    ["error_type"],
)

LLM_ERRORS = Counter(
    "llm_errors_total",
    "Groq API failures during LLM streaming or summary generation",
    ["error_type"],
)

TTS_ERRORS = Counter(
    "tts_errors_total",
    "Kokoro TTS synthesis failures",
    ["error_type"],
)

TURNS_COMPLETED = Counter(
    "conversation_turns_completed_total",
    "Successfully completed full conversation turns (VAD → audio out)",
)
