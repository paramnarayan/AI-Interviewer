

import json
import logging
import os

import redis.asyncio as redis

log = logging.getLogger("interview")

# Redis URL — overridable via env var
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379")
# Session TTL: default 24 hours
_TTL_S: int = int(os.getenv("REDIS_SESSION_TTL_S", str(24 * 60 * 60)))

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


class InterviewSession:
    def __init__(self, session_id: str = "default"):
        self.audio_buffer: bytearray = bytearray()
        self.history: list[dict] = []
        self.session_id = session_id
        self.profile: dict = {}

    # ── Profile ──────────────────────────────────────────────────────────────

    def set_profile(self, profile: dict) -> None:
        self.profile = profile

    # ── Conversation turns ────────────────────────────────────────────────────

    def add_user_turn(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def add_assistant_turn(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    # ── Redis persistence ─────────────────────────────────────────────────────

    async def save(self) -> None:
        """
        Persist the current conversation history and profile to Redis.
        Called after every completed turn so a reconnecting client can resume.
        Failures are logged but do not abort the interview.
        """
        try:
            pipe = redis_client.pipeline()
            history_key = f"interview:{self.session_id}:history"
            profile_key = f"interview:{self.session_id}:profile"

            pipe.set(history_key, json.dumps(self.history), ex=_TTL_S)
            pipe.set(profile_key, json.dumps(self.profile), ex=_TTL_S)
            await pipe.execute()
            log.debug(
                "Redis: saved %d turns for session %s", len(self.history), self.session_id
            )
        except Exception as exc:
            log.warning("Redis save failed for session %s: %s", self.session_id, exc)

    async def load(self) -> bool:
        """
        Restore history and profile from Redis (e.g. on reconnect).
        Returns True if a prior session was found and loaded, False otherwise.
        """
        try:
            history_key = f"interview:{self.session_id}:history"
            profile_key = f"interview:{self.session_id}:profile"

            raw_history, raw_profile = await redis_client.mget(history_key, profile_key)

            if raw_history:
                self.history = json.loads(raw_history)
            if raw_profile:
                self.profile = json.loads(raw_profile)

            found = bool(raw_history or raw_profile)
            if found:
                log.info(
                    "Redis: restored %d turns for session %s",
                    len(self.history),
                    self.session_id,
                )
            return found
        except Exception as exc:
            log.warning("Redis load failed for session %s: %s", self.session_id, exc)
            return False

    async def clear(self) -> None:
        """Remove this session's keys from Redis (called after interview ends)."""
        try:
            await redis_client.delete(
                f"interview:{self.session_id}:history",
                f"interview:{self.session_id}:profile",
            )
            log.debug("Redis: cleared session %s", self.session_id)
        except Exception as exc:
            log.warning("Redis clear failed for session %s: %s", self.session_id, exc)
