import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from erp_assistant.core.config import Settings
from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexJob,
)
from erp_assistant.orchestration.fake_providers import FakeEmbeddingProvider
from erp_assistant.orchestration.providers import EmbeddingProvider
from erp_assistant.rag.chunker import ChunkDraft
from erp_assistant.rag.document_indexer import (
    DocumentIndexer,
    DocumentIndexRetryableError,
    IndexDecision,
)
from erp_assistant.rag.pdf_parser import PypdfParser
from erp_assistant.rag.reindex import reindex_active_documents
from erp_assistant.rag.types import DocumentStatus, IndexJobStatus
from tests.pdf_factory import make_pdf


@dataclass
class MemoryStorage:
    objects: dict[str, bytes] = field(default_factory=dict)
    fail_reads: bool = False

    def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = content

    def get_bytes(self, key: str) -> bytes:
        if self.fail_reads:
            raise TimeoutError("storage unavailable")
        return self.objects[key]


@dataclass
class MemoryVectorWriter:
    identity: str = "memory-index-v1"
    documents: dict[UUID, list[UUID]] = field(default_factory=dict)
    deleted: list[UUID] = field(default_factory=list)
    upsert_count: int = 0
    fail_writes: bool = False
    write_then_fail: bool = False
    fail_deletes: bool = False

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
        self.upsert_count += 1
        if self.fail_writes:
            raise TimeoutError("vector index unavailable")
        assert len(chunks) == len(vectors)
        self.documents[document_id] = [chunk.id for chunk in chunks]
        if self.write_then_fail:
            raise TimeoutError("vector response lost after write")

    def delete_document(self, document_id: UUID) -> None:
        if self.fail_deletes:
            raise TimeoutError("vector cleanup unavailable")
        self.deleted.append(document_id)
        self.documents.pop(document_id, None)


@dataclass
class CommitFailure:
    fail_on: int
    calls: int = 0
    triggered: bool = False


class OneShotFailingSession(AsyncSession):
    async def commit(self) -> None:
        failure = self.info.get("commit_failure")
        assert isinstance(failure, CommitFailure)
        failure.calls += 1
        if failure.calls == failure.fail_on and not failure.triggered:
            failure.triggered = True
            raise OperationalError("COMMIT", {}, RuntimeError("database unavailable"))
        await super().commit()


class ReadFailingSession(AsyncSession):
    async def get(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OperationalError("SELECT", {}, RuntimeError("database unavailable"))


class FailingEmbeddingProvider:
    dimension = 256
    model = "failing-embedding-v1"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise TimeoutError("embedding provider unavailable")


class BlockingFirstEmbeddingProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
        return await super().embed(texts)


class SimulatedWorkerLoss(BaseException):
    pass


class WorkerLossVectorWriter(MemoryVectorWriter):
    def delete_document(self, document_id: UUID) -> None:
        del document_id
        raise SimulatedWorkerLoss


@pytest_asyncio.fixture
async def indexer_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "indexer.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _indexer(
    factory: async_sessionmaker[AsyncSession],
    storage: MemoryStorage,
    vectors: MemoryVectorWriter,
    embedding: EmbeddingProvider | None = None,
    lease_seconds: int = 900,
) -> DocumentIndexer:
    return DocumentIndexer(
        session_factory=factory,
        storage=storage,
        parser=PypdfParser(),
        embedding=embedding or FakeEmbeddingProvider(),
        vector_writer=vectors,
        settings=Settings(
            rag_enabled=True,
            knowledge_min_text_per_page=5,
            knowledge_chunk_target=120,
            knowledge_chunk_max=180,
            knowledge_chunk_overlap=20,
            knowledge_index_lease_seconds=lease_seconds,
        ),
    )


