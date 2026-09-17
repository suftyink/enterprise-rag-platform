from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from erp_assistant.infrastructure.database import Base


class PilotReadinessRecord(Base):
    __tablename__ = "pilot_readiness_report"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    report_version: Mapped[str] = mapped_column(String(32), nullable=False)
    release_version: Mapped[str] = mapped_column(String(128), nullable=False)
    release_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)


class PilotAccessRecord(Base):
    __tablename__ = "pilot_access_record"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    readiness_report_id: Mapped[UUID] = mapped_column(
        ForeignKey("pilot_readiness_report.id", ondelete="RESTRICT"), nullable=False
    )
    release_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    pilot_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    capacity: Mapped[str] = mapped_column(String(255), nullable=False)
    monitoring_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    rollback_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorpusReleaseRecord(Base):
    __tablename__ = "corpus_release"
    __table_args__ = (
        UniqueConstraint("release_version", name="uq_corpus_release_version"),
        Index(
            "uq_corpus_release_active_classification",
            "data_classification",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
            sqlite_where=text("status = 'ACTIVE'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    release_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    data_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    corpus_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    corpus_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("retrieval_profile.id", ondelete="RESTRICT"), nullable=False
    )
    index_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    index_build_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_version: Mapped[str] = mapped_column(String(128), nullable=False)
    configuration_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    release_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    dataset_version: Mapped[str | None] = mapped_column(String(128))
    dataset_hash: Mapped[str | None] = mapped_column(String(64))
    route_fingerprints: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    answer_route_fingerprints: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    readiness_report_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("pilot_readiness_report.id", ondelete="RESTRICT")
    )
    previous_release_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("corpus_release.id", ondelete="SET NULL")
    )
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approval_reason: Mapped[str | None] = mapped_column(Text)
    pilot_scope: Mapped[str | None] = mapped_column(String(255))
    capacity: Mapped[str | None] = mapped_column(String(255))
    monitoring_owner: Mapped[str | None] = mapped_column(String(128))
    rollback_owner: Mapped[str | None] = mapped_column(String(128))
    degraded_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CorpusReleaseDocumentRecord(Base):
    __tablename__ = "corpus_release_document"

    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("corpus_release.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("knowledge_document.id", ondelete="RESTRICT"), primary_key=True
    )
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    governance_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PilotRolloutRecord(Base):
    __tablename__ = "pilot_rollout_record"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("corpus_release.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    approved_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorpusReleaseAuditRecord(Base):
    __tablename__ = "corpus_release_audit"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    release_id: Mapped[UUID] = mapped_column(
        ForeignKey("corpus_release.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
