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
from erp_assistant.rag.cleanup_executor import CleanupDecision, KnowledgeCleanupExecutor
from erp_assistant.rag.document_indexer import DocumentIndexOutcome
from erp_assistant.rag.knowledge_corpus import SYSTEM_CORPUS_ACTOR, KnowledgeCorpus
from erp_assistant.rag.types import CleanupStatus, DocumentStatus, RetentionStatus


class UnusedProcessor:
    async def index(self, document_id: UUID) -> DocumentIndexOutcome:
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
    vectors = RecordingVectors()
    executor = KnowledgeCleanupExecutor(
        session_factory=session_factory,
        storage=storage,
        vectors=vectors,  # type: ignore[arg-type]
        corpus=corpus,
    )

    outcome = await executor.execute(document_id)

    assert outcome.decision is CleanupDecision.COMPLETED
    assert vectors.deleted == [vector_id]
    assert storage.deleted == [document.object_key, document.parsed_object_key]
    view = await corpus.inspect(document_id, SYSTEM_CORPUS_ACTOR)
    assert view.retention_status is RetentionStatus.PURGED
    assert view.cleanup_requests[-1].status is CleanupStatus.COMPLETED
    assert view.cleanup_requests[-1].attempt == 1
    assert view.audit_events[-1].action == "COMPLETE_CLEANUP"
    assert (await executor.execute(document_id)).decision is CleanupDecision.SKIPPED
