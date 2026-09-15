from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.models.knowledge import (
    KnowledgeChunk,
    KnowledgeCleanupRequest,
    KnowledgeCorpusHead,
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeLifecycleEvent,
)
from erp_assistant.rag._knowledge_lifecycle import apply_transition
from erp_assistant.rag.document_indexer import IndexDecision
from erp_assistant.rag.knowledge_corpus_contracts import (
    SYSTEM_CORPUS_ACTOR,
    CorpusActor,
    CorpusAuditEvent,
    CorpusAuthorizer,
    CorpusChunkSummary,
    CorpusCleanup,
    CorpusDocumentView,
    CorpusError,
    CorpusProcessor,
    LifecycleAction,
    TrustedActorAuthorizer,
)
from erp_assistant.rag.types import (
    CleanupStatus,
    DocumentStatus,
    IndexJobStatus,
    PublicationStatus,
    RetentionStatus,
)


class KnowledgeCorpus:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        processor: CorpusProcessor,
        authorizer: CorpusAuthorizer | None = None,
    ) -> None:
        self._sessions = session_factory
        self._processor = processor
        self._authorizer = authorizer or TrustedActorAuthorizer()

    async def submit(
        self,
        document_id: UUID,
        actor: CorpusActor,
        *,
        reason: str,
    ) -> CorpusDocumentView:
        reason = self._reason(reason)
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            self._authorizer.require(actor, "SUBMIT", document.scope, document.scope_id)

        outcome = await self._processor.index(document_id)
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            document.parse_warnings = list(outcome.warnings)
            job = await self._latest_job(session, document_id)
            if job is None:
                job = KnowledgeIndexJob(
                    document_id=document.id,
                    status=IndexJobStatus.QUEUED.value,
                    phase=document.status,
                )
                session.add(job)
            if job.status == IndexJobStatus.QUEUED.value:
                document.status = outcome.status.value
                job.status = (
                    IndexJobStatus.SUCCEEDED.value
                    if outcome.decision
                    in {
                        IndexDecision.INDEXED_FOR_CURRENT_POLICY,
                        IndexDecision.SKIPPED,
                    }
                    else IndexJobStatus.FAILED.value
                )
                job.phase = outcome.status.value
                job.attempt = outcome.attempt
                job.chunk_count = outcome.chunk_count
                job.warnings = list(outcome.warnings)
                job.pipeline_revision = outcome.pipeline.revision
                job.error_code = outcome.error_code
            if outcome.status is DocumentStatus.READY and outcome.decision in {
                IndexDecision.INDEXED_FOR_CURRENT_POLICY,
                IndexDecision.SKIPPED,
            }:
                document.index_artifacts_present = True
                if not document.active and document.publication_status in {
                    PublicationStatus.UNPUBLISHED.value,
                    PublicationStatus.REJECTED.value,
                    PublicationStatus.WITHDRAWN.value,
                }:
                    document.publication_status = PublicationStatus.REVIEW_PENDING.value
            head = await self._head(session, document)
            revision = head.revision if head is not None else 0
            document.governance_revision = revision
            session.add(
                KnowledgeLifecycleEvent(
                    document_id=document.id,
                    action="SUBMIT",
                    actor_id=actor.subject_id,
                    reason=reason,
                    document_version=document.version,
                    revision=revision,
                    result=outcome.decision.value,
                )
            )
            await session.commit()
        return await self.inspect(document_id, actor)

    async def inspect(self, document_id: UUID, actor: CorpusActor) -> CorpusDocumentView:
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            self._authorizer.require(actor, "INSPECT", document.scope, document.scope_id)
            return await self._view(session, document)

    async def transition(
        self,
        document_id: UUID,
        action: LifecycleAction,
        actor: CorpusActor,
        *,
        reason: str,
        expected_revision: int,
    ) -> CorpusDocumentView:
        reason = self._reason(reason)
        async with self._sessions() as session:
            async with session.begin():
                document = await session.get(KnowledgeDocument, document_id)
                if document is None:
                    raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
                self._authorizer.require(actor, action.value, document.scope, document.scope_id)
                head = await self._head(session, document, for_update=True)
                if head is None:
                    head = KnowledgeCorpusHead(
                        scope=document.scope,
                        scope_id=document.scope_id,
                        document_key=document.document_key,
                    )
                    session.add(head)
                    await session.flush()
                if expected_revision != head.revision:
                    raise CorpusError(
                        "STALE_CORPUS_REVISION",
                        f"知识修订已变化，当前为 {head.revision}",
                    )
                await apply_transition(session, head, document, action, reason)
                head.revision += 1
                document.governance_revision = head.revision
                session.add(
                    KnowledgeLifecycleEvent(
                        document_id=document.id,
                        action=action.value,
                        actor_id=actor.subject_id,
                        reason=reason,
                        document_version=document.version,
                        revision=head.revision,
                        result="SUCCEEDED",
                    )
                )
        return await self.inspect(document_id, actor)

    async def _view(self, session: AsyncSession, document: KnowledgeDocument) -> CorpusDocumentView:
        head = await self._head(session, document)
        job = await self._latest_job(session, document.id)
        chunk_row = (
            await session.execute(
                select(
                    func.count(KnowledgeChunk.id),
                    func.coalesce(func.sum(KnowledgeChunk.char_count), 0),
                    func.min(KnowledgeChunk.page_start),
                    func.max(KnowledgeChunk.page_end),
                ).where(KnowledgeChunk.document_id == document.id)
            )
        ).one()
        events = (
            await session.scalars(
                select(KnowledgeLifecycleEvent)
                .where(KnowledgeLifecycleEvent.document_id == document.id)
                .order_by(KnowledgeLifecycleEvent.created_at, KnowledgeLifecycleEvent.id)
            )
        ).all()
        cleanups = (
            await session.scalars(
                select(KnowledgeCleanupRequest)
                .where(KnowledgeCleanupRequest.document_id == document.id)
                .order_by(KnowledgeCleanupRequest.created_at, KnowledgeCleanupRequest.id)
            )
        ).all()
        publication = PublicationStatus(document.publication_status)
        retention = RetentionStatus(document.retention_status)
        return CorpusDocumentView(
            document_id=document.id,
            document_key=document.document_key,
            version=document.version,
            title=document.title,
            original_filename=document.original_filename,
            media_type=document.media_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            processing_status=DocumentStatus(document.status),
            publication_status=publication,
            retention_status=retention,
            revision=head.revision if head is not None else 0,
            retrieval_eligible=(
                document.active
                and document.status == DocumentStatus.READY.value
                and publication is PublicationStatus.PUBLISHED
                and retention is RetentionStatus.RETAINED
            ),
            warnings=tuple(job.warnings if job is not None else document.parse_warnings),
            pipeline_revision=job.pipeline_revision if job is not None else None,
            chunk_summary=CorpusChunkSummary(
                chunk_count=int(chunk_row[0]),
                character_count=int(chunk_row[1]),
                first_page=cast(int | None, chunk_row[2]),
                last_page=cast(int | None, chunk_row[3]),
            ),
            audit_events=tuple(
                CorpusAuditEvent(
                    action=event.action,
                    actor_id=event.actor_id,
                    reason=event.reason,
                    document_version=event.document_version,
                    revision=event.revision,
                    result=event.result,
                )
                for event in events
            ),
            cleanup_requests=tuple(
                CorpusCleanup(
                    id=request.id,
                    kind=request.kind,
                    status=CleanupStatus(request.status),
                    attempt=request.attempt,
                    artifacts=tuple(request.artifacts),
                    error_detail=request.error_detail,
                )
                for request in cleanups
            ),
        )

    @staticmethod
    async def _head(
        session: AsyncSession,
        document: KnowledgeDocument,
        *,
        for_update: bool = False,
    ) -> KnowledgeCorpusHead | None:
        statement = select(KnowledgeCorpusHead).where(
            KnowledgeCorpusHead.scope == document.scope,
            KnowledgeCorpusHead.scope_id == document.scope_id,
            KnowledgeCorpusHead.document_key == document.document_key,
        )
        if for_update:
            statement = statement.with_for_update()
        return cast(
            KnowledgeCorpusHead | None,
            await session.scalar(statement),
        )

    @staticmethod
    async def _latest_job(session: AsyncSession, document_id: UUID) -> KnowledgeIndexJob | None:
        return cast(
            KnowledgeIndexJob | None,
            await session.scalar(
                select(KnowledgeIndexJob)
                .where(KnowledgeIndexJob.document_id == document_id)
                .order_by(KnowledgeIndexJob.created_at.desc())
                .limit(1)
            ),
        )

    @staticmethod
    def _reason(reason: str) -> str:
        value = reason.strip()
        if not value or len(value) > 500:
            raise CorpusError("INVALID_REASON", "治理原因不能为空且不能超过 500 字符")
        return value


__all__ = [
    "CorpusActor",
    "CorpusAuthorizer",
    "CorpusChunkSummary",
    "CorpusDocumentView",
    "CorpusError",
    "KnowledgeCorpus",
    "LifecycleAction",
    "PublicationStatus",
    "RetentionStatus",
    "SYSTEM_CORPUS_ACTOR",
]
