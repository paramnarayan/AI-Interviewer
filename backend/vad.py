import numpy as np
import torch
from silero_vad import load_silero_vad

from config import VAD_THRESHOLD


class TurnDetector:
    def __init__(self, sample_rate: int = 16000, silence_ms: int = 1000, threshold: float = VAD_THRESHOLD):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.silence_samples_needed = int(sample_rate * silence_ms / 1000)
        self.window_size = 512
        self.model = load_silero_vad()
        self.model.eval()
        self._buffer = bytearray()
        self.speech_started = False
        self.silence_sample_count = 0

    def process(self, pcm_bytes: bytes) -> bool:
        self._buffer.extend(pcm_bytes)

        while len(self._buffer) >= self.window_size * 2:
            raw = bytes(self._buffer[: self.window_size * 2])
            self._buffer = self._buffer[self.window_size * 2 :]

            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            tensor = torch.from_numpy(samples)

            with torch.no_grad():
                prob = self.model(tensor, self.sample_rate).item()

            is_speech = prob >= self.threshold

            if is_speech:
                self.speech_started = True
                self.silence_sample_count = 0
            elif self.speech_started:
                self.silence_sample_count += self.window_size
                if self.silence_sample_count >= self.silence_samples_needed:
                    self.reset()
                    return True

        return False

    def reset(self):
        self.speech_started = False
        self.silence_sample_count = 0
        self._buffer.clear()
        self.model.reset_states()