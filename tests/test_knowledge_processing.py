from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.core.config import Settings
from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument
from erp_assistant.orchestration.fake_providers import FakeEmbeddingProvider
from erp_assistant.rag.chunker import ChunkDraft
from erp_assistant.rag.service import create_document, process_document, rebuild_active_index
from erp_assistant.rag.types import DocumentStatus, KnowledgeScope
from erp_assistant.rag.vector_store import VectorHit
from tests.pdf_factory import make_pdf


@dataclass
class MemoryStorage:
    objects: dict[str, bytes] = field(default_factory=dict)

    def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = content

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


@dataclass
class MemoryVectorStore:
    documents: dict[UUID, list[UUID]] = field(default_factory=dict)
    deleted: list[UUID] = field(default_factory=list)

    def upsert_document(
        self,
        document_id: UUID,
        scope: str,
        scope_id: str,
        version: int,
        chunks: list[ChunkDraft],
        vectors: list[list[float]],
    ) -> None:
        del scope, scope_id, version
        assert len(chunks) == len(vectors)
        self.documents[document_id] = [chunk.id for chunk in chunks]

    def delete_document(self, document_id: UUID) -> None:
        self.deleted.append(document_id)
        self.documents.pop(document_id, None)

    def search(
        self,
        query: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]:
        del query, vector, tenant_id, limit, rrf_k
        return []


@pytest_asyncio.fixture
async def processing_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_processing_activates_new_version_and_supersedes_old(
    processing_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("erp_assistant.rag.service.SessionFactory", processing_factory)
    settings = Settings(
        rag_enabled=True,
        knowledge_min_text_per_page=5,
        knowledge_chunk_target=120,
        knowledge_chunk_max=180,
        knowledge_chunk_overlap=20,
    )
    storage = MemoryStorage()
    vector_store = MemoryVectorStore()
    embedding = FakeEmbeddingProvider()

    first = await _create(
        processing_factory,
        storage,
        make_pdf(["Inventory policy. Count stock every week. " * 8]),
    )
    await process_document(
        first.id,
        settings=settings,
        storage=storage,
        vector_store=vector_store,
        embedding=embedding,
    )
    second = await _create(
        processing_factory,
        storage,
        make_pdf(["Inventory policy. Count stock every day. " * 8]),
    )
    await process_document(
        second.id,
        settings=settings,
        storage=storage,
        vector_store=vector_store,
        embedding=embedding,
    )

    async with processing_factory() as session:
        documents = list(
            (
                await session.scalars(
                    select(KnowledgeDocument).order_by(KnowledgeDocument.version)
                )
            ).all()
        )
        chunks = list((await session.scalars(select(KnowledgeChunk))).all())
    assert [document.status for document in documents] == [
        DocumentStatus.SUPERSEDED.value,
        DocumentStatus.READY.value,
    ]
    assert documents[0].active is False
    assert documents[1].active is True
    assert chunks
    assert all(chunk.embedding_model == "fake-lexical-v1" for chunk in chunks)
    assert first.id in vector_store.deleted
    assert second.id in vector_store.documents
    assert any(key.endswith("parsed/pages.json") for key in storage.objects)


@pytest.mark.asyncio
async def test_processing_marks_textless_pdf_as_ocr_required(
    processing_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("erp_assistant.rag.service.SessionFactory", processing_factory)
    storage = MemoryStorage()
    vector_store = MemoryVectorStore()
    document = await _create(processing_factory, storage, make_pdf([""]), key="scan")
    await process_document(
        document.id,
        settings=Settings(rag_enabled=True),
        storage=storage,
        vector_store=vector_store,
        embedding=FakeEmbeddingProvider(),
    )
    async with processing_factory() as session:
        refreshed = await session.get(KnowledgeDocument, document.id)
    assert refreshed is not None
    assert refreshed.status == DocumentStatus.OCR_REQUIRED.value
    assert refreshed.error_code == "OCR_REQUIRED"
    assert document.id not in vector_store.documents


@pytest.mark.asyncio
async def test_rebuild_active_index_is_idempotent(
    processing_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vector_store = MemoryVectorStore()
    document = await _create(
        processing_factory,
        storage,
        make_pdf(["Inventory policy. Count stock every day. " * 8]),
    )
    async with processing_factory() as session:
        current = await session.get(KnowledgeDocument, document.id)
        assert current is not None
        current.status = DocumentStatus.READY.value
        current.active = True
        chunk = KnowledgeChunk(
            id=UUID("11111111-1111-1111-1111-111111111111"),
            document_id=current.id,
            scope=current.scope,
            scope_id=current.scope_id,
            version=current.version,
            chunk_index=0,
            page_start=1,
            page_end=1,
            content="inventory policy",
            content_hash="a" * 64,
            char_count=16,
            embedding_model="fake-lexical-v1",
        )
        session.add(chunk)
        await session.commit()
        first = await rebuild_active_index(session, vector_store, FakeEmbeddingProvider())
        second = await rebuild_active_index(session, vector_store, FakeEmbeddingProvider())

    assert first == second
    assert first.document_count == 1
    assert first.chunk_count == 1
    assert vector_store.documents[document.id] == [chunk.id]


async def _create(
    factory: async_sessionmaker[AsyncSession],
    storage: MemoryStorage,
    content: bytes,
    key: str = "inventory-sop",
) -> KnowledgeDocument:
    async with factory() as session:
        result = await create_document(
            session,
            storage,
            lambda document_id: f"task-{document_id}",
            content=content,
            page_count=1,
            tenant_id="tenant-a",
            operator_id="user-a",
            scope=KnowledgeScope.TENANT,
            document_key=key,
            title=key,
            original_filename=f"{key}.pdf",
            media_type="application/pdf",
        )
        return result.document
