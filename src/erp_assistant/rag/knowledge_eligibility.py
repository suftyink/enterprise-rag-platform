from sqlalchemy.sql.elements import ColumnElement

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
