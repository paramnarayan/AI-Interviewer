from kokoro import KPipeline
import numpy as np
import time

from config import KOKORO_LANG_CODE, KOKORO_VOICE, SAMPLE_RATE_OUT as SAMPLE_RATE
from metrics import TTS_LATENCY, TTS_ERRORS

pipeline = KPipeline(lang_code=KOKORO_LANG_CODE)


def synthesize(text: str) -> bytes:
    """
    Synthesize text to raw PCM bytes via Kokoro.

    Records:
      - TTS_LATENCY: wall-clock time for the full synthesis call.
      - TTS_ERRORS: labelled by exception class name on failure.
    """
    start = time.perf_counter()
    try:
        audio_chunks = []
        for _, _, audio in pipeline(text, voice=KOKORO_VOICE):
            audio_chunks.append(audio)
        full_audio = np.concatenate(audio_chunks)
        return (full_audio * 32767).astype(np.int16).tobytes()
    except Exception as e:
        TTS_ERRORS.labels(error_type=type(e).__name__).inc()
        raise
    finally:
        TTS_LATENCY.observe(time.perf_counter() - start)