from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.infrastructure.database import Base
from erp_assistant.models.knowledge import (
    KnowledgeCleanupRequest,
    KnowledgeCorpusHead,
    KnowledgeDocument,
)
from erp_assistant.rag.cleanup_executor import (
    CleanupDecision,
    CleanupRetryableError,
    KnowledgeCleanupExecutor,
)
from erp_assistant.rag.document_indexer import DocumentIndexOutcome
from erp_assistant.rag.knowledge_corpus import SYSTEM_CORPUS_ACTOR, KnowledgeCorpus
from erp_assistant.rag.types import (
    CleanupStatus,
    DataClassification,
    DocumentStatus,
    RetentionStatus,
)
from tests.identity_factory import corpus_authorizer

_KnowledgeCorpus = KnowledgeCorpus


def KnowledgeCorpus(**kwargs: object):  # type: ignore[no-redef]
    return _KnowledgeCorpus(authorizer=corpus_authorizer(), **kwargs)


class UnusedProcessor:
    async def index(
        self, document_id: UUID, *, trace_id: UUID | None = None
    ) -> DocumentIndexOutcome:
        del trace_id
        del document_id
        raise AssertionError("cleanup must not invoke document processing")


@dataclass
class RecordingStorage:
    deleted: list[str] = field(default_factory=list)

    def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        del key, content, content_type

    def get_bytes(self, key: str) -> bytes:
        del key
        return b""

    def delete_object(self, key: str) -> None:
        self.deleted.append(key)


@dataclass
class RecordingVectors:
    identity: str = "recording-v1"
    deleted: list[UUID] = field(default_factory=list)

    def upsert_document(
        self,
        document_id: UUID,
        scope: str,
        scope_id: str,
        version: int,
        chunks: list[object],
        vectors: list[list[float]],
    ) -> None:
        del document_id, scope, scope_id, version, chunks, vectors

    def delete_document(self, document_id: UUID) -> None:
        self.deleted.append(document_id)



@dataclass
class FlakyVectors(RecordingVectors):
    failures_remaining: int = 1

    def delete_document(self, document_id: UUID) -> None:
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary vector failure")
        super().delete_document(document_id)


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
async def test_cleanup_executor_deletes_artifacts_and_completes_through_corpus(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = uuid4()
    vector_id = uuid4()
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="inventory-sop",
        version=1,
        title="Inventory SOP",
        original_filename="inventory.pdf",
        media_type="application/pdf",
        data_classification=DataClassification.RESTRICTED.value,
        size_bytes=100,
        sha256="a" * 64,
        object_key="knowledge/inventory/original.pdf",
        parsed_object_key="knowledge/inventory/parsed.json",
        vector_document_id=vector_id,
        status=DocumentStatus.READY.value,
        active=False,
        publication_status="WITHDRAWN",
        retention_status=RetentionStatus.PURGE_REQUESTED.value,
        index_artifacts_present=True,
        created_by="author-a",
    )
    cleanup = KnowledgeCleanupRequest(
        document_id=document_id,
        idempotency_key=f"{document_id}:VECTOR_AND_OBJECT:1",
        kind="VECTOR_AND_OBJECT",
        status=CleanupStatus.PENDING.value,
        artifacts=[
            {"kind": "vector_document", "document_id": str(vector_id)},
            {"kind": "object", "object_key": document.object_key},
            {"kind": "object", "object_key": document.parsed_object_key},
        ],
    )
    async with session_factory() as session:
        session.add_all(
            [
                document,
                KnowledgeCorpusHead(
                    scope=document.scope,
                    scope_id=document.scope_id,
                    document_key=document.document_key,
                    revision=1,
                ),
                cleanup,
            ]
        )
        await session.commit()
    corpus = KnowledgeCorpus(
        session_factory=session_factory,
        processor=UnusedProcessor(),
    )
    storage = RecordingStorage()
    ordinary_vectors = RecordingVectors(identity="ordinary-vectors")
    restricted_vectors_v1 = RecordingVectors(identity="restricted-vectors-v1")
    restricted_vectors_v2 = RecordingVectors(identity="restricted-vectors-v2")
    executor = KnowledgeCleanupExecutor(
        session_factory=session_factory,
        storage=storage,
        vectors=ordinary_vectors,  # type: ignore[arg-type]
        vectors_by_classification={
            DataClassification.ORDINARY: ordinary_vectors,  # type: ignore[dict-item]
            DataClassification.RESTRICTED: (
                restricted_vectors_v1,
                restricted_vectors_v2,
            ),  # type: ignore[dict-item]
        },
        corpus=corpus,
    )

    outcome = await executor.execute(document_id)

    assert outcome.decision is CleanupDecision.COMPLETED
    assert ordinary_vectors.deleted == []
    assert restricted_vectors_v1.deleted == [vector_id]
    assert restricted_vectors_v2.deleted == [vector_id]
    assert storage.deleted == [document.object_key, document.parsed_object_key]
    view = await corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
    assert view.retention_status is RetentionStatus.PURGED
    assert view.cleanup_requests[-1].status is CleanupStatus.COMPLETED
    assert view.cleanup_requests[-1].attempt == 1
    assert view.audit_events[-1].action == "COMPLETE_CLEANUP"
    assert (await executor.execute(document_id)).decision is CleanupDecision.SKIPPED


