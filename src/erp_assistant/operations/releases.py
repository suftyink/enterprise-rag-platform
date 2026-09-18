from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.infrastructure.database import get_session
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.models.operations import (
    CorpusReleaseDocumentRecord,
    CorpusReleaseRecord,
    CorpusReleaseRepairRequestRecord,
    PilotReadinessRecord,
    PilotRolloutRecord,
)
from erp_assistant.operations.readiness_models import ReleaseIdentity
from erp_assistant.operations.release_contracts import (
    CorpusReleaseView,
    ReleaseApproval,
    ReleaseError,
    ReleaseStatus,
    RolloutState,
)
from erp_assistant.operations.release_store import CorpusReleaseStore
from erp_assistant.rag.index_manifest import capture_corpus_snapshot
from erp_assistant.rag.types import DataClassification


class CorpusReleaseWorkflow:
    def __init__(
        self,
        session: AsyncSession,
        *,
        maximum_evidence_age: timedelta = timedelta(hours=24),
    ) -> None:
        self._session = session
        self._store = CorpusReleaseStore(session)
        self._maximum_evidence_age = maximum_evidence_age

    async def list(self) -> tuple[CorpusReleaseView, ...]:
        return await self._store.list()

    async def bootstrap(
        self,
        profile_id: UUID,
        *,
        actor_id: str,
        trace_id: UUID,
        commit: bool = True,
    ) -> CorpusReleaseView:
        profile = await self._store.profile(profile_id)
        existing = await self._session.scalar(
            select(CorpusReleaseRecord).where(
                CorpusReleaseRecord.data_classification == profile.data_classification,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
        )
        if existing is not None:
            if existing.profile_id != profile_id:
                raise ReleaseError(
                    "BOOTSTRAP_ACTIVE_RELEASE_CONFLICT",
                    "当前分类已有绑定其他 Profile 的活动 Release",
                )
            return await self._store.view(existing)
        if profile.status != "ACTIVE":
            raise ReleaseError("BOOTSTRAP_PROFILE_NOT_ACTIVE", "只能从活动 Profile 初始化 Release")
        snapshot = await capture_corpus_snapshot(
            self._session, self._store.classification(profile.data_classification)
        )
        self._require_complete_build(profile, snapshot.corpus_hash, snapshot.document_count)
        release = self._store.new_release(
            release_version=f"bootstrap-{profile.profile_key}-v{profile.version}",
            profile=profile,
            corpus_generation=await self._store.generation(profile.data_classification),
            corpus_hash=snapshot.corpus_hash,
            document_count=snapshot.document_count,
            created_by=actor_id,
            status=ReleaseStatus.ACTIVE,
        )
        release.activated_at = release.created_at
        self._session.add(release)
        await self._store.add_members(release.id, snapshot.document_ids)
        self._store.audit(
            release,
            action="BOOTSTRAP",
            actor_id=actor_id,
            reason="Validated existing active Profile manifest",
            reason_code="BOOTSTRAP_VALIDATED",
            trace_id=trace_id,
        )
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return await self._store.view(release)

    async def create_candidate(
        self,
        release_version: str,
        profile_id: UUID,
        *,
        actor_id: str,
        trace_id: UUID,
        commit: bool = True,
    ) -> CorpusReleaseView:
        if not release_version.strip():
            raise ReleaseError("RELEASE_VERSION_REQUIRED", "候选发布版本不能为空")
        duplicate = await self._session.scalar(
            select(CorpusReleaseRecord).where(
                CorpusReleaseRecord.release_version == release_version.strip()
            )
        )
        if duplicate is not None:
            if duplicate.profile_id != profile_id:
                raise ReleaseError(
                    "RELEASE_VERSION_CONFLICT",
                    "候选发布版本已绑定其他 Profile",
                )
            return await self._store.view(duplicate)
        profile = await self._store.profile(profile_id)
        if profile.status != "STAGING":
            raise ReleaseError("INVALID_PROFILE_TRANSITION", "候选发布必须使用 STAGING Profile")
        classification = self._store.classification(profile.data_classification)
        snapshot = await capture_corpus_snapshot(self._session, classification)
        if not snapshot.document_count:
            raise ReleaseError("CANDIDATE_CORPUS_EMPTY", "候选语料不能为空")
        built = (
            profile.index_build_hash is not None
            and profile.index_build_corpus_hash == snapshot.corpus_hash
            and profile.index_build_document_count == snapshot.document_count
        )
        release = self._store.new_release(
            release_version=release_version.strip(),
            profile=profile,
            corpus_generation=await self._store.generation(profile.data_classification),
            corpus_hash=snapshot.corpus_hash,
            document_count=snapshot.document_count,
            created_by=actor_id,
            status=ReleaseStatus.EVALUATING if built else ReleaseStatus.BUILDING,
        )
        self._session.add(release)
        await self._store.add_members(release.id, snapshot.document_ids)
        self._store.audit(
            release,
            action="CREATE_CANDIDATE",
            actor_id=actor_id,
            reason="Approved content snapshot frozen",
            reason_code="CANDIDATE_FROZEN",
            trace_id=trace_id,
        )
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return await self._store.view(release)

    async def sync_build(
        self,
        release_id: UUID,
        *,
        actor_id: str,
        trace_id: UUID,
        commit: bool = True,
    ) -> CorpusReleaseView:
        release = await self._store.release(release_id, lock=True)
        if release.status not in {
            ReleaseStatus.BUILDING.value,
            ReleaseStatus.EVALUATING.value,
        }:
            raise ReleaseError("INVALID_RELEASE_TRANSITION", "当前 Release 不能同步构建证明")
        profile = await self._store.profile(release.profile_id)
        self._require_complete_build(profile, release.corpus_hash, release.document_count)
        release.index_build_hash = cast(str, profile.index_build_hash)
        release.status = ReleaseStatus.EVALUATING.value
        release.updated_at = datetime.now(UTC)
        self._store.audit(
            release,
            action="SYNC_BUILD",
            actor_id=actor_id,
            reason="Candidate build manifest verified",
            reason_code="BUILD_VERIFIED",
            trace_id=trace_id,
        )
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return await self._store.view(release)

    async def bind_readiness(
        self, release_id: UUID, *, actor_id: str, trace_id: UUID
    ) -> CorpusReleaseView:
        release = await self._store.release(release_id, lock=True)
        if release.status not in {
            ReleaseStatus.EVALUATING.value,
            ReleaseStatus.READY.value,
        }:
            raise ReleaseError("INVALID_RELEASE_TRANSITION", "当前 Release 不能绑定 readiness")
        report = await self._session.scalar(
            select(PilotReadinessRecord)
            .where(PilotReadinessRecord.release_version == release.release_version)
            .order_by(PilotReadinessRecord.generated_at.desc(), PilotReadinessRecord.id.desc())
            .limit(1)
        )
        if report is None:
            raise ReleaseError("READINESS_REPORT_MISSING", "候选 Release 缺少 readiness 报告")
        generated_at = _aware(report.generated_at)
        if report.decision != "PASS":
            raise ReleaseError("READINESS_NOT_PASSING", "候选 Release readiness 未通过")
        if datetime.now(UTC) - generated_at > self._maximum_evidence_age:
            raise ReleaseError("READINESS_REPORT_STALE", "候选 Release readiness 已过期")
        identity = _identity_from_report(report)
        self._validate_identity(release, identity, report)
        release.release_fingerprint = identity.fingerprint
        release.dataset_version = identity.dataset_version
        release.dataset_hash = identity.dataset_hash
        release.route_fingerprints = sorted(identity.route_fingerprints)
        release.answer_route_fingerprints = sorted(identity.answer_route_fingerprints)
        release.readiness_report_id = report.id
        release.status = ReleaseStatus.READY.value
        release.updated_at = datetime.now(UTC)
        self._store.audit(
            release,
            action="BIND_READINESS",
            actor_id=actor_id,
            reason="Exact PASS readiness identity bound",
            reason_code="READINESS_BOUND",
            trace_id=trace_id,
        )
        await self._session.commit()
        return await self._store.view(release)

    async def activate(
        self,
        release_id: UUID,
        approval: ReleaseApproval,
        *,
        trace_id: UUID,
    ) -> CorpusReleaseView:
        values = (
            approval.pilot_scope,
            approval.capacity,
            approval.monitoring_owner,
            approval.rollback_owner,
            approval.reason,
        )
        if any(not value.strip() for value in values):
            raise ReleaseError("RELEASE_APPROVAL_INCOMPLETE", "发布批准信息不完整")
        release = await self._store.release(release_id, lock=True)
        if release.status != ReleaseStatus.READY.value:
            raise ReleaseError("RELEASE_NOT_READY", "只有 READY Release 可以激活")
        await self._assert_current_readiness(release)
        active = await self._session.scalar(
            select(CorpusReleaseRecord)
            .where(
                CorpusReleaseRecord.data_classification == release.data_classification,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
            .with_for_update()
        )
        candidate_profile = await self._session.scalar(
            select(RetrievalProfileRecord)
            .where(RetrievalProfileRecord.id == release.profile_id)
            .with_for_update()
        )
        active_profile = (
            await self._session.scalar(
                select(RetrievalProfileRecord)
                .where(RetrievalProfileRecord.id == active.profile_id)
                .with_for_update()
            )
            if active is not None and active.id != release.id
            else None
        )
        if candidate_profile is None:
            raise ReleaseError("PROFILE_NOT_FOUND", "候选 Release 的 Profile 不存在")
        if active is not None and active.id != release.id:
            active.status = ReleaseStatus.ROLLBACK.value
            active.updated_at = datetime.now(UTC)
            if active_profile is not None and active_profile.id != candidate_profile.id:
                active_profile.status = "ROLLBACK"
            await self._session.flush()
        candidate_profile.status = "ACTIVE"
        now = datetime.now(UTC)
        release.previous_release_id = active.id if active and active.id != release.id else None
        release.status = ReleaseStatus.ACTIVE.value
        release.approved_by = approval.approved_by
        release.approval_reason = approval.reason.strip()
        release.pilot_scope = approval.pilot_scope.strip()
        release.capacity = approval.capacity.strip()
        release.monitoring_owner = approval.monitoring_owner.strip()
        release.rollback_owner = approval.rollback_owner.strip()
        release.activated_at = now
        release.updated_at = now
        self._session.add(
            PilotRolloutRecord(
                id=uuid4(),
                release_id=release.id,
                state=RolloutState.DISABLED.value,
                subject_ids=[],
                approved_by=approval.approved_by,
                reason="Release activated with rollout disabled",
                created_at=now,
            )
        )
        self._store.audit(
            release,
            action="ACTIVATE",
            actor_id=approval.approved_by,
            reason=approval.reason.strip(),
            reason_code="RELEASE_ACTIVATED",
            trace_id=trace_id,
        )
        try:
            await self._session.commit()
        except SQLAlchemyError as exc:
            await self._session.rollback()
            raise ReleaseError(
                "ACTIVATION_COMMIT_FAILED",
                "Release 激活提交失败，原活动版本保持不变",
            ) from exc
        return await self._store.view(release)

    async def rollback(
        self,
        release_id: UUID,
        *,
        actor_id: str,
        reason: str,
        trace_id: UUID,
    ) -> CorpusReleaseView:
        if not reason.strip():
            raise ReleaseError("ROLLBACK_REASON_REQUIRED", "回滚原因不能为空")
        target = await self._store.release(release_id, lock=True)
        active = await self._session.scalar(
            select(CorpusReleaseRecord)
            .where(
                CorpusReleaseRecord.data_classification == target.data_classification,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                ),
            )
            .with_for_update()
        )
        if (
            active is None
            or target.status != ReleaseStatus.ROLLBACK.value
            or active.previous_release_id != target.id
        ):
            raise ReleaseError("INVALID_ROLLBACK_TARGET", "只能回滚到当前版本的直接前序")
        active.status = ReleaseStatus.ROLLBACK.value
        active.updated_at = datetime.now(UTC)
        active_profile = await self._session.scalar(
            select(RetrievalProfileRecord)
            .where(RetrievalProfileRecord.id == active.profile_id)
            .with_for_update()
        )
        target_profile = await self._session.scalar(
            select(RetrievalProfileRecord)
            .where(RetrievalProfileRecord.id == target.profile_id)
            .with_for_update()
        )
        if active_profile is None or target_profile is None:
            raise ReleaseError("PROFILE_NOT_FOUND", "回滚 Release 的 Profile 不存在")
        active_profile.status = "ROLLBACK"
        await self._session.flush()
        target_profile.status = "ACTIVE"
        target.status = ReleaseStatus.ACTIVE.value
        target.previous_release_id = None
        target.approved_by = actor_id
        target.approval_reason = reason.strip()
        target.activated_at = datetime.now(UTC)
        target.updated_at = target.activated_at
        self._session.add(
            PilotRolloutRecord(
                id=uuid4(),
                release_id=target.id,
                state=RolloutState.PAUSED.value,
                subject_ids=[],
                approved_by=actor_id,
                reason=f"Rollback: {reason.strip()}",
                created_at=datetime.now(UTC),
            )
        )
        self._store.audit(
            target,
            action="ROLLBACK",
            actor_id=actor_id,
            reason=reason.strip(),
            reason_code="RELEASE_ROLLED_BACK",
            trace_id=trace_id,
        )
        await self._session.commit()
        return await self._store.view(target)

    async def set_rollout(
        self,
        release_id: UUID,
        state: RolloutState,
        *,
        subject_ids: tuple[str, ...],
        actor_id: str,
        reason: str,
        trace_id: UUID,
    ) -> CorpusReleaseView:
        release = await self._store.release(release_id)
        if release.status not in {
            ReleaseStatus.ACTIVE.value,
            ReleaseStatus.DEGRADED.value,
        }:
            raise ReleaseError("ROLLOUT_RELEASE_NOT_ACTIVE", "只能管理活动 Release 的 rollout")
        normalized = tuple(sorted({item.strip() for item in subject_ids if item.strip()}))
        if state is RolloutState.CANARY and not normalized:
            raise ReleaseError("CANARY_SUBJECTS_REQUIRED", "CANARY rollout 必须指定员工")
        if not reason.strip():
            raise ReleaseError("ROLLOUT_REASON_REQUIRED", "rollout 变更原因不能为空")
        self._session.add(
            PilotRolloutRecord(
                id=uuid4(),
                release_id=release.id,
                state=state.value,
                subject_ids=list(normalized),
                approved_by=actor_id,
                reason=reason.strip(),
                created_at=datetime.now(UTC),
            )
        )
        self._store.audit(
            release,
            action="SET_ROLLOUT",
            actor_id=actor_id,
            reason=reason.strip(),
            reason_code=f"ROLLOUT_{state.value}",
            trace_id=trace_id,
        )
        await self._session.commit()
        return await self._store.view(release)

    async def mark_degraded(
        self,
        release_id: UUID,
        *,
        document_id: UUID,
        actor_id: str,
        reason: str,
        reason_code: str,
        trace_id: UUID,
    ) -> CorpusReleaseView:
        release = await self._store.release(release_id, lock=True)
        if release.status not in {
            ReleaseStatus.ACTIVE.value,
            ReleaseStatus.DEGRADED.value,
        }:
            raise ReleaseError("RELEASE_NOT_ACTIVE", "只有当前服务 Release 可以标记降级")
        member = await self._session.get(
            CorpusReleaseDocumentRecord, (release.id, document_id)
        )
        if member is None:
            raise ReleaseError("DOCUMENT_NOT_IN_RELEASE", "目标文档不属于活动 Release")
        release.status = ReleaseStatus.DEGRADED.value
        release.degraded_reason = reason_code
        release.updated_at = datetime.now(UTC)
        repair_request = await self._session.scalar(
            select(CorpusReleaseRepairRequestRecord).where(
                CorpusReleaseRepairRequestRecord.release_id == release.id,
                CorpusReleaseRepairRequestRecord.excluded_document_id == document_id,
            )
        )
        if repair_request is None:
            self._session.add(
                CorpusReleaseRepairRequestRecord(
                    id=uuid4(),
                    release_id=release.id,
                    excluded_document_id=document_id,
                    status="QUEUED",
                    requested_by=actor_id,
                    reason=reason.strip(),
                    trace_id=trace_id,
                    created_at=datetime.now(UTC),
                )
            )
        self._session.add(
            PilotRolloutRecord(
                id=uuid4(),
                release_id=release.id,
                state=RolloutState.PAUSED.value,
                subject_ids=[],
                approved_by=actor_id,
                reason=reason.strip(),
                created_at=datetime.now(UTC),
            )
        )
        self._store.audit(
            release,
            action="EMERGENCY_WITHDRAWAL",
            actor_id=actor_id,
            reason=reason.strip(),
            reason_code=reason_code,
            trace_id=trace_id,
        )
        if repair_request is None:
            self._store.audit(
                release,
                action="QUEUE_REPAIR",
                actor_id=actor_id,
                reason=reason.strip(),
                reason_code="REPAIR_CANDIDATE_QUEUED",
                trace_id=trace_id,
            )
        await self._session.commit()
        return await self._store.view(release)

    async def require_active_member(
        self, release_id: UUID, document_id: UUID
    ) -> None:
        release = await self._store.release(release_id)
        if release.status not in {
            ReleaseStatus.ACTIVE.value,
            ReleaseStatus.DEGRADED.value,
        }:
            raise ReleaseError("RELEASE_NOT_ACTIVE", "只有当前服务 Release 可以紧急撤回")
        member = await self._session.get(
            CorpusReleaseDocumentRecord, (release.id, document_id)
        )
        if member is None:
            raise ReleaseError("DOCUMENT_NOT_IN_RELEASE", "目标文档不属于活动 Release")

    async def active(
        self,
        classification: DataClassification = DataClassification.ORDINARY,
    ) -> CorpusReleaseView | None:
        release = await self._session.scalar(
            select(CorpusReleaseRecord).where(
                CorpusReleaseRecord.data_classification == classification.value,
                CorpusReleaseRecord.status.in_(
                    [ReleaseStatus.ACTIVE.value, ReleaseStatus.DEGRADED.value]
                )
            )
        )
        return await self._store.view(release) if release else None

    async def _assert_current_readiness(self, release: CorpusReleaseRecord) -> None:
        if release.readiness_report_id is None or release.release_fingerprint is None:
            raise ReleaseError("READINESS_REPORT_MISSING", "Release 缺少 readiness 绑定")
        report = await self._session.get(PilotReadinessRecord, release.readiness_report_id)
        if report is None or report.decision != "PASS":
            raise ReleaseError("READINESS_NOT_PASSING", "Release readiness 未通过")
        if datetime.now(UTC) - _aware(report.generated_at) > self._maximum_evidence_age:
            raise ReleaseError("READINESS_REPORT_STALE", "Release readiness 已过期")
        if report.release_fingerprint != release.release_fingerprint:
            raise ReleaseError("RELEASE_FINGERPRINT_MISMATCH", "Release readiness 指纹不匹配")

    def _validate_identity(
        self,
        release: CorpusReleaseRecord,
        identity: ReleaseIdentity,
        report: PilotReadinessRecord,
    ) -> None:
        matches = (
            identity.release_version == release.release_version
            and identity.index_identity == release.index_identity
            and identity.index_build_hash == release.index_build_hash
            and identity.parser_version == release.parser_version
            and identity.chunk_version == release.chunk_version
            and identity.configuration_fingerprint == release.configuration_fingerprint
            and report.release_fingerprint == identity.fingerprint
        )
        if not matches:
            raise ReleaseError(
                "RELEASE_EVIDENCE_MISMATCH", "readiness 证据与候选 Release 不匹配"
            )

    @staticmethod
    def _require_complete_build(
        profile: RetrievalProfileRecord,
        corpus_hash: str,
        document_count: int,
    ) -> None:
        if (
            profile.index_build_hash is None
            or profile.index_build_corpus_hash != corpus_hash
            or profile.index_build_document_count != document_count
            or not document_count
        ):
            raise ReleaseError(
                "CANDIDATE_BUILD_STALE", "Profile 缺少候选语料的完整索引构建证明"
            )

def get_corpus_release_workflow(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CorpusReleaseWorkflow:
    return CorpusReleaseWorkflow(session)


def _identity_from_report(report: PilotReadinessRecord) -> ReleaseIdentity:
    payload = cast(dict[str, Any], report.payload)
    raw = payload.get("release")
    if not isinstance(raw, dict):
        raise ReleaseError("READINESS_IDENTITY_MISSING", "readiness 报告缺少发布身份")
    try:
        return ReleaseIdentity(
            release_version=str(raw["releaseVersion"]),
            dataset_version=str(raw["datasetVersion"]),
            dataset_hash=str(raw["datasetHash"]),
            index_identity=str(raw["indexIdentity"]),
            index_build_hash=str(raw["indexBuildHash"]),
            parser_version=str(raw["parserVersion"]),
            chunk_version=str(raw["chunkVersion"]),
            configuration_fingerprint=str(raw["configurationFingerprint"]),
            route_fingerprints=frozenset(map(str, raw["routeFingerprints"])),
            answer_route_fingerprints=frozenset(
                map(str, raw["answerRouteFingerprints"])
            ),
        )
    except (KeyError, TypeError) as exc:
        raise ReleaseError(
            "READINESS_IDENTITY_INVALID", "readiness 发布身份字段无效"
        ) from exc


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
