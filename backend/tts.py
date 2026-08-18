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