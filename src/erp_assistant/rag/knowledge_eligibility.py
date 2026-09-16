from sqlalchemy import false, or_
from sqlalchemy.sql.elements import ColumnElement

from erp_assistant.identity.models import KnowledgeScopeRef
from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.types import DocumentStatus, PublicationStatus, RetentionStatus


def is_retrieval_eligible(document: KnowledgeDocument) -> bool:
    return (
        document.active
        and document.status == DocumentStatus.READY.value
        and document.publication_status == PublicationStatus.PUBLISHED.value
        and document.retention_status == RetentionStatus.RETAINED.value
    )


def retrieval_eligibility_filters() -> tuple[ColumnElement[bool], ...]:
    return (
        KnowledgeDocument.active.is_(True),
        KnowledgeDocument.status == DocumentStatus.READY.value,
        KnowledgeDocument.publication_status == PublicationStatus.PUBLISHED.value,
        KnowledgeDocument.retention_status == RetentionStatus.RETAINED.value,
    )


def authorized_scope_filter(
    scopes: tuple[KnowledgeScopeRef, ...],
) -> ColumnElement[bool]:
    filters = tuple(
        (KnowledgeDocument.scope == scope.scope.value)
        & (KnowledgeDocument.scope_id == scope.scope_id)
        for scope in scopes
    )
    return or_(*filters) if filters else false()
