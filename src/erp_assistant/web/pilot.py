from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.infrastructure.database import get_session
from erp_assistant.models.operations import PilotAccessRecord, PilotReadinessRecord


@dataclass(frozen=True)
class PilotGateState:
    enabled: bool
    available: bool
    reason_code: str
    report_id: UUID | None = None
    release_fingerprint: str | None = None
    generated_at: datetime | None = None
    approved_by: str | None = None
    pilot_scope: str | None = None
    capacity: str | None = None
    monitoring_owner: str | None = None
    rollback_owner: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "reasonCode": self.reason_code,
            "reportId": str(self.report_id) if self.report_id else None,
            "releaseFingerprint": self.release_fingerprint,
            "generatedAt": self.generated_at.isoformat() if self.generated_at else None,
            "approvedBy": self.approved_by,
            "pilotScope": self.pilot_scope,
            "capacity": self.capacity,
            "monitoringOwner": self.monitoring_owner,
            "rollbackOwner": self.rollback_owner,
        }


@dataclass(frozen=True)
class PilotAccessDecision:
    enabled: bool
    approved_by: str
    pilot_scope: str
    capacity: str
    monitoring_owner: str
    rollback_owner: str
    reason: str


class PilotAccessGate:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def state(self, *, now: datetime | None = None) -> PilotGateState:
        current_time = now or datetime.now(UTC)
        report = await self._session.scalar(
            select(PilotReadinessRecord)
            .order_by(PilotReadinessRecord.generated_at.desc(), PilotReadinessRecord.id.desc())
            .limit(1)
        )
        if report is None:
            return PilotGateState(False, False, "READINESS_REPORT_MISSING")
        generated_at = _aware(report.generated_at)
        if report.decision != "PASS":
            return _state(False, False, "READINESS_NOT_PASSING", report, generated_at)
        if current_time - generated_at > timedelta(hours=24):
            return _state(False, False, "READINESS_REPORT_STALE", report, generated_at)
        configured = self._settings.pilot_release_fingerprint
        if not configured or report.release_fingerprint != configured:
            return _state(
                False, False, "RELEASE_FINGERPRINT_MISMATCH", report, generated_at
            )
        approval = await self._session.scalar(
            select(PilotAccessRecord)
            .order_by(PilotAccessRecord.created_at.desc(), PilotAccessRecord.id.desc())
            .limit(1)
        )
        if approval is None or not approval.enabled:
            return _state(False, True, "PILOT_DISABLED", report, generated_at)
        if approval.readiness_report_id != report.id:
            return _state(
                False,
                True,
                "APPROVAL_REPORT_MISMATCH",
                report,
                generated_at,
                approval,
            )
        return _state(True, True, "PILOT_ENABLED", report, generated_at, approval)

    async def require_enabled(self) -> PilotGateState:
        state = await self.state()
        if not state.enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": state.reason_code,
                    "message": "内部试点当前未开放，请联系试点负责人",
                },
            )
        return state

    async def record(self, decision: PilotAccessDecision) -> PilotGateState:
        state = await self.state()
        if decision.enabled and not state.available:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": state.reason_code, "message": "当前发布证据不允许开放试点"},
            )
        if state.report_id is None or state.release_fingerprint is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "READINESS_REPORT_MISSING", "message": "缺少发布证据"},
            )
        values = (
            decision.pilot_scope,
            decision.capacity,
            decision.monitoring_owner,
            decision.rollback_owner,
            decision.reason,
        )
        if any(not value.strip() for value in values):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "PILOT_APPROVAL_INCOMPLETE", "message": "试点批准信息不完整"},
            )
        self._session.add(
            PilotAccessRecord(
                id=uuid4(),
                enabled=decision.enabled,
                readiness_report_id=state.report_id,
                release_fingerprint=state.release_fingerprint,
                approved_by=decision.approved_by,
                pilot_scope=decision.pilot_scope.strip(),
                capacity=decision.capacity.strip(),
                monitoring_owner=decision.monitoring_owner.strip(),
                rollback_owner=decision.rollback_owner.strip(),
                reason=decision.reason.strip(),
                created_at=datetime.now(UTC),
            )
        )
        await self._session.commit()
        return await self.state()


def get_pilot_access_gate(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> PilotAccessGate:
    return PilotAccessGate(session, settings)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _state(
    enabled: bool,
    available: bool,
    reason_code: str,
    report: PilotReadinessRecord,
    generated_at: datetime,
    approval: PilotAccessRecord | None = None,
) -> PilotGateState:
    return PilotGateState(
        enabled=enabled,
        available=available,
        reason_code=reason_code,
        report_id=report.id,
        release_fingerprint=report.release_fingerprint,
        generated_at=generated_at,
        approved_by=approval.approved_by if approval else None,
        pilot_scope=approval.pilot_scope if approval else None,
        capacity=approval.capacity if approval else None,
        monitoring_owner=approval.monitoring_owner if approval else None,
        rollback_owner=approval.rollback_owner if approval else None,
    )