async def _add_document(
    factory: async_sessionmaker[AsyncSession],
    storage: MemoryStorage,
    *,
    version: int,
    content: bytes,
    status: DocumentStatus,
    active: bool,
) -> KnowledgeDocument:
    document_id = uuid4()
    object_key = f"knowledge/tenant/tenant-a/inventory-sop/v{version}/{document_id}/original.pdf"
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="inventory-sop",
        version=version,
        title="Inventory SOP",
        original_filename="inventory.pdf",
        media_type="application/pdf",
        size_bytes=len(content),
        sha256=f"{version:064x}",
        object_key=object_key,
        page_count=1,
        status=status.value,
        active=active,
        publication_status="PUBLISHED" if active else "UNPUBLISHED",
        retention_status="RETAINED",
        index_artifacts_present=status == DocumentStatus.READY,
        created_by="user-a",
    )
    async with factory() as session:
        session.add(document)
        if status == DocumentStatus.QUEUED:
            session.add(
                KnowledgeIndexJob(
                    document_id=document.id,
                    status=IndexJobStatus.QUEUED.value,
                    phase=DocumentStatus.QUEUED.value,
                )
            )
        await session.commit()
    storage.objects[object_key] = content
    return document


@pytest.mark.asyncio
async def test_index_indexes_pdf_without_publishing_new_version(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter()
    old = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Old inventory policy content. " * 8]),
        status=DocumentStatus.READY,
        active=True,
    )
    new = await _add_document(
        indexer_factory,
        storage,
        version=2,
        content=make_pdf(["New inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    vectors.documents[old.id] = [uuid4()]

    outcome = await _indexer(indexer_factory, storage, vectors).index(new.id)

    assert outcome.document_id == new.id
    assert outcome.decision == IndexDecision.INDEXED_FOR_CURRENT_POLICY
    assert outcome.status == DocumentStatus.READY
    assert outcome.chunk_count > 0
    assert outcome.warnings == ()
    assert outcome.attempt == 1
    assert len(outcome.pipeline.revision) == 64
    async with indexer_factory() as session:
        documents = list(
            (
                await session.scalars(
                    select(KnowledgeDocument).order_by(KnowledgeDocument.version)
                )
            ).all()
        )
        chunks = list(
            (
                await session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.document_id == new.id)
                )
            ).all()
        )
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == new.id)
        )
    assert [document.status for document in documents] == [
        DocumentStatus.READY.value,
        DocumentStatus.READY.value,
    ]
    assert [document.active for document in documents] == [True, False]
    assert len(chunks) == outcome.chunk_count
    assert all(chunk.embedding_model == "fake-lexical-v1" for chunk in chunks)
    assert job is not None
    assert job.status == IndexJobStatus.SUCCEEDED.value
    assert job.phase == DocumentStatus.READY.value
    assert job.chunk_count == outcome.chunk_count
    assert job.pipeline_revision == outcome.pipeline.revision
    assert old.id not in vectors.deleted
    assert documents[1].vector_document_id in vectors.documents
    assert any(key.endswith("parsed/pages.json") for key in storage.objects)


@pytest.mark.asyncio
async def test_index_skips_identical_source_and_pipeline_without_duplicate_writes(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Stable inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    indexer = _indexer(indexer_factory, storage, vectors)

    first = await indexer.index(document.id)
    second = await indexer.index(document.id)

    assert first.decision == IndexDecision.INDEXED_FOR_CURRENT_POLICY
    assert second.decision == IndexDecision.SKIPPED
    assert second.attempt == 1
    assert second.chunk_count == first.chunk_count
    assert vectors.upsert_count == 1
    async with indexer_factory() as session:
        chunks = list(
            (
                await session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
                )
            ).all()
        )
    assert len(chunks) == first.chunk_count


