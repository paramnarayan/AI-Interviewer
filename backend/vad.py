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