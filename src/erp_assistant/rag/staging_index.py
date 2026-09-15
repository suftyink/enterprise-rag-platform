from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.rag.document_indexer import DocumentIndexer
from erp_assistant.rag.errors import RagError
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.reindex import ReindexResult, reindex_active_documents
from erp_assistant.rag.runtime_registry import RetrievalRuntimeRegistry
from erp_assistant.rag.types import DataClassification


async def build_staging_index(
    profile_id: UUID,
    registry: RetrievalRuntimeRegistry,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> ReindexResult:
    async with session_factory() as session:
        candidate = await session.get(RetrievalProfileRecord, profile_id)
    if candidate is None:
        raise RagError("PROFILE_NOT_FOUND", "staging Profile 不存在")
    if candidate.status != "STAGING":
        raise RagError("INVALID_PROFILE_TRANSITION", "只有 staging Profile 可以构建候选索引")
    try:
        classification = DataClassification(candidate.data_classification)
    except ValueError as exc:
        raise RagError("INVALID_RETRIEVAL_PROFILE", "检索 Profile 数据分类无效") from exc
    binding = registry.binding_for(candidate)
    indexer = DocumentIndexer(
        session_factory=session_factory,
        storage=get_object_storage(),
        parser=PypdfParser(),
        embedding=binding.embedding,
        vector_writer=binding.vector_store,
        settings=binding.settings,
    )
    if (
        indexer.pipeline.parser != candidate.parser_version
        or indexer.pipeline.chunking != candidate.chunk_version
        or indexer.pipeline.vector_index != candidate.index_identity
    ):
        raise RagError(
            "PROFILE_INDEX_BINDING_MISMATCH",
            "候选 Profile 与实际 Parser、Chunk 或索引身份不一致",
        )
    result = await reindex_active_documents(
        indexer,
        session_factory,
        data_classification=classification,
    )
    if (
        result.document_count == 0
        or result.ocr_required_count
        or result.permanent_failure_count
        or result.indexed_count + result.skipped_count != result.document_count
    ):
        raise RagError("STAGING_INDEX_INCOMPLETE", "候选 staging 索引构建不完整")
    return result
