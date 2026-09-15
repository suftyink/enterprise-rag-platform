import asyncio
from uuid import UUID

from erp_assistant.infrastructure.database import engine
from erp_assistant.rag.composition import get_knowledge_corpus
from erp_assistant.rag.document_indexer import DocumentIndexRetryableError
from erp_assistant.rag.knowledge_corpus import (
    SYSTEM_CORPUS_ACTOR,
    KnowledgeCorpus,
)
from erp_assistant.workers.celery_app import celery_app


@celery_app.task(name="erp_assistant.health")  # type: ignore[untyped-decorator]
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _run_index(
    document_id: UUID, corpus: KnowledgeCorpus | None = None
) -> dict[str, object]:
    try:
        view = await (corpus or get_knowledge_corpus()).submit(
            document_id,
            SYSTEM_CORPUS_ACTOR,
            reason="index pipeline completed",
        )
        return view.to_dict()
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="erp_assistant.index_document",
    acks_late=True,
    reject_on_worker_lost=True,
    autoretry_for=(DocumentIndexRetryableError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 5},
)
def index_document(document_id: str) -> dict[str, object]:
    return asyncio.run(_run_index(UUID(document_id)))
