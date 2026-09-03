import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import Column, ForeignKey, Integer, String, Text, DateTime, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

log = logging.getLogger("interview")

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./interviews.db")

_engine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("DB_ECHO", "").lower() in ("1", "true", "yes"),
    future=True,
)

_AsyncSessionLocal = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class _Base(DeclarativeBase):
    pass


class InterviewRecord(_Base):
    __tablename__ = "interview_records"

    id = Column(String, primary_key=True, nullable=False)
    candidate_name = Column(String, nullable=False, index=True)
    target_role = Column(String, nullable=False)
    company = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    summary_json = Column(Text, nullable=True)

    messages = relationship(
        "MessageRecord",
        back_populates="interview",
        cascade="all, delete-orphan",
        order_by="MessageRecord.created_at",
        lazy="selectin",
    )


class MessageRecord(_Base):
    __tablename__ = "message_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    interview_id = Column(String, ForeignKey("interview_records.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    interview = relationship("InterviewRecord", back_populates="messages")


async def init_db() -> None:
    async with _engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    log.info("Database initialised — %s", DATABASE_URL)


async def create_interview_record(interview_id: str, profile: dict) -> None:
    async with _AsyncSessionLocal() as session:
        record = InterviewRecord(
            id=interview_id,
            candidate_name=profile.get("name", "Unknown"),
            target_role=profile.get("role", "Unknown"),
            company=profile.get("company") or None,
            created_at=datetime.now(timezone.utc),
        )
        session.add(record)
        await session.commit()
    log.debug("Created interview record: %s", interview_id)


async def save_message(interview_id: str, role: str, content: str) -> None:
    async with _AsyncSessionLocal() as session:
        msg = MessageRecord(
            interview_id=interview_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        session.add(msg)
        await session.commit()
    log.debug("Saved %s message for interview %s", role, interview_id)


async def save_summary(interview_id: str, summary_json: str) -> None:
    async with _AsyncSessionLocal() as session:
        record = await session.get(InterviewRecord, interview_id)
        if record is None:
            log.warning("save_summary: interview_id %s not found — skipping", interview_id)
            return
        record.summary_json = summary_json
        await session.commit()
    log.debug("Saved summary for interview %s", interview_id)


async def get_user_interview_history(candidate_name: str) -> list[dict]:
    async with _AsyncSessionLocal() as session:
        stmt = (
            select(InterviewRecord)
            .where(InterviewRecord.candidate_name == candidate_name)
            .order_by(InterviewRecord.created_at.desc())
        )
        result = await session.execute(stmt)
        records = result.scalars().all()

    history = []
    for rec in records:
        summary = None
        if rec.summary_json:
            try:
                summary = json.loads(rec.summary_json)
            except json.JSONDecodeError:
                summary = None
        history.append({
            "id": rec.id,
            "candidate_name": rec.candidate_name,
            "target_role": rec.target_role,
            "company": rec.company,
            "created_at": rec.created_at.isoformat() if rec.created_at else None,
            "summary": summary,
            "message_count": len(rec.messages),
        })
    return history
