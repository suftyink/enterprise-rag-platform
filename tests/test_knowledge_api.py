from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from erp_assistant.core.config import Settings, get_settings
from erp_assistant.infrastructure.database import Base, get_session
from erp_assistant.infrastructure.object_storage import get_object_storage
from erp_assistant.main import app
from erp_assistant.models.knowledge import KnowledgeChunk, KnowledgeDocument
from erp_assistant.rag.types import DocumentStatus
from erp_assistant.rag.vector_store import VectorHit, get_vector_store
from erp_assistant.workers.dispatcher import get_document_dispatcher
from tests.pdf_factory import make_pdf

KnowledgeApiContext = tuple[
    AsyncClient,
    async_sessionmaker[AsyncSession],
    "FakeStorage",
    "FakeVectorStore",
]


@dataclass
class FakeStorage:
    objects: dict[str, bytes] = field(default_factory=dict)

    def put_bytes(self, key: str, content: bytes, content_type: str) -> None:
        del content_type
        self.objects[key] = content

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


@dataclass
class FakeVectorStore:
    hits: list[VectorHit] = field(default_factory=list)

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
        del document_id

    def search(
        self,
        query: str,
        vector: list[float],
        tenant_id: str,
        limit: int,
        rrf_k: int,
    ) -> list[VectorHit]:
        del query, vector, tenant_id, rrf_k
        return self.hits[:limit]


