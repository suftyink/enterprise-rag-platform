from collections.abc import Callable
from uuid import UUID

DocumentDispatcher = Callable[[UUID], str]


def enqueue_document(document_id: UUID) -> str:
    from erp_assistant.workers.tasks import index_document

    result = index_document.delay(str(document_id))
    return str(result.id)


def get_document_dispatcher() -> DocumentDispatcher:
    return enqueue_document