@pytest.mark.asyncio
async def test_index_atomically_claims_duplicate_deliveries(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Concurrent inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    indexer = _indexer(indexer_factory, storage, vectors)

    outcomes = await asyncio.gather(indexer.index(document.id), indexer.index(document.id))

    assert [outcome.decision for outcome in outcomes].count(
        IndexDecision.INDEXED_FOR_CURRENT_POLICY
    ) == 1
    assert [outcome.decision for outcome in outcomes].count(IndexDecision.SKIPPED) == 1
    assert vectors.upsert_count == 1


@pytest.mark.asyncio
async def test_index_reclaims_an_expired_running_attempt(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Recoverable inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    async with indexer_factory() as session:
        stored = await session.get_one(KnowledgeDocument, document.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document.id)
        )
        assert job is not None
        stored.status = DocumentStatus.PARSING.value
        job.status = IndexJobStatus.RUNNING.value
        job.attempt = 1
        job.started_at = datetime.now(UTC) - timedelta(hours=1)
        job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=30)
        await session.commit()

    outcome = await _indexer(indexer_factory, storage, vectors).index(document.id)

    assert outcome.decision == IndexDecision.INDEXED_FOR_CURRENT_POLICY
    assert outcome.attempt == 2


@pytest.mark.asyncio
async def test_expired_worker_is_fenced_before_vector_write_after_takeover(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter()
    embedding = BlockingFirstEmbeddingProvider()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Fenced inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    indexer = _indexer(
        indexer_factory,
        storage,
        vectors,
        embedding,
        lease_seconds=0,
    )

    expired_worker = asyncio.create_task(indexer.index(document.id))
    await embedding.started.wait()
    takeover = await indexer.index(document.id)
    embedding.release.set()
    stale = await expired_worker

    assert takeover.decision == IndexDecision.INDEXED_FOR_CURRENT_POLICY
    assert takeover.attempt == 2
    assert stale.decision == IndexDecision.SKIPPED
    assert stale.attempt == 2
    assert vectors.upsert_count == 1


@pytest.mark.asyncio
async def test_index_returns_ocr_required_and_persists_diagnostic_state(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf([""]),
        status=DocumentStatus.QUEUED,
        active=False,
    )

    outcome = await _indexer(indexer_factory, storage, MemoryVectorWriter()).index(document.id)

    assert outcome.decision == IndexDecision.OCR_REQUIRED
    assert outcome.status == DocumentStatus.OCR_REQUIRED
    assert outcome.error_code == "OCR_REQUIRED"
    async with indexer_factory() as session:
        stored = await session.get(KnowledgeDocument, document.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document.id)
        )
    assert stored is not None and stored.status == DocumentStatus.OCR_REQUIRED.value
    assert stored.active is False
    assert stored.error_code == "OCR_REQUIRED"
    assert job is not None and job.error_code == "OCR_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        (b"%PDF-invalid", "INVALID_PDF"),
        (b"encrypted", "ENCRYPTED_PDF"),
    ],
)
async def test_index_returns_stable_permanent_pdf_failure(
    indexer_factory: async_sessionmaker[AsyncSession],
    content: bytes,
    expected_code: str,
) -> None:
    if content == b"encrypted":
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.encrypt("secret")
        output = BytesIO()
        writer.write(output)
        content = output.getvalue()
    storage = MemoryStorage()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=content,
        status=DocumentStatus.QUEUED,
        active=False,
    )

    outcome = await _indexer(indexer_factory, storage, MemoryVectorWriter()).index(document.id)

    assert outcome.decision == IndexDecision.PERMANENT_FAILURE
    assert outcome.status == DocumentStatus.FAILED
    assert outcome.error_code == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_at", "expected_code"),
    [
        ("storage", "BLOB_READ_FAILED"),
        ("embedding", "EMBEDDING_FAILED"),
        ("vector", "VECTOR_WRITE_FAILED"),
    ],
)
async def test_index_raises_typed_retryable_error_and_persists_attempt(
    indexer_factory: async_sessionmaker[AsyncSession],
    fail_at: str,
    expected_code: str,
) -> None:
    storage = MemoryStorage(fail_reads=fail_at == "storage")
    vectors = MemoryVectorWriter(fail_writes=fail_at == "vector")
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Retryable inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )

    embedding = FailingEmbeddingProvider() if fail_at == "embedding" else None
    with pytest.raises(DocumentIndexRetryableError) as exc_info:
        await _indexer(indexer_factory, storage, vectors, embedding).index(document.id)

    assert exc_info.value.code == expected_code
    assert exc_info.value.attempt == 1
    async with indexer_factory() as session:
        stored = await session.get(KnowledgeDocument, document.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document.id)
        )
    assert stored is not None and stored.status == DocumentStatus.FAILED.value
    assert stored.active is False
    assert stored.error_code == expected_code
    assert job is not None and job.attempt == 1
    assert job.error_code == expected_code


