from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.models.knowledge import (
    KnowledgeCorpusGeneration,
    KnowledgeDocument,
    RetrievalProfileRecord,
)
from erp_assistant.models.operations import (
    CorpusReleaseAuditRecord,
    CorpusReleaseDocumentRecord,
    CorpusReleaseRecord,
    CorpusReleaseRepairRequestRecord,
    PilotRolloutRecord,
)
from erp_assistant.operations.release_contracts import (
    CorpusReleaseView,
    ReleaseError,
    ReleaseStatus,
    RolloutState,
)
from erp_assistant.rag.types import DataClassification


class CorpusReleaseStore:
    """Persistence boundary for immutable releases, membership, rollout and audit."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self) -> tuple[CorpusReleaseView, ...]:
        records = (
            await self.session.scalars(
                select(CorpusReleaseRecord).order_by(
                    CorpusReleaseRecord.created_at.desc(), CorpusReleaseRecord.id.desc()
                )
            )
        ).all()
        return tuple([await self.view(record) for record in records])

    async def view(self, release: CorpusReleaseRecord) -> CorpusReleaseView:
        rollout = await self.session.scalar(
            select(PilotRolloutRecord)
            .where(PilotRolloutRecord.release_id == release.id)
            .order_by(PilotRolloutRecord.created_at.desc(), PilotRolloutRecord.id.desc())
            .limit(1)
        )
        repair_document_ids = tuple(
            await self.session.scalars(
                select(CorpusReleaseRepairRequestRecord.excluded_document_id)
                .where(
                    CorpusReleaseRepairRequestRecord.release_id == release.id,
                    CorpusReleaseRepairRequestRecord.status == "QUEUED",
                )
                .order_by(CorpusReleaseRepairRequestRecord.created_at)
            )
        )
        return CorpusReleaseView(
            id=release.id,
            release_version=release.release_version,
            status=ReleaseStatus(release.status),
            data_classification=self.classification(release.data_classification),
            corpus_generation=release.corpus_generation,
            corpus_hash=release.corpus_hash,
            document_count=release.document_count,
            profile_id=release.profile_id,
            index_identity=release.index_identity,
            index_build_hash=release.index_build_hash,
            release_fingerprint=release.release_fingerprint,
            readiness_report_id=release.readiness_report_id,
            previous_release_id=release.previous_release_id,
            rollout_state=RolloutState(rollout.state) if rollout else RolloutState.DISABLED,
            rollout_subject_ids=tuple(rollout.subject_ids) if rollout else (),
            degraded_reason=release.degraded_reason,
            failure_reason_code=release.failure_reason_code,
            repair_excluded_document_ids=repair_document_ids,
        )

    async def profile(self, profile_id: UUID) -> RetrievalProfileRecord:
        profile = await self.session.get(RetrievalProfileRecord, profile_id)
        if profile is None:
            raise ReleaseError("PROFILE_NOT_FOUND", "检索 Profile 不存在")
        return profile

    async def release(
        self, release_id: UUID, *, lock: bool = False
    ) -> CorpusReleaseRecord:
        statement = select(CorpusReleaseRecord).where(CorpusReleaseRecord.id == release_id)
        if lock:
            statement = statement.with_for_update()
        release = await self.session.scalar(statement)
        if release is None:
            raise ReleaseError("RELEASE_NOT_FOUND", "CorpusRelease 不存在")
        return release

    async def generation(self, classification: str) -> int:
        generation = await self.session.get(KnowledgeCorpusGeneration, classification)
        return generation.generation if generation else 0

    async def add_members(
        self, release_id: UUID, document_ids: tuple[UUID, ...]
    ) -> None:
        documents = (
            await self.session.scalars(
                select(KnowledgeDocument).where(KnowledgeDocument.id.in_(document_ids))
            )
        ).all()
        now = datetime.now(UTC)
        self.session.add_all(
            [
                CorpusReleaseDocumentRecord(
                    release_id=release_id,
                    document_id=document.id,
                    document_sha256=document.sha256,
                    governance_revision=document.governance_revision,
                    created_at=now,
                )
                for document in documents
            ]
        )

    def audit(
        self,
        release: CorpusReleaseRecord,
        *,
        action: str,
        actor_id: str,
        reason: str,
        reason_code: str,
        trace_id: UUID,
    ) -> None:
        self.session.add(
            CorpusReleaseAuditRecord(
                id=uuid4(),
                release_id=release.id,
                action=action,
                actor_id=actor_id,
                reason=reason,
                reason_code=reason_code,
                trace_id=trace_id,
                created_at=datetime.now(UTC),
            )
        )

    @staticmethod
    def new_release(
        *,
        release_version: str,
        profile: RetrievalProfileRecord,
        corpus_generation: int,
        corpus_hash: str,
        document_count: int,
        created_by: str,
        status: ReleaseStatus,
    ) -> CorpusReleaseRecord:
        now = datetime.now(UTC)
        return CorpusReleaseRecord(
            id=uuid4(),
            release_version=release_version,
            status=status.value,
            data_classification=profile.data_classification,
            corpus_generation=corpus_generation,
            corpus_hash=corpus_hash,
            document_count=document_count,
            profile_id=profile.id,
            index_identity=profile.index_identity,
            index_build_hash=profile.index_build_hash or "",
            parser_version=profile.parser_version,
            chunk_version=profile.chunk_version,
            configuration_fingerprint=profile.configuration_fingerprint,
            route_fingerprints=[],
            answer_route_fingerprints=[],
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def classification(value: str) -> DataClassification:
        try:
            return DataClassification(value)
        except ValueError as exc:
            raise ReleaseError(
                "INVALID_DATA_CLASSIFICATION", "Release 数据分类无效"
            ) from exc
