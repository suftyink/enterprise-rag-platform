from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from erp_assistant.infrastructure.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_document"
    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "sha256", name="uq_knowledge_document_hash"),
        UniqueConstraint(
            "scope",
            "scope_id",
            "document_key",
            "version",
            name="uq_knowledge_document_version",
        ),
        Index("ix_knowledge_document_scope_status", "scope", "scope_id", "status"),
        Index("ix_knowledge_document_key_active", "scope", "scope_id", "document_key", "active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    parsed_object_key: Mapped[str | None] = mapped_column(String(512))
    page_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    parse_warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["KnowledgeIndexJob"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_knowledge_chunk_position"),
        Index("ix_knowledge_chunk_scope", "scope", "scope_id"),
        Index("ix_knowledge_chunk_document", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_document.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    section_title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class KnowledgeIndexJob(Base):
    __tablename__ = "knowledge_index_job"
    __table_args__ = (Index("ix_knowledge_index_job_document", "document_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_document.id", ondelete="CASCADE"), nullable=False
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document: Mapped[KnowledgeDocument] = relationship(back_populates="jobs")
