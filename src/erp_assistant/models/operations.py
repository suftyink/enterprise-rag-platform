from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, DateTime, String, Uuid
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
