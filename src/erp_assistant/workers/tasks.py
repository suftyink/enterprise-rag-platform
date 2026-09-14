import asyncio
from uuid import UUID

from erp_assistant.infrastructure.database import engine
from erp_assistant.rag.service import process_document
from erp_assistant.workers.celery_app import celery_app


@celery_app.task(name="erp_assistant.health")  # type: ignore[untyped-decorator]
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _run_index(document_id: UUID) -> None:
    try:
        await process_document(document_id)
    finally:
        await engine.dispose()


@celery_app.task(name="erp_assistant.index_document")  # type: ignore[untyped-decorator]
def index_document(document_id: str) -> dict[str, str]:
    asyncio.run(_run_index(UUID(document_id)))
    return {"documentId": document_id, "status": "processed"}
