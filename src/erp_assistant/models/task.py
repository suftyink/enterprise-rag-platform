from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from erp_assistant.infrastructure.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class AiTask(Base):
    __tablename__ = "ai_task"
    __table_args__ = (Index("ix_ai_task_tenant_status", "tenant_id", "status"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    trace_id: Mapped[UUID] = mapped_column(Uuid, unique=True, nullable=False, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[str | None] = mapped_column(String(64))
    store_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    mode: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="READY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    messages: Mapped[list["AiTaskMessage"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class AiTaskMessage(Base):
    __tablename__ = "ai_task_message"
    __table_args__ = (Index("ix_ai_task_message_task", "task_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_task.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    task: Mapped[AiTask] = relationship(back_populates="messages")