@pytest_asyncio.fixture
async def knowledge_api() -> AsyncIterator[KnowledgeApiContext]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = FakeStorage()
    vector_store = FakeVectorStore()
    settings = Settings(rag_enabled=True)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_vector_store] = lambda: vector_store
    app.dependency_overrides[get_document_dispatcher] = (
        lambda: lambda document_id: f"task-{document_id}"
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, storage, vector_store
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def _upload(
    client: AsyncClient,
    *,
    content: bytes | None = None,
    tenant_id: str = "tenant-a",
    operator_id: str = "user-a",
    scope: str = "TENANT",
    document_key: str = "inventory-sop",
) -> Response:
    return await client.post(
        "/api/assistant/v1/knowledge/documents",
        files={
            "file": (
                "inventory.pdf",
                content
                or make_pdf(["Inventory stock policy contains enough searchable text. " * 4]),
                "application/pdf",
            )
        },
        data={
            "tenantId": tenant_id,
            "operatorId": operator_id,
            "scope": scope,
            "documentKey": document_key,
        },
    )


@pytest.mark.asyncio
async def test_upload_status_and_hash_deduplication(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, _, storage, _ = knowledge_api
    first = await _upload(client)
    second = await _upload(client)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert first.json()["documentId"] == second.json()["documentId"]
    assert len(storage.objects) == 1

    status_response = await client.get(
        f"/api/assistant/v1/knowledge/documents/{first.json()['documentId']}",
        params={"tenantId": "tenant-a", "operatorId": "user-a"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "QUEUED"
    hidden = await client.get(
        f"/api/assistant/v1/knowledge/documents/{first.json()['documentId']}",
        params={"tenantId": "tenant-b", "operatorId": "user-b"},
    )
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_upload_validation_and_global_gate(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, _, _, _ = knowledge_api
    wrong_type = await client.post(
        "/api/assistant/v1/knowledge/documents",
        files={"file": ("note.txt", b"not pdf", "text/plain")},
        data={"tenantId": "tenant-a", "operatorId": "user-a"},
    )
    assert wrong_type.status_code == 415
    global_upload = await _upload(client, scope="GLOBAL")
    assert global_upload.status_code == 403


@pytest.mark.asyncio
async def test_retry_failed_but_not_ocr_document(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, factory, _, _ = knowledge_api
    uploaded = await _upload(client)
    document_id = UUID(uploaded.json()["documentId"])
    async with factory() as session:
        document = await session.get(KnowledgeDocument, document_id)
        assert document is not None
        document.status = DocumentStatus.FAILED.value
        await session.commit()
    retried = await client.post(
        f"/api/assistant/v1/knowledge/documents/{document_id}/retry",
        json={"tenantId": "tenant-a", "operatorId": "user-a"},
    )
    assert retried.status_code == 202
    assert retried.json()["status"] == "QUEUED"

    async with factory() as session:
        document = await session.get(KnowledgeDocument, document_id)
        assert document is not None
        document.status = DocumentStatus.OCR_REQUIRED.value
        await session.commit()
    ocr_retry = await client.post(
        f"/api/assistant/v1/knowledge/documents/{document_id}/retry",
        json={"tenantId": "tenant-a", "operatorId": "user-a"},
    )
    assert ocr_retry.status_code == 409


@pytest.mark.asyncio
async def test_search_filters_other_tenant_and_includes_global(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, factory, _, vector_store = knowledge_api
    tenant_document = _ready_document("TENANT", "tenant-a", "tenant-doc")
    other_document = _ready_document("TENANT", "tenant-b", "other-doc")
    global_document = _ready_document("GLOBAL", "GLOBAL", "global-doc")
    chunks = [
        _chunk(tenant_document, 0, "tenant inventory policy"),
        _chunk(other_document, 0, "secret tenant inventory"),
        _chunk(global_document, 0, "global inventory policy"),
    ]
    async with factory() as session:
        session.add_all([tenant_document, other_document, global_document, *chunks])
        await session.commit()
    vector_store.hits = [
        VectorHit(chunk_id=chunks[1].id, score=0.99),
        VectorHit(chunk_id=chunks[0].id, score=0.90),
        VectorHit(chunk_id=chunks[2].id, score=0.80),
    ]
    response = await client.post(
        "/api/assistant/v1/knowledge/search",
        json={
            "tenantId": "tenant-a",
            "operatorId": "user-a",
            "query": "inventory policy",
            "topK": 5,
        },
    )
    assert response.status_code == 200
    assert [item["documentKey"] for item in response.json()["results"]] == [
        "tenant-doc",
        "global-doc",
    ]
    assert response.json()["decision"] == "MATCHED"
    pipeline = response.json()["pipeline"]
    config_fingerprint = pipeline.pop("configFingerprint")
    assert pipeline == {
        "retrieval": "dense-bm25-rrf-v1",
        "embeddingModel": "fake-lexical-v1",
        "rerankModel": "fake-lexical-rerank-v2",
        "threshold": 0.1,
    }
    assert len(config_fingerprint) == 64
    assert response.json()["elapsedMs"] >= 0
    assert response.json()["results"][0]["retrievalRank"] == 2
    assert 0 <= response.json()["results"][0]["score"] <= 1
    assert all(item["pageStart"] == 1 for item in response.json()["results"])


@pytest.mark.asyncio
async def test_search_returns_insufficient_knowledge_below_threshold(
    knowledge_api: KnowledgeApiContext,
) -> None:
    client, factory, _, vector_store = knowledge_api
    document = _ready_document("TENANT", "tenant-a", "inventory-doc")
    chunk = _chunk(document, 0, "inventory policy")
    async with factory() as session:
        session.add_all([document, chunk])
        await session.commit()
    vector_store.hits = [VectorHit(chunk_id=chunk.id, score=0.99)]

    response = await client.post(
        "/api/assistant/v1/knowledge/search",
        json={
            "tenantId": "tenant-a",
            "operatorId": "user-a",
            "query": "completely unrelated question",
            "topK": 5,
        },
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "INSUFFICIENT_KNOWLEDGE"
    assert response.json()["results"] == []


def _ready_document(scope: str, scope_id: str, key: str) -> KnowledgeDocument:
    document_id = uuid4()
    return KnowledgeDocument(
        id=document_id,
        scope=scope,
        scope_id=scope_id,
        tenant_id="tenant-a" if scope == "GLOBAL" else scope_id,
        document_key=key,
        version=1,
        title=key,
        original_filename=f"{key}.pdf",
        media_type="application/pdf",
        size_bytes=100,
        sha256=uuid4().hex + uuid4().hex,
        object_key=f"knowledge/{document_id}/original.pdf",
        page_count=1,
        status=DocumentStatus.READY.value,
        active=True,
        created_by="user-a",
    )


def _chunk(document: KnowledgeDocument, index: int, content: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        document_id=document.id,
        scope=document.scope,
        scope_id=document.scope_id,
        version=document.version,
        chunk_index=index,
        page_start=1,
        page_end=1,
        content=content,
        content_hash=uuid4().hex + uuid4().hex,
        char_count=len(content),
        embedding_model="fake-lexical-v1",
    )
