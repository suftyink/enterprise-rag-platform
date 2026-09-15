import asyncio
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.infrastructure.database import SessionFactory, engine
from erp_assistant.models.knowledge import KnowledgeDocument
from erp_assistant.rag.composition import get_document_indexer
from erp_assistant.rag.document_indexer import DocumentIndexer, IndexDecision
from erp_assistant.rag.knowledge_eligibility import retrieval_eligibility_filters


@dataclass(frozen=True)
class ReindexResult:
    document_count: int
    indexed_count: int
    skipped_count: int
    ocr_required_count: int
    permanent_failure_count: int
    chunk_count: int


async def reindex_active_documents(
    indexer: DocumentIndexer,
    session_factory: async_sessionmaker[AsyncSession],
) -> ReindexResult:
    async with session_factory() as session:
        document_ids = list(
            (
                await session.scalars(
                    select(KnowledgeDocument.id).where(
                        *retrieval_eligibility_filters(),
                    )
                )
            ).all()
        )
    outcomes = [await indexer.index(document_id) for document_id in document_ids]
    return ReindexResult(
        document_count=len(outcomes),
        indexed_count=sum(
            outcome.decision is IndexDecision.INDEXED_FOR_CURRENT_POLICY for outcome in outcomes
        ),
        skipped_count=sum(outcome.decision is IndexDecision.SKIPPED for outcome in outcomes),
        ocr_required_count=sum(
            outcome.decision is IndexDecision.OCR_REQUIRED for outcome in outcomes
        ),
        permanent_failure_count=sum(
            outcome.decision is IndexDecision.PERMANENT_FAILURE for outcome in outcomes
        ),
        chunk_count=sum(outcome.chunk_count for outcome in outcomes),
    )


async def _run() -> None:
    try:
        result = await reindex_active_documents(get_document_indexer(), SessionFactory)
        print(
            json.dumps(
                {
                    "status": "completed",
                    "documents": result.document_count,
                    "indexed": result.indexed_count,
                    "skipped": result.skipped_count,
                    "ocrRequired": result.ocr_required_count,
                    "permanentFailures": result.permanent_failure_count,
                    "chunks": result.chunk_count,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
