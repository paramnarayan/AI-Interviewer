import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from prometheus_client import make_asgi_app

# Structured JSON logging — must be configured before any log calls
import logging_config
logging_config.configure_logging()

from vad import TurnDetector
from stt import transcribe
from llm import stream_reply, generate_summary
from tts import synthesize
from session import InterviewSession
from config import SAMPLE_RATE_IN, VAD_SILENCE_MS
from database import (
    init_db,
    create_interview_record,
    save_message,
    save_summary,
    get_user_interview_history,
)
from routers.resume import router as resume_router
from metrics import ACTIVE_SESSIONS, TURN_END_TO_END, TURNS_COMPLETED

log = logging.getLogger("interview")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Prometheus metrics endpoint — must be mounted before the catch-all StaticFiles
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

app.include_router(resume_router)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/history/{candidate_name}")
async def interview_history(candidate_name: str):
    history = await get_user_interview_history(candidate_name)
    return JSONResponse({"candidate_name": candidate_name, "interviews": history})


@app.websocket("/ws/interview")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ACTIVE_SESSIONS.inc()
    log.info("WebSocket connected")

    interview_id = str(uuid.uuid4())
    session = InterviewSession(session_id=interview_id)
    detector = TurnDetector(sample_rate=SAMPLE_RATE_IN, silence_ms=VAD_SILENCE_MS)

    try:
        setup_msg = await websocket.receive_json()
        session.set_profile(setup_msg)
        log.info(
            "Session profile — id=%s name=%s, role=%s, company=%s, stack=%s, resume=%s",
            interview_id,
            setup_msg.get("name"),
            setup_msg.get("role"),
            setup_msg.get("company"),
            setup_msg.get("tech_stack"),
            "yes" if setup_msg.get("resume_text") else "no",
        )

        try:
            await create_interview_record(interview_id, setup_msg)
        except Exception as db_exc:
            log.exception("DB: failed to create interview record: %s", db_exc)

        await session.save()

        context_terms = [session.profile.get("name", "")]
        if session.profile.get("company"):
            context_terms.append(session.profile["company"])
        context_terms += session.profile.get("tech_stack", [])
        context_terms = [t for t in context_terms if t]

        while True:
            msg = await websocket.receive()

            if msg["type"] == "websocket.disconnect":
                log.info("Client disconnected")
                break

            if msg["type"] == "websocket.receive" and msg.get("text"):
                try:
                    ctrl = json.loads(msg["text"])
                except Exception:
                    continue

                if ctrl.get("type") == "end_interview":
                    log.info("End-interview requested, generating summary…")
                    try:
                        summary = await generate_summary(session)
                        await websocket.send_json({"type": "summary", "data": summary})
                        log.info("Summary sent")
                        try:
                            await save_summary(interview_id, json.dumps(summary))
                        except Exception as db_exc:
                            log.exception("DB: failed to save summary: %s", db_exc)
                        await session.clear()
                    except Exception as e:
                        log.exception("Summary generation failed: %s", e)
                        await websocket.send_json({"type": "summary_error", "message": str(e)})
                    break

            elif msg["type"] == "websocket.receive" and msg.get("bytes"):
                data = msg["bytes"]
                session.audio_buffer.extend(data)

                if detector.process(data):
                    log.info(
                        "End-of-turn detected, transcribing %d bytes",
                        len(session.audio_buffer),
                        extra={"session_id": interview_id, "stage": "vad"},
                    )

                    turn_start = time.perf_counter()
                    audio_bytes = bytes(session.audio_buffer)
                    session.audio_buffer.clear()

                    user_text = await asyncio.to_thread(transcribe, audio_bytes, context_terms)
                    log.info(
                        "Transcription complete: %r",
                        user_text,
                        extra={"session_id": interview_id, "stage": "stt"},
                    )

                    if not user_text.strip():
                        detector.reset()
                        continue

                    session.add_user_turn(user_text)
                    await websocket.send_json({"type": "transcript", "text": user_text})

                    await session.save()
                    try:
                        await save_message(interview_id, "user", user_text)
                    except Exception as db_exc:
                        log.exception("DB: failed to save user message: %s", db_exc)

                    full_reply = ""
                    sentence_buffer = ""
                    first_audio_sent = False

                    async for token in stream_reply(session.history, session.profile):
                        full_reply += token
                        sentence_buffer += token
                        if any(p in token for p in ".?!"):
                            pcm = await asyncio.to_thread(synthesize, sentence_buffer)
                            if not first_audio_sent:
                                # Record full turn latency on the very first audio chunk
                                TURN_END_TO_END.observe(time.perf_counter() - turn_start)
                                first_audio_sent = True
                            await websocket.send_bytes(pcm)
                            log.debug(
                                "Audio chunk sent",
                                extra={"session_id": interview_id, "stage": "tts"},
                            )
                            sentence_buffer = ""

                    if sentence_buffer.strip():
                        pcm = await asyncio.to_thread(synthesize, sentence_buffer)
                        if not first_audio_sent:
                            TURN_END_TO_END.observe(time.perf_counter() - turn_start)
                            first_audio_sent = True
                        await websocket.send_bytes(pcm)

                    if first_audio_sent:
                        TURNS_COMPLETED.inc()

                    session.add_assistant_turn(full_reply)
                    await websocket.send_json({"type": "reply_complete", "text": full_reply})
                    log.info(
                        "Reply complete: %s",
                        full_reply[:80],
                        extra={"session_id": interview_id, "stage": "llm"},
                    )

                    await session.save()
                    try:
                        await save_message(interview_id, "assistant", full_reply)
                    except Exception as db_exc:
                        log.exception("DB: failed to save assistant message: %s", db_exc)

                    detector.reset()
                    session.audio_buffer.clear()

    except WebSocketDisconnect:
        log.info("WebSocket disconnected")
    except Exception as e:
        log.exception("Error in websocket handler: %s", e)
    finally:
        ACTIVE_SESSIONS.dec()


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
