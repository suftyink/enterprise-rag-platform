from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.identity import IdentityAction, IdentityResource
from erp_assistant.models.knowledge import (
    KnowledgeChunk,
    KnowledgeCleanupRequest,
    KnowledgeCorpusHead,
    KnowledgeDocument,
    KnowledgeIndexJob,
    KnowledgeLifecycleEvent,
)
from erp_assistant.operations.contracts import NullTelemetry, Telemetry
from erp_assistant.rag._knowledge_lifecycle import apply_transition
from erp_assistant.rag.corpus_generation import lock_corpus_generation
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
)
from erp_assistant.rag.knowledge_eligibility import is_retrieval_eligible
from erp_assistant.rag.knowledge_queries import latest_job
from erp_assistant.rag.types import (
    CleanupKind,
    CleanupStatus,
    DataClassification,
    DocumentStatus,
    IndexJobStatus,
    KnowledgeScope,
    PublicationStatus,
    RetentionStatus,
)


class KnowledgeCorpus:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        processor: CorpusProcessor,
        authorizer: CorpusAuthorizer,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._sessions = session_factory
        self._processor = processor
        self._authorizer = authorizer
        self._telemetry = telemetry or NullTelemetry()

    async def submit(
        self,
        document_id: UUID,
        actor: CorpusActor,
        *,
        reason: str,
        trace_id: UUID | None = None,
    ) -> CorpusDocumentView:
        reason = self._reason(reason)
        operation_trace = trace_id or uuid4()
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            await self._authorizer.require(
                actor,
                IdentityAction.SUBMIT,
                self._resource(document),
                operation_trace,
            )
            if document.active or document.publication_status == PublicationStatus.PUBLISHED.value:
                raise CorpusError(
                    "PUBLISHED_DOCUMENT_REQUIRES_NEW_VERSION",
                    "已发布文档必须创建新版本后再提交处理",
                )

        outcome = await self._processor.index(document_id, trace_id=operation_trace)
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            document.parse_warnings = list(outcome.warnings)
            job = await latest_job(session, document_id)
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
        self._emit_lifecycle(
            operation_trace,
            "SUBMIT",
            outcome.decision.value,
            document.version,
        )
        return await self.inspect(document_id, actor, trace_id=operation_trace)

    async def inspect(
        self,
        document_id: UUID,
        actor: CorpusActor,
        *,
        trace_id: UUID | None = None,
    ) -> CorpusDocumentView:
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            await self._authorizer.require(
                actor,
                IdentityAction.INSPECT,
                self._resource(document),
                trace_id or uuid4(),
            )
            return await self._view(session, document)

    async def transition(
        self,
        document_id: UUID,
        action: LifecycleAction,
        actor: CorpusActor,
        *,
        reason: str,
        expected_revision: int,
        trace_id: UUID | None = None,
    ) -> CorpusDocumentView:
        reason = self._reason(reason)
        operation_trace = trace_id or uuid4()
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
            await self._authorizer.require(
                actor,
                IdentityAction(action.value),
                self._resource(document),
                operation_trace,
            )
        try:
            await self._transition(document_id, action, actor, reason, expected_revision)
        except CorpusError as exc:
            self._telemetry.emit(
                trace_id=operation_trace,
                signal="lifecycle_transition",
                stage="lifecycle",
                outcome="FAILED",
                attributes={"decision": action.value, "reason_code": exc.code},
            )
            await self._record_failed_transition(
                document_id,
                action,
                actor,
                reason,
                exc.code,
            )
            raise
        self._emit_lifecycle(operation_trace, action.value, "SUCCEEDED", document.version)
        return await self.inspect(document_id, actor, trace_id=operation_trace)

    def _emit_lifecycle(
        self,
        trace_id: UUID,
        action: str,
        result: str,
        document_version: int,
    ) -> None:
        self._telemetry.emit(
            trace_id=trace_id,
            signal="lifecycle_transition",
            stage="lifecycle",
            outcome="FAILED" if result.endswith("FAILED") else "SUCCEEDED",
            attributes={
                "decision": action,
                "reason_code": result,
                "document_version": document_version,
            },
        )

    async def _transition(
        self,
        document_id: UUID,
        action: LifecycleAction,
        actor: CorpusActor,
        reason: str,
        expected_revision: int,
    ) -> None:
        async with self._sessions() as session:
            async with session.begin():
                document = await session.scalar(
                    select(KnowledgeDocument)
                    .where(KnowledgeDocument.id == document_id)
                    .with_for_update()
                )
                if document is None:
                    raise CorpusError("DOCUMENT_NOT_FOUND", "文档不存在")
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
                generation = await lock_corpus_generation(
                    session,
                    DataClassification(document.data_classification),
                )
                await apply_transition(session, head, document, action, reason)
                head.revision += 1
                document.governance_revision = head.revision
                generation.generation += 1
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

    async def _record_failed_transition(
        self,
        document_id: UUID,
        action: LifecycleAction,
        actor: CorpusActor,
        reason: str,
        result: str,
    ) -> None:
        async with self._sessions() as session:
            document = await session.get(KnowledgeDocument, document_id)
            if document is None:
                return
            head = await self._head(session, document)
            session.add(
                KnowledgeLifecycleEvent(
                    document_id=document.id,
                    action=action.value,
                    actor_id=actor.subject_id,
                    reason=reason,
                    document_version=document.version,
                    revision=head.revision if head is not None else 0,
                    result=result,
                )
            )
            await session.commit()

    async def _view(self, session: AsyncSession, document: KnowledgeDocument) -> CorpusDocumentView:
        head = await self._head(session, document)
        job = await latest_job(session, document.id)
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
            retrieval_eligible=is_retrieval_eligible(document),
            warnings=tuple(job.warnings if job is not None else document.parse_warnings),
            pipeline_revision=job.pipeline_revision if job is not None else None,
            parser_version=job.parser_version if job is not None else None,
            chunking_version=job.chunking_version if job is not None else None,
            quality_signals=job.quality_signals if job is not None else {},
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
                    created_at=event.created_at,
                )
                for event in events
            ),
            cleanup_requests=tuple(
                CorpusCleanup(
                    id=request.id,
                    idempotency_key=request.idempotency_key,
                    kind=CleanupKind(request.kind),
                    status=CleanupStatus(request.status),
                    attempt=request.attempt,
                    artifacts=tuple(request.artifacts),
                    error_detail=request.error_detail,
                )
                for request in cleanups
            ),
        )

    @staticmethod
    def _resource(document: KnowledgeDocument) -> IdentityResource:
        return IdentityResource(
            resource_type="knowledge_document",
            resource_id=str(document.id),
            scope=KnowledgeScope(document.scope),
            scope_id=document.scope_id,
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
