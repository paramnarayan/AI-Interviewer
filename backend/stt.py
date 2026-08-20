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


def transcribe(pcm_bytes: bytes, context_terms: list[str] = None) -> str:
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    duration = len(audio) / SAMPLE_RATE_IN
    if duration < MIN_DURATION_SEC:
        return ""

    rms = np.sqrt(np.mean(audio ** 2))
    if rms < MIN_RMS_THRESHOLD:
        return ""

    initial_prompt = None
    if context_terms:
        initial_prompt = "Relevant names and terms: " + ", ".join(context_terms)

    segments, info = whispermodel.transcribe(
        audio, language="en", beam_size=5, initial_prompt=initial_prompt
    )
    text = " ".join(seg.text for seg in segments).strip()

    if text.lower().strip() in HALLUCINATIONS:
        return ""

    return text