@pytest.mark.asyncio
async def test_index_classifies_initial_database_read_failure_as_retryable(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    failing_factory = async_sessionmaker(
        indexer_factory.kw["bind"],
        class_=ReadFailingSession,
        expire_on_commit=False,
    )

    with pytest.raises(DocumentIndexRetryableError) as exc_info:
        await _indexer(failing_factory, MemoryStorage(), MemoryVectorWriter()).index(uuid4())

    assert exc_info.value.code == "DATABASE_READ_FAILED"
    assert exc_info.value.attempt == 0


@pytest.mark.asyncio
async def test_index_recovers_chunks_after_vector_failure_and_preserves_old_version(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter(fail_writes=True)
    old = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Old usable policy content. " * 8]),
        status=DocumentStatus.READY,
        active=True,
    )
    new = await _add_document(
        indexer_factory,
        storage,
        version=2,
        content=make_pdf(["Replacement policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    indexer = _indexer(indexer_factory, storage, vectors)

    with pytest.raises(DocumentIndexRetryableError, match="临时故障"):
        await indexer.index(new.id)
    async with indexer_factory() as session:
        stored_old = await session.get_one(KnowledgeDocument, old.id)
        stored_new = await session.get_one(KnowledgeDocument, new.id)
        chunks_after_failure = list(
            (
                await session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.document_id == new.id)
                )
            ).all()
        )
    assert stored_old.active is True
    assert stored_old.status == DocumentStatus.READY.value
    assert stored_new.active is False
    assert chunks_after_failure

    vectors.fail_writes = False
    outcome = await indexer.index(new.id)

    assert outcome.decision == IndexDecision.INDEXED_FOR_CURRENT_POLICY
    assert outcome.attempt == 2
    async with indexer_factory() as session:
        chunks_after_recovery = list(
            (
                await session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.document_id == new.id)
                )
            ).all()
        )
    assert [chunk.id for chunk in chunks_after_recovery] == [
        chunk.id for chunk in chunks_after_failure
    ]
    assert old.id not in vectors.deleted
    async with indexer_factory() as session:
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == new.id)
        )
    assert job is not None and job.error_code is None
    assert job.error_detail is None


