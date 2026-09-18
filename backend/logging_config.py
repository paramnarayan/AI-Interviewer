"""
logging_config.py — Structured JSON logging for Loki ingestion.

Call `configure_logging()` once at startup (before any log calls).

Output format (one JSON object per line, goes to stdout):
    {
        "timestamp": "2026-09-15 18:00:00,123",
        "level": "INFO",
        "service": "backend-api",
        "logger": "interview",
        "message": "STT transcription complete",
        "session_id": "abc-123",
        "stage": "stt"
    }

Log with context using the `extra` kwarg:
    log.info("STT done", extra={"session_id": sid, "stage": "stt"})

Loki query to trace one full session across all stages:
    {service="backend-api"} | json | session_id="abc-123"
"""

import json
import logging
import sys


class _JSONFormatter(logging.Formatter):
    """Serialise every log record to a single-line JSON object."""

    SERVICE = "backend-api"

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "service": self.SERVICE,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Optional structured fields injected via `extra={}`
        for field in ("session_id", "stage", "turn_index"):
            val = getattr(record, field, None)
            if val is not None:
                payload[field] = val

        # Attach exception info if present
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """
    Replace the root logger's handlers with:
      1. A JSON-to-stdout handler (for local inspection / Docker).
      2. A JSON-to-file handler writing to logs/backend.json (tailed by Promtail).

    Call this once, before any other logging setup.
    """
    import pathlib

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any handlers that basicConfig (or uvicorn) may have added
    root.handlers.clear()

    formatter = _JSONFormatter()

    # 1. stdout — always present
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    # 2. File sink for Promtail (Option A: log file on host)
    log_dir = pathlib.Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "backend.json", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Quieten noisy third-party loggers without suppressing them entirely
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
