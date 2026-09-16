from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, Text, Uuid
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
