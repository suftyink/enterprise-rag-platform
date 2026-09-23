"""Current lifecycle capabilities; mutation paths still recheck under a lock."""

from dataclasses import dataclass
from uuid import UUID

from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.knowledge_corpus_contracts import LifecycleAction
from erp_assistant.rag.types import DocumentStatus, PublicationStatus, RetentionStatus


@dataclass(frozen=True)
class GovernanceActionDecision:
    allowed_actions: tuple[LifecycleAction, ...]
    unavailable_reason: str | None = None


def governance_action_decision(
    document: KnowledgeDocument,
    published_document_id: UUID | None,
    *,
    cleanup_running: bool = False,
) -> GovernanceActionDecision:
    publication = PublicationStatus(document.publication_status)
    retention = RetentionStatus(document.retention_status)
    ready = document.status == DocumentStatus.READY.value
    retained = retention is RetentionStatus.RETAINED
    has_index = document.index_artifacts_present
    actions: list[LifecycleAction] = []

    if publication is PublicationStatus.REVIEW_PENDING:
        if ready and retained and has_index and not cleanup_running:
            actions.append(LifecycleAction.PUBLISH)
        actions.append(LifecycleAction.REJECT)
    if publication is PublicationStatus.PUBLISHED and published_document_id == document.id:
        actions.append(LifecycleAction.WITHDRAW)
    if publication in {PublicationStatus.REJECTED, PublicationStatus.WITHDRAWN}:
        if has_index and not cleanup_running:
            actions.append(LifecycleAction.RESUBMIT)
    if publication in {PublicationStatus.SUPERSEDED, PublicationStatus.WITHDRAWN}:
        if ready and retained and has_index and not cleanup_running:
            actions.append(LifecycleAction.ROLLBACK)
    if publication is not PublicationStatus.PUBLISHED and retained:
        actions.append(LifecycleAction.REQUEST_PURGE)
    visible = {
        LifecycleAction.PUBLISH,
        LifecycleAction.WITHDRAW,
        LifecycleAction.ROLLBACK,
    }
    if any(action in visible for action in actions):
        return GovernanceActionDecision(tuple(actions))
    if not ready:
        reason = "PROCESSING_INCOMPLETE"
    elif cleanup_running:
        reason = "CLEANUP_IN_PROGRESS"
    elif not retained:
        reason = "RETENTION_NOT_RETAINED"
    elif not has_index:
        reason = "INDEX_ARTIFACTS_MISSING"
    elif publication is PublicationStatus.PUBLISHED:
        reason = "NOT_CURRENT_PUBLISHED_DOCUMENT"
    else:
        reason = "NO_AVAILABLE_GOVERNANCE_ACTION"
    return GovernanceActionDecision(tuple(actions), reason)