@pytest.mark.asyncio
async def test_cleanup_executor_reclaims_failed_request_on_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = uuid4()
    vector_id = uuid4()
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="inventory-sop",
        version=1,
        title="Inventory SOP",
        original_filename="inventory.pdf",
        media_type="application/pdf",
        data_classification=DataClassification.ORDINARY.value,
        size_bytes=100,
        sha256="a" * 64,
        object_key="knowledge/inventory/original.pdf",
        vector_document_id=vector_id,
        status=DocumentStatus.READY.value,
        active=False,
        publication_status="WITHDRAWN",
        retention_status=RetentionStatus.RETAINED.value,
        index_artifacts_present=True,
        created_by="author-a",
    )
    cleanup = KnowledgeCleanupRequest(
        document_id=document_id,
        idempotency_key=f"{document_id}:VECTOR:1",
        kind="VECTOR",
        status=CleanupStatus.PENDING.value,
        artifacts=[{"kind": "vector_document", "document_id": str(vector_id)}],
    )
    async with session_factory() as session:
        session.add_all(
            [
                document,
                KnowledgeCorpusHead(
                    scope=document.scope,
                    scope_id=document.scope_id,
                    document_key=document.document_key,
                    revision=1,
                ),
                cleanup,
            ]
        )
        await session.commit()
    corpus = KnowledgeCorpus(session_factory=session_factory, processor=UnusedProcessor())
    vectors = FlakyVectors()
    executor = KnowledgeCleanupExecutor(
        session_factory=session_factory,
        storage=RecordingStorage(),
        vectors=vectors,  # type: ignore[arg-type]
        corpus=corpus,
    )

    with pytest.raises(CleanupRetryableError):
        await executor.execute(document_id)
    failed = await corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
    assert failed.cleanup_requests[-1].status is CleanupStatus.FAILED
    assert failed.cleanup_requests[-1].attempt == 1

    outcome = await executor.execute(document_id)

    assert outcome.decision is CleanupDecision.COMPLETED
    assert vectors.deleted == [vector_id]
    completed = await corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
    assert completed.cleanup_requests[-1].status is CleanupStatus.COMPLETED
    assert completed.cleanup_requests[-1].attempt == 2


