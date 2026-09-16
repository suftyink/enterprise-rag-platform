from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import KnowledgeDocument, RetrievalProfileRecord
from erp_assistant.rag.classified_indexer import ActiveProfileDocumentIndexer
from erp_assistant.rag.document_indexer import (
    DocumentIndexOutcome,
    IndexDecision,
    IndexPipelineIdentity,
)
from erp_assistant.rag.types import DataClassification, DocumentStatus


@dataclass(frozen=True)
class Binding:
    name: str
    parser_version: str = "pypdf-v1"
    chunk_version: str = "chunk-v1"


class Registry:
    def binding_for(self, profile: RetrievalProfileRecord) -> Binding:
        return Binding(profile.profile_key)


class RecordingIndexer:
    def __init__(self, binding: Binding, calls: list[str]) -> None:
        self._binding = binding
        self._calls = calls
        self.pipeline = IndexPipelineIdentity(
            parser=binding.parser_version,
            chunking=binding.chunk_version,
            embedding="embedding-v1",
            vector_index=f"index-{binding.name}",
            revision=f"pipeline-{binding.name}",
        )

    async def index(self, document_id: UUID) -> DocumentIndexOutcome:
        self._calls.append(self._binding.name)
        return DocumentIndexOutcome(
            document_id=document_id,
            decision=IndexDecision.INDEXED_FOR_CURRENT_POLICY,
            status=DocumentStatus.READY,
            chunk_count=1,
            warnings=(),
            attempt=1,
            pipeline=self.pipeline,
        )


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incremental_indexing_follows_the_active_profile_after_switch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="policy",
        version=1,
        title="Policy",
        original_filename="policy.pdf",
        media_type="application/pdf",
        data_classification=DataClassification.ORDINARY.value,
        size_bytes=10,
        sha256="a" * 64,
        object_key="knowledge/policy.pdf",
        status=DocumentStatus.QUEUED.value,
        active=True,
        publication_status="PUBLISHED",
        retention_status="RETAINED",
        created_by="author-a",
    )

    def profile(profile_id: UUID, key: str, status: str) -> RetrievalProfileRecord:
        return RetrievalProfileRecord(
            id=profile_id,
            profile_key=key,
            version=1 if key == "v1" else 2,
            data_classification=DataClassification.ORDINARY.value,
            provider_boundary="CLOUD",
            embedding_deployment=f"embed-{key}",
            embedding_model="embedding",
            embedding_dimension=3,
            rerank_deployment=f"rerank-{key}",
            rerank_model="reranker",
            index_identity=f"index-{key}",
            parser_version="pypdf-v1",
            chunk_version="chunk-v1",
            retrieval_config={},
            configuration_fingerprint=f"fingerprint-{key}",
            production_ready=True,
            status=status,
        )

    async with session_factory() as session:
        session.add_all(
            [
                document,
                profile(first_id, "v1", "ACTIVE"),
                profile(second_id, "v2", "STAGING"),
            ]
        )
        await session.commit()

    calls: list[str] = []
    processor = ActiveProfileDocumentIndexer(
        session_factory,
        Registry(),  # type: ignore[arg-type]
        lambda binding: RecordingIndexer(binding, calls),  # type: ignore[arg-type,return-value]
    )

    await processor.index(document_id)
    async with session_factory() as session:
        first = await session.get_one(RetrievalProfileRecord, first_id)
        first.status = "ROLLBACK"
        await session.commit()
    async with session_factory() as session:
        second = await session.get_one(RetrievalProfileRecord, second_id)
        second.status = "ACTIVE"
        await session.commit()
    await processor.index(document_id)

    assert calls == ["v1", "v2"]
