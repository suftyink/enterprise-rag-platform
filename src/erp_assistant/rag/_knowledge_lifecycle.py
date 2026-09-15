from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp_assistant.models.knowledge import (
    KnowledgeCleanupRequest,
    KnowledgeCorpusHead,
    KnowledgeDocument,
)
from erp_assistant.rag.knowledge_corpus_contracts import (
    CorpusError,
    LifecycleAction,
)
from erp_assistant.rag.types import (
    CleanupStatus,
    DocumentStatus,
    PublicationStatus,
    RetentionStatus,
)


async def apply_transition(
    session: AsyncSession,
    head: KnowledgeCorpusHead,
    document: KnowledgeDocument,
    action: LifecycleAction,
    reason: str,
) -> None:
    publication = PublicationStatus(document.publication_status)
    retention = RetentionStatus(document.retention_status)
    if action is LifecycleAction.PUBLISH:
        if (
            document.status != DocumentStatus.READY.value
            or publication is not PublicationStatus.REVIEW_PENDING
            or retention is not RetentionStatus.RETAINED
            or not document.index_artifacts_present
        ):
            _invalid(action, publication)
        await _publish(session, head, document)
        return
    if action is LifecycleAction.REJECT:
        if publication is not PublicationStatus.REVIEW_PENDING:
            _invalid(action, publication)
        document.publication_status = PublicationStatus.REJECTED.value
        document.active = False
        return
    if action is LifecycleAction.WITHDRAW:
        if (
            publication is not PublicationStatus.PUBLISHED
            or head.published_document_id != document.id
        ):
            _invalid(action, publication)
        document.publication_status = PublicationStatus.WITHDRAWN.value
        document.active = False
        head.published_document_id = None
        _add_cleanup(session, document, "VECTOR", head.revision + 1)
        return
    if action is LifecycleAction.RESUBMIT:
        if (
            publication
            not in {
                PublicationStatus.REJECTED,
                PublicationStatus.WITHDRAWN,
            }
            or not document.index_artifacts_present
        ):
            _invalid(action, publication)
        document.publication_status = PublicationStatus.REVIEW_PENDING.value
        await _cancel_pending_cleanup(session, document.id)
        return
    if action is LifecycleAction.ROLLBACK:
        if (
            publication not in {PublicationStatus.SUPERSEDED, PublicationStatus.WITHDRAWN}
            or document.status != DocumentStatus.READY.value
            or retention is not RetentionStatus.RETAINED
            or not document.index_artifacts_present
        ):
            _invalid(action, publication)
        await _publish(session, head, document)
        return
    if action is LifecycleAction.REQUEST_PURGE:
        if publication is PublicationStatus.PUBLISHED:
            _invalid(action, publication)
        if retention is RetentionStatus.PURGED:
            _invalid(action, publication)
        document.retention_status = RetentionStatus.PURGE_REQUESTED.value
        document.active = False
        _add_cleanup(session, document, "VECTOR_AND_OBJECT", head.revision + 1)
        return
    cleanup = await _latest_cleanup(session, document.id)
    if cleanup is None:
        raise CorpusError("CLEANUP_NOT_FOUND", "文档没有待处理的清理任务")
    if action is LifecycleAction.MARK_CLEANUP_FAILED:
        if cleanup.status != CleanupStatus.PENDING.value:
            _invalid(action, publication)
        cleanup.status = CleanupStatus.FAILED.value
        cleanup.attempt += 1
        cleanup.error_detail = reason
        return
    if action is LifecycleAction.RETRY_CLEANUP:
        if cleanup.status != CleanupStatus.FAILED.value:
            _invalid(action, publication)
        cleanup.status = CleanupStatus.PENDING.value
        cleanup.error_detail = None
        return
    if action is LifecycleAction.COMPLETE_CLEANUP:
        if cleanup.status not in {
            CleanupStatus.PENDING.value,
            CleanupStatus.FAILED.value,
        }:
            _invalid(action, publication)
        cleanup.status = CleanupStatus.COMPLETED.value
        cleanup.error_detail = None
        document.index_artifacts_present = False
        if cleanup.kind == "VECTOR_AND_OBJECT":
            document.retention_status = RetentionStatus.PURGED.value
        return
    _invalid(action, publication)


async def _publish(
    session: AsyncSession,
    head: KnowledgeCorpusHead,
    document: KnowledgeDocument,
) -> None:
    current_documents = (
        await session.scalars(
            select(KnowledgeDocument).where(
                KnowledgeDocument.scope == document.scope,
                KnowledgeDocument.scope_id == document.scope_id,
                KnowledgeDocument.document_key == document.document_key,
                KnowledgeDocument.active.is_(True),
                KnowledgeDocument.id != document.id,
            )
        )
    ).all()
    for current in current_documents:
        current.active = False
        current.publication_status = PublicationStatus.SUPERSEDED.value
        current.governance_revision = head.revision + 1
    await session.flush()
    document.active = True
    document.publication_status = PublicationStatus.PUBLISHED.value
    document.published_at = datetime.now(UTC)
    head.published_document_id = document.id
    await _cancel_pending_cleanup(session, document.id)


def _add_cleanup(
    session: AsyncSession,
    document: KnowledgeDocument,
    kind: str,
    revision: int,
) -> None:
    artifacts = [
        {
            "kind": "vector_document",
            "document_id": str(document.vector_document_id or document.id),
        }
    ]
    if kind == "VECTOR_AND_OBJECT":
        artifacts.extend(
            {"kind": "object", "object_key": object_key}
            for object_key in [document.object_key, document.parsed_object_key]
            if object_key
        )
    session.add(
        KnowledgeCleanupRequest(
            document_id=document.id,
            idempotency_key=f"{document.id}:{kind}:{revision}",
            kind=kind,
            status=CleanupStatus.PENDING.value,
            artifacts=artifacts,
        )
    )


async def _cancel_pending_cleanup(session: AsyncSession, document_id: UUID) -> None:
    requests = (
        await session.scalars(
            select(KnowledgeCleanupRequest).where(
                KnowledgeCleanupRequest.document_id == document_id,
                KnowledgeCleanupRequest.status == CleanupStatus.PENDING.value,
            )
        )
    ).all()
    for request in requests:
        request.status = CleanupStatus.CANCELLED.value


async def _latest_cleanup(
    session: AsyncSession, document_id: UUID
) -> KnowledgeCleanupRequest | None:
    return cast(
        KnowledgeCleanupRequest | None,
        await session.scalar(
            select(KnowledgeCleanupRequest)
            .where(KnowledgeCleanupRequest.document_id == document_id)
            .order_by(KnowledgeCleanupRequest.created_at.desc())
            .limit(1)
        ),
    )


def _invalid(action: LifecycleAction, publication: PublicationStatus) -> None:
    raise CorpusError(
        "INVALID_CORPUS_TRANSITION",
        f"不能从 {publication.value} 执行 {action.value}",
    )