@pytest.mark.asyncio
async def test_cleanup_executor_marks_missing_classification_route_as_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = uuid4()
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="unclassified-sop",
        version=1,
        title="Unclassified SOP",
        original_filename="sop.pdf",
        media_type="application/pdf",
        data_classification=DataClassification.UNCLASSIFIED.value,
        size_bytes=100,
        sha256="c" * 64,
        object_key="knowledge/unclassified/original.pdf",
        vector_document_id=document_id,
        status=DocumentStatus.READY.value,
        active=False,
        publication_status="WITHDRAWN",
        retention_status=RetentionStatus.RETAINED.value,
        index_artifacts_present=True,
        created_by="legacy-import",
    )
    cleanup = KnowledgeCleanupRequest(
        document_id=document_id,
        idempotency_key=f"{document_id}:VECTOR:1",
        kind="VECTOR",
        status=CleanupStatus.PENDING.value,
        artifacts=[{"kind": "vector_document", "document_id": str(document_id)}],
    )
    async with session_factory() as session:
        session.add_all(
            [
                document,
                KnowledgeCorpusHead(
                    scope=document.scope,
                    scope_id=document.scope_id,
                    document_key=document.document_key,
                    revision=1,
                ),
                cleanup,
            ]
        )
        await session.commit()
    corpus = KnowledgeCorpus(session_factory=session_factory, processor=UnusedProcessor())
    vectors = RecordingVectors(identity="ordinary-vectors")
    executor = KnowledgeCleanupExecutor(
        session_factory=session_factory,
        storage=RecordingStorage(),
        vectors=vectors,  # type: ignore[arg-type]
        vectors_by_classification={
            DataClassification.ORDINARY: vectors,  # type: ignore[dict-item]
        },
        corpus=corpus,
    )

    with pytest.raises(CleanupRetryableError):
        await executor.execute(document_id)

    failed = await corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
    assert failed.cleanup_requests[-1].status is CleanupStatus.FAILED
    assert failed.cleanup_requests[-1].attempt == 1


@pytest.mark.asyncio
async def test_cleanup_executor_fails_closed_when_historical_index_route_is_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    document_id = uuid4()
    vector_id = uuid4()
    document = KnowledgeDocument(
        id=document_id,
        scope="TENANT",
        scope_id="tenant-a",
        tenant_id="tenant-a",
        document_key="historical-index-sop",
        version=1,
        title="Historical Index SOP",
        original_filename="sop.pdf",
        media_type="application/pdf",
        data_classification=DataClassification.ORDINARY.value,
        size_bytes=100,
        sha256="e" * 64,
        object_key="knowledge/historical/original.pdf",
        vector_document_id=vector_id,
        status=DocumentStatus.READY.value,
        active=False,
        publication_status="WITHDRAWN",
        retention_status=RetentionStatus.RETAINED.value,
        index_artifacts_present=True,
        created_by="test-author",
    )
    cleanup = KnowledgeCleanupRequest(
        document_id=document_id,
        idempotency_key=f"{document_id}:VECTOR:1",
        kind="VECTOR",
        status=CleanupStatus.PENDING.value,
        artifacts=[
            {
                "kind": "vector_document",
                "document_id": str(vector_id),
                "index_identity": "retired-index-v1",
            }
        ],
    )
    async with session_factory() as session:
        session.add_all(
            [
                document,
                KnowledgeCorpusHead(
                    scope=document.scope,
                    scope_id=document.scope_id,
                    document_key=document.document_key,
                    revision=1,
                ),
                cleanup,
            ]
        )
        await session.commit()
    corpus = KnowledgeCorpus(session_factory=session_factory, processor=UnusedProcessor())
    configured = RecordingVectors(identity="current-index-v2")
    executor = KnowledgeCleanupExecutor(
        session_factory=session_factory,
        storage=RecordingStorage(),
        vectors=configured,  # type: ignore[arg-type]
        vectors_by_classification={
            DataClassification.ORDINARY: configured,  # type: ignore[dict-item]
        },
        corpus=corpus,
    )

    with pytest.raises(CleanupRetryableError):
        await executor.execute(document_id)

    failed = await corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
    assert configured.deleted == []
    assert failed.cleanup_requests[-1].status is CleanupStatus.FAILED
    assert failed.cleanup_requests[-1].artifacts == tuple(cleanup.artifacts)