@pytest.mark.asyncio
async def test_index_records_vector_artifact_when_activation_commit_fails_and_recovers(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter()
    old = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Old usable policy content. " * 8]),
        status=DocumentStatus.READY,
        active=True,
    )
    new = await _add_document(
        indexer_factory,
        storage,
        version=2,
        content=make_pdf(["Replacement policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    failure = CommitFailure(fail_on=5)
    engine = indexer_factory.kw["bind"]
    failing_factory = async_sessionmaker(
        engine,
        class_=OneShotFailingSession,
        expire_on_commit=False,
        info={"commit_failure": failure},
    )

    with pytest.raises(DocumentIndexRetryableError) as exc_info:
        await _indexer(failing_factory, storage, vectors).index(new.id)

    assert exc_info.value.code == "DATABASE_WRITE_FAILED"
    assert len(vectors.documents) == 1
    staged_vector_id = next(iter(vectors.documents))
    async with indexer_factory() as session:
        stored_old = await session.get_one(KnowledgeDocument, old.id)
        stored_new = await session.get_one(KnowledgeDocument, new.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == new.id)
        )
    assert stored_old.active is True
    assert stored_old.status == DocumentStatus.READY.value
    assert stored_new.active is False
    assert job is not None and job.cleanup_required is True
    assert job.cleanup_artifacts == [
        {"kind": "vector_document", "document_id": str(staged_vector_id)}
    ]

    outcome = await _indexer(indexer_factory, storage, vectors).index(new.id)

    assert outcome.status == DocumentStatus.READY
    assert outcome.attempt == 2
    assert old.id not in vectors.deleted


@pytest.mark.asyncio
async def test_index_records_cleanup_when_vector_write_succeeds_before_timeout(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter(write_then_fail=True)
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Partially written policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=False,
    )
    indexer = _indexer(indexer_factory, storage, vectors)

    with pytest.raises(DocumentIndexRetryableError) as exc_info:
        await indexer.index(document.id)

    assert exc_info.value.code == "VECTOR_WRITE_FAILED"
    assert len(vectors.documents) == 1
    staged_vector_id = next(iter(vectors.documents))
    async with indexer_factory() as session:
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document.id)
        )
    assert job is not None and job.cleanup_required is True
    assert job.cleanup_artifacts == [
        {"kind": "vector_document", "document_id": str(staged_vector_id)}
    ]

    vectors.write_then_fail = False
    outcome = await indexer.index(document.id)

    assert outcome.status == DocumentStatus.READY
    assert staged_vector_id in vectors.deleted
    async with indexer_factory() as session:
        stored = await session.get_one(KnowledgeDocument, document.id)
    assert stored.vector_document_id in vectors.documents


