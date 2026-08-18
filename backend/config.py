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