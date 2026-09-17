from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.identity import ActorContext
from erp_assistant.infrastructure.database import get_session
from erp_assistant.models.operations import (
    CorpusReleaseRecord,
    PilotAccessRecord,
    PilotReadinessRecord,
    PilotRolloutRecord,
)
from erp_assistant.operations.release_contracts import ReleaseStatus, RolloutState
from erp_assistant.rag.types import DataClassification


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
    release_id: UUID | None = None
    release_version: str | None = None
    rollout_state: str | None = None

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
            "releaseId": str(self.release_id) if self.release_id else None,
            "releaseVersion": self.release_version,
            "rolloutState": self.rollout_state,
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

    async def state(
        self,
        actor: ActorContext | None = None,
        *,
        now: datetime | None = None,
    ) -> PilotGateState:
        current_time = now or datetime.now(UTC)
        release = await self._session.scalar(
            select(CorpusReleaseRecord).where(
                CorpusReleaseRecord.data_classification
                == DataClassification.ORDINARY.value,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                )
            )
        )
        if release is not None:
            return await self._release_state(release, actor, current_time)
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

    async def require_enabled(self, actor: ActorContext | None = None) -> PilotGateState:
        state = await self.state(actor)
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
        release = await self._session.scalar(
            select(CorpusReleaseRecord).where(
                CorpusReleaseRecord.data_classification
                == DataClassification.ORDINARY.value,
                CorpusReleaseRecord.status == ReleaseStatus.ACTIVE.value,
            )
        )
        if release is not None:
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
            if decision.enabled:
                await self._assert_release_ready(release)
            release.pilot_scope = decision.pilot_scope.strip()
            release.capacity = decision.capacity.strip()
            release.monitoring_owner = decision.monitoring_owner.strip()
            release.rollback_owner = decision.rollback_owner.strip()
            self._session.add(
                PilotRolloutRecord(
                    id=uuid4(),
                    release_id=release.id,
                    state=(
                        RolloutState.ENABLED.value
                        if decision.enabled
                        else RolloutState.DISABLED.value
                    ),
                    subject_ids=[],
                    approved_by=decision.approved_by,
                    reason=decision.reason.strip(),
                    created_at=datetime.now(UTC),
                )
            )
            await self._session.commit()
            return await self.state()
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

    async def _release_state(
        self,
        release: CorpusReleaseRecord,
        actor: ActorContext | None,
        now: datetime,
    ) -> PilotGateState:
        if release.status == ReleaseStatus.DEGRADED.value:
            return _release_gate_state(False, False, "RELEASE_DEGRADED", release)
        if release.readiness_report_id is None or release.release_fingerprint is None:
            return _release_gate_state(False, False, "READINESS_REPORT_MISSING", release)
        report = await self._session.get(PilotReadinessRecord, release.readiness_report_id)
        if report is None:
            return _release_gate_state(False, False, "READINESS_REPORT_MISSING", release)
        generated_at = _aware(report.generated_at)
        if report.decision != "PASS":
            return _release_gate_state(False, False, "READINESS_NOT_PASSING", release, report)
        if now - generated_at > timedelta(hours=24):
            return _release_gate_state(False, False, "READINESS_REPORT_STALE", release, report)
        if report.release_fingerprint != release.release_fingerprint:
            return _release_gate_state(
                False, False, "RELEASE_FINGERPRINT_MISMATCH", release, report
            )
        rollout = await self._session.scalar(
            select(PilotRolloutRecord)
            .where(PilotRolloutRecord.release_id == release.id)
            .order_by(PilotRolloutRecord.created_at.desc(), PilotRolloutRecord.id.desc())
            .limit(1)
        )
        if rollout is None or rollout.state in {
            RolloutState.DISABLED.value,
            RolloutState.PAUSED.value,
        }:
            reason = (
                "PILOT_PAUSED"
                if rollout and rollout.state == RolloutState.PAUSED.value
                else "PILOT_DISABLED"
            )
            return _release_gate_state(False, True, reason, release, report, rollout)
        if rollout.state == RolloutState.CANARY.value and (
            actor is None or actor.subject_id not in set(rollout.subject_ids)
        ):
            return _release_gate_state(
                False, True, "PILOT_COHORT_DENIED", release, report, rollout
            )
        return _release_gate_state(True, True, "PILOT_ENABLED", release, report, rollout)

    async def _assert_release_ready(self, release: CorpusReleaseRecord) -> None:
        state = await self._release_state(release, None, datetime.now(UTC))
        if not state.available:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": state.reason_code, "message": "当前发布证据不允许开放试点"},
            )


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


def _release_gate_state(
    enabled: bool,
    available: bool,
    reason_code: str,
    release: CorpusReleaseRecord,
    report: PilotReadinessRecord | None = None,
    rollout: PilotRolloutRecord | None = None,
) -> PilotGateState:
    return PilotGateState(
        enabled=enabled,
        available=available,
        reason_code=reason_code,
        report_id=report.id if report else release.readiness_report_id,
        release_fingerprint=release.release_fingerprint,
        generated_at=_aware(report.generated_at) if report else None,
        approved_by=rollout.approved_by if rollout else release.approved_by,
        pilot_scope=release.pilot_scope,
        capacity=release.capacity,
        monitoring_owner=release.monitoring_owner,
        rollback_owner=release.rollback_owner,
        release_id=release.id,
        release_version=release.release_version,
        rollout_state=rollout.state if rollout else None,
    )