@pytest.mark.asyncio
async def test_index_retries_deferred_old_vector_cleanup_without_reindexing(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter(fail_deletes=True)
    new = await _add_document(
        indexer_factory,
        storage,
        version=2,
        content=make_pdf(["Replacement policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=True,
    )
    indexer = _indexer(indexer_factory, storage, vectors)

    with pytest.raises(DocumentIndexRetryableError) as exc_info:
        await indexer.index(new.id)
    assert exc_info.value.code == "VECTOR_CLEANUP_FAILED"
    assert vectors.upsert_count == 1
    async with indexer_factory() as session:
        stored_new = await session.get_one(KnowledgeDocument, new.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == new.id)
        )
    assert stored_new.active is True
    assert stored_new.status == DocumentStatus.READY.value
    assert job is not None and job.status == IndexJobStatus.SUCCEEDED.value
    assert job.cleanup_required is True
    assert "VECTOR_CLEANUP_PENDING" in job.warnings

    vectors.fail_deletes = False
    outcome = await indexer.index(new.id)

    assert outcome.decision == IndexDecision.SKIPPED
    assert outcome.cleanup_required is False
    assert "VECTOR_CLEANUP_PENDING" not in outcome.warnings
    assert vectors.upsert_count == 1
    assert new.id in vectors.deleted


@pytest.mark.asyncio
async def test_index_persists_cleanup_intent_before_external_delete(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = WorkerLossVectorWriter()
    new = await _add_document(
        indexer_factory,
        storage,
        version=2,
        content=make_pdf(["Replacement policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=True,
    )

    with pytest.raises(SimulatedWorkerLoss):
        await _indexer(indexer_factory, storage, vectors).index(new.id)

    async with indexer_factory() as session:
        stored_new = await session.get_one(KnowledgeDocument, new.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == new.id)
        )
    assert stored_new.active is True
    assert stored_new.status == DocumentStatus.READY.value
    assert job is not None and job.cleanup_required is True
    assert job.cleanup_artifacts == [
        {"kind": "vector_document", "document_id": str(new.id)}
    ]


@pytest.mark.asyncio
async def test_reindex_uses_indexer_pipeline_revision_and_reports_idempotent_repeat(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    first_vectors = MemoryVectorWriter(identity="memory-index-v1")
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Active inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=True,
    )
    await _indexer(indexer_factory, storage, first_vectors).index(document.id)
    replacement_vectors = MemoryVectorWriter(identity="memory-index-v2")
    replacement_indexer = _indexer(indexer_factory, storage, replacement_vectors)

    first = await reindex_active_documents(replacement_indexer, indexer_factory)
    second = await reindex_active_documents(replacement_indexer, indexer_factory)

    assert first.document_count == 1
    assert first.indexed_count == 1
    assert first.skipped_count == 0
    assert first.chunk_count > 0
    assert second.document_count == 1
    assert second.indexed_count == 0
    assert second.skipped_count == 1
    assert second.chunk_count == first.chunk_count
    assert replacement_vectors.upsert_count == 1


@pytest.mark.asyncio
async def test_failed_active_reindex_keeps_document_retrievable_and_reports_failure(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Active inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=True,
    )
    await _indexer(
        indexer_factory,
        storage,
        MemoryVectorWriter(identity="memory-index-v1"),
    ).index(document.id)
    storage.objects[document.object_key] = b"%PDF-invalid"
    replacement_indexer = _indexer(
        indexer_factory,
        storage,
        MemoryVectorWriter(identity="memory-index-v2"),
    )

    result = await reindex_active_documents(replacement_indexer, indexer_factory)

    assert result.document_count == 1
    assert result.indexed_count == 0
    assert result.permanent_failure_count == 1
    async with indexer_factory() as session:
        stored = await session.get_one(KnowledgeDocument, document.id)
        job = await session.scalar(
            select(KnowledgeIndexJob).where(KnowledgeIndexJob.document_id == document.id)
        )
    assert stored.active is True
    assert stored.status == DocumentStatus.READY.value
    assert job is not None and job.status == IndexJobStatus.FAILED.value
    assert job.error_code == "INVALID_PDF"


@pytest.mark.asyncio
async def test_active_reindex_vector_failure_preserves_old_chunks_and_vectors(
    indexer_factory: async_sessionmaker[AsyncSession],
) -> None:
    storage = MemoryStorage()
    vectors = MemoryVectorWriter(identity="memory-index-v1")
    document = await _add_document(
        indexer_factory,
        storage,
        version=1,
        content=make_pdf(["Active inventory policy content. " * 8]),
        status=DocumentStatus.QUEUED,
        active=True,
    )
    await _indexer(indexer_factory, storage, vectors).index(document.id)
    async with indexer_factory() as session:
        stored_before = await session.get_one(KnowledgeDocument, document.id)
        old_chunk_ids = list(
            (
                await session.scalars(
                    select(KnowledgeChunk.id).where(
                        KnowledgeChunk.document_id == document.id
                    )
                )
            ).all()
        )
    assert stored_before.vector_document_id is not None
    old_vector_id = stored_before.vector_document_id
    assert old_vector_id in vectors.documents
    vectors.identity = "memory-index-v2"
    vectors.write_then_fail = True

    with pytest.raises(DocumentIndexRetryableError) as exc_info:
        await _indexer(indexer_factory, storage, vectors).index(document.id)

    assert exc_info.value.code == "VECTOR_WRITE_FAILED"
    async with indexer_factory() as session:
        stored = await session.get_one(KnowledgeDocument, document.id)
        current_chunk_ids = list(
            (
                await session.scalars(
                    select(KnowledgeChunk.id).where(
                        KnowledgeChunk.document_id == document.id
                    )
                )
            ).all()
        )
    assert stored.active is True
    assert stored.status == DocumentStatus.READY.value
    assert current_chunk_ids == old_chunk_ids
    assert vectors.documents[old_vector_id] == old_chunk_ids
