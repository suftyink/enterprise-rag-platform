from collections.abc import Callable
from uuid import UUID

DocumentDispatcher = Callable[[UUID, UUID], str]


def enqueue_document(document_id: UUID, trace_id: UUID) -> str:
    from erp_assistant.workers.tasks import index_document

    result = index_document.apply_async(
        args=[str(document_id)],
        headers={"trace_id": str(trace_id)},
    )
    return str(result.id)


def get_document_dispatcher() -> DocumentDispatcher:
    return enqueue_document
