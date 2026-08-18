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


@app.websocket("/ws/interview")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    log.info("WebSocket connected")
    session = InterviewSession()
    detector = TurnDetector(sample_rate=SAMPLE_RATE_IN, silence_ms=VAD_SILENCE_MS)

    try:
        while True:
            data = await websocket.receive_bytes()
            session.audio_buffer.extend(data)

            if detector.process(data):
                log.info("End-of-turn detected, transcribing %d bytes…", len(session.audio_buffer))

                audio_bytes = bytes(session.audio_buffer)
                session.audio_buffer.clear()
                user_text = await asyncio.to_thread(transcribe, audio_bytes)

                log.info("Transcription: %s", user_text)

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

    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    except Exception as e:
        log.exception("Error in websocket handler: %s", e)


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
