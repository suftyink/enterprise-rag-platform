from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from erp_assistant.infrastructure.database import SessionFactory
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.models.operations import CorpusReleaseDocumentRecord, CorpusReleaseRecord
from erp_assistant.rag.document_indexer import DocumentIndexer
from erp_assistant.rag.errors import RagError
from erp_assistant.rag.index_manifest import (
    build_manifest,
    capture_corpus_snapshot,
    capture_document_snapshot,
)
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.reindex import ReindexResult, reindex_active_documents
from erp_assistant.rag.runtime_registry import RetrievalRuntimeRegistry
from erp_assistant.rag.types import DataClassification


async def build_staging_index(
    profile_id: UUID,
    registry: RetrievalRuntimeRegistry,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    *,
    release_id: UUID | None = None,
) -> ReindexResult:
    async with session_factory() as session:
        candidate = await session.get(RetrievalProfileRecord, profile_id)
    if candidate is None:
        raise RagError("PROFILE_NOT_FOUND", "staging Profile 不存在")
    if candidate.status not in {"STAGING", "ROLLBACK"}:
        raise RagError("INVALID_PROFILE_TRANSITION", "只有候选或回滚 Profile 可以重建索引")
    try:
        classification = DataClassification(candidate.data_classification)
    except ValueError as exc:
        raise RagError("INVALID_RETRIEVAL_PROFILE", "检索 Profile 数据分类无效") from exc
    binding = registry.binding_for(candidate)
    async with session_factory() as session:
        stored_candidate = await session.get_one(RetrievalProfileRecord, profile_id)
        active = await session.scalar(
            select(RetrievalProfileRecord).where(
                RetrievalProfileRecord.data_classification == candidate.data_classification,
                RetrievalProfileRecord.status == "ACTIVE",
            )
        )
        if active is not None and active.index_identity == candidate.index_identity:
            raise RagError(
                "STAGING_INDEX_NOT_INDEPENDENT",
                "候选 Profile 必须使用独立于活动版本的向量索引",
            )
        if release_id is None:
            snapshot_before = await capture_corpus_snapshot(session, classification)
        else:
            release = await session.get(CorpusReleaseRecord, release_id)
            if release is None or release.profile_id != profile_id:
                raise RagError(
                    "RELEASE_PROFILE_MISMATCH", "候选 Release 与 staging Profile 不匹配"
                )
            document_ids = tuple(
                await session.scalars(
                    select(CorpusReleaseDocumentRecord.document_id).where(
                        CorpusReleaseDocumentRecord.release_id == release_id
                    )
                )
            )
            snapshot_before = await capture_document_snapshot(session, document_ids)
        stored_candidate.index_build_hash = None
        stored_candidate.index_build_corpus_hash = None
        stored_candidate.index_build_document_count = None
        stored_candidate.index_build_completed_at = None
        await session.commit()
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
        document_ids=snapshot_before.document_ids,
    )
    async with session_factory() as session:
        snapshot_after = (
            await capture_corpus_snapshot(session, classification)
            if release_id is None
            else await capture_document_snapshot(session, snapshot_before.document_ids)
        )
    if (
        result.document_count == 0
        or result.concurrent_indexing_count
        or result.ocr_required_count
        or result.permanent_failure_count
        or result.indexed_count + result.skipped_count != result.document_count
        or snapshot_after != snapshot_before
    ):
        raise RagError("STAGING_INDEX_INCOMPLETE", "候选 staging 索引构建不完整")
    manifest = build_manifest(candidate, snapshot_after)
    async with session_factory() as session:
        stored = await session.get_one(RetrievalProfileRecord, profile_id)
        if stored.status != candidate.status:
            raise RagError("INVALID_PROFILE_TRANSITION", "候选 Profile 状态已发生变化")
        stored.index_build_hash = manifest.build_hash
        stored.index_build_corpus_hash = manifest.corpus_hash
        stored.index_build_document_count = manifest.document_count
        stored.index_build_completed_at = datetime.now(UTC)
        await session.commit()
    return result
